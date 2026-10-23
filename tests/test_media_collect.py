# -*- coding: utf-8 -*-
'''
`collect --include-media`: the media of the collected messages are
recorded once each history is committed and downloaded after the last
one, in every storage mode. Through cli.main with fakes (no network,
no credentials, synthetic data).
'''

# import modules
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from unittest import mock

import offline_guard
import virtual_time

from telethon import errors, types

from fakes import FixtureWorld
from media_collect_support import ALPHA, BETA, KIB, MediaCase, MediaClient, photo, record_events, \
	standard_world
from media_download_support import content
from storage_modes_support import file_contents, kill_inside_a_write

from telegram_tracker import collector
from telegram_tracker.media import layout as media_layout
from telegram_tracker.media.collect import input_channel
from telegram_tracker.media.identity import IDENTITY_FILE, folder_identity
from telegram_tracker.media.layout import MEDIA_FOLDER, MediaLayout
from telegram_tracker.media.ledger import open_ledger
from telegram_tracker.media.selection import MediaSelection
from telegram_tracker.storage.records import LOCK_FILE, StorageError
from telegram_tracker.storage.sqlite.connection import DatabaseBusy, InterruptedWrite


MODES = ('files', 'sqlite', 'both')
BATCH = ('alpha_fx', 'beta_fx')
OLD_RUN = '00000000-0000-4000-8000-000000000001'

# what the standard world stores, by (channel, message): (media id, content)
SIZES = {
	(ALPHA, 1): (9001, 3000, 1), (ALPHA, 2): (7001, 600 * KIB, 2), (ALPHA, 4): (7002, 5000, 3),
	(ALPHA, 5): (7003, 7000, 4), (ALPHA, 6): (7001, 600 * KIB, 2), (BETA, 1): (7101, 9000, 5),
	(BETA, 2): (9101, 2000, 6)
}
DOWNLOADED_BYTES = sum(size for key, (_, size, _) in SIZES.items() if key != (ALPHA, 6))
ALPHA_KEYS = sorted(key for key in SIZES if key[0] == ALPHA)


def flood(seconds):
	return errors.FloodWaitError(request=None, capture=seconds)


def sha256(data):
	return hashlib.sha256(data).hexdigest()


def get_files(client, media_id=None):
	return [e for e in client.timeline if e[0] == 'GetFile' and (media_id is None or e[1] == media_id)]


class DefaultRunTests(MediaCase):
	'''
	Without --include-media nothing about media happens, and media do not
	change the collected data.
	'''

	def test_without_include_media_no_file_is_requested_in_any_mode(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, f'plain-{storage}')
				code, _, err = self.collect(targets=BATCH, out=out, storage=storage)
				self.assertEqual(code, 0, err)
				client = self.last_client
				self.assertEqual(client.files.requests, [])
				self.assertEqual(client.kinds('GetFile', 'GetMessages'), [])
				self.assertTrue(client.kinds('GetHistoryRequest'))
				self.assertFalse(os.path.lexists(os.path.join(out, MEDIA_FOLDER)))
				self.assertFalse(os.path.lexists(os.path.join(out, IDENTITY_FILE)))
				status = self.status(out)
				self.assertNotIn('media', status)
				self.assertNotIn('media', status['requests'])

				# the same world with --include-media: the checks above can see media
				out = os.path.join(self.tmp, f'media-{storage}')
				code, _, err = self.media(targets=BATCH, out=out, storage=storage)
				self.assertEqual(code, 0, err)
				self.assertTrue(get_files(self.last_client))
				self.assertTrue(os.path.isdir(os.path.join(out, MEDIA_FOLDER)))
				status = self.status(out)
				# media requests are counted in the media block only
				self.assertEqual(status['media']['requests']['media']['requests'], len(get_files(self.last_client)))
				self.assertNotIn('media', status['requests'])
				self.assertIn('history', status['requests'])

	def test_media_leave_the_collected_files_byte_identical(self):
		plain, media = os.path.join(self.tmp, 'plain'), os.path.join(self.tmp, 'media')
		self.assertEqual(self.collect(targets=BATCH, out=plain)[0], 0)
		self.assertEqual(self.media(targets=BATCH, out=media)[0], 0)
		with_media = file_contents(media)
		extra = sorted(name for name in with_media if name.startswith(f'{MEDIA_FOLDER}/') or name == IDENTITY_FILE)
		self.assertIn(IDENTITY_FILE, extra)
		self.assertIn(f'{MEDIA_FOLDER}/media.sqlite3', extra)
		for name in extra:
			del with_media[name]
		self.assertEqual(with_media, file_contents(plain))

	def test_media_leave_the_stored_messages_identical(self):
		plain, media = os.path.join(self.tmp, 'plain'), os.path.join(self.tmp, 'media')
		self.assertEqual(self.collect(targets=BATCH, out=plain, storage='sqlite')[0], 0)
		self.assertEqual(self.media(targets=BATCH, out=media, storage='sqlite')[0], 0)
		query = 'SELECT channel_id, message_id, raw FROM messages ORDER BY channel_id, message_id'
		stored = self.rows(query, out=media)
		self.assertEqual(len(stored), 8)
		self.assertEqual(stored, self.rows(query, out=plain))
		# the run's request statistics keep the collection's method classes
		classes = 'SELECT method_class, requests FROM run_requests ORDER BY method_class'
		self.assertEqual(self.rows(classes, out=media), self.rows(classes, out=plain))
		self.assertEqual(
			{name for name, _ in self.rows(classes, out=media)}, {'resolve', 'channel', 'enrichment', 'history'})


