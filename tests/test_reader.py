# -*- coding: utf-8 -*-
'''
The builders' collection reader (telegram_tracker.reader): source
selection, the gates before a database is read (each refusal leaves the
inputs and earlier outputs as they were and no lease behind), the lease
and read-only snapshot while reading, batched reads, the rebuilt
collected_chats text, notes, and the entry points on a sqlite folder.
Synthetic fixtures only.
'''

# import modules
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from datetime import datetime, timezone
from unittest import mock

import builder_data
import offline_guard

from builder_parity_support import BATCH, ParityCase, build, collect, inputs, read_bytes

from telegram_tracker.datasets import build_dataset
from telegram_tracker.network import build_network
from telegram_tracker.reader import database as reader_database
from telegram_tracker.reader import open_collection
from telegram_tracker.storage.manifest import MANIFEST_FILE
from telegram_tracker.storage.records import STATUS_FILE
from telegram_tracker.storage.sqlite.connection import create_database
from telegram_tracker.storage.sqlite.lease import DatabaseLocked, check_free, database_lease_path
from telegram_tracker.storage.sqlite.schema import DB_FILENAME
from telegram_tracker.utils import DataInputError


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def write_json(path, data):
	with open(path, 'w', encoding='utf-8') as fh:
		json.dump(data, fh)


def leases(folder):
	return sorted(n for n in os.listdir(folder) if n.endswith('.lock'))


@contextlib.contextmanager
def quiet():
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		yield


class ReaderCase(ParityCase):

	def folder(self, mode='sqlite', targets=BATCH, **kwargs):
		out = self.folders[mode]
		collect(out, mode, targets, **kwargs)
		return out

	def execute(self, out, sql, *args):
		con = sqlite3.connect(os.path.join(out, DB_FILENAME))
		try:
			with con:
				con.execute(sql, args)
		finally:
			con.close()


class ReconstructionTests(ReaderCase):

	def database_text(self, out):
		with open_collection(out, 'sqlite') as collection:
			return collection.collected_chats_text()

	def file_text(self, out):
		with open(os.path.join(out, 'collected_chats.csv'), encoding='utf-8', newline='') as fh:
			return fh.read()

	def test_rebuilt_text_is_the_file(self):
		out = self.folder('both')
		self.assertEqual(self.database_text(out), self.file_text(out))
		# runs with and without rows appended after it
		collect(out, 'both', ['alpha_fx'], metadata_only=True)
		collect(out, 'both', ['missing_fx'])
		collect(out, 'both', BATCH)
		self.assertEqual(self.database_text(out), self.file_text(out))

	def test_no_rows_is_a_header_only_file(self):
		out = self.folder('both', ['missing_fx'])
		text = self.database_text(out)
		self.assertEqual(text, self.file_text(out))
		self.assertEqual(len(text.splitlines()), 1)

	def test_each_run_keeps_its_own_counts(self):
		out = self.folder('both')
		collect(out, 'both', ['alpha_fx'])
		con = sqlite3.connect(os.path.join(out, DB_FILENAME))
		try:
			counts = con.execute(
				'SELECT run_id, counter FROM run_counters WHERE channel_id = 106 ORDER BY run_id').fetchall()
		finally:
			con.close()
		# shared_fx (106) was counted differently by the two runs
		self.assertEqual(len({c for _, c in counts}), 2, counts)
		self.assertEqual(self.database_text(out), self.file_text(out))

	def test_nested_dates_read_as_stored_text(self):
		'''
		The documented difference: a datetime nested in an object column
		is a Python repr in the files and the stored text in the database.
		'''
		until = datetime(2038, 1, 19, 3, 14, 7, tzinfo=timezone.utc)

		def restrict(world):
			world.chats[106]['default_banned_rights'] = {'_': 'ChatBannedRights', 'until_date': until}

		out = self.folder('both', world_change=restrict)
		stored = self.database_text(out)
		written = self.file_text(out)
		self.assertIn("'until_date': datetime.datetime(2038, 1, 19, 3, 14, 7, tzinfo=datetime.timezone.utc)", written)
		self.assertIn("'until_date': '2038-01-19 03:14:07+00:00'", stored)
		self.assertEqual(
			stored.replace("'2038-01-19 03:14:07+00:00'", 'X'),
			written.replace('datetime.datetime(2038, 1, 19, 3, 14, 7, tzinfo=datetime.timezone.utc)', 'X')
		)


