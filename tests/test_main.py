# -*- coding: utf-8 -*-
'''
Legacy collection CLI (`python main.py ...`) driven in-process with the
fake client. Argument errors must exit before credentials are read; the
offline guard fails any test that touches config/config.ini.
'''

# import modules
import contextlib
import functools
import io
import os
import tempfile
import unittest

from unittest import mock

import offline_guard
import virtual_time

from api.auth import ensure_authorized
from fakes import FIXTURE_CODE, FIXTURE_PHONE, FakeTelegramClient, ScriptedPrompt

import cli
import collector
import main


PASSWORD = 'S3NTINEL-cli-password'


class MainTestCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(self.tmp, 'data')
		self.factory_calls = 0
		virtual = mock.patch.object(cli, 'asyncio_run', virtual_time.run)
		virtual.start()
		self.addCleanup(virtual.stop)
		self.client = FakeTelegramClient()

	def factory(self):
		self.factory_calls += 1
		return self.client

	def run_main(self, argv, **kwargs):
		stdout, stderr = io.StringIO(), io.StringIO()
		kwargs.setdefault('client_factory', self.factory)
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = main.main(argv, **kwargs)
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def write_batch(self, *lines):
		path = os.path.join(self.tmp, 'channels.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write(''.join(f'{line}\n' for line in lines))
		return path


class ArgumentTests(MainTestCase):

	def test_help_is_offline(self):
		code, out, _ = self.run_main(['--help'])
		self.assertEqual(code, 0)
		for flag in ('--telegram-channel', '--batch-file', '--min-id', '--output'):
			self.assertIn(flag, out)
		self.assertEqual(self.factory_calls, 0)

	def test_help_works_without_injected_client(self):
		code, _, _ = self.run_main(['--help'], client_factory=None)
		self.assertEqual(code, 0)

	def test_target_selector_errors_exit_2_before_credentials(self):
		for argv in ([], ['--telegram-channel', 'a_fx', '--batch-file', 'b.txt'], ['--min-id', '5']):
			with self.subTest(argv=argv):
				code, _, err = self.run_main(argv, client_factory=None)
				self.assertEqual(code, 2)
				self.assertIn('--telegram-channel', err)

	def test_unreadable_batch_file_exits_2_before_credentials(self):
		missing = os.path.join(self.tmp, 'missing.txt')
		code, _, err = self.run_main(['--batch-file', missing], client_factory=None)
		self.assertEqual(code, 2)
		self.assertIn('cannot read --batch-file', err)

	def test_no_password_argument_exists(self):
		top, subparsers = cli.build_parsers()
		for parser in (top, cli.build_legacy_parser(), *subparsers.values()):
			self.assertFalse([a for a in parser._actions if 'pass' in (a.dest or '')])
		for argv in (['--telegram-channel', 'alpha_fx', '--password', PASSWORD],
				['collect', '--telegram-channel', 'alpha_fx', '--password', PASSWORD]):
			code, _, _ = self.run_main(argv, client_factory=None)
			self.assertEqual(code, 2)


class CollectionTests(MainTestCase):

	def test_single_channel(self):
		code, out, err = self.run_main(['--telegram-channel', 'alpha_fx', '-o', self.out])
		self.assertEqual((code, err), (0, ''))
		self.assertIn('Init program at', out)
		self.assertIn('End program at', out)
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')))
		self.assertEqual((self.client.connect_count, self.client.disconnect_count), (1, 1))

	def test_batch_file_with_equals_form_and_failures(self):
		batch = self.write_batch('alpha_fx', 'missing_fx')
		code, _, err = self.run_main([f'--batch-file={batch}', f'--output={self.out}/'])
		self.assertEqual(code, 1)
		self.assertIn('1 of 2 targets failed: missing_fx', err)
		self.assertTrue(os.path.isdir(os.path.join(self.out, 'alpha_fx')))

	def test_metadata_only_and_min_id_reach_the_collector(self):
		code, _, _ = self.run_main([
			'--telegram-channel', 'alpha_fx', '-o', self.out,
			'--limit-download-to-channel-metadata'
		])
		self.assertEqual(code, 0)
		self.assertFalse(os.path.exists(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')))

		self.client = FakeTelegramClient()
		code, _, _ = self.run_main(['--telegram-channel', 'alpha_fx', '-o', self.out, '--min-id', '6'])
		self.assertEqual(code, 0)
		history = [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']
		self.assertEqual({r['min_id'] for r in history}, {6})

	def test_default_output_folder_is_relative_to_working_directory(self):
		cwd = os.getcwd()
		os.chdir(self.tmp)
		self.addCleanup(os.chdir, cwd)
		code, _, _ = self.run_main(['--telegram-channel', 'alpha_fx'])
		self.assertEqual(code, 0)
		self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'output', 'data', 'counter.csv')))

	def test_empty_batch_succeeds(self):
		batch = self.write_batch()
		code, _, err = self.run_main(['--batch-file', batch, '-o', self.out])
		self.assertEqual((code, err), (0, ''))
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'collected_chats.csv')))


class LoginThroughCliTests(MainTestCase):
	'''
	Both legacy collection forms share the implicit login.
	'''

	def authorize(self, *code_answers, password_answers=()):
		return functools.partial(
			ensure_authorized,
			phone=FIXTURE_PHONE,
			code_prompt=ScriptedPrompt(*code_answers),
			password_prompt=ScriptedPrompt(*password_answers)
		)

	def test_mfa_login_for_both_collection_forms(self):
		batch = self.write_batch('alpha_fx')
		for argv in (['--telegram-channel', 'alpha_fx'], ['--batch-file', batch]):
			with self.subTest(argv=argv[0]):
				self.client = FakeTelegramClient(authorized=False, password=PASSWORD)
				code, out, err = self.run_main(
					argv + ['-o', self.out],
					authorize=self.authorize(FIXTURE_CODE, password_answers=(PASSWORD,))
				)
				self.assertEqual(code, 0)
				self.assertTrue(self.client.authorized)
				self.assertNotIn(PASSWORD, out + err)

	def test_failed_login_exits_1_with_clean_message(self):
		self.client = FakeTelegramClient(authorized=False, password=PASSWORD)
		code, out, err = self.run_main(
			['--telegram-channel', 'alpha_fx', '-o', self.out],
			authorize=self.authorize(FIXTURE_CODE, password_answers=('bad-1', 'bad-2', 'bad-3'))
		)
		self.assertEqual(code, 1)
		self.assertIn('> Login failed: The two-step verification password was not accepted', err)
		self.assertNotIn('Traceback', err)
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertFalse(os.path.exists(self.out))

	def test_interrupt_at_prompt_exits_130(self):
		self.client = FakeTelegramClient(authorized=False)
		code, _, err = self.run_main(
			['--telegram-channel', 'alpha_fx', '-o', self.out],
			authorize=self.authorize(KeyboardInterrupt)
		)
		self.assertEqual(code, 130)
		self.assertIn('Interrupted', err)
		self.assertEqual(self.client.disconnect_count, 1)


if __name__ == '__main__':
	unittest.main()
