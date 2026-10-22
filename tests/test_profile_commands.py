# -*- coding: utf-8 -*-
'''
Named profiles through the CLI, with a fake client and a temporary home:
`auth login --profile` creates and binds a profile, the other actions
refuse unknown profiles, a named profile never uses the default's files,
--session, TELEGRAM_TRACKER_SESSION, or ./config/config.ini, logout and
clear-credentials touch only the selected profile, `auth profiles` lists
without creating anything, and `collect` checks the account (one get_me)
and holds the account lease before any channel request. The synthetic
account ID and credentials never appear outside credentials.json and
profile.json.
'''

# import modules
import json
import os
import subprocess
import sys
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)
import virtual_time

from auth_support import API_HASH, API_ID, OTHER, PHONE, VALID, CredentialCase, env_values
from fakes import FIXTURE_ACCOUNT, FIXTURE_CODE, FakeTelegramClient

from telegram_tracker.cli import collect as cli_collect
from telegram_tracker.credentials import accounts, identity, profiles, sources, store
from telegram_tracker.credentials.lease import Lease


OTHER_ACCOUNT = 777000222
PRIVATE_FILES = (store.STORE_NAME, identity.IDENTITY_NAME)


class ProfileCase(CredentialCase):

	def setUp(self):
		super().setUp()
		self.patch(cli_collect, 'asyncio_run', virtual_time.run)
		self.out = os.path.join(self.tmp, 'out')

	def profile(self, name='alice'):
		return profiles.Profile(name, profiles.FLAG, self.home)

	def session_of(self, name='alice'):
		return os.path.join(self.home, 'profiles', name, 'session_file.session')

	def make_profile(self, name='alice', account=FIXTURE_ACCOUNT, values=VALID):
		profile = self.profile(name)
		profiles.prepare_root(profile)
		created = identity.ensure_identity(profile.root, name, account)
		if values is not None:
			store.save_store(profile.root, values)
		return created

	def record(self, name='alice'):
		profile = self.profile(name)
		return identity.read_identity(profile.root, name)

	def type_credentials(self, values=VALID):
		self.text_prompt.answers += [str(values['api_id']), values['phone']]
		self.secret_prompt.answers += [values['api_hash']]

	def assert_private_values_stay_private(self):
		'''
		No synthetic secret or account ID in any output or in any file
		other than credentials.json and profile.json.
		'''
		self.assert_no_secrets()
		texts = list(self.outputs)
		for root, _, files in os.walk(self.tmp):
			for name in files:
				if name not in PRIVATE_FILES:
					with open(os.path.join(root, name), encoding='utf-8', errors='replace') as fh:
						texts.append(fh.read())
		for text in texts:
			for value in (str(FIXTURE_ACCOUNT), str(OTHER_ACCOUNT)):
				self.assertNotIn(value, text)


