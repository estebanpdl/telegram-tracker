# -*- coding: utf-8 -*-
'''
History semantics in the database: full and window runs replace a
channel's current history (an empty selection included); metadata-only
runs keep it; --update upserts newer messages, keeps older ones, unites
entities with the fetched ones first, leaves the tables untouched when
nothing is new, falls back to a full collection without a stored history,
and fails only that target on a read error or another profile's history.
Takeovers are recorded. Typed keys keep equal IDs apart across channels
and peer kinds; a min object never replaces a full one.
'''

# import modules
import json
import os
import sqlite3

import sqlite_support

from sqlite_support import ALPHA, BETA, EMPTY, OTHER_PROFILE_ID, PROFILE_ID, add_messages, read_json

from telegram_tracker import storage
from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.storage.provenance import RunProfile
from telegram_tracker.storage.sqlite.histories import load_history
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


ALICE = RunProfile('alice', PROFILE_ID)
BOB = RunProfile('bob', OTHER_PROFILE_ID)


class HistoryCase(sqlite_support.SQLiteCase):

	def history_row(self, channel_id=ALPHA):
		return self.rows(
			'SELECT run_id, profile_name, profile_id, mode, window_bounds, message_count '
			'FROM histories WHERE channel_id = ?', channel_id)

	def current_state(self, path=None):
		'''
		The history tables (peers change with every channel snapshot).
		'''
		return {t: self.rows(f'SELECT * FROM {t} ORDER BY 1, 2', path=path) for t in (
			'histories', 'messages', 'history_entities')}

	def target(self, run_id, index=0):
		return self.rows(
			'SELECT status, history_mode, min_id, messages, reason, takeover, previous_profile_name, '
			'previous_profile_id FROM run_targets WHERE run_id = ? AND input_index = ?', run_id, index)[0]

	def history_requests(self, channel_id=None):
		return [
			r for r in self.last_client.requests if r['type'] == 'GetHistoryRequest'
			and (channel_id is None or r['peer'] == channel_id)
		]


class ReplaceTests(HistoryCase):

	async def test_window_replaces_and_records_its_bounds(self):
		await self.collect(['alpha_fx'])
		self.assertEqual(self.history_ids(ALPHA), [10, 9, 8, 7, 6, 5, 4])
		await self.collect(['alpha_fx'], window=HistoryWindow(max_messages=3))
		self.assertEqual(self.history_ids(ALPHA), [10, 9, 8])
		run_id, _, _, mode, bounds, count = self.history_row()[0]
		self.assertEqual((run_id, mode, count), (2, 'replaced', 3))
		self.assertEqual(json.loads(bounds)['max_messages'], 3)
		self.assertEqual(self.rows('SELECT DISTINCT run_id FROM messages'), [(2,)])
		self.assertIntact()

	async def test_empty_selection_differs_from_absent(self):
		await self.collect(['alpha_fx', 'empty_fx'])
		await self.collect(['alpha_fx'], window=HistoryWindow(min_id=1000))
		self.assertEqual(self.history_ids(ALPHA), [])
		self.assertEqual(self.history_row()[0][3:], ('replaced', json.dumps(HistoryWindow(min_id=1000).as_record(), separators=(',', ':')), 0))
		self.assertEqual(self.history_row(EMPTY)[0][5], 0)
		self.assertEqual(self.history_row(BETA), [])
		self.assertEqual(self.rows('SELECT count(*) FROM history_entities WHERE channel_id = ?', ALPHA), [(0,)])

	async def test_metadata_only_keeps_the_history(self):
		await self.collect(['alpha_fx'])
		before = self.current_state()['messages']
		summary = await self.collect(['alpha_fx'], metadata_only=True)
		self.assertEqual(self.current_state()['messages'], before)
		self.assertEqual(self.history_row()[0][0], 1)
		self.assertEqual(self.rows('SELECT run_id FROM channel_snapshots WHERE channel_id = ?', ALPHA), [(2,)])
		self.assertEqual(self.target(2)[:2], ('succeeded', 'not_collected'))
		self.assertEqual(summary.stale_histories, ['alpha_fx'])
		self.assertEqual(self.history_requests(), [])

	async def test_takeover_by_another_profile(self):
		await self.collect(['alpha_fx'], profile=ALICE)
		self.assertEqual(self.history_row()[0][1:3], ('alice', PROFILE_ID))
		summary = await self.collect(['alpha_fx'], profile=BOB)
		self.assertEqual(self.history_row()[0][1:3], ('bob', OTHER_PROFILE_ID))
		self.assertEqual(self.target(2)[5:], (1, 'alice', PROFILE_ID))
		self.assertEqual(summary.takeovers, [('alpha_fx', 'alice')])
		await self.collect(['alpha_fx'], profile=BOB)
		self.assertEqual(self.target(3)[5:], (0, None, None))
		self.assertEqual(
			self.rows('SELECT profile_name, profile_id FROM collection_runs ORDER BY run_id'),
			[('alice', PROFILE_ID), ('bob', OTHER_PROFILE_ID), ('bob', OTHER_PROFILE_ID)])


