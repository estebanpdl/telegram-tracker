# -*- coding: utf-8 -*-
'''
Sequential collector against the fake client. Expected requests, files,
counters, and JSON fields come from fixtures/expected/collection.json,
which was written from static inspection of the baseline code.
'''

# import modules
import asyncio
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

import offline_guard

from api.auth import AuthenticationError, ensure_authorized
from fakes import FakeTelegramClient, FakeTL, load_expected

import collector


EXPECTED = load_expected('collection.json')
SCENARIOS = EXPECTED['scenarios']


def request_log(client):
	'''
	Compact request sequence: get_entity targets, full-channel ids, and
	history (peer, offset_id).
	'''
	log = []
	for name, details in client.calls:
		if name == 'get_entity':
			log.append(['get_entity', details['target']])
		elif name == 'GetFullChannelRequest':
			log.append([name, details['channel']])
		elif name == 'GetHistoryRequest':
			log.append([name, details['peer'], details['offset_id']])
	return log


def cached_requests(baseline):
	'''
	The baseline request sequence as sent with the per-run metadata cache
	(fix F7): a repeated GetFullChannelRequest for the same chat is served
	from the cache.
	'''
	seen, out = set(), []
	for entry in baseline:
		if entry[0] == 'GetFullChannelRequest':
			if entry[1] in seen:
				continue
			seen.add(entry[1])
		out.append(entry)
	return out


def read_csv_rows(path):
	with open(path, encoding='utf-8', newline='') as fh:
		reader = csv.DictReader(fh)
		return reader.fieldnames, list(reader)


def list_files(root):
	return sorted(
		os.path.relpath(os.path.join(d, f), root).replace('\\', '/')
		for d, _, files in os.walk(root) for f in files
	)


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def lookup(data, path):
	'''
	Resolve 'messages[0].media.document' style paths.
	'''
	for part in path.replace(']', '').split('.'):
		name, _, index = part.partition('[')
		data = data[name]
		if index:
			data = data[int(index)]
	return data


