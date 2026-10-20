# -*- coding: utf-8 -*-
'''
Owned-database identity: creating schema version 1, classifying a file
from its header before connecting, and refusing, untouched, every file
that is not a current data database (missing, foreign, Telethon-session
shaped, empty, damaged, older, newer) and every protected path. Session
files are refused by name before any open; the offline guard stays in
place and records any attempt.
'''

# import modules
import os
import sqlite3
import subprocess
import sys
import uuid

from unittest import mock

import offline_guard
import sqlite_support

from telegram_tracker.storage.sqlite import connection, schema
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import DatabaseBusy, create_database, open_database
from telegram_tracker.storage.sqlite.schema import DatabaseRefused, classify
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


DOC_DDL = os.path.join(sqlite_support.source_path.ROOT, 'docs', 'sqlite-schema.sql')

# the tables Telethon's SQLite session creates (synthetic copy; no real session)
TELETHON_SHAPED = '''
CREATE TABLE version (version integer primary key);
CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer);
CREATE TABLE entities (id integer primary key, hash integer not null, username text, phone integer, name text, date integer);
CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer, id integer, hash integer, primary key(md5_digest, file_size, type));
CREATE TABLE update_state (id integer primary key, pts integer, qts integer, date integer, seq integer);
INSERT INTO version VALUES (7);
'''


class Case(sqlite_support.SQLiteCase):

	def setUp(self):
		super().setUp()
		os.makedirs(self.out)

	def make_file(self, name, content):
		path = os.path.join(self.out, name)
		with open(path, 'wb') as fh:
			fh.write(content)
		return path

	def make_sqlite(self, name, script):
		path = os.path.join(self.out, name)
		con = sqlite3.connect(path)
		con.executescript(script)
		con.close()
		return path

	def created(self):
		create_database(self.db)
		return self.db

	def assertRefusedUntouched(self, path, fragment, **kwargs):
		before = (sqlite_support.read_bytes(path), os.stat(path).st_mtime_ns)
		with mock.patch.object(connection.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
			with self.assertRaises(DatabaseRefused) as caught:
				open_database(path, create=True, **kwargs)
			with self.assertRaises(DatabaseRefused):
				SQLiteWriter.open(path, targets=['alpha_fx'])
		self.assertIn(fragment, str(caught.exception))
		self.assertEqual((sqlite_support.read_bytes(path), os.stat(path).st_mtime_ns), before)
		self.assertEqual([c for c in connect.call_args_list if c.args[:1] == (path,)], [])
		self.assertEqual(sorted(os.listdir(self.out)), [os.path.basename(path)])


class SchemaCopyTests(offline_guard.OfflineTestCase):

	def test_packaged_ddl_is_the_frozen_document(self):
		with open(DOC_DDL, encoding='utf-8') as fh:
			documented = fh.read()
		self.assertEqual(schema.schema_sql().replace('\r\n', '\n'), documented.replace('\r\n', '\n'))

	def test_identity_constants_match_the_ddl(self):
		text = schema.schema_sql()
		self.assertIn(f'PRAGMA application_id = {schema.APPLICATION_ID};', text)
		self.assertIn(f'PRAGMA user_version = {schema.SCHEMA_VERSION};', text)


class CreationTests(Case):

	def test_new_database_has_schema_version_one(self):
		con = open_database(self.db, create=True)
		self.addCleanup(con.close)
		self.assertEqual(con.execute('PRAGMA application_id').fetchone(), (schema.APPLICATION_ID,))
		self.assertEqual(con.execute('PRAGMA user_version').fetchone(), (1,))
		self.assertEqual(schema.schema_objects(con), schema.expected_objects())
		version, dataset, created, updated, binding = con.execute(
			'SELECT schema_version, dataset_uuid, created_at, updated_at, output_binding FROM schema_meta'
		).fetchone()
		self.assertEqual(version, 1)
		self.assertEqual(str(uuid.UUID(dataset)), dataset)
		self.assertEqual(created, updated)
		self.assertIsNone(binding)
		self.assertEqual(con.execute('PRAGMA journal_mode').fetchone(), ('delete',))
		self.assertEqual(con.execute('PRAGMA foreign_keys').fetchone(), (1,))
		self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone(), (connection.BUSY_TIMEOUT_MS,))
		self.assertEqual(sorted(os.listdir(self.out)), [schema.DB_FILENAME])
		self.assertIntact()

	def test_missing_database_is_not_created_without_create(self):
		self.assertEqual(classify(self.db).kind, schema.MISSING)
		with self.assertRaises(DatabaseRefused):
			open_database(self.db)
		self.assertFalse(os.path.exists(self.db))
		self.assertEqual(os.listdir(self.out), [])

	def test_failed_creation_leaves_nothing_at_the_path(self):
		with mock.patch.object(connection, 'verify', side_effect=DatabaseRefused('check failed')):
			with self.assertRaises(DatabaseRefused):
				create_database(self.db)
		self.assertEqual(os.listdir(self.out), [])

	def test_a_file_that_appears_during_creation_is_kept(self):
		real_link = os.link

		def racing_link(src, dst):
			with open(dst, 'wb') as fh:
				fh.write(b'other process')
			return real_link(src, dst)

		with mock.patch.object(connection.os, 'link', racing_link):
			with self.assertRaises(DatabaseRefused) as caught:
				create_database(self.db)
		self.assertIn('appeared', str(caught.exception))
		self.assertEqual(sqlite_support.read_bytes(self.db), b'other process')
		self.assertEqual(os.listdir(self.out), [schema.DB_FILENAME])

	def test_creation_without_hard_links(self):
		with mock.patch.object(connection.os, 'link', side_effect=OSError(1, 'not supported')):
			if os.name != 'nt':
				with self.assertRaises(StorageError):
					create_database(self.db)
				self.assertEqual(os.listdir(self.out), [])
				return
			create_database(self.db)
		self.assertEqual(classify(self.db).kind, schema.CURRENT)
		self.assertEqual(os.listdir(self.out), [schema.DB_FILENAME])
		self.open()

	def test_unsafe_rename_fallback_cannot_replace_a_racing_file(self):
		real_exists, replacing_rename = os.path.lexists, os.replace

		def racing_exists(path):
			if path == self.db:
				with open(path, 'wb') as writer:
					writer.write(b'other process')
				return False
			return real_exists(path)

		with mock.patch.object(connection.os, 'link', side_effect=OSError(1, 'not supported')), \
				mock.patch.object(connection.os.path, 'lexists', racing_exists), \
				mock.patch.object(connection.os, 'rename', replacing_rename), \
				mock.patch.object(connection, '_RENAME_REFUSES_EXISTING', False, create=True):
			with self.assertRaises(StorageError):
				create_database(self.db)
		self.assertEqual(sqlite_support.read_bytes(self.db), b'other process')
		self.assertEqual(os.listdir(self.out), [schema.DB_FILENAME])

	def test_current_database_opens_and_is_not_rewritten(self):
		self.created()
		before = sqlite_support.read_bytes(self.db)
		con = open_database(self.db, create=True)
		con.close()
		self.assertEqual(sqlite_support.read_bytes(self.db), before)

	def test_sqlite_floor_and_json1_are_checked(self):
		self.created()
		with mock.patch.object(connection.sqlite3, 'sqlite_version_info', (3, 30, 1)):
			with self.assertRaises(DatabaseRefused) as caught:
				open_database(self.db)
		self.assertIn('3.31', str(caught.exception))

		class NoJson:
			def execute(self, sql, *args):
				raise sqlite3.OperationalError('no such function: json_valid')

		with self.assertRaises(DatabaseRefused) as caught:
			connection.check_library(NoJson())
		self.assertIn('JSON1', str(caught.exception))


