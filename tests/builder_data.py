# -*- coding: utf-8 -*-
'''
Collection outputs for the offline builder tests. The batch scenario is
collected once per process with the fake client (collection.json) and
copied into each test's temporary directory.
'''

# import modules
import asyncio
import atexit
import contextlib
import io
import os
import shutil
import tempfile

from fakes import FIXTURES, FakeTelegramClient, load_expected
from virtual_time import VirtualTimeLoop


_TEMPLATES = {}


def _collect(targets, out):
	'''
	Run the sequential collector with the fake client into `out`.
	'''
	from api.auth import ensure_authorized

	import collector

	expected = load_expected('collection.json')
	client = FakeTelegramClient(page_size=expected['page_size'])
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		asyncio.run(
			collector.run_collection(
				targets,
				out,
				client_factory=lambda: client,
				authorize=lambda c: ensure_authorized(c, None)
			),
			loop_factory=VirtualTimeLoop
		)


def collected_data(dest, scenario='batch'):
	'''
	Copy the collected output of `scenario` ('batch' or 'empty') to
	`dest` and return `dest`.
	'''
	if scenario not in _TEMPLATES:
		root = tempfile.mkdtemp(prefix=f'tt-{scenario}-')
		targets = load_expected('collection.json')['scenarios']['batch']['targets'] \
			if scenario == 'batch' else []
		_collect(targets, os.path.join(root, 'data'))
		_TEMPLATES[scenario] = os.path.join(root, 'data')

	shutil.copytree(_TEMPLATES[scenario], dest)
	return dest


def network_fixture(dest, name):
	'''
	A data folder holding fixtures/network/<name> as collected_chats.csv.
	'''
	os.makedirs(dest, exist_ok=True)
	shutil.copyfile(
		os.path.join(FIXTURES, 'network', name),
		os.path.join(dest, 'collected_chats.csv')
	)
	return dest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, 'tests')

SCRIPT_PROBE = '''
import json, runpy, sys
sys.path[:0] = [{tests!r}, {root!r}]
import offline_guard
before = set(sys.modules)
sys.argv = [{script!r}] + {argv!r}
code = 0
try:
	runpy.run_path({script!r}, run_name='__main__')
except SystemExit as exc:
	code = exc.code
watched = ('telethon', 'api', 'collector', 'matplotlib')
loaded = {{m.split('.')[0] for m in set(sys.modules) - before}}
modules = [m for m in watched if m in loaded]
print('REPORT ' + json.dumps({{'code': code, 'modules': modules, 'violations': offline_guard.VIOLATIONS}}))
'''


def run_script(script, argv, cwd):
	'''
	Run a repository script as __main__ in a fresh interpreter with the
	offline guard installed. Returns (report, stdout_without_report, stderr);
	report holds the exit code, which of telethon/api/collector/matplotlib
	gained modules after the guard was installed (the guard itself imports
	telethon), and guard violations.
	'''
	import json
	import subprocess
	import sys
	import textwrap

	code = SCRIPT_PROBE.format(
		tests=TESTS, root=ROOT, script=os.path.join(ROOT, script), argv=list(argv)
	)
	completed = subprocess.run(
		[sys.executable, '-I', '-c', textwrap.dedent(code)],
		cwd=cwd,
		capture_output=True,
		text=True,
		encoding='utf-8',
		timeout=300
	)
	lines = completed.stdout.splitlines()
	reports = [l for l in lines if l.startswith('REPORT ')]
	if not reports:
		raise AssertionError(f'probe failed: {completed.stderr[-2000:]}')
	other = '\n'.join(l for l in lines if not l.startswith('REPORT '))
	return json.loads(reports[-1][len('REPORT '):]), other, completed.stderr


@atexit.register
def cleanup_templates():
	for path in _TEMPLATES.values():
		shutil.rmtree(os.path.dirname(path), ignore_errors=True)
	_TEMPLATES.clear()