class CollectorTestCase(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(tmp.name, 'data')
		self.pauses = []
		self.stdout = io.StringIO()
		self.stderr = io.StringIO()

	async def pause(self, seconds):
		self.pauses.append(seconds)

	async def run_targets(self, targets, client=None, **kwargs):
		self.client = client or FakeTelegramClient(page_size=EXPECTED['page_size'])
		authorize = kwargs.pop('authorize', None) or (
			lambda c: ensure_authorized(c, None)
		)
		with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
			return await collector.run_collection(
				targets,
				self.out,
				client_factory=lambda: self.client,
				authorize=authorize,
				pause=self.pause,
				**kwargs
			)

	def assert_single_lifecycle(self):
		self.assertEqual(self.client.connect_count, 1)
		self.assertEqual(self.client.disconnect_count, 1)
		self.assertEqual(self.client.call_names()[-1], 'disconnect')

	def assert_counter(self, expected_rows):
		header, rows = read_csv_rows(os.path.join(self.out, 'counter.csv'))
		self.assertEqual(header, EXPECTED['counter_columns'])
		self.assertEqual(rows, [
			{k: str(v) for k, v in row.items()} for row in expected_rows
		])

	def assert_chats_txt(self, expected_ids):
		with open(os.path.join(self.out, 'chats.txt'), encoding='utf-8') as fh:
			self.assertEqual(fh.read().splitlines(), [str(i) for i in expected_ids])


class BatchScenarioTests(CollectorTestCase):

	async def asyncSetUp(self):
		self.spec = SCENARIOS['batch']
		self.summary = await self.run_targets(self.spec['targets'])

	def test_request_sequence(self):
		self.assertEqual(request_log(self.client), cached_requests(self.spec['requests']))

	def test_every_history_request_uses_baseline_arguments(self):
		history = [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']
		self.assertTrue(history)
		for request in history:
			self.assertEqual(
				(request['limit'], request['max_id'], request['min_id'],
					request['add_offset'], request['offset_date'], request['hash']),
				(100, 0, 0, 0, 0, 0)
			)
		pages = [(r['peer'], r['offset_id']) for r in history]
		self.assertEqual(len(pages), len(set(pages)), 'a page was requested twice')

	def test_lifecycle_and_summary(self):
		self.assert_single_lifecycle()
		self.assertEqual(self.summary.succeeded, self.spec['succeeded'])
		self.assertEqual([t for t, _ in self.summary.failed], self.spec['failed'])
		self.assertEqual(self.pauses, self.spec['pauses'])

	def test_files(self):
		self.assertEqual(list_files(self.out), self.spec['files'])

	def test_channel_json(self):
		for username, spec in self.spec['channel_json'].items():
			data = read_json(os.path.join(self.out, username, f'{username}.json'))
			self.assertEqual(data['full_chat']['id'], spec['full_chat_id'])
			self.assertEqual([c['id'] for c in data['chats']], spec['chat_ids'])

	def test_messages_json(self):
		for username, spec in self.spec['messages_json'].items():
			data = read_json(os.path.join(self.out, username, f'{username}_messages.json'))
			self.assertEqual(list(data), self.spec['messages_json_keys'])
			self.assertEqual(data['count'], spec['count'])
			self.assertEqual([m['id'] for m in data['messages']], spec['message_ids'])
			self.assertEqual([c['id'] for c in data['chats']], spec['chat_ids'])
			self.assertEqual([u['id'] for u in data['users']], spec['user_ids'])

	def test_json_values_are_encoded_like_baseline(self):
		for key, expected in self.spec['json_value_checks'].items():
			path, field = key.split(':')
			self.assertEqual(lookup(read_json(os.path.join(self.out, path)), field), expected)
		with open(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json'), encoding='utf-8') as fh:
			raw = fh.read()
		self.assertIn('مرحبا بالعالم', raw)
		self.assertNotIn(', "', raw)

	def test_chats_txt(self):
		self.assert_chats_txt(self.spec['chats_txt'])

	def test_counter_csv(self):
		self.assert_counter(self.spec['counter'])

	def test_collected_chats_csv(self):
		header, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual(header, EXPECTED['chat_columns'] + EXPECTED['counter_columns'][2:])
		selected = [{k: row[k] for k in self.spec['collected_chats'][0]} for row in rows]
		self.assertEqual(selected, [
			{k: str(v) for k, v in row.items()} for row in self.spec['collected_chats']
		])

	def test_exception_logs(self):
		for name, lines in self.spec['exceptions'].items():
			with open(os.path.join(self.out, name), encoding='utf-8') as fh:
				self.assertEqual(fh.read().splitlines(), lines)

	def test_counter_key_fix_f1(self):
		_, rows = read_csv_rows(os.path.join(self.out, 'counter.csv'))
		row = next(r for r in rows if r['id'] == '106')
		self.assertEqual(row['channel_req_targeted_by'], "{'channels': ['alpha_fx', 'empty_fx']}")
		_, chats = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertIn('103', [r['id'] for r in chats])

	def test_first_page_chats_are_not_counted(self):
		# shared_fx appears on alpha's first page (msg 9) and third page (msg 6);
		# only the third-page observation and beta's second page count
		_, rows = read_csv_rows(os.path.join(self.out, 'counter.csv'))
		row = next(r for r in rows if r['username'] == 'shared_fx')
		self.assertEqual((row['counter'], row['from_messages']), ('2', '2'))


class OtherScenarioTests(CollectorTestCase):

	async def test_metadata_only_makes_no_history_requests(self):
		spec = SCENARIOS['metadata_only']
		await self.run_targets(spec['targets'], metadata_only=True)
		self.assertEqual(request_log(self.client), cached_requests(spec['requests']))
		self.assertEqual(list_files(self.out), spec['files'])
		self.assert_chats_txt(spec['chats_txt'])
		self.assert_counter(spec['counter'])
		self.assertEqual(self.pauses, spec['pauses'])
		self.assert_single_lifecycle()

	async def test_min_id_filters_every_history_request(self):
		spec = SCENARIOS['min_id']
		await self.run_targets(spec['targets'], min_id=spec['min_id'])
		self.assertEqual(request_log(self.client), cached_requests(spec['requests']))
		for request in self.client.requests:
			if request['type'] == 'GetHistoryRequest':
				self.assertEqual(request['min_id'], spec['min_id'])
		data = read_json(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json'))
		expected = spec['messages_json']['alpha_fx']
		self.assertEqual([m['id'] for m in data['messages']], expected['message_ids'])
		self.assertEqual([c['id'] for c in data['chats']], expected['chat_ids'])
		self.assert_counter(spec['counter'])
		self.assertEqual(self.pauses, [])

	async def test_empty_batch_writes_header_correct_outputs(self):
		summary = await self.run_targets([])
		self.assertEqual(request_log(self.client), [])
		self.assertEqual(list_files(self.out), SCENARIOS['empty_batch']['files'])
		self.assert_chats_txt([])
		self.assert_counter([])
		header, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual(header, EXPECTED['empty_collected_chats_columns'])
		self.assertEqual(rows, [])
		self.assertEqual((summary.succeeded, summary.failed), ([], []))
		self.assert_single_lifecycle()

	async def test_all_failed_batch(self):
		spec = SCENARIOS['all_failed']
		summary = await self.run_targets(spec['targets'])
		self.assertEqual(request_log(self.client), cached_requests(spec['requests']))
		self.assertEqual(list_files(self.out), spec['files'])
		self.assertEqual([t for t, _ in summary.failed], spec['targets'])
		header, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual((header, rows), (EXPECTED['empty_collected_chats_columns'], []))
		self.assert_single_lifecycle()

	async def test_empty_run_then_data_run_keeps_a_consistent_header(self):
		await self.run_targets([])
		await self.run_targets(['alpha_fx'], metadata_only=True)
		header, rows = read_csv_rows(os.path.join(self.out, 'collected_chats.csv'))
		self.assertEqual(header, EXPECTED['chat_columns'] + EXPECTED['counter_columns'][2:])
		self.assertEqual([r['id'] for r in rows], ['101', '106'])


class PaginationTests(CollectorTestCase):

	async def test_stalled_offset_fails_only_that_channel(self):
		client = FakeTelegramClient(page_size=2)

		async def replay_first_page():
			return client.world.page(101, page_size=2)

		client.script('GetHistoryRequest', None, replay_first_page)
		summary = await self.run_targets(['alpha_fx', 'beta_fx'], client=client)
		self.assertEqual(summary.succeeded, ['beta_fx'])
		self.assertEqual(summary.failed[0][0], 'alpha_fx')
		self.assertIn('did not advance', summary.failed[0][1])
		self.assertIn('did not advance', self.stderr.getvalue())
		self.assertFalse(os.path.exists(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')))
		self.assertTrue(os.path.exists(os.path.join(self.out, 'beta_fx', 'beta_fx_messages.json')))
		self.assert_single_lifecycle()

	async def test_repeated_identities_are_deduplicated(self):
		client = FakeTelegramClient(page_size=2)
		world = client.world

		async def overlapping_page():
			page = world.page(101, offset_id=10, page_size=2).to_dict()
			page['chats'] = page['chats'] + page['chats']
			return FakeTL(page)

		client.script('GetHistoryRequest', None, overlapping_page)
		await self.run_targets(['alpha_fx'], client=client)
		data = read_json(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json'))
		ids = [m['id'] for m in data['messages']]
		self.assertEqual(ids, [10, 9, 8, 7, 6, 5, 4])
		self.assertEqual(len({c['id'] for c in data['chats']}), len(data['chats']))

	async def test_empty_final_page_ends_history(self):
		await self.run_targets(['beta_fx'])
		history = [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']
		self.assertEqual([r['offset_id'] for r in history], [0, 4, 2, 1])


class LifecycleTests(CollectorTestCase):

	async def test_client_is_created_and_used_on_one_running_loop(self):
		loops = []
		client = FakeTelegramClient()

		def factory():
			loops.append(id(asyncio.get_running_loop()))
			return client

		self.client = client
		with contextlib.redirect_stdout(self.stdout):
			await collector.run_collection(
				['alpha_fx'], self.out,
				client_factory=factory,
				authorize=lambda c: ensure_authorized(c, None),
				pause=self.pause
			)
		self.assertEqual(set(client.loop_ids), set(loops))
		self.assert_single_lifecycle()

	async def test_unexpected_error_propagates_and_disconnects_once(self):
		client = FakeTelegramClient()
		client.script('GetHistoryRequest', None, RuntimeError('boom'))
		with self.assertRaisesRegex(RuntimeError, 'boom'):
			await self.run_targets(['alpha_fx', 'beta_fx'], client=client)
		self.assert_single_lifecycle()
		self.assertNotIn('beta_fx', [d.get('target') for _, d in client.calls])

	async def test_cancellation_disconnects_once(self):
		client = FakeTelegramClient()
		gate = asyncio.Event()
		started = asyncio.Event()

		async def block():
			started.set()
			await gate.wait()

		client.script('GetHistoryRequest', None, block)
		task = asyncio.create_task(self.run_targets(['alpha_fx'], client=client))
		await started.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assert_single_lifecycle()

	async def test_failed_login_creates_no_output_and_disconnects_once(self):
		async def refuse(client):
			raise AuthenticationError('login refused')

		with self.assertRaises(AuthenticationError):
			await self.run_targets(['alpha_fx'], authorize=refuse)
		self.assertFalse(os.path.exists(self.out))
		self.assertEqual(self.client.call_names(), ['connect', 'disconnect'])

	async def test_connect_failure_still_disconnects_once(self):
		client = FakeTelegramClient()
		client.script('connect', ConnectionError('offline'))
		with self.assertRaises(ConnectionError):
			await self.run_targets(['alpha_fx'], client=client)
		self.assertEqual(client.disconnect_count, 1)


class ObservationTests(offline_guard.OfflineTestCase):
	'''
	Pure counting helpers.
	'''

	def test_observe_chat(self):
		counter = {}
		self.assertTrue(collector.observe_chat(counter, 1, 'a_fx', 'src_fx', 'channel_request'))
		self.assertFalse(collector.observe_chat(counter, 1, 'a_fx', 'other_fx', 'from_messages'))
		self.assertFalse(collector.observe_chat(counter, 1, 'a_fx', 'other_fx', 'from_messages'))
		self.assertEqual(counter[1], {
			'username': 'a_fx', 'counter': 3, 'from_messages': 2, 'channel_request': 1,
			'channel_req_targeted_by': {'channels': ['self']},
			'source': ['src_fx', 'other_fx']
		})

	def test_observe_linked_chat(self):
		counter = {}
		collector.observe_linked_chat(counter, 2, 'b_fx', 'src_fx', 'parent_fx')
		collector.observe_linked_chat(counter, 2, 'b_fx', 'other_fx', 'second_fx')
		self.assertEqual(counter[2], {
			'username': 'b_fx', 'counter': 2, 'from_messages': 0, 'channel_request': 2,
			'channel_req_targeted_by': {'channels': ['parent_fx', 'second_fx']},
			'source': ['src_fx']
		})


if __name__ == '__main__':
	unittest.main()
