# -*- coding: utf-8 -*-
'''
The destination of a database run: the output lock, then the database
lease (checked before connecting, each with its own message, and taken
after login), the read-only check before credentials and again under the
lease ('pending' markers, the folder's own database), runs left running by
an ended process, migrate under the lease, aliases of the output folder,
and session contention. Lease holders in other processes use synthetic
temporary destinations only.
'''

# import modules
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest

from unittest import mock

import storage_modes_support

from storage_modes_support import BATCH, MANIFEST_FILE, read_json

from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials.lease import Lease
from telegram_tracker.storage import destination as destinations
from telegram_tracker.storage import files as storage_files
from telegram_tracker.storage.records import LOCK_FILE, STATUS_FILE
from telegram_tracker.storage.sqlite import connection, migrations
from telegram_tracker.storage.sqlite.connection import open_database
from telegram_tracker.storage.sqlite.lease import DatabaseLease, DatabaseLocked, database_lease_path


SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')

# Holders report their own pid: the process Popen starts may be a launcher
# (a Windows virtual environment's python.exe) rather than the interpreter
# that takes the lease.
HOLD_LEASE = (
	'import os, sys; sys.path.insert(0, sys.argv[1]); '
	'from telegram_tracker.storage.sqlite.lease import DatabaseLease; '
	'lease = DatabaseLease(sys.argv[2]); lease.__enter__(); print("held", os.getpid(), flush=True); '
	'sys.stdin.readline(); lease.__exit__(None, None, None)'
)

HOLD_WRITE = (
	'import os, sqlite3, sys; con = sqlite3.connect(sys.argv[1], isolation_level=None); '
	'con.execute("BEGIN IMMEDIATE"); print("held", os.getpid(), flush=True); sys.stdin.readline(); '
	'con.execute("ROLLBACK"); con.close()'
)


def failing_replace(suffix, real=os.replace):
	'''
	os.replace failing (disk full) for destinations ending in `suffix`.
	'''
	def replace(src, dst):
		if dst.endswith(suffix):
			raise OSError(28, 'No space left on device')
		return real(src, dst)

	return replace


class DestinationCase(storage_modes_support.ModesCase):

	def holder(self, code, *args):
		'''
		A process running `code` until its stdin gets a line; started and
		holding when this returns. `owner_pid` is the pid the holding
		interpreter reported for itself.
		'''
		process = subprocess.Popen(
			[sys.executable, '-I', '-c', code, *args],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
		)
		self.addCleanup(lambda: process.poll() is not None or process.communicate('\n', timeout=60))
		word, pid = process.stdout.readline().split()
		self.assertEqual(word, 'held')
		process.owner_pid = int(pid)
		return process

	def leave_pending(self, out=None, suffix='beta_fx_messages.json'):
		'''
		A both run whose publication of `suffix` fails: exit 1, 'pending'.
		'''
		with mock.patch.object(storage_files.os, 'replace', failing_replace(suffix)):
			code, _, err = self.collect(targets=BATCH, out=out, storage='both')
		self.assertEqual(code, 1, err)
		self.assertEqual(self.rows('SELECT kind FROM destination_pending', out=out), [('target',)])

	def channel_requests(self):
		'''
		The last client's calls other than the session and account steps.
		'''
		return [
			name for name in self.last_client.call_names()
			if name not in ('connect', 'disconnect', 'is_user_authorized', 'get_me')
		]


