# -*- coding: utf-8 -*-
'''
Diagnostics of `auth` and `collect`, with a temporary home and a fake
client:

	- `auth status` notes a ./config/config.ini that saved credentials
	  shadow (fields by name only), and the selection stays as it was;
	- with --check, the Session line leaves the login to the Login line;
	- failures of the session file (SQLite), of other local files, and
	  of the network, and an occupied or unusable session lease, each
	  get their own message with exit 1 in `auth login`, `auth status
	  --check`, `auth logout`, and both collect forms, for the default
	  and a named profile, and change no file.

The INI file and session files are pretended (the offline guard forbids
creating them); sqlite3 errors come from SQLite itself.
'''

# import modules
import errno
import json
import os
import tempfile
import unittest

import source_path  # noqa: F401  (imports telegram_tracker from src)

from auth_support import VALID, CredentialCase, env_values
from fakes import FIXTURE_ACCOUNT, FakeTelegramClient
from test_local_errors import sqlite_errors

from telegram_tracker import api
from telegram_tracker.credentials import identity, profiles, sources, store
from telegram_tracker.credentials.lease import Lease


# synthetic INI values: none may appear in any output
INI_VALUES = {'api_id': '5550001', 'api_hash': 'INIHASH0a1b2c3d4e5f60718293a4b5', 'phone': '+990000INIPHONE'}
STORE_LABELS = 'Credentials in use: api_id: credentials.json; api_hash: credentials.json; phone: credentials.json'
SHADOW_NOTE = (
	'Note: ./config/config.ini also sets api_id, api_hash, phone but is not used, because '
	'credentials.json comes first. Pass --config ./config/config.ini to use it instead.'
)


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def commands(out, *extra):
	'''
	{name: argv} of every command that opens the session. The legacy
	collection form takes neither --profile nor --session, so it runs
	only without `extra`.
	'''
	collect = ['--telegram-channel', 'alpha_fx', '-o', out, *extra]
	found = {
		'login': ['auth', 'login', '--no-save', *extra],
		'status --check': ['auth', 'status', '--check', *extra],
		'logout': ['auth', 'logout', '--yes', *extra],
		'collect': ['collect', *collect],
	}
	if not extra:
		found['legacy collect'] = collect
	return found


class DiagnosticsCase(CredentialCase):

	def setUp(self):
		super().setUp()
		self.legacy_ini = os.path.join(self.cwd, sources.LEGACY_INI)
		self.ini = {}
		self.ini_reads = []
		self.out = os.path.join(self.tmp, 'out')

		def read_ini(path, label):
			self.ini_reads.append(label)
			value = self.ini[label]
			if isinstance(value, Exception):
				raise value
			return dict(value)

		self.patch(sources, 'read_ini', read_ini)

	def legacy(self, values=INI_VALUES):
		self.pretend_files(self.legacy_ini)
		self.ini[sources.LEGACY_CONFIG] = values

	def assert_no_ini_values(self):
		for text in self.outputs:
			for value in INI_VALUES.values():
				self.assertNotIn(value, text)

	def notes(self, out):
		return [line for line in out.splitlines() if line.startswith('Note:')]

	def line(self, out, prefix):
		found = [line for line in out.splitlines() if line.startswith(prefix)]
		self.assertEqual(len(found), 1, out)
		return found[0]

	def named_profile(self, name='alice'):
		profile = profiles.Profile(name, profiles.FLAG, self.home)
		profiles.prepare_root(profile)
		identity.ensure_identity(profile.root, name, FIXTURE_ACCOUNT)
		store.save_store(profile.root, VALID)
		return os.path.join(profile.root, 'session_file.session')

	def snapshot(self):
		'''
		Every file under the temporary directory with its size and mtime.
		'''
		found = {}
		for root, _, files in os.walk(self.tmp):
			for name in files:
				path = os.path.join(root, name)
				stat = os.stat(path)
				found[path] = (stat.st_size, stat.st_mtime_ns)
		return found