class ModesTests(MediaCase):
	'''
	A batch of two channels with media, in each storage mode.
	'''

	def run_mode(self, storage):
		out = os.path.join(self.tmp, storage)
		client = standard_world()
		with record_events(client, out):
			code, stdout, err = self.media('--concurrency', '3', targets=BATCH, out=out, storage=storage,
				client=client)
		self.assertEqual(code, 0, err)
		return out, client, stdout

	def test_every_file_is_stored_and_recorded(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out, client, stdout = self.run_mode(storage)
				items = {(i['channel_id'], i['message_id']): i for i in self.items(out)}
				# message 3 is text: no row
				self.assertEqual(sorted(items), sorted(SIZES))
				for key, (media_id, size, seed) in SIZES.items():
					item = items[key]
					self.assertEqual((item['media_id'], item['state'], item['reason']), (media_id, 'complete', None))
					data = content(size, seed)
					self.assertEqual(item['sha256'], sha256(data))
					path = self.ledger_rows(
						'SELECT path FROM blobs WHERE channel_id = ? AND sha256 = ?', key[0], item['sha256'],
						out=out)[0]['path']
					self.assertEqual(self.stored(path, out), data)
				# the same video in messages 2 and 6: one file, requested once (two pulls)
				self.assertEqual(items[(ALPHA, 2)]['sha256'], items[(ALPHA, 6)]['sha256'])
				self.assertEqual(len(get_files(client, 7001)), 2)
				self.assertEqual(len(self.media_files(out)), 6)
				self.assertEqual(self.partial_files(out), [])
				self.assertEqual(
					os.listdir(os.path.join(out, MEDIA_FOLDER, '.partial')) if os.path.isdir(
						os.path.join(out, MEDIA_FOLDER, '.partial')) else [], [])

				block = self.status(out)['media']
				self.assertEqual(block['state'], 'complete')
				self.assertIsNone(block['stop_reason'])
				self.assertEqual(block['items'], {
					'selected': 7, 'complete': 7, 'downloaded': 6, 'skipped': 0, 'failed': 0, 'not_attempted': 0
				})
				self.assertEqual(block['bytes_received'], DOWNLOADED_BYTES)
				self.assertEqual((block['types'], block['max_media_size'], block['max_media_total']),
					(['image', 'video', 'audio', 'file'], None, None))
				self.assertEqual(self.status(out)['state'], 'complete')
				self.assertIn('> Media: 7 of 7 selected file(s) stored (6 downloaded in this run)', stdout)
				self.assertEqual(block['requests']['media']['requests'], 7)
				self.assertIn('>   media        7 / 0 / 0 / 0', stdout)
				self.assertNotIn('media', self.status(out)['requests'])
				self.assertIn('Media: image, video, audio, file into downloaded-media/; no size limits', stdout)

	def test_the_ledger_run_names_the_collection_run(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out, _, _ = self.run_mode(storage)
				status = self.status(out)
				run, = self.runs(out)
				collection_run = status['database']['run_uuid'] if storage != 'files' else status['started_at']
				self.assertEqual(
					(run['command'], run['state'], run['collection_run'], json.loads(run['channels'])),
					('collect', 'complete', collection_run, [ALPHA, BETA]))
				self.assertEqual(run['run_uuid'], status['media']['run_uuid'])
				self.assertEqual(json.loads(run['selection']), {
					'types': ['image', 'video', 'audio', 'file'], 'max_media_size': None, 'max_media_total': None})
				self.assertEqual({i['source_run'] for i in self.items(out)}, {collection_run})
				self.assertEqual(run['bytes_received'], DOWNLOADED_BYTES)
				self.assertEqual(run['app_requests'], len(get_files(self.last_client)))

	def test_downloads_start_after_the_last_history_commit(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				_, client, _ = self.run_mode(storage)
				timeline = client.timeline
				first_file = client.index('GetFile')
				self.assertIsNotNone(first_file)
				self.assertGreater(first_file, client.index('commit', last=True))
				self.assertGreater(first_file, client.index('GetHistoryRequest', last=True))
				self.assertGreater(first_file, client.index('GetFullChannelRequest', last=True))
				# each channel's rows only after its history is committed
				for channel in (ALPHA, BETA):
					commit = timeline.index(('commit', channel))
					self.assertGreater(timeline.index(('ledger_request', channel)), commit)
				self.assertEqual(client.kinds('GetMessages'), [])

	def test_the_ledger_is_written_only_with_the_output_folder_locked(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out, client, _ = self.run_mode(storage)
				writes = client.kinds('ledger_write')
				self.assertGreater(len(writes), 10)
				self.assertTrue(all(locked for _, _, locked in writes), writes)
				self.assertFalse(os.path.exists(os.path.join(out, LOCK_FILE)))

	def test_the_ledger_belongs_to_the_folder_data(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out, _, _ = self.run_mode(storage)
				meta, = self.ledger_rows('SELECT dataset_uuid, storage FROM ledger_meta', out=out)
				identity = folder_identity(out)
				self.assertEqual((meta['dataset_uuid'], meta['storage']), (identity.dataset_uuid, storage))
				has_record = os.path.isfile(os.path.join(out, IDENTITY_FILE))
				self.assertEqual(has_record, storage == 'files')
				if storage != 'files':
					self.assertEqual(meta['dataset_uuid'], self.manifest(out)['dataset_uuid'])

	def test_items_join_the_stored_messages(self):
		out, _, _ = self.run_mode('sqlite')
		con = sqlite3.connect(f'file:{self.db(out)}?mode=ro', uri=True)
		try:
			con.execute('ATTACH ? AS media', (f'file:{self.ledger_path(out)}?mode=ro',))
			joined = con.execute(
				"SELECT m.channel_id, m.message_id, coalesce(i.state, 'not selected') FROM messages m "
				'LEFT JOIN media.items i ON i.channel_id = m.channel_id AND i.message_id = m.message_id '
				'AND i.media_id = m.media_id WHERE m.media_id IS NOT NULL ORDER BY 1, 2').fetchall()
			orphans = con.execute(
				'SELECT count(*) FROM media.items i LEFT JOIN messages m ON m.channel_id = i.channel_id '
				'AND m.message_id = i.message_id AND m.media_id = i.media_id WHERE m.message_id IS NULL'
			).fetchone()[0]
		finally:
			con.close()
		self.assertEqual(joined, [(*key, 'complete') for key in sorted(SIZES)])
		self.assertEqual(orphans, 0)


class SelectionTests(MediaCase):

	def test_media_types_select_the_rows_and_requests(self):
		code, stdout, err = self.media('--media-types', 'audio,video')
		self.assertEqual(code, 0, err)
		self.assertEqual(sorted(self.states()), [(ALPHA, 2), (ALPHA, 4), (ALPHA, 6)])
		requested = {e[1] for e in get_files(self.last_client)}
		self.assertEqual(requested, {7001, 7002})
		self.assertIn('Media: video, audio into downloaded-media/; no size limits', stdout)
		self.assertEqual(self.status()['media']['types'], ['video', 'audio'])

	def test_a_file_over_the_size_limit_is_skipped_without_a_request(self):
		code, stdout, err = self.media('--max-media-size', '10K')
		self.assertEqual(code, 0, err)
		states = self.states()
		self.assertEqual(states[(ALPHA, 2)], ('skipped', 'over_file_limit'))
		self.assertEqual(states[(ALPHA, 6)], ('skipped', 'over_file_limit'))
		self.assertEqual(states[(ALPHA, 1)], ('complete', None))
		self.assertEqual(get_files(self.last_client, 7001), [])
		self.assertTrue(get_files(self.last_client, 9001))
		self.assertIn('at most 10 KiB per file', stdout)
		self.assertEqual(self.status()['media']['items']['skipped'], 2)
		self.assertEqual(self.status()['media']['state'], 'complete')

	def test_the_run_limit_bounds_the_bytes_received(self):
		code, stdout, err = self.media('--max-media-total', '1M')
		self.assertEqual(code, 0, err)
		states = self.states()
		# newest first: the video of message 6 reserves two whole pulls (1 MiB)
		# and leaves 448 KiB, less than the one pull each smaller file reserves
		self.assertEqual(states[(ALPHA, 6)], ('complete', None))
		self.assertEqual(states[(ALPHA, 2)], ('complete', None))
		for message in (5, 4, 1):
			self.assertEqual(states[(ALPHA, message)], ('skipped', 'over_run_limit'))
		block = self.status()['media']
		self.assertEqual(block['bytes_received'], 600 * KIB)
		self.assertLessEqual(block['bytes_received'], 1024 * KIB)
		self.assertEqual({e[1] for e in get_files(self.last_client)}, {7001})
		self.assertEqual(block['state'], 'complete')
		self.assertIn('at most 1 MiB per run', stdout)


class LifecycleTests(MediaCase):

	def add_posts(self, client):
		files = client.files
		files.post(7, photo(files, 9002, 4000, 7), ALPHA)
		files.post(8, files.document_media(7001, size=600 * KIB), ALPHA)

	def test_update_covers_only_the_new_messages(self):
		for storage in ('files', 'sqlite'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = standard_world()
				self.assertEqual(self.media(out=out, storage=storage, client=client)[0], 0)
				first, = self.runs(out)
				before = {(i['channel_id'], i['message_id']): i for i in self.items(out)}
				stored = self.media_files(out)
				self.add_posts(client)
				client.timeline.clear()

				code, _, err = self.media('--update', out=out, client=client)
				self.assertEqual(code, 0, err)
				second = self.runs(out)[1]
				items = {(i['channel_id'], i['message_id']): i for i in self.items(out)}
				self.assertEqual(sorted(items), sorted(before) + [(ALPHA, 7), (ALPHA, 8)])
				for key in before:
					self.assertEqual(items[key], before[key])
				for key in ((ALPHA, 7), (ALPHA, 8)):
					self.assertEqual(
						(items[key]['state'], items[key]['first_run_uuid']), ('complete', second['run_uuid']))
				# message 8 reuses the stored video; only the new photo is requested
				self.assertEqual({e[1] for e in get_files(client)}, {9002})
				self.assertEqual(items[(ALPHA, 8)]['sha256'], items[(ALPHA, 2)]['sha256'])
				self.assertTrue(set(stored) < set(self.media_files(out)))
				self.assertEqual(json.loads(second['channels']), [ALPHA])
				self.assertEqual(self.status(out)['media']['items']['selected'], 2)
				self.assertNotEqual(first['run_uuid'], second['run_uuid'])

	def test_messages_a_server_sends_outside_the_bounds_get_no_rows(self):
		for storage in ('files', 'sqlite'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = standard_world(honor_bounds=False)
				code, _, err = self.media('--min-id', '4', '--yes', out=out, storage=storage, client=client)
				self.assertEqual(code, 0, err)
				# the pages also carried messages 4 and 3, which the window drops
				self.assertIn(4, [i for page in client.world.served for i in page])
				self.assertEqual(sorted(self.states(out)), [(ALPHA, 5), (ALPHA, 6)])

				self.add_posts(client)
				client.world.served.clear()
				code, _, err = self.media('--update', out=out, client=client)
				self.assertEqual(code, 0, err)
				self.assertIn(6, [i for page in client.world.served for i in page])
				second = self.runs(out)[1]
				added = sorted(i['message_id'] for i in self.items(out) if i['first_run_uuid'] == second['run_uuid'])
				self.assertEqual(added, [7, 8])
				self.assertEqual(self.status(out)['media']['items']['selected'], 2)
				self.assertEqual(json.loads(second['channels']), [ALPHA])

	def test_update_with_nothing_new_records_nothing(self):
		client = standard_world()
		self.assertEqual(self.media(client=client)[0], 0)
		before = self.items()
		client.timeline.clear()
		code, _, err = self.media('--update', client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual(self.items(), before)
		self.assertEqual(get_files(client), [])
		second = self.runs()[1]
		self.assertEqual((second['state'], json.loads(second['channels'])), ('complete', []))
		self.assertEqual(self.status()['media']['items']['selected'], 0)

	def test_a_window_run_covers_the_messages_it_keeps_and_nothing_is_deleted(self):
		client = standard_world()
		code, _, err = self.media('--max-messages', '2', '--yes', client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual(sorted(self.states()), [(ALPHA, 5), (ALPHA, 6)])
		self.assertEqual({e[1] for e in get_files(client)}, {7001, 7003})

		# a full run: the stored files are reused, not requested again
		client.timeline.clear()
		self.assertEqual(self.media(client=client)[0], 0)
		self.assertEqual(sorted(self.states()), ALPHA_KEYS)
		self.assertEqual({e[1] for e in get_files(client)}, {9001, 7002})
		self.assertEqual(set(self.states().values()), {('complete', None)})
		files = self.media_files()

		# a narrower window replaces the history; files and rows stay
		client.timeline.clear()
		self.assertEqual(self.media('--max-messages', '1', '--yes', client=client)[0], 0)
		with open(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json'), encoding='utf-8') as reader:
			self.assertEqual([m['id'] for m in json.load(reader)['messages']], [6])
		self.assertEqual(self.media_files(), files)
		self.assertEqual(set(self.states().values()), {('complete', None)})
		self.assertEqual(sorted(self.states()), ALPHA_KEYS)
		self.assertEqual(get_files(client), [])


class StopTests(MediaCase):

	def test_an_account_stop_while_downloading_ends_the_run_and_keeps_every_history(self):
		client = standard_world()
		# beta's photo (message 2) is its first download
		client.files.fault(lambda r: r.kind == 'GetFile' and r.media_id == 9101, flood(3600))
		code, _, err = self.media('--concurrency', '1', targets=BATCH, client=client)
		self.assertEqual(code, 1)
		status = self.status()
		self.assertEqual(status['state'], 'interrupted')
		self.assertIn('account stopped', status['stop_reason'])
		self.assertEqual([t['status'] for t in status['targets']], ['succeeded', 'succeeded'])
		for target in BATCH:
			self.assertTrue(os.path.isfile(os.path.join(self.out, target, f'{target}_messages.json')))
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'counter.csv')))
		states = self.states()
		self.assertEqual(states[(BETA, 2)], ('failed', 'flood_stop'))
		self.assertEqual(states[(BETA, 1)], ('requested', None))
		self.assertEqual(get_files(client, 7101), [])
		self.assertEqual({states[k] for k in ALPHA_KEYS}, {('complete', None)})
		run, = self.runs()
		self.assertEqual(run['state'], 'interrupted')
		self.assertIn('account stopped', run['stop_reason'])
		block = status['media']
		self.assertEqual((block['state'], block['items']['failed'], block['items']['not_attempted']),
			('interrupted', 1, 1))
		# reported once, with the run: not again as a media stop
		self.assertIn(f'> Stopped: {status["stop_reason"]}', err)
		detail = status['stop_reason'].split(': ', 1)[1]
		self.assertEqual(err.count(detail), 1, err)

	def test_a_full_disk_stops_media_only(self):
		client = standard_world()
		failing = mock.patch(
			'telegram_tracker.media.staging.PartialFile.write',
			side_effect=OSError(errno.ENOSPC, 'No space left on device'))
		with failing:
			code, stdout, err = self.media(client=client)
		self.assertEqual(code, 1)
		status = self.status()
		self.assertEqual((status['state'], status['stop_reason']), ('complete', None))
		self.assertEqual(status['targets'][0]['status'], 'succeeded')
		block = status['media']
		self.assertEqual(block['state'], 'interrupted')
		self.assertIn('the disk is full', block['stop_reason'])
		states = self.states()
		# newest first: message 6 is the first download
		self.assertEqual(states[(ALPHA, 6)], ('failed', 'disk_full'))
		self.assertEqual(sorted(states), ALPHA_KEYS)
		self.assertEqual({states[k] for k in ALPHA_KEYS if k != (ALPHA, 6)}, {('requested', None)})
		self.assertEqual(block['items']['not_attempted'], 4)
		self.assertEqual(self.partial_files(), [])
		self.assertIn('> Media downloads stopped: the disk is full', err)
		run, = self.runs()
		self.assertEqual((run['state'], run['stop_reason']), ('interrupted', block['stop_reason']))

	def test_an_account_stop_before_the_downloads_leaves_committed_media_requested(self):
		client = standard_world()
		client.script('get_entity', None, flood(3600))
		code, _, err = self.media('--concurrency', '1', targets=BATCH, client=client)
		self.assertEqual(code, 1)
		self.assertEqual(get_files(client), [])
		self.assertEqual([t['status'] for t in self.status()['targets']], ['succeeded', 'cancelled'])
		self.assertEqual(sorted(self.states()), ALPHA_KEYS)
		self.assertEqual(set(self.states().values()), {('requested', None)})
		run, = self.runs()
		self.assertEqual((run['state'], json.loads(run['channels'])), ('interrupted', [ALPHA]))
		self.assertIn('account stopped', run['stop_reason'])

	def test_a_ledger_that_cannot_be_written_stops_media_only(self):
		client = standard_world()
		with mock.patch(
				'telegram_tracker.media.ledger.Ledger.request',
				side_effect=StorageError('cannot record requested media')):
			code, _, err = self.media(targets=BATCH, client=client)
		self.assertEqual(code, 1)
		status = self.status()
		self.assertEqual((status['state'], [t['status'] for t in status['targets']]),
			('complete', ['succeeded', 'succeeded']))
		self.assertEqual(get_files(client), [])
		self.assertIn('storage error: cannot record requested media', status['media']['stop_reason'])
		self.assertEqual(self.items(), [])
		self.assertIn('The collected data is not affected', err)

	def test_an_account_stop_reaches_the_download_in_flight_at_its_next_request(self):
		client = standard_world()
		# message 6's video takes two pulls; message 5's file is requested
		# between them and meets the flood wait
		client.files.fault(lambda r: r.kind == 'GetFile' and r.media_id == 7003, flood(3600))
		# --concurrency applies to a batch
		code, _, err = self.media('--concurrency', '2', targets=BATCH, client=client)
		self.assertEqual(code, 1)
		# jobs follow the commit order; alpha's first two run side by side
		self.assertEqual([e[1:] for e in get_files(client, 7001)], [(7001, 0)])
		self.assertEqual([e[1:] for e in get_files(client, 7003)], [(7003, 0)])
		self.assertEqual(get_files(client)[-2:], [('GetFile', 7001, 0), ('GetFile', 7003, 0)])
		states = self.states()
		self.assertEqual(states[(ALPHA, 6)], ('failed', 'flood_stop'))
		self.assertEqual(states[(ALPHA, 5)], ('failed', 'flood_stop'))
		self.assertEqual({states[k] for k in ((ALPHA, 4), (ALPHA, 2), (ALPHA, 1))}, {('requested', None)})
		self.assertEqual({states[k][0] for k in ((BETA, 2), (BETA, 1))}, {'complete'})
		self.assertEqual(self.partial_files(), [])
		self.assertEqual(self.runs()[0]['state'], 'interrupted')

	def test_a_failed_file_leaves_the_media_run_partial(self):
		client = standard_world()
		client.files.fault(
			lambda r: r.kind == 'GetFile' and r.media_id == 7003, errors.FileIdInvalidError(request=None))
		code, stdout, err = self.media(client=client)
		self.assertEqual(code, 1)
		status = self.status()
		self.assertEqual((status['state'], status['stop_reason']), ('complete', None))
		self.assertEqual(self.states()[(ALPHA, 5)], ('failed', 'request_failed'))
		block = status['media']
		self.assertEqual((block['state'], block['stop_reason']), ('partial', None))
		self.assertEqual((block['items']['complete'], block['items']['failed']), (4, 1))
		self.assertEqual(self.runs()[0]['state'], 'partial')
		self.assertIn('> 1 media file(s) failed', err)
		self.assertNotIn('Media downloads stopped', err)


class ReferenceTests(MediaCase):

	def test_an_expired_reference_is_refreshed_through_the_channel_of_the_job(self):
		client = standard_world()
		files = client.files

		async def expire(request):
			# the location captured while paging now holds an old reference
			files.expire('document', 7002)

		files.fault(lambda r: r.kind == 'GetFile' and r.media_id == 7002, expire)
		code, _, err = self.media(client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual(client.kinds('GetMessages'), [('GetMessages', ALPHA)])
		self.assertEqual(self.states()[(ALPHA, 4)], ('complete', None))
		attempt, = self.ledger_rows('SELECT refreshed FROM attempts WHERE message_id = 4')
		self.assertEqual(attempt['refreshed'], 1)
		self.assertEqual(self.runs()[0]['refreshes'], 1)

	def test_the_peer_is_the_resolved_channel(self):
		entity = FixtureWorld().entity('alpha_fx')
		self.assertEqual(input_channel(entity), types.InputChannel(channel_id=ALPHA, access_hash=1000))


class LedgerGuardTests(MediaCase):

	def copy_media(self, source, target):
		shutil.copytree(os.path.join(source, MEDIA_FOLDER), os.path.join(target, MEDIA_FOLDER))

	def test_a_ledger_of_other_data_is_refused_before_credentials(self):
		for storage, message in (('files', IDENTITY_FILE), ('sqlite', 'belongs to another dataset')):
			with self.subTest(storage=storage):
				source = os.path.join(self.tmp, f'source-{storage}')
				target = os.path.join(self.tmp, f'target-{storage}')
				self.assertEqual(self.media(out=source, storage=storage)[0], 0)
				self.assertEqual(self.collect(out=target, storage=storage)[0], 0)
				self.copy_media(source, target)
				before = file_contents(target)
				calls = self.factory_calls
				code, _, err = self.media(out=target)
				self.assertEqual(code, 1)
				self.assertIn(message, err)
				self.assertEqual(self.factory_calls, calls)
				self.assertEqual(file_contents(target), before)
				self.assertFalse(os.path.exists(os.path.join(target, LOCK_FILE)))

	def test_a_ledger_of_other_data_found_under_the_lock_stops_the_run_before_any_channel_request(self):
		source, target = os.path.join(self.tmp, 'source'), os.path.join(self.tmp, 'target')
		self.assertEqual(self.media(out=source)[0], 0)
		self.assertEqual(self.collect(out=target)[0], 0)
		self.copy_media(source, target)
		# the target was collected without media: it has no identity record
		self.assertFalse(os.path.exists(os.path.join(target, IDENTITY_FILE)))
		client = standard_world()

		async def authorize(_client):
			pass

		with self.assertRaises(StorageError) as caught, open(os.devnull, 'w') as sink, \
				mock.patch('sys.stdout', sink), mock.patch('sys.stderr', sink):
			virtual_time.run(collector.run_collection(
				['alpha_fx'], target, client_factory=lambda: client, authorize=authorize,
				media=MediaSelection()))
		self.assertIn(IDENTITY_FILE, str(caught.exception))
		self.assertEqual(client.call_names(), ['connect', 'disconnect'])
		status = self.status(target)
		self.assertEqual(status['state'], 'interrupted')
		self.assertIn('storage error', status['stop_reason'])
		self.assertEqual(status['targets'][0]['status'], 'cancelled')
		self.assertFalse(os.path.exists(os.path.join(target, LOCK_FILE)))

	def test_a_dead_media_run_is_recovered_and_its_partial_files_removed(self):
		client = standard_world()
		self.assertEqual(self.media(client=client)[0], 0)
		media = client.files.post(7, photo(client.files, 9002, 4000, 7), ALPHA).ref
		layout = MediaLayout(self.out)
		with open_ledger(layout, folder_identity(self.out)) as ledger:
			ledger.start_run(OLD_RUN, 'collect', selection=MediaSelection().record())
			ledger.request(OLD_RUN, ALPHA, [(7, media)])
			ledger.begin_download(OLD_RUN, (ALPHA, 7, 9002))
		folder = layout.partial_folder(OLD_RUN)
		with open(os.path.join(folder, f'{ALPHA}-7-9002.part'), 'wb') as writer:
			writer.write(b'partial')

		code, _, err = self.media(client=client)
		self.assertEqual(code, 0, err)
		self.assertIn('1 earlier media run(s) never finished', err)
		self.assertIn('removed 1 partial download(s)', err)
		self.assertFalse(os.path.exists(folder))
		runs = {r['run_uuid']: r for r in self.runs()}
		self.assertEqual(runs[OLD_RUN]['state'], 'interrupted')
		self.assertEqual(self.states()[(ALPHA, 7)], ('complete', None))
		attempts = self.ledger_rows('SELECT n, run_uuid, outcome, reason FROM attempts WHERE message_id = 7 ORDER BY n')
		self.assertEqual([(a['run_uuid'], a['outcome'], a['reason']) for a in attempts][0],
			(OLD_RUN, 'failed', 'interrupted'))
		self.assertEqual(len(attempts), 2)


class InterruptedLedgerWriteTests(MediaCase):
	'''
	A ledger left with a hot journal by a killed writer: the read-only
	check before credentials cannot read it, so the run's own open under
	the lock rolls it back and checks it.
	'''

	def test_the_next_media_run_recovers_a_ledger_left_by_a_killed_writer(self):
		client = standard_world()
		self.assertEqual(self.media(client=client)[0], 0)
		layout = MediaLayout(self.out)
		with open_ledger(layout, folder_identity(self.out)) as ledger:
			ledger.start_run(OLD_RUN, 'collect', selection=MediaSelection().record())
		journal = kill_inside_a_write(self.ledger_path())
		# what the check before credentials meets: no read-only connection works
		with self.assertRaises(InterruptedWrite) as refused:
			open_ledger(layout, folder_identity(self.out), readonly=True)
		self.assertNotIn('JSON1', str(refused.exception))
		calls = self.factory_calls

		code, _, err = self.media(client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual(self.factory_calls, calls + 1)
		self.assertIn('has an unfinished write', err)
		self.assertIn('1 earlier media run(s) never finished', err)
		self.assertFalse(os.path.exists(journal))
		runs = {r['run_uuid']: r for r in self.runs()}
		self.assertEqual(runs[OLD_RUN]['state'], 'interrupted')
		self.assertEqual(self.ledger_rows("SELECT count(*) AS n FROM sqlite_master WHERE name = 'junk'"), [{'n': 0}])
		self.assertEqual(self.status()['media']['state'], 'complete')

	def test_a_busy_ledger_is_reported_as_busy_not_as_a_missing_json1(self):
		self.assertEqual(self.media()[0], 0)
		holder = sqlite3.connect(self.ledger_path(), isolation_level=None)
		try:
			holder.execute('BEGIN EXCLUSIVE')
			with self.assertRaises(DatabaseBusy) as refused:
				open_ledger(MediaLayout(self.out), folder_identity(self.out), readonly=True)
		finally:
			holder.execute('ROLLBACK')
			holder.close()
		self.assertIn('in use by another process', str(refused.exception))
		self.assertNotIn('JSON1', str(refused.exception))

	def test_a_ledger_of_other_data_with_an_unfinished_write_is_refused_under_the_lock(self):
		source, target = os.path.join(self.tmp, 'source'), os.path.join(self.tmp, 'target')
		self.assertEqual(self.media(out=source)[0], 0)
		self.assertEqual(self.collect(out=target)[0], 0)
		shutil.copytree(os.path.join(source, MEDIA_FOLDER), os.path.join(target, MEDIA_FOLDER))
		kill_inside_a_write(self.ledger_path(target))
		client = standard_world()
		code, _, err = self.media(out=target, client=client)
		self.assertEqual(code, 1)
		self.assertIn('has an unfinished write', err)
		self.assertIn(IDENTITY_FILE, err)
		self.assertNotIn('get_entity', client.call_names())
		self.assertEqual(client.kinds('GetHistoryRequest', 'GetFile'), [])
		self.assertEqual(self.status(target)['state'], 'interrupted')


class ArgumentTests(MediaCase):

	def assertArgumentError(self, argv, message):
		code, _, err = self.run_cli(['collect', '-o', self.out, '--telegram-channel', 'alpha_fx', *argv])
		self.assertEqual(code, 2, err)
		self.assertIn(message, err)
		self.assertEqual(self.factory_calls, 0)
		self.assertFalse(os.path.exists(self.out))

	def test_media_options_need_include_media(self):
		self.assertArgumentError(['--media-types', 'video'], '--media-types needs --include-media')
		self.assertArgumentError(
			['--media-types', 'video', '--max-media-size', '1M', '--max-media-total', '2M'],
			'--media-types, --max-media-size, --max-media-total need --include-media')

	def test_media_needs_message_collection(self):
		self.assertArgumentError(
			['--include-media', '--limit-download-to-channel-metadata'], '--include-media needs message collection')

	def test_bad_media_values_are_refused(self):
		self.assertArgumentError(['--include-media', '--media-types', 'video,bogus'], "unknown media type(s) 'bogus'")
		self.assertArgumentError(['--include-media', '--media-types', 'video,'], "unknown media type(s) ''")
		self.assertArgumentError(['--include-media', '--max-media-size', '0'], 'must be larger than zero')
		self.assertArgumentError(['--include-media', '--max-media-total', '5X'], 'is not a size')

	def test_the_legacy_form_has_no_media_options(self):
		code, _, err = self.run_cli(['--telegram-channel', 'alpha_fx', '-o', self.out, '--include-media'])
		self.assertEqual(code, 2)
		self.assertIn('unrecognized arguments: --include-media', err)

	def test_a_run_without_media_files_still_records_its_run(self):
		client = MediaClient()
		code, stdout, err = self.media(client=client)
		self.assertEqual(code, 0, err)
		run, = self.runs()
		self.assertEqual((run['state'], json.loads(run['channels'])), ('complete', []))
		self.assertIn('> Media: 0 of 0 selected file(s) stored', stdout)


class FolderSyncTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name

	def test_a_new_folder_is_synced_in_its_parent_once(self):
		synced = []
		with mock.patch.object(media_layout, 'sync_folder', synced.append):
			layout = MediaLayout(self.tmp)
			folder = layout.type_folder(ALPHA, 'video')
			self.assertEqual(synced, [self.tmp, layout.root, os.path.join(layout.root, str(ALPHA))])
			self.assertEqual(layout.type_folder(ALPHA, 'video'), folder)
			self.assertEqual(len(synced), 3)
			layout.type_folder(ALPHA, 'image')
			self.assertEqual(synced[3:], [os.path.join(layout.root, str(ALPHA))])

	def test_a_folder_whose_name_cannot_be_synced_is_refused(self):
		with mock.patch.object(media_layout, 'sync_folder', side_effect=OSError(errno.EIO, 'I/O error')):
			with self.assertRaises(media_layout.MediaPathError):
				MediaLayout(self.tmp).type_folder(ALPHA, 'video')


if __name__ == '__main__':
	unittest.main()
