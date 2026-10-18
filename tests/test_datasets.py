# -*- coding: utf-8 -*-
'''
Offline dataset builder (datasets.py, build-datasets.py) over the batch
collection produced by the fake client. Expected rows, statistics, and
append behavior come from fixtures/expected/dataset.json, written from
static inspection of the baseline build-datasets.py.
'''

# import modules
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from builder_data import collected_data, run_script
from fakes import load_expected

from telegram_tracker import datasets

from telegram_tracker.utils import DataInputError, chats_dataset_columns, msgs_dataset_columns


EXPECTED = load_expected('dataset.json')


def read_rows(path):
	with open(path, encoding='utf-8', newline='') as fh:
		reader = csv.DictReader(fh)
		return reader.fieldnames, list(reader)


def csv_value(value):
	'''
	How the baseline per-row DataFrame writes a value.
	'''
	return '' if value is None else str(value)


def file_bytes(path):
	with open(path, 'rb') as fh:
		return fh.read()


class DatasetTestCase(offline_guard.OfflineTestCase):

	scenario = 'batch'

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.data = collected_data(os.path.join(tmp.name, 'data'), self.scenario)
		self.msgs = os.path.join(self.data, 'msgs_dataset.csv')
		self.xlsx = os.path.join(self.data, 'collected_chats.xlsx')

	def build(self):
		with contextlib.redirect_stdout(io.StringIO()):
			return datasets.build_dataset(self.data, progress=False)

	def xlsx_frame(self):
		import pandas as pd

		return pd.read_excel(self.xlsx)


class BatchDatasetTests(DatasetTestCase):

	def setUp(self):
		super().setUp()
		self.summary = self.build()

	def test_summary(self):
		self.assertEqual(self.summary.rows_written, EXPECTED['msgs_dataset_rows'])
		self.assertEqual(self.summary.channels, ['alpha_fx', 'beta_fx', 'empty_fx'])
		self.assertEqual(os.path.normpath(self.summary.msgs_dataset), os.path.normpath(self.msgs))

	def test_message_rows_and_columns(self):
		header, rows = read_rows(self.msgs)
		self.assertEqual(header, msgs_dataset_columns())
		self.assertEqual(len(rows), EXPECTED['msgs_dataset_rows'])
		by_signature = {row['signature']: row for row in rows}
		for signature, fields in EXPECTED['rows'].items():
			for column, value in fields.items():
				with self.subTest(row=signature, column=column):
					self.assertEqual(by_signature[signature][column], csv_value(value))

	def test_service_messages_are_not_rows(self):
		# alpha_fx's sixth stored item (index 5) is a MessageService
		_, rows = read_rows(self.msgs)
		alpha = [r['signature'].split('.')[1] for r in rows if r['channel_name'] == 'alpha_fx']
		self.assertEqual(alpha, ['0', '1', '2', '3', '4', '6'])

	def test_chat_statistics_in_xlsx(self):
		frame = self.xlsx_frame()
		self.assertEqual(list(frame.columns), chats_dataset_columns())
		self.assertEqual(len(frame), EXPECTED['xlsx_rows'])
		for username, stats in EXPECTED['chat_stats'].items():
			rows = frame[frame['username'] == username]
			self.assertFalse(rows.empty, username)
			for column, value in stats.items():
				with self.subTest(username=username, column=column):
					self.assertEqual(rows[column].tolist(), [value] * len(rows))

	def test_second_run_appends_rows_and_replaces_xlsx(self):
		self.build()
		header_lines = [
			line for line in file_bytes(self.msgs).decode('utf-8').splitlines()
			if line.startswith('signature,')
		]
		self.assertEqual(len(header_lines), 1)
		_, rows = read_rows(self.msgs)
		self.assertEqual(len(rows), EXPECTED['msgs_dataset_rows_after_second_run'])
		self.assertEqual(rows[:11], rows[11:])
		self.assertEqual(len(self.xlsx_frame()), EXPECTED['xlsx_rows'])

	def test_no_temporary_files_remain(self):
		self.assertEqual([f for f in os.listdir(self.data) if '.tmp' in f], [])


