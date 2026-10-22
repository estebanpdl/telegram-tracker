# -*- coding: utf-8 -*-
'''
POSIX file modes and permission errors: the application home, profile
folders, credentials.json and profile.json are 0700/0600 whatever the
umask, a group- or world-accessible file or another owner's file is
refused when read, permission errors name the path and the reason, and
lock files that are symlinks are never followed.

These run on Linux and macOS only. On Windows, owner-only protection is a
protected DACL (test_credentials, test_profiles), and symlinks need a
privilege the test account usually lacks. Permission-denied cases are
skipped under root, which bypasses file modes.
'''

# import modules
import os
import stat
import tempfile
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from auth_support import API_HASH, PHONE, VALID
from fakes import FIXTURE_ACCOUNT

from telegram_tracker.credentials import identity, profiles, protection, store
from telegram_tracker.credentials.lease import Lease, LeaseError, LeaseHeld, lease_path
from telegram_tracker.storage.records import LOCK_FILE, OutputLocked
from telegram_tracker.storage.sqlite.lease import DatabaseLease, DatabaseLocked
from telegram_tracker.storage.writer import OutputLock


POSIX_ONLY = unittest.skipIf(
	os.name == 'nt', 'POSIX modes and symlinks; Windows uses the protected-DACL path tested elsewhere'
)


def mode(path):
	return stat.S_IMODE(os.stat(path).st_mode)


class PosixCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)

	def assert_no_values(self, text):
		for value in (API_HASH, PHONE):
			self.assertNotIn(value, text)


@POSIX_ONLY
class OwnerOnlyModeTests(PosixCase):

	def test_modes_do_not_depend_on_the_umask(self):
		for mask in (0o000, 0o022, 0o077):
			with self.subTest(umask=oct(mask)):
				home = os.path.join(self.tmp, f'home-{mask:o}')
				profile = profiles.Profile('alice', profiles.FLAG, home)
				root = profile.root
				previous = os.umask(mask)
				try:
					store.save_store(home, VALID)
					profiles.prepare_root(profile)
					identity.ensure_identity(root, 'alice', FIXTURE_ACCOUNT)
				finally:
					os.umask(previous)
				self.assertEqual(root, os.path.join(home, 'profiles', 'alice'))
				for folder in (home, os.path.dirname(root), root):
					self.assertEqual(mode(folder), 0o700, folder)
				self.assertEqual(mode(store.store_path(home)), 0o600)
				self.assertEqual(mode(identity.identity_path(root)), 0o600)
				self.assertEqual(sorted(os.listdir(home)), ['credentials.json', 'profiles'])
				self.assertEqual(os.listdir(root), ['profile.json'])

	def test_an_open_home_and_an_open_file_are_made_private_when_written(self):
		home = os.path.join(self.tmp, 'home')
		os.mkdir(home)
		os.chmod(home, 0o755)
		path = store.store_path(home)
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('{}')
		os.chmod(path, 0o644)
		store.save_store(home, VALID)
		self.assertEqual(mode(home), 0o700)
		self.assertEqual(mode(path), 0o600)
		self.assertEqual(store.read_store(home), VALID)

	def test_files_open_to_others_are_refused_when_read(self):
		home = os.path.join(self.tmp, 'home')
		root = os.path.join(self.tmp, 'root')
		store.save_store(home, VALID)
		protection.ensure_private_dir(root)
		identity.ensure_identity(root, 'alice', FIXTURE_ACCOUNT)
		for bits in (0o640, 0o604, 0o620, 0o660, 0o644):
			with self.subTest(mode=oct(bits)):
				os.chmod(store.store_path(home), bits)
				with self.assertRaises(store.StoreError) as caught:
					store.read_store(home)
				self.assertIn('is accessible to other accounts', str(caught.exception))
				self.assert_no_values(str(caught.exception))
				os.chmod(identity.identity_path(root), bits)
				with self.assertRaises(identity.IdentityError) as caught:
					identity.read_identity(root, 'alice')
				self.assertIn('is accessible to other accounts', str(caught.exception))
		os.chmod(store.store_path(home), 0o600)
		os.chmod(identity.identity_path(root), 0o600)
		self.assertEqual(store.read_store(home), VALID)
		self.assertEqual(identity.read_identity(root, 'alice').account_id, FIXTURE_ACCOUNT)

	def test_a_folder_open_to_others_fails_the_check(self):
		folder = os.path.join(self.tmp, 'home')
		os.mkdir(folder)
		os.chmod(folder, 0o750)
		with self.assertRaises(protection.ProtectionError):
			protection.check(folder)
		protection.ensure_private_dir(folder)
		self.assertEqual(mode(folder), 0o700)

	def test_a_file_of_another_owner_is_refused(self):
		home = os.path.join(self.tmp, 'home')
		store.save_store(home, VALID)
		protection.check(store.store_path(home))
		with mock.patch.object(protection.os, 'getuid', return_value=os.getuid() + 1):
			with self.assertRaises(store.StoreError) as caught:
				store.read_store(home)
		self.assertIn('is accessible to other accounts', str(caught.exception))


