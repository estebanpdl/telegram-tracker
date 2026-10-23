# -*- coding: utf-8 -*-
'''
`download-media`: the media of a collected folder, after collection, in
every storage mode; its --dry-run. Through cli.main with fakes (no
network, no credentials, synthetic data).
'''

# import modules
import json
import os
import sqlite3
import unittest

import offline_guard

from unittest import mock

from telethon import errors

from media_collect_support import ALPHA, BETA, KIB, MediaCase, document, standard_world
from media_download_support import any_request, content
from storage_modes_support import file_contents

from telegram_tracker.media import fetch, plan
from telegram_tracker.media.identity import IDENTITY_FILE, folder_identity
from telegram_tracker.media.layout import MEDIA_FOLDER, MediaLayout
from telegram_tracker.media.ledger import open_ledger
from telegram_tracker.media.selection import MediaSelection
from telegram_tracker.reader.records import ChannelHistory, HistoryProvenance
from telegram_tracker.storage import status as status_module
from telegram_tracker.storage.records import LOCK_FILE
from telegram_tracker.storage.sqlite.lease import database_lease_path


MODES = ('files', 'sqlite', 'both')
# a started_at after the first collection's
LATER = '2099-01-01T00:00:00+00:00'
BATCH = ('alpha_fx', 'beta_fx')
# the stored channels' access hash (fixture channel defaults)
ACCESS_HASH = 1000
OLD_RUN = '00000000-0000-4000-8000-000000000001'

# what the standard world stores, by (channel, message): (media id, size, seed)
SIZES = {
	(ALPHA, 1): (9001, 3000, 1), (ALPHA, 2): (7001, 600 * KIB, 2), (ALPHA, 4): (7002, 5000, 3),
	(ALPHA, 5): (7003, 7000, 4), (ALPHA, 6): (7001, 600 * KIB, 2), (BETA, 1): (7101, 9000, 5),
	(BETA, 2): (9101, 2000, 6)
}
# GetFile requests for every file once (the video of 2 and 6 takes two pulls, once)
ALL_PULLS = 7



def flood(seconds):
	return errors.FloodWaitError(request=None, capture=seconds)


def get_files(client):
	return [event for event in client.timeline if event[0] == 'GetFile']


class FetchCase(MediaCase):

	def collected(self, *extra, storage=None, out=None, client=None, media=False):
		'''
		Collect BATCH (with --include-media when `media`); returns the client.
		'''
		client = client or standard_world()
		run = self.media if media else self.collect
		code, _, err = run(*extra, targets=BATCH, out=out, storage=storage, client=client)
		self.assertEqual(code, 0, err)
		client.timeline.clear()
		client.lookups.clear()
		return client

	def assertStored(self, out=None, keys=SIZES):
		'''
		Each item of `keys` is complete and its file holds the content.
		'''
		items = {(i['channel_id'], i['message_id']): i for i in self.items(out)}
		blobs = {
			(b['channel_id'], b['sha256']): b['path']
			for b in self.ledger_rows('SELECT channel_id, sha256, path FROM blobs', out=out)
		}
		for key in keys:
			media_id, size, seed = SIZES[key]
			item = items[key]
			self.assertEqual((item['state'], item['media_id']), ('complete', media_id), key)
			self.assertEqual(self.stored(blobs[(key[0], item['sha256'])], out), content(size, seed), key)


