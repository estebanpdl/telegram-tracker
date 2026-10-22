# -*- coding: utf-8 -*-
'''
build-urls makes no network request and writes nothing outside its data
folder, including on the suffix library's first use in a fresh
interpreter: sockets and DNS are blocked by the offline guard, HTTP
sends through requests (tldextract's downloader) are recorded and
refused, a suffix-list URL set in the environment is ignored, and the
library's default cache folder (under a temporary home) stays empty.
The builder needs none of the dataset and network dependencies.
Synthetic fixtures only.
'''

# import modules
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from unittest import mock

import offline_guard

from builder_data import ROOT, TESTS
from builder_parity_support import BATCH, collect
from url_build_support import run_build
from url_data import OCCURRENCES, add_url_messages


SUFFIX_LIST_ENV = 'TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS'
SUFFIX_LIST_URL = 'https://suffix-list.example.invalid/public_suffix_list.dat'

# a fresh interpreter: offline guard, requests sends refused and recorded,
# then `main.py build-urls -d DATA`; `blocked` modules cannot be imported
CHILD = '''
import importlib.abc, json, runpy, sys
sys.path[:0] = [{tests!r}]
import offline_guard
BLOCKED = {blocked!r}
class Block(importlib.abc.MetaPathFinder):
	def find_spec(self, name, path=None, target=None):
		if name.split('.')[0] in BLOCKED:
			raise ModuleNotFoundError(f'No module named {{name!r}}', name=name)
sys.meta_path.insert(0, Block())
import requests.adapters
sends = []
def send(self, request, *args, **kwargs):
	sends.append(request.url)
	raise OSError('blocked')
requests.adapters.HTTPAdapter.send = send
import tldextract.cache
cache_dir = tldextract.cache.get_cache_dir()
sys.argv = [{script!r}, 'build-urls', '-d', {data!r}]
code = 0
try:
	runpy.run_path({script!r}, run_name='__main__')
except SystemExit as exc:
	code = exc.code
print('REPORT ' + json.dumps({{
	'code': code, 'violations': offline_guard.VIOLATIONS, 'sends': sends, 'cache_dir': cache_dir,
	'loaded': sorted(m for m in sys.modules if m.split('.')[0] in ('tldextract', 'pandas', 'networkx'))
}}))
'''


class OfflineBuildTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.data = os.path.join(self.tmp, 'data')
		collect(self.data, 'files', BATCH, world_change=add_url_messages)

	def test_no_download_in_process(self):
		import requests.adapters

		sends = []

		def send(adapter, request, *args, **kwargs):
			sends.append(request.url)
			raise OSError('blocked')

		with mock.patch.dict(os.environ, {SUFFIX_LIST_ENV: SUFFIX_LIST_URL}), \
				mock.patch.object(requests.adapters.HTTPAdapter, 'send', send):
			summary, _, _ = run_build(self.data)
		self.assertEqual(sends, [])
		self.assertEqual(summary.occurrences, len(OCCURRENCES))
		self.assertEqual(offline_guard.VIOLATIONS, [])

	def run_child(self, *, blocked=()):
		home = os.path.join(self.tmp, 'home')
		os.makedirs(home)
		env = dict(os.environ)
		env.update({
			'HOME': home, 'USERPROFILE': home, 'LOCALAPPDATA': home, 'APPDATA': home,
			'XDG_CACHE_HOME': os.path.join(home, '.cache'), SUFFIX_LIST_ENV: SUFFIX_LIST_URL
		})
		code = CHILD.format(tests=TESTS, blocked=tuple(blocked), script=os.path.join(ROOT, 'main.py'),
			data=self.data)
		completed = subprocess.run(
			[sys.executable, '-I', '-c', textwrap.dedent(code)],
			cwd=self.tmp, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120
		)
		reports = [line for line in completed.stdout.splitlines() if line.startswith('REPORT ')]
		if not reports:
			raise AssertionError(f'child failed: {completed.stderr[-2000:]}')
		return json.loads(reports[-1][len('REPORT '):]), completed, home

	def test_first_use_in_a_fresh_interpreter(self):
		report, completed, home = self.run_child()
		self.assertEqual(report['code'], 0, completed.stderr)
		self.assertIn(f'> Wrote {len(OCCURRENCES)} URL occurrences from 3 histories', completed.stdout)
		self.assertEqual((report['violations'], report['sends']), ([], []))
		# the library's default cache would be under the temporary home: it stays empty
		self.assertTrue(os.path.realpath(report['cache_dir']).startswith(home), report['cache_dir'])
		self.assertEqual(os.listdir(home), [])
		self.assertIn('tldextract', report['loaded'])

	def test_needs_none_of_the_dataset_and_network_dependencies(self):
		blocked = ('pandas', 'networkx', 'matplotlib', 'community', 'tqdm', 'openpyxl', 'telethon')
		report, completed, _ = self.run_child(blocked=blocked)
		self.assertEqual(report['code'], 0, completed.stderr)
		self.assertIn('tldextract', report['loaded'])
		self.assertEqual(report['loaded'], [m for m in report['loaded'] if m.startswith('tldextract')])


if __name__ == '__main__':
	unittest.main()