class PendingTests(DestinationCase):

	def test_pending_blocks_every_later_run_before_the_client(self):
		self.leave_pending()
		calls = self.factory_calls
		self.assertFalse(os.path.exists(os.path.join(self.out, LOCK_FILE)))
		for extra in ([], ['--storage', 'both'], ['--update']):
			with self.subTest(extra=extra):
				code, _, err = self.collect(*extra, targets=BATCH)
				self.assertEqual(code, 1)
				self.assertIn('is blocked: an earlier run stopped after writing the database', err)
				self.assertIn('(1 channel(s))', err)
				self.assertIn(f'Deleting {LOCK_FILE}', err)
		self.assertEqual(self.factory_calls, calls)
		code, _, err = self.collect(storage='files')
		self.assertEqual(code, 2)
		self.assertEqual(self.status()['state'], 'interrupted')

	def test_deleting_the_lock_files_does_not_clear_pending(self):
		self.leave_pending()
		for name in (LOCK_FILE, os.path.basename(database_lease_path(self.db()))):
			with open(os.path.join(self.out, name), 'w', encoding='utf-8') as fh:
				fh.write('{}')
			os.remove(os.path.join(self.out, name))
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('is blocked', err)

	def test_pending_is_checked_again_under_the_lease(self):
		self.assertEqual(self.collect(storage='both')[0], 0)
		runs = self.rows('SELECT count(*) FROM collection_runs')
		original = destinations.Destination.check

		def check_then_race(destination):
			original(destination)
			con = sqlite3.connect(self.db())
			con.execute("UPDATE run_targets SET files_state = 'pending'")
			con.commit()
			con.close()

		with mock.patch.object(destinations.Destination, 'check', check_then_race):
			code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('is blocked', err)
		self.assertEqual(self.factory_calls, 2)  # past login: the lease is taken after it
		self.assertEqual(self.channel_requests(), [])
		self.assertEqual(self.rows('SELECT count(*) FROM collection_runs'), runs)
		self.assertNoLocks()


class FilesRaceTests(DestinationCase):

	def contents(self, out):
		data = {}
		for name in storage_modes_support.listing(out):
			with open(os.path.join(out, name), 'rb') as fh:
				data[name] = fh.read()
		return data

	def test_a_files_run_refuses_a_folder_set_up_meanwhile(self):
		original = destinations.Destination.check
		for mode, legacy in (('both', False), ('sqlite', False), ('both', True)):
			out = os.path.join(self.tmp, f'{mode}-{"legacy" if legacy else "collect"}')
			raced = {}

			def check_then_race(destination):
				original(destination)
				if not raced:
					# another run sets the empty folder up after this run's check
					raced['started'] = True
					raced['code'] = self.collect(out=out, storage=mode)[0]
					raced['contents'] = self.contents(out)

			with self.subTest(mode=mode, legacy=legacy):
				client = self.make_client()
				argv = ['--telegram-channel', 'beta_fx', '-o', out] if legacy else \
					['collect', '--telegram-channel', 'beta_fx', '-o', out]
				with mock.patch.object(destinations.Destination, 'check', check_then_race):
					code, _, err = self.run_cli(argv, client=client)
				self.assertEqual(raced['code'], 0)
				self.assertEqual(code, 1)
				self.assertIn(f'was set up for --storage {mode} by another run meanwhile; nothing was written', err)
				self.assertIn('get_me', client.call_names())  # refused under the lock, after login
				self.assertNotIn('get_entity', client.call_names())
				self.assertEqual(self.contents(out), raced['contents'])
				self.assertEqual(self.rows('SELECT count(*) FROM collection_runs', out=out), [(1,)])
				self.assertNoLocks(out)


