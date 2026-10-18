# -*- coding: utf-8 -*-
'''
`auth login|status|logout|clear-credentials` with a fake client, injected
prompts, and a temporary application home. Saving rules: typed values are
saved after a successful login unless --no-save; values from the
environment or an INI file, or a different saved set, are saved only on
consent or --save; a failed login never writes. Every test scans outputs
and files for the synthetic secret values.
'''

# import modules
import os
import unittest

from telethon import errors

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from auth_support import API_HASH, API_ID, OTHER, PASSWORD, PHONE, VALID, CredentialCase, env_values
from fakes import FIXTURE_CODE, FakeTelegramClient

from telegram_tracker.cli import auth as cli_auth
from telegram_tracker.credentials import protection, store
from telegram_tracker.credentials.lease import Lease


class LoginTests(CredentialCase):

	def test_missing_telethon_returns_a_dependency_error(self):
		self.save()
		real_import = __import__

		def without_telethon(name, *args, **kwargs):
			if name == 'telegram_tracker.api.auth':
				raise ModuleNotFoundError("No module named 'telethon'", name='telethon')
			return real_import(name, *args, **kwargs)

		with unittest.mock.patch('builtins.__import__', side_effect=without_telethon):
			code, _, err = self.run_main(['auth', 'login', '--no-save'])
		self.assertEqual(code, 1)
		self.assertIn("missing dependency 'telethon'", err)
		self.assertEqual(self.created, [])
		self.assert_no_secrets()

	def type_credentials(self):
		self.text_prompt.answers += [str(API_ID), PHONE]
		self.secret_prompt.answers += [API_HASH]

	def test_first_login_asks_logs_in_and_saves_typed_values(self):
		self.client = FakeTelegramClient(authorized=False)
		self.type_credentials()
		self.code_prompt.answers.append(FIXTURE_CODE)
		code, out, err = self.run_main(['auth', 'login'])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.created, [(self.home_session, API_ID, API_HASH)])
		sent = [d for name, d in self.client.calls if name == 'send_code_request']
		self.assertEqual(sent[0]['phone'], PHONE)
		self.assertEqual(self.stored(), VALID)
		protection.check(self.home)
		protection.check(store.store_path(self.home))
		self.assertIn('api_id: prompt; api_hash: prompt; phone: prompt', out)
		self.assertIn('saved to', out)
		self.assertEqual((self.client.connect_count, self.client.disconnect_count), (1, 1))
		self.assertEqual(self.text_prompt.prompts, ['API ID: ', 'Phone number, international format: '])
		self.assertFalse(os.path.exists(self.home_session + '.lock'))
		self.assert_no_secrets()

	def test_no_save_logs_in_without_writing_credentials(self):
		self.type_credentials()
		code, out, _ = self.run_main(['auth', 'login', '--no-save'])
		self.assertEqual(code, 0)
		self.assertIsNone(self.stored())
		self.assertIn('not saved (--no-save)', out)
		self.assertNotIn('saved to', out)
		self.assert_no_secrets()

	def test_saved_credentials_and_authorized_session_ask_nothing(self):
		self.save()
		code, out, _ = self.run_main(['auth', 'login'])
		self.assertEqual(code, 0)
		self.assertEqual(self.created, [(self.home_session, API_ID, API_HASH)])
		self.assertEqual(self.text_prompt.count + self.secret_prompt.count + self.code_prompt.count, 0)
		self.assertNotIn('send_code_request', self.client.call_names())
		self.assertIn('unchanged', out)
		self.assert_no_secrets()

	def test_environment_values_are_saved_only_on_consent(self):
		os.environ.update(env_values(VALID))
		for answer, saved in ((False, None), (None, None), (True, VALID)):
			with self.subTest(answer=answer):
				store.clear_store(self.home)
				self.answers.append(answer)
				code, out, _ = self.run_main(['auth', 'login'])
				self.assertEqual(code, 0)
				self.assertEqual(self.stored(), saved)
				if answer is None:
					self.assertIn('--save', out)
		self.assertIn('api_id: environment', out)
		self.assert_no_secrets()

	def test_save_flag_saves_external_values_without_asking(self):
		os.environ.update(env_values(VALID))
		code, _, _ = self.run_main(['auth', 'login', '--save'])
		self.assertEqual((code, self.stored()), (0, VALID))

	def test_replacing_a_different_saved_set_needs_consent(self):
		self.save(OTHER)
		os.environ.update(env_values(VALID))
		self.answers.append(False)
		self.assertEqual(self.run_main(['auth', 'login'])[0], 0)
		self.assertEqual(self.stored(), OTHER)
		self.assertIn('Replace the credentials saved', self.outputs[-3])
		self.assertIn(
			'Changed: api_id (from environment), api_hash (from environment), phone (from environment).',
			self.outputs[-3]
		)
		self.answers.append(True)
		self.assertEqual(self.run_main(['auth', 'login'])[0], 0)
		self.assertEqual(self.stored(), VALID)

	def test_failed_login_never_saves(self):
		self.client = FakeTelegramClient(authorized=False)
		self.type_credentials()
		self.code_prompt.answers += ['00000', '00001', '00002']
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('Login failed', err)
		self.assertIsNone(self.stored())
		self.assertEqual(self.client.disconnect_count, 1)
		self.assert_no_secrets()

	def test_login_flood_wait_is_fatal_and_saves_nothing(self):
		self.client = FakeTelegramClient(authorized=False)
		self.client.script('send_code_request', errors.FloodWaitError(request=None, capture=30))
		self.type_credentials()
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('rate-limited', err)
		self.assertIsNone(self.stored())

	def test_two_step_password_is_never_stored_or_shown(self):
		self.client = FakeTelegramClient(authorized=False, password=PASSWORD)
		self.type_credentials()
		self.code_prompt.answers.append(FIXTURE_CODE)
		with unittest.mock.patch('getpass.getpass', return_value=PASSWORD):
			code, _, _ = self.run_main(['auth', 'login'])
		self.assertEqual(code, 0)
		self.assertEqual(self.stored(), VALID)
		with open(store.store_path(self.home), encoding='utf-8') as fh:
			self.assertNotIn(PASSWORD, fh.read())
		self.assert_no_secrets()

	def test_unavailable_input_stops_before_any_client(self):
		self.text_prompt.answers.append(EOFError)
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('input is unavailable', err)
		self.assertEqual(self.created, [])
		self.assertFalse(os.path.exists(self.home))

	def test_interrupt_at_a_prompt_exits_130(self):
		self.text_prompt.answers.append(KeyboardInterrupt)
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 130)
		self.assertEqual(self.created, [])

	def test_invalid_answers_are_retried_then_refused(self):
		self.text_prompt.answers += ['abc', '-1', str(API_ID), PHONE]
		self.secret_prompt.answers += [API_HASH]
		self.assertEqual(self.run_main(['auth', 'login'])[0], 0)
		self.assertEqual(self.stored(), VALID)

		store.clear_store(self.home)
		self.text_prompt.answers += ['x', 'y', 'z']
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('not accepted after 3 attempts', err)
		self.assertEqual(len(self.created), 1)

	def test_conflicting_save_switches_exit_2(self):
		code, _, err = self.run_main(['auth', 'login', '--save', '--no-save'])
		self.assertEqual(code, 2)
		self.assertIn('not allowed with', err)

	def test_unusable_home_fails_before_connecting(self):
		blocker = os.path.join(self.tmp, 'file')
		open(blocker, 'w').close()
		os.environ['TELEGRAM_TRACKER_HOME'] = os.path.join(blocker, 'home')
		self.type_credentials()
		code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('TELEGRAM_TRACKER_HOME', err)
		self.assertEqual(self.created, [])

	def test_save_failure_after_login_is_reported_separately(self):
		self.type_credentials()

		def failing_save(home, values):
			raise store.StoreError('cannot save credentials.json (disk full)')

		self.patch(cli_auth, 'save_store', failing_save)
		code, out, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('Logged in, but the credentials were not saved', err)
		self.assertIn('> Session:', out)

	def test_a_held_session_lease_blocks_login_before_any_client(self):
		self.save()
		os.makedirs(self.home, exist_ok=True)
		with Lease(self.home_session, 'The session'):
			code, _, err = self.run_main(['auth', 'login'])
		self.assertEqual(code, 1)
		self.assertIn('in use by another process', err)
		self.assertEqual(self.created, [])

	def test_legacy_session_in_the_working_directory_is_used_with_a_notice(self):
		self.save()
		legacy = os.path.join(self.cwd, 'session_file.session')
		self.pretend_files(legacy)
		code, out, _ = self.run_main(['auth', 'login'])
		self.assertEqual(code, 0)
		self.assertEqual(self.created[0][0], legacy)
		self.assertIn('from the working directory', out)

	def test_home_session_wins_over_the_legacy_one(self):
		self.save()
		self.pretend_files(os.path.join(self.cwd, 'session_file.session'), self.home_session)
		code, out, _ = self.run_main(['auth', 'login'])
		self.assertEqual((code, self.created[0][0]), (0, self.home_session))
		self.assertNotIn('working directory', out)


