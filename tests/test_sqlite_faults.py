# -*- coding: utf-8 -*-
'''
Failures while writing the database: a statement failing mid-channel, a
real busy lock from another connection, a real full database
(max_page_count), invalid JSON, failures while finalizing or finishing
the run, a refused database at the start, and cancellation. Each failed
channel is rolled back whole, earlier channels and their previous state
survive, no file fallback happens, the run ends interrupted, and the
database passes integrity and foreign-key checks.
'''

# import modules
import asyncio
import os
import sqlite3

import sqlite_support

from sqlite_support import ALPHA, BETA, read_json

from telegram_tracker import storage
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import DatabaseBusy
from telegram_tracker.storage.sqlite.schema import DatabaseRefused
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


TARGETS = ['alpha_fx', 'beta_fx', 'empty_fx']


class Hooked:
	'''
	A connection proxy calling `hook(sql, seen)` before every statement;
	`seen` counts the statements by their first two words. The hook may
	raise to fail the statement.
	'''
	def __init__(self, con, hook):
		self.con = con
		self.hook = hook
		self.seen = {}

	def _before(self, sql):
		key = ' '.join(sql.split()[:2]).upper()
		self.seen[key] = self.seen.get(key, 0) + 1
		self.hook(sql, self.seen)

	def execute(self, sql, *args):
		self._before(sql)
		return self.con.execute(sql, *args)

	def executemany(self, sql, *args):
		self._before(sql)
		return self.con.executemany(sql, *args)

	@property
	def in_transaction(self):
		return self.con.in_transaction

	def close(self):
		self.con.close()


class FaultCase(sqlite_support.SQLiteCase):

	def hooked(self, hook):
		def factory(**kwargs):
			writer = SQLiteWriter.open(self.db, busy_timeout_ms=100, **kwargs)
			writer.con = writer.histories.con = Hooked(writer.con, hook)
			return writer

		return factory

	def begins(self, sql, seen):
		return seen.get('BEGIN IMMEDIATE', 0) if sql == 'BEGIN IMMEDIATE' else None

	def status(self):
		return read_json(os.path.join(self.out, storage.records.STATUS_FILE))

	def assertInterrupted(self, fragment):
		status = self.status()
		self.assertEqual(status['state'], 'interrupted')
		self.assertIn(fragment, status['stop_reason'])
		run = self.rows('SELECT state, stop_reason FROM collection_runs ORDER BY run_id DESC LIMIT 1')[0]
		self.assertEqual(run[0], 'interrupted')
		self.assertIn(fragment, run[1])
		self.assertFalse(os.path.exists(os.path.join(self.out, storage.records.LOCK_FILE)))
		self.assertEqual(
			sorted(os.listdir(self.out)), ['collection-status.json', 'telegram-tracker.sqlite3'])
		self.assertIntact()

	def channel_rows(self, channel_id):
		return {t: self.rows(f'SELECT * FROM {t} WHERE channel_id = ? ORDER BY 1, 2', channel_id) for t in (
			'channel_snapshots', 'histories', 'messages', 'history_entities')}


class ChannelFailureTests(FaultCase):

	async def test_statement_failure_rolls_back_the_whole_channel(self):
		await self.collect(['beta_fx'])
		earlier = self.channel_rows(BETA)

		def fail_beta_entities(sql, seen):
			if 'INSERT INTO history_entities' in sql and self.writing == 'beta_fx':
				raise sqlite3.OperationalError('injected failure')

		def factory(**kwargs):
			writer = self.hooked(fail_beta_entities)(**kwargs)
			commit = writer.commit

			def tracking(result):
				self.writing = result.target
				return commit(result)

			writer.commit = tracking
			return writer

		self.writing = None
		with self.assertRaisesRegex(StorageError, 'injected failure'):
			await self.collect(TARGETS, writer_factory=factory)
		self.assertEqual(self.channel_rows(BETA), earlier)
		self.assertEqual(self.channel_rows(ALPHA)['histories'][0][1], 2)
		self.assertEqual(
			self.rows('SELECT status, substr(reason, 1, 14) FROM run_targets WHERE run_id = 2 ORDER BY input_index'),
			[('succeeded', None), ('failed', 'storage error:'), ('cancelled', None)])
		self.assertEqual(self.status()['targets'][1]['status'], 'failed')
		self.assertTrue(self.rows('SELECT count(*) FROM run_observations WHERE run_id = 2')[0][0] > 0)
		self.assertInterrupted('injected failure')

	async def test_real_busy_lock_fails_the_channel_and_the_run(self):
		blockers = []

		def hold_during_beta(sql, seen):
			nth = self.begins(sql, seen)
			if nth == 2:
				blocker = sqlite3.connect(self.db, isolation_level=None)
				self.addCleanup(blocker.close)
				blocker.execute('BEGIN IMMEDIATE')
				blockers.append(blocker)
			elif nth == 3:
				blockers[0].execute('ROLLBACK')

		with self.assertRaises(DatabaseBusy) as caught:
			await self.collect(TARGETS, writer_factory=self.hooked(hold_during_beta))
		self.assertIn('in use by another process', str(caught.exception))
		self.assertEqual(self.channel_rows(BETA)['channel_snapshots'], [])
		self.assertEqual(len(self.channel_rows(ALPHA)['messages']), 7)
		self.assertInterrupted('in use by another process')

	async def test_real_full_database_fails_the_channel(self):
		def fill_after_alpha(sql, seen):
			nth = self.begins(sql, seen)
			con = self.writer_con
			if nth == 2:
				pages = con.execute('PRAGMA page_count').fetchone()[0]
				con.execute(f'PRAGMA max_page_count = {pages}')
			elif nth == 3:
				con.execute('PRAGMA max_page_count = 1073741823')

		def factory(**kwargs):
			writer = self.hooked(fill_after_alpha)(**kwargs)
			self.writer_con = writer.con.con
			return writer

		with self.assertRaisesRegex(StorageError, 'full'):
			await self.collect(TARGETS, writer_factory=factory)
		self.assertEqual(self.channel_rows(BETA)['messages'], [])
		self.assertEqual(len(self.channel_rows(ALPHA)['messages']), 7)
		self.assertInterrupted('full')

	async def test_invalid_json_is_refused_without_a_partial_channel(self):
		client = self.client()
		client.world.histories[BETA][0]['views'] = float('nan')
		with self.assertRaisesRegex(StorageError, 'cannot save beta_fx: Out of range float values'):
			await self.collect(TARGETS, client)
		self.assertEqual(self.channel_rows(BETA)['channel_snapshots'], [])
		self.assertEqual(len(self.channel_rows(ALPHA)['messages']), 7)
		self.assertInterrupted('beta_fx')