class EmptyDatasetTests(DatasetTestCase):
	'''
	Intended correction: a collection with no channels produced no
	msgs_dataset.csv in the baseline; now it gets a header-only file.
	'''

	scenario = 'empty'

	def test_header_only_outputs(self):
		summary = self.build()
		self.assertEqual(summary.rows_written, 0)
		header, rows = read_rows(self.msgs)
		self.assertEqual((header, rows), (msgs_dataset_columns(), []))
		frame = self.xlsx_frame()
		self.assertEqual((list(frame.columns), len(frame)), (chats_dataset_columns(), 0))


class InvalidInputTests(DatasetTestCase):
	'''
	Malformed input fails before writing and leaves earlier outputs intact.
	'''

	def setUp(self):
		super().setUp()
		self.build()
		self.before = {p: file_bytes(p) for p in (self.msgs, self.xlsx)}

	def assert_rejected(self, message):
		with self.assertRaises(DataInputError) as ctx:
			self.build()
		self.assertIn(message, str(ctx.exception))
		for path, content in self.before.items():
			self.assertEqual(file_bytes(path), content, path)
		self.assertEqual([f for f in os.listdir(self.data) if '.tmp' in f], [])

	def overwrite(self, relpath, text):
		with open(os.path.join(self.data, relpath), 'w', encoding='utf-8') as fh:
			fh.write(text)

	def test_missing_collected_chats(self):
		os.remove(os.path.join(self.data, 'collected_chats.csv'))
		self.assert_rejected('collected_chats.csv not found')

	def test_collected_chats_missing_columns(self):
		self.overwrite('collected_chats.csv', 'id,username\n1,a_fx\n')
		self.assert_rejected('missing columns: _, title')

	def test_malformed_json(self):
		self.overwrite('beta_fx/beta_fx_messages.json', '{"count": 1, "messages": [')
		self.assert_rejected('beta_fx_messages.json is not valid JSON')

	def test_json_without_messages(self):
		self.overwrite('beta_fx/beta_fx_messages.json', json.dumps({'count': 0}))
		self.assert_rejected('is not a messages file')

	def test_message_missing_a_field(self):
		path = os.path.join(self.data, 'beta_fx', 'beta_fx_messages.json')
		with open(path, encoding='utf-8') as fh:
			obj = json.load(fh)
		del obj['messages'][-1]['peer_id']
		self.overwrite('beta_fx/beta_fx_messages.json', json.dumps(obj))
		self.assert_rejected("cannot read message fields (KeyError: 'peer_id')")


class LegacyScriptTests(DatasetTestCase):
	'''
	build-datasets.py in a fresh interpreter: fully offline, same outputs.
	'''

	def test_data_path_forms(self):
		for argv in (['--data-path', self.data], ['-d', self.data + '/'], [f'--data-path={self.data}']):
			with self.subTest(argv=argv):
				for name in ('msgs_dataset.csv', 'collected_chats.xlsx'):
					if os.path.exists(os.path.join(self.data, name)):
						os.remove(os.path.join(self.data, name))
				report, out, _ = run_script('build-datasets.py', argv, self.tmp)
				self.assertEqual(report['code'], 0)
				self.assertEqual(report['violations'], [])
				self.assertEqual(report['modules'], [])
				self.assertIn('Reading data from channel -> alpha_fx', out)
				self.assertEqual(len(read_rows(self.msgs)[1]), EXPECTED['msgs_dataset_rows'])

	def test_default_data_path_is_relative_to_working_directory(self):
		cwd = os.path.join(self.tmp, 'cwd')
		collected_data(os.path.join(cwd, 'output', 'data'))
		report, _, _ = run_script('build-datasets.py', [], cwd)
		self.assertEqual(report['code'], 0)
		self.assertTrue(os.path.isfile(os.path.join(cwd, 'output', 'data', 'msgs_dataset.csv')))

	def test_missing_input_exits_1_with_message(self):
		report, _, err = run_script('build-datasets.py', ['-d', os.path.join(self.tmp, 'nowhere')], self.tmp)
		self.assertEqual(report['code'], 1)
		self.assertIn('> build-dataset:', err)
		self.assertIn('collected_chats.csv not found', err)
		self.assertNotIn('Traceback', err)

	def test_help_is_offline(self):
		report, out, _ = run_script('build-datasets.py', ['--help'], self.tmp)
		self.assertEqual(report['code'], 0)
		self.assertIn('--data-path', out)
		self.assertEqual(report['modules'], [])


if __name__ == '__main__':
	unittest.main()