class LoginTests(ProfileCase):

	def test_login_creates_and_binds_a_named_profile(self):
		self.client = FakeTelegramClient(authorized=False)
		self.type_credentials()
		self.code_prompt.answers.append(FIXTURE_CODE)
		code, out, err = self.run_main(['auth', 'login', '--profile', 'alice'])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.created, [(self.session_of(), API_ID, API_HASH)])
		self.assertEqual(self.client.call_names().count('get_me'), 1)
		record = self.record()
		self.assertEqual(record.account_id, FIXTURE_ACCOUNT)
		self.assertEqual(store.read_store(self.profile().root), VALID)
		self.assertIsNone(store.read_store(self.home))
		self.assertFalse(os.path.exists(os.path.join(self.home, identity.IDENTITY_NAME)))
		self.assertIn('Profile: alice (--profile)', out)
		self.assertIn('Profile record:', out)
		self.assert_private_values_stay_private()

	def test_named_profile_uses_none_of_the_default_sources(self):
		self.save(OTHER)
		legacy_ini = os.path.join(self.cwd, sources.LEGACY_INI)
		real_isfile = os.path.isfile
		self.patch(sources, 'read_ini', mock.Mock(side_effect=AssertionError('INI read')))
		self.pretend_files(os.path.join(self.cwd, 'session_file.session'), self.home_session)
		self.type_credentials()
		with mock.patch.object(sources.os.path, 'isfile', lambda p: p == legacy_ini or real_isfile(p)):
			code, out, _ = self.run_main(['auth', 'login', '--profile', 'alice'])
		self.assertEqual(code, 0)
		self.assertIn('api_id: prompt; api_hash: prompt; phone: prompt', out)
		self.assertEqual(self.created[0][0], self.session_of())
		self.assertEqual(store.read_store(self.home), OTHER)

	def test_session_flag_and_variable_are_refused_for_a_named_profile(self):
		for extra, environ in ((['--session', 'mine'], {}), ([], {'TELEGRAM_TRACKER_SESSION': 'mine'})):
			with self.subTest(extra=extra, environ=environ):
				with mock.patch.dict(os.environ, environ):
					code, _, err = self.run_main(['auth', 'login', '--profile', 'alice', *extra])
				self.assertEqual(code, 2)
				self.assertIn('always uses its own session', err)
		self.assertEqual(self.created, [])
		self.assertFalse(os.path.exists(self.home))

	def test_invalid_names_exit_2_before_any_path(self):
		for name in ('Alice', '../x', 'con', 'a' * 33):
			with self.subTest(name=name):
				code, _, err = self.run_main(['auth', 'login', '--profile', name])
				self.assertEqual(code, 2)
				self.assertIn('--profile', err)
		self.assertFalse(os.path.exists(self.home))

	def test_failed_login_leaves_no_usable_profile(self):
		self.client = FakeTelegramClient(authorized=False)
		self.type_credentials()
		self.code_prompt.answers += ['00000', '00001', '00002']
		self.assertEqual(self.run_main(['auth', 'login', '--profile', 'alice'])[0], 1)
		self.assertIsNone(self.record())
		self.assertIsNone(store.read_store(self.profile().root))
		code, _, err = self.run_main(['auth', 'status', '--profile', 'alice'])
		self.assertEqual(code, 2)
		self.assertIn('there is no profile alice', err)

	def test_session_of_another_account_is_refused(self):
		self.make_profile()
		before = self.record()
		self.client = FakeTelegramClient(account_id=OTHER_ACCOUNT)
		os.environ.update(env_values(OTHER))
		code, _, err = self.run_main(['auth', 'login', '--profile', 'alice', '--save'])
		self.assertEqual(code, 1)
		self.assertIn('different Telegram account', err)
		self.assertIn('auth logout --profile alice', err)
		self.assertEqual(self.record(), before)
		self.assertEqual(store.read_store(self.profile().root), VALID)
		self.assert_private_values_stay_private()

	def test_session_of_another_phone_number_is_refused(self):
		self.save()
		# the synthetic PHONE's digits are 9900003
		for phone, expected in (('991111', 1), ('9900003', 0)):
			with self.subTest(phone=phone):
				self.client = FakeTelegramClient(account_phone=phone)
				code, _, err = self.run_main(['auth', 'login'])
				self.assertEqual(code, expected, err)
				if expected:
					self.assertIn('different account than the phone number', err)
					self.assertFalse(os.path.exists(os.path.join(self.home, identity.IDENTITY_NAME)))

	def test_default_login_records_the_root_profile(self):
		self.save()
		code, out, _ = self.run_main(['auth', 'login'])
		self.assertEqual(code, 0)
		root = identity.read_identity(self.home, 'default')
		self.assertEqual(root.account_id, FIXTURE_ACCOUNT)
		self.assertEqual(self.created[0][0], self.home_session)

	def test_default_login_with_an_explicit_session_binds_no_account(self):
		self.save()
		code, out, _ = self.run_main(['auth', 'login', '--session', 'mine'])
		self.assertEqual(code, 0)
		self.assertIsNone(identity.read_identity(self.home, 'default').account_id)
		self.assertIn('account not bound', out)

	def test_identity_survives_clear_credentials_and_a_new_login(self):
		self.type_credentials()
		self.assertEqual(self.run_main(['auth', 'login', '--profile', 'alice'])[0], 0)
		first = self.record()
		self.assertEqual(self.run_main(['auth', 'clear-credentials', '--profile', 'alice', '--yes'])[0], 0)
		self.assertIsNone(store.read_store(self.profile().root))
		self.assertEqual(self.record(), first)
		self.type_credentials()
		self.assertEqual(self.run_main(['auth', 'login', '--profile', 'alice'])[0], 0)
		self.assertEqual(self.record(), first)

	def test_profile_from_the_environment(self):
		self.make_profile()
		os.environ[profiles.PROFILE_ENV] = 'alice'
		code, out, _ = self.run_main(['auth', 'status'])
		self.assertEqual(code, 0)
		self.assertIn(f'Profile: alice ({profiles.PROFILE_ENV})', out)
		self.assertIn(self.session_of(), out)


