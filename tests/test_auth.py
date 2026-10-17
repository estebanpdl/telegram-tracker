# -*- coding: utf-8 -*-
'''
Interactive login with two-step verification, against the
fake client and scripted prompts. Sentinel secrets must never appear in
output, error messages, or written files.
'''

# import modules
import asyncio
import contextlib
import getpass
import io
import os
import signal
import tempfile
import unittest
import warnings

from unittest import mock

import offline_guard

from telethon import errors

from api import auth
from api.auth import AuthenticationError, ensure_authorized
from fakes import FIXTURE_CODE, FIXTURE_PHONE, FakeTelegramClient, ScriptedPrompt

import collector


PASSWORD = 'S3NTINEL-cloud-password'
WRONG_PASSWORD = 'S3NTINEL-wrong-password'
WRONG_CODE = '99999'


class AuthTestCase(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		self.out = io.StringIO()

	async def login(self, client, code_prompt=None, password_prompt=None):
		self.code_prompt = code_prompt or ScriptedPrompt()
		self.password_prompt = password_prompt or ScriptedPrompt()
		with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.out):
			await ensure_authorized(
				client,
				FIXTURE_PHONE,
				code_prompt=self.code_prompt,
				password_prompt=self.password_prompt
			)

	def assert_no_secrets(self, *texts):
		for text in texts + (self.out.getvalue(),):
			for secret in (PASSWORD, WRONG_PASSWORD, FIXTURE_CODE, WRONG_CODE, FIXTURE_PHONE):
				self.assertNotIn(secret, text)


class AuthorizedSessionTests(AuthTestCase):

	async def test_authorized_session_skips_every_prompt(self):
		client = FakeTelegramClient(authorized=True)
		await self.login(client)
		self.assertEqual(client.call_names(), ['is_user_authorized'])
		self.assertEqual((self.code_prompt.count, self.password_prompt.count), (0, 0))


class CodeLoginTests(AuthTestCase):

	async def test_code_only_sign_in(self):
		client = FakeTelegramClient(authorized=False)
		await self.login(client, code_prompt=ScriptedPrompt(FIXTURE_CODE))
		self.assertEqual(client.calls[1:], [
			('send_code_request', {'phone': FIXTURE_PHONE, 'flood_sleep_threshold': 0}),
			('sign_in_code', {'phone': FIXTURE_PHONE, 'code': FIXTURE_CODE, 'flood_sleep_threshold': 0}),
		])
		self.assertTrue(client.authorized)
		self.assertEqual(self.code_prompt.prompts, [auth.CODE_PROMPT])
		self.assertEqual(self.password_prompt.count, 0)
		self.assertEqual(client.flood_sleep_threshold, 60)
		self.assert_no_secrets()

	async def test_code_is_trimmed(self):
		client = FakeTelegramClient(authorized=False)
		await self.login(client, code_prompt=ScriptedPrompt(f' {FIXTURE_CODE}\n'))
		self.assertTrue(client.authorized)

	async def test_invalid_code_is_retried_within_the_limit(self):
		client = FakeTelegramClient(authorized=False)
		await self.login(client, code_prompt=ScriptedPrompt(WRONG_CODE, '', FIXTURE_CODE))
		self.assertTrue(client.authorized)
		self.assertEqual(client.call_names().count('sign_in_code'), 2)
		self.assertEqual(client.call_names().count('send_code_request'), 1)

	async def test_three_invalid_codes_stop_cleanly(self):
		client = FakeTelegramClient(authorized=False)
		with self.assertRaises(AuthenticationError) as ctx:
			await self.login(client, code_prompt=ScriptedPrompt(WRONG_CODE, WRONG_CODE, WRONG_CODE))
		self.assertIn('3 attempts', str(ctx.exception))
		self.assertEqual(client.call_names().count('sign_in_code'), 3)
		# no chained Telethon error (its request would carry the code)
		self.assertIsNone(ctx.exception.__context__)
		self.assertFalse(client.authorized)
		self.assert_no_secrets(str(ctx.exception))

	async def test_expired_code_is_not_retried(self):
		client = FakeTelegramClient(authorized=False)
		client.script('sign_in_code', errors.PhoneCodeExpiredError(request=None))
		with self.assertRaisesRegex(AuthenticationError, 'expired'):
			await self.login(client, code_prompt=ScriptedPrompt(FIXTURE_CODE))
		self.assertEqual(self.code_prompt.count, 1)