class StatusTests(CredentialCase):

	def test_offline_status_reports_labels_and_creates_nothing(self):
		code, out, err = self.run_main(['auth', 'status'])
		self.assertEqual((code, err), (0, ''))
		self.assertIn(f'Application home: {self.home} (TELEGRAM_TRACKER_HOME)', out)
		self.assertIn(f'Session: {self.home_session} (application home): missing', out)
		self.assertIn('Saved credentials:', out)
		self.assertIn(': absent', out)
		self.assertIn('api_id: missing; api_hash: missing; phone: missing', out)
		self.assertFalse(os.path.exists(self.home))
		self.assertEqual(self.created, [])

	def test_status_names_sources_never_values(self):
		self.save()
		os.environ['TELEGRAM_TRACKER_PHONE'] = '+990000OTHER'
		self.pretend_files(self.home_session)
		code, out, _ = self.run_main(['auth', 'status'])
		self.assertEqual(code, 0)
		self.assertIn('present; login not verified (offline)', out)
		self.assertIn('api_id: credentials.json; api_hash: credentials.json; phone: environment', out)
		self.assertNotIn('+990000OTHER', out)
		self.assert_no_secrets()

	def test_invalid_saved_credentials_exit_2(self):
		protection.ensure_private_dir(self.home)
		path = store.store_path(self.home)
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('{"schema_version": 9}')
		protection.protect(path)
		code, out, _ = self.run_main(['auth', 'status'])
		self.assertEqual(code, 2)
		self.assertIn('invalid', out)

	def test_check_needs_an_existing_session_and_never_sends_a_code(self):
		self.save()
		code, _, err = self.run_main(['auth', 'status', '--check'])
		self.assertEqual(code, 1)
		self.assertIn('never creates one', err)
		self.assertEqual(self.created, [])

		self.pretend_files(self.home_session)
		for authorized, expected in ((True, 0), (False, 1)):
			with self.subTest(authorized=authorized):
				self.client = FakeTelegramClient(authorized=authorized)
				code, out, _ = self.run_main(['auth', 'status', '--check'])
				self.assertEqual(code, expected)
				self.assertIn('Login: logged in' if authorized else 'Login: not logged in', out)
				self.assertEqual(self.client.call_names(), ['connect', 'is_user_authorized', 'disconnect'])

	def test_check_without_app_credentials_exits_2(self):
		self.pretend_files(self.home_session)
		code, _, err = self.run_main(['auth', 'status', '--check'])
		self.assertEqual(code, 2)
		self.assertIn('missing api_id, api_hash', err)


