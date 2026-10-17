# -*- coding: utf-8 -*-
'''
Collection scheduler: bounded channel workers under one request policy, on
the virtual-time loop with fake latency. Covers overlap and the in-flight
bound, output equivalence for N=1/3/10, alias coalescing, the discovered
private chat, account stops, shared flood pauses, transient
failures, rejected sessions, cancellation, the result-queue bound, no
write handles across requests, no hidden throttle, target normalization,
and CLI wiring of the controls.
'''

# import modules
import asyncio
import contextlib
import inspect
import io
import json
import os
import tempfile
import unittest

from unittest import mock

import offline_guard
import scheduler_experiment as experiment
import virtual_time

from telethon import errors

from api.auth import ensure_authorized
from fakes import FakeTelegramClient, FixtureWorld

import api
import cli
import cli.collect
import collector
import storage
import storage.files
import storage.records


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


class SchedulerTestCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(self.tmp, 'data')

	def client(self, latency=experiment.LATENCY, **kwargs):
		return FakeTelegramClient(
			FixtureWorld(raw=experiment.scheduler_world()),
			page_size=experiment.PAGE_SIZE, latency=latency, **kwargs
		)

	def collect(self, concurrency=3, **kwargs):
		return experiment.collect(concurrency, self.out, **kwargs)

	def status(self):
		return read_json(os.path.join(self.out, storage.records.STATUS_FILE))


class ConcurrencyTests(SchedulerTestCase):

	def test_experiment_levels(self):
		rows, identical = experiment.run_experiment()
		self.assertTrue(identical, 'outputs differ between concurrency levels')
		by_n = {r['concurrency']: r for r in rows}
		self.assertEqual(by_n[1]['peak_in_flight'], 1)
		self.assertEqual(by_n[3]['peak_in_flight'], 3)
		self.assertLessEqual(by_n[10]['peak_in_flight'], 10)
		self.assertGreater(by_n[1]['virtual_seconds'], by_n[3]['virtual_seconds'])
		self.assertGreater(by_n[3]['virtual_seconds'], by_n[10]['virtual_seconds'])
		for row in rows:
			self.assertEqual(row['requests'], by_n[1]['requests'])
			self.assertEqual((row['succeeded'], row['failed'], row['duplicates']), (10, 2, 1))
			self.assertEqual((row['connects'], row['disconnects']), (1, 1))

	def test_serial_run_has_no_overlap_and_no_hidden_throttle(self):
		summary, client, elapsed = self.collect(1, latency=0)
		times = [t for t, _, _ in client.dispatches]
		self.assertEqual(times, [i * experiment.INTERVAL for i in range(len(times))])
		self.assertEqual(elapsed, (len(times) - 1) * experiment.INTERVAL)
		self.assertNotIn('pause', inspect.signature(collector.run_collection).parameters)
		self.assertFalse(hasattr(collector, 'BATCH_PAUSE_SECONDS'))

	def test_in_flight_bound_includes_enrichment(self):
		for n in (2, 3):
			with self.subTest(concurrency=n):
				out = os.path.join(self.tmp, f'n{n}')
				# slow enrichment so it overlaps history pages of other channels
				latency = lambda kind, key: 3.0 if kind == 'GetFullChannelRequest' else 1.0
				summary, client, _ = experiment.collect(n, out, latency=latency)
				self.assertEqual(client.peak_active, n)
				self.assertLessEqual(summary.peak_in_flight, n)
				self.assertLessEqual(summary.peak_queued_results, n)

	def test_result_queue_is_bounded_by_the_worker_count(self):
		created = []
		real_queue = asyncio.Queue

		class RecordingQueue(real_queue):
			def __init__(self, maxsize=0):
				super().__init__(maxsize)
				created.append(maxsize)

		with mock.patch.object(asyncio, 'Queue', RecordingQueue):
			summary, _, _ = self.collect(3)
		self.assertEqual(created, [3])
		self.assertLessEqual(summary.peak_queued_results, 3)

	def test_login_once_before_workers_and_one_disconnect(self):
		calls = []

		async def authorize(client):
			calls.append(len(client.dispatches))
			await ensure_authorized(client, None)

		client = self.client()
		experiment.collect(3, self.out, client=client, authorize=authorize)
		self.assertEqual(calls, [0])
		self.assertEqual(client.call_names()[:2], ['connect', 'is_user_authorized'])
		self.assertEqual((client.connect_count, client.disconnect_count), (1, 1))
		self.assertEqual(client.active_at_disconnect, 0)


