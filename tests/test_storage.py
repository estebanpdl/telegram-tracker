# -*- coding: utf-8 -*-
'''
Collection persistence: one writer, completion-order-independent aggregates,
atomic files, reruns into an existing folder, the output lock, the
collection-status.json sidecar and builder gate, cancellation, storage
failures, and --update history merging.

Expected counts come from fixtures/expected/collection.json (static
inspection of the baseline); this module checks those counts together
with persistence, recovery, and update behavior.
'''

# import modules
import asyncio
import contextlib
import copy
import csv
import io
import json
import os
import tempfile
import unittest

from datetime import datetime, timedelta
from unittest import mock

import offline_guard

from api.auth import ensure_authorized
from fakes import FakeTelegramClient, FixtureWorld, load_expected

import cli
import collector
import datasets
import network
import storage
import storage.records

from collector import HistoryWindow, MetadataCache, Requests
from utils import DataInputError


EXPECTED = load_expected('collection.json')
SCENARIOS = EXPECTED['scenarios']
BATCH = SCENARIOS['batch']['targets']
ALPHA = 101


def read_bytes(path):
	with open(path, 'rb') as fh:
		return fh.read()


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def read_csv_rows(path):
	with open(path, encoding='utf-8', newline='') as fh:
		reader = csv.DictReader(fh)
		return reader.fieldnames, list(reader)


def snapshot(root):
	'''
	{relative path: bytes} for every file under root except the sidecar,
	whose timestamps differ between runs.
	'''
	files = {}
	for d, _, names in os.walk(root):
		for name in names:
			path = os.path.join(d, name)
			rel = os.path.relpath(path, root).replace('\\', '/')
			if rel != storage.records.STATUS_FILE:
				files[rel] = read_bytes(path)
	return files


def add_messages(world, channel_id, *ids, forward_from=None):
	'''
	Prepend new messages (copies of the newest one) to a fixture history.
	'''
	history = world.histories[channel_id]
	newest = history[0]
	for offset, msg_id in enumerate(sorted(ids, reverse=True)):
		message = copy.deepcopy(newest)
		message['id'] = msg_id
		message['date'] = newest['date'] + timedelta(days=len(ids) - offset)
		message['message'] = f'Synthetic new post {msg_id}'
		message['views'] = 1000 + msg_id
		if forward_from is not None:
			message['fwd_from'] = {
				'_': 'MessageFwdHeader',
				'from_id': {'_': 'PeerChannel', 'channel_id': forward_from},
				'date': newest['date'],
				'channel_post': 1
			}
		history.insert(0, message)


