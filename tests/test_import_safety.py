# -*- coding: utf-8 -*-
'''
Importing project modules must be inert: no event loop, argv parsing,
config reads, connections, or writes. Each check runs in a fresh
interpreter (-I) with the offline guard installed first, invalid argv, and
an empty working directory that has no config or session files.
'''

# import modules
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import offline_guard


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, 'tests')

COLLECTION_MODULES = ['utils', 'api', 'api.auth', 'collector', 'main']

PROBE = '''
import asyncio, json, sys
sys.path[:0] = [{tests!r}, {root!r}]
sys.argv = ['main.py', '--not-a-real-flag']
import offline_guard
report = {{'imported': [], 'error': None, 'loops': []}}
try:
	for name in {modules!r}:
		__import__(name)
		report['imported'].append(name)
except BaseException as exc:
	report['error'] = f'{{type(exc).__name__}}: {{exc}}'
for name in report['imported']:
	module = sys.modules[name]
	for attr, value in vars(module).items():
		if isinstance(value, asyncio.AbstractEventLoop):
			report['loops'].append(f'{{name}}.{{attr}}')
try:
	asyncio.get_event_loop()
	report['current_loop'] = 'present'
except RuntimeError:
	report['current_loop'] = 'absent'
report['violations'] = offline_guard.VIOLATIONS
print('REPORT ' + json.dumps(report))
'''


def import_in_fresh_process(modules):
	'''
	Import `modules` in a fresh interpreter from an empty temporary
	directory; return (report, other_stdout_lines, stderr, cwd_listing).
	'''
	with tempfile.TemporaryDirectory() as cwd:
		code = PROBE.format(tests=TESTS, root=ROOT, modules=list(modules))
		completed = subprocess.run(
			[sys.executable, '-I', '-c', textwrap.dedent(code)],
			cwd=cwd,
			capture_output=True,
			text=True,
			encoding='utf-8',
			timeout=120
		)
		listing = sorted(os.listdir(cwd))

	lines = completed.stdout.splitlines()
	report_lines = [l for l in lines if l.startswith('REPORT ')]
	if not report_lines:
		raise AssertionError(f'probe failed: {completed.stderr[-2000:]}')
	other = [l for l in lines if not l.startswith('REPORT ')]
	return json.loads(report_lines[-1][len('REPORT '):]), other, completed.stderr, listing


class ImportSafetyTests(offline_guard.OfflineTestCase):

	def test_collection_modules_import_inertly(self):
		report, output, stderr, listing = import_in_fresh_process(COLLECTION_MODULES)
		self.assertIsNone(report['error'])
		self.assertEqual(report['imported'], COLLECTION_MODULES)
		self.assertEqual(report['loops'], [])
		self.assertEqual(report['current_loop'], 'absent')
		self.assertEqual(report['violations'], [])
		self.assertEqual(output, [])
		self.assertEqual(stderr, '')
		self.assertEqual(listing, [])

	def test_utils_no_longer_loads_telethon(self):
		code = textwrap.dedent(f'''
			import sys
			sys.path[:0] = [{TESTS!r}, {ROOT!r}]
			import utils
			print('telethon' in sys.modules, hasattr(utils, 'loop'))
		''')
		completed = subprocess.run(
			[sys.executable, '-I', '-c', code],
			capture_output=True, text=True, timeout=120
		)
		self.assertEqual(completed.stdout.strip(), 'False False', completed.stderr)


if __name__ == '__main__':
	unittest.main()
