# -*- coding: utf-8 -*-
'''
`download-media --check`: the ledger, the stored files and the saved
histories compared, offline and read only, in every storage mode.
Through cli.main with fakes (no network, no credentials, synthetic
data).
'''

# import modules
import os
import shutil
import sqlite3
import unittest

from unittest import mock

from media_collect_support import ALPHA, BETA, MediaCase, document, standard_world, started_later
from storage_modes_support import file_contents, kill_inside_a_write

from telegram_tracker.media.identity import folder_identity
from telegram_tracker.media.layout import MEDIA_FOLDER, MediaLayout
from telegram_tracker.media.ledger import Ledger, open_ledger
from telegram_tracker.media.selection import MediaSelection


MODES = ('files', 'sqlite', 'both')
BATCH = ('alpha_fx', 'beta_fx')
OLD_RUN = '00000000-0000-4000-8000-000000000001'

# complete / failed / skipped / requested / downloading / selected, no row / not selected
ALL_COMPLETE = {
	'image': '2 / 0 / 0 / 0 / 0 / 0 / 0', 'video': '3 / 0 / 0 / 0 / 0 / 0 / 0',
	'audio': '1 / 0 / 0 / 0 / 0 / 0 / 0', 'file': '1 / 0 / 0 / 0 / 0 / 0 / 0'
}


class CheckCase(MediaCase):

	def check(self, *extra, out=None):
		return self.download('--check', *extra, out=out)

	def with_media(self, *extra, storage=None, out=None, client=None):
		client = client or standard_world()
		code, _, err = self.media(*extra, targets=BATCH, out=out, storage=storage, client=client)
		self.assertEqual(code, 0, err)
		return client

	def assertTable(self, stdout, expected):
		for media_type, counts in expected.items():
			self.assertIn(f'>   {media_type:<5} {counts}\n', stdout)

	def blob_paths(self, out=None):
		return [row['path'] for row in self.ledger_rows('SELECT path FROM blobs ORDER BY path', out=out)]


