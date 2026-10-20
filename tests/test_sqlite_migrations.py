# -*- coding: utf-8 -*-
'''
Version detection and the explicit offline upgrade boundary: a checked
backup before every step, one transaction per step (rolled back on any
failure, including broken foreign keys), backups kept and restorable,
and no upgrade from a version without a step (there is no version 0
migration), from a newer version, or of a file that is not a data
database. Upgrade steps here are synthetic; schema version 1 has none.
'''

# import modules
import os
import re
import sqlite3

from unittest import mock

import sqlite_support

from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite import migrations, schema
from telegram_tracker.storage.sqlite.connection import open_database
from telegram_tracker.storage.sqlite.migrations import MigrationFailed, migrate
from telegram_tracker.storage.sqlite.schema import DatabaseRefused


def add_notes(con):
	con.execute('CREATE TABLE notes (run_id INTEGER REFERENCES collection_runs (run_id), body TEXT)')
	con.execute("INSERT INTO notes VALUES (1, 'synthetic')")


def fail_halfway(con):
	con.execute('CREATE TABLE half (x)')
	raise RuntimeError('step failed')


def break_foreign_keys(con):
	con.execute('PRAGMA defer_foreign_keys = ON')
	con.execute(
		"INSERT INTO run_targets (run_id, input_index, input, status) VALUES (99, 0, 'x', 'pending')"
	)


