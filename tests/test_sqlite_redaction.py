# -*- coding: utf-8 -*-
'''
The collecting account's User.phone is redacted before storage: every
User object flagged `is_self` is stored with `phone` null in raw columns
and in the computed `peers.phone`, wherever it appears (channel snapshot,
history, an --update merge, nested in a message, repeated), and the
synthetic fixture's number has no copy in the database file. Every other
field of that user and every other user, phone number included, is stored unfiltered.
Synthetic users only.
'''

# import modules
import copy
import json
import os

import sqlite_support

from sqlite_support import ALPHA, BETA, add_messages

from telegram_tracker.storage.records import ChannelResult
from telegram_tracker.storage.sqlite.histories import load_history
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


SELF_PHONE = '990000111'
OTHER_PHONE = '990000222'
SELF_ID = 9001
OTHER_ID = 9002


def user(user_id, *, is_self=False, phone=None):
	return {
		'_': 'User', 'id': user_id, 'is_self': is_self, 'contact': False, 'bot': False, 'min': False,
		'access_hash': user_id * 10, 'first_name': 'Synthetic', 'last_name': f'User {user_id}',
		'username': None, 'phone': phone, 'status': {'_': 'UserStatusRecently'}
	}


def nested_page(*users):
	return {'_': 'MessageMediaWebPage', 'webpage': {'_': 'WebPage', 'id': 1, 'cached_page': {
		'_': 'Page', 'users': list(users)}}}


class RedactionCase(sqlite_support.SQLiteCase):

	def database_bytes(self):
		data = sqlite_support.read_bytes(self.db)
		journal = self.db + '-journal'
		self.assertFalse(os.path.exists(journal))
		return data

	def peer(self, user_id):
		row = self.rows("SELECT phone, raw FROM peers WHERE peer_kind = 'user' AND peer_id = ?", user_id)
		return row[0][0], json.loads(row[0][1])

	def commit(self, *results):
		os.makedirs(self.out, exist_ok=True)
		writer = SQLiteWriter.open(self.db, targets=[r.target for r in results])
		for result in results:
			writer.commit(result)
		writer.close()

	def result(self, index, channel_id, *, snapshot_users=(), history_users=(), messages=()):
		return ChannelResult(
			index=index, target=f't{index}', channel_id=channel_id,
			channel_data={'_': 'messages.ChatFull', 'full_chat': {'_': 'ChannelFull', 'id': channel_id},
				'chats': [], 'users': copy.deepcopy(list(snapshot_users))},
			history={'_': 'messages.ChannelMessages', 'count': len(messages),
				'messages': copy.deepcopy(list(messages)), 'chats': [], 'users': copy.deepcopy(list(history_users))},
			history_mode='replaced', messages=len(messages)
		)


class RedactionTests(RedactionCase):

	def test_self_phone_is_absent_everywhere_and_the_rest_is_kept(self):
		own = user(SELF_ID, is_self=True, phone=SELF_PHONE)
		other = user(OTHER_ID, phone=OTHER_PHONE)
		message = {'_': 'Message', 'id': 3, 'message': 'x', 'media': nested_page(own, other)}
		self.commit(
			self.result(0, 1001, snapshot_users=[own, other], history_users=[other, own], messages=[message]),
			self.result(1, 1002, snapshot_users=[own], history_users=[own, own], messages=[message]),
		)
		self.assertNotIn(SELF_PHONE.encode(), self.database_bytes())
		self.assertIn(OTHER_PHONE.encode(), self.database_bytes())

		phone, raw = self.peer(SELF_ID)
		self.assertIsNone(phone)
		self.assertEqual(raw, {**own, 'phone': None})
		phone, raw = self.peer(OTHER_ID)
		self.assertEqual((phone, raw), (OTHER_PHONE, other))

		for channel_id in (1001, 1002):
			history = load_history(self.open(), channel_id)
			self.assertNotIn(SELF_PHONE, json.dumps(history))
			nested = history['messages'][0]['media']['webpage']['cached_page']['users']
			self.assertEqual(nested, [{**own, 'phone': None}, other])
		snapshot = json.loads(self.rows('SELECT raw FROM channel_snapshots WHERE channel_id = 1001')[0][0])
		self.assertEqual(snapshot['users'], [{**own, 'phone': None}, other])
		self.assertEqual(self.rows("SELECT count(*) FROM peers WHERE phone IS NOT NULL"), [(1,)])

	def test_self_user_without_phone_and_other_users_are_unchanged(self):
		own = user(SELF_ID, is_self=True)
		other = user(OTHER_ID, phone=OTHER_PHONE)
		self.commit(self.result(0, 1001, snapshot_users=[own], history_users=[other]))
		self.assertEqual(self.peer(SELF_ID), (None, own))
		self.assertEqual(self.peer(OTHER_ID), (OTHER_PHONE, other))

	def test_without_a_self_user_phones_are_stored(self):
		lookalikes = [
			user(OTHER_ID, phone=OTHER_PHONE),
			{**user(OTHER_ID + 1, phone=SELF_PHONE), 'is_self': 1},
		]
		self.commit(self.result(0, 1001, history_users=lookalikes))
		self.assertEqual(self.peer(OTHER_ID)[0], OTHER_PHONE)
		self.assertEqual(self.peer(OTHER_ID + 1)[0], SELF_PHONE)

	async def test_collection_and_update_never_store_the_own_phone(self):
		client = self.client()
		client.world.users[SELF_ID].update(is_self=True, phone=SELF_PHONE)
		await self.collect(['alpha_fx', 'beta_fx'], client)
		client = self.client()
		client.world.users[SELF_ID].update(is_self=True, phone=SELF_PHONE)
		client.world.users[OTHER_ID] = user(OTHER_ID, phone=OTHER_PHONE)
		add_messages(client.world, ALPHA, 11, user_id=SELF_ID)
		add_messages(client.world, BETA, 50, user_id=OTHER_ID)
		summary = await self.collect(['alpha_fx', 'beta_fx'], client, update=True)
		self.assertEqual(summary.succeeded, ['alpha_fx', 'beta_fx'])
		self.assertEqual(self.rows('SELECT history_mode FROM run_targets WHERE run_id = 2'), [('merged',), ('merged',)])

		self.assertNotIn(SELF_PHONE.encode(), self.database_bytes())
		phone, raw = self.peer(SELF_ID)
		self.assertIsNone(phone)
		self.assertTrue(raw['is_self'])
		self.assertEqual(raw['last_name'], 'Author')
		self.assertEqual(self.peer(OTHER_ID)[0], OTHER_PHONE)
		self.assertIn(SELF_ID, [u['id'] for u in load_history(self.open(), ALPHA)['users']])
		self.assertIntact()
