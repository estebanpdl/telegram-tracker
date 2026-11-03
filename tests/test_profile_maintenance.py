# -*- coding: utf-8 -*-
'''
`auth rename-profile` and `auth delete-profile` on temporary homes:
rename keeps the profile ID, account binding, credential and session
bytes; an interrupted rename is refused on use and finished by a rerun,
while a copied folder is refused; deletion removes only the profile's
own files and folder and reports a failure part way; dry runs take no
lease. Every command holding or leaving one of the profile's leases
refuses both. Named profile folders are no longer recreated by collect,
download-media, or login's save step once they were renamed or deleted,
and refusal messages name a renamed profile's current name. No Telegram
client is created.
'''

# import modules
import hashlib
import os
import shutil
import types
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from auth_support import VALID, CredentialCase
from fakes import FIXTURE_ACCOUNT

from telegram_tracker.cli import auth as cli_auth
from telegram_tracker.cli import profile_maintenance, run_setup
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import identity, maintenance, profiles, protection, store
from telegram_tracker.credentials.lease import Lease, LeaseHeld, lease_path
from telegram_tracker.media.fetch import profile_refusal
from telegram_tracker.storage.provenance import RunProfile, refusal


OTHER_ACCOUNT = 777000333
# the guard forbids creating files named *.session or *.session-journal,
# so deletion tests give the session file another name (see
# test_the_files_of_a_profile_are_the_production_names)
STANDIN_SESSION = 'session_file.standin'


def digest(path):
	with open(path, 'rb') as fh:
		return hashlib.sha256(fh.read()).hexdigest()


class MaintenanceCase(CredentialCase):

	def setUp(self):
		super().setUp()
		self.terminal = True
		self.patch(profile_maintenance, 'confirm', self.confirm)
		self.patch(profile_maintenance, 'interactive', lambda: self.terminal)

	def profile(self, name):
		return profiles.Profile(name, profiles.FLAG, self.home)

	def root(self, name):
		return self.profile(name).root

	def make_profile(self, name, account=FIXTURE_ACCOUNT, values=VALID):
		profile = self.profile(name)
		profiles.prepare_root(profile)
		created = identity.ensure_identity(profile.root, name, account)
		if values is not None:
			store.save_store(profile.root, values)
		return created

	def files(self, name):
		root = self.root(name)
		return {f: digest(os.path.join(root, f)) for f in sorted(os.listdir(root))}

	def locks(self, name):
		return sorted(f for f in os.listdir(self.root(name)) if f.endswith('.lock'))

	def auth(self, *argv):
		return self.run_main(['auth', *argv])

	def use_standin_session(self, name):
		'''
		Give profile `name` a session file and companions under the stand-in
		name; returns the files written.
		'''
		self.patch(maintenance, 'SESSION_FILE', STANDIN_SESSION)
		written = [STANDIN_SESSION] + [STANDIN_SESSION + s for s in maintenance.COMPANION_SUFFIXES]
		for file in written:
			with open(os.path.join(self.root(name), file), 'wb') as fh:
				fh.write(b'synthetic session bytes ' + file.encode('ascii'))
		return written