class StorageTestCase(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(tmp.name, 'data')
		self.stdout = io.StringIO()
		self.stderr = io.StringIO()

	async def collect(self, targets, client=None, out=None, **kwargs):
		self.client = client or FakeTelegramClient(page_size=EXPECTED['page_size'])
		self.factory_calls = 0

		def factory():
			self.factory_calls += 1
			return self.client

		with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
			return await collector.run_collection(
				targets,
				out or self.out,
				client_factory=factory,
				authorize=lambda c: ensure_authorized(c, None),
				**kwargs
			)

	def status(self, out=None):
		return read_json(os.path.join(out or self.out, storage.records.STATUS_FILE))

	def full_channel_requests(self, client=None):
		return [
			r['channel'] for r in (client or self.client).requests
			if r['type'] == 'GetFullChannelRequest'
		]


class ReducerTests(StorageTestCase):
	'''
	5.1 / 5.2: workers return results; one reducer replays them in input
	order.
	'''

	async def channel_results(self, client):
		cache = MetadataCache(Requests(client))
		results = []
		with contextlib.redirect_stdout(io.StringIO()):
			for index, target in enumerate(['alpha_fx', 'beta_fx', 'empty_fx']):
				results.append(await collector.collect_channel(
					cache.requests, cache, index, target, output_folder=self.out
				))
		await cache.close()
		return results, cache

	async def test_reversed_completion_order_gives_identical_files(self):
		results, cache = await self.channel_results(FakeTelegramClient(page_size=2))
		folders = []
		for order in (results, list(reversed(results))):
			out = os.path.join(self.tmp, f'out{len(folders)}')
			os.makedirs(out)
			writer = storage.OutputWriter(out)
			for result in order:
				writer.commit(result)
			writer.finalize(cache.outcomes)
			folders.append(snapshot(out))

		self.assertEqual(folders[0], folders[1])
		_, rows = read_csv_rows(os.path.join(self.tmp, 'out1', 'counter.csv'))
		self.assertEqual(
			[r['id'] for r in rows],
			[str(r['id']) for r in SCENARIOS['batch']['counter']]
		)

	async def test_results_carry_ordered_observations_and_no_files(self):
		results, _ = await self.channel_results(FakeTelegramClient(page_size=2))
		self.assertFalse(os.path.exists(self.out), 'a worker wrote output')
		alpha = results[0]
		self.assertEqual(alpha.index, 0)
		# pages after the first: (8, 7), (6, 5) forwarding 201, (4) forwarding
		# basic group 204, which has no username
		self.assertEqual(
			[(kind, [i for i, _ in chats]) for kind, chats in alpha.observations],
			[('channel_request', [101, 106]), ('from_messages', [101]),
				('from_messages', [101, 201]), ('from_messages', [101])]
		)

	def test_reducer_counts_match_the_sequential_contract(self):
		outcome_101 = storage.records.EnrichOutcome(linked=[(106, 'alpha_chat_fx')], rows=[{'id': 101}, {'id': 106}])
		outcome_106 = storage.records.EnrichOutcome(linked=[(101, 'alpha_fx')], rows=[{'id': 106}, {'id': 101}])
		result = storage.records.ChannelResult(index=0, target='alpha_fx', observations=[
			('channel_request', [(101, 'alpha_fx'), (106, 'alpha_chat_fx')]),
			('from_messages', [(101, 'alpha_fx')])
		])
		agg = storage.reduce_results([result], {101: outcome_101, 106: outcome_106})
		self.assertEqual(agg.chat_ids, [101, 106, 106, 101])
		self.assertEqual(agg.counter[101]['counter'], 2)
		self.assertEqual(agg.counter[106]['counter'], 2)
		# 106 was counted through 101's enrichment first, so its own
		# outcome is not applied
		self.assertEqual([r['id'] for r in agg.metadata], [101, 106])

	async def test_shared_chat_is_enriched_once(self):
		await self.collect(['alpha_fx', 'beta_fx'])
		requests = self.full_channel_requests()
		self.assertEqual(requests.count(201), 1)
		self.assertEqual(len(requests), len(set(requests)))
		_, rows = read_csv_rows(os.path.join(self.out, 'counter.csv'))
		shared = next(r for r in rows if r['id'] == '201')
		self.assertEqual((shared['counter'], shared['source']), ('2', "['alpha_fx', 'beta_fx']"))


class CacheTests(StorageTestCase):
	'''
	5.2: one in-flight request per chat; transient failures are not kept.
	'''

	async def test_concurrent_callers_share_one_request(self):
		client = FakeTelegramClient()
		gate = asyncio.Event()

		async def slow():
			await gate.wait()

		client.script('GetFullChannelRequest', slow)
		cache = MetadataCache(Requests(client))
		waiters = [asyncio.create_task(cache.full_channel(201, 'enrichment')) for _ in range(3)]
		await asyncio.sleep(0)
		gate.set()
		responses = await asyncio.gather(*waiters)
		self.assertEqual(self.full_channel_requests(client), [201])
		self.assertEqual({r.full_chat.id for r in responses}, {201})
		await cache.close()

	async def test_transient_failure_is_not_cached(self):
		client = FakeTelegramClient()
		client.script('GetFullChannelRequest', RuntimeError('server hiccup'))
		cache = MetadataCache(Requests(client))
		with self.assertRaises(RuntimeError):
			await cache.enrich(201)
		self.assertNotIn(201, cache.outcomes)
		await cache.enrich(201)
		self.assertEqual([i for i, _ in cache.outcomes[201].linked], [202])
		self.assertEqual(self.full_channel_requests(client), [201, 201, 202])
		await cache.close()

	async def test_access_errors_are_permanent_outcomes(self):
		client = FakeTelegramClient()
		cache = MetadataCache(Requests(client))
		await cache.enrich(203)
		await cache.enrich(203)
		self.assertEqual(cache.outcomes[203].error, 'not_found')
		self.assertEqual(self.full_channel_requests(client), [203])
		await cache.close()


class FileTests(StorageTestCase):
	'''
	5.3: atomic replacement, reruns, and no _x/_y columns.
	'''

	async def test_rerun_keeps_legacy_columns_and_earlier_rows(self):
		await self.collect(BATCH)
		first_header, first_rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		first_chats = read_bytes(os.path.join(self.out, 'chats.txt'))
		first_counter = read_bytes(os.path.join(self.out, 'counter.csv'))

		await self.collect(BATCH)
		header, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual(header, first_header)
		self.assertFalse([c for c in header if c.endswith(('_x', '_y'))])
		self.assertEqual(rows, first_rows + first_rows)
		self.assertEqual(read_bytes(os.path.join(self.out, 'chats.txt')), first_chats)
		self.assertEqual(read_bytes(os.path.join(self.out, 'counter.csv')), first_counter)
		with open(os.path.join(self.out, storage.records.CHANNELS_NOT_FOUND), encoding='utf-8') as fh:
			self.assertEqual(fh.read().splitlines(), ['missing_fx', 'private_fx'] * 2)

	async def test_rerun_with_other_channels_keeps_earlier_counts_and_builds(self):
		await self.collect(['alpha_fx'])
		_, alpha_rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		await self.collect(['beta_fx'])
		_, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual(rows[:len(alpha_rows)], alpha_rows)
		self.assertTrue(all(r['counter'] for r in rows))
		self.assertEqual(rows[-1]['source'], "['beta_fx']")
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')))
		with contextlib.redirect_stdout(io.StringIO()):
			summary = network.build_network(self.out, image=False)
		self.assertGreater(summary.edges, 0)

	async def test_run_without_new_rows_leaves_collected_chats_untouched(self):
		await self.collect(['alpha_fx'])
		path = os.path.join(self.out, 'collected_chats.csv')
		before = read_bytes(path)
		await self.collect(['missing_fx'])
		self.assertEqual(read_bytes(path), before)

	async def test_no_temporary_files_remain(self):
		await self.collect(BATCH)
		leftovers = [f for f in snapshot(self.out) if '.tmp' in f]
		self.assertEqual(leftovers, [])

	async def test_failure_between_replacements_keeps_earlier_history(self):
		await self.collect(['alpha_fx', 'beta_fx'])
		beta_messages = os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')
		before = read_bytes(beta_messages)
		real_replace = os.replace

		def failing_replace(src, dst):
			if dst.endswith('beta_fx_messages.json'):
				raise OSError(28, 'No space left on device')
			return real_replace(src, dst)

		with mock.patch.object(storage.os, 'replace', failing_replace):
			with self.assertRaises(storage.records.StorageError):
				await self.collect(['alpha_fx', 'beta_fx', 'empty_fx'])

		self.assertEqual(read_bytes(beta_messages), before)
		status = self.status()
		self.assertEqual(status['state'], storage.records.INTERRUPTED)
		self.assertIn('storage error', status['stop_reason'])
		self.assertEqual(
			[t['status'] for t in status['targets']],
			['succeeded', 'failed', 'cancelled']
		)
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))
		self.assertEqual([f for f in snapshot(self.out) if '.tmp' in f], [])

	async def test_failed_channel_keeps_its_earlier_files(self):
		await self.collect(['alpha_fx'])
		before = snapshot(self.out)
		client = FakeTelegramClient(page_size=2)

		async def replay_first_page():
			return client.world.page(ALPHA, page_size=2)

		client.script('GetHistoryRequest', None, replay_first_page)
		summary = await self.collect(['alpha_fx'], client=client)
		self.assertEqual([t for t, _ in summary.failed], ['alpha_fx'])
		after = snapshot(self.out)
		for name in ('alpha_fx/alpha_fx.json', 'alpha_fx/alpha_fx_messages.json'):
			self.assertEqual(after[name], before[name])


