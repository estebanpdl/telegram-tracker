# -*- coding: utf-8 -*-
'''
--storage both beyond write faults: stops that leave both sinks consistent
and do not block, --update agreement between the sinks (checked on message
IDs before the first history request, also when a basic group and a
channel share a numeric ID), and the database lease keeping a second
writer or an upgrade out while a run holds it.
'''

# import modules
import asyncio
import os
import sqlite3
import tempfile
import unittest

from unittest import mock

import offline_guard
import storage_modes_support

from storage_modes_support import read_json

from fakes import FakeTelegramClient, FakeTL
from sqlite_support import ALPHA, BETA, add_messages

from telethon import errors

from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.collector import run_collection
from telegram_tracker.storage import dual
from telegram_tracker.storage import files as storage_files
from telegram_tracker.storage.destination import select_destination
from telegram_tracker.storage.sqlite import migrations
from telegram_tracker.storage.sqlite.lease import DatabaseLocked


class StopTests(offline_guard.OfflineAsyncTestCase):
	'''
	Stops that leave both sinks consistent do not block the folder.
	'''

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(tmp.name, 'data')

	async def collect(self, targets, client):
		destination = select_destination(self.out, 'both')
		destination.check()
		return await run_collection(
			targets, self.out,
			client_factory=lambda: client,
			authorize=lambda c: ensure_authorized(c, None),
			request_interval=0.1,
			writer_factory=destination.open_writer,
			preflight=destination.check_unlocked
		)

	def rows(self, sql):
		con = sqlite3.connect(os.path.join(self.out, 'telegram-tracker.sqlite3'))
		try:
			return con.execute(sql).fetchall()
		finally:
			con.close()

	async def test_cancellation_does_not_block(self):
		client = FakeTelegramClient(page_size=2)
		started = asyncio.Event()

		async def block():
			started.set()
			await asyncio.Event().wait()

		client.script('GetHistoryRequest', None, None, None, None, None, block)
		task = asyncio.create_task(self.collect(['alpha_fx', 'beta_fx', 'empty_fx'], client))
		await started.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertEqual(self.rows('SELECT state, stop_reason, aggregate_files FROM collection_runs'),
			[('interrupted', 'cancelled', 'written')])
		self.assertEqual(self.rows('SELECT count(*) FROM destination_pending'), [(0,)])
		summary = await self.collect(['empty_fx'], FakeTelegramClient(page_size=2))
		self.assertEqual(summary.succeeded, ['empty_fx'])

	async def test_a_flood_stop_does_not_block(self):
		client = FakeTelegramClient(page_size=2)
		client.script('GetHistoryRequest', None, None, None, None, None,
			errors.FloodWaitError(request=None, capture=999))
		summary = await self.collect(['alpha_fx', 'beta_fx'], client)
		self.assertIn('Telegram asked to wait 999 s', summary.stop_reason)
		self.assertEqual(self.rows('SELECT state, aggregate_files FROM collection_runs'),
			[('interrupted', 'written')])
		self.assertEqual(self.rows('SELECT count(*) FROM destination_pending'), [(0,)])
		summary = await self.collect(['beta_fx'], FakeTelegramClient(page_size=2))
		self.assertEqual(summary.succeeded, ['beta_fx'])


	async def test_a_publish_failure_after_a_stop_records_both_reasons(self):
		client = FakeTelegramClient(page_size=2)
		client.script('GetHistoryRequest', None, None, None, None, None,
			errors.FloodWaitError(request=None, capture=999))
		real = os.replace

		def replace(src, dst):
			if dst.endswith('counter.csv'):
				raise OSError(28, 'No space left on device')
			return real(src, dst)

		with mock.patch.object(storage_files.os, 'replace', replace):
			summary = await self.collect(['alpha_fx', 'beta_fx'], client)
		(state, reason, aggregate_files), = self.rows(
			'SELECT state, stop_reason, aggregate_files FROM collection_runs')
		self.assertEqual((state, aggregate_files), ('interrupted', 'pending'))
		self.assertTrue(reason.startswith(summary.stop_reason + '; storage error: cannot replace'), reason)
		self.assertIn('Telegram asked to wait 999 s', reason)


