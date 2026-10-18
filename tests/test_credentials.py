# -*- coding: utf-8 -*-
'''
Per-user credentials: the credentials.json store (schema, owner-only
protection, atomic saves, leases, removal), the application home and
session selection, and credential source precedence. Everything runs in
temporary directories; the offline guard refuses the real home, config.ini
and .session files, so existence checks for those names go through
injected adapters.
'''

# import modules
import json
import os
import stat
import tempfile
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.credentials import home, protection, sources, store
from telegram_tracker.credentials.lease import Lease


API_ID = 7654321
API_HASH = 'S3NTINEL0a1b2c3d4e5f60718293a4b5c'
PHONE = '+990000S3NTINEL'
SECRETS = (str(API_ID), API_HASH, PHONE)
VALID = {'api_id': API_ID, 'api_hash': API_HASH, 'phone': PHONE}


class SecretsMixin:

	def assert_no_secrets(self, *texts):
		for text in texts:
			for secret in SECRETS:
				self.assertNotIn(secret, str(text))


class TempHomeCase(SecretsMixin, offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.home = os.path.join(self.tmp, 'home')

	def store_file(self):
		return store.store_path(self.home)

	def write_raw(self, data, *, private=True):
		'''
		Write credentials.json content directly (bypassing the store).
		'''
		if private:
			protection.ensure_private_dir(self.home)
		else:
			os.makedirs(self.home, exist_ok=True)
		with open(self.store_file(), 'w', encoding='utf-8') as fh:
			fh.write(data if isinstance(data, str) else json.dumps(data))
		if private:
			protection.protect(self.store_file())


class StoreTests(TempHomeCase):

	def test_round_trip_holds_exactly_the_schema(self):
		store.save_store(self.home, VALID)
		self.assertEqual(store.read_store(self.home), VALID)
		with open(self.store_file(), encoding='utf-8') as fh:
			data = json.load(fh)
		self.assertEqual(data, {'schema_version': 1, **VALID})
		self.assertEqual(sorted(os.listdir(self.home)), ['credentials.json'])

	def test_home_and_file_are_private(self):
		store.save_store(self.home, VALID)
		protection.check(self.home)
		protection.check(self.store_file())
		if os.name != 'nt':
			self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)
			self.assertEqual(stat.S_IMODE(os.stat(self.store_file()).st_mode), 0o600)

	def test_missing_store_reads_as_none(self):
		self.assertIsNone(store.read_store(self.home))

	def test_invalid_stores_fail_without_values(self):
		cases = {
			'corrupt': '{"api_id": 7654321, "api_hash": "S3NTINEL0a1b2c3d4e5f60718293a4b5c", ',
			'newer version': {'schema_version': 2, **VALID},
			'extra key': {'schema_version': 1, **VALID, 'password': 'x'},
			'missing key': {'schema_version': 1, 'api_id': API_ID, 'api_hash': API_HASH},
			'bad api_id': {'schema_version': 1, **VALID, 'api_id': 'S3NTINEL'},
			'empty phone': {'schema_version': 1, **VALID, 'phone': ' '},
			'not an object': [VALID],
		}
		for label, data in cases.items():
			with self.subTest(case=label):
				self.write_raw(data)
				with self.assertRaises(store.StoreError) as ctx:
					store.read_store(self.home)
				self.assert_no_secrets(ctx.exception)
		self.assertIn('schema version', str(self._error({'schema_version': 2, **VALID})))

	def test_schema_version_errors_never_echo_stored_values(self):
		for version in (API_HASH, VALID, API_ID, True, 1.0):
			with self.subTest(value_type=type(version).__name__):
				error = self._error({'schema_version': version, **VALID})
				self.assertIsInstance(error, store.StoreError)
				self.assert_no_secrets(error)

	def _error(self, data):
		self.write_raw(data)
		try:
			store.read_store(self.home)
		except store.StoreError as exc:
			return exc

	def test_insecure_store_is_refused(self):
		self.write_raw({'schema_version': 1, **VALID}, private=False)
		with self.assertRaises(store.StoreError) as ctx:
			store.read_store(self.home)
		self.assertIn('accessible to other accounts', str(ctx.exception))
		self.assertIn('clear-credentials', str(ctx.exception))
		self.assert_no_secrets(ctx.exception)

	def test_failed_replace_keeps_the_earlier_file(self):
		store.save_store(self.home, VALID)
		with mock.patch.object(store.os, 'replace', side_effect=OSError(28, 'No space left')):
			with self.assertRaises(store.StoreError):
				store.save_store(self.home, dict(VALID, phone='+990000OTHER'))
		self.assertEqual(store.read_store(self.home), VALID)
		self.assertEqual(sorted(os.listdir(self.home)), ['credentials.json'])

	def test_unprotectable_staging_fails_closed_before_writing(self):
		sizes = []

		def failing_protect(path, directory=False):
			sizes.append(os.path.getsize(path))
			raise protection.ProtectionError(f'cannot restrict access to {path}')

		protection.ensure_private_dir(self.home)
		with mock.patch.object(store, 'protect', failing_protect):
			with self.assertRaises(store.StoreError):
				store.save_store(self.home, VALID)
		self.assertEqual(sizes, [0])
		self.assertEqual(os.listdir(self.home), [])

	def test_unwritable_home_fails(self):
		blocker = os.path.join(self.tmp, 'file')
		open(blocker, 'w').close()
		with self.assertRaises(store.StoreError):
			store.save_store(os.path.join(blocker, 'home'), VALID)

	def test_links_are_refused(self):
		target = os.path.join(self.tmp, 'elsewhere')
		os.mkdir(target)
		if os.name == 'nt':
			import _winapi

			_winapi.CreateJunction(target, self.home)
		else:
			os.symlink(target, self.home)
		try:
			with self.assertRaises(store.StoreError):
				store.save_store(self.home, VALID)
			self.assertEqual(os.listdir(target), [])
		finally:
			os.rmdir(self.home) if os.name == 'nt' else os.remove(self.home)

	def test_a_held_lease_blocks_saving_and_clearing(self):
		store.save_store(self.home, VALID)
		with Lease(self.store_file(), 'The credentials file'):
			with self.assertRaises(store.StoreError) as ctx:
				store.save_store(self.home, dict(VALID, phone='+990000OTHER'))
			self.assertIn('in use by another process', str(ctx.exception))
			with self.assertRaises(store.StoreError):
				store.clear_store(self.home)
		self.assertEqual(store.read_store(self.home), VALID)
		self.assertFalse(os.path.exists(self.store_file() + '.lock'))

	def test_clear_removes_only_the_credentials_file_without_reading_it(self):
		self.write_raw('not json at all')
		other = os.path.join(self.home, 'session_file.session-sentinel')
		open(other, 'w').close()
		with mock.patch.object(store.json, 'load', side_effect=AssertionError('parsed')):
			self.assertTrue(store.clear_store(self.home))
		self.assertEqual(os.listdir(self.home), ['session_file.session-sentinel'])
		self.assertFalse(store.clear_store(self.home))

	def test_save_validates_the_complete_set(self):
		for values in ({'api_id': API_ID, 'api_hash': API_HASH}, dict(VALID, api_id=0)):
			with self.assertRaises(store.StoreError) as ctx:
				store.save_store(self.home, values)
			self.assert_no_secrets(ctx.exception)
		self.assertFalse(os.path.exists(self.store_file()))