class LockTests(StorageTestCase):

	async def test_locked_folder_is_refused_before_connecting(self):
		os.makedirs(self.out)
		with open(storage.lock_path(self.out), 'w', encoding='utf-8') as fh:
			json.dump({'pid': 4242, 'started_at': '2026-10-17T00:00:00+00:00'}, fh)
		with self.assertRaises(storage.records.OutputLocked) as caught:
			await self.collect(['alpha_fx'])
		self.assertEqual(self.factory_calls, 0)
		self.assertIn('pid 4242', str(caught.exception))
		self.assertIn('fresh --output', str(caught.exception))
		self.assertTrue(os.path.exists(storage.lock_path(self.out)), 'stale lock was removed')

	async def test_lock_is_held_during_the_run_and_released(self):
		held = []
		client = FakeTelegramClient()

		async def look():
			held.append(os.path.exists(storage.lock_path(self.out)))

		client.script('GetHistoryRequest', look)
		await self.collect(['alpha_fx'], client=client)
		self.assertEqual(held, [True])
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))

	async def test_second_lock_on_the_same_folder_fails(self):
		os.makedirs(self.out)
		with storage.OutputLock(self.out):
			with self.assertRaises(storage.records.OutputLocked):
				with storage.OutputLock(self.out):
					pass
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))


