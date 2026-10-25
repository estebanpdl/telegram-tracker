# -*- coding: utf-8 -*-
'''
The opt-in sync switch: what it changes on a connection and on fsync, what
it must leave alone (errors, busy waits, the busy timeout), and that it is
off unless asked for and never reaches child processes.
'''

# import modules
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from unittest import mock

import offline_guard
import no_sync


TESTS = os.path.dirname(os.path.abspath(__file__))


def plain_connect():
	'''
	The suite's sqlite3.connect without the switch, whether or not this
	run turned it on.
	'''
	connect = sqlite3.connect
	if connect.__name__ == 'no_sync_connect':
		connect = connect.__wrapped__
	return connect


def traced_connect(statements):
	'''
	A plain connect that records every statement run on the connection.
	'''
	def connect(*args, **kwargs):
		con = plain_connect()(*args, **kwargs)
		con.set_trace_callback(statements.append)
		return con

	return connect


class ConnectionTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.db = os.path.join(tmp.name, 'data.db')
		self.junk = os.path.join(tmp.name, 'junk.db')
		with open(self.junk, 'wb') as fh:
			fh.write(b'not a database' * 100)

	def open(self, connect, *args, **kwargs):
		con = connect(*args, **kwargs)
		self.addCleanup(con.close)
		return con

	def test_new_connections_skip_syncs(self):
		plain = self.open(plain_connect(), self.db)
		self.assertEqual(plain.execute('PRAGMA synchronous').fetchone(), (2,))
		switched = self.open(no_sync.connect_without_sync(plain_connect()), self.db)
		self.assertEqual(switched.execute('PRAGMA synchronous').fetchone(), (0,))

	def test_the_pragma_runs_without_a_busy_wait_and_the_timeout_is_restored(self):
		statements = []
		connect = no_sync.connect_without_sync(traced_connect(statements))
		for timeout, ms in ((3.5, 3500), (None, 5000)):
			with self.subTest(timeout=timeout):
				del statements[:]
				kwargs = {} if timeout is None else {'timeout': timeout}
				con = self.open(connect, self.db, **kwargs)
				self.assertEqual(statements, [
					'PRAGMA busy_timeout', 'PRAGMA busy_timeout = 0',
					'PRAGMA synchronous = OFF', f'PRAGMA busy_timeout = {ms}',
				])
				self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone(), (ms,))

	def test_a_damaged_file_fails_at_the_first_statement_as_without_the_switch(self):
		for label, connect in (('plain', plain_connect()), ('switch', no_sync.connect_without_sync(plain_connect()))):
			with self.subTest(connect=label):
				con = self.open(connect, self.junk)
				with self.assertRaises(sqlite3.DatabaseError) as raised:
					con.execute('SELECT name FROM sqlite_master').fetchall()
				self.assertEqual(raised.exception.sqlite_errorname, 'SQLITE_NOTADB')

	def test_a_locked_file_fails_at_the_first_statement_as_without_the_switch(self):
		holder = self.open(plain_connect(), self.db, isolation_level=None)
		holder.execute('CREATE TABLE t (x)')
		holder.execute('BEGIN EXCLUSIVE')
		statements = []
		con = self.open(no_sync.connect_without_sync(traced_connect(statements)), self.db, timeout=0.05)
		# the refused pragma never runs, so it is not traced; the timeout is restored anyway
		self.assertEqual(statements, ['PRAGMA busy_timeout', 'PRAGMA busy_timeout = 0', 'PRAGMA busy_timeout = 50'])
		self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone(), (50,))
		with self.assertRaises(sqlite3.OperationalError) as raised:
			con.execute('SELECT x FROM t').fetchall()
		self.assertEqual(raised.exception.sqlite_errorname, 'SQLITE_BUSY')
		holder.execute('ROLLBACK')
		# the refused pragma left the connection's default in place
		self.assertEqual(con.execute('PRAGMA synchronous').fetchone(), (2,))