class ShadowedConfigTests(DiagnosticsCase):

	def status(self, *extra):
		code, out, err = self.run_main(['auth', 'status', *extra])
		self.assertEqual(err, '')
		return code, out

	def test_saved_credentials_shadowing_a_real_ini_are_noted(self):
		self.save()
		self.legacy()
		code, out = self.status()
		self.assertEqual(code, 0)
		self.assertEqual(self.line(out, 'Credentials in use:'), STORE_LABELS)
		self.assertEqual(self.notes(out), [SHADOW_NOTE])
		self.assertEqual(self.ini_reads, [sources.LEGACY_CONFIG])
		self.assert_no_ini_values()
		self.assert_no_secrets()

	def test_no_note_without_a_real_ini(self):
		self.save()
		for name, values in (
				('absent', None),
				('placeholders', {'api_id': 'api_id', 'api_hash': '<your api_hash>', 'phone': ''})):
			with self.subTest(ini=name):
				del self.ini_reads[:]
				if values is not None:
					self.legacy(values)
				code, out = self.status()
				self.assertEqual(code, 0)
				self.assertEqual(self.line(out, 'Credentials in use:'), STORE_LABELS)
				self.assertEqual(self.notes(out), [])
				self.assertEqual(self.ini_reads, [] if values is None else [sources.LEGACY_CONFIG])

	def test_invalid_ini_is_noted_without_changing_the_result(self):
		self.save()
		cases = (
			(sources.CredentialError('./config/config.ini cannot be read (permission denied)'),
				'Note: ./config/config.ini cannot be read (permission denied); not used, because '
				'credentials.json comes first'),
			(sources.CredentialError('./config/config.ini has no [Telegram API credentials] section'),
				'Note: ./config/config.ini has no [Telegram API credentials] section; not used, '
				'because credentials.json comes first'),
			(dict(INI_VALUES, api_id='not-a-number'),
				SHADOW_NOTE.replace('phone but', 'phone (invalid: api_id) but')),
			({'api_id': 'api_id', 'api_hash': INI_VALUES['api_hash'], 'phone': 'phone'},
				SHADOW_NOTE.replace('api_id, api_hash, phone', 'api_hash')),
		)
		for values, note in cases:
			with self.subTest(note=note):
				self.legacy(values)
				code, out = self.status()
				self.assertEqual(code, 0)
				self.assertEqual(self.line(out, 'Credentials in use:'), STORE_LABELS)
				self.assertEqual(self.notes(out), [note])
		self.assertNotIn('not-a-number', ''.join(self.outputs))
		self.assert_no_ini_values()

	def test_environment_and_explicit_ini_keep_precedence_and_read_no_legacy_ini(self):
		self.save()
		self.legacy()
		os.environ.update(env_values(VALID))
		code, out = self.status()
		self.assertEqual(code, 0)
		self.assertIn('api_id: environment; api_hash: environment; phone: environment', out)
		self.assertEqual((self.notes(out), self.ini_reads), ([], []))
		for variable in sources.ENV_VARS.values():
			del os.environ[variable]

		explicit = os.path.join(self.cwd, 'other.ini')
		self.pretend_files(explicit)
		self.ini[sources.CONFIG] = INI_VALUES
		code, out = self.status('--config', 'other.ini')
		self.assertEqual(code, 0)
		self.assertIn('api_id: --config; api_hash: --config; phone: --config', out)
		self.assertEqual((self.notes(out), self.ini_reads), ([], [sources.CONFIG]))
		self.assert_no_ini_values()

	def test_partial_environment_over_saved_credentials_still_notes_the_ini(self):
		self.save()
		self.legacy()
		os.environ[sources.ENV_VARS['phone']] = '+990000ENVPHONE'
		code, out = self.status()
		self.assertEqual(code, 0)
		self.assertIn('api_id: credentials.json; api_hash: credentials.json; phone: environment', out)
		self.assertEqual(self.notes(out), [SHADOW_NOTE])
		self.assertNotIn('+990000ENVPHONE', out)

	def test_ini_used_without_saved_credentials_is_not_a_shadow(self):
		self.legacy()
		code, out = self.status()
		self.assertEqual(code, 0)
		self.assertIn('api_id: ./config/config.ini; api_hash: ./config/config.ini; phone: ./config/config.ini', out)
		self.assertEqual(self.notes(out), [])
		self.assert_no_ini_values()

	def test_named_profile_never_reads_the_ini(self):
		self.named_profile()
		self.legacy()
		code, out = self.status('--profile', 'alice')
		self.assertEqual(code, 0)
		self.assertEqual(self.line(out, 'Credentials in use:'), STORE_LABELS)
		self.assertEqual((self.notes(out), self.ini_reads), ([], []))