class StatusTests(StorageTestCase):
	'''
	5.4: sidecar lifecycle and contents.
	'''

	async def test_partial_batch(self):
		await self.collect(BATCH)
		status = self.status()
		self.assertEqual(status['state'], storage.records.PARTIAL)
		self.assertEqual(status['summary'], {'given': 5, 'requested': 5, 'succeeded': 3, 'failed': 2, 'duplicates': 0, 'cancelled': 0})
		self.assertEqual(status['parameters']['mode'], 'full')
		by_input = {t['input']: t for t in status['targets']}
		self.assertEqual(by_input['alpha_fx']['history'], 'replaced')
		self.assertEqual(by_input['alpha_fx']['messages'], 7)
		self.assertEqual(by_input['empty_fx']['messages'], 0)
		self.assertEqual(by_input['empty_fx']['status'], 'succeeded')
		self.assertEqual(by_input['private_fx']['status'], 'failed')
		raw = json.dumps(status, ensure_ascii=False)
		for text in ('Synthetic update', 'مرحبا', 'api_hash', 'phone'):
			self.assertNotIn(text, raw)

	async def test_running_while_collecting(self):
		seen = []
		client = FakeTelegramClient()

		async def look():
			seen.append(self.status()['state'])

		client.script('GetHistoryRequest', look)
		await self.collect(['alpha_fx'], client=client)
		self.assertEqual(seen, [storage.records.RUNNING])
		self.assertEqual(self.status()['state'], storage.records.COMPLETE)

	async def test_window_and_metadata_parameters(self):
		window = HistoryWindow(min_id=6, max_messages=2)
		await self.collect(['alpha_fx'], window=window)
		parameters = self.status()['parameters']
		self.assertEqual(parameters['mode'], 'window')
		self.assertEqual(parameters['window']['min_id'], 6)
		self.assertEqual(parameters['window']['max_messages'], 2)
		self.assertEqual(parameters['window_description'], 'id > 6, newest 2 messages')

		await self.collect(['alpha_fx'], metadata_only=True)
		status = self.status()
		self.assertEqual(status['parameters']['mode'], 'metadata_only')
		entry = status['targets'][0]
		self.assertEqual((entry['history'], entry['stale_history']), ('not_collected', True))

	async def test_cancellation_keeps_completed_channels(self):
		client = FakeTelegramClient(page_size=2)
		started = asyncio.Event()

		async def block():
			started.set()
			await asyncio.Event().wait()

		# alpha_fx has 5 history pages; beta_fx blocks on its first page
		client.script('GetHistoryRequest', None, None, None, None, None, block)
		task = asyncio.create_task(self.collect(['alpha_fx', 'beta_fx', 'empty_fx'], client=client))
		await started.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task

		status = self.status()
		self.assertEqual(status['state'], storage.records.INTERRUPTED)
		self.assertEqual(status['stop_reason'], 'cancelled')
		self.assertEqual([t['status'] for t in status['targets']], ['succeeded', 'cancelled', 'cancelled'])
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')))
		self.assertFalse(os.path.exists(os.path.join(self.out, 'beta_fx')))
		_, rows = read_csv_rows(os.path.join(self.out, 'counter.csv'))
		self.assertEqual({r['source'] for r in rows}, {"['alpha_fx']"})
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertFalse(os.path.exists(storage.lock_path(self.out)))

	async def test_unexpected_error_interrupts_and_finalizes(self):
		client = FakeTelegramClient()
		client.script('GetFullChannelRequest', None, None, None, None, None, RuntimeError('boom'))
		with self.assertRaisesRegex(RuntimeError, 'boom'):
			await self.collect(['alpha_fx', 'beta_fx'], client=client)
		status = self.status()
		self.assertEqual(status['state'], storage.records.INTERRUPTED)
		self.assertIn('RuntimeError: boom', status['stop_reason'])
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'counter.csv')))


