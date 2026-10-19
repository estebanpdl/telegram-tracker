# -*- coding: utf-8 -*-
'''
Profiles, profile records, and account leases, below the CLI: name rules
checked before any path, selection precedence, the default profile mapped
onto the application home, named folders refused when they are links or
differ in case, profile.json creation/binding/refusal, and the account
lease and binding decision. Temporary homes only.
'''

# import modules
import json
import os
import tempfile
import unittest
import uuid

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient

from telegram_tracker.credentials import accounts, home as homes, identity, profiles, protection
from telegram_tracker.credentials.lease import Lease, LeaseHeld


ACCOUNT = 777000111
OTHER_ACCOUNT = 777000222


class TempHomeCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.home = os.path.join(self.tmp, 'home')

	def profile(self, name='alice'):
		source = profiles.DEFAULT_SOURCE if name == profiles.DEFAULT else profiles.FLAG
		return profiles.Profile(name, source, self.home)

	def link(self, target, link):
		if os.name == 'nt':
			import _winapi

			_winapi.CreateJunction(target, link)
		else:
			os.symlink(target, link)
		self.addCleanup(lambda: os.rmdir(link) if os.name == 'nt' else os.remove(link))


class NameTests(offline_guard.OfflineTestCase):

	def test_valid_names(self):
		for name in ('a', 'alice', 'team-2', 'x_y', 'a' * 32, 'default', 'con1', 'comx'):
			with self.subTest(name=name):
				self.assertEqual(profiles.check_name(name), name)

	def test_invalid_names_are_refused_without_echo(self):
		for name in (
				'', 'Alice', 'ALICE', '1team', '-a', '_a', 'a' * 33, 'a.b', 'alice.',
				'a/b', 'a\\b', '..', '../x', 'a b', 'ä', 'con', 'prn', 'aux', 'nul',
				'com1', 'com9', 'lpt1', 'lpt9', None):
			with self.subTest(name=name):
				with self.assertRaises(profiles.ProfileError) as caught:
					profiles.check_name(name)
				if name and name not in profiles.DEVICE_NAMES:
					self.assertNotIn(name, str(caught.exception))


class SelectionTests(TempHomeCase):

	def test_flag_then_environment_then_default(self):
		env = {profiles.PROFILE_ENV: 'bob'}
		self.assertEqual(profiles.select_profile('alice', env, self.home).name, 'alice')
		self.assertEqual(profiles.select_profile('alice', env, self.home).source, profiles.FLAG)
		chosen = profiles.select_profile(None, env, self.home)
		self.assertEqual((chosen.name, chosen.source), ('bob', profiles.PROFILE_ENV))
		for environ in ({}, {profiles.PROFILE_ENV: ''}, {profiles.PROFILE_ENV: '  '}):
			with self.subTest(environ=environ):
				chosen = profiles.select_profile(None, environ, self.home)
				self.assertEqual((chosen.name, chosen.is_default), (profiles.DEFAULT, True))

	def test_invalid_environment_name_names_the_variable(self):
		with self.assertRaises(profiles.ProfileError) as caught:
			profiles.select_profile(None, {profiles.PROFILE_ENV: 'Bad/Name'}, self.home)
		self.assertIn(profiles.PROFILE_ENV, str(caught.exception))

	def test_selection_touches_no_path(self):
		with mock.patch('os.stat', side_effect=AssertionError('stat')), \
				mock.patch('os.listdir', side_effect=AssertionError('listdir')):
			profiles.select_profile('alice', {}, self.home)
		self.assertFalse(os.path.exists(self.home))

	def test_default_maps_onto_the_home_and_named_onto_its_folder(self):
		default, alice = self.profile(profiles.DEFAULT), self.profile()
		self.assertEqual(default.root, self.home)
		self.assertEqual(default.store_path, os.path.join(self.home, 'credentials.json'))
		self.assertEqual(default.identity_path, os.path.join(self.home, 'profile.json'))
		folder = os.path.join(self.home, 'profiles', 'alice')
		self.assertEqual(alice.root, folder)
		self.assertEqual(alice.store_path, os.path.join(folder, 'credentials.json'))

	def test_explicit_default_is_the_default_profile(self):
		chosen = profiles.select_profile('default', {}, self.home)
		self.assertTrue(chosen.is_default)
		self.assertEqual(chosen.root, self.home)


