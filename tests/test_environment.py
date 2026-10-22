# -*- coding: utf-8 -*-
'''
Environment readiness: interpreter, pinned dependency versions, and the
Telethon 1.x interfaces the collector and login flow rely on. Also pins the
two known library API removals the builders must avoid.
'''

# import modules
import importlib
import inspect
import os
import re
import sys
import unittest

from importlib import metadata

import offline_guard


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMPORT_NAMES = {
	'telethon': 'telethon',
	'pandas': 'pandas',
	'openpyxl': 'openpyxl',
	'tqdm': 'tqdm',
	'networkx': 'networkx',
	'matplotlib': 'matplotlib',
	'python-louvain': 'community',
	'tldextract': 'tldextract',
}


def read_pins():
	'''
	Parse `name==version` lines from requirements.txt.
	'''
	pins = {}
	with open(os.path.join(ROOT, 'requirements.txt'), encoding='utf-8') as fh:
		for line in fh:
			line = line.split('#', 1)[0].strip()
			if line:
				name, version = re.fullmatch(r'([A-Za-z0-9_.-]+)==(\S+)', line).groups()
				pins[name.lower()] = version
	return pins


class EnvironmentTests(offline_guard.OfflineTestCase):

	def test_interpreter_is_registered_python_314(self):
		self.assertGreaterEqual(sys.version_info[:2], (3, 14))
		self.assertIn('telegram-tracker', sys.prefix.replace('\\', '/'))

	def test_every_requirement_is_pinned_and_installed_at_that_version(self):
		pins = read_pins()
		self.assertEqual(set(pins), set(IMPORT_NAMES))
		for name, version in pins.items():
			with self.subTest(package=name):
				self.assertEqual(metadata.version(name), version)
				importlib.import_module(IMPORT_NAMES[name])

	def test_telethon_stays_on_major_version_1(self):
		import telethon

		self.assertEqual(telethon.__version__.split('.')[0], '1')


class TelethonInterfaceTests(offline_guard.OfflineTestCase):
	'''
	The login flow depends on these Telethon 1.45 interfaces.
	'''

	def test_password_challenge_interface(self):
		from telethon import errors
		from telethon.client.auth import AuthMethods

		for name in (
			'SessionPasswordNeededError',
			'PasswordHashInvalidError',
			'PhoneCodeInvalidError',
			'PhoneCodeExpiredError',
			'FloodWaitError',
			'ChannelPrivateError',
		):
			self.assertTrue(issubclass(getattr(errors, name), errors.RPCError), name)

		# the offline guard shadows TelegramClient.sign_in; inspect the original
		params = inspect.signature(AuthMethods.sign_in).parameters
		self.assertEqual(params['password'].kind, inspect.Parameter.KEYWORD_ONLY)
		self.assertIn('code', params)

	def test_flood_sleep_threshold_is_configurable(self):
		from telethon.client.telegrambaseclient import TelegramBaseClient

		# the offline guard shadows TelegramClient.__init__; inspect the original
		params = inspect.signature(TelegramBaseClient.__init__).parameters
		self.assertEqual(params['flood_sleep_threshold'].default, 60)
		self.assertIsInstance(TelegramBaseClient.flood_sleep_threshold, property)


class KnownLibraryRemovalTests(offline_guard.OfflineTestCase):
	'''
	Characterize API removals the offline builders used to depend on.
	These tests flag a dependency change, and
	the source check keeps the removed calls from coming back.
	'''

	def test_project_code_avoids_the_removed_calls(self):
		import glob

		sources = [
			p for p in glob.glob(os.path.join(ROOT, '**', '*.py'), recursive=True)
			if os.sep + 'tests' + os.sep not in p
		]
		self.assertTrue(sources)
		for path in sources:
			with open(path, encoding='utf-8') as fh:
				text = fh.read()
			with self.subTest(path=os.path.relpath(path, ROOT)):
				self.assertNotIn('infer_datetime_format', text)
				self.assertNotIn('get_cmap', text)

	def test_pandas_3_rejects_infer_datetime_format(self):
		import pandas as pd

		with self.assertRaises(TypeError):
			pd.to_datetime(['2026-01-01'], infer_datetime_format=True)

	def test_matplotlib_311_has_no_cm_get_cmap(self):
		import matplotlib
		import matplotlib.cm as cm

		self.assertFalse(hasattr(cm, 'get_cmap'))
		self.assertIn('viridis', matplotlib.colormaps)


if __name__ == '__main__':
	unittest.main()