class RecordTests(ReaderCase):

	def histories(self, out, source):
		with open_collection(out, source) as collection:
			return [(h.name, h.channel_id, h.load()) for h in collection.histories()]

	def test_both_sources_give_the_same_histories(self):
		out = self.folder('both')
		collect(out, 'both', BATCH, update=True)
		files = self.histories(out, 'files')
		stored = self.histories(out, 'sqlite')
		self.assertEqual([h[:2] for h in stored], [h[:2] for h in files])
		self.assertGreater(len(files), 1)
		ids = lambda items: [(i.get('_'), i['id']) for i in items]
		for (name, _, want), (_, _, got) in zip(files, stored):
			with self.subTest(channel=name):
				self.assertEqual(sorted(got), sorted(want))
				self.assertEqual(got['messages'], want['messages'])
				self.assertEqual(got['count'], want['count'])
				self.assertEqual(ids(got['chats']), ids(want['chats']))
				self.assertEqual(ids(got['users']), ids(want['users']))
		self.assertTrue(any(h[2]['chats'] for h in stored))


class SelectionTests(ReaderCase):

	def test_parser_choices_are_the_reader_sources(self):
		from telegram_tracker import reader
		from telegram_tracker.cli import parsers

		self.assertEqual(parsers.SOURCES, reader.SOURCES)
		self.assertEqual(parsers.AUTO, reader.AUTO)

	def test_auto_reads_the_files_of_a_both_folder(self):
		out = self.folder('both')
		with open_collection(out) as collection:
			self.assertEqual((collection.source, collection.storage), ('files', 'both'))
		with open_collection(out, 'sqlite') as collection:
			self.assertEqual((collection.source, collection.storage), ('sqlite', 'both'))

	def test_auto_reads_the_database_of_a_sqlite_folder(self):
		out = self.folder('sqlite')
		with open_collection(out) as collection:
			self.assertEqual(collection.source, 'sqlite')
		with self.assertRaisesRegex(DataInputError, 'holds only the database'):
			with open_collection(out, 'files'):
				pass

	def test_a_files_folder_has_no_database(self):
		out = self.folder('files')
		with self.assertRaisesRegex(DataInputError, 'has no database'):
			with open_collection(out, 'sqlite'):
				pass

	def test_files_never_fall_back_to_the_database(self):
		out = self.folder('both')
		os.remove(os.path.join(out, 'collected_chats.csv'))
		with self.assertRaisesRegex(DataInputError, 'collected_chats.csv not found'):
			build(out)
		# the database still reads when chosen
		dataset, _, _, _ = build(out, 'sqlite')
		self.assertGreater(dataset.rows_written, 0)

	def loaded_database_modules(self, out, source):
		code = (
			'import sys, json\n'
			f'sys.path[:0] = [{builder_data.TESTS!r}, {os.path.join(builder_data.ROOT, "src")!r}]\n'
			'import offline_guard\n'
			'from telegram_tracker.datasets import build_dataset\n'
			'from telegram_tracker.network import build_network\n'
			f'build_dataset({out!r}, source={source!r}, progress=False)\n'
			f'build_network({out!r}, source={source!r}, image=False)\n'
			"print('LOADED', json.dumps(sorted(m for m in sys.modules if m.startswith('telegram_tracker.storage.sqlite') or m == 'telegram_tracker.reader.database')))\n"
		)
		import subprocess
		import sys
		done = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True,
			encoding='utf-8', timeout=300)
		line = [l for l in done.stdout.splitlines() if l.startswith('LOADED ')]
		self.assertTrue(line, done.stderr[-2000:])
		return json.loads(line[0][len('LOADED '):])

	def test_reading_files_loads_no_database_code(self):
		out = self.folder('both')
		self.assertEqual(self.loaded_database_modules(out, 'files'), [])
		# the same probe sees the database modules when they load
		self.assertIn('telegram_tracker.reader.database', self.loaded_database_modules(out, 'sqlite'))


