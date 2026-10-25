# -*- coding: utf-8 -*-
'''
The media ledger file and its identity guard: creation, the refusals
that leave a file untouched, foreign-identity refusal in files, sqlite
and both folders, collection-identity.json, and the one-connection,
one-short-transaction rule.
'''

# import modules
import asyncio
import inspect
import os
import shutil
import sqlite3
import unittest
import uuid

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import CHANNEL, RUN_A, SELECTION, SHA_A, MediaTestCase, document_media, ref_of
from telegram_tracker.media import identity as identity_module
from telegram_tracker.media import ledger_schema
from telegram_tracker.media.identity import IDENTITY_FILE, FolderIdentity, IdentityError, ensure_identity, \
	folder_identity, read_identity_record
from telegram_tracker.media.layout import MediaLayout, MediaPathError
from telegram_tracker.media.ledger import InvalidTransition, Ledger, LedgerError, open_ledger
from telegram_tracker.media.ledger_schema import LEDGER_APPLICATION_ID, LedgerRefused
from telegram_tracker.storage.destination import canonical_folder
from telegram_tracker.storage.manifest import MANIFEST_FILE, write_manifest
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import create_database


def read_bytes(path):
	with open(path, 'rb') as reader:
		return reader.read()


def write_text(path, text):
	with open(path, 'w', encoding='utf-8') as writer:
		writer.write(text)


class CreationTests(MediaTestCase):

	def test_files_folder_ledger(self):
		ledger = self.files_ledger()
		record = read_identity_record(self.out)
		self.assertEqual(ledger.meta.dataset_uuid, record['dataset_uuid'])
		self.assertEqual(ledger.meta.storage, 'files')
		self.assertEqual(ledger.meta.output_binding, canonical_folder(self.out))
		self.assertEqual(str(uuid.UUID(ledger.meta.ledger_uuid)), ledger.meta.ledger_uuid)
		self.assertEqual(ledger.con.execute('PRAGMA application_id').fetchone(), (LEDGER_APPLICATION_ID,))
		self.assertEqual(ledger.con.execute('PRAGMA user_version').fetchone(), (1,))
		self.assertEqual(ledger.con.execute('PRAGMA foreign_keys').fetchone(), (1,))
		self.assertEqual(ledger.con.execute('PRAGMA recursive_triggers').fetchone(), (1,))
		self.assertEqual(sorted(os.listdir(self.layout.root)), ['media.sqlite3'])
		ledger.close()
		self.assertEqual(self.reopen().meta, ledger.meta)

	def test_every_connection_has_recursive_triggers(self):
		'''
		The creating connection, a writer and a reader: each one has the
		pragma on when it is closed.
		'''
		seen = []

		class Recorded(sqlite3.Connection):
			closed = False

			def close(self):
				# in-memory probes (library and DDL checks) hold no ledger
				if not self.closed and self.execute('PRAGMA database_list').fetchone()[2]:
					seen.append(self.execute('PRAGMA recursive_triggers').fetchone())
				self.closed = True
				super().close()

		real_connect = sqlite3.connect

		def recording_connect(*args, **kwargs):
			return real_connect(*args, factory=Recorded, **kwargs)

		with mock.patch.object(sqlite3, 'connect', recording_connect):
			self.files_ledger().close()
			self.reopen().close()
			self.reopen(readonly=True).close()
		# creation (1), the new ledger's connection, the writer, the reader
		self.assertEqual(seen, [(1,)] * 4)

	def test_a_library_without_recursive_triggers_is_refused(self):
		real_connect = ledger_schema.connect
		closed = []

		class Ignoring:
			def __init__(self, con):
				self.con = con

			def execute(self, sql, *args):
				if sql == 'PRAGMA recursive_triggers':
					return self.con.execute('SELECT 0')
				return self.con.execute(sql, *args)

			def close(self):
				closed.append(True)
				self.con.close()

		with mock.patch.object(ledger_schema, 'connect', lambda *a, **k: Ignoring(real_connect(*a, **k))):
			with self.assertRaises(LedgerRefused) as caught:
				self.files_ledger()
		self.assertIn('recursive triggers', str(caught.exception))
		self.assertEqual(closed, [True])
		self.assertFalse(os.path.exists(self.layout.ledger_path))

	def test_a_missing_ledger_is_created_only_when_asked(self):
		identity = ensure_identity(self.out)
		for options in ({}, {'readonly': True}, {'create': True, 'readonly': True}):
			with self.subTest(**options):
				with self.assertRaises(LedgerRefused):
					open_ledger(self.layout, identity, **options)
				self.assertFalse(os.path.exists(self.layout.root))

	def test_no_ledger_without_a_dataset_identity(self):
		with self.assertRaises(LedgerRefused) as caught:
			open_ledger(self.layout, folder_identity(self.out), create=True)
		self.assertIn('no dataset identity', str(caught.exception))
		self.assertFalse(os.path.exists(self.layout.ledger_path))

	def test_readonly_connection_cannot_write(self):
		self.files_ledger().close()
		ledger = self.reopen(readonly=True)
		with self.assertRaises(StorageError):
			ledger.start_run(RUN_A, 'download-media', selection=SELECTION)
		self.assertEqual(ledger.runs(), [])

	def test_a_file_that_appears_during_creation_is_kept(self):
		real_link = ledger_schema.link_new

		def racing_link(tmp, path):
			write_text(path, 'other process')
			return real_link(tmp, path)

		with mock.patch.object(ledger_schema, 'link_new', racing_link):
			with self.assertRaises(LedgerRefused) as caught:
				self.files_ledger()
		self.assertIn('appeared', str(caught.exception))
		self.assertEqual(read_bytes(self.layout.ledger_path), b'other process')
		self.assertEqual(os.listdir(self.layout.root), ['media.sqlite3'])

	def test_failed_creation_leaves_nothing(self):
		with mock.patch.object(ledger_schema, 'verify', side_effect=LedgerRefused('check failed')):
			with self.assertRaises(LedgerRefused):
				self.files_ledger()
		self.assertEqual(os.listdir(self.layout.root), [])

	def test_a_link_above_the_ledger_is_refused(self):
		identity = ensure_identity(self.out)
		target = os.path.join(self.tmp, 'elsewhere')
		os.mkdir(target)
		self.link(target, self.layout.root)
		with self.assertRaises(MediaPathError):
			open_ledger(self.layout, identity, create=True)
		self.assertEqual(os.listdir(target), [])


