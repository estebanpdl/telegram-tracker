# -*- coding: utf-8 -*-
'''
How build-urls reads and publishes: the collection database lease while
histories load, its own output lease for the whole build (two builds of
a folder refuse each other in every mode), outputs replaced only after
the collection lease is released, failures that leave earlier outputs
and no temporary files, and outputs of different builds found and noted.
Synthetic fixtures only.
'''

# import modules
import json
import os
import re
import sqlite3
import unittest

from unittest import mock

from builder_parity_support import BATCH, ParityCase, collect, read_bytes
from url_build_support import inputs, leftovers, output_bytes, record, run_build
from url_data import add_url_messages

from telegram_tracker.datasets import build_dataset
from telegram_tracker.reader import database as reader_database
from telegram_tracker.reader import files as reader_files
from telegram_tracker.storage.sqlite.lease import DatabaseLocked, check_free
from telegram_tracker.storage.sqlite.schema import DB_FILENAME
from telegram_tracker.urls import build as urls_build
from telegram_tracker.urls.build import build_urls
from telegram_tracker.urls.outputs import BUILD_FILE, URLS_DATABASE, URLS_DATASET, publication_problem
from telegram_tracker.utils import DataInputError


OUTPUT_LEASE = URLS_DATABASE + '.lock'


class LifecycleCase(ParityCase):

	def folder(self, mode):
		out = self.folders[mode]
		collect(out, mode, BATCH, world_change=add_url_messages)
		return out


class LeaseTests(LifecycleCase):

	def test_histories_load_under_both_leases(self):
		out = self.folder('sqlite')
		db = os.path.join(out, DB_FILENAME)
		original = reader_database.DatabaseCollection.load_history
		seen = []

		def load(collection, channel_id):
			# collection and the other builders are refused while it loads
			with self.assertRaises(DatabaseLocked):
				check_free(db)
			self.assertTrue(os.path.exists(os.path.join(out, OUTPUT_LEASE)))
			seen.append(channel_id)
			return original(collection, channel_id)

		with mock.patch.object(reader_database.DatabaseCollection, 'load_history', load):
			run_build(out)
		self.assertEqual(len(seen), 3)
		self.assertEqual(leftovers(out), [])

	def test_another_builder_is_refused_while_the_database_is_read(self):
		out = self.folder('sqlite')
		original = reader_database.DatabaseCollection.load_history
		refusals = []

		def load(collection, channel_id):
			if not refusals:
				with self.assertRaisesRegex(DataInputError, 'in use by another collection or upgrade, or a build'):
					build_dataset(out, progress=False)
				refusals.append(channel_id)
			return original(collection, channel_id)

		with mock.patch.object(reader_database.DatabaseCollection, 'load_history', load):
			run_build(out)
		self.assertEqual(len(refusals), 1)
		self.assertFalse(os.path.exists(os.path.join(out, 'msgs_dataset.csv')))

	def test_two_url_builds_of_a_files_folder_refuse_each_other(self):
		out = self.folder('files')
		original = reader_files.read_messages_file
		refusals = []

		def read(path):
			if not refusals:
				with self.assertRaisesRegex(DataInputError, f'The URL outputs .* is in use by another process'):
					build_urls(out)
				refusals.append(path)
			return original(path)

		with mock.patch.object(reader_files, 'read_messages_file', read):
			run_build(out)
		self.assertEqual(len(refusals), 1)
		self.assertIsNone(publication_problem(out))

	def test_a_held_output_lease_refuses_before_reading(self):
		out = self.folder('both')
		lease = os.path.join(out, OUTPUT_LEASE)
		with open(lease, 'w', encoding='utf-8') as fh:
			fh.write('{"pid": 1}')
		before = inputs(out)
		with mock.patch.object(urls_build, 'open_collection') as opened, \
				self.assertRaisesRegex(DataInputError, f'pid 1.*delete {re.escape(lease)}'):
			build_urls(out)
		opened.assert_not_called()
		self.assertEqual(read_bytes(lease), b'{"pid": 1}')
		self.assertEqual(inputs(out), before)
		self.assertEqual(output_bytes(out), {})

	def test_outputs_are_replaced_after_the_collection_lease_is_released(self):
		out = self.folder('sqlite')
		db = os.path.join(out, DB_FILENAME)
		real_replace = os.replace
		replaced = []

		def replace(src, dst):
			check_free(db)
			self.assertTrue(os.path.exists(os.path.join(out, OUTPUT_LEASE)))
			replaced.append(os.path.basename(dst))
			return real_replace(src, dst)

		with mock.patch.object(urls_build.os, 'replace', replace):
			run_build(out)
		self.assertEqual(replaced, [URLS_DATASET, URLS_DATABASE, BUILD_FILE])