class HomeTests(offline_guard.OfflineTestCase):

	def test_default_home_is_in_the_user_profile(self):
		path, label = home.app_home({})
		self.assertEqual(path, os.path.join(os.path.expanduser('~'), '.telegram_tracker'))
		self.assertEqual(label, 'default')

	def test_override_must_be_absolute(self):
		with tempfile.TemporaryDirectory() as tmp:
			self.assertEqual(home.app_home({home.HOME_ENV: tmp}), (os.path.normpath(tmp), home.HOME_ENV))
		for value in ('', '   ', 'relative/dir'):
			with self.subTest(value=value):
				with self.assertRaises(home.HomeError):
					home.app_home({home.HOME_ENV: value})


class SessionSelectionTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.cwd = os.path.join(os.path.realpath(tmp.name), 'work')
		self.home = os.path.join(os.path.realpath(tmp.name), 'home')
		self.env = {home.HOME_ENV: self.home}
		self.home_session = os.path.join(self.home, 'session_file.session')
		self.legacy = os.path.join(self.cwd, 'session_file.session')

	def select(self, *existing, flag=None, env=None):
		present = {os.path.normcase(p) for p in existing}
		return home.select_session(
			flag, environ=env or self.env, cwd=self.cwd,
			isfile=lambda p: os.path.normcase(os.path.abspath(p)) in present
		)

	def test_precedence(self):
		env = dict(self.env, **{home.SESSION_ENV: 'env-session'})
		choice = self.select(self.home_session, self.legacy, flag='flag', env=env)
		self.assertEqual((choice.path, choice.source), (os.path.join(self.cwd, 'flag.session'), '--session'))
		choice = self.select(self.home_session, self.legacy, env=env)
		self.assertEqual((choice.path, choice.source), (os.path.join(self.cwd, 'env-session.session'), home.SESSION_ENV))
		choice = self.select(self.home_session, self.legacy)
		self.assertEqual((choice.path, choice.source, choice.notice), (self.home_session, home.HOME, None))

	def test_legacy_session_is_used_with_a_notice_only_without_a_home_session(self):
		choice = self.select(self.legacy)
		self.assertEqual((choice.path, choice.source), (self.legacy, home.WORKING_DIRECTORY))
		self.assertIn('working directory', choice.notice)
		self.assertIn('--session', choice.notice)

	def test_first_use_defaults_to_the_home(self):
		choice = self.select()
		self.assertEqual((choice.path, choice.source, choice.in_home), (self.home_session, home.HOME, True))

	def test_paths_are_absolute_with_one_session_suffix(self):
		for given, expected in (
				('a', 'a.session'), ('a.session', 'a.session'), ('A.SESSION', 'A.SESSION.session'),
				(os.path.join('sub', '..', 'b'), 'b.session')):
			with self.subTest(given=given):
				self.assertEqual(home.session_file(given, self.cwd), os.path.join(self.cwd, expected))
		with self.assertRaises(home.HomeError):
			home.session_file('  ', self.cwd)

	def test_selected_path_matches_the_telethon_database_path(self):
		from telethon.sessions import SQLiteSession

		for name in ('a', 'a.session', 'a.SESSION', 'a.Session'):
			with self.subTest(name=name):
				selected = home.session_file(name, self.cwd)
				with mock.patch('sqlite3.connect', side_effect=RuntimeError('synthetic open blocked')) as connect:
					with self.assertRaisesRegex(RuntimeError, 'synthetic open blocked'):
						SQLiteSession(selected)
				self.assertEqual(connect.call_count, 1)
				self.assertEqual(connect.call_args.args[0], selected)

	def test_uppercase_suffix_uses_the_actual_database_lease(self):
		self.assertEqual(
			home.session_file('a.SESSION', self.cwd),
			home.session_file('a.SESSION.session', self.cwd)
		)

	def test_link_targets_without_a_telethon_suffix_are_refused(self):
		for target in ('actual.sqlite', 'actual.SESSION'):
			with self.subTest(target=target):
				with mock.patch.object(home.os.path, 'realpath', return_value=os.path.join(self.cwd, target)):
					with self.assertRaises(home.HomeError):
						home.session_file('alias.session', self.cwd)
					with self.assertRaises(home.HomeError):
						self.select(flag='alias.session')
					with self.assertRaises(home.HomeError):
						self.select(env={**self.env, home.SESSION_ENV: 'alias.session'})
					with self.assertRaises(home.HomeError):
						self.select(self.home_session)

	def test_paths_through_links_are_canonical(self):
		os.makedirs(self.home)
		alias = os.path.join(os.path.dirname(self.home), 'alias')
		if os.name == 'nt':
			import _winapi

			_winapi.CreateJunction(self.home, alias)
		else:
			os.symlink(self.home, alias)
		try:
			self.assertEqual(
				home.session_file(os.path.join(alias, 'session_file'), self.cwd),
				self.home_session
			)
			self.assertTrue(home.is_within(os.path.join(alias, 'x'), self.home))
		finally:
			os.rmdir(alias) if os.name == 'nt' else os.remove(alias)

	def test_is_within(self):
		self.assertTrue(home.is_within(os.path.join(self.cwd, 'out', 'x'), os.path.join(self.cwd, 'out')))
		self.assertFalse(home.is_within(os.path.join(self.cwd, 'outside'), os.path.join(self.cwd, 'out')))