class RefusalTests(MediaTestCase):
	'''
	Files that are not a usable ledger are refused and left untouched.
	'''
	def setUp(self):
		super().setUp()
		self.identity = ensure_identity(self.out)
		os.mkdir(self.layout.root)
		self.path = self.layout.ledger_path

	def assertRefused(self, *fragments):
		before = read_bytes(self.path) if os.path.isfile(self.path) else None
		for options in ({}, {'create': True}, {'readonly': True}):
			with self.subTest(**options):
				with self.assertRaises(LedgerRefused) as caught:
					open_ledger(self.layout, self.identity, **options)
				for fragment in fragments:
					self.assertIn(fragment, str(caught.exception))
		if before is not None:
			self.assertEqual(read_bytes(self.path), before)

	def make_sqlite(self, script, *, app_id=LEDGER_APPLICATION_ID, version=1):
		con = sqlite3.connect(self.path)
		con.executescript(script)
		con.execute(f'PRAGMA application_id = {app_id}')
		con.execute(f'PRAGMA user_version = {version}')
		con.commit()
		con.close()

	def valid_ledger(self):
		open_ledger(self.layout, self.identity, create=True).close()

	def test_other_sqlite_database(self):
		self.make_sqlite('CREATE TABLE t (x);', app_id=0, version=0)
		self.assertRefused('not a Telegram Tracker media ledger')

	def test_a_data_database(self):
		create_database(self.path)
		self.assertRefused('not a Telegram Tracker media ledger')

	def test_not_sqlite(self):
		write_text(self.path, 'not a database at all, but long enough to fill a header ' * 3)
		self.assertRefused('not a Telegram Tracker media ledger')

	def test_empty_file(self):
		write_text(self.path, '')
		self.assertRefused('empty file')

	def test_newer_and_older_versions(self):
		self.valid_ledger()
		for version, fragment in ((2, 'newer'), (0, 'not supported')):
			with self.subTest(version=version):
				con = sqlite3.connect(self.path)
				con.execute(f'PRAGMA user_version = {version}')
				con.close()
				self.assertRefused(fragment)

	def test_changed_objects(self):
		changes = {
			'extra table': 'CREATE TABLE extra (x)',
			'dropped trigger': 'DROP TRIGGER items_state_transition',
			'dropped index': 'DROP INDEX items_by_media'
		}
		for label, sql in changes.items():
			with self.subTest(label):
				self.valid_ledger()
				con = sqlite3.connect(self.path)
				con.execute(sql)
				con.commit()
				con.close()
				self.assertRefused('do not match')
				os.remove(self.path)

	def test_missing_meta_row(self):
		self.make_sqlite(ledger_schema.ledger_sql())
		self.assertRefused('ledger_meta row is missing')

	def test_a_folder_or_link_at_the_ledger_path(self):
		os.mkdir(self.path)
		self.assertRefused('not a regular file')
		os.rmdir(self.path)
		target = os.path.join(self.tmp, 'target')
		os.mkdir(target)
		self.link(target, self.path)
		self.assertRefused('not a regular file')


