# -*- coding: utf-8 -*-
'''
Entry points: the `tgtracker` command, `python -m telegram_tracker`, and the
source-checkout scripts parse the same arguments and reach the same
handlers; only the program name in help and errors differs. The scripts
run from a fresh checkout without installation.
'''

# import modules
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from builder_data import ROOT, TESTS

from telegram_tracker import cli
from telegram_tracker.cli import parsers


SRC = os.path.join(ROOT, 'src')
PROGS = (parsers.SCRIPT_PROG, parsers.COMMAND_PROG, parsers.MODULE_PROG)
TIMES = re.compile(r'(Init|End) program at \w{3} \w{3} +\d+ [\d:]{8} \d{4}')

# argv cases run through every entry point: help, overview, argument errors
# (exit 2 before credentials) and an offline builder data error (exit 1)
CASES = [
	[],
	['--help'],
	['collect', '--help'],
	['build-dataset', '--help'],
	['build-network', '--help'],
	['--telegram-channel', 'x', '--help'],
	['bogus'],
	['collect'],
	['collect', '--telegram-channel', '../x'],
	['collect', '--telegram-channel', 'a', '--concurrency', '11'],
	['--telegram-channel', 'a', '--update'],
	['build-network', '-d', 'nowhere'],
]

# a fresh interpreter (-I: no user site, no PYTHONPATH, no cwd on sys.path)
# that runs one entry point with the offline guard installed
ENTRY_PROBE = '''
import json, runpy, sys
sys.path[:0] = [{tests!r}]
import offline_guard
sys.argv = [{argv0!r}] + {argv!r}
code = 0
try:
	if {module!r}:
		sys.path.insert(0, {src!r})
		runpy.run_module('telegram_tracker', run_name='__main__', alter_sys=True)
	else:
		runpy.run_path({script!r}, run_name='__main__')
except SystemExit as exc:
	code = exc.code
package = sys.modules.get('telegram_tracker')
print('REPORT ' + json.dumps({{
	'code': code,
	'package': getattr(package, '__file__', None),
	'violations': offline_guard.VIOLATIONS
}}))
'''


def describe(parser):
	'''
	Every argument's behavior (no help text).
	'''
	rows = []
	for action in parser._actions:
		choices = action.choices
		if isinstance(action, argparse._SubParsersAction):
			choices = sorted(choices)
		rows.append((
			type(action).__name__, tuple(action.option_strings), action.dest,
			action.default, action.nargs, action.required, choices, action.const,
			getattr(action.type, '__qualname__', action.type)
		))
	return rows


def all_parsers(prog):
	top, subparsers = parsers.build_parsers(prog)
	return {'top': top, **subparsers, 'legacy': parsers.build_legacy_parser(prog)}


def neutral(text, prog):
	'''
	Output text with the program name and start/end times replaced and
	whitespace collapsed (a longer name wraps lines differently).
	'''
	text = text.replace(parsers.invocation(prog), '<prog>').replace(prog, '<prog>')
	text = TIMES.sub(r'\1 program at <time>', text)
	return ' '.join(text.split())


def run_entry(argv, cwd, *, script=None):
	'''
	Run main.py (script given) or `python -m telegram_tracker` in a fresh
	interpreter. Returns (report, stdout_without_report, stderr).
	'''
	module = script is None
	code = ENTRY_PROBE.format(
		tests=TESTS, src=SRC, module=module, script=script,
		argv0=os.path.join(SRC, 'telegram_tracker', '__main__.py') if module else script,
		argv=list(argv)
	)
	completed = subprocess.run(
		[sys.executable, '-I', '-c', textwrap.dedent(code)],
		cwd=cwd, capture_output=True, text=True, encoding='utf-8', timeout=120
	)
	lines = completed.stdout.splitlines()
	reports = [l for l in lines if l.startswith('REPORT ')]
	if not reports:
		raise AssertionError(f'probe failed: {completed.stderr[-2000:]}')
	other = '\n'.join(l for l in lines if not l.startswith('REPORT '))
	return json.loads(reports[-1][len('REPORT '):]), other, completed.stderr