class LeaseTests(DestinationCase):

	def test_a_held_database_lease_is_refused_before_connecting(self):
		self.collect(storage='sqlite')
		with DatabaseLease(self.db()):
			code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn(f'The database {self.db()} is in use by another collection or upgrade', err)
		self.assertNotIn('is in use by another collection. If none', err)
		self.assertEqual(self.factory_calls, 1)
		self.assertEqual(self.collect()[0], 0)

	def test_the_output_lock_is_checked_first(self):
		self.collect(storage='sqlite')
		open(os.path.join(self.out, LOCK_FILE), 'w').close()
		with DatabaseLease(self.db()):
			code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn(f'{self.out} is in use by another collection', err)
		self.assertNotIn('The database', err)

	def test_a_lease_held_by_another_process(self):
		self.collect(storage='both')
		process = self.holder(HOLD_LEASE, SRC, self.db())
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn(f'pid {process.owner_pid},', err)
		self.assertNotEqual(process.owner_pid, os.getpid())
		self.assertEqual(self.factory_calls, 1)
		process.communicate('\n', timeout=60)
		self.assertEqual(self.collect()[0], 0)
		self.assertNoLocks()

	def test_a_lease_taken_between_check_and_login_stops_the_run(self):
		self.collect(storage='sqlite')
		holding = []

		def check_unlocked_then_race(destination):
			lease = DatabaseLease(destination.database_path)
			lease.__enter__()
			holding.append(lease)

		with mock.patch.object(destinations.Destination, 'check_unlocked', check_unlocked_then_race):
			code, _, err = self.collect()
		holding[0].__exit__(None, None, None)
		self.assertEqual(code, 1)
		self.assertIn('is in use by another collection or upgrade', err)
		self.assertEqual(self.channel_requests(), [])
		self.assertEqual(self.rows('SELECT count(*) FROM collection_runs'), [(1,)])
		self.assertFalse(os.path.exists(os.path.join(self.out, LOCK_FILE)))

	def test_a_busy_database_fails_the_run_cleanly(self):
		self.collect(storage='sqlite')
		process = self.holder(HOLD_WRITE, self.db())
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('the database is in use by another process', err)
		self.assertEqual(self.channel_requests(), [])
		process.communicate('\n', timeout=60)
		self.assertNoLocks()
		self.assertEqual(self.collect()[0], 0)
		self.assertIn('get_entity', self.channel_requests())
		self.assertEqual(self.rows('SELECT state FROM collection_runs ORDER BY run_id'), [('complete',)] * 2)

	def test_the_session_lease_comes_before_any_database_write(self):
		session = os.path.join(os.environ[homes.HOME_ENV], 'session_file.session')
		os.makedirs(os.path.dirname(session), exist_ok=True)
		with Lease(session, 'The session'):
			code, _, err = self.collect(storage='both')
		self.assertEqual(code, 1)
		self.assertIn('The session is in use by another process', err)
		self.assertEqual(self.factory_calls, 0)
		self.assertFalse(os.path.exists(self.out))

	def test_migrate_holds_the_lease(self):
		self.collect(storage='sqlite')
		lease = database_lease_path(self.db())
		with DatabaseLease(self.db()):
			with self.assertRaises(DatabaseLocked):
				migrations.migrate(self.db())
		seen = []
		backups = migrations.migrate(
			self.db(), upgrades={1: lambda con: seen.append(os.path.exists(lease))}, target_version=2)
		self.assertEqual(seen, [True])
		self.assertEqual(len(backups), 1)
		self.assertFalse(os.path.exists(lease))


@unittest.skipUnless(os.name == 'nt', 'directory junctions are a Windows feature')
class AliasTests(DestinationCase):

	def setUp(self):
		super().setUp()
		import _winapi

		self.collect(storage='sqlite')
		self.alias = os.path.join(self.tmp, 'alias')
		_winapi.CreateJunction(self.out, self.alias)
		self.addCleanup(os.rmdir, self.alias)

	def test_the_database_lease_is_canonical(self):
		self.assertEqual(
			os.path.normcase(database_lease_path(os.path.join(self.alias, 'telegram-tracker.sqlite3'))),
			os.path.normcase(database_lease_path(self.db()))
		)
		with DatabaseLease(self.db()):
			code, _, err = self.collect(out=self.alias)
		self.assertEqual(code, 1)
		self.assertIn('is in use by another collection or upgrade', err)

	def test_the_output_lock_is_shared_through_the_alias(self):
		open(os.path.join(self.out, LOCK_FILE), 'w').close()
		code, _, err = self.collect(out=self.alias)
		self.assertEqual(code, 1)
		self.assertIn('is in use by another collection', err)
		os.remove(os.path.join(self.out, LOCK_FILE))

	def test_the_alias_is_the_same_destination(self):
		code, _, err = self.collect(out=self.alias)
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.rows('SELECT count(*) FROM collection_runs'), [(2,)])