class FolderTests(TempHomeCase):

	def test_unknown_named_profile_does_not_exist(self):
		self.assertTrue(profiles.exists(self.profile(profiles.DEFAULT)))
		self.assertFalse(profiles.exists(self.profile()))
		with self.assertRaises(profiles.ProfileError) as caught:
			profiles.require(self.profile())
		self.assertIn('auth login --profile alice', str(caught.exception))
		self.assertFalse(os.path.exists(self.home))

	def test_folder_without_record_is_not_a_profile(self):
		profiles.prepare_root(self.profile())
		self.assertFalse(profiles.exists(self.profile()))

	def test_prepare_root_makes_every_level_private(self):
		alice = self.profile()
		profiles.prepare_root(alice)
		for path in (self.home, os.path.join(self.home, 'profiles'), alice.root):
			with self.subTest(path=path):
				protection.check(path)

	def test_case_variant_folder_is_refused(self):
		os.makedirs(os.path.join(self.home, 'profiles', 'Alice'))
		with self.assertRaises(profiles.ProfileError) as caught:
			profiles.exists(self.profile())
		self.assertIn('only in case', str(caught.exception))

	def test_linked_profile_folder_is_refused(self):
		elsewhere = os.path.join(self.tmp, 'elsewhere')
		os.makedirs(elsewhere)
		os.makedirs(os.path.join(self.home, 'profiles'))
		self.link(elsewhere, os.path.join(self.home, 'profiles', 'alice'))
		for call in (profiles.exists, profiles.prepare_root, profiles.own_session):
			with self.subTest(call=call.__name__):
				with self.assertRaises(profiles.ProfileError):
					call(self.profile())

	def test_linked_profiles_folder_is_refused(self):
		elsewhere = os.path.join(self.tmp, 'elsewhere')
		os.makedirs(os.path.join(elsewhere, 'alice'))
		os.makedirs(self.home)
		self.link(elsewhere, os.path.join(self.home, 'profiles'))
		with self.assertRaises(profiles.ProfileError):
			profiles.exists(self.profile())


class SessionTests(TempHomeCase):

	def test_named_profile_uses_only_its_own_session(self):
		session = profiles.select_session(self.profile(), None, {}, self.tmp)
		self.assertEqual(
			session.path, os.path.join(self.home, 'profiles', 'alice', 'session_file.session')
		)
		self.assertEqual(session.source, profiles.PROFILE_SESSION)
		self.assertTrue(profiles.is_managed(session))

	def test_named_profile_refuses_session_flag_and_variable(self):
		for flag, environ in (('x.session', {}), (None, {homes.SESSION_ENV: 'x.session'})):
			with self.subTest(flag=flag, environ=environ):
				with self.assertRaises(profiles.ProfileError) as caught:
					profiles.select_session(self.profile(), flag, environ, self.tmp)
				self.assertNotIn('x.session', str(caught.exception))

	def test_default_profile_keeps_the_shared_session_choice(self):
		environ = {homes.HOME_ENV: self.home}
		default = self.profile(profiles.DEFAULT)
		chosen = profiles.select_session(default, None, environ, self.tmp)
		self.assertEqual(chosen, homes.select_session(None, environ=environ, cwd=self.tmp))
		self.assertTrue(profiles.is_managed(chosen))
		for flag, env in (('x', {}), (None, {homes.SESSION_ENV: 'x'})):
			chosen = profiles.select_session(default, flag, {**environ, **env}, self.tmp)
			self.assertFalse(profiles.is_managed(chosen))


class ListingTests(TempHomeCase):

	def test_linked_profiles_parent_is_not_traversed(self):
		elsewhere = os.path.join(self.tmp, 'elsewhere')
		os.makedirs(os.path.join(elsewhere, 'alice'))
		os.makedirs(self.home)
		self.link(elsewhere, os.path.join(self.home, 'profiles'))
		with mock.patch.object(profiles.os, 'listdir', side_effect=AssertionError('linked parent traversed')):
			entries, skipped = profiles.list_profiles(self.home)
		self.assertEqual([entry.name for entry in entries], ['default'])
		self.assertEqual(skipped, 0)

	def test_missing_home_lists_the_default_and_creates_nothing(self):
		entries, skipped = profiles.list_profiles(self.home)
		self.assertEqual([e.name for e in entries], ['default'])
		self.assertEqual(skipped, 0)
		self.assertFalse(os.path.exists(self.home))

	def test_named_folders_in_order_and_unusable_ones_counted(self):
		identity_root = os.path.join(self.home, 'profiles')
		for name in ('zed', 'alice', 'Bad', 'default', 'con'):
			os.makedirs(os.path.join(identity_root, name))
		with open(os.path.join(identity_root, 'note.txt'), 'w') as fh:
			fh.write('x')
		identity.ensure_identity(self.profile().root, 'alice', ACCOUNT)
		entries, skipped = profiles.list_profiles(self.home)
		self.assertEqual([e.name for e in entries], ['default', 'alice', 'zed'])
		self.assertEqual(skipped, 4)
		alice = entries[1]
		self.assertEqual((alice.identity, alice.credentials, alice.session), (True, False, False))
		self.assertFalse(entries[2].identity)