class UpdateTests(HistoryCase):

	async def test_merge_matches_the_files(self):
		def new_posts(world):
			add_messages(world, ALPHA, 11, 12, user_id=9001, forward_from=205)

		for sqlite, out in ((False, os.path.join(self.tmp, 'files')), (True, self.out)):
			await self.collect(['alpha_fx'], out=out, sqlite=sqlite)
			client = self.client()
			new_posts(client.world)
			await self.collect(['alpha_fx'], client, out=out, sqlite=sqlite, update=True)

		merged = read_json(os.path.join(self.tmp, 'files', 'alpha_fx', 'alpha_fx_messages.json'))
		self.assertEqual(
			sqlite_support.normalized(load_history(self.open(), ALPHA)), sqlite_support.normalized(merged))
		self.assertEqual(self.history_ids(ALPHA), [12, 11, 10, 9, 8, 7, 6, 5, 4])
		self.assertEqual(
			self.rows('SELECT message_id, run_id FROM messages WHERE message_id > 9 ORDER BY message_id'),
			[(10, 1), (11, 2), (12, 2)])
		run_id, profile, _, mode, bounds, count = self.history_row()[0]
		self.assertEqual((run_id, profile, mode, bounds, count), (2, None, 'merged', None, 9))
		self.assertEqual(self.target(2)[:4], ('succeeded', 'merged', 10, 9))
		self.assertEqual({r['min_id'] for r in self.history_requests()}, {10})
		users = self.rows("SELECT ordinal, peer_id FROM history_entities WHERE list = 'users'")
		self.assertEqual(users, [(0, 9001)])
		chats = [r[0] for r in self.rows(
			"SELECT peer_id FROM history_entities WHERE list = 'chats' ORDER BY ordinal")]
		self.assertEqual(chats[:2], [ALPHA, 205])
		self.assertGreater(len(chats), 2)
		self.assertIntact()

	async def test_nothing_new_leaves_the_tables_untouched(self):
		await self.collect(['alpha_fx'])
		before = self.current_state()
		await self.collect(['alpha_fx'], update=True)
		self.assertEqual(self.current_state(), before)
		self.assertEqual(self.target(2)[:4], ('succeeded', 'unchanged', 10, 7))
		self.assertEqual(len(self.history_requests()), 1)

	async def test_absent_history_is_collected_in_full(self):
		await self.collect(['beta_fx'])
		summary = await self.collect(['alpha_fx', 'beta_fx'], update=True)
		self.assertEqual(summary.collected_in_full, ['alpha_fx'])
		self.assertEqual(self.target(2, 0)[:3], ('succeeded', 'created', 0))
		self.assertEqual(self.target(2, 1)[:2], ('succeeded', 'unchanged'))
		self.assertEqual(self.history_row()[0][3:], ('created', None, 7))

	async def test_read_error_fails_only_that_target(self):
		await self.collect(['alpha_fx', 'beta_fx'])
		before = self.current_state()

		class Unreadable:
			def __init__(self, con):
				self.con = con

			def execute(self, sql, *args):
				if 'FROM histories h' in sql and args[0] == (ALPHA,):
					raise sqlite3.DatabaseError('database disk image is malformed')
				return self.con.execute(sql, *args)

		def factory(**kwargs):
			writer = SQLiteWriter.open(self.db, **kwargs)
			writer.histories.con = Unreadable(writer.con)
			return writer

		client = self.client()
		add_messages(client.world, BETA, 50)
		summary = await self.collect(['alpha_fx', 'beta_fx'], client, update=True, writer_factory=factory)
		self.assertEqual([t for t, _ in summary.failed], ['alpha_fx'])
		self.assertIn('cannot be read', summary.failed[0][1])
		self.assertEqual(summary.succeeded, ['beta_fx'])
		self.assertEqual(self.history_requests(ALPHA), [])
		after = self.current_state()
		self.assertEqual(
			[r for r in after['messages'] if r[0] == ALPHA], [r for r in before['messages'] if r[0] == ALPHA])
		self.assertEqual(self.target(2, 0)[0], 'failed')

	async def test_another_profiles_history_is_refused_before_history_requests(self):
		cases = (
			(ALICE, BOB, False),
			(ALICE, RunProfile('alice', None), False),
			(ALICE, ALICE, True),
			(RunProfile('alice', None), RunProfile('alice', None), True),
			(None, RunProfile('default', None), True),
			(None, ALICE, False),
			(ALICE, None, False),
		)
		for number, (owner, runner, allowed) in enumerate(cases):
			with self.subTest(owner=owner, runner=runner):
				out = os.path.join(self.tmp, f'case{number}')
				db = os.path.join(out, os.path.basename(self.db))
				await self.collect(['alpha_fx'], out=out, profile=owner)
				before = self.current_state(db)
				summary = await self.collect(['alpha_fx', 'beta_fx'], out=out, profile=runner, update=True)
				self.assertEqual(summary.succeeded, ['alpha_fx', 'beta_fx'] if allowed else ['beta_fx'])
				self.assertTrue(self.history_requests(BETA))
				if allowed:
					self.assertTrue(self.history_requests(ALPHA))
				else:
					self.assertIn('--update refused', summary.failed[0][1])
					self.assertEqual(self.history_requests(ALPHA), [])
					self.assertEqual(self.current_state(db)['histories'][:1], before['histories'][:1])