class SourceTests(SecretsMixin, offline_guard.OfflineTestCase):
	'''
	Precedence and whole-set file sources, with recording fake readers.
	'''

	def setUp(self):
		super().setUp()
		self.cwd = os.path.join(tempfile.gettempdir(), 'tgtracker-sources-cwd')
		self.reads = []
		self.files = set()
		self.ini = {}
		self.stored = None

	def ini_reader(self, path, label):
		self.reads.append(label)
		return self.ini[label]

	def store_reader(self, home_path):
		self.reads.append('store')
		if isinstance(self.stored, Exception):
			raise self.stored
		return self.stored

	def resolve(self, env=None, config=None):
		return sources.resolve(
			config=config, environ=env or {}, home='H', cwd=self.cwd,
			ini_reader=self.ini_reader, store_reader=self.store_reader,
			isfile=lambda p: os.path.normcase(p) in {os.path.normcase(f) for f in self.files}
		)

	def env(self, **fields):
		return {sources.ENV_VARS[k]: str(v) for k, v in fields.items()}

	def legacy_present(self, values):
		self.files.add(os.path.join(self.cwd, 'config', 'config.ini'))
		self.ini[sources.LEGACY_CONFIG] = values

	def test_complete_environment_reads_no_file(self):
		self.stored = VALID
		self.legacy_present(VALID)
		result = self.resolve(self.env(**VALID))
		self.assertEqual(result.values, VALID)
		self.assertEqual(set(result.sources.values()), {sources.ENVIRONMENT})
		self.assertEqual(self.reads, [])

	def test_environment_overrides_single_fields_of_the_file_source(self):
		self.stored = dict(VALID, phone='+990000STORED')
		result = self.resolve(self.env(phone=PHONE))
		self.assertEqual(result.values, VALID)
		self.assertEqual(result.sources, {'api_id': 'credentials.json', 'api_hash': 'credentials.json', 'phone': 'environment'})

	def test_store_wins_over_the_placeholder_repository_ini(self):
		self.stored = VALID
		self.legacy_present({'api_id': 'api_id', 'api_hash': 'api_hash', 'phone': 'phone'})
		result = self.resolve()
		self.assertEqual((result.values, result.file_source), (VALID, 'credentials.json'))
		self.assertEqual(self.reads, ['store'])

	def test_explicit_config_is_used_whole_and_never_filled_from_the_store(self):
		config = os.path.join(self.cwd, 'creds.ini')
		self.files.add(config)
		self.ini[sources.CONFIG] = {'api_id': str(API_ID), 'api_hash': '<your api_hash>', 'phone': PHONE}
		self.stored = VALID
		result = self.resolve(config='creds.ini')
		self.assertEqual(result.file_source, '--config')
		self.assertEqual(result.missing, ['api_hash'])
		self.assertNotIn('store', self.reads)

	def test_missing_explicit_config_is_an_error(self):
		with self.assertRaises(sources.CredentialError) as ctx:
			self.resolve(config='nowhere.ini')
		self.assertIn('--config', str(ctx.exception))

	def test_legacy_ini_is_used_when_there_is_no_store(self):
		self.legacy_present({'api_id': str(API_ID), 'api_hash': API_HASH, 'phone': PHONE})
		result = self.resolve()
		self.assertEqual((result.values, result.file_source), (VALID, './config/config.ini'))

	def test_placeholder_only_legacy_ini_counts_as_absent(self):
		for values in (
				{'api_id': 'api_id', 'api_hash': 'api_hash', 'phone': 'phone'},
				{'api_id': '<your api_id>', 'api_hash': '<your api_hash>', 'phone': ''},
				{'api_id': None, 'api_hash': None, 'phone': None}):
			with self.subTest(values=values):
				self.legacy_present(values)
				result = self.resolve()
				self.assertEqual((result.values, result.file_source), ({}, None))
				self.assertIn('placeholder', result.notes[0])

	def test_partial_legacy_ini_leaves_the_rest_missing(self):
		self.legacy_present({'api_id': str(API_ID), 'api_hash': 'api_hash', 'phone': PHONE})
		result = self.resolve()
		self.assertEqual(result.missing, ['api_hash'])
		self.assertEqual(result.labels(), ['api_id: ./config/config.ini', 'api_hash: missing', 'phone: ./config/config.ini'])

	def test_invalid_values_fail_without_falling_back(self):
		self.stored = VALID
		for env in (self.env(api_id='S3NTINEL'), self.env(api_id='-5'), self.env(api_id='0')):
			with self.subTest(env=env):
				with self.assertRaises(sources.CredentialError) as ctx:
					self.resolve(env)
				self.assertIn('TELEGRAM_TRACKER_API_ID', str(ctx.exception))
				self.assertNotIn('S3NTINEL', str(ctx.exception))

	def test_invalid_store_is_an_error(self):
		self.stored = store.StoreError('H/credentials.json cannot be read (JSONDecodeError)')
		with self.assertRaises(sources.CredentialError):
			self.resolve()

	def test_nothing_anywhere_is_incomplete(self):
		result = self.resolve()
		self.assertEqual(result.missing, ['api_id', 'api_hash', 'phone'])

	def test_placeholder_detection(self):
		for field, value, expected in (
				('api_id', 'api_id', True), ('api_id', 'API_ID', True), ('api_hash', '<x>', True),
				('phone', '  ', True), ('phone', None, True), ('phone', '+1', False),
				('api_id', '123', False), ('api_hash', 'api_id', False)):
			with self.subTest(field=field, value=value):
				self.assertIs(sources.is_placeholder(field, value), expected)


class IniReaderTests(SecretsMixin, offline_guard.OfflineTestCase):
	'''
	The real INI reader on a temporary file that is not named config.ini.
	'''

	def write(self, text):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		path = os.path.join(tmp.name, 'creds.ini')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write(text)
		return path

	def test_reads_the_credentials_section(self):
		path = self.write(f'[Telegram API credentials]\napi_id = {API_ID}\napi_hash = {API_HASH}%\nphone = {PHONE}\n')
		self.assertEqual(
			sources.read_ini(path, '--config'),
			{'api_id': str(API_ID), 'api_hash': API_HASH + '%', 'phone': PHONE}
		)

	def test_errors_name_the_source_only(self):
		for text in ('[Other]\napi_id = 1\n', f'api_hash = {API_HASH}\n'):
			with self.subTest(text=text):
				with self.assertRaises(sources.CredentialError) as ctx:
					sources.read_ini(self.write(text), '--config')
				self.assertIn('--config', str(ctx.exception))
				self.assert_no_secrets(ctx.exception)


if __name__ == '__main__':
	unittest.main()
