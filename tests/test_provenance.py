# -*- coding: utf-8 -*-
'''
Per-history provenance in file outputs: every history a profiled run
writes gets <channel>_provenance.json; --update merges only into the same
profile's history (ID, then name, then default-only for unknown), refused
channels fail before any request while the others go on, a merge keeps
the recorded profile and never creates a record, and a full or window run
replacing another profile's history records a takeover in the record,
the sidecar, and the summary. Synthetic fixtures; no profile ever names
an account.
'''

# import modules
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient

from telegram_tracker import collector, storage
from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.storage.provenance import RunProfile, provenance_path, refusal


ALICE = RunProfile('alice', '11111111-1111-4111-8111-111111111111')
ALICE_ELSEWHERE = RunProfile('alice', '22222222-2222-4222-8222-222222222222')
BOB = RunProfile('bob', '33333333-3333-4333-8333-333333333333')
DEFAULT = RunProfile('default', None)
DEFAULT_WITH_ID = RunProfile('default', '44444444-4444-4444-8444-444444444444')
ALPHA = 101


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def add_post(client, channel_id=ALPHA):
	history = client.world.histories[channel_id]
	post = copy.deepcopy(history[0])
	post['id'] = history[0]['id'] + 1
	history.insert(0, post)


class ProvenanceCase(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(tmp.name, 'data')
		self.stderr = io.StringIO()

	async def collect(self, profile, targets=('alpha_fx',), client=None, **kwargs):
		self.client = client or FakeTelegramClient(page_size=2)
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(self.stderr):
			return await collector.run.run_collection(
				list(targets),
				self.out,
				client_factory=lambda: self.client,
				authorize=lambda c: ensure_authorized(c, None),
				profile=profile,
				**kwargs
			)

	async def update(self, profile, targets=('alpha_fx',)):
		client = FakeTelegramClient(page_size=2)
		add_post(client)
		return await self.collect(profile, targets, client=client, update=True)

	def record(self, target='alpha_fx'):
		return read_json(provenance_path(self.out, target))

	def messages(self, target='alpha_fx'):
		return read_json(storage.files.history_path(self.out, target))

	def status(self):
		return read_json(os.path.join(self.out, storage.records.STATUS_FILE))

	def history_requests(self, channel_id=ALPHA):
		return [
			r for r in self.client.requests
			if r['type'] == 'GetHistoryRequest' and r['peer'] == channel_id
		]

	def remove_record(self, target='alpha_fx'):
		os.remove(provenance_path(self.out, target))


class RecordTests(ProvenanceCase):

	async def test_full_run_records_profile_and_fingerprint(self):
		await self.collect(ALICE)
		record = self.record()
		messages = self.messages()
		self.assertEqual(record['format'], 1)
		self.assertEqual((record['profile_name'], record['profile_id']), ('alice', ALICE.id))
		self.assertEqual(record['channel_id'], ALPHA)
		self.assertEqual(record['history'], 'replaced')
		self.assertEqual(record['message_count'], len(messages['messages']))
		self.assertEqual(record['newest_id'], max(m['id'] for m in messages['messages']))
		self.assertEqual(record['run_started_at'], self.status()['started_at'])
		self.assertEqual(
			(record['takeover'], record['previous_profile_name'], record['previous_profile_id']),
			(False, None, None)
		)
		status = self.status()
		self.assertEqual((status['profile_name'], status['profile_id']), ('alice', ALICE.id))

	async def test_run_without_profile_writes_no_record(self):
		summary = await self.collect(None)
		self.assertIsNone(summary.profile)
		self.assertFalse(os.path.exists(provenance_path(self.out, 'alpha_fx')))
		self.assertEqual((self.status()['profile_name'], self.status()['profile_id']), (None, None))

	async def test_metadata_only_leaves_the_record(self):
		await self.collect(ALICE)
		before = self.record()
		await self.collect(BOB, metadata_only=True)
		self.assertEqual(self.record(), before)
		self.assertNotIn('takeover', self.status()['targets'][0])

	async def test_update_creating_a_history_records_its_profile(self):
		summary = await self.collect(BOB, update=True)
		self.assertEqual(summary.collected_in_full, ['alpha_fx'])
		self.assertEqual((self.record()['profile_name'], self.record()['history']), ('bob', 'created'))


class UpdateTests(ProvenanceCase):

	async def test_same_profile_merges_and_keeps_the_record(self):
		await self.collect(ALICE)
		first = self.record()
		summary = await self.update(ALICE)
		self.assertEqual(summary.succeeded, ['alpha_fx'])
		record = self.record()
		self.assertEqual(record['history'], 'merged')
		self.assertEqual(
			{k: record[k] for k in ('profile_name', 'profile_id', 'channel_id', 'takeover')},
			{k: first[k] for k in ('profile_name', 'profile_id', 'channel_id', 'takeover')}
		)
		self.assertEqual(record['message_count'], first['message_count'] + 1)
		self.assertEqual(record['newest_id'], first['newest_id'] + 1)

	async def test_other_profile_is_refused_before_any_request_for_that_channel(self):
		await self.collect(ALICE, targets=('alpha_fx', 'beta_fx'))
		await self.collect(BOB, targets=('beta_fx',))
		before = self.messages('alpha_fx')
		summary = await self.update(BOB, targets=('alpha_fx', 'beta_fx'))

		self.assertEqual(summary.succeeded, ['beta_fx'])
		self.assertEqual([t for t, _ in summary.failed], ['alpha_fx'])
		reason = summary.failed[0][1]
		self.assertIn('belongs to profile alice', reason)
		self.assertIn('without --update', reason)
		self.assertEqual(self.history_requests(ALPHA), [])
		self.assertTrue(self.history_requests(102))
		resolved = [d.get('target') for name, d in self.client.calls if name == 'get_entity']
		self.assertEqual(resolved, ['beta_fx'])
		self.assertEqual(self.messages('alpha_fx'), before)
		self.assertEqual(self.record('alpha_fx')['profile_name'], 'alice')
		entry = self.status()['targets'][0]
		self.assertEqual(entry['status'], 'failed')
		self.assertEqual(self.status()['state'], 'partial')

	async def test_matching_rules(self):
		cases = (
			('same ID', ALICE, ALICE, True),
			('same name, other ID', ALICE, ALICE_ELSEWHERE, False),
			('other name', ALICE, BOB, False),
			('name only, same name', RunProfile('alice', None), ALICE, True),
			('name only, other name', RunProfile('alice', None), BOB, False),
			('ID recorded, run without ID', DEFAULT_WITH_ID, DEFAULT, False),
			('default without ID, default with ID', DEFAULT, DEFAULT_WITH_ID, True),
		)
		for label, writer, updater, allowed in cases:
			with self.subTest(case=label):
				if os.path.exists(self.out):
					import shutil

					shutil.rmtree(self.out)
				await self.collect(writer)
				summary = await self.update(updater)
				self.assertEqual(summary.succeeded == ['alpha_fx'], allowed, summary.failed)
				self.assertEqual(self.record()['profile_name'], writer.name)
				self.assertEqual(self.record()['profile_id'], writer.id)

	async def test_history_without_record_accepts_only_the_default_profile(self):
		await self.collect(None)
		for profile, allowed in ((BOB, False), (DEFAULT_WITH_ID, True), (DEFAULT, True)):
			with self.subTest(profile=profile):
				summary = await self.update(profile)
				self.assertEqual(summary.succeeded == ['alpha_fx'], allowed, summary.failed)
				if not allowed:
					self.assertIn('no profile record', summary.failed[0][1])
				self.assertFalse(os.path.exists(provenance_path(self.out, 'alpha_fx')))

	async def test_record_not_matching_the_history_refuses_every_update(self):
		await self.collect(ALICE)
		path = storage.files.history_path(self.out, 'alpha_fx')
		history = self.messages()
		history['messages'] = history['messages'][1:]
		with open(path, 'w', encoding='utf-8') as fh:
			json.dump(history, fh)
		summary = await self.update(ALICE)
		self.assertEqual(summary.succeeded, [])
		self.assertIn('does not match the saved history', summary.failed[0][1])

	async def test_unreadable_record_refuses_the_update(self):
		await self.collect(ALICE)
		for content in ('{', '[]', '{"profile_name": 5, "message_count": 1, "newest_id": 1}'):
			with self.subTest(content=content):
				with open(provenance_path(self.out, 'alpha_fx'), 'w', encoding='utf-8') as fh:
					fh.write(content)
				summary = await self.update(ALICE)
				self.assertEqual(summary.succeeded, [])
				self.assertIn('--update refused', summary.failed[0][1])

	def test_refusal_rule_names_no_identifier(self):
		reason = refusal({'profile_name': 'alice', 'profile_id': ALICE.id}, ALICE_ELSEWHERE)
		self.assertIn('another profile ID', reason)
		self.assertNotIn(ALICE.id, reason)
		self.assertNotIn(ALICE_ELSEWHERE.id, reason)


class TakeoverTests(ProvenanceCase):

	async def test_full_run_by_another_profile_records_the_takeover(self):
		await self.collect(ALICE)
		summary = await self.collect(BOB)
		record = self.record()
		self.assertEqual((record['profile_name'], record['profile_id']), ('bob', BOB.id))
		self.assertEqual(
			(record['takeover'], record['previous_profile_name'], record['previous_profile_id']),
			(True, 'alice', ALICE.id)
		)
		entry = self.status()['targets'][0]
		self.assertEqual(
			(entry['takeover'], entry['previous_profile_name'], entry['previous_profile_id']),
			(True, 'alice', ALICE.id)
		)
		self.assertEqual(summary.takeovers, [('alpha_fx', 'alice')])

	async def test_window_run_by_another_profile_records_the_takeover(self):
		await self.collect(ALICE)
		summary = await self.collect(BOB, window=HistoryWindow(max_messages=2))
		self.assertEqual(summary.takeovers, [('alpha_fx', 'alice')])
		self.assertEqual(self.record()['message_count'], 2)

	async def test_same_profile_rerun_is_no_takeover(self):
		await self.collect(ALICE)
		summary = await self.collect(ALICE)
		self.assertEqual(summary.takeovers, [])
		self.assertFalse(self.record()['takeover'])
		self.assertNotIn('takeover', self.status()['targets'][0])

	async def test_history_without_record(self):
		await self.collect(None)
		summary = await self.collect(DEFAULT)
		self.assertEqual(summary.takeovers, [])
		self.remove_record()
		summary = await self.collect(BOB)
		self.assertEqual(summary.takeovers, [('alpha_fx', None)])
		self.assertEqual(
			(self.record()['previous_profile_name'], self.record()['previous_profile_id']), (None, None)
		)

	async def test_unreadable_record_is_taken_over(self):
		await self.collect(ALICE)
		with open(provenance_path(self.out, 'alpha_fx'), 'w', encoding='utf-8') as fh:
			fh.write('{')
		summary = await self.collect(ALICE)
		self.assertEqual(summary.takeovers, [('alpha_fx', None)])
		self.assertFalse(self.record()['previous_profile_name'])

	async def test_takeover_record_is_kept_by_the_new_owners_update(self):
		await self.collect(ALICE)
		await self.collect(BOB)
		summary = await self.update(BOB)
		self.assertEqual(summary.succeeded, ['alpha_fx'])
		record = self.record()
		self.assertEqual((record['profile_name'], record['takeover'], record['previous_profile_name']),
			('bob', True, 'alice'))


if __name__ == '__main__':
	unittest.main()
