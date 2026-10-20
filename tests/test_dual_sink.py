# -*- coding: utf-8 -*-
'''
--storage both: the write order stage -> database commit ('pending') ->
publish -> 'written', for channels and aggregates. A failure is injected
at each step; every case checks the target's outcome, the run's end
state, the 'pending' markers, whether the next run is blocked, the files
on disk and that no temporary file is left. Also: the bounded Windows
replace retry (both mode only), newline bytes of the exception logs, and
a real process ended between publish and mark. Stops and --update
agreement are in test_dual_update.
'''

# import modules
import ast
import csv
import json
import os
import sqlite3
import subprocess
import sys
import unittest

from unittest import mock

import storage_modes_support

from storage_modes_support import BATCH, listing, read_json

from sqlite_support import BETA

from telegram_tracker.storage import aggregates, dual
from telegram_tracker.storage import files as storage_files
from telegram_tracker.storage.records import CHANNELS_NOT_FOUND, LOCK_FILE, StorageError
from telegram_tracker.storage.sqlite import writer as sqlite_writer
from telegram_tracker.storage.sqlite.lease import database_lease_path
from telegram_tracker.storage.status import StatusFile


TESTS = os.path.dirname(os.path.abspath(__file__))


def fail_when(name_suffix, error=None):
	'''
	A replacement for a (src, dst, ...) function that raises for a
	destination ending in `name_suffix` and calls the original otherwise.
	'''
	def wrap(original):
		def run(src, dst, *args, **kwargs):
			if dst.endswith(name_suffix):
				raise error or OSError(28, 'No space left on device')
			return original(src, dst, *args, **kwargs)
		return run
	return wrap