class GateTests(ReaderCase):
	'''
	Each refusal: DataInputError naming the reason, inputs and an earlier
	msgs_dataset.csv unchanged, no output written, no lease left.
	'''

	def assertRefused(self, out, pattern, source='auto', *, lease_kept=False):
		earlier = os.path.join(out, 'msgs_dataset.csv')
		with open(earlier, 'wb') as fh:
			fh.write(b'earlier\n')
		before = inputs(out)
		locks = leases(out)
		for builder in (build_dataset, build_network):
			with self.subTest(builder=builder.__name__):
				with self.assertRaisesRegex(DataInputError, pattern), quiet():
					if builder is build_dataset:
						builder(out, source=source, progress=False)
					else:
						builder(out, source=source, image=False)
		self.assertEqual(inputs(out), before)
		self.assertEqual(read_bytes(earlier), b'earlier\n')
		for name in ('collected_chats.xlsx', 'Graph.gexf', 'dataset-build.json', 'network-build.json'):
			self.assertFalse(os.path.exists(os.path.join(out, name)), name)
		self.assertEqual(leases(out), locks if lease_kept else [])
		self.assertEqual([n for n in os.listdir(out) if '.tmp' in n], [])

	def sidecar(self, out, **changes):
		path = os.path.join(out, STATUS_FILE)
		data = read_json(path)
		data.update(changes)
		write_json(path, data)

	def test_running_sidecar(self):
		out = self.folder()
		self.sidecar(out, state='running')
		self.assertRefused(out, 'says a collection is still running')

	def test_interrupted_sidecar(self):
		out = self.folder()
		self.sidecar(out, state='interrupted', stop_reason='cancelled')
		self.assertRefused(out, r'was interrupted \(cancelled\)')

	def test_missing_sidecar_is_not_a_finished_collection(self):
		out = self.folder()
		os.remove(os.path.join(out, STATUS_FILE))
		self.assertRefused(out, 'is missing, so it is unknown whether the last collection')

	def test_sidecar_of_another_run(self):
		out = self.folder()
		data = read_json(os.path.join(out, STATUS_FILE))
		data['database']['run_uuid'] = '00000000-0000-4000-8000-000000000000'
		write_json(os.path.join(out, STATUS_FILE), data)
		self.assertRefused(out, 'describe different runs')

	def test_database_run_still_running(self):
		for state in ('running', 'interrupted'):
			with self.subTest(state=state):
				out = os.path.join(self.tmp, state)
				collect(out, 'sqlite', BATCH)
				self.execute(out, 'UPDATE collection_runs SET state = ? WHERE run_id = (SELECT max(run_id) '
					'FROM collection_runs)', state)
				self.assertRefused(out, f'says its last run is {state}, not finished')

	def test_missing_database(self):
		out = self.folder()
		os.remove(os.path.join(out, DB_FILENAME))
		self.assertRefused(out, f'{DB_FILENAME} is missing')

	def test_newer_database(self):
		out = self.folder()
		self.execute(out, 'PRAGMA user_version = 2')
		self.assertRefused(out, 'newer')

	def test_foreign_database(self):
		out = self.folder()
		os.remove(os.path.join(out, DB_FILENAME))
		con = sqlite3.connect(os.path.join(out, DB_FILENAME))
		con.execute('CREATE TABLE t (x)')
		con.close()
		self.assertRefused(out, 'not a Telegram Tracker')

	def test_damaged_database(self):
		out = self.folder()
		with open(os.path.join(out, DB_FILENAME), 'r+b') as fh:
			fh.write(b'not a database at all, just bytes')
		self.assertRefused(out, DB_FILENAME)

	def test_another_dataset(self):
		out = self.folder()
		path = os.path.join(out, MANIFEST_FILE)
		manifest = read_json(path)
		manifest['dataset_uuid'] = '00000000-0000-4000-8000-000000000000'
		write_json(path, manifest)
		self.assertRefused(out, 'another dataset')

	def test_database_without_runs(self):
		out = self.folders['sqlite']
		os.makedirs(out)
		db = os.path.join(out, DB_FILENAME)
		create_database(db)
		con = sqlite3.connect(db)
		dataset_uuid, = con.execute('SELECT dataset_uuid FROM schema_meta').fetchone()
		con.close()
		write_json(os.path.join(out, MANIFEST_FILE), {
			'format': 1, 'storage': 'sqlite', 'database': DB_FILENAME, 'dataset_uuid': dataset_uuid,
			'created_at': 'x'})
		write_json(os.path.join(out, STATUS_FILE), {'state': 'complete', 'database': {'run_uuid': None}})
		self.assertRefused(out, 'holds no collection run')

	def test_held_lease(self):
		out = self.folder()
		lease = database_lease_path(os.path.join(out, DB_FILENAME))
		with open(lease, 'w', encoding='utf-8') as fh:
			fh.write('{"pid": 1}')
		self.assertRefused(out, 'is in use by another collection or upgrade, or a build reading it',
			lease_kept=True)
		self.assertEqual(read_bytes(lease), b'{"pid": 1}')

	def test_pending_publication(self):
		for kind, sql in (
				('target', "UPDATE run_targets SET files_state = 'pending' WHERE input = 'alpha_fx'"),
				('aggregates', "UPDATE collection_runs SET aggregate_files = 'pending'")):
			with self.subTest(kind=kind):
				out = os.path.join(self.tmp, kind)
				collect(out, 'both', BATCH)
				self.execute(out, sql)
				self.assertRefused(out, 'is blocked', source='sqlite')
				# the files read as before
				dataset, _, _, _ = build(out)
				self.assertEqual(dataset.source, 'files')