class RunFailureTests(FaultCase):

	async def test_failed_finish_marks_the_run_interrupted_and_keeps_observations(self):
		def fail_counters(sql, seen):
			if 'INSERT INTO run_counters' in sql:
				raise sqlite3.OperationalError('injected failure')

		with self.assertRaisesRegex(StorageError, 'injected failure'):
			await self.collect(TARGETS, writer_factory=self.hooked(fail_counters))
		self.assertEqual(self.rows('SELECT count(*) FROM run_counters'), [(0,)])
		self.assertEqual(self.rows('SELECT count(*) FROM run_requests'), [(0,)])
		self.assertGreater(self.rows('SELECT count(*) FROM run_observations')[0][0], 0)
		self.assertEqual(self.rows("SELECT count(*) FROM histories"), [(3,)])
		self.assertInterrupted('injected failure')

	async def test_failed_finalize_still_finishes_the_run(self):
		def fail_observations(sql, seen):
			if 'INSERT INTO run_observations' in sql:
				raise sqlite3.OperationalError('injected failure')

		with self.assertRaisesRegex(StorageError, 'injected failure'):
			await self.collect(TARGETS, writer_factory=self.hooked(fail_observations))
		self.assertEqual(self.rows('SELECT count(*) FROM run_observations'), [(0,)])
		self.assertEqual(self.rows('SELECT count(*) FROM run_counters'), [(0,)])
		self.assertGreater(self.rows('SELECT count(*) FROM run_requests')[0][0], 0)
		self.assertEqual(
			self.rows('SELECT status FROM run_targets ORDER BY input_index'),
			[('succeeded',), ('succeeded',), ('succeeded',)])
		self.assertInterrupted('injected failure')

	async def test_refused_database_stops_before_any_request(self):
		os.makedirs(self.out)
		with open(self.db, 'wb') as fh:
			fh.write(b'not a database')
		with self.assertRaises(DatabaseRefused):
			await self.collect(TARGETS)
		self.assertEqual(self.last_client.requests, [])
		self.assertEqual([name for name, _ in self.last_client.calls if name == 'get_entity'], [])
		self.assertEqual(os.listdir(self.out), ['telegram-tracker.sqlite3'])
		self.assertEqual(sqlite_support.read_bytes(self.db), b'not a database')
		self.assertEqual(self.last_client.disconnect_count, 1)

	async def test_cancellation_keeps_completed_channels(self):
		client = self.client()
		started = asyncio.Event()

		async def block():
			started.set()
			await asyncio.Event().wait()

		client.script('GetHistoryRequest', None, None, None, None, None, block)
		task = asyncio.create_task(self.collect(TARGETS, client))
		await started.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertEqual(
			self.rows('SELECT status FROM run_targets ORDER BY input_index'),
			[('succeeded',), ('cancelled',), ('cancelled',)])
		self.assertEqual(self.rows('SELECT channel_id FROM histories'), [(ALPHA,)])
		self.assertEqual(
			self.rows('SELECT DISTINCT json_extract(sources, "$[0]") FROM run_counters'), [('alpha_fx',)])
		self.assertInterrupted('cancelled')