class MigrationCase(sqlite_support.SQLiteCase):

	async def asyncSetUp(self):
		await super().asyncSetUp()
		await self.collect(['alpha_fx'])
		self.before = self.dump()

	def dump(self, path=None):
		con = self.connect(path)
		tables = [r[0] for r in con.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
		data = {t: con.execute(f'SELECT * FROM {t} ORDER BY 1, 2').fetchall() for t in tables}
		con.close()
		return data

	def backups(self):
		return sorted(n for n in os.listdir(self.out) if n.endswith('.bak.sqlite3'))

	def state(self):
		return (sqlite_support.read_bytes(self.db), os.stat(self.db).st_mtime_ns)


class DetectionTests(MigrationCase):

	def test_current_database_needs_nothing(self):
		state = self.state()
		self.assertEqual(migrations.UPGRADES, {})
		self.assertEqual(migrate(self.db), [])
		self.assertEqual(self.state(), state)
		self.assertEqual(self.backups(), [])

	def test_no_upgrade_from_version_zero(self):
		con = sqlite3.connect(self.db)
		con.execute('PRAGMA user_version = 0')
		con.close()
		state = self.state()
		for upgrades in ({}, {1: add_notes}):
			with self.assertRaises(DatabaseRefused) as caught:
				migrate(self.db, upgrades=upgrades, target_version=2)
			self.assertIn('no upgrade leads from version 0', str(caught.exception))
		self.assertEqual(self.state(), state)
		self.assertEqual(self.backups(), [])

	def test_newer_foreign_corrupt_and_missing_are_refused_untouched(self):
		con = sqlite3.connect(self.db)
		con.execute('PRAGMA user_version = 3')
		con.close()
		other = os.path.join(self.out, 'other.sqlite3')
		con = sqlite3.connect(other)
		con.execute('CREATE TABLE sessions (dc_id INTEGER)')
		con.close()
		damaged = os.path.join(self.out, 'damaged.sqlite3')
		with open(damaged, 'wb') as fh:
			fh.write(b'not sqlite')
		for path in (self.db, other, damaged):
			with self.subTest(path=os.path.basename(path)):
				state = (sqlite_support.read_bytes(path), os.stat(path).st_mtime_ns)
				with self.assertRaises(DatabaseRefused):
					migrate(path, upgrades={1: add_notes}, target_version=2)
				self.assertEqual((sqlite_support.read_bytes(path), os.stat(path).st_mtime_ns), state)
		with self.assertRaises(DatabaseRefused):
			migrate(os.path.join(self.out, 'absent.sqlite3'))
		self.assertEqual(self.backups(), [])

	async def test_collection_does_not_upgrade(self):
		con = sqlite3.connect(self.db)
		con.execute('PRAGMA user_version = 0')
		con.close()
		state = self.state()
		with self.assertRaises(DatabaseRefused) as caught:
			await self.collect(['alpha_fx'])
		self.assertIn('does not upgrade', str(caught.exception))
		self.assertEqual(self.state(), state)
		self.assertEqual(self.last_client.requests, [])


class UpgradeTests(MigrationCase):

	def test_step_runs_after_a_checked_backup(self):
		backups = migrate(self.db, upgrades={1: add_notes}, target_version=2)

		self.assertEqual(len(backups), 1)
		name = os.path.basename(backups[0])
		self.assertRegex(name, r'^telegram-tracker\.v1-\d{8}T\d{6}\.\d{6}Z\.bak\.sqlite3$')
		self.assertEqual(self.backups(), [name])
		self.assertEqual(self.dump(backups[0]), self.before)
		self.assertEqual(schema.classify(backups[0]), schema.Classification(schema.CURRENT, 1))
		self.assertIntact(backups[0])

		con = self.connect()
		self.assertEqual(con.execute('PRAGMA user_version').fetchone(), (2,))
		self.assertEqual(con.execute('SELECT schema_version FROM schema_meta').fetchone(), (2,))
		self.assertEqual(
			con.execute('SELECT version, backup_path FROM migrations').fetchall(), [(2, backups[0])])
		self.assertEqual(con.execute('SELECT body FROM notes').fetchall(), [('synthetic',)])
		self.assertIntact()
		with self.assertRaises(DatabaseRefused):
			open_database(self.db)

	def test_backup_restores_the_previous_version(self):
		backup = migrate(self.db, upgrades={1: add_notes}, target_version=2)[0]
		restored = os.path.join(self.tmp, 'restored.sqlite3')
		source, target = sqlite3.connect(backup), sqlite3.connect(restored)
		source.backup(target)
		source.close()
		target.close()
		con = open_database(restored)
		con.close()
		self.assertEqual(self.dump(restored), self.before)

	def test_steps_chain_with_a_backup_each(self):
		def add_more(con):
			con.execute('CREATE TABLE more (x)')

		backups = migrate(self.db, upgrades={1: add_notes, 2: add_more}, target_version=3)
		self.assertEqual([re.search(r'\.v(\d)-', b).group(1) for b in backups], ['1', '2'])
		self.assertEqual(self.rows('SELECT version FROM migrations ORDER BY version'), [(2,), (3,)])
		self.assertEqual(self.rows('PRAGMA user_version'), [(3,)])

	def test_failed_step_rolls_back_and_keeps_the_backup(self):
		for step, fragment in ((fail_halfway, 'step failed'), (break_foreign_keys, 'foreign keys')):
			with self.subTest(step=step.__name__):
				with self.assertRaises(MigrationFailed) as caught:
					migrate(self.db, upgrades={1: step}, target_version=2)
				self.assertIn(fragment, str(caught.exception))
				self.assertEqual(self.dump(), self.before)
				self.assertEqual(self.rows('PRAGMA user_version'), [(1,)])
				self.open()
				self.assertIntact()
		self.assertEqual(len(self.backups()), 2)
		for name in self.backups():
			self.assertEqual(self.dump(os.path.join(self.out, name)), self.before)

	def test_busy_database_keeps_its_version(self):
		blocker = sqlite3.connect(self.db, isolation_level=None)
		self.addCleanup(blocker.close)
		blocker.execute('BEGIN IMMEDIATE')
		with self.assertRaises(MigrationFailed) as caught:
			migrate(self.db, upgrades={1: add_notes}, target_version=2, busy_timeout_ms=50)
		self.assertIn('in use by another process', str(caught.exception))
		self.assertIn('keeps version 1', str(caught.exception))
		blocker.execute('ROLLBACK')
		self.assertEqual(self.dump(), self.before)

	def test_existing_backup_name_is_not_overwritten(self):
		taken = migrations.backup_path(self.db, 1)
		with open(taken, 'wb') as fh:
			fh.write(b'earlier backup')
		with mock.patch.object(migrations, 'backup_path', return_value=taken):
			with self.assertRaises(StorageError) as caught:
				migrate(self.db, upgrades={1: add_notes}, target_version=2)
		self.assertIn('exists', str(caught.exception))
		self.assertEqual(sqlite_support.read_bytes(taken), b'earlier backup')
		self.assertEqual(self.dump(), self.before)