@POSIX_ONLY
@unittest.skipIf(os.name != 'nt' and os.geteuid() == 0, 'root bypasses file modes')
class PermissionErrorTests(PosixCase):

	def setUp(self):
		super().setUp()
		self.locked = os.path.join(self.tmp, 'locked')
		os.mkdir(self.locked)
		os.chmod(self.locked, 0o500)
		self.addCleanup(os.chmod, self.locked, 0o700)

	def test_a_home_that_cannot_be_created(self):
		with self.assertRaises(store.StoreError) as caught:
			store.save_store(os.path.join(self.locked, 'home'), VALID)
		message = str(caught.exception)
		self.assertIn(os.path.join(self.locked, 'home'), message)
		self.assertIn('Permission denied', message)
		self.assert_no_values(message)
		self.assertEqual(os.listdir(self.locked), [])

	def test_a_lease_that_cannot_be_created(self):
		guarded = os.path.join(self.locked, 'session_file.session')
		with self.assertRaises(LeaseError) as caught:
			Lease(guarded, 'The session').__enter__()
		self.assertNotIsInstance(caught.exception, LeaseHeld)
		self.assertEqual(
			str(caught.exception), f'cannot lock the session ({guarded}.lock: permission denied)'
		)

	def test_an_output_lock_that_cannot_be_created(self):
		with self.assertRaises(Exception) as caught:
			OutputLock(self.locked).__enter__()
		self.assertNotIsInstance(caught.exception, OutputLocked)
		self.assertIn(os.path.join(self.locked, LOCK_FILE), str(caught.exception))
		self.assertEqual(os.listdir(self.locked), [])


@POSIX_ONLY
class LockSymlinkTests(PosixCase):
	'''
	A lock path that is a symlink, even a dangling one, counts as held:
	O_EXCL never follows it, so the link's target is never created.
	'''

	def setUp(self):
		super().setUp()
		self.target = os.path.join(self.tmp, 'elsewhere', 'created-through-link')
		os.mkdir(os.path.dirname(self.target))

	def link(self, path):
		os.symlink(self.target, path)
		self.addCleanup(os.remove, path)

	def assert_target_untouched(self):
		self.assertEqual(os.listdir(os.path.dirname(self.target)), [])

	def test_a_lease(self):
		guarded = os.path.join(self.tmp, 'session_file.session')
		self.link(lease_path(guarded))
		with self.assertRaises(LeaseHeld):
			Lease(guarded, 'The session').__enter__()
		self.assert_target_untouched()

	def test_the_output_lock(self):
		out = os.path.join(self.tmp, 'out')
		os.mkdir(out)
		self.link(os.path.join(out, LOCK_FILE))
		with self.assertRaises(OutputLocked):
			OutputLock(out).__enter__()
		self.assert_target_untouched()

	def test_the_database_lease(self):
		database = os.path.join(self.tmp, 'collection.sqlite3')
		self.link(database + '.lock')
		with self.assertRaises(DatabaseLocked):
			DatabaseLease(database).__enter__()
		self.assert_target_untouched()
