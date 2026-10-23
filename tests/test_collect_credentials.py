# -*- coding: utf-8 -*-
'''
`collect` with the shared session and credential resolution: saved
credentials and the home session are found from any working directory
without prompts; the session lease is taken before any client exists;
missing or invalid credentials stop before a client with exit 2. The
canonical and legacy forms resolve the same way.
'''

# import modules
import json
import os
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)
import virtual_time

from auth_support import API_HASH, API_ID, PHONE, VALID, CredentialCase, env_values
from test_platform_paths import ignores_case

from telegram_tracker.cli import collect as cli_collect
from telegram_tracker.credentials import sources, store
from telegram_tracker.credentials.lease import Lease


class CollectCredentialTests(CredentialCase):

	def setUp(self):
		super().setUp()
		self.patch(cli_collect, 'asyncio_run', virtual_time.run)
		self.out = os.path.join(self.tmp, 'out')

	def collect(self, *extra, legacy=False):
		argv = ['--telegram-channel', 'alpha_fx', '-o', self.out, *extra]
		return self.run_main(argv if legacy else ['collect', *argv])

	def test_saved_credentials_and_home_session_from_an_unrelated_directory(self):
		self.save()
		self.pretend_files(self.home_session)
		for legacy in (False, True):
			with self.subTest(legacy=legacy):
				del self.created[:]
				code, out, err = self.collect(legacy=legacy)
				self.assertEqual((code, err), (0, ''))
				self.assertEqual(self.created, [(self.home_session, API_ID, API_HASH)])
				self.assertEqual(self.code_prompt.count, 0)
				self.assertNotIn('send_code_request', self.client.call_names())
		self.assertFalse(os.path.exists(self.home_session + '.lock'))
		self.assert_no_secrets()

	def test_store_wins_over_the_placeholder_repository_ini(self):
		self.save()
		legacy_ini = os.path.join(self.cwd, sources.LEGACY_INI)
		real_isfile = os.path.isfile
		self.patch(sources, 'read_ini', mock.Mock(side_effect=AssertionError('INI read')))
		with mock.patch.object(sources.os.path, 'isfile', lambda p: p == legacy_ini or real_isfile(p)):
			code, _, err = self.collect()
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.created[0][1:], (API_ID, API_HASH))

	def test_first_collection_creates_the_private_home_for_its_session(self):
		os.environ.update(env_values(VALID))
		code, _, _ = self.collect()
		self.assertEqual(code, 0)
		self.assertEqual(self.created[0][0], self.home_session)
		self.assertTrue(os.path.isdir(self.home))
		self.assertIsNone(store.read_store(self.home))

	def test_legacy_session_only_is_used_with_a_notice(self):
		os.environ.update(env_values(VALID))
		legacy = os.path.join(self.cwd, 'session_file.session')
		self.pretend_files(legacy)
		code, _, err = self.collect(legacy=True)
		self.assertEqual(code, 0)
		self.assertEqual(self.created[0][0], legacy)
		self.assertIn('from the working directory', err)

	def test_missing_credentials_stop_before_a_client(self):
		code, _, err = self.collect()
		self.assertEqual(code, 2)
		self.assertIn('Missing app credentials: api_id, api_hash, phone', err)
		self.assertIn('auth login', err)
		self.assertEqual(self.created, [])
		self.assertFalse(os.path.exists(self.out))

	def test_invalid_saved_credentials_stop_before_a_client(self):
		from telegram_tracker.credentials import protection

		protection.ensure_private_dir(self.home)
		path = store.store_path(self.home)
		with open(path, 'w', encoding='utf-8') as fh:
			json.dump({'schema_version': 1, **VALID, 'api_id': 'S3NTINEL'}, fh)
		protection.protect(path)
		code, _, err = self.collect()
		self.assertEqual(code, 2)
		self.assertIn('api_id must be a positive integer', err)
		self.assertEqual(self.created, [])
		self.assert_no_secrets()

	def test_explicit_config_file(self):
		path = os.path.join(self.tmp, 'creds.ini')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write(f'[Telegram API credentials]\napi_id = {API_ID}\napi_hash = {API_HASH}\nphone = {PHONE}\n')
		self.save({'api_id': 1112223, 'api_hash': 'f' * 32, 'phone': '+990000OTHER'})
		code, _, _ = self.collect('--config', path)
		self.assertEqual((code, self.created[0][1:]), (0, (API_ID, API_HASH)))
		self.assert_no_secrets(path)

	def test_explicit_session_and_environment_session(self):
		os.environ.update(env_values(VALID))
		code, _, _ = self.collect('--session', 'mine')
		self.assertEqual((code, self.created[-1][0]), (0, os.path.join(self.cwd, 'mine.session')))
		os.environ['TELEGRAM_TRACKER_SESSION'] = os.path.join(self.tmp, 'env')
		code, _, _ = self.collect(legacy=True)
		self.assertEqual((code, self.created[-1][0]), (0, os.path.join(self.tmp, 'env.session')))

	def test_session_inside_the_output_folder_is_refused(self):
		os.environ.update(env_values(VALID))
		code, _, err = self.collect('--session', os.path.join(self.out, 'x'))
		self.assertEqual(code, 2)
		self.assertIn('must not be inside the --output folder', err)
		self.assertEqual(self.created, [])

	def test_output_inside_the_application_home_is_refused(self):
		import tempfile

		os.environ.update(env_values(VALID))
		os.makedirs(self.home)
		elsewhere = tempfile.TemporaryDirectory()
		self.addCleanup(elsewhere.cleanup)
		session = os.path.join(elsewhere.name, 's')
		for out in (os.path.join(self.home, 'data'), self.tmp):
			with self.subTest(out=out):
				code, _, err = self.run_main(
					['collect', '--telegram-channel', 'alpha_fx', '-o', out, '--session', session]
				)
				self.assertEqual(code, 2)
				self.assertIn('outside the application home', err)
		self.assertEqual(self.created, [])

	def test_a_symlink_to_the_session_file_shares_its_lease(self):
		os.environ.update(env_values(VALID))
		os.makedirs(self.home)
		alias = os.path.join(self.tmp, 'alias.session')
		try:
			os.symlink(self.home_session, alias)
		except (OSError, NotImplementedError) as exc:
			self.skipTest(f'cannot create a file symlink here ({type(exc).__name__})')
		try:
			with Lease(self.home_session, 'The session'):
				code, _, err = self.collect('--session', alias)
			self.assertEqual(code, 1)
			self.assertIn('in use by another process', err)
			self.assertEqual(self.created, [])
		finally:
			os.remove(alias)

	def test_an_alias_of_the_session_shares_its_lease(self):
		os.environ.update(env_values(VALID))
		os.makedirs(self.home)
		alias = os.path.join(self.tmp, 'alias')
		if os.name == 'nt':
			import _winapi

			_winapi.CreateJunction(self.home, alias)
		else:
			os.symlink(self.home, alias)
		try:
			with Lease(self.home_session, 'The session'):
				code, _, err = self.collect('--session', os.path.join(alias, 'session_file'))
			self.assertEqual(code, 1)
			self.assertIn('in use by another process', err)
			self.assertEqual(self.created, [])
		finally:
			os.rmdir(alias) if os.name == 'nt' else os.remove(alias)

	def test_a_case_variant_shares_the_lease_only_where_it_is_the_same_file(self):
		os.environ.update(env_values(VALID))
		os.makedirs(self.home)
		variant = os.path.join(self.home, 'SESSION_FILE')
		with Lease(self.home_session, 'The session'):
			code, _, err = self.collect('--session', variant)
		if ignores_case(self.home):
			self.assertEqual(code, 1)
			self.assertIn('in use by another process', err)
			self.assertEqual(self.created, [])
		else:
			# another file on a case-sensitive file system, so another session
			self.assertEqual(code, 0, err)
			self.assertEqual([path for path, *_ in self.created], [variant + '.session'])

	def test_held_session_lease_blocks_before_any_client(self):
		os.environ.update(env_values(VALID))
		os.makedirs(self.home)
		with Lease(self.home_session, 'The session'):
			code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn('in use by another process', err)
		self.assertEqual(self.created, [])
		self.assertFalse(os.path.exists(self.out))

	def test_lease_is_held_while_the_client_exists(self):
		os.environ.update(env_values(VALID))
		held = []
		real_connect = self.client.connect

		async def connect():
			held.append(os.path.exists(self.home_session + '.lock'))
			await real_connect()

		self.client.connect = connect
		self.assertEqual(self.collect()[0], 0)
		self.assertEqual(held, [True])
		self.assertFalse(os.path.exists(self.home_session + '.lock'))

	def test_injected_clients_skip_credential_resolution(self):
		code, _, _ = self.run_main(
			['collect', '--telegram-channel', 'alpha_fx', '-o', self.out],
			client_factory=lambda: self.client
		)
		self.assertEqual(code, 0)
		self.assertEqual(self.created, [])


if __name__ == '__main__':
	unittest.main()