class ChannelFaultTests(storage_modes_support.BothCase):
	'''
	Faults while writing beta_fx (the second target) on a fresh folder.
	'''

	def test_staging_failure_leaves_both_sinks_untouched(self):
		original = dual.stage_file

		def stage(path, write):
			if path.endswith('beta_fx_messages.json'):
				raise StorageError(f'cannot write {path}: no space')
			return original(path, write)

		with mock.patch.object(dual, 'stage_file', stage):
			code, _, err = self.both()
		self.assertEqual(code, 1)
		self.assertIn('beta_fx_messages.json: no space', err)
		self.assertConsistentTargets()
		self.assertEqual(self.rows('SELECT count(*) FROM histories WHERE channel_id = ?', BETA), [(0,)])
		self.assertEqual(listing(os.path.join(self.out, 'beta_fx')), [])
		self.assertEqual(self.pending(), [])
		self.assertEqual(self.run_state(), ('interrupted', 'written'))
		self.assertNoTemporaryFiles()
		self.assertBlocked(False)

	def test_database_failure_discards_the_staged_files(self):
		original = sqlite_writer.SQLiteWriter._write_channel

		def write_channel(writer, result, files_state=None):
			if result.target == 'beta_fx':
				raise sqlite3.OperationalError('disk I/O error')
			return original(writer, result, files_state)

		with mock.patch.object(sqlite_writer.SQLiteWriter, '_write_channel', write_channel):
			code, _, err = self.both()
		self.assertEqual(code, 1)
		self.assertIn('cannot save beta_fx: disk I/O error', err)
		self.assertConsistentTargets()
		self.assertEqual(listing(os.path.join(self.out, 'beta_fx')), [])
		self.assertEqual(self.pending(), [])
		self.assertNoTemporaryFiles()
		self.assertBlocked(False)

	def publish_failure(self, suffix):
		real = os.replace
		with mock.patch.object(storage_files.os, 'replace', fail_when(suffix)(real)):
			code, _, err = self.both()
		self.assertEqual(code, 1)
		self.assertIn(f'cannot replace', err)
		self.assertEqual(self.targets()[1], ('beta_fx', 'failed', 'pending', 1))
		self.assertEqual(self.pending(), [('target', 1)])
		self.assertEqual(self.run_state(), ('interrupted', 'written'))
		self.assertEqual(self.status()['state'], 'interrupted')
		self.assertEqual(self.status()['publication']['pending_targets'], ['beta_fx'])
		self.assertNoTemporaryFiles()
		# the run's aggregates leave beta_fx out in both sinks
		sql_sources = {s for (raw,) in self.rows('SELECT sources FROM run_counters') for s in json.loads(raw)}
		self.assertIn('alpha_fx', sql_sources)
		self.assertNotIn('beta_fx', sql_sources)
		with open(os.path.join(self.out, 'counter.csv'), encoding='utf-8', newline='') as fh:
			file_sources = {s for row in csv.DictReader(fh) for s in ast.literal_eval(row['source'])}
		self.assertEqual(file_sources, sql_sources)
		self.assertBlocked(True)

	def test_first_file_replace_failure_blocks(self):
		self.publish_failure('beta_fx.json')
		self.assertFalse(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx.json')))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')))

	def test_second_file_replace_failure_blocks(self):
		self.publish_failure('beta_fx_messages.json')
		self.assertTrue(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx.json')))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')))

	def test_the_failed_outcome_is_recorded_before_the_run_ends(self):
		# finish cannot store the outcome either: the publish failure's own record stays
		real = os.replace

		def finish(writer, state, **kwargs):
			raise StorageError('cannot finish the run: disk I/O error')

		with mock.patch.object(storage_files.os, 'replace', fail_when('beta_fx.json')(real)), \
				mock.patch.object(sqlite_writer.SQLiteWriter, 'finish', finish):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.targets()[1], ('beta_fx', 'failed', 'pending', 1))
		self.assertEqual(
			self.rows('SELECT reason FROM run_targets WHERE input_index = 1')[0][0][:len('storage error: ')],
			'storage error: ')

	def test_mark_written_failure_blocks_although_the_files_are_published(self):
		original = sqlite_writer.SQLiteWriter.mark_target_written

		def mark(writer, index):
			if index == 1:
				raise StorageError('cannot record the published files: disk I/O error')
			return original(writer, index)

		with mock.patch.object(sqlite_writer.SQLiteWriter, 'mark_target_written', mark):
			code, _, err = self.both()
		self.assertEqual(code, 1)
		self.assertTrue(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')))
		self.assertEqual(self.targets()[1], ('beta_fx', 'failed', 'pending', 1))
		self.assertEqual(self.pending(), [('target', 1)])
		self.assertBlocked(True)


class AggregateFaultTests(storage_modes_support.BothCase):

	def test_aggregate_staging_failure_writes_no_aggregates(self):
		original = dual.stage_file

		def stage(path, write):
			if path.endswith('counter.csv'):
				raise StorageError(f'cannot write {path}: no space')
			return original(path, write)

		with mock.patch.object(dual, 'stage_file', stage):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.run_state(), ('interrupted', None))
		self.assertEqual(self.rows('SELECT count(*) FROM run_counters'), [(0,)])
		self.assertFalse(os.path.exists(os.path.join(self.out, 'counter.csv')))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'chats.txt')))
		self.assertEqual(self.pending(), [])
		self.assertNoTemporaryFiles()
		self.assertBlocked(False)

	def test_aggregate_database_failure_writes_no_aggregate_files(self):
		def write_aggregates(writer, agg):
			raise ValueError('cannot bind a value')

		with mock.patch.object(sqlite_writer.SQLiteWriter, '_write_aggregates', write_aggregates):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.run_state(), ('interrupted', None))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'chats.txt')))
		self.assertEqual(self.pending(), [])
		self.assertNoTemporaryFiles()
		self.assertBlocked(False)

	def test_aggregate_replace_failure_blocks(self):
		real = os.replace
		with mock.patch.object(storage_files.os, 'replace', fail_when('counter.csv')(real)):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.run_state(), ('interrupted', 'pending'))
		self.assertTrue(os.path.exists(os.path.join(self.out, 'chats.txt')))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'counter.csv')))
		self.assertEqual(self.pending(), [('aggregates', 1)])
		self.assertEqual(self.status()['state'], 'interrupted')
		self.assertNoTemporaryFiles()
		self.assertBlocked(True)

	def test_log_append_failure_blocks(self):
		def append(path, lines):
			if lines:
				raise StorageError(f'cannot write {path}: no space')

		with mock.patch.object(aggregates, 'append_lines', append):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.run_state(), ('interrupted', 'pending'))
		self.assertTrue(os.path.exists(os.path.join(self.out, 'collected_chats.csv')))
		self.assertBlocked(True)

	def test_aggregate_mark_failure_blocks(self):
		def mark(writer):
			raise StorageError('cannot record the published aggregate files: disk I/O error')

		with mock.patch.object(sqlite_writer.SQLiteWriter, 'mark_aggregates_written', mark):
			code, _, _ = self.both()
		self.assertEqual(code, 1)
		self.assertEqual(self.pending(), [('aggregates', 1)])
		self.assertBlocked(True)

	def test_sidecar_failure_leaves_the_database_authoritative(self):
		def finish(status, state, **kwargs):
			raise StorageError(f'cannot write {status.path}: no space')

		with mock.patch.object(StatusFile, 'finish', finish):
			code, _, err = self.both()
		self.assertEqual(code, 1)
		self.assertIn('cannot write', err)
		self.assertEqual(self.run_state(), ('partial', 'written'))
		self.assertEqual(self.status()['state'], 'running')
		self.assertEqual(self.pending(), [])
		self.assertBlocked(False)


