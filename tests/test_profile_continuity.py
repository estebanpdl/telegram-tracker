# -*- coding: utf-8 -*-
'''
Collected folders across profile maintenance, in every storage mode,
through cli.main with fake clients: after `auth rename-profile`, the
renamed profile continues `collect --update` and `download-media` on the
histories recorded with its profile ID, and the rename changes no output
byte. After `auth delete-profile`, a new profile at the same name (a new
profile ID) is refused on those histories. A history recorded with a
name and no profile ID is accepted for any profile that later takes the
name: the documented, accepted risk of reusing names.
'''

# import modules
import hashlib
import json
import os
import sqlite3
import unittest

from unittest import mock

from fakes import FIXTURE_ACCOUNT
from media_collect_support import BETA, MediaCase, standard_world

from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import identity, profiles


OTHER_ACCOUNT = 777000444
MODES = ('files', 'sqlite', 'both')
BATCH = ('alpha_fx', 'beta_fx')


class ContinuityCase(MediaCase):

	def setUp(self):
		super().setUp()
		self.home = os.path.join(self.tmp, 'home')
		environ = dict(os.environ)
		environ[homes.HOME_ENV] = self.home
		environ.pop(homes.SESSION_ENV, None)
		environ.pop(profiles.PROFILE_ENV, None)
		patcher = mock.patch.dict(os.environ, environ, clear=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def make_profile(self, name, account=FIXTURE_ACCOUNT):
		profile = profiles.Profile(name, profiles.FLAG, self.home)
		profiles.prepare_root(profile)
		return identity.ensure_identity(profile.root, name, account)

	def auth(self, *argv):
		code, out, err = self.run_cli(['auth', *argv])
		self.assertEqual(code, 0, err)
		return out

	def collected(self, name, mode, out, account=FIXTURE_ACCOUNT, *extra):
		return self.collect(
			'--profile', name, *extra, targets=BATCH, out=out, storage=mode,
			client=standard_world(account_id=account)
		)

	def histories(self, out, mode):
		'''
		What each saved history records about its profile, from the
		provenance files (files) or the histories table (sqlite, both).
		'''
		if mode == 'files':
			found = {}
			for channel in BATCH:
				with open(os.path.join(out, channel, f'{channel}_provenance.json'), encoding='utf-8') as fh:
					record = json.load(fh)
				found[channel] = (record['profile_name'], record['profile_id'])
			return found
		con = sqlite3.connect(f'file:{self.db(out)}?mode=ro', uri=True)
		try:
			rows = con.execute('SELECT channel_id, profile_name, profile_id FROM histories ORDER BY channel_id')
			return {channel: (name, pid) for channel, name, pid in rows}
		finally:
			con.close()

	def tree(self, out):
		'''
		sha256 of every file in the output folder, by relative path.
		'''
		found = {}
		for root, _, files in os.walk(out):
			for file in files:
				path = os.path.join(root, file)
				with open(path, 'rb') as fh:
					found[os.path.relpath(path, out)] = hashlib.sha256(fh.read()).hexdigest()
		return found


class ContinuityTests(ContinuityCase):

	def test_update_and_download_continue_after_a_rename(self):
		for mode in MODES:
			with self.subTest(storage=mode):
				out = os.path.join(self.tmp, mode)
				created = self.make_profile(f'old{mode}')
				code, _, err = self.collected(f'old{mode}', mode, out)
				self.assertEqual(code, 0, err)
				before = self.tree(out)
				self.auth('rename-profile', f'old{mode}', f'new{mode}')
				self.assertEqual(self.tree(out), before)

				code, _, err = self.collected(f'new{mode}', None, out, FIXTURE_ACCOUNT, '--update')
				self.assertEqual(code, 0, err)
				self.assertNotIn('refused', err)
				recorded = self.histories(out, mode)
				self.assertEqual(set(recorded.values()), {(f'old{mode}', created.profile_id)})
				self.assertEqual(self.status(out)['profile_name'], f'new{mode}')

				code, _, err = self.download(
					'--profile', f'new{mode}', out=out, client=standard_world(account_id=FIXTURE_ACCOUNT))
				self.assertEqual(code, 0, err)
				self.assertTrue(self.items(out))
				self.assertEqual({item['state'] for item in self.items(out)}, {'complete'})

	def test_a_reused_name_is_refused_on_histories_of_the_old_profile_id(self):
		for mode in MODES:
			with self.subTest(storage=mode):
				name = f'reused{mode}'
				out = os.path.join(self.tmp, mode)
				first = self.make_profile(name)
				self.assertEqual(self.collected(name, mode, out)[0], 0)
				self.auth('delete-profile', name, '--yes')
				again = self.make_profile(name, OTHER_ACCOUNT)
				self.assertNotEqual(again.profile_id, first.profile_id)
				recorded = self.histories(out, mode)

				code, _, err = self.collected(name, None, out, OTHER_ACCOUNT, '--update')
				self.assertEqual(code, 1)
				self.assertIn(f'its saved history belongs to profile {name} with another profile ID', err)
				self.assertEqual(self.histories(out, mode), recorded)

				code, _, err = self.download('--profile', name, out=out, client=standard_world(account_id=OTHER_ACCOUNT))
				self.assertEqual(code, 1)
				self.assertIn(f'collected by profile {name} with another profile ID', err)
				self.assertFalse(self.items(out))

	def test_a_name_only_history_follows_whoever_takes_the_name(self):
		'''
		The accepted risk: only a profile ID tells reused names apart.
		'''
		for mode in ('files', 'sqlite'):
			with self.subTest(storage=mode):
				name = f'label{mode}'
				out = os.path.join(self.tmp, mode)
				self.make_profile(name)
				self.assertEqual(self.collected(name, mode, out)[0], 0)
				if mode == 'files':
					path = os.path.join(out, 'beta_fx', 'beta_fx_provenance.json')
					with open(path, encoding='utf-8') as fh:
						record = json.load(fh)
					record['profile_id'] = None
					with open(path, 'w', encoding='utf-8') as fh:
						json.dump(record, fh)
				else:
					con = sqlite3.connect(self.db(out))
					with con:
						con.execute('UPDATE histories SET profile_id = NULL WHERE channel_id = ?', (BETA,))
					con.close()
				self.auth('delete-profile', name, '--yes')
				self.make_profile(name, OTHER_ACCOUNT)

				code, _, err = self.collected(name, None, out, OTHER_ACCOUNT, '--update')
				self.assertEqual(code, 1)
				self.assertIn(f'alpha_fx: --update refused: its saved history belongs to profile {name} with another', err)
				self.assertNotIn('beta_fx: --update refused', err)
				self.assertEqual(self.histories(out, mode)['beta_fx' if mode == 'files' else BETA], (name, None))


if __name__ == '__main__':
	unittest.main()
