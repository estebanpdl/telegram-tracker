# -*- coding: utf-8 -*-
'''
local_errors: file failure reasons, session file (SQLite) messages from
real sqlite3 errors and from errors without a result code, and the split
of client errors into session, local file, and network failures. Also
the readers of credentials.json, profile.json and INI files, and the
lease, which name the reason instead of an exception type.
'''

# import modules
import configparser
import errno
import json
import os
import sqlite3
import tempfile
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker import local_errors
from telegram_tracker.credentials import identity, lease, sources, store
from telegram_tracker.credentials.protection import ensure_private_dir, protect


def sqlite_errors(folder):
	'''
	{kind: sqlite3 error} raised by SQLite itself on files in `folder`.
	'''
	found = {}
	path = os.path.join(folder, 'held.db')
	con = sqlite3.connect(path)
	con.execute('CREATE TABLE t (x)')
	con.commit()
	con.close()

	holder = sqlite3.connect(path, isolation_level=None)
	holder.execute('BEGIN EXCLUSIVE')
	reader = sqlite3.connect(path, timeout=0)
	try:
		reader.execute('SELECT * FROM t')
	except sqlite3.Error as exc:
		found['busy'] = exc
	finally:
		reader.close()
		holder.execute('ROLLBACK')
		holder.close()

	readonly = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
	try:
		readonly.execute('INSERT INTO t VALUES (1)')
	except sqlite3.Error as exc:
		found['readonly'] = exc
	finally:
		readonly.close()

	try:
		sqlite3.connect(os.path.join(folder, 'missing', 'x.db'))
	except sqlite3.Error as exc:
		found['cantopen'] = exc

	try:
		sqlite3.connect(folder)
	except sqlite3.Error as exc:
		found['isdir'] = exc

	garbage = os.path.join(folder, 'garbage.db')
	with open(garbage, 'wb') as fh:
		fh.write(b'not a database ' * 128)
	con = sqlite3.connect(garbage)
	try:
		con.execute('SELECT * FROM sqlite_master')
	except sqlite3.Error as exc:
		found['notadb'] = exc
	finally:
		con.close()
	return found