class OutcomeTests(SchedulerTestCase):

	def test_alias_private_discovered_chat_and_partial_state(self):
		summary, _, _ = self.collect(3)
		status = self.status()
		self.assertEqual(status['state'], storage.records.PARTIAL)
		alias = next(t for t in status['targets'] if t['input'] == experiment.ALIAS)
		self.assertEqual((alias['status'], alias['alias_of']), ('duplicate', 'gen01_fx'))
		self.assertFalse(os.path.exists(os.path.join(self.out, experiment.ALIAS)))
		self.assertEqual(summary.duplicates, [(experiment.ALIAS, 'gen01_fx')])
		# A private discovered chat is logged without stopping the run
		with open(os.path.join(self.out, storage.records.USERS_NOT_FOUND), encoding='utf-8') as fh:
			self.assertIn(f'ID - {experiment.PRIVATE_CHAT}', fh.read().splitlines())
		self.assertEqual(len(summary.succeeded), 10)
		self.assertEqual([t for t, _ in summary.failed], ['missing_fx', 'private_fx'])
		self.assertEqual(status['parameters']['concurrency'], {'requested': 3, 'effective': 3})
		self.assertEqual(status['summary']['duplicates'], 1)

	def test_discovered_private_chat_in_the_base_fixture_batch(self):
		raw = experiment.scheduler_world(channels=2)
		targets = ['alpha_fx', 'gen02_fx', 'beta_fx']
		summary, _, _ = self.collect(3, raw=raw, targets=targets)
		self.assertEqual(summary.succeeded, targets)
		self.assertIsNone(summary.stop_reason)
		with open(os.path.join(self.out, storage.records.USERS_NOT_FOUND), encoding='utf-8') as fh:
			self.assertEqual(fh.read().splitlines(), ['ID - 401', 'ID - 203'])

	def test_transient_failures_fail_only_that_channel(self):
		client = self.client()
		failures = [errors.ServerError(None, 'INTERNAL') for _ in range(4)]
		client.script('GetHistoryRequest', *failures)
		summary, _, _ = self.collect(1, client=client)
		self.assertEqual(summary.failed[0][0], 'gen01_fx')
		self.assertIn('history request failed after 4 attempt(s)', summary.failed[0][1])
		self.assertEqual(len(summary.succeeded), 9)
		self.assertEqual(summary.requests['history']['retries'], 3)
		self.assertEqual(self.status()['state'], storage.records.PARTIAL)

	def test_rejected_session_stops_the_run(self):
		client = self.client()
		client.script('GetHistoryRequest', None, None, errors.AuthKeyUnregisteredError(request=None))
		summary, _, _ = self.collect(3, client=client)
		self.assertIn('session rejected', summary.stop_reason)
		self.assertIn('AuthKeyUnregisteredError', summary.stop_reason)
		self.assertEqual(self.status()['state'], storage.records.INTERRUPTED)
		self.assertEqual(client.disconnect_count, 1)


class FloodTests(SchedulerTestCase):

	def flood_client(self, seconds, after):
		'''
		A client whose history request number `after` + 1 answers with a
		flood wait; records when it was answered.
		'''
		client = self.client()
		answered = []

		async def flood():
			answered.append(asyncio.get_running_loop().time())
			raise errors.FloodWaitError(request=None, capture=seconds)

		client.script('GetHistoryRequest', *([None] * after), flood)
		return client, answered

	def test_flood_pauses_every_worker(self):
		client, answered = self.flood_client(5, after=4)
		summary, _, _ = self.collect(3, client=client)
		t = answered[0]
		self.assertFalse([d for d, _, _ in client.dispatches if t < d < t + 5])
		self.assertTrue([d for d, _, _ in client.dispatches if d >= t + 5])
		self.assertEqual(len(summary.succeeded), 10)
		history = summary.requests['history']
		self.assertEqual((history['flood_waits'], history['flood_wait_seconds'], history['retries']), (1, 5, 1))
		self.assertEqual(self.status()['requests'], summary.requests)

	def test_flood_over_the_cap_stops_the_account(self):
		client, answered = self.flood_client(120, after=20)
		summary, _, _ = self.collect(3, client=client)
		self.assertTrue(summary.stop_reason.startswith('account stopped'))
		self.assertIn('120 s', summary.stop_reason)
		# requests already in flight may finish; none is admitted afterwards
		self.assertFalse([d for d, _, _ in client.dispatches if d > answered[0]])
		status = self.status()
		self.assertEqual(status['state'], storage.records.INTERRUPTED)
		done = [t['input'] for t in status['targets'] if t['status'] == 'succeeded']
		self.assertTrue(done)
		for target in done:
			read_json(storage.files.history_path(self.out, target))
		self.assertTrue([t for t in status['targets'] if t['status'] == 'cancelled'])
		self.assertEqual((client.disconnect_count, client.active_at_disconnect), (1, 0))
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))