class IdentityTests(TempHomeCase):

	def setUp(self):
		super().setUp()
		self.root = self.profile().root
		profiles.prepare_root(self.profile())

	def path(self):
		return identity.identity_path(self.root)

	def test_created_with_a_uuid_and_kept_private(self):
		created = identity.ensure_identity(self.root, 'alice', ACCOUNT)
		self.assertEqual(str(uuid.UUID(created.profile_id)), created.profile_id)
		self.assertEqual(created.account_id, ACCOUNT)
		protection.check(self.path())
		with open(self.path(), encoding='utf-8') as fh:
			self.assertEqual(sorted(json.load(fh)), sorted(identity.KEYS))
		self.assertEqual(identity.read_identity(self.root, 'alice'), created)
		self.assertFalse(os.path.exists(self.path() + '.lock'))

	def test_unbound_record_is_bound_once_and_keeps_its_id(self):
		unbound = identity.ensure_identity(self.root, 'alice')
		self.assertIsNone(unbound.account_id)
		bound = identity.ensure_identity(self.root, 'alice', ACCOUNT)
		self.assertEqual((bound.profile_id, bound.account_id), (unbound.profile_id, ACCOUNT))
		self.assertEqual(identity.ensure_identity(self.root, 'alice'), bound)
		self.assertEqual(identity.ensure_identity(self.root, 'alice', ACCOUNT), bound)

	def test_other_account_is_refused_and_nothing_written(self):
		identity.ensure_identity(self.root, 'alice', ACCOUNT)
		with open(self.path(), 'rb') as fh:
			before = fh.read()
		with self.assertRaises(identity.AccountMismatch) as caught:
			identity.ensure_identity(self.root, 'alice', OTHER_ACCOUNT)
		self.assertNotIn(str(OTHER_ACCOUNT), str(caught.exception))
		self.assertNotIn(str(ACCOUNT), str(caught.exception))
		with open(self.path(), 'rb') as fh:
			self.assertEqual(fh.read(), before)

	def write_raw(self, data):
		with open(self.path(), 'w', encoding='utf-8') as fh:
			fh.write(data if isinstance(data, str) else json.dumps(data))
		protection.protect(self.path())

	def test_invalid_records_are_refused_without_values(self):
		good = {'schema_version': 1, 'name': 'alice', 'profile_id': str(uuid.uuid4()), 'account_id': ACCOUNT}
		for label, data in (
				('not json', '{'),
				('not an object', [1]),
				('extra key', {**good, 'phone': '+1'}),
				('missing key', {k: v for k, v in good.items() if k != 'account_id'}),
				('newer version', {**good, 'schema_version': 2}),
				('version as text', {**good, 'schema_version': '1'}),
				('other profile', {**good, 'name': 'bob'}),
				('uppercase uuid', {**good, 'profile_id': good['profile_id'].upper()}),
				('not a uuid', {**good, 'profile_id': 'x'}),
				('boolean account', {**good, 'account_id': True}),
				('negative account', {**good, 'account_id': -5}),
				('text account', {**good, 'account_id': str(ACCOUNT)})):
			with self.subTest(case=label):
				self.write_raw(data)
				with self.assertRaises(identity.IdentityError) as caught:
					identity.read_identity(self.root, 'alice')
				self.assertNotIn(str(ACCOUNT), str(caught.exception))
				with self.assertRaises(identity.IdentityError):
					identity.ensure_identity(self.root, 'alice', ACCOUNT)

	def test_held_lease_is_reported(self):
		with Lease(self.path(), 'The profile file'):
			with self.assertRaises(identity.IdentityError) as caught:
				identity.ensure_identity(self.root, 'alice', ACCOUNT)
		self.assertIn('in use', str(caught.exception))
		self.assertFalse(os.path.exists(self.path()))

	def test_failed_write_keeps_the_earlier_record(self):
		created = identity.ensure_identity(self.root, 'alice')
		with mock.patch.object(identity, 'replace_privately', side_effect=identity.StoreError('disk full')):
			with self.assertRaises(identity.IdentityError):
				identity.ensure_identity(self.root, 'alice', ACCOUNT)
		self.assertEqual(identity.read_identity(self.root, 'alice'), created)