class IdentityGuardTests(MediaTestCase):
	'''
	A ledger is refused in any folder whose dataset is not its own.
	'''
	def db_folder(self, path, storage, dataset_uuid=None):
		os.makedirs(path, exist_ok=True)
		dataset_uuid = dataset_uuid or str(uuid.uuid4())
		write_manifest(path, storage, dataset_uuid, '2024-01-01T00:00:00+00:00')
		return dataset_uuid

	def assertForeign(self, output, fragment):
		with self.assertRaises(LedgerRefused) as caught:
			open_ledger(MediaLayout(output), folder_identity(output), create=True)
		self.assertIn(fragment, str(caught.exception))

	def test_db_folders_bind_to_the_manifest_dataset(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				output = os.path.join(self.tmp, storage)
				dataset_uuid = self.db_folder(output, storage)
				with open_ledger(MediaLayout(output), folder_identity(output), create=True) as ledger:
					self.assertEqual((ledger.meta.dataset_uuid, ledger.meta.storage), (dataset_uuid, storage))
				self.assertFalse(os.path.exists(os.path.join(output, IDENTITY_FILE)))

	def test_foreign_dataset_in_each_mode(self):
		for storage in ('files', 'sqlite', 'both'):
			with self.subTest(storage=storage):
				source = os.path.join(self.tmp, f'source-{storage}')
				other = os.path.join(self.tmp, f'other-{storage}')
				os.makedirs(source)
				os.makedirs(other)
				if storage == 'files':
					ensure_identity(source)
					ensure_identity(other)
				else:
					self.db_folder(source, storage)
					self.db_folder(other, storage)
				open_ledger(MediaLayout(source), folder_identity(source), create=True).close()
				shutil.copytree(os.path.join(source, 'downloaded-media'), os.path.join(other, 'downloaded-media'))
				before = read_bytes(MediaLayout(other).ledger_path)
				self.assertForeign(other, 'belongs to another dataset')
				self.assertEqual(read_bytes(MediaLayout(other).ledger_path), before)

	def test_a_files_ledger_without_its_identity_record(self):
		self.files_ledger().close()
		os.remove(os.path.join(self.out, IDENTITY_FILE))
		self.assertForeign(self.out, f'has no {IDENTITY_FILE}')
		self.assertFalse(os.path.exists(os.path.join(self.out, IDENTITY_FILE)))

	def test_storage_mode_must_match(self):
		output = os.path.join(self.tmp, 'db')
		dataset_uuid = self.db_folder(output, 'sqlite')
		open_ledger(MediaLayout(output), folder_identity(output), create=True).close()
		os.remove(os.path.join(output, MANIFEST_FILE))
		self.db_folder(output, 'both', dataset_uuid)
		self.assertForeign(output, 'created for a sqlite folder')

	def test_files_ledger_in_a_db_folder_with_the_same_uuid(self):
		self.files_ledger().close()
		dataset_uuid = read_identity_record(self.out)['dataset_uuid']
		output = os.path.join(self.tmp, 'db')
		self.db_folder(output, 'sqlite', dataset_uuid)
		shutil.copytree(self.layout.root, os.path.join(output, 'downloaded-media'))
		self.assertForeign(output, 'created for a files folder')

	def test_a_whole_moved_folder_keeps_its_ledger(self):
		ledger = self.files_ledger()
		meta = ledger.meta
		ledger.close()
		moved = os.path.join(self.tmp, 'moved')
		shutil.move(self.out, moved)
		with open_ledger(MediaLayout(moved), folder_identity(moved)) as reopened:
			self.assertEqual(reopened.meta, meta)
			self.assertEqual(reopened.meta.output_binding, canonical_folder(self.out))


class IdentityRecordTests(MediaTestCase):

	def test_written_once_and_never_replaced(self):
		first = ensure_identity(self.out)
		self.assertEqual((first.storage, first.source), ('files', IDENTITY_FILE))
		record = read_identity_record(self.out)
		self.assertEqual(set(record), {'format', 'dataset_uuid', 'created_at'})
		self.assertEqual(record['format'], 1)
		before = read_bytes(os.path.join(self.out, IDENTITY_FILE))
		self.assertEqual(ensure_identity(self.out), first)
		self.assertEqual(read_bytes(os.path.join(self.out, IDENTITY_FILE)), before)
		self.assertEqual(sorted(os.listdir(self.out)), [IDENTITY_FILE])

	def test_db_folders_get_no_record(self):
		write_manifest(self.out, 'sqlite', str(uuid.uuid4()), 't')
		identity = ensure_identity(self.out)
		self.assertEqual((identity.storage, identity.source), ('sqlite', MANIFEST_FILE))
		self.assertEqual(os.listdir(self.out), [MANIFEST_FILE])

	def test_the_manifest_decides_over_a_stray_record(self):
		ensure_identity(self.out)
		dataset_uuid = str(uuid.uuid4())
		write_manifest(self.out, 'both', dataset_uuid, 't')
		self.assertEqual(folder_identity(self.out), FolderIdentity('both', dataset_uuid, MANIFEST_FILE))

	def test_a_record_written_meanwhile_stands(self):
		other = {'format': 1, 'dataset_uuid': str(uuid.uuid4()), 'created_at': 't'}
		real_link = identity_module.link_new

		def racing_link(tmp, path):
			write_text(path, identity_module.pretty_json_text(other))
			return real_link(tmp, path)

		with mock.patch.object(identity_module, 'link_new', racing_link):
			identity = ensure_identity(self.out)
		self.assertEqual(identity.dataset_uuid, other['dataset_uuid'])
		self.assertEqual(os.listdir(self.out), [IDENTITY_FILE])

	def test_unusable_records(self):
		path = os.path.join(self.out, IDENTITY_FILE)
		cases = {
			'not json': '{',
			'not an object': '[]',
			'no format': '{"dataset_uuid": "x"}',
			'newer format': '{"format": 2, "dataset_uuid": "00000000-0000-4000-8000-000000000000"}',
			'bad uuid': '{"format": 1, "dataset_uuid": "not-a-uuid"}',
			'upper-case uuid': '{"format": 1, "dataset_uuid": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}'
		}
		for label, text in cases.items():
			with self.subTest(label):
				write_text(path, text)
				for call in (read_identity_record, folder_identity, ensure_identity):
					self.assertRaises(IdentityError, call, self.out)
				self.assertEqual(read_bytes(path).decode(), text)

	def test_a_ledger_that_lost_its_record_gets_no_new_one(self):
		self.files_ledger().close()
		record_path = os.path.join(self.out, IDENTITY_FILE)
		record = read_bytes(record_path)
		os.remove(record_path)
		ledger_bytes = read_bytes(self.layout.ledger_path)
		with self.assertRaises(LedgerRefused) as caught:
			ensure_identity(self.out)
		self.assertIn(f'{IDENTITY_FILE} is missing', str(caught.exception))
		self.assertEqual(sorted(os.listdir(self.out)), ['downloaded-media'])
		self.assertEqual(read_bytes(self.layout.ledger_path), ledger_bytes)
		# the lost record, restored, binds the ledger again
		with open(record_path, 'wb') as writer:
			writer.write(record)
		self.assertEqual(ensure_identity(self.out).dataset_uuid, self.reopen().meta.dataset_uuid)

	def test_anything_at_the_ledger_path_blocks_a_new_record(self):
		os.makedirs(self.layout.ledger_path)
		self.assertRaises(LedgerRefused, ensure_identity, self.out)
		self.assertEqual(os.listdir(self.out), ['downloaded-media'])

	def test_the_record_reaches_the_disk_before_it_is_published(self):
		events = []
		real_fsync = os.fsync
		real_link = identity_module.link_new

		def fsync(fd):
			events.append(('fsync', os.fstat(fd).st_size))
			real_fsync(fd)

		def link(tmp, path):
			events.append(('link', os.path.getsize(tmp)))
			return real_link(tmp, path)

		with mock.patch.object(identity_module.os, 'fsync', fsync), \
				mock.patch.object(identity_module, 'link_new', link):
			ensure_identity(self.out)
		size = os.path.getsize(os.path.join(self.out, IDENTITY_FILE))
		self.assertGreater(size, 0)
		self.assertEqual(events, [('fsync', size), ('link', size)])

	def test_a_link_at_the_record_is_refused(self):
		target = os.path.join(self.tmp, 'target')
		os.mkdir(target)
		self.link(target, os.path.join(self.out, IDENTITY_FILE))
		self.assertRaises(IdentityError, ensure_identity, self.out)


class ConnectionTests(MediaTestCase):
	'''
	One connection per ledger, and no transaction outlives a method call.
	'''
	def test_no_coroutine_methods(self):
		for name, member in inspect.getmembers(Ledger, inspect.isfunction):
			with self.subTest(name):
				self.assertFalse(inspect.iscoroutinefunction(member))
				self.assertFalse(inspect.isasyncgenfunction(member))

	def lifecycle(self, ledger):
		'''
		(label, call) pairs covering every write method, failures included.
		'''
		key = (CHANNEL, 3, 5002)
		ref = ref_of(document_media())
		return [
			('start', lambda: ledger.start_run(RUN_A, 'collect', selection=SELECTION)),
			('request', lambda: ledger.request(RUN_A, CHANNEL, [(3, ref)])),
			('bad transition', lambda: ledger.skip(RUN_A, key, 'over_file_limit', attempt=1)),
			('begin', lambda: ledger.begin_download(RUN_A, key)),
			('begin twice', lambda: ledger.begin_download(RUN_A, key)),
			('counters', lambda: ledger.add_counters(RUN_A, app_requests=2, bytes_received=10)),
			('complete', lambda: ledger.complete(RUN_A, key, sha256=SHA_A, actual_bytes=10, attempt=1)),
			('fail complete', lambda: ledger.fail(RUN_A, key, 'request_failed')),
			('finish', lambda: ledger.finish_run(RUN_A, 'complete')),
			('finish twice', lambda: ledger.finish_run(RUN_A, 'complete'))
		]

	def test_every_call_leaves_no_open_transaction_and_opens_no_connection(self):
		ledger = self.files_ledger()
		connects = []
		real_connect = sqlite3.connect

		def counting_connect(*args, **kwargs):
			connects.append(args)
			return real_connect(*args, **kwargs)

		with mock.patch.object(sqlite3, 'connect', counting_connect):
			for label, call in self.lifecycle(ledger):
				with self.subTest(label):
					try:
						call()
					except (StorageError, ValueError):
						self.assertIn(label, ('bad transition', 'begin twice', 'fail complete', 'finish twice'))
					self.assertFalse(ledger.con.in_transaction)
		self.assertEqual(connects, [])
		self.assertEqual(ledger.item((CHANNEL, 3, 5002))['state'], 'complete')
		self.assertEqual(ledger.run(RUN_A)['state'], 'complete')

	def test_tasks_interleaving_at_awaits_share_the_connection(self):
		ledger = self.files_ledger()
		ledger.start_run(RUN_A, 'collect', selection=SELECTION)
		refs = {n: ref_of(document_media(media_id=n)) for n in range(1, 9)}
		ledger.request(RUN_A, CHANNEL, [(n, ref) for n, ref in refs.items()])
		open_seen = []

		async def worker(n):
			key = (CHANNEL, n, n)
			attempt = ledger.begin_download(RUN_A, key)
			open_seen.append(ledger.con.in_transaction)
			await asyncio.sleep(0)
			open_seen.append(ledger.con.in_transaction)
			if n % 2:
				ledger.complete(RUN_A, key, sha256=f'{n:064x}', actual_bytes=n, attempt=attempt)
			else:
				ledger.fail(RUN_A, key, 'request_failed', attempt=attempt)
			await asyncio.sleep(0)
			open_seen.append(ledger.con.in_transaction)

		async def main():
			await asyncio.gather(*(worker(n) for n in refs))

		asyncio.run(main())
		self.assertEqual(open_seen, [False] * 24)
		states = [ledger.item((CHANNEL, n, n))['state'] for n in refs]
		self.assertEqual(states, ['complete', 'failed'] * 4)

	def test_a_refused_write_changes_nothing(self):
		ledger = self.files_ledger()
		ledger.start_run(RUN_A, 'collect', selection=SELECTION)
		ledger.request(RUN_A, CHANNEL, [(3, ref_of(document_media()))])
		ledger.begin_download(RUN_A, (CHANNEL, 3, 5002))
		before = (ledger.items(), ledger.attempts((CHANNEL, 3, 5002)))
		# downloading -> complete is allowed, but attempt 2 is not the open
		# one: refused after the blob row was written in the transaction
		with self.assertRaises(LedgerError) as caught:
			ledger.complete(RUN_A, (CHANNEL, 3, 5002), sha256=SHA_A, actual_bytes=1, attempt=2)
		self.assertNotIsInstance(caught.exception, InvalidTransition)
		self.assertEqual((ledger.items(), ledger.attempts((CHANNEL, 3, 5002))), before)
		self.assertIsNone(ledger.blob(CHANNEL, SHA_A))
		self.assertFalse(ledger.con.in_transaction)


if __name__ == '__main__':
	unittest.main()