class RenameTests(MaintenanceCase):

	def test_rename_keeps_the_record_and_the_file_bytes(self):
		created = self.make_profile('old')
		before = self.files('old')
		code, out, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 0, err)
		self.assertFalse(os.path.lexists(self.root('old')))
		self.assertEqual(identity.read_identity(self.root('new'), 'new'),
			identity.Identity('new', created.profile_id, FIXTURE_ACCOUNT))
		after = self.files('new')
		self.assertEqual(after[store.STORE_NAME], before[store.STORE_NAME])
		self.assertEqual(sorted(after), sorted(before))
		self.assertEqual(self.locks('new'), [])
		self.assertIn('Renamed profile old to new', out)
		self.assertIn('no profile ID does not follow', out)
		self.assertEqual(self.created, [])
		self.assert_no_secrets()
		self.assertNotIn(str(FIXTURE_ACCOUNT), out + err)

	def test_a_free_name_can_be_reused_and_renamed_back(self):
		first = self.make_profile('old')
		self.assertEqual(self.auth('rename-profile', 'old', 'new')[0], 0)
		fresh = self.make_profile('old', account=OTHER_ACCOUNT)
		self.assertNotEqual(fresh.profile_id, first.profile_id)
		code, _, err = self.auth('rename-profile', 'new', 'old')
		self.assertEqual(code, 1)
		self.assertIn('profile old already exists', err)
		self.assertEqual(self.auth('rename-profile', 'old', 'spare')[0], 0)
		self.assertEqual(self.auth('rename-profile', 'new', 'old')[0], 0)
		self.assertEqual(identity.read_identity(self.root('old'), 'old').profile_id, first.profile_id)
		self.assertEqual(identity.read_identity(self.root('spare'), 'spare').profile_id, fresh.profile_id)

	def test_operands_that_name_no_usable_pair_exit_2(self):
		self.make_profile('old')
		before = self.files('old')
		os.mkdir(os.path.join(self.home, 'profiles', 'Case'))
		for label, argv, expected in (
				('default as OLD', ('default', 'new'), 'default profile cannot'),
				('default as NEW', ('old', 'default'), 'default profile cannot'),
				('same name', ('old', 'old'), 'same profile name'),
				('invalid name', ('old', 'New'), 'lowercase letter'),
				('device name', ('old', 'nul'), 'reserved'),
				('unknown OLD', ('missing', 'new'), 'there is no profile missing'),
				('case variant of NEW', ('old', 'case'), 'only in case')):
			with self.subTest(case=label):
				code, _, err = self.auth('rename-profile', *argv)
				self.assertEqual(code, 2, err)
				self.assertIn(expected, err)
				self.assertEqual(self.files('old'), before)

	def test_an_existing_new_refuses_with_exit_1(self):
		self.make_profile('old')
		self.make_profile('other', account=OTHER_ACCOUNT)
		before = self.files('old')
		profiles_dir = os.path.join(self.home, 'profiles')
		for label, make in (
				('empty folder', lambda path: os.mkdir(path)),
				('file', lambda path: open(path, 'w').close()),
				('another profile', None)):
			with self.subTest(case=label):
				target = 'other' if make is None else 'taken'
				if make is not None:
					make(os.path.join(profiles_dir, target))
				code, _, err = self.auth('rename-profile', 'old', target)
				self.assertEqual(code, 1, err)
				self.assertIn(f'profile {target} already exists', err)
				self.assertEqual(self.files('old'), before)
				if make is not None:
					path = os.path.join(profiles_dir, target)
					os.rmdir(path) if os.path.isdir(path) else os.remove(path)

	def test_held_or_stale_leases_refuse_and_change_nothing(self):
		self.make_profile('old')
		before = self.files('old')
		for file, what in maintenance.leased_files():
			with self.subTest(file=file):
				with Lease(os.path.join(self.root('old'), file), what, 'old'):
					code, _, err = self.auth('rename-profile', 'old', 'new')
					self.assertEqual(code, 1, err)
					self.assertIn(f'{what} is in use by another process', err)
					self.assertIn('profile old', err)
				self.assertFalse(os.path.lexists(self.root('new')))
				self.assertEqual(self.files('old'), before)

	def test_a_consumer_session_lease_refuses_the_rename(self):
		'''
		The lease collect, download-media and login take on the session.
		'''
		self.make_profile('old')
		with Lease(profiles.own_session(self.profile('old')), 'The session', 'old'):
			code, _, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 1)
		self.assertIn('The session is in use', err)
		self.assertTrue(os.path.isdir(self.root('old')))

	def test_an_invalid_old_record_refuses(self):
		self.make_profile('old')
		path = os.path.join(self.root('old'), identity.IDENTITY_NAME)
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('{')
		protection.protect(path)
		code, _, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 1)
		self.assertIn('cannot be read', err)
		self.assertFalse(os.path.lexists(self.root('new')))

	def move_without_relabel(self, old='old', new='new'):
		os.rename(self.root(old), self.root(new))

	def test_an_interrupted_rename_is_refused_on_use_and_finished_by_a_rerun(self):
		created = self.make_profile('old')
		self.move_without_relabel()
		with self.assertRaises(identity.IdentityError) as caught:
			identity.read_identity(self.root('new'), 'new')
		self.assertIn('belongs to profile old', str(caught.exception))
		self.assertIn('`auth rename-profile old new` was interrupted', str(caught.exception))
		code, out, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 0, err)
		self.assertIn('Finished renaming profile old to new', out)
		self.assertEqual(identity.read_identity(self.root('new'), 'new').profile_id, created.profile_id)

	def test_locks_moved_by_a_killed_rename_refuse_until_removed(self):
		self.make_profile('old')
		self.move_without_relabel()
		stale = lease_path(os.path.join(self.root('new'), maintenance.SESSION_FILE))
		with open(stale, 'w', encoding='utf-8') as fh:
			fh.write('{"pid": 1, "started_at": "2026-01-01T00:00:00+00:00", "profile": "old"}')
		code, _, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 1)
		self.assertIn(f'delete {stale}', err)
		self.assertEqual(identity.read_record(self.root('new')).name, 'old')
		os.remove(stale)
		self.assertEqual(self.auth('rename-profile', 'old', 'new')[0], 0)

	def test_a_fresh_profile_at_the_old_name_does_not_stop_finishing(self):
		created = self.make_profile('old')
		self.move_without_relabel()
		fresh = self.make_profile('old', account=OTHER_ACCOUNT)
		code, out, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 0, err)
		self.assertIn('is another profile', out)
		self.assertEqual(identity.read_identity(self.root('new'), 'new').profile_id, created.profile_id)
		self.assertEqual(identity.read_identity(self.root('old'), 'old'), fresh)

	def test_a_copy_is_refused_not_relabelled(self):
		self.make_profile('old')
		shutil.copytree(self.root('old'), self.root('new'))
		old_before, new_before = self.files('old'), self.files('new')
		for argv in (('rename-profile', 'old', 'new'), ('rename-profile', '--dry-run', 'old', 'new')):
			with self.subTest(argv=argv):
				code, _, err = self.auth(*argv)
				self.assertEqual(code, 2, err)
				self.assertIn('is a copy of profile old', err)
				self.assertEqual(self.files('old'), old_before)
				self.assertEqual(self.files('new'), new_before)

	def test_a_failed_rewrite_after_the_move_releases_the_leases_at_new(self):
		created = self.make_profile('old')
		with mock.patch.object(identity, 'replace_privately', side_effect=store.StoreError('disk full')):
			code, _, err = self.auth('rename-profile', 'old', 'new')
		self.assertEqual(code, 1)
		self.assertIn('run the command again to finish', err)
		self.assertFalse(os.path.lexists(self.root('old')))
		self.assertEqual(self.locks('new'), [])
		self.assertEqual(identity.read_record(self.root('new')).name, 'old')
		self.assertEqual(self.auth('rename-profile', 'old', 'new')[0], 0)
		self.assertEqual(identity.read_identity(self.root('new'), 'new').profile_id, created.profile_id)

	def test_a_failed_move_moves_nothing(self):
		self.make_profile('old')
		before = self.files('old')
		real_rename = os.rename
		for label, error, expected in (
				('open file', PermissionError(13, 'Access is denied'),
					'is open in another program' if os.name == 'nt' else 'permission denied'),
				('created meanwhile', FileExistsError(17, 'File exists'), 'was created meanwhile')):
			with self.subTest(case=label):
				def failing(source, target, *args, **kwargs):
					if os.path.basename(target) == 'new':
						raise error
					return real_rename(source, target, *args, **kwargs)
				with mock.patch.object(maintenance.os, 'rename', failing):
					code, _, err = self.auth('rename-profile', 'old', 'new')
				self.assertEqual(code, 1)
				self.assertIn(expected, err)
				self.assertIn('Nothing was moved', err)
				self.assertEqual(self.files('old'), before)
				self.assertEqual(self.locks('old'), [])

	def test_the_leases_are_held_across_the_move(self):
		self.make_profile('old')
		seen = {}
		real_rename, real_relabel = os.rename, maintenance.relabel

		def rename(source, target, *args, **kwargs):
			seen['before move'] = sorted(f for f in os.listdir(source) if f.endswith('.lock'))
			return real_rename(source, target, *args, **kwargs)

		def relabel(root, name, new_name):
			try:
				with Lease(os.path.join(root, maintenance.SESSION_FILE), 'The session'):
					seen['consumer'] = 'leased'
			except LeaseHeld:
				seen['consumer'] = 'refused'
			return real_relabel(root, name, new_name)

		with mock.patch.object(maintenance.os, 'rename', rename), \
				mock.patch.object(maintenance, 'relabel', relabel):
			self.assertEqual(self.auth('rename-profile', 'old', 'new')[0], 0)
		self.assertEqual(seen['before move'], sorted(maintenance.lock_files()))
		self.assertEqual(seen['consumer'], 'refused')
		self.assertEqual(self.locks('new'), [])

	def test_a_record_changed_after_the_check_is_refused_under_the_leases(self):
		created = self.make_profile('old')
		plan = maintenance.plan_rename(self.home, 'old', 'new')
		path = os.path.join(self.root('old'), identity.IDENTITY_NAME)
		other = identity.Identity('old', '00000000-0000-4000-8000-00000000000a', FIXTURE_ACCOUNT)
		identity._write(path, other)
		with self.assertRaises(maintenance.MaintenanceError) as caught:
			maintenance.rename(plan)
		self.assertIn('changed while the rename was checked', str(caught.exception))
		self.assertFalse(os.path.lexists(self.root('new')))
		self.assertEqual(self.locks('old'), [])
		self.assertNotEqual(identity.read_identity(self.root('old'), 'old').profile_id, created.profile_id)

	def test_dry_run_takes_no_lease_and_changes_nothing(self):
		self.make_profile('old')
		before = self.files('old')
		entered = []

		class Recording(Lease):
			def __enter__(self):
				entered.append(self.path)
				return super().__enter__()

		with mock.patch.object(maintenance, 'Lease', Recording):
			code, out, err = self.auth('rename-profile', '--dry-run', 'old', 'new')
			self.assertEqual(code, 0, err)
			self.assertIn('Would rename profile old to new', out)
			session = profiles.own_session(self.profile('old'))
			with Lease(session, 'The session', 'old'):
				code, out, err = self.auth('rename-profile', '--dry-run', 'old', 'new')
				self.assertEqual(code, 1)
				self.assertIn('would be refused: The session is in use', err)
				self.assertTrue(os.path.lexists(lease_path(session)))
		self.assertEqual(entered, [])
		self.assertEqual(self.files('old'), before)
		self.assertFalse(os.path.lexists(self.root('new')))