class FailureTests(LifecycleCase):

	def test_a_malformed_history_replaces_nothing(self):
		out = self.folder('sqlite')
		run_build(out)
		earlier = output_bytes(out)
		con = sqlite3.connect(os.path.join(out, DB_FILENAME))
		with con:
			con.execute(
				"UPDATE messages SET raw = json_remove(raw, '$.id') WHERE channel_id = 101 AND message_id = 12")
		con.close()
		before = inputs(out)
		with self.assertRaisesRegex(DataInputError, r'history of alpha_fx.*cannot read message fields \(KeyError'):
			run_build(out)
		self.assertEqual(output_bytes(out), earlier)
		self.assertEqual(inputs(out), before)
		self.assertEqual(leftovers(out), [])

	def test_a_missing_folder(self):
		missing = os.path.join(self.tmp, 'nowhere')
		with self.assertRaisesRegex(DataInputError, 'not found; run collection first'):
			build_urls(missing)
		self.assertFalse(os.path.exists(missing))

	def test_a_source_the_folder_lacks(self):
		out = self.folder('files')
		with self.assertRaisesRegex(DataInputError, 'has no database'):
			build_urls(out, source='sqlite')
		self.assertEqual(leftovers(out), [])
		self.assertEqual(output_bytes(out), {})

	def test_a_stop_while_publishing_is_found_and_repaired(self):
		out = self.folder('files')
		run_build(out)
		earlier = output_bytes(out)
		real_replace = os.replace
		calls = []

		def replace(src, dst):
			calls.append(os.path.basename(dst))
			if len(calls) == 2:
				raise OSError('disk full')
			return real_replace(src, dst)

		# a changed history, so the new CSV differs from the published one
		with open(os.path.join(out, 'beta_fx', 'beta_fx_messages.json'), encoding='utf-8') as fh:
			payload = json.load(fh)
		payload['messages'] = []
		with open(os.path.join(out, 'beta_fx', 'beta_fx_messages.json'), 'w', encoding='utf-8') as fh:
			json.dump(payload, fh)
		with mock.patch.object(urls_build.os, 'replace', replace), \
				self.assertRaisesRegex(OSError, 'disk full'):
			run_build(out)
		self.assertEqual(calls, [URLS_DATASET, URLS_DATABASE])
		self.assertEqual(leftovers(out), [])
		now = output_bytes(out)
		self.assertNotEqual(now[URLS_DATASET], earlier[URLS_DATASET])
		self.assertEqual((now[URLS_DATABASE], now[BUILD_FILE]), (earlier[URLS_DATABASE], earlier[BUILD_FILE]))
		self.assertEqual(publication_problem(out),
			f'{URLS_DATASET} is not from the build {BUILD_FILE} describes')

		_, _, err = run_build(out)
		self.assertIn(f'> Note: {URLS_DATASET} is not from the build {BUILD_FILE} describes; this build '
			'replaces all three outputs.', err)
		self.assertIsNone(publication_problem(out))


class PublicationTests(LifecycleCase):

	def setUp(self):
		super().setUp()
		self.out = self.folder('files')
		run_build(self.out)
		self.paths = {name: os.path.join(self.out, name) for name in (URLS_DATASET, URLS_DATABASE, BUILD_FILE)}

	def assertProblem(self, expected):
		self.assertEqual(publication_problem(self.out), expected)
		_, _, err = run_build(self.out)
		self.assertIn(f'> Note: {expected}; this build replaces all three outputs.', err)
		self.assertIsNone(publication_problem(self.out))

	def test_a_complete_build(self):
		self.assertIsNone(publication_problem(self.out))
		_, _, err = run_build(self.out)
		self.assertNotIn('replaces all three', err)

	def test_no_outputs(self):
		self.assertIsNone(publication_problem(self.tmp))

	def test_outputs_without_a_record(self):
		os.remove(self.paths[BUILD_FILE])
		self.assertProblem(f'{URLS_DATASET}, {URLS_DATABASE} found without {BUILD_FILE}: an earlier '
			'build did not finish')

	def test_an_unreadable_record(self):
		with open(self.paths[BUILD_FILE], 'w', encoding='utf-8') as fh:
			fh.write('{"build_uuid": ')
		self.assertProblem(f'{BUILD_FILE} cannot be read')

	def test_a_database_of_another_build(self):
		built = record(self.out)
		built['build_uuid'] = '00000000-0000-4000-8000-000000000000'
		with open(self.paths[BUILD_FILE], 'w', encoding='utf-8') as fh:
			json.dump(built, fh)
		self.assertProblem(f'{URLS_DATABASE} is not from the build {BUILD_FILE} describes')

	def test_a_missing_or_changed_csv(self):
		with open(self.paths[URLS_DATASET], 'ab') as fh:
			fh.write(b'x\n')
		self.assertProblem(f'{URLS_DATASET} is not from the build {BUILD_FILE} describes')
		os.remove(self.paths[URLS_DATASET])
		self.assertProblem(f'{URLS_DATASET} is not from the build {BUILD_FILE} describes')

	def test_a_damaged_database(self):
		with open(self.paths[URLS_DATABASE], 'wb') as fh:
			fh.write(b'not a database')
		self.assertProblem(f'{URLS_DATABASE} is not from the build {BUILD_FILE} describes')


if __name__ == '__main__':
	unittest.main()
