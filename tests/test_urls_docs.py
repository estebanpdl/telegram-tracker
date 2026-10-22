# -*- coding: utf-8 -*-
'''
docs/urls-dataset.md against the code: every CSV column and every table
and column of urls.sqlite3 is documented, in order, and nothing else is;
the example queries run on a built folder; the documented line ends
hold. Synthetic fixtures only.
'''

# import modules
import os
import re
import sqlite3
import tempfile
import unittest

import offline_guard

from builder_data import ROOT
from builder_parity_support import BATCH, collect, read_bytes
from url_build_support import run_build
from url_data import add_url_messages

from telegram_tracker.urls.outputs import COLUMN_NAMES, URLS_DATABASE, URLS_DATASET


DOC = os.path.join(ROOT, 'docs', 'urls-dataset.md')


def table_names(lines):
	'''
	First-column names of the markdown table starting at `lines[0]`.
	'''
	names = []
	for line in lines[2:]:
		if not line.startswith('|'):
			break
		names.append(re.match(r'\| `([^`]+)` \|', line).group(1))
	return names


class UrlsDocTests(offline_guard.OfflineTestCase):

	@classmethod
	def setUpClass(cls):
		with open(DOC, encoding='utf-8') as fh:
			cls.text = fh.read()
		cls.lines = cls.text.splitlines()

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(os.path.realpath(tmp.name), 'data')
		collect(self.out, 'files', BATCH, world_change=add_url_messages)
		run_build(self.out)
		self.con = sqlite3.connect(os.path.join(self.out, URLS_DATABASE))
		self.addCleanup(self.con.close)

	def documented(self, heading):
		'''
		Column names of the first table after the line `heading`.
		'''
		start = self.lines.index(heading)
		first = next(i for i in range(start + 1, len(self.lines)) if self.lines[i].startswith('| Column'))
		return table_names(self.lines[first:])

	def test_csv_columns(self):
		self.assertEqual(self.documented('## `urls_dataset.csv` and `occurrences`'), list(COLUMN_NAMES))
		columns = [row[1] for row in self.con.execute('PRAGMA table_info(occurrences)')]
		self.assertEqual(columns, list(COLUMN_NAMES))

	def test_other_tables(self):
		tables = sorted(name for name, in self.con.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table'"))
		documented = re.findall(r'^\*\*`(\w+)`\*\*', self.text, re.M)
		self.assertEqual(sorted(documented + ['occurrences']), tables)
		for table in documented:
			with self.subTest(table=table):
				heading = next(line for line in self.lines if line.startswith(f'**`{table}`**'))
				columns = [row[1] for row in self.con.execute(f'PRAGMA table_info({table})')]
				self.assertEqual(self.documented(heading), columns)
		self.assertEqual(self.con.execute('PRAGMA user_version').fetchone(), (1,))

	def test_example_queries_run(self):
		queries = re.findall(r'```sql\n(.*?)```', self.text, re.S)
		self.assertEqual(len(queries), 3)
		results = [self.con.execute(query).fetchall() for query in queries]
		self.assertGreater(len(results[0]), 5)
		self.assertEqual(results[1], [('alpha_fx', 12, 'https://example.com/a', 'https://evil.example.net/x')])

	def test_line_ends(self):
		data = read_bytes(os.path.join(self.out, URLS_DATASET))
		self.assertNotIn(b'\r', data)
		self.assertEqual(data.count(b'\n'), len(self.con.execute('SELECT 1 FROM occurrences').fetchall()) + 1)


if __name__ == '__main__':
	unittest.main()