class FileReasonTests(unittest.TestCase):

	def test_reasons(self):
		cases = [
			(FileNotFoundError(errno.ENOENT, 'No such file', 'x'), 'not found'),
			(PermissionError(errno.EACCES, 'Access is denied', 'x'), 'permission denied'),
			(PermissionError(errno.EPERM, 'Operation not permitted', 'x'), 'permission denied'),
			(IsADirectoryError(errno.EISDIR, 'Is a directory', 'x'), 'it is a folder'),
			(OSError(errno.ENOSPC, 'No space left on device', 'x'), 'no space left on the device'),
			(OSError(errno.EIO, 'Input/output error.', 'x'), 'input/output error'),
			(OSError('no errno'), 'OSError'),
			(json.JSONDecodeError('Expecting value', 'x', 0), 'not valid JSON'),
			(UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'), 'not UTF-8 text'),
			(configparser.MissingSectionHeaderError('x', 1, 'line'), 'not a valid INI file'),
			(RuntimeError('other'), 'RuntimeError'),
		]
		for exc, expected in cases:
			with self.subTest(exc=type(exc).__name__, expected=expected):
				self.assertEqual(local_errors.file_reason(exc), expected)


class SessionFailureTests(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		cls.folder = os.path.realpath(cls.tmp.name)
		cls.errors = sqlite_errors(cls.folder)

	@classmethod
	def tearDownClass(cls):
		cls.errors = None
		cls.tmp.cleanup()

	EXPECTED = {
		'busy': ('SQLITE_BUSY', 'is locked by another program'),
		'readonly': ('SQLITE_READONLY', 'cannot be written'),
		'cantopen': ('SQLITE_CANTOPEN', 'cannot be opened'),
		# the Windows VFS has a result code for a folder; the unix VFS does not
		'isdir': ('SQLITE_CANTOPEN_ISDIR' if os.name == 'nt' else 'SQLITE_CANTOPEN',
			'is a folder, not a session file'),
		'notadb': ('SQLITE_NOTADB', 'is damaged or not a session file'),
	}

	def test_real_errors_are_named_by_their_result_code(self):
		self.assertEqual(sorted(self.errors), sorted(self.EXPECTED))
		for kind, (code, phrase) in self.EXPECTED.items():
			with self.subTest(kind=kind):
				exc = self.errors[kind]
				self.assertEqual(exc.sqlite_errorname, code)
				# the folder SQLite was asked to open, or a path that does not exist
				path = self.folder if kind == 'isdir' else 'S:/x.session'
				message = local_errors.session_failure(exc, path)
				self.assertIn(path, message)
				self.assertIn(phrase, message)
				self.assertIn(f'(SQLite: {exc})', message)

	def test_a_folder_is_named_whichever_code_reports_it(self):
		for code in ('SQLITE_CANTOPEN', 'SQLITE_CANTOPEN_ISDIR'):
			with self.subTest(code=code):
				exc = sqlite3.OperationalError('unable to open database file')
				exc.sqlite_errorname = code
				self.assertIn('is a folder', local_errors.session_failure(exc, self.folder))
		exc = sqlite3.OperationalError('unable to open database file')
		exc.sqlite_errorname = 'SQLITE_CANTOPEN'
		missing = os.path.join(self.folder, 'missing', 'x.session')
		self.assertIn('cannot be opened', local_errors.session_failure(exc, missing))

	def test_errors_without_a_result_code_use_their_message(self):
		for text, phrase in (
				('database is locked', 'is locked by another program'),
				('attempt to write a readonly database', 'cannot be written'),
				('unable to open database file', 'cannot be opened'),
				('file is not a database', 'is damaged or not a session file'),
				('database disk image is malformed', 'is damaged or not a session file'),
				('disk I/O error', 'cannot be used')):
			with self.subTest(text=text):
				exc = sqlite3.OperationalError(text)
				self.assertIsNone(getattr(exc, 'sqlite_errorname', None))
				self.assertIn(phrase, local_errors.session_failure(exc, 'S'))

	def test_no_message_advises_deleting_the_session_or_logging_in(self):
		for exc in self.errors.values():
			message = local_errors.session_failure(exc, 'S').lower()
			for word in ('delete', 'remove', 'log in', 'login'):
				self.assertNotIn(word, message)


class ClientFailureTests(unittest.TestCase):

	def test_errors_are_sorted_by_origin(self):
		cases = [
			(sqlite3.OperationalError('database is locked'), 'The session file S is locked'),
			(PermissionError(errno.EACCES, 'Access is denied', 'D:/f.txt'),
				'A local file cannot be used: D:/f.txt (permission denied).'),
			(ConnectionError('Connection to Telegram failed 5 time(s)'), 'Could not reach Telegram (ConnectionError).'),
			(ConnectionRefusedError(errno.ECONNREFUSED, 'refused'), 'Could not reach Telegram (ConnectionRefusedError).'),
			(TimeoutError(), 'Could not reach Telegram (TimeoutError).'),
			(OSError(errno.ENETUNREACH, 'unreachable'), 'Could not reach Telegram (OSError).'),
		]
		for exc, expected in cases:
			with self.subTest(exc=type(exc).__name__):
				self.assertTrue(local_errors.client_failure(exc, 'S').startswith(expected))
		self.assertIsNone(local_errors.client_failure(RuntimeError('x'), 'S'))


class ReaderReasonTests(offline_guard.OfflineTestCase):
	'''
	The credential readers and the lease name the reason.
	'''

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)

	def write(self, name, data):
		path = os.path.join(self.tmp, name)
		with open(path, 'wb') as fh:
			fh.write(data)
		return path

	def test_ini_reasons(self):
		for data, reason in (
				(b'\xff\xfe[x', 'not UTF-8 text'),
				(b'no section header', 'not a valid INI file')):
			with self.subTest(reason=reason):
				path = self.write('creds.ini', data)
				with self.assertRaises(sources.CredentialError) as ctx:
					sources.read_ini(path, '--config')
				self.assertEqual(str(ctx.exception), f'--config cannot be read ({reason})')
		with self.assertRaises(sources.CredentialError) as ctx:
			sources.read_ini(os.path.join(self.tmp, 'absent.ini'), '--config')
		self.assertEqual(str(ctx.exception), '--config cannot be read (not found)')

	def private_file(self, root, name, data):
		ensure_private_dir(root)
		path = os.path.join(root, name)
		with open(path, 'wb') as fh:
			fh.write(data)
		protect(path)
		return path

	def test_store_and_identity_reasons(self):
		root = os.path.join(self.tmp, 'home')
		for module, reader, error, name in (
				(store, lambda: store.read_store(root), store.StoreError, store.STORE_NAME),
				(identity, lambda: identity.read_identity(root, 'default'), identity.IdentityError,
					identity.IDENTITY_NAME)):
			with self.subTest(file=name):
				path = self.private_file(root, name, b'{not json')
				with self.assertRaises(error) as ctx:
					reader()
				self.assertEqual(str(ctx.exception), f'{path} cannot be read (not valid JSON)')

				real_open = open

				def denied(file, *args, **kwargs):
					if os.path.normcase(str(file)) == os.path.normcase(path):
						raise PermissionError(errno.EACCES, 'Access is denied', path)
					return real_open(file, *args, **kwargs)

				with mock.patch.object(module, 'open', denied, create=True):
					with self.assertRaises(error) as ctx:
						reader()
				self.assertEqual(str(ctx.exception), f'{path} cannot be read (permission denied)')
				os.remove(path)

	def test_lease_reasons(self):
		guarded = os.path.join(self.tmp, 'missing', 'x.json')
		with self.assertRaises(lease.LeaseError) as ctx:
			with lease.Lease(guarded, 'The file'):
				pass
		self.assertNotIsInstance(ctx.exception, lease.LeaseHeld)
		self.assertEqual(
			str(ctx.exception),
			f'cannot lock the file ({guarded}.lock: its folder does not exist)'
		)

		guarded = os.path.join(self.tmp, 'x.json')
		with mock.patch.object(lease.os, 'open', side_effect=PermissionError(errno.EACCES, 'Access is denied')):
			with self.assertRaises(lease.LeaseError) as ctx:
				with lease.Lease(guarded, 'The file'):
					pass
		self.assertEqual(str(ctx.exception), f'cannot lock the file ({guarded}.lock: permission denied)')


if __name__ == '__main__':
	unittest.main()