class LogoutTests(CredentialCase):

	def setUp(self):
		super().setUp()
		self.save()
		self.pretend_files(self.home_session)

	def test_no_session_is_a_no_op(self):
		self.existing_files.clear()
		code, out, _ = self.run_main(['auth', 'logout', '--yes'])
		self.assertEqual(code, 0)
		self.assertIn('nothing to log out', out)
		self.assertEqual(self.created, [])

	def test_declined_confirmation_changes_nothing(self):
		self.answers.append(False)
		self.assertEqual(self.run_main(['auth', 'logout'])[0], 1)
		self.assertEqual(self.created, [])

	def test_logout_revokes_and_keeps_saved_credentials(self):
		self.answers.append(True)
		code, out, _ = self.run_main(['auth', 'logout'])
		self.assertEqual(code, 0)
		self.assertIn('Logged out', out)
		self.assertEqual(self.client.call_names(), ['connect', 'is_user_authorized', 'log_out', 'disconnect'])
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertEqual(self.stored(), VALID)
		self.assertNotIn('send_code_request', self.client.call_names())

	def test_not_logged_in_session_has_nothing_to_revoke(self):
		self.client = FakeTelegramClient(authorized=False)
		code, out, _ = self.run_main(['auth', 'logout', '--yes'])
		self.assertEqual(code, 0)
		self.assertIn('nothing to revoke', out)
		self.assertEqual(self.client.disconnect_count, 1)

	def test_unconfirmed_logout_exits_1(self):
		async def refused():
			return False

		self.client.script('log_out', refused)
		code, _, err = self.run_main(['auth', 'logout', '--yes'])
		self.assertEqual(code, 1)
		self.assertIn('did not confirm', err)
		self.assertEqual(self.client.disconnect_count, 1)