class CancellationTests(SchedulerTestCase):

	def test_cancel_after_mixed_failures_keeps_completed_output(self):
		client = self.client()

		async def main():
			task = asyncio.create_task(collector.run_collection(
				experiment.batch_targets(),
				self.out,
				client_factory=lambda: client,
				authorize=lambda c: ensure_authorized(c, None),
				concurrency=3
			))
			await asyncio.sleep(12)
			task.cancel()
			with self.assertRaises(asyncio.CancelledError):
				await task

		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
			virtual_time.run(main())

		status = self.status()
		self.assertEqual((status['state'], status['stop_reason']), (storage.records.INTERRUPTED, 'cancelled'))
		states = {t['status'] for t in status['targets']}
		self.assertTrue({'succeeded', 'cancelled'} <= states)
		for entry in status['targets']:
			folder = os.path.join(self.out, entry['input'])
			if entry['status'] == 'succeeded':
				read_json(storage.files.history_path(self.out, entry['input']))
			else:
				self.assertFalse(os.path.exists(folder), entry)
		self.assertEqual((client.disconnect_count, client.active_at_disconnect), (1, 0))
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))
		self.assertEqual([f for f in os.listdir(self.out) if '.tmp' in f], [])


class WriteHandleTests(SchedulerTestCase):

	def test_no_output_file_is_open_while_a_request_is_in_flight(self):
		open_files = []
		violations = []
		real_open = open

		def tracking_open(*args, **kwargs):
			handle = real_open(*args, **kwargs)
			open_files.append(handle)
			return handle

		def latency(kind, key):
			if [h for h in open_files if not h.closed]:
				violations.append((kind, key))
			return 1.0

		with mock.patch('storage.open', tracking_open, create=True), \
				mock.patch('storage.files.open', tracking_open, create=True):
			self.collect(3, latency=latency)
		self.assertTrue(open_files)
		self.assertEqual(violations, [])


class TargetTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.calls = []

		async def fake_run_collection(targets, output_folder, **kwargs):
			self.calls.append({'targets': targets, **kwargs})
			return collector.CollectionSummary(
				requested=targets, output_folder=output_folder, succeeded=list(targets)
			)

		patcher = mock.patch.object(collector, 'run_collection', fake_run_collection)
		patcher.start()
		self.addCleanup(patcher.stop)

	def batch(self, *lines):
		path = os.path.join(self.tmp, 'channels.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('\n'.join(lines) + '\n')
		return path

	def run_cli(self, argv):
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = cli.main(argv, client_factory=lambda: None, authorize=lambda c: None)
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def test_normalization_and_deduplication(self):
		batch = self.batch(
			'@gen01_fx', 'GEN01_FX', 'https://t.me/gen01_fx', '', '  t.me/s/gen02_fx/  ',
			'telegram.me/gen01_old_fx', '-1001234', '1234567', 'gen02_fx'
		)
		code, out, _ = self.run_cli(['collect', '--batch-file', batch])
		self.assertEqual(code, 0)
		call = self.calls[0]
		self.assertEqual(call['targets'], ['gen01_fx', 'gen02_fx', 'gen01_old_fx', '-1001234', '1234567'])
		self.assertEqual(call['targets_given'], 8)
		self.assertIn('Targets: 5 (3 duplicate line(s) skipped)', out)

	def test_unsupported_targets_are_rejected_with_line_numbers(self):
		for bad in ('..\\..\\Windows', '../etc', 'C:\\data\\x', 'folder/name', 't.me/joinchat/AbCd',
				't.me/+AbCd', 't.me/name/123', 'two words', 'name.json'):
			with self.subTest(target=bad):
				code, _, err = self.run_cli(['collect', '--batch-file', self.batch('ok_fx', bad)])
				self.assertEqual(code, 2)
				self.assertIn('line 2: unsupported target', err)
				self.assertEqual(self.calls, [])
		code, _, err = self.run_cli(['collect', '--telegram-channel', '../x'])
		self.assertEqual(code, 2)
		self.assertIn('--telegram-channel: unsupported target', err)

	def test_controls_reach_the_collector(self):
		batch = self.batch('a_fx', 'b_fx')
		cases = (
			(['collect', '--batch-file', batch], 3, 3),
			(['collect', '--batch-file', batch, '--concurrency', '7'], 7, 7),
			(['--batch-file', batch], 3, 3),
			(['collect', '--telegram-channel', 'a_fx', '--concurrency', '5'], 1, 5),
			(['--telegram-channel', 'a_fx'], 1, 1),
		)
		for argv, concurrency, requested in cases:
			with self.subTest(argv=argv):
				del self.calls[:]
				code, _, _ = self.run_cli(argv)
				self.assertEqual(code, 0)
				call = self.calls[0]
				self.assertEqual((call['concurrency'], call['requested_concurrency']), (concurrency, requested))
				self.assertEqual(
					(call['request_interval'], call['max_flood_wait'], call['max_retries']),
					(0.25, 60, 3)
				)

		del self.calls[:]
		self.run_cli(['collect', '--batch-file', batch, '--request-interval', '1.5',
			'--max-flood-wait', '0', '--max-retries', '0'])
		call = self.calls[0]
		self.assertEqual((call['request_interval'], call['max_flood_wait'], call['max_retries']), (1.5, 0, 0))

	def test_legacy_form_rejects_control_flags(self):
		code, _, err = self.run_cli(['--batch-file', self.batch('a_fx'), '--concurrency', '2'])
		self.assertEqual(code, 2)
		self.assertIn('unrecognized arguments: --concurrency', err)


class CliRunTests(SchedulerTestCase):

	def run_cli(self, argv, client):
		stdout, stderr = io.StringIO(), io.StringIO()
		with mock.patch.object(cli.collect, 'asyncio_run', virtual_time.run), \
				contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = cli.main(argv, client_factory=lambda: client)
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def batch(self):
		path = os.path.join(self.tmp, 'channels.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write('\n'.join(experiment.batch_targets()) + '\ngen01_fx\n')
		return path

	def test_summary_lists_counts_and_request_statistics(self):
		code, out, err = self.run_cli(['collect', '--batch-file', self.batch(), '-o', self.out], self.client())
		self.assertEqual(code, 1)
		self.assertIn('> Targets: 14 given, 13 after removing duplicates; 10 succeeded, 2 failed, '
			'1 same channel as an earlier target, 0 cancelled. Concurrency 3.', out)
		self.assertIn('>   gen01_old_fx: same channel as gen01_fx, skipped', out)
		self.assertIn('> Requests by method class (sent / retries / flood waits / flood wait seconds):', out)
		status = read_json(os.path.join(self.out, storage.records.STATUS_FILE))
		for name, stats in status['requests'].items():
			self.assertIn(
				f'>   {name:<11} {stats["requests"]} / {stats["retries"]} / '
				f'{stats["flood_waits"]} / {stats["flood_wait_seconds"]}', out
			)
		self.assertEqual(set(status['requests']), {'resolve', 'channel', 'history', 'enrichment'})
		self.assertIn('2 of 13 targets failed: missing_fx, private_fx', err)

	def test_account_stop_exits_1(self):
		client = self.client()
		client.script('GetHistoryRequest', errors.FloodWaitError(request=None, capture=999))
		code, _, err = self.run_cli(['collect', '--batch-file', self.batch(), '-o', self.out], client)
		self.assertEqual(code, 1)
		self.assertIn('> Stopped: account stopped: Telegram asked to wait 999 s', err)
		self.assertIn('target(s) not collected', err)


class ClientSettingsTests(offline_guard.OfflineTestCase):

	def test_create_client_hands_retries_and_floods_to_the_policy(self):
		created = []

		class Recorder:
			def __init__(self, *args, **kwargs):
				created.append((args, kwargs))

		with mock.patch.object(api, 'TelegramClient', Recorder):
			api.create_client('session_file', 1, 'hash')
		args, kwargs = created[0]
		self.assertEqual(args, ('session_file', 1, 'hash'))
		self.assertEqual(kwargs['flood_sleep_threshold'], 0)
		self.assertEqual(kwargs['request_retries'], 1)
		self.assertTrue(kwargs['raise_last_call_error'])
		self.assertFalse(kwargs['receive_updates'])


if __name__ == '__main__':
	unittest.main()