class LeaseTests(ReaderCase):

	def test_reading_holds_the_lease_and_a_snapshot(self):
		out = self.folder()
		db = os.path.join(out, DB_FILENAME)
		original = reader_database.DatabaseCollection.load_history
		seen = []

		def load(collection, channel_id):
			payload = original(collection, channel_id)
			if not seen:
				# a collection would be refused now
				with self.assertRaises(DatabaseLocked):
					check_free(db)
				# the connection cannot write
				with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
					collection.con.execute("UPDATE schema_meta SET app_version = 'x'")
				# a writer ignoring the lease cannot commit while the snapshot is open
				writer = sqlite3.connect(db, timeout=0.1)
				try:
					with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
						with writer:
							writer.execute("UPDATE schema_meta SET app_version = 'x'")
				finally:
					writer.close()
			seen.append(channel_id)
			return payload

		before = inputs(out)
		with mock.patch.object(reader_database.DatabaseCollection, 'load_history', load):
			build(out)
		self.assertGreater(len(seen), 1)
		self.assertEqual(leases(out), [])
		self.assertEqual(inputs(out), before)

	def test_a_failed_read_releases_the_lease(self):
		out = self.folder()
		self.execute(out, "UPDATE histories SET envelope = '{}' WHERE channel_id = 101")
		with self.assertRaisesRegex(DataInputError, 'needs "count" and "messages"'), quiet():
			build_dataset(out, progress=False)
		self.assertEqual(leases(out), [])
		self.assertFalse(os.path.exists(os.path.join(out, 'msgs_dataset.csv')))

	def test_outputs_are_written_after_the_lease_is_released(self):
		out = self.folder()
		db = os.path.join(out, DB_FILENAME)
		real_replace = os.replace
		checked = []

		def replace(src, dst):
			check_free(db)
			checked.append(os.path.basename(dst))
			return real_replace(src, dst)

		with mock.patch('telegram_tracker.datasets.os.replace', replace), \
				mock.patch('telegram_tracker.network.os.replace', replace):
			build(out)
		self.assertEqual(sorted(checked), sorted([
			'msgs_dataset.csv', 'collected_chats.xlsx', 'dataset-build.json', 'Graph.gexf', 'network-build.json']))


