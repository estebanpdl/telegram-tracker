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


if __name__ == '__main__':
	unittest.main()