class DeleteTests(MaintenanceCase):

	def test_the_files_of_a_profile_are_the_production_names(self):
		self.assertEqual(maintenance.SESSION_FILE, homes.SESSION_NAME)
		self.assertEqual(maintenance.profile_files(), (
			'session_file.session-journal', 'session_file.session-wal', 'session_file.session-shm',
			'session_file.session', 'credentials.json', 'profile.json'))
		self.assertEqual(maintenance.lock_files(), (
			'session_file.session.lock', 'credentials.json.lock', 'profile.json.lock'))

	def test_delete_removes_only_the_profile_files_and_its_folder(self):
		self.make_profile('gone')
		self.make_profile('kept', account=OTHER_ACCOUNT)
		self.use_standin_session('gone')
		kept = self.files('kept')
		outside = os.path.join(self.tmp, 'out')
		os.mkdir(outside)
		with open(os.path.join(outside, 'data.json'), 'w', encoding='utf-8') as fh:
			fh.write('{}')
		removed = []
		real_remove = os.remove

		def remove(path, *args, **kwargs):
			removed.append(os.path.basename(path))
			return real_remove(path, *args, **kwargs)

		with mock.patch.object(maintenance.os, 'remove', remove):
			code, out, err = self.auth('delete-profile', 'gone', '--yes')
		self.assertEqual(code, 0, err)
		self.assertFalse(os.path.lexists(self.root('gone')))
		files = [f for f in removed if not f.endswith('.lock')]
		self.assertEqual(files, list(maintenance.profile_files()))
		self.assertEqual(removed[-1], STANDIN_SESSION + '.lock')
		self.assertEqual(self.files('kept'), kept)
		self.assertEqual(os.listdir(outside), ['data.json'])
		self.assertIn('not logged out on Telegram', out)
		self.assertIn('auth logout --profile gone', out)
		self.assertEqual(self.created, [])
		self.assert_no_secrets()

	def test_confirmation_defaults_to_no(self):
		self.make_profile('alice')
		before = self.files('alice')
		for label, answer, terminal, expected in (
				('declined', False, True, 1),
				('end of input', None, True, 1),
				('no terminal', None, False, 2)):
			with self.subTest(case=label):
				self.terminal = terminal
				if terminal:
					self.answers.append(answer)
				code, _, err = self.auth('delete-profile', 'alice')
				self.assertEqual(code, expected, err)
				self.assertEqual(self.files('alice'), before)
		self.assertIn('Pass --yes', err)
		self.assertEqual(self.answers, [])

	def test_confirmed_deletion(self):
		self.make_profile('alice')
		self.answers.append(True)
		code, _, err = self.auth('delete-profile', 'alice')
		self.assertEqual(code, 0, err)
		self.assertFalse(os.path.lexists(self.root('alice')))
		self.assertIn('Delete profile alice from this computer?', self.outputs)

	def test_refusals(self):
		self.make_profile('alice')
		root = self.root('alice')
		for label, setup, argv, code, expected in (
				('default', None, ('default',), 2, 'default profile cannot'),
				('unknown', None, ('bob',), 2, 'there is no profile bob'),
				('invalid name', None, ('Alice',), 2, 'lowercase letter'),
				('unexpected file', ('file', 'notes.txt'), ('alice',), 1, 'notes.txt'),
				('subfolder', ('dir', 'extra'), ('alice',), 1, 'extra'),
				('staging leftover', ('file', 'credentials.json.123.tmp'), ('alice',), 1, 'credentials.json.123.tmp')):
			with self.subTest(case=label):
				before = self.files('alice') if setup is None else None
				made = None
				if setup is not None:
					made = os.path.join(root, setup[1])
					if setup[0] == 'dir':
						os.mkdir(made)
					else:
						open(made, 'w').close()
					before = self.files('alice') if setup[0] == 'file' else None
				result, _, err = self.auth('delete-profile', *argv, '--yes')
				self.assertEqual(result, code, err)
				self.assertIn(expected, err)
				self.assertTrue(os.path.isfile(os.path.join(root, identity.IDENTITY_NAME)))
				if before is not None:
					self.assertEqual(self.files('alice'), before)
				if made:
					os.rmdir(made) if os.path.isdir(made) else os.remove(made)

	def test_a_hard_linked_file_refuses(self):
		self.make_profile('alice')
		link = os.path.join(self.tmp, 'elsewhere.json')
		os.link(os.path.join(self.root('alice'), store.STORE_NAME), link)
		code, _, err = self.auth('delete-profile', 'alice', '--yes')
		self.assertEqual(code, 1)
		self.assertIn('other hard links', err)
		self.assertTrue(os.path.isfile(os.path.join(self.root('alice'), store.STORE_NAME)))

	def test_a_record_of_another_profile_refuses_with_the_rename_hint(self):
		self.make_profile('old')
		self.move_folder('old', 'new')
		code, _, err = self.auth('delete-profile', 'new', '--yes')
		self.assertEqual(code, 1)
		self.assertIn('`auth rename-profile old new` was interrupted', err)
		self.assertTrue(os.path.isdir(self.root('new')))

	def move_folder(self, old, new):
		os.rename(self.root(old), self.root(new))

	def test_held_or_stale_leases_refuse(self):
		self.make_profile('alice')
		before = self.files('alice')
		for file, what in maintenance.leased_files():
			with self.subTest(file=file):
				with Lease(os.path.join(self.root('alice'), file), what, 'alice'):
					code, _, err = self.auth('delete-profile', 'alice', '--yes')
					self.assertEqual(code, 1, err)
					self.assertIn(f'{what} is in use by another process', err)
				self.assertEqual(self.files('alice'), before)

	def test_a_failure_part_way_is_reported_and_a_rerun_finishes(self):
		self.make_profile('alice')
		self.use_standin_session('alice')
		real_remove = os.remove

		def remove(path, *args, **kwargs):
			if os.path.basename(path) == store.STORE_NAME:
				raise PermissionError(13, 'Access is denied', path)
			return real_remove(path, *args, **kwargs)

		with mock.patch.object(maintenance.os, 'remove', remove):
			code, _, err = self.auth('delete-profile', 'alice', '--yes')
		self.assertEqual(code, 1)
		self.assertIn('deleted only in part', err)
		self.assertIn(f'Removed: {STANDIN_SESSION}-journal', err)
		self.assertIn('Run the command again to finish', err)
		self.assertEqual(sorted(os.listdir(self.root('alice'))), [store.STORE_NAME, identity.IDENTITY_NAME])
		code, _, err = self.auth('delete-profile', 'alice', '--yes')
		self.assertEqual(code, 0, err)
		self.assertFalse(os.path.lexists(self.root('alice')))

	def test_an_entry_added_after_the_check_is_refused_under_the_leases(self):
		self.make_profile('alice')
		plan = maintenance.plan_delete(self.home, 'alice')
		with open(os.path.join(self.root('alice'), 'notes.txt'), 'w', encoding='utf-8') as fh:
			fh.write('kept')
		before = self.files('alice')
		with self.assertRaises(maintenance.MaintenanceError) as caught:
			maintenance.delete(plan)
		self.assertIn('notes.txt', str(caught.exception))
		self.assertEqual(self.files('alice'), before)

	def test_a_folder_left_without_profile_json_can_be_deleted(self):
		self.make_profile('alice')
		os.remove(os.path.join(self.root('alice'), identity.IDENTITY_NAME))
		code, out, err = self.auth('delete-profile', 'alice', '--yes')
		self.assertEqual(code, 0, err)
		self.assertIn('It has no profile.json', out)
		self.assertFalse(os.path.lexists(self.root('alice')))

	def test_a_new_profile_at_a_deleted_name_gets_a_new_id(self):
		first = self.make_profile('alice')
		self.assertEqual(self.auth('delete-profile', 'alice', '--yes')[0], 0)
		again = self.make_profile('alice', account=OTHER_ACCOUNT)
		self.assertNotEqual(again.profile_id, first.profile_id)

	def test_dry_run_takes_no_lease_and_removes_nothing(self):
		self.make_profile('alice')
		before = self.files('alice')
		entered = []

		class Recording(Lease):
			def __enter__(self):
				entered.append(self.path)
				return super().__enter__()

		with mock.patch.object(maintenance, 'Lease', Recording):
			code, out, err = self.auth('delete-profile', '--dry-run', 'alice')
			self.assertEqual(code, 0, err)
			self.assertIn('Dry run: nothing was changed', out)
			store_lock = os.path.join(self.root('alice'), store.STORE_NAME)
			with Lease(store_lock, 'The credentials file'):
				code, _, err = self.auth('delete-profile', '--dry-run', 'alice')
				self.assertEqual(code, 1)
				self.assertIn('would be refused: The credentials file is in use', err)
		self.assertEqual(entered, [])
		self.assertEqual(self.files('alice'), before)
		self.assertEqual(self.answers, [])