class OtherActionTests(ProfileCase):

	def test_unknown_profile_is_refused_and_nothing_created(self):
		for action in (['status'], ['logout', '--yes'], ['clear-credentials', '--yes']):
			with self.subTest(action=action):
				code, _, err = self.run_main(['auth', *action, '--profile', 'alice'])
				self.assertEqual(code, 2)
				self.assertIn('auth login --profile alice', err)
		self.assertFalse(os.path.exists(self.home))
		self.assertEqual(self.created, [])

	def test_status_reports_the_profile_by_labels(self):
		self.make_profile()
		code, out, _ = self.run_main(['auth', 'status', '--profile', 'alice'])
		self.assertEqual(code, 0)
		self.assertIn(f'Profile record: {self.profile().identity_path}: present', out)
		self.assertIn(f'Session: {self.session_of()} (profile): missing', out)
		self.assertIn('api_id: credentials.json', out)
		self.assert_private_values_stay_private()

	def test_profiles_listing_creates_nothing(self):
		code, out, _ = self.run_main(['auth', 'profiles'])
		self.assertEqual(code, 0)
		self.assertIn('default (the application home itself): profile record absent', out)
		self.assertFalse(os.path.exists(self.home))

		self.make_profile()
		self.make_profile('bob', OTHER_ACCOUNT, values=None)
		os.makedirs(os.path.join(self.home, 'profiles', 'Carol'))
		before = sorted(os.walk(self.home))
		code, out, _ = self.run_main(['auth', 'profiles'])
		self.assertEqual(code, 0)
		lines = out.splitlines()
		self.assertEqual(lines[2], 'alice: profile record present; credentials present; session missing')
		self.assertEqual(lines[3], 'bob: profile record present; credentials absent; session missing')
		self.assertIn('1 folder(s)', lines[4])
		self.assertEqual(sorted(os.walk(self.home)), before)
		self.assert_private_values_stay_private()

	def test_logout_uses_only_the_profiles_session(self):
		self.make_profile()
		self.pretend_files(self.session_of(), self.home_session)
		code, out, _ = self.run_main(['auth', 'logout', '--profile', 'alice', '--yes'])
		self.assertEqual(code, 0)
		self.assertEqual(self.created, [(self.session_of(), API_ID, API_HASH)])
		self.assertIn('log_out', self.client.call_names())
		self.assertEqual(store.read_store(self.profile().root), VALID)
		self.assertIsNotNone(self.record())

	def test_clear_credentials_removes_only_the_profiles_store(self):
		self.save(OTHER)
		self.make_profile()
		code, out, _ = self.run_main(['auth', 'clear-credentials', '--profile', 'alice', '--yes'])
		self.assertEqual(code, 0)
		self.assertIsNone(store.read_store(self.profile().root))
		self.assertEqual(store.read_store(self.home), OTHER)
		self.assertIsNotNone(self.record())
		self.assertIn('profile record', out)
		self.assertNotIn('config.ini', out)

	def test_clear_credentials_waits_for_the_profiles_session(self):
		self.make_profile()
		with Lease(self.session_of(), 'The session'):
			code, _, err = self.run_main(['auth', 'clear-credentials', '--profile', 'alice', '--yes'])
		self.assertEqual(code, 1)
		self.assertIn('in use by another process', err)
		self.assertEqual(store.read_store(self.profile().root), VALID)