class RefusalTests(Case):

	def test_telethon_session_schema_under_another_name(self):
		path = self.make_sqlite('copied.sqlite3', TELETHON_SHAPED)
		self.assertEqual(classify(path).kind, schema.FOREIGN)
		self.assertRefusedUntouched(path, 'not a Telegram Tracker database')

	def test_other_sqlite_database(self):
		path = self.make_sqlite(schema.DB_FILENAME, 'CREATE TABLE notes (body TEXT); INSERT INTO notes VALUES (1);')
		self.assertRefusedUntouched(path, 'not a Telegram Tracker database')

	def test_empty_file(self):
		path = self.make_file(schema.DB_FILENAME, b'')
		self.assertRefusedUntouched(path, 'empty file')

	def test_not_an_sqlite_file(self):
		for content in (b'{"messages": []}', b'SQLite format 3\x00' + b'\x00' * 20):
			with self.subTest(content=content[:8]):
				path = self.make_file(schema.DB_FILENAME, content)
				self.assertEqual(classify(path).kind, schema.CORRUPT)
				self.assertRefusedUntouched(path, 'damaged')
				os.remove(path)

	def test_newer_and_older_versions(self):
		for version, kind in ((2, schema.NEWER), (0, schema.OLDER)):
			with self.subTest(version=version):
				self.created()
				con = sqlite3.connect(self.db)
				con.execute(f'PRAGMA user_version = {version}')
				con.close()
				self.assertEqual(classify(self.db), schema.Classification(kind, version))
				self.assertRefusedUntouched(self.db, f'schema version {version}')
				os.remove(self.db)

	def test_damaged_schema_is_refused(self):
		damage = {
			'dropped view': 'DROP VIEW chat_ids_all',
			'extra table': 'CREATE TABLE extra (x)',
			'changed index': 'DROP INDEX messages_by_run; CREATE INDEX messages_by_run ON messages (channel_id)',
			'no meta row': 'DELETE FROM schema_meta',
		}
		for name, script in damage.items():
			with self.subTest(damage=name):
				self.created()
				con = sqlite3.connect(self.db)
				con.executescript(script)
				con.close()
				before = sqlite_support.read_bytes(self.db)
				with self.assertRaises(DatabaseRefused):
					open_database(self.db)
				self.assertEqual(sqlite_support.read_bytes(self.db), before)
				os.remove(self.db)

	def test_damaged_schema_page_is_refused(self):
		self.created()
		data = bytearray(sqlite_support.read_bytes(self.db))
		data[100:4096] = b'\xff' * (4096 - 100)
		with open(self.db, 'wb') as fh:
			fh.write(data)
		with self.assertRaises(DatabaseRefused) as caught:
			open_database(self.db)
		self.assertIn('damaged', str(caught.exception))
		self.assertEqual(sqlite_support.read_bytes(self.db), bytes(data))