class ConsumerTests(MaintenanceCase):
	'''
	The two places that recreated a named profile's folder after it was
	moved or deleted, and the current-name messages.
	'''

	def test_prepare_session_does_not_recreate_a_moved_folder(self):
		self.make_profile('old')
		profile = self.profile('old')
		session = profiles.select_session(profile)
		os.rename(self.root('old'), self.root('new'))
		with mock.patch('sys.stderr') as stderr:
			self.assertEqual(run_setup.prepare_session(session, profile, self.home), 1)
		written = ''.join(c.args[0] for c in stderr.write.call_args_list)
		self.assertIn('Profile old was renamed or deleted after this command started', written)
		self.assertFalse(os.path.lexists(self.root('old')))

	def test_prepare_session_protects_an_existing_named_folder(self):
		self.make_profile('alice')
		profile = self.profile('alice')
		session = profiles.select_session(profile)
		protected = []
		real_protect = protection.protect

		def protect(path, **kwargs):
			protected.append(os.path.normcase(path))
			return real_protect(path, **kwargs)

		with mock.patch.object(protection, 'protect', protect):
			self.assertIsNone(run_setup.prepare_session(session, profile, self.home))
		self.assertIn(os.path.normcase(profile.root), protected)

	def test_login_does_not_recreate_a_removed_folder_when_saving(self):
		self.make_profile('alice', values=None)
		profile = self.profile('alice')
		shutil.rmtree(profile.root)
		resolution = types.SimpleNamespace(values=dict(VALID), sources={})
		with mock.patch('sys.stderr') as stderr:
			code = cli_auth.finish_save(cli_auth.SAVE, profile, resolution, None)
		self.assertEqual(code, 1)
		written = ''.join(c.args[0] for c in stderr.write.call_args_list)
		self.assertIn('Profile alice was renamed or deleted after this command started', written)
		self.assertFalse(os.path.lexists(profile.root))

	def test_saving_into_an_existing_named_folder_still_protects_it(self):
		self.make_profile('alice', values=None)
		protected = []
		real_protect = protection.protect

		def protect(path, **kwargs):
			protected.append(os.path.normcase(path))
			return real_protect(path, **kwargs)

		with mock.patch.object(protection, 'protect', protect):
			store.save_store(self.root('alice'), VALID, create=False)
		self.assertIn(os.path.normcase(self.root('alice')), protected)
		self.assertEqual(store.read_store(self.root('alice')), VALID)

	def test_current_labels_skip_copies_and_invalid_records(self):
		alice = self.make_profile('alice')
		self.make_profile('copied')
		shutil.copytree(self.root('copied'), self.root('copy'))
		path = os.path.join(self.root('copy'), identity.IDENTITY_NAME)
		record = identity.read_record(self.root('copied'))
		identity._write(path, identity.Identity('copy', record.profile_id, record.account_id))
		os.mkdir(self.root('broken'))
		with open(os.path.join(self.root('broken'), identity.IDENTITY_NAME), 'w') as fh:
			fh.write('{')
		labels = profiles.current_labels(self.home)
		self.assertEqual(labels, {alice.profile_id: 'alice'})

	def test_refusals_name_the_current_profile(self):
		renamed = self.make_profile('old')
		self.assertEqual(self.auth('rename-profile', 'old', 'new')[0], 0)
		fresh = self.make_profile('old', account=OTHER_ACCOUNT)
		labels = profiles.current_labels(self.home)
		previous = {'profile_name': 'old', 'profile_id': renamed.profile_id}
		run = RunProfile('old', fresh.profile_id, labels=labels)
		self.assertEqual(
			refusal(previous, run),
			'its saved history belongs to profile old (now profile new) with another profile ID'
		)
		self.assertEqual(
			refusal(previous, RunProfile('old', fresh.profile_id)),
			'its saved history belongs to profile old with another profile ID'
		)
		self.assertIsNone(refusal(previous, RunProfile('new', renamed.profile_id, labels=labels)))
		message = profile_refusal(types.SimpleNamespace(**previous), run)
		self.assertIn('collected by profile old (now profile new)', message)
		self.assertTrue(message.endswith('Use --profile new'), message)
		self.assertEqual(run, RunProfile('old', fresh.profile_id))


if __name__ == '__main__':
	unittest.main()