class ParserParityTests(offline_guard.OfflineTestCase):

	def test_every_entry_point_parses_the_same_arguments(self):
		expected = {name: describe(p) for name, p in all_parsers(parsers.SCRIPT_PROG).items()}
		for prog in PROGS:
			for name, parser in all_parsers(prog).items():
				with self.subTest(prog=prog, parser=name):
					self.assertEqual(describe(parser), expected[name])

	def test_help_differs_only_in_the_program_name(self):
		expected = {
			name: neutral(p.format_help(), parsers.SCRIPT_PROG)
			for name, p in all_parsers(parsers.SCRIPT_PROG).items()
		}
		for prog in PROGS:
			for name, parser in all_parsers(prog).items():
				with self.subTest(prog=prog, parser=name):
					self.assertEqual(neutral(parser.format_help(), prog), expected[name])

	def test_help_names_the_program_it_was_started_as(self):
		for prog, run in (
				('main.py', 'python main.py'),
				('tgtracker', 'tgtracker'),
				('python -m telegram_tracker', 'python -m telegram_tracker')):
			with self.subTest(prog=prog):
				text = parsers.build_parser(prog).format_help()
				self.assertTrue(text.startswith(f'usage: {prog} '), text[:80])
				self.assertIn(f'Run `{run} <command> --help`', text)
				legacy = parsers.build_legacy_parser(prog).format_help()
				self.assertIn(f'Legacy form of `{run} collect`', legacy)


class CommandMainTests(offline_guard.OfflineTestCase):

	def run_command(self, argv):
		stdout, stderr = io.StringIO(), io.StringIO()
		with mock.patch.object(sys, 'argv', ['tgtracker', *argv]), \
				contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = cli.command_main()
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def test_overview_and_errors_use_the_command_name(self):
		code, out, err = self.run_command([])
		self.assertEqual((code, out), (2, ''))
		self.assertTrue(err.startswith('usage: tgtracker '), err[:80])

		code, _, err = self.run_command(['bogus'])
		self.assertEqual(code, 2)
		self.assertIn('tgtracker: error:', err)

		code, out, _ = self.run_command(['collect', '--help'])
		self.assertEqual(code, 0)
		self.assertTrue(out.startswith('usage: tgtracker collect '), out[:80])

	def test_reaches_the_same_handlers_as_main(self):
		calls = []

		def record(*args, **kwargs):
			calls.append(kwargs.get('legacy'))
			return 0

		with mock.patch.object(cli.dispatch, 'run_collect', record):
			for argv in (['collect', '--telegram-channel', 'a'], ['--telegram-channel', 'a']):
				with self.subTest(argv=argv):
					self.assertEqual(self.run_command(argv)[0], 0)
		self.assertEqual(calls, [False, True])


class FreshProcessEntryTests(offline_guard.OfflineTestCase):
	'''
	Real interpreters from an empty working directory: `python -m
	telegram_tracker` against the source tree, and main.py with nothing
	added to sys.path, as in a fresh clone.
	'''

	def test_module_form_matches_main_py(self):
		script = os.path.join(ROOT, 'main.py')
		with tempfile.TemporaryDirectory() as cwd:
			for argv in CASES:
				with self.subTest(argv=argv):
					want, want_out, want_err = run_entry(argv, cwd, script=script)
					got, got_out, got_err = run_entry(argv, cwd)
					self.assertEqual(got['code'], want['code'])
					self.assertEqual((got['violations'], want['violations']), ([], []))
					self.assertEqual(neutral(got_out, parsers.MODULE_PROG), neutral(want_out, 'main.py'))
					self.assertEqual(neutral(got_err, parsers.MODULE_PROG), neutral(want_err, 'main.py'))
			self.assertEqual(os.listdir(cwd), [])

	def test_scripts_import_the_package_from_their_checkout(self):
		with tempfile.TemporaryDirectory() as cwd:
			for name in ('main.py', 'build-datasets.py', 'channels-to-network.py'):
				with self.subTest(script=name):
					report, out, _ = run_entry(['--help'], cwd, script=os.path.join(ROOT, name))
					self.assertEqual(report['code'], 0)
					self.assertTrue(out.startswith(f'usage: {name} '), out[:80])
					self.assertEqual(
						os.path.normcase(report['package']),
						os.path.normcase(os.path.join(SRC, 'telegram_tracker', '__init__.py'))
					)
			self.assertEqual(os.listdir(cwd), [])

	def test_scripts_run_with_no_path_setup(self):
		with tempfile.TemporaryDirectory() as cwd:
			completed = subprocess.run(
				[sys.executable, '-I', os.path.join(ROOT, 'main.py'), '--help'],
				cwd=cwd, capture_output=True, text=True, encoding='utf-8', timeout=120
			)
			self.assertEqual(completed.returncode, 0, completed.stderr)
			self.assertTrue(completed.stdout.startswith('usage: main.py '))
			self.assertEqual(os.listdir(cwd), [])

	def test_package_import_loads_no_submodule(self):
		code = textwrap.dedent(f'''
			import sys
			sys.path.insert(0, {SRC!r})
			import telegram_tracker
			print(sorted(m for m in sys.modules if m.startswith('telegram_tracker.')))
		''')
		completed = subprocess.run(
			[sys.executable, '-I', '-c', code],
			capture_output=True, text=True, timeout=120
		)
		self.assertEqual(completed.stdout.strip(), '[]', completed.stderr)


if __name__ == '__main__':
	unittest.main()
