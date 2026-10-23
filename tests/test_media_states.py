# -*- coding: utf-8 -*-
'''
Ledger item states and runs: every allowed and refused transition, both
through the Ledger API and through raw SQL (the DDL's triggers), the
reasons, immutable rows, requests and coverage, blobs shared by
messages, attempts, counters, and dead-run recovery.
'''

# import modules
import itertools
import json
import sqlite3
import unittest

import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import CHANNEL, PROFILE_ID, RUN_A, RUN_B, RUN_C, SELECTION, SHA_A, SHA_B, MediaTestCase, \
	document_media, photo_media, ref_of
from telegram_tracker.media.ledger import COMPLETE, DOWNLOADING, FAIL_REASONS, FAILED, REQUESTED, SKIP_REASONS, \
	SKIPPED, STATES, TRANSITIONS, AttemptStats, InvalidTransition, LedgerError, RequestCounts, StartedRun
from telegram_tracker.storage.records import StorageError


KEY = (CHANNEL, 3, 5002)
REF = ref_of(document_media())


class StateTestCase(MediaTestCase):

	def setUp(self):
		super().setUp()
		self.ledger = self.files_ledger()
		self.ledger.start_run(RUN_A, 'download-media', selection=SELECTION, profile_name='work',
			profile_id=PROFILE_ID)

	def state(self, key=KEY):
		return self.ledger.item(key)['state']

	def put(self, state, key=KEY, ref=REF, run=RUN_A):
		'''
		Bring a new item to `state` through the API; returns its open
		attempt number (None unless downloading).
		'''
		self.ledger.request(run, key[0], [(key[1], ref)])
		if state == REQUESTED:
			return None
		if state == SKIPPED:
			self.ledger.skip(run, key, 'over_file_limit')
			return None
		if state == FAILED:
			self.ledger.fail(run, key, 'message_gone')
			return None
		attempt = self.ledger.begin_download(run, key)
		if state == COMPLETE:
			self.ledger.complete(run, key, sha256=f'{key[1]:064x}', actual_bytes=7, attempt=attempt)
			return None
		return attempt

	def move(self, state, key, attempt, run=RUN_A):
		'''
		Ask the API for `state`.
		'''
		if state == REQUESTED:
			self.ledger.request(run, key[0], [(key[1], REF)])
		elif state == DOWNLOADING:
			self.ledger.begin_download(run, key)
		elif state == COMPLETE:
			# a stored blob, so a requested item can complete by reuse
			self.ledger.complete(run, key, sha256=self.stored_sha, actual_bytes=7, attempt=attempt)
		elif state == SKIPPED:
			self.ledger.skip(run, key, 'over_run_limit', attempt=attempt)
		else:
			self.ledger.fail(run, key, 'request_failed', attempt=attempt)


