# -*- coding: utf-8 -*-
'''
Local session failures through the three entry points, in fresh
interpreters with the offline guard installed: `python main.py`, `python
-m telegram_tracker`, and the `tgtracker` command (cli.command_main). An
occupied session lease and a session folder that does not exist stop
`collect` and `auth login` with the same exit code and message in every
form, before any client exists, and change no file.
'''

# import modules
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from auth_support import CREDENTIAL_VARS, VALID, env_values
from builder_data import ROOT, TESTS


SRC = os.path.join(ROOT, 'src')
FORMS = ('main.py', 'module', 'tgtracker')
TIMES = re.compile(r'(Init|End) program at .*')
HELD = {'pid': 4242, 'started_at': '2026-01-01T00:00:00+00:00'}

PROBE = '''
import json, runpy, sys
sys.path[:0] = [{tests!r}]
import offline_guard
code = 0
try:
	if {form!r} == 'main.py':
		sys.argv = [{script!r}] + {argv!r}
		runpy.run_path({script!r}, run_name='__main__')
	else:
		sys.path.insert(0, {src!r})
		if {form!r} == 'module':
			sys.argv = [{module_main!r}] + {argv!r}
			runpy.run_module('telegram_tracker', run_name='__main__', alter_sys=True)
		else:
			sys.argv = ['tgtracker'] + {argv!r}
			from telegram_tracker.cli import command_main
			code = command_main()
except SystemExit as exc:
	code = exc.code
print('REPORT ' + json.dumps({{'code': code, 'violations': offline_guard.VIOLATIONS}}))
'''


def run_form(form, argv, cwd, env):
	'''
	(report, stdout without the report, stderr) of one entry point.
	'''
	code = PROBE.format(
		tests=TESTS, src=SRC, form=form, script=os.path.join(ROOT, 'main.py'),
		module_main=os.path.join(SRC, 'telegram_tracker', '__main__.py'), argv=list(argv)
	)
	completed = subprocess.run(
		[sys.executable, '-I', '-c', textwrap.dedent(code)],
		cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120
	)
	lines = completed.stdout.splitlines()
	reports = [line for line in lines if line.startswith('REPORT ')]
	if not reports:
		raise AssertionError(f'probe failed: {completed.stderr[-2000:]}')
	other = '\n'.join(line for line in lines if not line.startswith('REPORT '))
	return json.loads(reports[-1][len('REPORT '):]), TIMES.sub('', other), completed.stderr


class EntryPointDiagnosticsTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.home = os.path.join(self.tmp, 'home')
		self.cwd = os.path.join(self.tmp, 'work')
		self.out = os.path.join(self.tmp, 'out')
		os.makedirs(self.home)
		os.mkdir(self.cwd)
		self.session = os.path.join(self.home, 'session_file.session')
		self.env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
		self.env.update(env_values(VALID))
		self.env['TELEGRAM_TRACKER_HOME'] = self.home
		self.env['TELEGRAM_TRACKER_SESSION'] = self.session
		self.env.pop('TELEGRAM_TRACKER_PROFILE', None)

	def files(self):
		found = {}
		for root, _, names in os.walk(self.tmp):
			for name in names:
				path = os.path.join(root, name)
				with open(path, 'rb') as fh:
					found[path] = fh.read()
		return found

	def check_forms(self, argv, expected):
		results = []
		for form in FORMS:
			with self.subTest(form=form):
				before = self.files()
				report, out, err = run_form(form, argv, self.cwd, self.env)
				self.assertEqual((report['code'], report['violations']), (1, []))
				self.assertEqual(err.splitlines()[-1], expected)
				self.assertNotIn('Traceback', err)
				self.assertEqual(self.files(), before)
				self.assertFalse(os.path.exists(self.out))
				results.append((out, err))
		self.assertEqual(len(set(results)), 1, results)

	def hold_lease(self):
		lock = self.session + '.lock'
		with open(lock, 'w', encoding='utf-8') as fh:
			json.dump(HELD, fh)
		return lock

	def test_occupied_lease(self):
		lock = self.hold_lease()
		expected = (
			f'> The session is in use by another process (pid {HELD["pid"]}, started '
			f'{HELD["started_at"]}). If none is running, an earlier run was interrupted: delete '
			f'{lock} and try again.'
		)
		for argv in (
				['collect', '--telegram-channel', 'alpha_fx', '-o', self.out],
				['auth', 'login', '--no-save']):
			with self.subTest(argv=argv):
				self.check_forms(argv, expected)
		self.assertTrue(os.path.exists(lock))

	def test_missing_session_folder(self):
		session = os.path.join(self.tmp, 'missing', 'session_file.session')
		expected = f'> cannot lock the session ({session}.lock: its folder does not exist)'
		for argv in (
				['collect', '--telegram-channel', 'alpha_fx', '-o', self.out, '--session', session],
				['auth', 'login', '--no-save', '--session', session]):
			with self.subTest(argv=argv):
				self.check_forms(argv, expected)
		self.assertFalse(os.path.exists(os.path.dirname(session)))


if __name__ == '__main__':
	unittest.main()