class TwoStepLoginTests(AuthTestCase):

	async def test_code_then_password_completes_sign_in_on_the_same_client(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		await self.login(
			client,
			code_prompt=ScriptedPrompt(FIXTURE_CODE),
			password_prompt=ScriptedPrompt(PASSWORD)
		)
		self.assertTrue(client.authorized)
		self.assertEqual(client.call_names(), [
			'is_user_authorized', 'send_code_request', 'sign_in_code', 'sign_in_password'
		])
		self.assertEqual(client.calls[-1][1], {'password_matched': True, 'flood_sleep_threshold': 0})
		self.assertEqual(self.password_prompt.prompts, [auth.PASSWORD_PROMPT])
		self.assertIn('Two-step verification is enabled', self.out.getvalue())
		self.assert_no_secrets()

	async def test_invalid_password_is_retried_within_the_limit(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		await self.login(
			client,
			code_prompt=ScriptedPrompt(FIXTURE_CODE),
			password_prompt=ScriptedPrompt(WRONG_PASSWORD, '', PASSWORD)
		)
		self.assertTrue(client.authorized)
		self.assertEqual(client.call_names().count('sign_in_password'), 2)
		self.assertEqual(client.call_names().count('sign_in_code'), 1)
		self.assert_no_secrets()

	async def test_three_invalid_passwords_stop_cleanly(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		with self.assertRaises(AuthenticationError) as ctx:
			await self.login(
				client,
				code_prompt=ScriptedPrompt(FIXTURE_CODE),
				password_prompt=ScriptedPrompt(WRONG_PASSWORD, WRONG_PASSWORD, WRONG_PASSWORD)
			)
		self.assertIn('3 attempts', str(ctx.exception))
		self.assertEqual(client.call_names().count('sign_in_password'), 3)
		self.assertFalse(client.authorized)
		self.assert_no_secrets(str(ctx.exception), repr(ctx.exception))

	async def test_password_is_not_passed_to_the_code_prompt(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		await self.login(
			client,
			code_prompt=ScriptedPrompt(FIXTURE_CODE),
			password_prompt=ScriptedPrompt(PASSWORD)
		)
		self.assertEqual(self.code_prompt.prompts, [auth.CODE_PROMPT])


class UnavailableAndCancelledInputTests(AuthTestCase):

	async def test_eof_at_code_prompt(self):
		client = FakeTelegramClient(authorized=False)
		with self.assertRaisesRegex(AuthenticationError, 'input is unavailable'):
			await self.login(client, code_prompt=ScriptedPrompt(EOFError))
		self.assertNotIn('sign_in_code', client.call_names())

	async def test_eof_at_password_prompt(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		with self.assertRaisesRegex(AuthenticationError, 'input is unavailable'):
			await self.login(
				client,
				code_prompt=ScriptedPrompt(FIXTURE_CODE),
				password_prompt=ScriptedPrompt(EOFError)
			)
		self.assertNotIn('sign_in_password', client.call_names())

	async def test_interrupt_at_either_prompt_propagates(self):
		for kwargs in (
			{'code_prompt': ScriptedPrompt(KeyboardInterrupt)},
			{'code_prompt': ScriptedPrompt(FIXTURE_CODE), 'password_prompt': ScriptedPrompt(KeyboardInterrupt)},
		):
			with self.subTest(prompt=list(kwargs)[-1]):
				client = FakeTelegramClient(authorized=False, password=PASSWORD)
				with self.assertRaises(KeyboardInterrupt):
					await self.login(client, **kwargs)
				self.assertEqual(client.flood_sleep_threshold, 60)

	async def test_prompts_run_with_the_default_sigint_handler(self):
		before = signal.getsignal(signal.SIGINT)
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		await self.login(
			client,
			code_prompt=ScriptedPrompt(FIXTURE_CODE),
			password_prompt=ScriptedPrompt(PASSWORD)
		)
		self.assertEqual(
			self.code_prompt.sigint_handlers + self.password_prompt.sigint_handlers,
			[signal.default_int_handler] * 2
		)
		self.assertIs(signal.getsignal(signal.SIGINT), before)

	async def test_cancellation_during_login_request(self):
		client = FakeTelegramClient(authorized=False)
		started = asyncio.Event()

		async def block():
			started.set()
			await asyncio.Event().wait()

		client.script('send_code_request', block)
		task = asyncio.create_task(self.login(client))
		await started.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertEqual(client.flood_sleep_threshold, 60)


class LoginRateLimitTests(AuthTestCase):

	async def test_flood_wait_on_code_request_is_fatal_and_not_retried(self):
		client = FakeTelegramClient(authorized=False)
		client.script('send_code_request', errors.FloodWaitError(request=None, capture=420))
		with self.assertRaises(AuthenticationError) as ctx:
			await self.login(client)
		self.assertIn('wait 420 seconds', str(ctx.exception))
		self.assertTrue(ctx.exception.__suppress_context__)
		self.assertEqual(client.call_names(), ['is_user_authorized', 'send_code_request'])
		self.assertEqual(client.calls[1][1]['flood_sleep_threshold'], 0)
		self.assertEqual(client.flood_sleep_threshold, 60)

	async def test_flood_wait_on_sign_in_is_fatal_and_not_retried(self):
		for kind, kwargs in (
			('sign_in_code', {'code_prompt': ScriptedPrompt(FIXTURE_CODE)}),
			('sign_in_password', {
				'code_prompt': ScriptedPrompt(FIXTURE_CODE),
				'password_prompt': ScriptedPrompt(PASSWORD)
			}),
		):
			with self.subTest(step=kind):
				client = FakeTelegramClient(authorized=False, password=PASSWORD)
				client.script(kind, errors.FloodWaitError(request=None, capture=30))
				with self.assertRaisesRegex(AuthenticationError, 'rate-limited'):
					await self.login(client, **kwargs)
				self.assertEqual(client.call_names().count(kind), 1)
				self.assert_no_secrets()

	async def test_other_telegram_refusals_name_only_the_error_type(self):
		client = FakeTelegramClient(authorized=False)
		client.script('send_code_request', errors.PhoneNumberInvalidError(request=None))
		with self.assertRaisesRegex(AuthenticationError, r'\(PhoneNumberInvalidError\)'):
			await self.login(client)
		self.assert_no_secrets()


class PromptBoundaryTests(offline_guard.OfflineTestCase):

	def test_password_prompt_uses_getpass(self):
		with mock.patch('getpass.getpass', return_value=PASSWORD) as fake_getpass:
			self.assertEqual(auth.prompt_password(auth.PASSWORD_PROMPT), PASSWORD)
		fake_getpass.assert_called_once_with(auth.PASSWORD_PROMPT)

	def test_password_prompt_refuses_echo_fallback(self):
		def echoing_getpass(prompt):
			warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
			return PASSWORD

		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			with mock.patch('getpass.getpass', echoing_getpass):
				with self.assertRaisesRegex(AuthenticationError, 'without echo') as ctx:
					auth.prompt_password(auth.PASSWORD_PROMPT)
		self.assertNotIn(PASSWORD, str(ctx.exception))

	def test_code_prompt_uses_input(self):
		with mock.patch('builtins.input', return_value=FIXTURE_CODE) as fake_input:
			self.assertEqual(auth.prompt_code(auth.CODE_PROMPT), FIXTURE_CODE)
		fake_input.assert_called_once_with(auth.CODE_PROMPT)

	def test_login_has_no_password_parameter(self):
		import inspect

		params = inspect.signature(ensure_authorized).parameters
		self.assertEqual(
			[p for p in params if 'pass' in p.lower()],
			['password_prompt']
		)


class LoginInCollectionTests(offline_guard.OfflineAsyncTestCase):
	'''
	The login runs inside the collection lifecycle: one client, and no
	secret reaches output files.
	'''

	async def test_mfa_login_then_collection_writes_no_secrets(self):
		client = FakeTelegramClient(authorized=False, password=PASSWORD)
		code_prompt = ScriptedPrompt(FIXTURE_CODE)
		password_prompt = ScriptedPrompt(WRONG_PASSWORD, PASSWORD)
		captured = io.StringIO()
		with tempfile.TemporaryDirectory() as tmp:
			out = os.path.join(tmp, 'data')
			with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
				summary = await collector.run_collection(
					['alpha_fx'], out,
					client_factory=lambda: client,
					authorize=lambda c: ensure_authorized(
						c, FIXTURE_PHONE,
						code_prompt=code_prompt,
						password_prompt=password_prompt
					),
				)
			written = ''
			for d, _, files in os.walk(out):
				for name in files:
					with open(os.path.join(d, name), encoding='utf-8') as fh:
						written += fh.read()

		self.assertEqual(summary.succeeded, ['alpha_fx'])
		self.assertEqual((client.connect_count, client.disconnect_count), (1, 1))
		for secret in (PASSWORD, WRONG_PASSWORD, FIXTURE_CODE, FIXTURE_PHONE):
			self.assertNotIn(secret, captured.getvalue())
			self.assertNotIn(secret, written)


if __name__ == '__main__':
	unittest.main()