class TransitionTests(StateTestCase):

	def test_every_pair_through_the_api(self):
		self.put(COMPLETE, (CHANNEL, 999, 5002))
		self.stored_sha = f'{999:064x}'
		for position, (source, target) in enumerate(itertools.product(STATES, STATES), start=1):
			with self.subTest(source=source, target=target):
				key = (CHANNEL, position, 5002)
				attempt = self.put(source, key)
				before = self.ledger.item(key)
				if target in TRANSITIONS[source]:
					self.move(target, key, attempt)
					self.assertEqual(self.state(key), target)
				elif target == REQUESTED:
					# a request leaves complete and downloading items as they are
					self.move(target, key, attempt)
					self.assertEqual(self.ledger.item(key), before)
				else:
					with self.assertRaises(InvalidTransition):
						self.move(target, key, attempt)
					self.assertEqual(self.ledger.item(key), before)

	def test_the_documented_table(self):
		self.assertEqual(
			{s: sorted(t) for s, t in TRANSITIONS.items()},
			{
				REQUESTED: sorted(STATES),
				DOWNLOADING: [COMPLETE, FAILED, SKIPPED],
				SKIPPED: [REQUESTED],
				FAILED: [REQUESTED],
				COMPLETE: []
			}
		)

	def raw_set(self, target, key):
		'''
		A raw UPDATE moving `key` to `target` with otherwise valid columns.
		'''
		if target == COMPLETE:
			self.ledger.con.execute(
				'INSERT OR IGNORE INTO blobs (channel_id, sha256, bytes, type, ext, path, first_run_uuid, '
				"created_at) VALUES (?, ?, 1, 'file', 'pdf', ?, ?, 't')",
				(CHANNEL, SHA_B, f'{CHANNEL}/files/1-{"b" * 16}.pdf', RUN_A))
			columns = f"state = 'complete', reason = NULL, sha256 = '{SHA_B}', actual_bytes = 1"
		elif target == SKIPPED:
			columns = "state = 'skipped', reason = 'over_run_limit', sha256 = NULL, actual_bytes = NULL"
		elif target == FAILED:
			columns = "state = 'failed', reason = 'disk_full', sha256 = NULL, actual_bytes = NULL"
		else:
			columns = f"state = '{target}', reason = NULL, sha256 = NULL, actual_bytes = NULL"
		self.ledger.con.execute(
			f'UPDATE items SET {columns} WHERE channel_id = ? AND message_id = ? AND media_id = ?', key)

	def test_every_pair_through_raw_sql(self):
		for position, (source, target) in enumerate(itertools.product(STATES, STATES), start=1):
			with self.subTest(source=source, target=target):
				key = (CHANNEL, position, 5002)
				self.put(source, key)
				if target in TRANSITIONS[source]:
					self.raw_set(target, key)
					self.assertEqual(self.state(key), target)
				else:
					with self.assertRaises(sqlite3.IntegrityError) as caught:
						self.raw_set(target, key)
					# a complete item is refused by either trigger, whichever fires first
					expected = {'invalid media item state transition'}
					if source == COMPLETE:
						expected.add('a complete media item never changes')
					self.assertIn(str(caught.exception), expected)
					self.assertEqual(self.state(key), source)

	def test_items_start_as_requested(self):
		for state in STATES[1:]:
			with self.subTest(state=state):
				with self.assertRaises(sqlite3.IntegrityError) as caught:
					self.ledger.con.execute(
						'INSERT INTO items (channel_id, message_id, media_id, media_type, media_kind, type, subtype, '
						'ext, state, reason, first_run_uuid, last_run_uuid, updated_at) '
						"VALUES (1, 1, 1, 'MessageMediaDocument', 'document', 'file', 'document', 'pdf', ?, ?, ?, ?, 't')",
						(state, {SKIPPED: 'over_run_limit', FAILED: 'disk_full'}.get(state), RUN_A, RUN_A))
				self.assertIn('starts as requested', str(caught.exception))


class ReasonTests(StateTestCase):

	def test_api_reasons(self):
		for position, reason in enumerate(SKIP_REASONS + FAIL_REASONS, start=1):
			with self.subTest(reason=reason):
				key = (CHANNEL, position, 5002)
				self.put(REQUESTED, key)
				end = self.ledger.skip if reason in SKIP_REASONS else self.ledger.fail
				end(RUN_A, key, reason)
				self.assertEqual(self.ledger.item(key)['reason'], reason)

	def test_reasons_belong_to_their_state(self):
		self.put(REQUESTED)
		cases = [
			(self.ledger.skip, 'disk_full'), (self.ledger.skip, None), (self.ledger.fail, 'over_file_limit'),
			(self.ledger.fail, None), (self.ledger.fail, 'gone fishing')
		]
		for end, reason in cases:
			with self.subTest(end=end.__name__, reason=reason):
				self.assertRaises(ValueError, end, RUN_A, KEY, reason)
				self.assertEqual(self.state(), REQUESTED)

	def test_the_ddl_checks_reasons(self):
		self.put(REQUESTED)
		bad = [
			"state = 'failed', reason = NULL", "state = 'failed', reason = 'over_file_limit'",
			"state = 'skipped', reason = 'disk_full'", "state = 'skipped', reason = NULL",
			"state = 'requested', reason = 'disk_full'", "state = 'failed', reason = 'gone fishing'"
		]
		for columns in bad:
			with self.subTest(columns):
				with self.assertRaises(sqlite3.IntegrityError):
					self.ledger.con.execute(f'UPDATE items SET {columns} WHERE message_id = 3')
		for reason in FAIL_REASONS:
			with self.subTest(reason=reason):
				self.ledger.con.execute("UPDATE items SET state = 'failed', reason = ? WHERE message_id = 3", (reason,))
				self.ledger.con.execute("UPDATE items SET state = 'requested', reason = NULL WHERE message_id = 3")

	def test_complete_needs_its_blob(self):
		self.put(DOWNLOADING)
		for columns in (
				"state = 'complete', sha256 = NULL, actual_bytes = 1",
				f"state = 'complete', sha256 = '{SHA_A}', actual_bytes = NULL",
				f"state = 'complete', sha256 = '{SHA_A}', actual_bytes = 1"):
			with self.subTest(columns):
				with self.assertRaises(sqlite3.IntegrityError):
					self.ledger.con.execute(f'UPDATE items SET {columns} WHERE message_id = 3')
		self.assertEqual(self.state(), DOWNLOADING)