class BindingTests(DestinationCase):

	def test_another_dataset_is_refused(self):
		self.collect(storage='both')
		manifest = self.manifest()
		manifest['dataset_uuid'] = '00000000-0000-4000-8000-000000000000'
		storage_files.write_json_pretty(os.path.join(self.out, MANIFEST_FILE), manifest)
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('another dataset', err)
		self.assertEqual(self.factory_calls, 1)

	def test_a_moved_folder_is_refused(self):
		self.collect(storage='sqlite')
		moved = os.path.join(self.tmp, 'moved')
		shutil.move(self.out, moved)
		code, _, err = self.collect(out=moved)
		self.assertEqual(code, 1)
		self.assertIn(f'was created for the folder {self.out}', err)
		self.assertEqual(self.factory_calls, 1)

	def test_a_missing_or_foreign_database_is_refused(self):
		self.collect(storage='sqlite')
		os.remove(self.db())
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('telegram-tracker.sqlite3 is missing', err)
		con = sqlite3.connect(self.db())
		con.execute('CREATE TABLE other (x)')
		con.close()
		before = storage_modes_support.listing(self.out)
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('is not a Telegram Tracker database', err)
		self.assertEqual(self.factory_calls, 1)
		self.assertEqual(storage_modes_support.listing(self.out), before)

	def test_a_protected_path_is_never_opened(self):
		self.collect(storage='sqlite')
		alias = os.path.join(self.tmp, 'protected-alias')
		os.link(self.db(), alias)
		destination = destinations.select_destination(self.out)
		destination.protected = (alias,)
		with self.assertRaises(destinations.DestinationError) as caught:
			destination.check()
		self.assertIn('is a protected file', str(caught.exception))
		destination.protected = ()
		destination.check()

	def test_the_check_before_credentials_cannot_write(self):
		self.collect(storage='sqlite')
		con = open_database(self.db(), readonly=True)
		try:
			with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
				con.execute('CREATE TABLE probe (x)')
		finally:
			con.close()
		seen = []
		original = connection.connect

		def connect(path, busy_timeout_ms=connection.BUSY_TIMEOUT_MS, *, readonly=False):
			seen.append(readonly)
			return original(path, busy_timeout_ms, readonly=readonly)

		with mock.patch.object(connection, 'connect', connect):
			destinations.select_destination(self.out).check()
		self.assertEqual(seen, [True])

	def test_collect_protects_the_selected_session(self):
		seen = []
		original = destinations.Destination.check

		def check(destination):
			seen.append(destination.protected)
			original(destination)

		with mock.patch.object(destinations.Destination, 'check', check):
			self.assertEqual(self.collect(storage='sqlite')[0], 0)
		session = os.path.join(os.environ[homes.HOME_ENV], 'session_file.session')
		self.assertEqual(seen, [(os.path.realpath(session),)])


class StaleRunTests(DestinationCase):

	def test_a_run_left_running_is_recorded_as_interrupted(self):
		self.collect(storage='both')
		con = sqlite3.connect(self.db())
		con.execute("UPDATE collection_runs SET state = 'running', finished_at = NULL")
		con.commit()
		con.close()
		code, _, err = self.collect()
		self.assertEqual(code, 0)
		self.assertIn('1 earlier run(s)', err)
		self.assertIn('now recorded as interrupted', err)
		(first, first_reason, first_end), second = self.rows(
			'SELECT state, stop_reason, finished_at FROM collection_runs ORDER BY run_id')
		self.assertEqual((first, first_end), ('interrupted', None))
		self.assertTrue(first_reason.startswith('stale: its process ended without finishing'))
		self.assertEqual(second, ('complete', None, second[2]))
		self.assertIsNotNone(second[2])
		self.assertEqual(read_json(os.path.join(self.out, STATUS_FILE))['state'], 'complete')

	def test_a_finished_run_is_not_touched(self):
		self.collect(storage='sqlite')
		code, _, err = self.collect()
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.rows('SELECT state FROM collection_runs'), [('complete',), ('complete',)])


if __name__ == '__main__':
	unittest.main()