class CollectTests(ProfileCase):

	def collect(self, *extra, profile='alice', legacy=False):
		argv = ['--telegram-channel', 'alpha_fx', '-o', self.out, *extra]
		if profile:
			argv += ['--profile', profile]
		return self.run_main(argv if legacy else ['collect', *argv])

	def status(self):
		with open(os.path.join(self.out, 'collection-status.json'), encoding='utf-8') as fh:
			return json.load(fh)

	def channel_calls(self):
		return [n for n in self.client.call_names() if n in ('get_entity', 'GetFullChannelRequest', 'GetHistoryRequest')]

	def test_named_profile_collects_with_one_account_check(self):
		created = self.make_profile()
		code, out, err = self.collect()
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.created, [(self.session_of(), API_ID, API_HASH)])
		names = self.client.call_names()
		self.assertEqual(names.count('get_me'), 1)
		self.assertLess(names.index('get_me'), names.index('get_entity'))
		status = self.status()
		self.assertEqual((status['profile_name'], status['profile_id']), ('alice', created.profile_id))
		self.assertEqual(status['account_checks'], {'get_me': 1})
		self.assertNotIn('get_me', status['requests'])
		with open(os.path.join(self.out, 'alpha_fx', 'alpha_fx_provenance.json'), encoding='utf-8') as fh:
			self.assertEqual(json.load(fh)['profile_id'], created.profile_id)
		self.assertIn('> Profile: alice; account checked with 1 request (get_me).', out)
		self.assertEqual(os.listdir(accounts.leases_dir(self.home)), [])
		self.assert_private_values_stay_private()

	def test_unknown_profile_and_session_options_exit_2_before_a_client(self):
		code, _, err = self.collect()
		self.assertEqual(code, 2)
		self.assertIn('there is no profile alice', err)
		self.make_profile()
		code, _, err = self.collect('--session', 'mine')
		self.assertEqual(code, 2)
		self.assertIn('always uses its own session', err)
		self.assertEqual(self.created, [])
		self.assertFalse(os.path.exists(self.out))

	def test_session_of_another_account_stops_before_channel_requests(self):
		self.make_profile()
		self.client = FakeTelegramClient(account_id=OTHER_ACCOUNT)
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('different Telegram account', err)
		self.assertEqual(self.channel_calls(), [])
		self.assertFalse(os.path.exists(self.out))
		self.assertEqual(self.record().account_id, FIXTURE_ACCOUNT)
		self.assertEqual(self.client.disconnect_count, 1)
		self.assert_private_values_stay_private()

	def test_account_in_use_by_another_profile_is_refused(self):
		self.make_profile()
		accounts.prepare(self.home)
		with accounts.lease_for(self.home, FIXTURE_ACCOUNT, 'bob'):
			code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('The Telegram account is in use by another process', err)
		self.assertIn('profile bob', err)
		self.assertEqual(self.channel_calls(), [])
		self.assertFalse(os.path.exists(self.out))

	def test_interrupted_run_releases_its_leases(self):
		self.make_profile()
		self.client.script('get_entity', KeyboardInterrupt())
		code, _, err = self.collect()
		self.assertEqual(code, 130)
		self.assertEqual(os.listdir(accounts.leases_dir(self.home)), [])
		self.assertFalse(os.path.exists(self.session_of() + '.lock'))
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertEqual(self.collect()[0], 0)

	def test_missing_credentials_name_only_the_profiles_sources(self):
		self.make_profile(values=None)
		code, _, err = self.collect()
		self.assertEqual(code, 2)
		self.assertIn('auth login --profile alice', err)
		self.assertNotIn('config.ini', err)
		self.assertEqual(self.created, [])

	def test_other_accounts_collect_at_the_same_time(self):
		self.make_profile()
		accounts.prepare(self.home)
		with accounts.lease_for(self.home, OTHER_ACCOUNT, 'bob'):
			code, _, err = self.collect()
		self.assertEqual((code, err), (0, ''))

	def test_account_lease_held_by_another_process(self):
		self.make_profile()
		accounts.prepare(self.home)
		code = (
			'import sys; sys.path.insert(0, sys.argv[1]); '
			'from telegram_tracker.credentials import accounts; '
			'lease = accounts.lease_for(sys.argv[2], int(sys.argv[3]), "bob"); '
			'import os; lease.__enter__(); print("held", os.getpid(), flush=True); '
			'sys.stdin.readline(); lease.__exit__(None, None, None)'
		)
		src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
		holder = subprocess.Popen(
			[sys.executable, '-I', '-c', code, src, self.home, str(FIXTURE_ACCOUNT)],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
		)
		try:
			# the holding interpreter's own pid: Popen may have started a launcher
			word, owner_pid = holder.stdout.readline().split()
			self.assertEqual(word, 'held')
			code, _, err = self.collect()
			self.assertEqual(code, 1)
			self.assertIn(f'pid {owner_pid},', err)
			self.assertNotEqual(int(owner_pid), os.getpid())
			self.assertEqual(self.channel_calls(), [])
		finally:
			holder.communicate('\n', timeout=60)
		self.assertEqual(self.collect()[0], 0)

	def test_legacy_form_takes_the_profile_from_the_environment(self):
		created = self.make_profile()
		os.environ[profiles.PROFILE_ENV] = 'alice'
		code, _, err = self.collect(profile=None, legacy=True)
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.created[0][0], self.session_of())
		self.assertEqual(self.status()['profile_id'], created.profile_id)

	def test_default_profile_runs_without_a_record(self):
		os.environ.update(env_values(VALID))
		code, out, err = self.collect(profile=None)
		self.assertEqual((code, err), (0, ''))
		self.assertEqual((self.status()['profile_name'], self.status()['profile_id']), ('default', None))
		self.assertIn('> Profile: default (no profile ID)', out)
		self.assertFalse(os.path.exists(os.path.join(self.home, identity.IDENTITY_NAME)))

	def test_default_explicit_session_of_another_account_runs_without_the_id(self):
		self.save()
		identity.ensure_identity(self.home, 'default', OTHER_ACCOUNT)
		code, _, err = self.collect('--session', 'mine', profile=None)
		self.assertEqual(code, 0)
		self.assertIn('recorded without its profile ID', err)
		self.assertIsNone(self.status()['profile_id'])
		self.assertEqual(identity.read_identity(self.home, 'default').account_id, OTHER_ACCOUNT)

	def test_default_own_session_of_another_account_is_refused(self):
		self.save()
		identity.ensure_identity(self.home, 'default', OTHER_ACCOUNT)
		code, _, err = self.collect(profile=None)
		self.assertEqual(code, 1)
		self.assertIn('auth logout`', err)
		self.assertEqual(self.channel_calls(), [])

	def test_unusable_home_stops_before_a_client(self):
		blocker = os.path.join(self.tmp, 'file')
		open(blocker, 'w').close()
		os.environ.update(env_values(VALID))
		os.environ['TELEGRAM_TRACKER_HOME'] = os.path.join(blocker, 'home')
		code, _, err = self.collect('--session', os.path.join(self.tmp, 's'), profile=None)
		self.assertEqual(code, 1)
		self.assertIn('writable application home', err)
		self.assertEqual(self.created, [])

	def test_update_into_another_profiles_folder(self):
		self.make_profile()
		self.make_profile('bob', OTHER_ACCOUNT)
		self.assertEqual(self.collect()[0], 0)
		self.client = FakeTelegramClient(account_id=OTHER_ACCOUNT)
		code, _, err = self.collect('--update', profile='bob')
		self.assertEqual(code, 1)
		self.assertIn('belongs to profile alice', err)
		self.client = FakeTelegramClient(account_id=OTHER_ACCOUNT)
		code, out, _ = self.collect(profile='bob')
		self.assertEqual(code, 0)
		self.assertIn('replaced a history of another profile', out)
		self.assertIn('alpha_fx (was alice)', out)
		self.assert_private_values_stay_private()


if __name__ == '__main__':
	unittest.main()
