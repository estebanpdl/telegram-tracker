# -*- coding: utf-8 -*-
'''
Shared setup for the auth command and collection credential tests: a
temporary application home and working directory, no credential
variables, a recording replacement for telegram_tracker.api.create_client
that returns a fake client, injected app-credential prompts, and a scan of
every output and file for the synthetic secret values.
'''

# import modules
import contextlib
import functools
import io
import os
import tempfile

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient, ScriptedPrompt

from telegram_tracker import api, cli
from telegram_tracker.cli import auth as cli_auth
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources, store


API_ID = 7654321
API_HASH = 'S3NTINEL0a1b2c3d4e5f60718293a4b5c'
PHONE = '+990000S3NTINEL'
PASSWORD = 'S3NTINEL-cloud-password'
VALID = {'api_id': API_ID, 'api_hash': API_HASH, 'phone': PHONE}
SECRETS = (str(API_ID), API_HASH, PHONE, PASSWORD)
OTHER = {'api_id': 1112223, 'api_hash': 'f' * 32, 'phone': '+990000OTHER'}

CREDENTIAL_VARS = tuple(sources.ENV_VARS.values())


def env_values(values):
	return {sources.ENV_VARS[k]: str(v) for k, v in values.items()}


class CredentialCase(offline_guard.OfflineTestCase):
	'''
	Runs `main(argv)` from a fresh working directory with a temporary
	home. `self.client` is what create_client returns; `self.created`
	records each create_client call as (session path, api_id, api_hash).
	'''

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.home = os.path.join(self.tmp, 'home')
		self.cwd = os.path.join(self.tmp, 'work')
		os.mkdir(self.cwd)
		self.home_session = os.path.join(self.home, 'session_file.session')

		environ = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
		environ[homes.HOME_ENV] = self.home
		environ.pop(homes.SESSION_ENV, None)
		patcher = mock.patch.dict(os.environ, environ, clear=True)
		patcher.start()
		self.addCleanup(patcher.stop)

		previous = os.getcwd()
		os.chdir(self.cwd)
		self.addCleanup(os.chdir, previous)

		self.client = FakeTelegramClient()
		self.created = []

		def create_client(session_file, api_id, api_hash):
			self.created.append((session_file, api_id, api_hash))
			return self.client

		self.patch(api, 'create_client', create_client)
		self.text_prompt = ScriptedPrompt()
		self.secret_prompt = ScriptedPrompt()
		self.code_prompt = ScriptedPrompt()
		self.answers = []
		self.patch(cli_auth, 'prompt_text', self.text_prompt)
		self.patch(cli_auth, 'prompt_secret', self.secret_prompt)
		self.patch(cli_auth, 'confirm', self.confirm)
		self.existing_files = set()
		self.outputs = []

	def patch(self, owner, name, value):
		patcher = mock.patch.object(owner, name, value)
		patcher.start()
		self.addCleanup(patcher.stop)

	def confirm(self, question):
		self.outputs.append(question)
		if not self.answers:
			raise AssertionError(f'unexpected confirmation: {question}')
		return self.answers.pop(0)

	def pretend_files(self, *paths):
		'''
		Make session existence checks see `paths` (the guard forbids
		creating real .session files).
		'''
		self.existing_files.update(os.path.normcase(os.path.abspath(p)) for p in paths)
		real_select = homes.select_session
		real_isfile = os.path.isfile

		def isfile(path):
			return os.path.normcase(os.path.abspath(path)) in self.existing_files or real_isfile(path)

		self.patch(homes, 'select_session', functools.partial(real_select, isfile=isfile))
		self.patch(cli_auth.os.path, 'isfile', isfile)

	def run_main(self, argv, **kwargs):
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
				mock.patch('builtins.input', self.code_prompt):
			try:
				code = cli.main(argv, **kwargs)
			except SystemExit as exc:
				code = exc.code
		out, err = stdout.getvalue(), stderr.getvalue()
		self.outputs.extend((out, err))
		return code, out, err

	def save(self, values=VALID):
		store.save_store(self.home, values)

	def stored(self):
		return store.read_store(self.home)

	def assert_no_secrets(self, *skip):
		'''
		No synthetic secret in any output, prompt text, or file under the
		temporary directory except credentials.json.
		'''
		texts = list(self.outputs)
		texts += self.text_prompt.prompts + self.secret_prompt.prompts + self.code_prompt.prompts
		for root, _, files in os.walk(self.tmp):
			for name in files:
				path = os.path.join(root, name)
				if name == store.STORE_NAME or path in skip:
					continue
				with open(path, encoding='utf-8', errors='replace') as fh:
					texts.append(fh.read())
		for text in texts:
			for secret in SECRETS:
				self.assertNotIn(secret, text)
