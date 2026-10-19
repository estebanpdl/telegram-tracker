# -*- coding: utf-8 -*-
'''
The collecting account's User.phone is redacted when written: a User
object with `is_self` keeps every field except `phone`, which becomes null, in
<channel>.json and <channel>_messages.json. Every other user, including
one with a phone number, is written unfiltered. Synthetic users only.
'''

# import modules
import contextlib
import copy
import io
import json
import os
import tempfile

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient

from telegram_tracker import collector, storage
from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.storage.records import ChannelResult
from telegram_tracker.storage.writer import OutputWriter, drop_own_phone


SELF_PHONE = '990000111'
OTHER_PHONE = '990000222'
AUTHOR = 9001


def user(user_id, *, is_self=False, phone=None):
	return {
		'_': 'User', 'id': user_id, 'is_self': is_self, 'contact': False, 'bot': False,
		'access_hash': user_id * 10, 'first_name': 'Synthetic', 'last_name': f'User {user_id}',
		'username': None, 'phone': phone, 'status': {'_': 'UserStatusRecently'}
	}


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


class DropOwnPhoneTests(offline_guard.OfflineTestCase):

	def test_self_user_loses_only_its_phone(self):
		own = user(1, is_self=True, phone=SELF_PHONE)
		payload = {'users': [own, user(2, phone=OTHER_PHONE)]}
		expected = copy.deepcopy(payload)
		expected['users'][0]['phone'] = None

		self.assertEqual(drop_own_phone(payload), 1)
		self.assertEqual(payload, expected)
		self.assertEqual(payload['users'][1]['phone'], OTHER_PHONE)

	def test_self_user_without_phone_is_unchanged(self):
		payload = {'users': [user(1, is_self=True)]}
		expected = copy.deepcopy(payload)
		self.assertEqual(drop_own_phone(payload), 0)
		self.assertEqual(payload, expected)

	def test_payload_without_self_user_is_unchanged(self):
		payload = {
			'users': [user(2, phone=OTHER_PHONE), user(3)],
			'messages': [{'_': 'Message', 'id': 5, 'media': {
				'_': 'MessageMediaContact', 'phone_number': OTHER_PHONE, 'user_id': 2
			}}]
		}
		expected = copy.deepcopy(payload)
		self.assertEqual(drop_own_phone(payload), 0)
		self.assertEqual(payload, expected)

	def test_every_occurrence_is_redacted(self):
		payload = {
			'users': [user(1, is_self=True, phone=SELF_PHONE), user(2, phone=OTHER_PHONE)],
			'messages': [{'_': 'Message', 'id': 5, 'media': {
				'_': 'MessageMediaWebPage', 'webpage': {'_': 'WebPage', 'cached_page': {
					'_': 'Page', 'users': [user(1, is_self=True, phone=SELF_PHONE)]
				}}
			}}],
			'nested': [[user(1, is_self=True, phone=SELF_PHONE)]]
		}
		self.assertEqual(drop_own_phone(payload), 3)
		self.assertNotIn(SELF_PHONE, json.dumps(payload))
		self.assertEqual(json.dumps(payload).count(OTHER_PHONE), 1)

	def test_only_user_objects_flagged_self_are_redacted(self):
		lookalikes = [
			{'_': 'UserFull', 'id': 1, 'is_self': True, 'phone': SELF_PHONE},
			{'_': 'User', 'id': 1, 'is_self': 1, 'phone': SELF_PHONE},
			{'_': 'User', 'id': 1, 'is_self': 'true', 'phone': SELF_PHONE},
			{'_': 'User', 'id': 1, 'phone': SELF_PHONE}
		]
		payload = copy.deepcopy(lookalikes)
		self.assertEqual(drop_own_phone(payload), 0)
		self.assertEqual(payload, lookalikes)


class WriterTests(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(tmp.name, 'data')

	def test_commit_writes_both_files_without_the_own_phone(self):
		result = ChannelResult(
			index=0,
			target='alpha_fx',
			channel_id=101,
			channel_data={'_': 'messages.ChatFull', 'chats': [], 'users': [
				user(1, is_self=True, phone=SELF_PHONE), user(2, phone=OTHER_PHONE)
			]},
			history={'messages': [], 'chats': [], 'users': [
				user(2, phone=OTHER_PHONE), user(1, is_self=True, phone=SELF_PHONE)
			]},
			history_mode='replaced'
		)
		OutputWriter(self.out).commit(result)

		channel = read_json(os.path.join(self.out, 'alpha_fx', 'alpha_fx.json'))
		history = read_json(storage.files.history_path(self.out, 'alpha_fx'))
		self.assertEqual([u['phone'] for u in channel['users']], [None, OTHER_PHONE])
		self.assertEqual([u['phone'] for u in history['users']], [OTHER_PHONE, None])
		self.assertEqual(history['users'][1], {**user(1, is_self=True), 'phone': None})

	async def collect(self, client, **kwargs):
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
			return await collector.run.run_collection(
				['beta_fx'],
				self.out,
				client_factory=lambda: client,
				authorize=lambda c: ensure_authorized(c, None),
				**kwargs
			)

	def self_author_client(self):
		client = FakeTelegramClient(page_size=2)
		client.world.users[AUTHOR].update(is_self=True, phone=SELF_PHONE)
		return client

	def messages(self):
		return read_json(storage.files.history_path(self.out, 'beta_fx'))

	async def test_collected_history_keeps_the_self_author_without_phone(self):
		summary = await self.collect(self.self_author_client())
		self.assertEqual(summary.succeeded, ['beta_fx'])
		authors = [u for u in self.messages()['users'] if u['id'] == AUTHOR]
		self.assertEqual(len(authors), 1)
		self.assertTrue(authors[0]['is_self'])
		self.assertIsNone(authors[0]['phone'])
		self.assertEqual(authors[0]['last_name'], 'Author')

	async def test_other_author_keeps_its_phone(self):
		client = FakeTelegramClient(page_size=2)
		client.world.users[AUTHOR]['phone'] = OTHER_PHONE
		await self.collect(client)
		authors = [u for u in self.messages()['users'] if u['id'] == AUTHOR]
		self.assertEqual([u['phone'] for u in authors], [OTHER_PHONE])

	async def test_update_redacts_an_earlier_history_it_merges(self):
		await self.collect(FakeTelegramClient(page_size=2))
		path = storage.files.history_path(self.out, 'beta_fx')
		earlier = self.messages()
		earlier['users'].append(user(1, is_self=True, phone=SELF_PHONE))
		with open(path, 'w', encoding='utf-8') as fh:
			json.dump(earlier, fh)

		client = self.self_author_client()
		newest = client.world.histories[102][0]
		added = copy.deepcopy(newest)
		added['id'] = newest['id'] + 1
		client.world.histories[102].insert(0, added)
		summary = await self.collect(client, update=True)

		self.assertEqual(summary.succeeded, ['beta_fx'])
		with open(path, encoding='utf-8') as fh:
			text = fh.read()
		self.assertNotIn(SELF_PHONE, text)
		users = {u['id']: u for u in json.loads(text)['users']}
		self.assertIsNone(users[1]['phone'])
		self.assertTrue(users[1]['is_self'])