class AccountLeaseTests(TempHomeCase):

	def test_lease_file_does_not_name_the_account(self):
		accounts.prepare(self.home)
		protection.check(accounts.leases_dir(self.home))
		with accounts.lease_for(self.home, ACCOUNT, 'alice'):
			names = os.listdir(accounts.leases_dir(self.home))
			self.assertEqual(len(names), 1)
			self.assertNotIn(str(ACCOUNT), names[0])
			with open(os.path.join(accounts.leases_dir(self.home), names[0]), encoding='utf-8') as fh:
				content = fh.read()
			self.assertNotIn(str(ACCOUNT), content)
			self.assertIn('alice', content)
		self.assertEqual(os.listdir(accounts.leases_dir(self.home)), [])

	def test_same_account_is_refused_and_other_accounts_are_not(self):
		accounts.prepare(self.home)
		with accounts.lease_for(self.home, ACCOUNT, 'alice'):
			with self.assertRaises(LeaseHeld) as caught:
				with accounts.lease_for(self.home, ACCOUNT, 'bob'):
					pass
			self.assertIn('profile alice', str(caught.exception))
			self.assertNotIn(str(ACCOUNT), str(caught.exception))
			with accounts.lease_for(self.home, OTHER_ACCOUNT, 'bob'):
				pass

	def test_unusable_home_fails_before_login(self):
		blocker = os.path.join(self.tmp, 'file')
		with open(blocker, 'w') as fh:
			fh.write('x')
		with self.assertRaises(accounts.AccountError) as caught:
			accounts.prepare(os.path.join(blocker, 'home'))
		self.assertIn('writable application home', str(caught.exception))


class VerifiedAccountTests(offline_guard.OfflineAsyncTestCase):

	async def test_get_me_once(self):
		client = FakeTelegramClient(account_id=ACCOUNT)
		self.assertEqual(await accounts.verified_account_id(client), ACCOUNT)
		self.assertEqual(client.call_names(), ['get_me'])

	async def test_failures_name_the_error_only(self):
		from telethon import errors

		for outcome in (errors.FloodWaitError(request=None, capture=30), RuntimeError('x')):
			with self.subTest(outcome=type(outcome).__name__):
				client = FakeTelegramClient(account_id=ACCOUNT).script('get_me', outcome)
				with self.assertRaises(accounts.AccountError) as caught:
					await accounts.verified_account_id(client)
				self.assertIn(type(outcome).__name__, str(caught.exception))
				self.assertEqual(client.call_names(), ['get_me'])
		with self.assertRaises(accounts.AccountError):
			await accounts.verified_account_id(FakeTelegramClient(authorized=False))


class BindingDecisionTests(TempHomeCase):

	def session(self, source=profiles.PROFILE_SESSION):
		return homes.SessionChoice(os.path.join(self.tmp, 'x.session'), source)

	def test_matrix(self):
		alice, default = self.profile(), self.profile(profiles.DEFAULT)
		external = self.session(homes.FLAG)

		# unbound default: verified but not recorded
		self.assertEqual(accounts.decide_profile_id(default, self.session(homes.HOME), ACCOUNT), (None, None))
		self.assertFalse(os.path.exists(default.identity_path))

		# named profile record bound through its own session
		profiles.prepare_root(alice)
		created = identity.ensure_identity(alice.root, 'alice')
		self.assertEqual(accounts.decide_profile_id(alice, self.session(), ACCOUNT), (created.profile_id, None))
		self.assertEqual(identity.read_identity(alice.root, 'alice').account_id, ACCOUNT)
		self.assertEqual(accounts.decide_profile_id(alice, self.session(), ACCOUNT), (created.profile_id, None))
		with self.assertRaises(accounts.AccountError) as caught:
			accounts.decide_profile_id(alice, self.session(), OTHER_ACCOUNT)
		self.assertIn('auth logout --profile alice', str(caught.exception))
		self.assertNotIn(str(OTHER_ACCOUNT), str(caught.exception))

		# default with an external session: never bound, other account recorded without ID
		os.makedirs(self.home, exist_ok=True)
		unbound = identity.ensure_identity(self.home, 'default')
		self.assertEqual(accounts.decide_profile_id(default, external, ACCOUNT), (None, None))
		self.assertIsNone(identity.read_identity(self.home, 'default').account_id)
		identity.ensure_identity(self.home, 'default', ACCOUNT)
		self.assertEqual(accounts.decide_profile_id(default, external, ACCOUNT), (unbound.profile_id, None))
		profile_id, notice = accounts.decide_profile_id(default, external, OTHER_ACCOUNT)
		self.assertIsNone(profile_id)
		self.assertIn('without its profile ID', notice)
		with self.assertRaises(accounts.AccountError):
			accounts.decide_profile_id(default, self.session(homes.HOME), OTHER_ACCOUNT)

	def test_named_profile_without_record_is_refused(self):
		with self.assertRaises(accounts.AccountError):
			accounts.decide_profile_id(self.profile(), self.session(), ACCOUNT)


if __name__ == '__main__':
	unittest.main()
