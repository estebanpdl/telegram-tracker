# -*- coding: utf-8 -*-
'''
Builder parity across storage modes: the same collection sequence run
into a files, a sqlite and a both folder gives the same msgs_dataset.csv
bytes, collected_chats.xlsx cells, Graph.gexf nodes and edges (layout
and image excluded) and build records (minus source fields), whether a
folder is read from its files or its database, on a first build and on a
repeated one (which appends the same rows again). Inputs stay
byte-identical. A renamed channel is the documented difference.
Synthetic fixtures only.
'''

# import modules
import os
import unittest

from builder_parity_support import ALICE, ALPHA, BATCH, BOB, ParityCase, add_post, build, \
	build_record, collect, rename

from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.datasets import STAT_COLUMNS


class ParityTests(ParityCase):

	def check(self, *steps, repeat=2):
		self.sequence(*steps)
		results = self.build_copies(repeat=repeat)
		self.assertParity(results)
		return results

	def test_partial_batch(self):
		# missing and private targets fail: a finalized partial run
		results = self.check((BATCH, {}))
		rows = results['files'][0]['msgs_dataset.csv'].count(b'\n')
		self.assertGreater(rows, 1)
		# the repeated build appended every row again
		self.assertEqual(results['files'][1]['msgs_dataset.csv'].count(b'\n'), 2 * rows - 1)

	def test_update_appends_merged_histories(self):
		self.check(
			(BATCH, {'profile': ALICE}),
			(BATCH, {'profile': ALICE, 'update': True, 'world_change': add_post})
		)

	def test_window_after_full(self):
		self.check(
			(BATCH, {}),
			(['alpha_fx'], {'window': HistoryWindow(max_messages=2)})
		)

	def test_profiles_and_takeover(self):
		# older histories of two profiles, then bob takes alpha over
		results = self.check(
			(['alpha_fx'], {'profile': ALICE}),
			(['beta_fx'], {'profile': BOB}),
			(['alpha_fx'], {'profile': BOB}),
			(['beta_fx'], {'profile': BOB, 'update': True, 'world_change': lambda w: add_post(w, 102)})
		)
		histories = results['files'][0]['dataset-build.json']['histories']
		self.assertEqual(
			[(h['channel'], h['profile_name']) for h in histories],
			[('alpha_fx', 'bob'), ('beta_fx', 'bob')]
		)

	def test_unknown_profile_stays_unknown(self):
		results = self.check((['alpha_fx'], {}), repeat=1)
		history, = results['files'][0]['dataset-build.json']['histories']
		self.assertEqual((history['profile_name'], history['profile_id']), (None, None))
		self.assertEqual(history['channel_id'], ALPHA)

	def test_metadata_only_rerun(self):
		self.check((BATCH, {}), (BATCH, {'metadata_only': True}))

	def test_no_histories(self):
		# every target fails: no histories, a header-only collected_chats
		results = self.check((['missing_fx'], {}))
		self.assertEqual(results['files'][0]['Graph.gexf'], ([], []))
		self.assertEqual(results['files'][0]['collected_chats.xlsx'][1], [])

	def test_duplicate_rows_across_runs(self):
		# the same chats observed in three runs: rows repeat, edges collapse
		results = self.check((BATCH, {}), (BATCH, {}), (['alpha_fx'], {}), repeat=1)
		_, rows = results['files'][0]['collected_chats.xlsx']
		self.assertGreater(len(rows), len({tuple(map(str, r)) for r in rows}))


class ProvenanceRecordTests(ParityCase):

	def test_database_records_runs_and_takeover(self):
		self.sequence((['alpha_fx'], {'profile': ALICE}), (['alpha_fx'], {'profile': BOB}))
		for label, out, source in (('sqlite', self.folders['sqlite'], 'auto'),
				('both', self.folders['both'], 'sqlite')):
			with self.subTest(label):
				build(out, source)
				record = build_record(os.path.join(out, 'dataset-build.json'))
				self.assertEqual(record['source'], 'sqlite')
				self.assertEqual(record['database']['schema_version'], 1)
				history, = record['histories']
				self.assertEqual(
					(history['profile_name'], history['mode'], history['takeover']),
					('bob', 'replaced', True)
				)
				self.assertEqual(history['run'], record['last_run']['run_uuid'])

	def test_files_record_their_provenance_file(self):
		self.sequence((['alpha_fx'], {'profile': ALICE}), (['alpha_fx'], {'profile': BOB}))
		out = self.folders['both']
		build(out)
		record = build_record(os.path.join(out, 'dataset-build.json'))
		self.assertEqual((record['source'], record['storage'], record['database']), ('files', 'both', None))
		history, = record['histories']
		self.assertEqual((history['profile_name'], history['run'], history['takeover']), ('bob', None, True))
		self.assertEqual(record['last_run']['profile_name'], 'bob')


class RenameTests(ParityCase):
	'''
	A channel collected under its old and then its new username: the
	files keep a messages file per name, the database one history under
	the latest name. Collected chats and the network still agree.
	'''

	def test_rename(self):
		self.sequence(
			(['alpha_fx'], {}),
			(['alpha_new_fx'], {'world_change': lambda w: rename(w, ALPHA, 'alpha_new_fx')})
		)
		results = self.build_copies()
		self.assertParity(results, labels=['both'])
		for label in ('sqlite', 'both-sqlite'):
			for name in ('Graph.gexf', 'network-build.json'):
				with self.subTest(build=label, output=name):
					self.assertEqual(results[label][0][name], results['files'][0][name])

		channels = lambda label: [h['channel'] for h in results[label][0]['dataset-build.json']['histories']]
		self.assertEqual(channels('files'), ['alpha_fx', 'alpha_new_fx'])
		self.assertEqual(channels('sqlite'), ['alpha_new_fx'])
		self.assertEqual(channels('both-sqlite'), ['alpha_new_fx'])

		# the workbook differs only in the statistics of the old name's
		# rows: the files still hold its history
		columns, files_rows = results['files'][0]['collected_chats.xlsx']
		stats = [columns.index(c) for c in STAT_COLUMNS]
		for label in ('sqlite', 'both-sqlite'):
			got_columns, rows = results[label][0]['collected_chats.xlsx']
			self.assertEqual(got_columns, columns)
			self.assertEqual(len(rows), len(files_rows))
			for row, want in zip(rows, files_rows):
				username = want[columns.index('username')]
				if username == 'alpha_fx':
					self.assertEqual([row[i] for i in stats], [0] * len(stats))
					self.assertNotEqual([want[i] for i in stats], [0] * len(stats))
					row = [v for i, v in enumerate(row) if i not in stats]
					want = [v for i, v in enumerate(want) if i not in stats]
				self.assertEqual(row, want, label)


class OrderTests(ParityCase):

	def test_histories_follow_the_files_path_order(self):
		# 'alpha_fx/...' and 'alpha_fxZ/...': the path separator sorts
		# before 'Z' on POSIX and after it on Windows; the database
		# reader orders histories as the files builder does on this one
		self.sequence((['alpha_fx', 'alpha_fxZ'], {'world_change': lambda w: rename(w, 102, 'alpha_fxZ')}))
		results = self.build_copies(repeat=1)
		self.assertParity(results)
		channels = [h['channel'] for h in results['sqlite'][0]['dataset-build.json']['histories']]
		expected = ['alpha_fxZ', 'alpha_fx'] if os.sep == '\\' else ['alpha_fx', 'alpha_fxZ']
		self.assertEqual(channels, expected)


if __name__ == '__main__':
	unittest.main()
