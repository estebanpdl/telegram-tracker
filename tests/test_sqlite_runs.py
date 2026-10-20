# -*- coding: utf-8 -*-
'''
The database against the file outputs on the same fixtures: reconstructed
histories and channel snapshots equal the JSON files; counters and source
order equal counter.csv; observations give chats.txt; collected-chat rows
keep the CSV's append semantics; errors match the exception logs; run and
target records mirror collection-status.json. Counts and source order are
compared separately from the appended rows.
'''

# import modules
import ast
import csv
import json
import os

import sqlite_support

from sqlite_support import ALPHA, BATCH, read_json

from telegram_tracker import storage
from telegram_tracker.storage.files import to_json_text
from telegram_tracker.storage.sqlite.histories import load_history
from telegram_tracker.storage.sqlite.schema import DB_FILENAME
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


def read_csv(path):
	with open(path, encoding='utf-8', newline='') as fh:
		return list(csv.DictReader(fh))


def read_lines(path):
	if not os.path.exists(path):
		return []
	with open(path, encoding='utf-8') as fh:
		return [line.rstrip('\n') for line in fh]


class TwoSinkCase(sqlite_support.SQLiteCase):
	'''
	Every scenario runs twice with identical fake worlds: into the files
	(self.files) and into the database (self.out).
	'''
	def setUp(self):
		super().setUp()
		self.files = os.path.join(self.tmp, 'files')

	async def both(self, targets=None, world_change=None, **kwargs):
		summaries = []
		for sqlite in (False, True):
			client = self.client()
			if world_change is not None:
				world_change(client.world)
			summaries.append(await self.collect(
				targets, client, out=self.out if sqlite else self.files, sqlite=sqlite, **kwargs
			))
		return summaries

	def file(self, *parts):
		return os.path.join(self.files, *parts)

	def status_pair(self):
		return read_json(self.file(storage.records.STATUS_FILE)), read_json(
			os.path.join(self.out, storage.records.STATUS_FILE))


class SnapshotTests(TwoSinkCase):

	async def test_histories_and_snapshots_equal_the_files(self):
		await self.both()
		con = self.open()
		targets = read_json(self.file(storage.records.STATUS_FILE))['targets']
		stored = 0
		for entry in targets:
			if entry['status'] != 'succeeded':
				continue
			target, channel_id = entry['input'], entry['channel_id']
			with self.subTest(target=target):
				history = read_json(self.file(target, f'{target}_messages.json'))
				self.assertEqual(
					sqlite_support.normalized(load_history(con, channel_id)),
					sqlite_support.normalized(history)
				)
				snapshot = con.execute(
					'SELECT raw FROM channel_snapshots WHERE channel_id = ?', (channel_id,)).fetchone()[0]
				self.assertEqual(json.loads(snapshot), read_json(self.file(target, f'{target}.json')))
				for message in history['messages']:
					raw = con.execute(
						'SELECT raw FROM messages WHERE channel_id = ? AND message_id = ?',
						(channel_id, message['id'])).fetchone()[0]
					self.assertEqual(raw, to_json_text(message))
				stored += 1
		self.assertEqual(stored, 3)
		self.assertEqual(os.listdir(self.out), ['collection-status.json', 'telegram-tracker.sqlite3'])
		self.assertIntact()

	async def test_run_and_targets_mirror_the_sidecar(self):
		await self.both()
		_, status = self.status_pair()
		run = self.rows(
			'SELECT state, mode, storage, started_at, finished_at IS NOT NULL, json(summary), stop_reason, '
			'profile_name, profile_id, serializer_version, aggregate_files FROM collection_runs')
		self.assertEqual(run, [(
			status['state'], 'full', 'sqlite', status['started_at'], 1,
			json.dumps(status['summary'], separators=(',', ':')), None, None, None, 1, None
		)])
		self.assertEqual(json.loads(self.rows('SELECT parameters FROM collection_runs')[0][0]), status['parameters'])
		targets = self.rows(
			'SELECT input, status, channel_id, history_mode, messages, reason, files_state '
			'FROM run_targets ORDER BY input_index')
		self.assertEqual(targets, [
			(t['input'], t['status'], t.get('channel_id'), t.get('history'), t.get('messages'), t.get('reason'), None)
			for t in status['targets']
		])
		requests = {
			name: dict(zip(('requests', 'retries', 'flood_waits', 'flood_wait_seconds'), values))
			for name, *values in self.rows(
				'SELECT method_class, requests, retries, flood_waits, flood_wait_seconds FROM run_requests')
		}
		self.assertEqual(requests, status['requests'])
		participants = self.rows(
			"SELECT input, participants_count FROM run_targets WHERE status = 'succeeded' ORDER BY input_index")
		self.assertEqual(participants, [
			(t, read_json(self.file(t, f'{t}.json'))['full_chat']['participants_count'])
			for t, _ in participants
		])
		self.assertEqual(participants[:2], [('alpha_fx', 1500), ('beta_fx', 800)])