class FsyncTests(offline_guard.OfflineTestCase):

	def test_open_descriptors_and_files_pass_and_invalid_ones_fail(self):
		with tempfile.TemporaryFile() as fh:
			fh.write(b'x')
			self.assertIsNone(no_sync.fsync_without_flush(fh))
			self.assertIsNone(no_sync.fsync_without_flush(fh.fileno()))
		for fd in (-1, fh):
			with self.subTest(fd=fd):
				with self.assertRaises((OSError, ValueError)):
					no_sync.fsync_without_flush(fd)


class InstallTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		self.connect = mock.Mock(name='connect')
		self.fsync = mock.Mock(name='fsync')
		for patcher in (
			mock.patch.object(sqlite3, 'connect', self.connect),
			mock.patch.object(os, 'fsync', self.fsync),
			mock.patch.object(no_sync, 'ENABLED', False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_unset_empty_and_zero_keep_syncs(self):
		for environ in ({}, {no_sync.ENV: ''}, {no_sync.ENV: '0'}):
			with self.subTest(environ=environ):
				self.assertFalse(no_sync.install(environ))
				self.assertIs(sqlite3.connect, self.connect)
				self.assertIs(os.fsync, self.fsync)
				self.assertFalse(no_sync.ENABLED)
				self.assertNotIn(no_sync.ENV, environ)

	def test_one_installs_both_and_removes_the_variable(self):
		environ = {no_sync.ENV: '1', 'OTHER': 'kept'}
		self.assertTrue(no_sync.install(environ))
		self.assertIs(sqlite3.connect.__wrapped__, self.connect)
		self.assertIs(os.fsync, no_sync.fsync_without_flush)
		self.assertTrue(no_sync.ENABLED)
		self.assertEqual(environ, {'OTHER': 'kept'})

	def test_other_values_are_refused_and_install_nothing(self):
		for value in ('yes', 'true', ' 1'):
			with self.subTest(value=value):
				with self.assertRaises(ValueError) as raised:
					no_sync.install({no_sync.ENV: value})
				self.assertIn(no_sync.ENV, str(raised.exception))
				self.assertIs(sqlite3.connect, self.connect)
				self.assertIs(os.fsync, self.fsync)


CHILD = '''
import json, os, sqlite3, subprocess, sys, tempfile
sys.path.insert(0, {tests!r})
import offline_guard, no_sync
report = {{'enabled': no_sync.ENABLED, 'variable_left': no_sync.ENV in os.environ,
	'fsync_replaced': os.fsync is no_sync.fsync_without_flush}}
with tempfile.TemporaryDirectory() as tmp:
	con = sqlite3.connect(os.path.join(tmp, 'x.db'))
	report['synchronous'] = con.execute('PRAGMA synchronous').fetchone()[0]
	con.close()
if len(sys.argv) > 1:
	child = subprocess.run([sys.executable, '-I', '-c', sys.argv[1]], capture_output=True, text=True, timeout=120)
	report['child'] = json.loads(child.stdout) if child.returncode == 0 else child.stderr
print(json.dumps(report))
'''


class ProcessTests(offline_guard.OfflineTestCase):
	'''
	Real test processes: the switch applies only where the variable is set,
	and processes that a switched process starts keep real syncs.
	'''

	def run_child(self, env):
		code = CHILD.format(tests=TESTS)
		completed = subprocess.run(
			[sys.executable, '-I', '-c', code, code], env=env,
			capture_output=True, text=True, timeout=120
		)
		self.assertEqual(completed.returncode, 0, completed.stderr)
		return json.loads(completed.stdout)

	def test_only_a_process_started_with_the_variable_skips_syncs(self):
		off = {'enabled': False, 'variable_left': False, 'fsync_replaced': False, 'synchronous': 2}
		environ = {k: v for k, v in os.environ.items() if k != no_sync.ENV}
		with self.subTest(variable='unset'):
			self.assertEqual(self.run_child(environ), dict(off, child=off))
		with self.subTest(variable='1'):
			self.assertEqual(
				self.run_child(dict(environ, **{no_sync.ENV: '1'})),
				{'enabled': True, 'variable_left': False, 'fsync_replaced': True, 'synchronous': 0, 'child': off}
			)


if __name__ == '__main__':
	unittest.main()