class KeyTests(HistoryCase):

	def result(self, index, channel_id, messages, users=(), chats=(), mode='replaced'):
		return storage.records.ChannelResult(
			index=index, target=f't{index}', channel_id=channel_id,
			channel_data={'_': 'messages.ChatFull', 'full_chat': {'_': 'ChannelFull', 'id': channel_id},
				'chats': [], 'users': []},
			history={'_': 'messages.ChannelMessages', 'count': len(messages), 'messages': list(messages),
				'chats': list(chats), 'users': list(users)},
			history_mode=mode, messages=len(messages)
		)

	def writer(self, targets=2):
		os.makedirs(self.out, exist_ok=True)
		writer = SQLiteWriter.open(self.db, targets=[f't{i}' for i in range(targets)])
		self.addCleanup(writer.close)
		return writer

	def test_equal_ids_stay_apart_across_channels_and_peer_kinds(self):
		writer = self.writer()
		user = {'_': 'User', 'id': 5, 'first_name': 'Synthetic', 'min': False}
		chat = {'_': 'Chat', 'id': 5, 'title': 'Synthetic chat'}
		channel = {'_': 'Channel', 'id': 5, 'title': 'Synthetic channel', 'min': False}
		writer.commit(self.result(0, 1001, [{'_': 'Message', 'id': 1, 'message': 'a'}], [user], [chat, channel]))
		writer.commit(self.result(1, 1002, [{'_': 'Message', 'id': 1, 'message': 'b'}], [user], [channel]))
		self.assertEqual(
			self.rows('SELECT channel_id, message_id, text FROM messages ORDER BY channel_id'),
			[(1001, 1, 'a'), (1002, 1, 'b')])
		self.assertEqual(
			self.rows('SELECT peer_kind, peer_id, constructor FROM peers ORDER BY peer_kind'),
			[('channel', 5, 'Channel'), ('chat', 5, 'Chat'), ('user', 5, 'User')])
		self.assertEqual(
			self.rows('SELECT list, ordinal, peer_kind FROM history_entities WHERE channel_id = 1001 ORDER BY list, ordinal'),
			[('chats', 0, 'chat'), ('chats', 1, 'channel'), ('users', 0, 'user')])

	def test_min_object_never_replaces_a_full_one(self):
		full = {'_': 'User', 'id': 7, 'min': False, 'access_hash': 70, 'first_name': 'Full'}
		smaller = {'_': 'User', 'id': 7, 'min': True, 'access_hash': 71, 'first_name': 'Min'}
		newer = dict(full, first_name='Newer', access_hash=72)
		expected = [
			(0, 'Full', 70, 1, 1, 1),
			(0, 'Full', 70, 1, 1, 2),
			(0, 'Newer', 72, 3, 1, 3),
		]
		for run, user in enumerate((full, smaller, newer)):
			writer = self.writer(1)
			writer.commit(self.result(0, 1001, [], [user]))
			writer.close()
			with self.subTest(run=run + 1):
				self.assertEqual(self.rows(
					'SELECT is_min, first_name, access_hash, run_id, first_seen_run, last_seen_run FROM peers'
				), [expected[run]])

	def test_min_objects_replace_each_other(self):
		first = {'_': 'Channel', 'id': 8, 'min': True, 'access_hash': 80}
		second = dict(first, access_hash=81)
		for user in (first, second):
			writer = self.writer(1)
			writer.commit(self.result(0, 1001, [], chats=[user]))
			writer.close()
		self.assertEqual(self.rows('SELECT is_min, access_hash, run_id FROM peers'), [(1, 81, 2)])

	def test_unknown_constructors_and_fields_round_trip(self):
		variants = sqlite_support.read_json(os.path.join(
			sqlite_support.source_path.ROOT, 'tests', 'fixtures', 'sqlite', 'variants.json'))
		messages = [v['raw'] for v in variants['messages']]
		peers = [v['raw'] for v in variants['peers']]
		users = [p for p in peers if p['_'].startswith('User')]
		chats = [p for p in peers if not p['_'].startswith('User')]
		self.assertEqual(len(users) + len(chats), 11)
		writer = self.writer(1)
		result = self.result(0, 1001, sorted(messages, key=lambda m: m['id'], reverse=True), users, chats)
		expected = sqlite_support.normalized(result.history)
		writer.commit(result)
		self.assertEqual(sqlite_support.normalized(load_history(writer.con, 1001)), expected)
		self.assertEqual(self.rows('SELECT count(*) FROM messages'), [(len(messages),)])
		self.assertIntact()

	def test_newest_id_and_date_lookups_use_indexes(self):
		self.writer(1)
		con = self.connect()
		plans = {
			'newest': 'SELECT max(message_id) FROM messages WHERE channel_id = 1',
			'date': "SELECT message_id FROM messages WHERE channel_id = 1 AND date >= '2025'",
			'forward': "SELECT count(*) FROM messages WHERE fwd_peer_kind = 'channel' AND fwd_peer_id = 2",
		}
		for name, sql in plans.items():
			with self.subTest(query=name):
				plan = ' '.join(r[-1] for r in con.execute(f'EXPLAIN QUERY PLAN {sql}'))
				self.assertTrue(plan.startswith('SEARCH messages USING'), plan)