class CheckSessionLineTests(DiagnosticsCase):

	def setUp(self):
		super().setUp()
		self.save()

	def session_line(self, out):
		return self.line(out, 'Session:')

	def test_offline_status_still_says_unverified(self):
		self.pretend_files(self.home_session)
		code, out, _ = self.run_main(['auth', 'status'])
		self.assertEqual(code, 0)
		self.assertEqual(
			self.session_line(out),
			f'Session: {self.home_session} (application home): present; login not verified (offline)'
		)
		self.assertNotIn('Login:', out)

	def test_check_leaves_the_login_to_the_login_line(self):
		self.pretend_files(self.home_session)
		for authorized, code, login in ((True, 0, 'Login: logged in'), (False, 1, 'Login: not logged in')):
			with self.subTest(authorized=authorized):
				self.client = FakeTelegramClient(authorized=authorized)
				got, out, _ = self.run_main(['auth', 'status', '--check'])
				self.assertEqual(got, code)
				self.assertEqual(self.session_line(out), f'Session: {self.home_session} (application home): present')
				self.assertEqual(out.splitlines()[-1], login)
				self.assertNotIn('not verified', out)

	def test_failed_check_prints_no_login_line(self):
		self.pretend_files(self.home_session)
		self.client.script('connect', ConnectionError('Connection to Telegram failed 5 time(s)'))
		code, out, err = self.run_main(['auth', 'status', '--check'])
		self.assertEqual(code, 1)
		self.assertEqual(self.session_line(out), f'Session: {self.home_session} (application home): present')
		self.assertNotIn('Login:', out)
		self.assertEqual(err, '> Could not reach Telegram (ConnectionError).\n')

	def test_missing_session_with_check(self):
		code, out, err = self.run_main(['auth', 'status', '--check'])
		self.assertEqual(code, 1)
		self.assertEqual(self.session_line(out), f'Session: {self.home_session} (application home): missing')
		self.assertIn('a check never creates one', err)


