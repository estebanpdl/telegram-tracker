# -*- coding: utf-8 -*-
'''
The offline guard must reject network, real-client, and secret-file access.
Each test attempts the forbidden action and expects OfflineViolation; if the
guard were bypassed these attempts would connect, construct a client, or
open the file instead, and the assertions would fail.
'''

# import modules
import asyncio
import os
import pathlib
import socket
import sqlite3
import tempfile
import unittest

import offline_guard

from offline_guard import OfflineViolation, expected_violations


class SocketGuardTests(offline_guard.OfflineTestCase):

	def test_socket_connect_is_rejected(self):
		with expected_violations() as caught:
			sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			try:
				with self.assertRaises(OfflineViolation):
					sock.connect(('127.0.0.1', 9))
			finally:
				sock.close()
		self.assertEqual(len(caught), 1)
		self.assertIn('socket.connect', caught[0])

	def test_create_connection_and_dns_are_rejected(self):
		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				socket.create_connection(('149.154.167.50', 443), timeout=1)
			with self.assertRaises(OfflineViolation):
				socket.getaddrinfo('example.org', 443)
		self.assertEqual(len(caught), 2)

	def test_socketpair_still_works_for_event_loops(self):
		left, right = socket.socketpair()
		try:
			left.sendall(b'x')
			self.assertEqual(right.recv(1), b'x')
		finally:
			left.close()
			right.close()


class AsyncioGuardTests(offline_guard.OfflineAsyncTestCase):

	async def test_open_connection_is_rejected(self):
		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				await asyncio.open_connection('127.0.0.1', 9)
		self.assertEqual(len(caught), 1)


class TelethonGuardTests(offline_guard.OfflineTestCase):

	def test_real_client_cannot_be_constructed(self):
		from telethon import TelegramClient

		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				TelegramClient('guard_probe', 1, 'hash')
		self.assertEqual(caught, ['telethon: real TelegramClient constructed'])


class SecretFileGuardTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name

	def test_config_and_session_paths_are_rejected(self):
		config = os.path.join(self.tmp, 'config.ini')
		session = os.path.join(self.tmp, 'session_file.session')
		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				open(config, encoding='utf-8')
			with self.assertRaises(OfflineViolation):
				pathlib.Path(session).read_bytes()
			with self.assertRaises(OfflineViolation):
				os.open(session + '-journal', os.O_RDONLY)
			with self.assertRaises(OfflineViolation):
				sqlite3.connect(session)
		self.assertEqual(len(caught), 4)
		self.assertEqual(os.listdir(self.tmp), [])

	def test_configparser_cannot_read_config(self):
		from configparser import ConfigParser

		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				ConfigParser().read(os.path.join(self.tmp, 'config.ini'))
		self.assertEqual(len(caught), 1)

	def test_ordinary_files_are_allowed(self):
		path = os.path.join(self.tmp, 'notes.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('ok')
		self.assertEqual(pathlib.Path(path).read_text(encoding='utf-8'), 'ok')


class GuardEnforcementTests(offline_guard.OfflineTestCase):

	def test_guard_reports_installed(self):
		self.assertTrue(offline_guard.is_installed())

	def test_restoring_an_original_is_detected(self):
		guarded = socket.socket.connect
		socket.socket.connect = lambda self, address: None
		try:
			self.assertFalse(offline_guard.is_installed())
		finally:
			socket.socket.connect = guarded
		self.assertTrue(offline_guard.is_installed())

	def test_swallowed_violation_still_fails_the_test(self):
		class Inner(offline_guard.OfflineTestCase):
			def test_swallows(self):
				try:
					socket.create_connection(('127.0.0.1', 9))
				except Exception:
					pass

		result = unittest.TestResult()
		Inner('test_swallows').run(result)
		self.assertEqual(len(result.failures), 1)
		self.assertIn('offline guard violations', result.failures[0][1])


class RealHomeGuardTests(offline_guard.OfflineTestCase):
	'''
	The user's real application home is never touched: every call below is
	refused before it reaches the file system. Alias forms are checked
	against a temporary stand-in home, so no real-home setup is needed.
	'''

	REAL_HOME = os.path.join(os.path.expanduser('~'), offline_guard.APP_DIR)

	def assert_refused(self, call, *args, **kwargs):
		with expected_violations() as caught:
			with self.assertRaises(OfflineViolation):
				call(*args, **kwargs)
		self.assertEqual(len(caught), 1, caught)
		self.assertTrue(caught[0].startswith('home:'), caught)

	def test_real_home_identity_is_captured_from_the_user_profile(self):
		self.assertIn(os.path.normcase(self.REAL_HOME), offline_guard._home_identities)

	def test_every_file_system_entry_point_refuses_the_real_home(self):
		target = os.path.join(self.REAL_HOME, 'credentials.json')
		with tempfile.TemporaryDirectory() as tmp:
			outside = os.path.join(tmp, 'x.json')
			for label, call, args in (
					('open', open, (target,)),
					('os.open', os.open, (target, os.O_RDONLY)),
					('os.stat', os.stat, (target,)),
					('os.lstat', os.lstat, (self.REAL_HOME,)),
					('os.path.exists', os.path.exists, (self.REAL_HOME,)),
					('os.path.isfile', os.path.isfile, (target,)),
					('os.path.isdir', os.path.isdir, (self.REAL_HOME,)),
					('os.listdir', os.listdir, (self.REAL_HOME,)),
					('os.scandir', os.scandir, (self.REAL_HOME,)),
					('os.mkdir', os.mkdir, (self.REAL_HOME,)),
					('os.makedirs', os.makedirs, (os.path.join(self.REAL_HOME, 'sub'),)),
					('os.remove', os.remove, (target,)),
					('os.rmdir', os.rmdir, (self.REAL_HOME,)),
					('os.chmod', os.chmod, (target, 0o600)),
					('os.replace into', os.replace, (outside, target)),
					('os.rename out of', os.rename, (target, outside)),
					('pathlib exists', lambda p: pathlib.Path(p).exists(), (target,)),
					('sqlite3.connect', sqlite3.connect, (os.path.join(self.REAL_HOME, 'x.db'),)),
					('sqlite3 file: URI', sqlite3.connect,
						('file:' + pathlib.Path(self.REAL_HOME, 'x.db').as_posix() + '?mode=ro',))):
				with self.subTest(call=label):
					self.assert_refused(call, *args)

	def test_test_processes_use_a_temporary_home_and_session(self):
		home = os.environ[offline_guard.HOME_ENV]
		session = os.environ[offline_guard.SESSION_ENV]
		self.assertTrue(os.path.isabs(home))
		self.assertFalse(offline_guard.in_real_home(home))
		self.assertTrue(os.path.isdir(home))
		self.assertEqual(os.path.dirname(session), home)

	def test_a_real_home_override_is_replaced_in_subprocesses(self):
		import subprocess
		import sys

		env = dict(os.environ, **{
			offline_guard.HOME_ENV: os.path.join(self.REAL_HOME, 'nested'),
			offline_guard.SESSION_ENV: os.path.join(self.REAL_HOME, 'session_file.session')
		})
		tests = os.path.dirname(os.path.abspath(__file__))
		code = (
			f'import sys; sys.path.insert(0, {tests!r}); import os, offline_guard; '
			f'print(offline_guard.in_real_home(os.environ[{offline_guard.HOME_ENV!r}]), '
			f'offline_guard.in_real_home(os.environ[{offline_guard.SESSION_ENV!r}]))'
		)
		completed = subprocess.run(
			[sys.executable, '-I', '-c', code], env=env, capture_output=True, text=True, timeout=120
		)
		self.assertEqual(completed.stdout.strip(), 'False False', completed.stderr)

	def test_aliases_of_the_home_are_refused(self):
		with tempfile.TemporaryDirectory() as tmp:
			stand_in = os.path.join(tmp, 'apphome')
			os.mkdir(stand_in)
			os.mkdir(os.path.join(tmp, 'other'))
			link = os.path.join(tmp, 'link')
			if os.name == 'nt':
				import _winapi

				_winapi.CreateJunction(stand_in, link)
			else:
				os.symlink(stand_in, link)
			short = _short_path(stand_in)
			cwd = os.getcwd()
			with offline_guard.pretend_real_home(stand_in):
				cases = [
					('plain', os.path.join(stand_in, 'credentials.json')),
					('upper case', os.path.join(stand_in, 'credentials.json').upper()),
					('dot-dot', os.path.join(tmp, 'other', '..', 'apphome', 'credentials.json')),
					('junction or symlink', os.path.join(link, 'credentials.json')),
					('link itself', link),
				]
				if short and os.path.normcase(short) != os.path.normcase(stand_in):
					cases.append(('8.3 short name', os.path.join(short, 'credentials.json')))
				for label, path in cases:
					with self.subTest(alias=label):
						self.assert_refused(os.stat, path)
				os.chdir(tmp)
				try:
					with self.subTest(alias='relative'):
						self.assert_refused(open, os.path.join('apphome', 'credentials.json'))
				finally:
					os.chdir(cwd)
				with self.subTest(alias='sibling outside'):
					self.assertTrue(os.path.isdir(os.path.join(tmp, 'other')))
			os.rmdir(link) if os.name == 'nt' else os.remove(link)


def _short_path(path):
	'''
	The Windows 8.3 form of an existing path ('' elsewhere or when short
	names are disabled on the volume).
	'''
	if os.name != 'nt':
		return ''
	import ctypes

	buffer = ctypes.create_unicode_buffer(32768)
	if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
		return buffer.value
	return ''


if __name__ == '__main__':
	unittest.main()