class BuilderGateTests(StorageTestCase):
	'''
	5.4: builders refuse running/interrupted generations and note partial
	or subset ones.
	'''

	def write_status(self, **fields):
		os.makedirs(self.out, exist_ok=True)
		data = {'format': 1, 'state': storage.records.COMPLETE, 'targets': [], 'parameters': {'mode': 'full'}}
		data.update(fields)
		with open(os.path.join(self.out, storage.records.STATUS_FILE), 'w', encoding='utf-8') as fh:
			json.dump(data, fh)

	def test_running_and_interrupted_are_rejected(self):
		for state, text in ((storage.records.RUNNING, 'still running'), (storage.records.INTERRUPTED, 'interrupted')):
			with self.subTest(state=state):
				self.write_status(state=state, stop_reason='cancelled')
				for build in (datasets.build_dataset, network.build_network):
					with self.assertRaisesRegex(DataInputError, text):
						build(self.out)
				self.assertEqual(sorted(os.listdir(self.out)), [storage.records.STATUS_FILE])

	def test_unreadable_sidecar_is_rejected(self):
		os.makedirs(self.out)
		with open(os.path.join(self.out, storage.records.STATUS_FILE), 'w', encoding='utf-8') as fh:
			fh.write('{"state": ')
		with self.assertRaisesRegex(DataInputError, 'cannot be read'):
			storage.check_collection_status(self.out)

	def test_notes(self):
		self.write_status(
			state=storage.records.PARTIAL,
			targets=[{'input': 'a_fx', 'status': 'succeeded'}, {'input': 'b_fx', 'status': 'failed'}],
			parameters={'mode': 'window', 'window_description': 'id > 6'}
		)
		notes = storage.check_collection_status(self.out)
		self.assertEqual(len(notes), 2)
		self.assertIn('1 failed target(s), excluded from its output: b_fx', notes[0])
		self.assertIn('history window (id > 6)', notes[1])
		self.assertEqual(len(storage.check_collection_status(self.out, messages=False)), 1)

		self.write_status(parameters={'mode': 'update'})
		self.assertIn('duplicating earlier rows', storage.check_collection_status(self.out)[0])

	def test_folder_without_sidecar_is_accepted(self):
		os.makedirs(self.out)
		self.assertEqual(storage.check_collection_status(self.out), [])

	async def test_builders_print_notes_for_a_partial_collection(self):
		await self.collect(BATCH)
		stderr = io.StringIO()
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
			datasets.build_dataset(self.out, progress=False)
			network.build_network(self.out, image=False)
		self.assertEqual(stderr.getvalue().count('2 failed target(s)'), 2)