class UpdateAgreementTests(storage_modes_support.BothCase):

	def setUp(self):
		super().setUp()
		self.assertEqual(self.both(targets=['alpha_fx', 'beta_fx'])[0], 0)

	def history_file(self, target='alpha_fx'):
		return os.path.join(self.out, target, f'{target}_messages.json')

	def history_peers(self):
		return {r['peer'] for r in self.last_client.requests if r['type'] == 'GetHistoryRequest'}

	def sql(self, statement):
		con = sqlite3.connect(self.db())
		try:
			con.execute(statement)
			con.commit()
		finally:
			con.close()

	def test_zero_new_leaves_both_histories_untouched(self):
		before = (storage_modes_support.file_contents(self.out), self.rows('SELECT * FROM messages'))
		code, _, err = self.collect('--update', targets=['alpha_fx', 'beta_fx'])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.pending(), [])
		self.assertEqual(
			self.rows('SELECT history_mode, files_state FROM run_targets WHERE run_id = 2 ORDER BY input_index'),
			[('unchanged', 'written'), ('unchanged', 'written')])
		after = storage_modes_support.file_contents(self.out)
		for name in ('alpha_fx/alpha_fx_messages.json', 'beta_fx/beta_fx_messages.json'):
			self.assertEqual(after[name], before[0][name])
		self.assertEqual(self.rows('SELECT * FROM messages'), before[1])
		self.assertEqual(self.history_peers(), {ALPHA, BETA})

	def test_new_posts_reach_both_sinks(self):
		client = self.make_client()
		add_messages(client.world, ALPHA, 9001, 9002)
		code, _, err = self.collect('--update', targets=['alpha_fx', 'beta_fx'], client=client)
		self.assertEqual((code, err), (0, ''))
		ids = [m['id'] for m in read_json(self.history_file())['messages']]
		self.assertEqual(ids[:2], [9002, 9001])
		self.assertEqual(
			[r[0] for r in self.rows(
				'SELECT message_id FROM messages WHERE channel_id = ? ORDER BY message_id DESC', ALPHA)],
			ids)
		self.assertEqual(self.rows('SELECT history_mode FROM run_targets WHERE run_id = 2 AND input_index = 0'),
			[('merged',)])

	def disagree(self):
		before = self.rows('SELECT count(*) FROM messages')
		code, _, err = self.collect('--update', targets=['alpha_fx', 'beta_fx'])
		self.assertEqual(code, 1)
		self.assertIn('> alpha_fx: ', err)
		self.assertEqual([s[:2] for s in self.targets()], [('alpha_fx', 'failed'), ('beta_fx', 'succeeded')])
		# checked before alpha_fx's first history request; beta_fx still updates
		self.assertEqual(self.history_peers(), {BETA})
		self.assertEqual(self.rows('SELECT count(*) FROM messages'), before)
		self.assertEqual(self.pending(), [])
		return err

	def test_a_message_set_mismatch_fails_only_that_channel(self):
		# an older message only: the newest ID and the files' provenance stay valid
		self.sql(
			f'DELETE FROM messages WHERE channel_id = {ALPHA} AND message_id = '
			f'(SELECT min(message_id) FROM messages WHERE channel_id = {ALPHA})')
		err = self.disagree()
		self.assertIn('hold different histories', err)
		self.assertIn('1 message ID(s) in only one', err)

	def test_a_history_in_one_sink_only_fails_that_channel(self):
		os.remove(self.history_file())
		self.assertIn('exists only in the database', self.disagree())

	def test_a_newer_id_in_one_sink_is_never_taken_as_a_repair(self):
		self.sql(
			f'DELETE FROM messages WHERE channel_id = {ALPHA} AND message_id = '
			f'(SELECT max(message_id) FROM messages WHERE channel_id = {ALPHA})')
		self.assertIn('newest message', self.disagree())


class CollisionTests(storage_modes_support.BothCase):
	'''
	A basic group and a channel with the same numeric ID in one history:
	the files unite entities by bare ID, the database by typed key, so
	their entity lists can differ; the --update check compares message
	IDs only and must not report a divergence.
	'''

	def colliding_client(self):
		client = self.make_client()

		async def first_page():
			data = client.world.page(ALPHA, page_size=storage_modes_support.PAGE_SIZE).to_dict()
			data['chats'].append(dict(client.world.chat(204), id=ALPHA))
			return FakeTL(data)

		client.script('GetHistoryRequest', first_page)
		return client

	def file_chats(self):
		history = read_json(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json'))
		return sorted((c['_'], c['id']) for c in history['chats'] if c['id'] == ALPHA)

	def stored_chats(self):
		return self.rows(
			"SELECT peer_kind, peer_id FROM history_entities WHERE channel_id = ? AND list = 'chats' "
			'AND peer_id = ? ORDER BY 1', ALPHA, ALPHA)

	def test_typed_and_bare_entity_unions_do_not_count_as_divergence(self):
		self.assertEqual(self.both(targets=['alpha_fx'], client=self.colliding_client())[0], 0)
		self.assertEqual(self.file_chats(), [('Channel', ALPHA), ('Chat', ALPHA)])
		self.assertEqual(self.stored_chats(), [('channel', ALPHA), ('chat', ALPHA)])

		client = self.make_client()
		add_messages(client.world, ALPHA, 9001)
		code, _, err = self.collect('--update', targets=['alpha_fx'], client=client)
		self.assertEqual((code, err), (0, ''))
		# the unions now differ: the files dropped the group, the database kept it
		self.assertEqual(self.file_chats(), [('Channel', ALPHA)])
		self.assertEqual(self.stored_chats(), [('channel', ALPHA), ('chat', ALPHA)])

		code, _, err = self.collect('--update', targets=['alpha_fx'])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(
			self.rows('SELECT status, history_mode FROM run_targets WHERE run_id = 3'), [('succeeded', 'unchanged')])
		self.assertEqual(self.pending(), [])


class OwnershipRaceTests(storage_modes_support.BothCase):
	'''
	While a run holds the database lease, from its first history lookup to
	its last write, neither another collection's writer nor an upgrade can
	open the database.
	'''

	def test_no_second_writer_or_upgrade_during_a_run(self):
		self.assertEqual(self.both(targets=['alpha_fx'])[0], 0)
		seen = []
		original = dual.DualHistories.after_resolve

		def after_resolve(histories, target, channel_id, saved):
			attempts = (
				lambda: migrations.migrate(self.db()),
				lambda: select_destination(self.out).open_writer(
					output_folder=self.out, profile=None, started_at='now', targets=['x'], parameters={})
			)
			for attempt in attempts:
				try:
					attempt()
					seen.append('opened')
				except DatabaseLocked:
					seen.append('locked')
			return original(histories, target, channel_id, saved)

		with mock.patch.object(dual.DualHistories, 'after_resolve', after_resolve):
			code, _, err = self.collect('--update', targets=['alpha_fx'])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(seen, ['locked', 'locked'])
		self.assertEqual(self.rows('SELECT count(*) FROM collection_runs'), [(2,)])
		self.assertEqual(self.rows('SELECT count(*) FROM migrations'), [(0,)])


if __name__ == '__main__':
	unittest.main()