class ClearCredentialsTests(CredentialCase):

	def test_nothing_saved_is_a_no_op(self):
		code, out, _ = self.run_main(['auth', 'clear-credentials', '--yes'])
		self.assertEqual(code, 0)
		self.assertIn('nothing to remove', out)

	def test_declined_confirmation_keeps_the_file(self):
		self.save()
		self.answers.append(False)
		self.assertEqual(self.run_main(['auth', 'clear-credentials'])[0], 1)
		self.assertEqual(self.stored(), VALID)

	def test_removes_only_the_credentials_file_and_names_what_remains(self):
		self.save()
		self.pretend_files(self.home_session)
		os.environ['TELEGRAM_TRACKER_API_ID'] = str(API_ID)
		self.answers.append(True)
		code, out, _ = self.run_main(['auth', 'clear-credentials'])
		self.assertEqual(code, 0)
		self.assertFalse(os.path.exists(store.store_path(self.home)))
		self.assertTrue(os.path.isdir(self.home))
		self.assertIn('TELEGRAM_TRACKER_API_ID', out)
		self.assertIn('stays logged in', out)
		self.assertEqual(self.created, [])
		self.assert_no_secrets()

	def test_corrupt_file_is_removed_without_reading_it(self):
		protection.ensure_private_dir(self.home)
		with open(store.store_path(self.home), 'w', encoding='utf-8') as fh:
			fh.write(f'not json {API_HASH}')
		code, out, err = self.run_main(['auth', 'clear-credentials', '--yes'])
		self.assertEqual((code, err), (0, ''))
		self.assertNotIn(API_HASH, out)
		self.assertFalse(os.path.exists(store.store_path(self.home)))

	def test_a_held_store_lease_fails(self):
		self.save()
		with Lease(store.store_path(self.home), 'The credentials file'):
			code, _, err = self.run_main(['auth', 'clear-credentials', '--yes'])
		self.assertEqual(code, 1)
		self.assertIn('in use by another process', err)
		self.assertEqual(self.stored(), VALID)


class AuthIsolationTests(offline_guard.OfflineTestCase):

	def test_help_and_builders_never_resolve_the_home(self):
		import contextlib
		import io

		from unittest import mock

		from telegram_tracker import cli
		from telegram_tracker.credentials import home as homes

		def refuse(*args, **kwargs):
			raise AssertionError('application home resolved')

		with mock.patch.object(homes, 'app_home', refuse), \
				contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
			for argv in (['--help'], ['auth', '--help'], ['auth', 'login', '--help'], ['collect', '--help']):
				with self.subTest(argv=argv):
					with self.assertRaises(SystemExit) as ctx:
						cli.main(argv)
					self.assertEqual(ctx.exception.code, 0)
			self.assertEqual(cli.main(['build-network', '-d', 'nowhere']), 1)


if __name__ == '__main__':
	unittest.main()