class ConsistentTests(CheckCase):

	def test_a_consistent_folder_passes_in_each_mode_and_nothing_is_written(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				self.with_media(storage=storage, out=out)
				before = file_contents(out, skip=())
				calls = self.factory_calls
				code, stdout, err = self.check(out=out)
				self.assertEqual(code, 0, err)
				self.assertEqual(self.factory_calls, calls)
				self.assertIn('> No problem found.', stdout)
				self.assertIn('> Identity: the ledger belongs to the dataset recorded in', stdout)
				self.assertTable(stdout, ALL_COMPLETE)
				self.assertIn('> Ledger items: 7; 7 match their saved message, 0 superseded', stdout)
				self.assertIn('> Stored files: 6 checked', stdout)
				self.assertEqual(file_contents(out, skip=()), before)
				self.assertNoLocks(out)

	def test_a_folder_without_media_has_every_reference_not_selected(self):
		code, _, err = self.collect(targets=BATCH, client=standard_world())
		self.assertEqual(code, 0, err)
		code, stdout, err = self.check()
		self.assertEqual(code, 0, err)
		self.assertIn('> No media ledger', stdout)
		self.assertTable(stdout, {'image': '0 / 0 / 0 / 0 / 0 / 0 / 2', 'video': '0 / 0 / 0 / 0 / 0 / 0 / 3'})
		self.assertFalse(os.path.lexists(os.path.join(self.out, MEDIA_FOLDER)))

	def test_failed_and_skipped_items_are_counted_not_problems(self):
		self.with_media('--max-media-size', '4K')
		code, stdout, err = self.check()
		self.assertEqual(code, 0, err)
		self.assertTable(stdout, {'image': '2 / 0 / 0 / 0 / 0 / 0 / 0', 'video': '0 / 0 / 3 / 0 / 0 / 0 / 0'})


class ProblemTests(CheckCase):

	def test_files_that_differ_are_missing_or_unrecorded_are_listed(self):
		self.with_media()
		root = os.path.join(self.out, MEDIA_FOLDER)
		changed, missing = self.blob_paths()[:2]
		with open(os.path.join(root, *changed.split('/')), 'ab') as writer:
			writer.write(b'x')
		os.remove(os.path.join(root, *missing.split('/')))
		orphan = f'{ALPHA}/files/9-{"ab" * 8}.pdf'
		with open(os.path.join(root, *orphan.split('/')), 'wb') as writer:
			writer.write(b'left by a stopped run')
		os.makedirs(os.path.join(root, 'notes'))
		partial = os.path.join(root, '.partial', OLD_RUN)
		os.makedirs(partial)
		with open(os.path.join(partial, f'{ALPHA}-1-9001.part'), 'wb') as writer:
			writer.write(b'partial')
		before = file_contents(self.out, skip=())

		code, stdout, err = self.check()
		self.assertEqual(code, 1)
		self.assertNotIn('No problem found', stdout)
		self.assertIn(f'1 recorded files missing: {missing}', err)
		self.assertIn(f'1 stored files whose size or SHA-256 differs from the record: {changed}', err)
		self.assertIn(f'1 files that no blob records (a run stopped after publishing them; download-media '
			f'adopts one when it downloads the same content again): {orphan}', err)
		self.assertIn('1 other entries under downloaded-media (left as they are, never followed): notes', err)
		self.assertIn(f'leftover partial downloads', err)
		self.assertIn(f'.partial/{OLD_RUN}', err)
		# nothing was repaired or removed
		self.assertEqual(file_contents(self.out, skip=()), before)

	def test_an_unfinished_run_is_reported_and_left_as_it_is(self):
		self.with_media('--media-types', 'image')
		with open_ledger(MediaLayout(self.out), folder_identity(self.out)) as ledger:
			ledger.start_run(OLD_RUN, 'download-media', selection=MediaSelection().record())
		code, _, err = self.check()
		self.assertEqual(code, 1)
		self.assertIn(f'1 runs recorded as running', err)
		self.assertIn(OLD_RUN, err)
		self.assertEqual(self.runs()[-1]['state'], 'running')

	def test_a_ledger_of_another_dataset_is_refused(self):
		other = os.path.join(self.tmp, 'other')
		self.with_media(out=other)
		self.assertEqual(self.collect(targets=BATCH, client=standard_world())[0], 0)
		shutil.copytree(os.path.join(other, MEDIA_FOLDER), os.path.join(self.out, MEDIA_FOLDER))
		code, _, err = self.check()
		self.assertEqual(code, 1)
		self.assertIn('cannot be tied to the collected data here', err)

	def test_an_unfinished_ledger_write_is_refused_naming_the_runs_that_recover_it(self):
		self.with_media()
		journal = kill_inside_a_write(self.ledger_path())
		code, _, err = self.check()
		self.assertEqual(code, 1)
		self.assertIn('has an unfinished write', err)
		self.assertIn('`download-media` (without --check) or `collect --include-media`', err)
		# read only: the journal is still there for a writer to roll back
		self.assertTrue(os.path.exists(journal))
		self.assertNoLocks()

	def test_a_locked_folder_is_refused(self):
		self.with_media()
		open(os.path.join(self.out, '.collection.lock'), 'w').close()
		code, _, err = self.check()
		self.assertEqual(code, 1)
		self.assertIn('is in use by another collection', err)


class HistoryTests(CheckCase):

	def test_an_edited_message_saved_again_with_other_media_is_superseded_not_an_orphan(self):
		# --update only adds messages newer than the saved ones; a later full
		# run saves the edited post again
		for storage in ('files', 'sqlite'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = self.with_media(storage=storage, out=out)
				files = client.files
				files.post(5, document(files, 7555, 4000, 9), ALPHA)
				with started_later():
					code, _, err = self.collect(targets=BATCH, out=out, client=client)
				self.assertEqual(code, 0, err)
				code, stdout, err = self.check(out=out)
				self.assertEqual(code, 0, err)
				self.assertIn('> Ledger items: 7; 6 match their saved message, 1 superseded', stdout)
				# the new document has no row: nothing selected it
				self.assertTable(stdout, {'file': '0 / 0 / 0 / 0 / 0 / 0 / 1'})
				self.assertIn('> Stored files: 6 checked', stdout)

	def test_items_of_a_replaced_history_are_not_in_history(self):
		client = self.with_media()
		code, _, err = self.collect('--min-id', '4', '--yes', targets=BATCH, client=client)
		self.assertEqual(code, 0, err)
		code, stdout, err = self.check()
		self.assertEqual(code, 0, err)
		# alpha keeps 5 and 6; beta (ids 1 and 2) keeps nothing
		self.assertIn('2 match their saved message, 0 superseded (the message was merged later with other '
			'media), 5 whose message is not in the current histories', stdout)

	def test_references_a_stopped_collect_never_recorded_are_selected_no_row(self):
		for storage, source in (('files', 'auto'), ('sqlite', 'auto'), ('both', 'files'), ('both', 'sqlite')):
			with self.subTest(storage=storage, source=source):
				out = os.path.join(self.tmp, f'{storage}-{source}')
				request = Ledger.request

				def refuse_beta(ledger, run_uuid, channel_id, refs, **kwargs):
					if channel_id == BETA:
						raise sqlite3.OperationalError('disk I/O error')
					return request(ledger, run_uuid, channel_id, refs, **kwargs)

				client = standard_world()
				with mock.patch.object(Ledger, 'request', refuse_beta):
					# one channel at a time: alpha's rows are recorded, then beta's fail
					code, _, err = self.media('--concurrency', '1', targets=BATCH, out=out, storage=storage,
						client=client)
				self.assertEqual(code, 1)
				self.assertIn('media downloads stopped: storage error', err)

				code, stdout, err = self.check('--source', source, out=out)
				self.assertEqual(code, 1)
				self.assertIn("2 references that a collect --include-media run selected but never recorded", err)
				self.assertIn(f'({BETA}, 1, 7101)', err)

				# download-media requests them; the folder is then consistent
				code, _, err = self.download(out=out, client=client)
				self.assertEqual(code, 0, err)
				code, stdout, err = self.check('--source', source, out=out)
				self.assertEqual(code, 0, err)
				self.assertTable(stdout, ALL_COMPLETE)


if __name__ == '__main__':
	unittest.main()