class DownloadTests(FetchCase):

	def test_downloads_every_selected_file_in_each_mode(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = self.collected(storage=storage, out=out)
				before = file_contents(out)
				messages = 'SELECT channel_id, message_id, raw FROM messages ORDER BY channel_id, message_id'
				stored_before = self.rows(messages, out=out) if storage != 'files' else None
				calls = self.factory_calls

				code, stdout, err = self.download(out=out, client=client)
				self.assertEqual(code, 0, err)
				self.assertEqual(self.factory_calls, calls + 1)
				self.assertStored(out)
				self.assertEqual(len(get_files(client)), ALL_PULLS)
				# one lookup per channel, with the stored access hash, before its files
				self.assertEqual(
					sorted((channel, access_hash, sorted(ids)) for channel, access_hash, ids in client.lookups),
					[(ALPHA, ACCESS_HASH, [1, 2, 4, 5, 6]), (BETA, ACCESS_HASH, [1, 2])]
				)
				lookups = [i for i, event in enumerate(client.timeline) if event[0] == 'GetMessages']
				self.assertLess(lookups[0], client.timeline.index(('GetFile', 9001, 0)))
				self.assertLess(lookups[1], client.timeline.index(('GetFile', 7101, 0)))

				run, = self.runs(out)
				self.assertEqual((run['command'], run['state']), ('download-media', 'complete'))
				status = self.status(out)
				expected_run = (status.get('database') or {}).get('run_uuid') or status['started_at']
				self.assertEqual(run['collection_run'], expected_run)
				self.assertEqual(run['app_requests'], ALL_PULLS + 2)
				self.assertEqual(sorted(json.loads(run['channels'])), [ALPHA, BETA])
				self.assertIn('> Media: 7 of 7 selected file(s) stored (6 downloaded in this run)', stdout)

				# the collected data is not changed; files folders get their identity record
				after = file_contents(out)
				extra = sorted(n for n in after if n.startswith(f'{MEDIA_FOLDER}/') or n == IDENTITY_FILE)
				self.assertEqual(IDENTITY_FILE in extra, storage == 'files')
				for name in extra:
					del after[name]
				self.assertEqual(after, before)
				if stored_before is not None:
					self.assertEqual(self.rows(messages, out=out), stored_before)
					self.assertEqual(self.rows('SELECT count(*) FROM collection_runs', out=out), [(1,)])
				self.assertNoLocks(out)

	def test_a_second_run_finds_nothing_to_download_without_logging_in(self):
		client = self.collected()
		self.assertEqual(self.download(client=client)[0], 0)
		calls = self.factory_calls
		code, stdout, err = self.download(client=client)
		self.assertEqual(code, 0, err)
		self.assertIn('Nothing to download', stdout)
		self.assertEqual(self.factory_calls, calls)
		self.assertEqual(len(self.runs()), 1)

	def test_files_skipped_or_failed_by_collect_are_retried_and_stored_ones_are_left(self):
		client = standard_world()
		client.files.fault(lambda r: r.kind == 'GetFile' and r.media_id == 9001, errors.FileIdInvalidError(request=None))
		code, _, err = self.media('--max-media-size', '4K', targets=BATCH, client=client)
		self.assertEqual(code, 1, err)
		states = self.states()
		self.assertEqual(states[(ALPHA, 1)], ('failed', 'request_failed'))
		self.assertEqual(states[(BETA, 2)], ('complete', None))
		self.assertEqual(states[(ALPHA, 2)], ('skipped', 'over_file_limit'))
		client.timeline.clear()
		client.lookups.clear()

		code, stdout, err = self.download(client=client)
		self.assertEqual(code, 0, err)
		self.assertStored()
		# the stored photo of beta is neither fetched again nor requested
		self.assertEqual(
			sorted((channel, sorted(ids)) for channel, _, ids in client.lookups),
			[(ALPHA, [1, 2, 4, 5, 6]), (BETA, [1])]
		)
		self.assertNotIn(9101, [event[1] for event in get_files(client)])
		self.assertIn('1 selected file(s) were already stored', stdout)

	def test_target_and_types_narrow_the_run(self):
		client = self.collected(storage='sqlite')
		code, _, err = self.download('--telegram-channel', 'BETA_FX', '--media-types', 'image', client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual(self.states(), {(BETA, 2): ('complete', None)})
		self.assertEqual(client.lookups, [(BETA, ACCESS_HASH, [2])])
		run, = self.runs()
		self.assertEqual(json.loads(run['selection'])['types'], ['image'])

	def test_a_target_without_a_saved_history_fails_the_run(self):
		client = self.collected()
		code, _, err = self.download('--batch-file', self.batch_file(['beta_fx', 'gamma_fx']), client=client)
		self.assertEqual(code, 1)
		self.assertIn('1 target(s) have no saved history in this folder: gamma_fx', err)
		self.assertEqual(sorted(self.states()), [(BETA, 1), (BETA, 2)])

	def test_the_folder_is_held_as_collect_holds_it(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = self.collected(storage=storage, out=out)
				held = []

				async def look(request):
					held.append((
						os.path.exists(os.path.join(out, LOCK_FILE)),
						os.path.exists(database_lease_path(self.db(out)))
					))

				client.files.fault(any_request('GetMessages'), look)
				self.assertEqual(self.download(out=out, client=client)[0], 0)
				self.assertEqual(held, [(True, True)])
				self.assertNoLocks(out)

	def test_files_are_fetched_again_batch_by_batch(self):
		client = self.collected()
		with mock.patch.object(fetch, 'LOOKUP_BATCH', 2):
			code, _, err = self.download('--telegram-channel', 'alpha_fx', client=client)
		self.assertEqual(code, 0, err)
		self.assertEqual([len(ids) for _, _, ids in client.lookups], [2, 2, 1])
		# each batch's files are requested before the next batch is fetched
		order = [event[0] for event in client.timeline if event[0] in ('GetMessages', 'GetFile')]
		self.assertEqual(order[0], 'GetMessages')
		batches = [i for i, kind in enumerate(order) if kind == 'GetMessages']
		self.assertTrue(all(order[i + 1] == 'GetFile' for i in batches[:-1] if i + 1 < len(order)))
		self.assertStored(keys=[key for key in SIZES if key[0] == ALPHA])


class OutcomeTests(FetchCase):

	def test_a_message_gone_or_with_other_media_fails_that_file_only(self):
		client = self.collected()
		files = client.files
		del files.messages[(BETA, 1)]
		files.post(4, document(files, 7444, 4000, 9), ALPHA)
		code, _, err = self.download(client=client)
		self.assertEqual(code, 1)
		states = self.states()
		self.assertEqual(states[(BETA, 1)], ('failed', 'message_gone'))
		self.assertEqual(states[(ALPHA, 4)], ('failed', 'media_changed'))
		self.assertStored(keys=[key for key in SIZES if key not in ((BETA, 1), (ALPHA, 4))])
		self.assertNotIn(7101, [event[1] for event in get_files(client)])
		self.assertIn('2 media file(s) failed', err)

	def test_a_failed_lookup_ends_that_channel_only(self):
		client = self.collected()
		client.files.fault(any_request('GetMessages'), errors.ChannelPrivateError(request=None))
		code, _, err = self.download(client=client)
		self.assertEqual(code, 1)
		states = self.states()
		self.assertEqual({states[key] for key in SIZES if key[0] == ALPHA}, {('failed', 'request_failed')})
		self.assertStored(keys=[(BETA, 1), (BETA, 2)])
		self.assertIn('alpha_fx: fetching its messages again failed (ChannelPrivateError)', err)
		self.assertIn('1 channel(s) not fetched again: alpha_fx', err)

	def test_a_flood_wait_over_the_limit_stops_the_run(self):
		client = self.collected()
		client.files.fault(any_request(), flood(120), times=1)
		code, _, err = self.download('--max-flood-wait', '10', client=client)
		self.assertEqual(code, 1)
		run, = self.runs()
		self.assertEqual(run['state'], 'interrupted')
		self.assertIn('account stopped', run['stop_reason'])
		self.assertIn('> Stopped: account stopped', err)
		states = self.states()
		self.assertIn(('failed', 'flood_stop'), states.values())
		# beta was not reached: no row, no lookup
		self.assertNotIn(BETA, [channel for channel, _, _ in client.lookups])
		self.assertFalse([key for key in states if key[0] == BETA])

	def test_another_profile_s_history_is_refused_before_any_request(self):
		for storage in ('files', 'sqlite'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = self.collected(storage=storage, out=out)
				if storage == 'files':
					path = os.path.join(out, 'beta_fx', 'beta_fx_provenance.json')
					with open(path, encoding='utf-8') as reader:
						record = json.load(reader)
					record.update(profile_name='alice', profile_id=None)
					with open(path, 'w', encoding='utf-8') as writer:
						json.dump(record, writer)
				else:
					con = sqlite3.connect(self.db(out))
					with con:
						con.execute("UPDATE histories SET profile_name = 'alice', profile_id = NULL WHERE channel_id = ?",
							(BETA,))
					con.close()
				code, _, err = self.download(out=out, client=client)
				self.assertEqual(code, 1)
				self.assertIn(
					'beta_fx: its saved history was collected by profile alice; download-media needs the same '
					'profile', err)
				self.assertIn('Use --profile alice', err)
				self.assertEqual([channel for channel, _, _ in client.lookups], [ALPHA])
				self.assertEqual({key[0] for key in self.states(out)}, {ALPHA})

	def test_an_interrupted_collection_is_read_with_a_note(self):
		client = self.collected()
		path = os.path.join(self.out, 'collection-status.json')
		with open(path, encoding='utf-8') as reader:
			status = json.load(reader)
		status.update(state='interrupted', stop_reason='account stopped: test')
		with open(path, 'w', encoding='utf-8') as writer:
			json.dump(status, writer)
		code, _, err = self.download(client=client)
		self.assertEqual(code, 0, err)
		self.assertIn('the last collection was interrupted (account stopped: test)', err)
		self.assertStored()

	def test_a_dead_media_run_is_recorded_and_the_run_goes_on(self):
		client = self.collected('--media-types', 'image', media=True)
		with open_ledger(MediaLayout(self.out), folder_identity(self.out)) as ledger:
			ledger.start_run(OLD_RUN, 'collect', selection=MediaSelection().record())
		code, _, err = self.download(client=client)
		self.assertEqual(code, 0, err)
		self.assertIn('1 earlier media run(s) never finished', err)
		runs = {run['run_uuid']: run for run in self.runs()}
		self.assertEqual(runs[OLD_RUN]['state'], 'interrupted')
		self.assertStored()

	def test_a_session_rejected_during_a_lookup_stops_the_run(self):
		client = self.collected()
		client.files.fault(any_request('GetMessages'), errors.AuthKeyUnregisteredError(request=None))
		code, _, err = self.download(client=client)
		self.assertEqual(code, 1)
		run, = self.runs()
		self.assertEqual(run['state'], 'interrupted')
		self.assertTrue(run['stop_reason'].startswith('session rejected: '), run['stop_reason'])
		self.assertIn('> Stopped: session rejected', err)
		self.assertIn('log in again', err)
		# the first channel's items stay requested; the second was not reached
		self.assertEqual(len(client.lookups), 1)
		channel = client.lookups[0][0]
		states = self.states()
		self.assertEqual({key[0] for key in states}, {channel})
		self.assertEqual(set(states.values()), {('requested', None)})
		self.assertEqual(get_files(client), [])
		self.assertNoLocks()


class SourceRunTests(FetchCase):
	'''
	Each item names the collection run that wrote its message, in the
	form collect records it, not the folder's last collection run.
	'''

	def collection_run(self, out):
		status = self.status(out)
		return (status.get('database') or {}).get('run_uuid') or status['started_at']

	def later(self, *extra, out, client, media=False):
		'''
		Collect alpha_fx again, with another started_at than the first run
		(files name runs by their started_at, to the second).
		'''
		run = self.media if media else self.collect
		with mock.patch.object(status_module, 'utc_now', return_value=LATER):
			code, _, err = run(*extra, targets=('alpha_fx',), out=out, client=client)
		self.assertEqual(code, 0, err)
		client.timeline.clear()
		client.lookups.clear()
		return self.collection_run(out)

	def sources(self, out):
		return {(i['channel_id'], i['message_id']): i['source_run'] for i in self.items(out)}

	def test_an_item_names_the_run_that_wrote_its_history_in_each_mode(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = self.collected(storage=storage, out=out)
				first = self.collection_run(out)
				second = self.later('--yes', out=out, client=client)
				self.assertNotEqual(first, second)

				code, _, err = self.download(out=out, client=client)
				self.assertEqual(code, 0, err)
				self.assertStored(out)
				expected = {key: second if key[0] == ALPHA else first for key in SIZES}
				self.assertEqual(self.sources(out), expected)
				# the media run still names the folder's last collection run
				run, = self.runs(out)
				self.assertEqual(run['collection_run'], second)

	def test_a_retry_keeps_the_run_collect_recorded(self):
		# message 1 of alpha fails under collect, then alpha is merged by
		# --update with a new message 7: files cannot tell which run wrote
		# a merged history's messages, a database can
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = standard_world()
				client.files.fault(
					lambda r: r.kind == 'GetFile' and r.media_id == 9001, errors.FileIdInvalidError(request=None))
				code, _, err = self.media(targets=BATCH, out=out, storage=storage, client=client)
				self.assertEqual(code, 1, err)
				first = self.collection_run(out)
				self.assertEqual(self.states(out)[(ALPHA, 1)], ('failed', 'request_failed'))
				self.assertEqual(self.sources(out)[(ALPHA, 1)], first)
				client.files.post(7, document(client.files, 7555, 3000, 11), ALPHA)
				second = self.later('--update', out=out, client=client)

				code, _, err = self.download(out=out, client=client)
				self.assertEqual(code, 0, err)
				self.assertEqual(self.states(out)[(ALPHA, 1)], ('complete', None))
				self.assertEqual(self.states(out)[(ALPHA, 7)], ('complete', None))
				sources = self.sources(out)
				self.assertEqual(sources[(ALPHA, 1)], first)
				self.assertEqual(sources[(ALPHA, 7)], second if storage == 'sqlite' else None)


class PreflightTests(FetchCase):

	def test_a_locked_folder_is_refused_before_login(self):
		client = self.collected()
		open(os.path.join(self.out, LOCK_FILE), 'w').close()
		calls = self.factory_calls
		code, stdout, err = self.download(client=client)
		self.assertEqual(code, 1)
		self.assertIn('is in use by another collection', err)
		self.assertEqual(self.factory_calls, calls)
		# before credentials: the run never started
		self.assertNotIn('Init program', stdout)

	def test_a_missing_folder_is_refused(self):
		code, _, err = self.download(out=os.path.join(self.tmp, 'nowhere'))
		self.assertEqual(code, 1)
		self.assertIn('does not exist', err)
		self.assertEqual(self.factory_calls, 0)

	def test_argument_errors(self):
		self.collected()
		for extra in (
				('--check', '--media-types', 'image'), ('--check', '--telegram-channel', 'alpha_fx'),
				('--check', '--concurrency', '2'), ('--check', '--dry-run'), ('--media-types', 'pictures'),
				('--max-media-size', '0')):
			with self.subTest(extra=extra):
				code, _, err = self.download(*extra)
				self.assertEqual(code, 2, err)
		self.assertIn('--check takes only --output and --source, not --media-types', self.download(
			'--check', '--media-types', 'image')[2])

	def test_source_sqlite_needs_a_database(self):
		self.collected()
		code, _, err = self.download('--source', 'sqlite', '--dry-run')
		self.assertEqual(code, 1)
		self.assertIn('has no database', err)


class PlanTests(offline_guard.OfflineTestCase):
	'''
	media.plan on a stand-in reader.
	'''

	class Collection:
		def __init__(self, histories, chats=()):
			self.listed = histories
			self.chats = list(chats)

		def histories(self):
			return iter(self.listed)

		def channel_chats(self, history):
			return self.chats

	@staticmethod
	def history(name, channel_id, messages, chats=()):
		payload = {'messages': messages, 'chats': list(chats)}
		return ChannelHistory(name, name, channel_id, HistoryProvenance(), lambda: payload)

	def test_only_a_full_channel_of_that_id_gives_the_access_hash(self):
		chats = [
			{'_': 'Channel', 'id': 5, 'min': True, 'access_hash': 1}, {'_': 'Channel', 'id': 6, 'access_hash': 2},
			{'_': 'Chat', 'id': 5, 'access_hash': 3}, {'_': 'Channel', 'id': 5, 'access_hash': '4'},
			{'_': 'Channel', 'id': 5, 'access_hash': 9}
		]
		self.assertEqual(plan.stored_access_hash(chats, 5), 9)
		self.assertIsNone(plan.stored_access_hash(chats[:4], 5))
		# the history's chats first, then the channel as stored
		found = plan.read_plans(self.Collection([self.history('a', 5, [], chats[:4])], chats[4:]), MediaSelection())
		self.assertEqual(found.plans[0].access_hash, 9)

	def test_a_second_history_of_a_channel_is_a_duplicate(self):
		histories = [self.history('alpha', 5, []), self.history('alias', 5, []), self.history('beta', 6, [])]
		found = plan.read_plans(self.Collection(histories), MediaSelection(), ['ALPHA', 'alias', 'beta', 'gamma'])
		self.assertEqual([p.name for p in found.plans], ['alpha', 'beta'])
		self.assertEqual(found.duplicates, [('alias', 'alpha')])
		self.assertEqual(found.missing, ['gamma'])

	def test_the_messages_give_the_channel_id_without_a_snapshot(self):
		messages = [{'_': 'Message', 'id': 3, 'peer_id': {'_': 'PeerChannel', 'channel_id': 42}}]
		found = plan.read_plans(self.Collection([self.history('a', None, messages)]), MediaSelection())
		self.assertEqual(found.plans[0].channel_id, 42)


class DryRunTests(FetchCase):

	def test_dry_run_lists_what_a_run_would_request_offline(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				self.collected(storage=storage, out=out)
				before = file_contents(out, skip=())
				listing = sorted(os.listdir(out))
				calls = self.factory_calls
				code, stdout, err = self.download('--dry-run', '--max-media-size', '8K', out=out)
				self.assertEqual(code, 0, err)
				self.assertEqual(self.factory_calls, calls)
				self.assertEqual(sorted(os.listdir(out)), listing)
				self.assertEqual(file_contents(out, skip=()), before)
				# beta's 9000-byte video and alpha's 600 KiB video (twice) are over 8 KiB
				self.assertIn('>   alpha_fx video 0 / 0 / 0 / 2', stdout)
				self.assertIn('>   beta_fx video 0 / 0 / 0 / 1', stdout)
				self.assertIn('>   alpha_fx file  1 / 7000 / 0 / 0', stdout)
				self.assertIn('> Total: 4 file(s), 17000 bytes known', stdout)

	def test_dry_run_leaves_out_stored_files(self):
		client = self.collected()
		self.assertEqual(self.download('--media-types', 'image', client=client)[0], 0)
		code, stdout, err = self.download('--dry-run')
		self.assertEqual(code, 0, err)
		self.assertIn('2 selected file(s) already stored; 5 not stored', stdout)
		self.assertNotIn(' image ', stdout)


if __name__ == '__main__':
	unittest.main()