class ProtectedPathTests(Case):

	def test_session_and_config_names_are_refused_before_any_open(self):
		for name in ('session_file.session', 'ACCOUNT.SESSION', 'a.session-journal', 'a.session-wal', 'config.ini'):
			with self.subTest(name=name):
				path = os.path.join(self.out, name)
				for call in (classify, open_database, SQLiteWriter.open, create_database):
					with self.assertRaises(DatabaseRefused):
						call(path)
				self.assertFalse(os.path.exists(path))

	def test_other_non_database_paths(self):
		for path in ('', ':memory:', 'file:data.sqlite3?mode=ro', os.path.join(self.out, 'dump.sql'), self.out):
			with self.subTest(path=path):
				with self.assertRaises(DatabaseRefused):
					open_database(path, create=True)
		self.assertEqual(os.listdir(self.out), [])

	def test_protected_file_and_its_aliases(self):
		protected = self.make_file('account.bin', b'not a data database')
		alias = os.path.join(self.out, 'alias.sqlite3')
		os.link(protected, alias)
		for path in (protected, alias, protected.upper() if os.name == 'nt' else protected):
			with self.subTest(path=path):
				with self.assertRaises(DatabaseRefused) as caught:
					open_database(path, create=True, protected=[protected])
				self.assertIn('protected', str(caught.exception))
		self.assertEqual(sqlite_support.read_bytes(protected), b'not a data database')

	def test_guard_still_refuses_session_opens(self):
		path = os.path.join(self.out, 'x.session')
		with offline_guard.expected_violations() as caught:
			with self.assertRaises(offline_guard.OfflineViolation):
				sqlite3.connect(path)
		self.assertEqual(len(caught), 1)
		self.assertFalse(os.path.exists(path))


class BusyOpenTests(Case):

	def test_busy_database_is_reported(self):
		self.created()
		blocker = sqlite3.connect(self.db, isolation_level=None)
		self.addCleanup(blocker.close)
		blocker.execute('BEGIN EXCLUSIVE')
		with self.assertRaises(DatabaseBusy) as caught:
			SQLiteWriter.open(self.db, targets=['alpha_fx'], busy_timeout_ms=50)
		self.assertIn('in use by another process', str(caught.exception))
		blocker.execute('ROLLBACK')
		self.assertEqual(self.rows('SELECT count(*) FROM collection_runs'), [(0,)])


class ImportTests(offline_guard.OfflineTestCase):

	def test_builders_do_not_load_the_database_modules(self):
		code = (
			'import sys; import telegram_tracker.cli.builders, telegram_tracker.storage; '
			"print(sorted(m for m in sys.modules if m.startswith('telegram_tracker.storage.sqlite')))"
		)
		self.assertEqual(self.run_python(code), '[]')

	def test_database_modules_use_the_standard_library_only(self):
		code = (
			'import sys; import telegram_tracker.storage.sqlite.writer, telegram_tracker.storage.sqlite.migrations; '
			"print(sorted(m for m in ('telethon', 'pandas', 'networkx') if m in sys.modules))"
		)
		self.assertEqual(self.run_python(code), '[]')

	def run_python(self, code):
		env = dict(os.environ, PYTHONPATH=sqlite_support.source_path.SRC)
		done = subprocess.run(
			[sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=60
		)
		self.assertEqual(done.returncode, 0, done.stderr)
		return done.stdout.strip()