class ReplaceRetryTests(unittest.TestCase):

	def run_retry(self, failures, *, windows, error=PermissionError):
		calls, sleeps = [], []

		def replace(src, dst):
			calls.append((src, dst))
			if len(calls) <= failures:
				raise error(13, 'The process cannot access the file')

		run = dual.retrying_replace(windows=windows, sleep=sleeps.append, replace=replace)
		try:
			run('a.tmp', 'a')
			return len(calls), sleeps, None
		except OSError as exc:
			return len(calls), sleeps, exc

	def test_windows_retries_a_refused_replace_up_to_the_bound(self):
		self.assertEqual(self.run_retry(4, windows=True), (5, [0.05, 0.1, 0.2, 0.4], None))
		attempts, sleeps, error = self.run_retry(5, windows=True)
		self.assertEqual((attempts, sleeps), (dual.REPLACE_ATTEMPTS, list(dual.REPLACE_DELAYS)))
		self.assertIsInstance(error, PermissionError)
		self.assertLessEqual(sum(dual.REPLACE_DELAYS), 0.75)

	def test_other_platforms_and_other_errors_are_not_retried(self):
		self.assertEqual(self.run_retry(1, windows=False)[:2], (1, []))
		self.assertEqual(self.run_retry(1, windows=True, error=FileNotFoundError)[:2], (1, []))


@unittest.skipUnless(os.name == 'nt', 'the replace retry applies on Windows only')
class WindowsRetryRunTests(storage_modes_support.BothCase):

	def transient(self):
		real = os.replace
		left = {'refusals': 2}

		def replace(src, dst):
			if dst.endswith('beta_fx_messages.json') and left['refusals']:
				left['refusals'] -= 1
				raise PermissionError(13, 'The process cannot access the file')
			return real(src, dst)

		return replace

	def test_both_mode_sits_out_a_transient_refusal(self):
		with mock.patch.object(storage_files.os, 'replace', self.transient()), \
				mock.patch.object(dual.time, 'sleep') as sleep:
			code, _, err = self.both()
		self.assertEqual(code, 1, err)  # missing_fx and private_fx only
		self.assertEqual([c.args for c in sleep.call_args_list], [(0.05,), (0.1,)])
		self.assertEqual(self.pending(), [])
		self.assertEqual(self.targets()[1][:3], ('beta_fx', 'succeeded', 'written'))

	def test_files_mode_keeps_its_single_attempt(self):
		with mock.patch.object(storage_files.os, 'replace', self.transient()), \
				mock.patch.object(dual.time, 'sleep') as sleep:
			code, _, err = self.collect(targets=BATCH, out=self.files_out)
		self.assertEqual(code, 1)
		self.assertIn('cannot write', err)
		self.assertEqual(read_json(os.path.join(self.files_out, 'collection-status.json'))['state'], 'interrupted')
		sleep.assert_not_called()


class LogNewlineTests(storage_modes_support.BothCase):

	def test_exception_logs_keep_the_platform_newlines(self):
		self.both()
		self.both()
		self.collect(targets=BATCH, out=self.files_out)
		self.collect(targets=BATCH, out=self.files_out)
		path = os.path.join(self.out, CHANNELS_NOT_FOUND)
		with open(path, 'rb') as fh:
			data = fh.read()
		newline = os.linesep.encode()
		self.assertEqual(data, (b'missing_fx' + newline + b'private_fx' + newline) * 2)
		with open(os.path.join(self.files_out, CHANNELS_NOT_FOUND), 'rb') as fh:
			self.assertEqual(fh.read(), data)


class CrashTests(storage_modes_support.BothCase):

	def test_a_process_ended_after_publishing_still_blocks(self):
		completed = subprocess.run(
			[sys.executable, '-I', os.path.join(TESTS, 'dual_crash_child.py'), self.out],
			capture_output=True, text=True, timeout=120
		)
		self.assertEqual(completed.returncode, 9, completed.stderr)
		self.assertIn('crashing', completed.stdout)
		# the files were published, but nothing recorded it
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')))
		self.assertEqual(self.pending(), [('target', 1)])
		self.assertEqual(self.rows('SELECT state FROM collection_runs'), [('running',)])
		# blocked with the stale locks in place, and after removing them
		self.assertTrue(os.path.exists(os.path.join(self.out, LOCK_FILE)))
		self.assertBlocked(True)
		os.remove(os.path.join(self.out, LOCK_FILE))
		os.remove(database_lease_path(self.db()))
		self.assertBlocked(True)
		messages = read_json(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json'))['messages']
		self.assertEqual(
			sorted(m['id'] for m in messages),
			[r[0] for r in self.rows('SELECT message_id FROM messages ORDER BY message_id')]
		)


if __name__ == '__main__':
	unittest.main()