class BatchTests(ReaderCase):

	def test_small_batches_read_everything(self):
		out = self.folder()
		build(out)
		expected = read_bytes(os.path.join(out, 'msgs_dataset.csv'))
		os.remove(os.path.join(out, 'msgs_dataset.csv'))
		with mock.patch.object(reader_database, 'BATCH_SIZE', 1):
			build(out)
		self.assertEqual(read_bytes(os.path.join(out, 'msgs_dataset.csv')), expected)


class NoteTests(ReaderCase):

	def test_a_moved_folder_is_read_with_a_note(self):
		out = self.folder()
		moved = os.path.join(self.tmp, 'moved')
		shutil.move(out, moved)
		_, _, stdout, stderr = build(moved)
		self.assertIn('was collected into another folder', stderr)
		self.assertIn('Reading the database of', stdout)

	def test_window_note_names_the_stored_histories(self):
		from telegram_tracker.collector.history import HistoryWindow

		out = self.folder()
		collect(out, 'sqlite', ['alpha_fx'], window=HistoryWindow(max_messages=1))
		self.assertEqual(read_json(os.path.join(out, STATUS_FILE))['parameters']['mode'], 'window')
		_, _, _, stderr = build(out)
		self.assertIn('its stored histories hold only that selection', stderr)

	def test_appending_to_an_existing_dataset_is_noted(self):
		for mode in ('files', 'sqlite'):
			with self.subTest(mode=mode):
				out = self.folder(mode)
				_, _, _, first = build(out)
				self.assertNotIn('this build appends its rows', first)
				_, _, _, second = build(out)
				self.assertIn('this build appends its rows', second)
				record = read_json(os.path.join(out, 'dataset-build.json'))
				self.assertTrue(record['outputs']['msgs_dataset.csv']['appended_to_existing'])


class EntryPointTests(offline_guard.OfflineTestCase):
	'''
	main.py and both legacy builder scripts build a sqlite folder in a
	fresh interpreter, loading no Telegram client, API or collector code.
	'''

	def test_scripts_build_a_sqlite_folder(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		out = os.path.join(os.path.realpath(tmp.name), 'data')
		collect(out, 'sqlite', BATCH)
		for script, argv, output in (
				('main.py', ['build-dataset', '-d', out], 'msgs_dataset.csv'),
				('build-datasets.py', ['--data-path', out, '--source', 'sqlite'], 'collected_chats.xlsx'),
				('main.py', ['build-network', '-d', out, '--source', 'auto'], 'Graph.gexf'),
				('channels-to-network.py', ['-d', out], 'network.png')):
			with self.subTest(script=script, argv=argv):
				if os.path.exists(os.path.join(out, output)):
					os.remove(os.path.join(out, output))
				report, stdout, stderr = builder_data.run_script(script, argv, tmp.name)
				self.assertEqual(report['code'], 0, stderr[-2000:])
				self.assertEqual(report['violations'], [])
				self.assertEqual(
					[m for m in report['modules'] if m != 'matplotlib'], [])
				self.assertIn('Reading the database of', stdout)
				self.assertTrue(os.path.isfile(os.path.join(out, output)))
		self.assertEqual(leases(out), [])


if __name__ == '__main__':
	unittest.main()