class UpdateTests(StorageTestCase):
	'''
	5.3 / 5.4 / 5.6: --update.
	'''

	def messages_path(self, target='alpha_fx'):
		return storage.history_path(self.out, target)

	def history_requests(self):
		return [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']

	async def test_merge_new_posts(self):
		await self.collect(['alpha_fx'])
		earlier = read_json(self.messages_path())

		client = FakeTelegramClient(page_size=2)
		add_messages(client.world, ALPHA, 11, 12, forward_from=205)
		summary = await self.collect(['alpha_fx'], client=client, update=True)

		self.assertEqual(summary.succeeded, ['alpha_fx'])
		self.assertEqual({r['min_id'] for r in self.history_requests()}, {10})
		merged = read_json(self.messages_path())
		self.assertEqual([m['id'] for m in merged['messages']], [12, 11, 10, 9, 8, 7, 6, 5, 4])
		self.assertEqual(merged['messages'][2:], earlier['messages'])
		self.assertEqual(merged['count'], 9)
		chat_ids = [c['id'] for c in merged['chats']]
		self.assertEqual(chat_ids[:2], [101, 205])
		self.assertEqual(sorted(chat_ids), sorted(set(chat_ids)))
		self.assertTrue({c['id'] for c in earlier['chats']} <= set(chat_ids))
		self.assertEqual(list(merged), list(earlier))

		entry = self.status()['targets'][0]
		self.assertEqual(self.status()['parameters']['mode'], 'update')
		self.assertEqual((entry['history'], entry['min_id'], entry['messages']), ('merged', 10, 9))

	async def test_zero_new_posts_leaves_the_file_byte_identical(self):
		await self.collect(['alpha_fx'])
		before = read_bytes(self.messages_path())
		os.utime(self.messages_path(), (1, 1))
		await self.collect(['alpha_fx'], update=True)
		self.assertEqual(read_bytes(self.messages_path()), before)
		self.assertEqual(os.stat(self.messages_path()).st_mtime, 1)
		self.assertEqual(len(self.history_requests()), 1)
		self.assertEqual(self.status()['targets'][0]['history'], 'unchanged')

	async def test_failed_request_keeps_the_earlier_history(self):
		await self.collect(['alpha_fx'])
		before = read_bytes(self.messages_path())
		client = FakeTelegramClient(page_size=2)
		add_messages(client.world, ALPHA, 11, 12, 13)

		async def replay_first_page():
			return client.world.page(ALPHA, min_id=10, page_size=2)

		client.script('GetHistoryRequest', None, replay_first_page)
		summary = await self.collect(['alpha_fx'], client=client, update=True)
		self.assertEqual([t for t, _ in summary.failed], ['alpha_fx'])
		self.assertEqual(read_bytes(self.messages_path()), before)

		client = FakeTelegramClient(page_size=2)
		add_messages(client.world, ALPHA, 11)
		client.script('GetHistoryRequest', RuntimeError('connection reset'))
		with self.assertRaises(RuntimeError):
			await self.collect(['alpha_fx'], client=client, update=True)
		self.assertEqual(read_bytes(self.messages_path()), before)

	async def test_unreadable_history_fails_the_channel_untouched(self):
		cases = {
			'not json': b'{"messages": [',
			'wrong shape': b'{"messages": {"id": 1}}',
			'id not int': b'{"messages": [{"id": "10"}], "chats": [], "users": []}',
			'bad chats': b'{"messages": [{"id": 10}], "chats": {}, "users": []}',
			'not utf-8': b'\xff\xfe{}',
		}
		for name, content in cases.items():
			with self.subTest(case=name):
				os.makedirs(os.path.dirname(self.messages_path()), exist_ok=True)
				with open(self.messages_path(), 'wb') as fh:
					fh.write(content)
				summary = await self.collect(['alpha_fx', 'beta_fx'], update=True)
				self.assertEqual(read_bytes(self.messages_path()), content)
				self.assertEqual(summary.failed[0][0], 'alpha_fx')
				self.assertIn('alpha_fx_messages.json', summary.failed[0][1])
				self.assertEqual(summary.succeeded, ['beta_fx'])
				self.assertNotIn('alpha_fx', [d.get('target') for _, d in self.client.calls])
				self.assertEqual(self.status()['targets'][0]['status'], 'failed')

	async def test_no_saved_history_falls_back_to_a_full_collection(self):
		await self.collect(['beta_fx'])
		summary = await self.collect(['alpha_fx', 'beta_fx'], update=True)
		self.assertEqual(summary.collected_in_full, ['alpha_fx'])
		self.assertEqual(
			[m['id'] for m in read_json(self.messages_path())['messages']],
			[10, 9, 8, 7, 6, 5, 4]
		)
		alpha, beta = self.status()['targets']
		self.assertEqual((alpha['history'], alpha['min_id']), ('created', 0))
		self.assertEqual((beta['history'], beta['min_id']), ('unchanged', 5))

	def test_merge_rules(self):
		earlier = {
			'_': 'messages.ChannelMessages', 'count': 3,
			'messages': [{'id': 9, 'v': 'old'}, {'id': 5, 'v': 'old'}],
			'chats': [{'id': 1, 'v': 'old'}, {'id': 2, 'v': 'old'}],
			'users': [{'id': 7, 'v': 'old'}]
		}
		fetched = {
			'_': 'messages.ChannelMessages', 'count': 5,
			'messages': [{'id': 12, 'v': 'new'}, {'id': 9, 'v': 'new'}],
			'chats': [{'id': 2, 'v': 'new'}, {'id': 3, 'v': 'new'}],
			'users': []
		}
		merged = storage.merge_histories(earlier, fetched)
		self.assertEqual([(m['id'], m['v']) for m in merged['messages']], [(12, 'new'), (9, 'new'), (5, 'old')])
		self.assertEqual([(c['id'], c['v']) for c in merged['chats']], [(2, 'new'), (3, 'new'), (1, 'old')])
		self.assertEqual(merged['users'], [{'id': 7, 'v': 'old'}])
		self.assertEqual(merged['count'], 5)
		self.assertIsNone(storage.merge_histories(earlier, dict(fetched, messages=[])))


class UpdateCliTests(offline_guard.OfflineTestCase):
	'''
	--update is collect-only and excludes every window flag and
	metadata-only, before credentials.
	'''

	def run_cli(self, argv):
		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = cli.main(argv)
			except SystemExit as exc:
				code = exc.code
		return code, stderr.getvalue()

	def test_exclusivity(self):
		base = ['collect', '--telegram-channel', 'alpha_fx', '--update']
		for extra, text in (
				(['--since', '2026-01-01'], '--update cannot be combined'),
				(['--until', '2026-01-01'], '--update cannot be combined'),
				(['--min-id', '3'], '--update cannot be combined'),
				(['--max-id', '9'], '--update cannot be combined'),
				(['--max-messages', '2'], '--update cannot be combined'),
				(['--limit-download-to-channel-metadata'], '--update needs message collection')):
			with self.subTest(extra=extra):
				code, err = self.run_cli(base + extra)
				self.assertEqual(code, 2)
				self.assertIn(text, err)

	def test_legacy_form_has_no_update(self):
		code, err = self.run_cli(['--telegram-channel', 'alpha_fx', '--update'])
		self.assertEqual(code, 2)
		self.assertIn('unrecognized arguments: --update', err)

	def test_builders_reject_update(self):
		for command in ('build-dataset', 'build-network'):
			code, _ = self.run_cli([command, '--update'])
			self.assertEqual(code, 2)


class CliStorageErrorTests(offline_guard.OfflineTestCase):

	def test_locked_output_exits_1_before_credentials(self):
		with tempfile.TemporaryDirectory() as tmp:
			out = os.path.join(tmp, 'data')
			os.makedirs(out)
			open(storage.lock_path(out), 'w').close()
			stderr = io.StringIO()
			with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
				code = cli.main(
					['collect', '--telegram-channel', 'alpha_fx', '-o', out],
					client_factory=lambda: self.fail('client created'),
					authorize=lambda c: None
				)
			self.assertEqual(code, 1)
			self.assertIn('in use by another collection', stderr.getvalue())


if __name__ == '__main__':
	unittest.main()
