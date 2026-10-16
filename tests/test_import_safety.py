# -*- coding: utf-8 -*-
'''
Importing project modules must be inert: no event loop, argv parsing,
config reads, connections, or writes. Each check runs in a fresh
interpreter (-I) with the offline guard installed first, from an empty
working directory that has no config or session files.
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

PROBE = '''
import asyncio, json, sys
sys.path[:0] = [{tests!r}, {root!r}]
sys.argv = ['main.py', '--not-a-real-flag']
import offline_guard
report = {{'imported': [], 'error': None}}
try:
	for name in {modules!r}:
		__import__(name)
		report['imported'].append(name)
except BaseException as exc:
	report['error'] = f'{{type(exc).__name__}}: {{exc}}'
try:
	asyncio.get_event_loop()
	report['loop'] = 'present'
except RuntimeError:
	report['loop'] = 'absent'
report['violations'] = offline_guard.VIOLATIONS
print('REPORT ' + json.dumps(report))
'''


def import_in_fresh_process(modules):
	'''
	Import `modules` in a fresh interpreter from an empty temporary
	directory; return (report, stdout_without_report, cwd_listing).
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
	return json.loads(report_lines[-1][len('REPORT '):]), other, listing


class BaselineImportTests(offline_guard.OfflineTestCase):
	'''
	Baseline (Phase 1): utils creates a global loop at import, which
	Python 3.14 rejects. Phase 2 makes this import inert.
	'''

	@unittest.expectedFailure
	def test_utils_import_is_inert(self):
		report, output, listing = import_in_fresh_process(['utils'])
		self.assertIsNone(report['error'])
		self.assertEqual(report['loop'], 'absent')
		self.assertEqual((output, listing, report['violations']), ([], [], []))

	def test_baseline_failure_is_the_event_loop_error(self):
		report, _, listing = import_in_fresh_process(['utils'])
		self.assertIn('RuntimeError', report['error'] or '')
		self.assertIn('no current event loop', report['error'])
		self.assertEqual(listing, [])
		self.assertEqual(report['violations'], [])


if __name__ == '__main__':
	unittest.main()