class ImmutableRowTests(StateTestCase):

	def test_rows_are_never_deleted(self):
		self.put(COMPLETE)
		for table in ('ledger_meta', 'media_runs', 'blobs', 'items', 'attempts'):
			with self.subTest(table=table):
				with self.assertRaises(sqlite3.IntegrityError) as caught:
					self.ledger.con.execute(f'DELETE FROM {table}')
				self.assertIn('never deleted', str(caught.exception))
				self.assertGreater(self.ledger.con.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

	def test_final_rows_never_change(self):
		self.put(COMPLETE)
		updates = {
			'ledger_meta': "UPDATE ledger_meta SET created_at = 'x'",
			'blobs': "UPDATE blobs SET created_at = 'x'",
			'complete item': "UPDATE items SET updated_at = 'x'",
			'ended attempt': "UPDATE attempts SET app_requests = 9"
		}
		for label, sql in updates.items():
			with self.subTest(label):
				with self.assertRaises(sqlite3.IntegrityError) as caught:
					self.ledger.con.execute(sql)
				self.assertIn('never changes', str(caught.exception))


class RequestTests(StateTestCase):

	def test_counts_and_coverage(self):
		refs = [(m, ref_of(document_media(media_id=m))) for m in (1, 2, 3, 4, 5)]
		self.assertEqual(self.ledger.request(RUN_A, CHANNEL, refs), RequestCounts(new=5))
		self.ledger.skip(RUN_A, (CHANNEL, 2, 2), 'over_file_limit')
		self.ledger.fail(RUN_A, (CHANNEL, 3, 3), 'request_failed')
		attempt = self.ledger.begin_download(RUN_A, (CHANNEL, 4, 4))
		self.ledger.begin_download(RUN_A, (CHANNEL, 5, 5))
		self.ledger.complete(RUN_A, (CHANNEL, 4, 4), sha256=SHA_A, actual_bytes=1, attempt=attempt)
		refs.append((6, ref_of(photo_media(media_id=6))))
		self.assertEqual(
			self.ledger.request(RUN_A, CHANNEL, refs), RequestCounts(new=1, again=3, complete=1, downloading=1))
		self.assertEqual([i['state'] for i in self.ledger.items()],
			[REQUESTED, REQUESTED, REQUESTED, COMPLETE, DOWNLOADING, REQUESTED])
		self.assertIsNone(self.ledger.item((CHANNEL, 2, 2))['reason'])
		self.ledger.request(RUN_A, 7, [(1, REF)])
		self.ledger.request(RUN_A, 3, [])
		self.assertEqual(json.loads(self.ledger.run(RUN_A)['channels']), [3, 7, CHANNEL])

	def test_rows_carry_classification_and_provenance(self):
		photo = ref_of(photo_media(media_id=-6))
		self.ledger.request(RUN_A, CHANNEL, [(9, photo)], source_run='collection-run-1')
		item = self.ledger.item((CHANNEL, 9, -6))
		self.assertEqual(
			{k: item[k] for k in ('media_type', 'media_kind', 'photo_size', 'type', 'subtype', 'mime_type', 'ext',
				'expected_bytes', 'state', 'reason', 'attempts', 'source_run', 'first_run_uuid', 'last_run_uuid',
				'profile_name', 'profile_id', 'sha256', 'actual_bytes')},
			{'media_type': 'MessageMediaPhoto', 'media_kind': 'photo', 'photo_size': 'y', 'type': 'image',
				'subtype': 'photo', 'mime_type': 'image/jpeg', 'ext': 'jpg', 'expected_bytes': 15000,
				'state': REQUESTED, 'reason': None, 'attempts': 0, 'source_run': 'collection-run-1',
				'first_run_uuid': RUN_A, 'last_run_uuid': RUN_A, 'profile_name': 'work', 'profile_id': PROFILE_ID,
				'sha256': None, 'actual_bytes': None}
		)

	def test_a_later_run_takes_the_item_over(self):
		self.put(FAILED)
		self.ledger.finish_run(RUN_A, 'partial')
		self.ledger.start_run(RUN_B, 'collect', selection=SELECTION, collection_run='run-x')
		self.ledger.request(RUN_B, CHANNEL, [(3, REF)], source_run='run-x')
		item = self.ledger.item(KEY)
		self.assertEqual(
			(item['state'], item['first_run_uuid'], item['last_run_uuid'], item['profile_name'], item['source_run']),
			(REQUESTED, RUN_A, RUN_B, None, 'run-x')
		)

	def test_a_failed_request_writes_nothing(self):
		bad = ref_of(document_media(media_id=8))
		bad = type(bad)(**{**bad.__dict__, 'subtype': 'hologram'})
		with self.assertRaises(StorageError):
			self.ledger.request(RUN_A, CHANNEL, [(1, REF), (2, bad)])
		self.assertEqual(self.ledger.items(), [])
		self.assertEqual(self.ledger.run(RUN_A)['channels'], '[]')

	def test_writes_need_a_running_run(self):
		self.put(DOWNLOADING)
		self.ledger.finish_run(RUN_A, 'complete')
		calls = {
			'request': lambda: self.ledger.request(RUN_A, CHANNEL, [(4, REF)]),
			'begin': lambda: self.ledger.begin_download(RUN_A, KEY),
			'fail': lambda: self.ledger.fail(RUN_A, KEY, 'disk_full'),
			'counters': lambda: self.ledger.add_counters(RUN_A, retries=1),
			'unknown run': lambda: self.ledger.request(RUN_C, CHANNEL, [(4, REF)])
		}
		for label, call in calls.items():
			with self.subTest(label):
				with self.assertRaises(LedgerError) as caught:
					call()
				self.assertIn('is not running', str(caught.exception))


class BlobTests(StateTestCase):

	def test_messages_with_the_same_content_share_a_blob(self):
		first, second = (CHANNEL, 3, 5002), (CHANNEL, 4, 7777)
		self.put(REQUESTED, first)
		self.put(REQUESTED, second, ref_of(document_media(media_id=7777)))
		attempt = self.ledger.begin_download(RUN_A, first)
		planned = self.ledger.blob_path(first, SHA_A)
		self.assertEqual(planned, f'{CHANNEL}/files/3-{"a" * 16}.pdf')
		self.assertEqual(self.ledger.complete(RUN_A, first, sha256=SHA_A, actual_bytes=5, attempt=attempt), planned)
		# the second message's content turns out to be stored already
		self.assertEqual(self.ledger.blob_path(second, SHA_A), planned)
		self.assertEqual(self.ledger.complete(RUN_A, second, sha256=SHA_A, actual_bytes=5), planned)
		self.assertEqual(self.ledger.con.execute('SELECT count(*) FROM blobs').fetchone(), (1,))
		self.assertEqual(self.ledger.attempts(second), [])
		self.assertEqual(self.ledger.blob(CHANNEL, SHA_A)['first_run_uuid'], RUN_A)

	def test_a_size_that_contradicts_the_blob_is_refused(self):
		self.put(COMPLETE, (CHANNEL, 3, 5002))
		self.put(REQUESTED, (CHANNEL, 4, 5002))
		with self.assertRaises(LedgerError):
			self.ledger.complete(RUN_A, (CHANNEL, 4, 5002), sha256=f'{3:064x}', actual_bytes=8)
		self.assertEqual(self.state((CHANNEL, 4, 5002)), REQUESTED)

	def test_blob_hint(self):
		self.assertIsNone(self.ledger.blob_hint(CHANNEL, 'document', 5002))
		self.put(COMPLETE)
		hint = self.ledger.blob_hint(CHANNEL, 'document', 5002)
		self.assertEqual(hint['sha256'], f'{3:064x}')
		self.assertIsNone(self.ledger.blob_hint(CHANNEL, 'photo', 5002))
		self.assertIsNone(self.ledger.blob_hint(CHANNEL + 1, 'document', 5002))
		self.put(FAILED, (CHANNEL, 9, 6000), ref_of(document_media(media_id=6000)))
		self.assertIsNone(self.ledger.blob_hint(CHANNEL, 'document', 6000))

	def test_a_requested_item_completes_only_by_reuse(self):
		self.put(REQUESTED)
		with self.assertRaises(LedgerError) as caught:
			self.ledger.complete(RUN_A, KEY, sha256=SHA_A, actual_bytes=1)
		self.assertIn('can only reuse a stored file', str(caught.exception))
		self.assertEqual(self.state(), REQUESTED)
		self.assertIsNone(self.ledger.blob(CHANNEL, SHA_A))

	def test_blob_path_of_an_unknown_item(self):
		with self.assertRaises(LedgerError):
			self.ledger.blob_path(KEY, SHA_A)


class AttemptTests(StateTestCase):

	def test_attempts_are_numbered_across_runs_and_record_stats(self):
		attempt = self.put(DOWNLOADING)
		self.assertEqual(attempt, 1)
		self.ledger.fail(RUN_A, KEY, 'request_failed', attempt=1,
			stats=AttemptStats(bytes_received=1024, app_requests=3, refreshed=True))
		self.ledger.finish_run(RUN_A, 'partial')
		self.ledger.start_run(RUN_B, 'download-media', selection=SELECTION)
		self.ledger.request(RUN_B, CHANNEL, [(3, REF)])
		self.assertEqual(self.ledger.begin_download(RUN_B, KEY), 2)
		self.ledger.skip(RUN_B, KEY, 'over_run_limit', attempt=2)
		rows = [(a['n'], a['run_uuid'], a['outcome'], a['reason'], a['bytes_received'], a['app_requests'],
			a['refreshed']) for a in self.ledger.attempts(KEY)]
		self.assertEqual(rows, [
			(1, RUN_A, FAILED, 'request_failed', 1024, 3, 1),
			(2, RUN_B, SKIPPED, 'over_run_limit', 0, 0, 0)
		])
		self.assertEqual(self.ledger.item(KEY)['attempts'], 2)
		self.assertTrue(all(a['ended_at'] for a in self.ledger.attempts(KEY)))

	def test_the_open_attempt_must_be_named(self):
		attempt = self.put(DOWNLOADING)
		for wrong in (None, attempt + 1, 0):
			with self.subTest(attempt=wrong):
				with self.assertRaises(LedgerError):
					self.ledger.fail(RUN_A, KEY, 'disk_full', attempt=wrong)
				self.assertEqual(self.state(), DOWNLOADING)

	def test_only_the_run_downloading_it_ends_it(self):
		self.put(DOWNLOADING)
		# a second running run cannot exist under the output lock; the
		# ledger refuses it anyway
		self.ledger.con.execute(
			"INSERT INTO media_runs (run_uuid, command, state, started_at, selection, app_version) "
			"VALUES (?, 'collect', 'running', 't', '{}', 'x')", (RUN_B,))
		with self.assertRaises(LedgerError):
			self.ledger.complete(RUN_B, KEY, sha256=SHA_A, actual_bytes=1, attempt=1)
		self.assertEqual(self.state(), DOWNLOADING)


class RunTests(StateTestCase):

	def test_run_row(self):
		run = self.ledger.run(RUN_A)
		self.assertEqual(
			(run['command'], run['state'], run['profile_name'], run['profile_id'], json.loads(run['selection']),
				run['channels'], run['finished_at']),
			('download-media', 'running', 'work', PROFILE_ID, SELECTION, '[]', None)
		)

	def test_counters(self):
		self.ledger.add_counters(RUN_A, app_requests=3, bytes_received=100)
		self.ledger.add_counters(RUN_A, app_requests=1, retries=2, flood_waits=1, refreshes=1)
		self.ledger.add_counters(RUN_A)
		run = self.ledger.run(RUN_A)
		self.assertEqual(
			[run[c] for c in ('app_requests', 'retries', 'flood_waits', 'refreshes', 'bytes_received')],
			[4, 2, 1, 1, 100]
		)
		for bad in ({'app_requests': -1}, {'bytes': 1}, {'retries': 1.0}, {'retries': True}):
			with self.subTest(bad=bad):
				self.assertRaises(ValueError, self.ledger.add_counters, RUN_A, **bad)

	def test_finish_interrupts_its_own_downloads(self):
		self.put(DOWNLOADING)
		self.put(REQUESTED, (CHANNEL, 4, 5002))
		self.ledger.finish_run(RUN_A, 'interrupted', stop_reason='SIGINT')
		self.assertEqual((self.state(), self.ledger.item(KEY)['reason']), (FAILED, 'interrupted'))
		self.assertEqual(self.state((CHANNEL, 4, 5002)), REQUESTED)
		attempt = self.ledger.attempts(KEY)[0]
		self.assertEqual((attempt['outcome'], attempt['reason']), (FAILED, 'interrupted'))
		run = self.ledger.run(RUN_A)
		self.assertEqual((run['state'], run['stop_reason']), ('interrupted', 'SIGINT'))
		self.assertIsNotNone(run['finished_at'])

	def test_finish_rules(self):
		self.assertRaises(ValueError, self.ledger.finish_run, RUN_A, 'running')
		self.ledger.finish_run(RUN_A, 'complete')
		with self.assertRaises(LedgerError):
			self.ledger.finish_run(RUN_A, 'partial')
		self.assertEqual(self.ledger.run(RUN_A)['state'], 'complete')
		self.assertRaises(ValueError, self.ledger.start_run, RUN_B, 'build', selection=SELECTION)
		with self.assertRaises(StorageError):
			self.ledger.start_run(RUN_A, 'collect', selection=SELECTION)


class RecoveryTests(StateTestCase):
	'''
	A run whose process ended is found by the next run's start.
	'''
	def test_dead_run_recovery(self):
		downloading = self.put(DOWNLOADING, (CHANNEL, 1, 5002))
		self.assertEqual(downloading, 1)
		self.put(REQUESTED, (CHANNEL, 2, 5002))
		self.put(COMPLETE, (CHANNEL, 3, 5002))
		self.put(SKIPPED, (CHANNEL, 4, 5002))
		before = {k: self.ledger.item((CHANNEL, k, 5002)) for k in (2, 3, 4)}

		# run A's process ended here: no finish_run
		started = self.ledger.start_run(RUN_B, 'download-media', selection=SELECTION, started_at='2024-02-02T00:00:00')
		self.assertEqual(started, StartedRun(stale_runs=(RUN_A,), interrupted_items=1))

		dead = self.ledger.run(RUN_A)
		self.assertEqual(dead['state'], 'interrupted')
		self.assertIn('found by the run started 2024-02-02T00:00:00', dead['stop_reason'])
		self.assertIsNone(dead['finished_at'])
		item = self.ledger.item((CHANNEL, 1, 5002))
		self.assertEqual((item['state'], item['reason'], item['last_run_uuid']), (FAILED, 'interrupted', RUN_A))
		attempt = self.ledger.attempts((CHANNEL, 1, 5002))[0]
		self.assertEqual((attempt['outcome'], attempt['reason']), (FAILED, 'interrupted'))
		self.assertIsNotNone(attempt['ended_at'])
		self.assertEqual({k: self.ledger.item((CHANNEL, k, 5002)) for k in (2, 3, 4)}, before)
		self.assertEqual(self.ledger.run(RUN_B)['state'], 'running')

		# the new run retries it
		self.ledger.request(RUN_B, CHANNEL, [(1, REF)])
		self.assertEqual(self.ledger.begin_download(RUN_B, (CHANNEL, 1, 5002)), 2)

	def test_a_clean_start_finds_nothing(self):
		self.ledger.finish_run(RUN_A, 'complete')
		self.assertEqual(
			self.ledger.start_run(RUN_B, 'collect', selection=SELECTION), StartedRun(stale_runs=(), interrupted_items=0))
		self.assertEqual(self.ledger.run(RUN_A)['state'], 'complete')

	def test_several_dead_runs(self):
		self.put(DOWNLOADING, (CHANNEL, 1, 5002))
		self.ledger.con.execute(
			"INSERT INTO media_runs (run_uuid, command, state, started_at, selection, app_version) "
			"VALUES (?, 'collect', 'running', '2099', '{}', 'x')", (RUN_B,))
		self.put(DOWNLOADING, (CHANNEL, 2, 5002), run=RUN_B)
		started = self.ledger.start_run(RUN_C, 'collect', selection=SELECTION)
		self.assertEqual(started, StartedRun(stale_runs=(RUN_A, RUN_B), interrupted_items=2))
		self.assertEqual([r['state'] for r in self.ledger.runs()], ['interrupted', 'running', 'interrupted'])


if __name__ == '__main__':
	unittest.main()