class SessionAndNetworkFailureTests(DiagnosticsCase):
	'''
	Each failure through each command that opens the session.
	'''

	@classmethod
	def setUpClass(cls):
		cls.sqlite_tmp = tempfile.TemporaryDirectory()
		cls.sqlite = sqlite_errors(os.path.realpath(cls.sqlite_tmp.name))

	@classmethod
	def tearDownClass(cls):
		cls.sqlite = None
		cls.sqlite_tmp.cleanup()

	def commands(self, profile=None):
		return commands(self.out, *(['--profile', profile] if profile else []))

	def failures(self, session):
		'''
		(name, where, error, expected stderr).
		'''
		local = os.path.join(self.tmp, 'some-file')
		return [
			('locked', 'create', self.sqlite['busy'],
				f'> The session file {session} is locked by another program (SQLite: database is locked). '
				'Close the program that has it open, then try again.'),
			('read-only', 'connect', self.sqlite['readonly'],
				f'> The session file {session} cannot be written (SQLite: attempt to write a readonly '
				'database). Make the file and its folder writable for your account.'),
			('cannot open', 'create', self.sqlite['cantopen'],
				f'> The session file {session} cannot be opened (SQLite: unable to open database file). '
				'Check that its folder exists and that your account can read and write it.'),
			('damaged', 'create', self.sqlite['notadb'],
				f'> The session file {session} is damaged or not a session file (SQLite: file is not a database).'),
			('local file', 'connect', PermissionError(errno.EACCES, 'Access is denied', local),
				f'> A local file cannot be used: {local} (permission denied).'),
			('network', 'connect', ConnectionError('Connection to Telegram failed 5 time(s)'),
				'> Could not reach Telegram (ConnectionError).'),
			('timeout', 'connect', TimeoutError(), '> Could not reach Telegram (TimeoutError).'),
		]

	def inject(self, where, error):
		'''
		`error` raised when the client is created (the session file is
		opened then) or when it connects.
		'''
		client = FakeTelegramClient()
		if where == 'connect':
			client.script('connect', error)

		def create_client(session_file, api_id, api_hash):
			self.created.append((session_file, api_id, api_hash))
			if where == 'create':
				raise error
			return client

		self.patch(api, 'create_client', create_client)

	def check_matrix(self, session, profile=None):
		for failure, where, error, expected in self.failures(session):
			for command, argv in self.commands(profile).items():
				with self.subTest(failure=failure, command=command):
					del self.created[:]
					self.inject(where, error)
					before = self.snapshot()
					code, out, err = self.run_main(argv)
					self.assertEqual(code, 1)
					self.assertEqual(err.splitlines()[-1], expected)
					self.assertNotIn('Traceback', err)
					self.assertEqual([c[0] for c in self.created], [session])
					self.assertEqual(self.snapshot(), before)
					self.assertFalse(os.path.exists(self.out))
					self.assertFalse(os.path.exists(session + '.lock'))
		self.assert_no_secrets()

	def test_default_profile(self):
		self.save()
		self.pretend_files(self.home_session)
		os.makedirs(os.path.join(self.home, 'account-leases'))
		self.check_matrix(self.home_session)

	def test_named_profile(self):
		session = self.named_profile()
		self.pretend_files(session)
		os.makedirs(os.path.join(self.home, 'account-leases'))
		self.check_matrix(session, profile='alice')

	def test_a_session_failure_during_collection_keeps_saved_data(self):
		self.save()
		self.pretend_files(self.home_session)
		code, _, err = self.run_main(['collect', '--telegram-channel', 'alpha_fx', '-o', self.out])
		self.assertEqual((code, err), (0, ''))
		messages = os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')
		with open(messages, 'rb') as fh:
			saved = fh.read()

		self.client = FakeTelegramClient()
		self.client.script('GetHistoryRequest', self.sqlite['busy'])
		code, _, err = self.run_main(['collect', '--telegram-channel', 'alpha_fx', '-o', self.out])
		self.assertEqual(code, 1)
		self.assertIn(f'> The session file {self.home_session} is locked by another program', err)
		self.assertNotIn('Traceback', err)
		with open(messages, 'rb') as fh:
			self.assertEqual(fh.read(), saved)
		status = read_json(os.path.join(self.out, 'collection-status.json'))
		self.assertEqual(status['state'], 'interrupted')
		self.assertIn('database is locked', status['stop_reason'])
		self.assertEqual(self.client.disconnect_count, 1)


class SessionLeaseTests(DiagnosticsCase):
	'''
	An occupied lease, and a lease that cannot be created because the
	session's folder does not exist: no client, no change.
	'''

	def commands(self, *extra):
		return commands(self.out, *extra)

	def check(self, commands, expected, session):
		for command, argv in commands.items():
			with self.subTest(command=command):
				before = self.snapshot()
				code, _, err = self.run_main(argv)
				self.assertEqual(code, 1)
				self.assertEqual(err.splitlines()[-1], expected)
				self.assertEqual(self.created, [])
				self.assertEqual(self.snapshot(), before)
				self.assertFalse(os.path.exists(self.out))

	def held(self, session, *extra):
		with Lease(session, 'The session'):
			lock = session + '.lock'
			with open(lock, encoding='utf-8') as fh:
				holder = fh.read()
			self.assertIn('"pid"', holder)
			self.check(
				self.commands(*extra),
				f'> The session is in use by another process (pid {os.getpid()}, started '
				f'{read_json(lock)["started_at"]}). If none is running, an earlier run was '
				f'interrupted: delete {lock} and try again.',
				session
			)
		self.assertFalse(os.path.exists(session + '.lock'))

	def test_held_lease_default_and_named_profile(self):
		self.save()
		self.pretend_files(self.home_session)
		os.makedirs(os.path.join(self.home, 'account-leases'))
		self.held(self.home_session)
		session = self.named_profile()
		self.pretend_files(session)
		self.held(session, '--profile', 'alice')

	def test_missing_session_folder(self):
		self.save()
		os.makedirs(os.path.join(self.home, 'account-leases'))
		session = os.path.join(self.tmp, 'missing', 'session_file.session')
		self.pretend_files(session)
		self.check(
			self.commands('--session', session),
			f'> cannot lock the session ({session}.lock: its folder does not exist)',
			session
		)
		self.assertFalse(os.path.exists(os.path.dirname(session)))


if __name__ == '__main__':
	unittest.main()