class AggregateTests(TwoSinkCase):

	def counters(self, run_id):
		return [
			(str(i), u or '', str(c), str(fm), str(cr), json.loads(t), json.loads(s))
			for i, u, c, fm, cr, t, s in self.rows(
				'SELECT channel_id, username, counter, from_messages, channel_request, targeted_by, sources '
				'FROM run_counters WHERE run_id = ? ORDER BY row_order', run_id)
		]

	def csv_counters(self):
		return [
			(r['id'], r['username'], r['counter'], r['from_messages'], r['channel_request'],
				ast.literal_eval(r['channel_req_targeted_by'])['channels'], ast.literal_eval(r['source']))
			for r in read_csv(self.file('counter.csv'))
		]

	async def test_counts_and_source_order_equal_counter_csv(self):
		await self.both()
		self.assertEqual(self.counters(1), self.csv_counters())
		self.assertGreater(len(self.counters(1)), 5)

	async def test_observations_give_chats_txt(self):
		await self.both()
		self.assertEqual(
			[str(r[0]) for r in self.rows('SELECT channel_id FROM chat_ids_all')],
			read_lines(self.file('chats.txt'))
		)
		kinds = {r[0] for r in self.rows('SELECT DISTINCT kind FROM run_observations')}
		self.assertEqual(kinds, {'channel_request', 'from_messages', 'linked'})
		pages = self.rows(
			"SELECT DISTINCT page FROM run_observations WHERE kind = 'from_messages' ORDER BY page")
		self.assertEqual(pages[0], (2,))
		self.assertEqual(self.rows(
			"SELECT count(*) FROM run_observations WHERE (kind = 'linked') != (parent_channel_id IS NOT NULL)"
		), [(0,)])

	async def test_reruns_append_like_the_files(self):
		for _ in range(2):
			await self.both()
		self.assertEqual(
			[str(r[0]) for r in self.rows('SELECT channel_id FROM chat_ids_all')],
			read_lines(self.file('chats.txt'))
		)
		rows = read_csv(self.file('collected_chats.csv'))
		appended = self.rows(
			'SELECT channel_id, metadata, counter, sources FROM collected_chats_all')
		self.assertEqual([str(r[0]) for r in appended], [r['id'] for r in rows])
		for (_, metadata, counter, sources), row in zip(appended, rows):
			metadata = json.loads(metadata)
			self.assertEqual(
				(metadata['username'], metadata['title'], str(metadata['participants_count'])),
				(row['username'], row['title'], row['participants_count'])
			)
			self.assertEqual((str(counter), json.loads(sources)), (row['counter'], ast.literal_eval(row['source'])))
		self.assertEqual(self.counters(2), self.csv_counters())
		self.assertEqual(self.rows('SELECT count(DISTINCT run_id) FROM collected_chat_rows'), [(2,)])

	async def test_errors_match_the_exception_logs(self):
		def lose_a_linked_chat(world):
			world.full_channels[201] = {'error': 'ChannelPrivateError'}

		await self.both(world_change=lose_a_linked_chat)
		errors = self.rows(
			'SELECT category, input_index, peer_kind, peer_id FROM collection_errors ORDER BY error_index')
		targets = [r for r in errors if r[0] == 'target_not_found']
		self.assertEqual(
			[BATCH[r[1]] for r in targets],
			read_lines(self.file(storage.records.CHANNELS_NOT_FOUND))
		)
		self.assertEqual(
			[f'ID - {r[3]}' for r in errors if r[0] == 'enrichment_not_found'],
			read_lines(self.file(storage.records.USERS_NOT_FOUND))
		)
		self.assertTrue(all(r[2] == 'channel' for r in errors if r[0] != 'target_not_found'))
		self.assertIn(('enrichment_not_found', 201), [(r[0], r[3]) for r in errors])

	async def test_completion_order_does_not_change_the_rows(self):
		orders = []
		for out, latency, workers in ((self.out, 0, 1), (os.path.join(self.tmp, 'slow'), slow_alpha, 3)):
			commits = []
			orders.append(commits)

			def factory(**kwargs):
				writer = SQLiteWriter.open(os.path.join(kwargs['output_folder'], DB_FILENAME), **kwargs)
				commit = writer.commit
				writer.commit = lambda result: (commits.append(result.target), commit(result))[1]
				return writer

			await self.collect(out=out, client=self.client(latency=latency), concurrency=workers, writer_factory=factory)
		self.assertEqual(orders[0], ['alpha_fx', 'beta_fx', 'empty_fx'])
		self.assertEqual(sorted(orders[1]), orders[0])
		self.assertNotEqual(orders[1][0], 'alpha_fx')

		slow_db = os.path.join(self.tmp, 'slow', DB_FILENAME)
		for table, order in (('run_observations', 'seq'), ('run_counters', 'row_order'),
				('collected_chat_rows', 'row_index'), ('collection_errors', 'error_index')):
			sql = f'SELECT * FROM {table} ORDER BY {order}'
			self.assertEqual(
				[r[1:] for r in self.rows(sql)], [r[1:] for r in self.rows(sql, path=slow_db)], table)


def slow_alpha(kind, key):
	return 5 if 'alpha' in str(key) or key == ALPHA else 0.1
