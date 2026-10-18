# -*- coding: utf-8 -*-
'''
Fixture integrity: synthetic-only content, every field the existing helpers
read, and expectations that agree with the fixture world. Column lists are
read from utils/__init__.py with `ast`, without importing project code.
'''

# import modules
import ast
import csv
import os
import re
import unittest

import offline_guard

from fakes import FIXTURES, WORLD_PATH, FixtureWorld, load_expected


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# fields read by build-datasets.py for Message / MessageService items
MESSAGE_FIELDS = (
	'_', 'id', 'peer_id', 'date', 'message', 'from_id', 'views', 'forwards',
	'replies', 'fwd_from', 'reply_to', 'media'
)
SERVICE_FIELDS = ('_', 'id', 'peer_id', 'date', 'action', 'from_id', 'reply_to')

# columns build-datasets.py adds itself before selecting chats_dataset_columns()
BUILDER_STAT_COLUMNS = (
	'collected_actions', 'collected_posts', 'replies', 'other_actions',
	'number_views', 'forwards', 'replies_received'
)


def static_list(function_name):
	'''
	Return the literal list returned by a function in telegram_tracker/utils/__init__.py.
	'''
	with open(os.path.join(ROOT, 'src', 'telegram_tracker', 'utils', '__init__.py'), encoding='utf-8') as fh:
		tree = ast.parse(fh.read())
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name == function_name:
			for stmt in node.body:
				if isinstance(stmt, ast.Return):
					return ast.literal_eval(stmt.value)
	raise LookupError(function_name)


class SyntheticContentTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		self.world = FixtureWorld()
		with open(WORLD_PATH, encoding='utf-8') as fh:
			self.text = fh.read()

	def test_usernames_and_targets_are_synthetic(self):
		for chat in self.world.chats.values():
			username = chat.get('username')
			if username is not None:
				self.assertTrue(username.endswith('_fx'), username)
		for target in self.world.entities:
			self.assertTrue(target.endswith('_fx'), target)

	def test_ids_are_small_invented_values(self):
		for chat_id in self.world.chats:
			self.assertLess(chat_id, 1000)
		for user_id in self.world.users:
			self.assertLess(user_id, 10000)

	def test_only_placeholder_phone_numbers(self):
		phones = set(re.findall(r'\+\d{6,}', self.text))
		self.assertEqual(phones, {'+0000000000'})

	def test_links_use_reserved_example_domain(self):
		for url in re.findall(r'https?://[^\s"]+', self.text):
			self.assertRegex(url, r'^https://www\.example\.org/')


class RequiredFieldTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		self.world = FixtureWorld()

	def test_messages_have_every_field_the_dataset_builder_reads(self):
		for channel_id, history in self.world.histories.items():
			for message in history:
				fields = SERVICE_FIELDS if message['_'] == 'MessageService' else MESSAGE_FIELDS
				for field in fields:
					with self.subTest(channel=channel_id, msg=message['id'], field=field):
						self.assertIn(field, message)

	def test_channels_have_every_selected_chat_column(self):
		columns = [
			c for c in static_list('chats_dataset_columns')
			if c not in BUILDER_STAT_COLUMNS
		]
		for chat_id, chat in self.world.chats.items():
			if chat['_'] != 'Channel':
				continue
			for column in columns:
				with self.subTest(chat=chat_id, column=column):
					self.assertIn(column, chat)

	def test_expected_empty_header_matches_static_columns(self):
		expected = load_expected('collection.json')
		base = [
			c for c in static_list('chats_dataset_columns')
			if c not in BUILDER_STAT_COLUMNS
		]
		counter = expected['counter_columns'][1:]
		counter.remove('username')
		self.assertEqual(expected['empty_collected_chats_columns'], base + counter)

	def test_expected_dataset_rows_use_known_columns(self):
		columns = set(static_list('msgs_dataset_columns'))
		for signature, row in load_expected('dataset.json')['rows'].items():
			for column in row:
				with self.subTest(row=signature, column=column):
					self.assertIn(column, columns)

	def test_coverage_of_message_kinds(self):
		kinds = {
			(m.get('media') or {}).get('_')
			for history in self.world.histories.values() for m in history
		}
		self.assertTrue({
			None, 'MessageMediaWebPage', 'MessageMediaDocument', 'MessageMediaPoll',
			'MessageMediaGeo', 'MessageMediaContact', 'MessageMediaVenue'
		} <= kinds)
		messages = [m for h in self.world.histories.values() for m in h]
		self.assertTrue(any(m['_'] == 'MessageService' for m in messages))
		self.assertTrue(any(m.get('fwd_from') for m in messages))
		self.assertTrue(any(m.get('reply_to') and m['_'] == 'Message' for m in messages))
		self.assertEqual(self.world.histories[103], [])


class ExpectationConsistencyTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		self.world = FixtureWorld()
		self.collection = load_expected('collection.json')

	def test_message_counts_match_world(self):
		batch = self.collection['scenarios']['batch']
		for username, spec in batch['messages_json'].items():
			channel_id = self.world.entities[username]['chat']
			ids = [m['id'] for m in self.world.histories[channel_id]]
			self.assertEqual(spec['message_ids'], ids)
			self.assertEqual(spec['count'], len(ids))

	def test_two_requested_channels_share_a_discovered_chat(self):
		sources = {
			row['username']: row['source']
			for row in self.collection['scenarios']['batch']['counter']
		}
		self.assertEqual(sources['shared_fx'], "['alpha_fx', 'beta_fx']")

	def test_counter_and_chats_txt_agree(self):
		batch = self.collection['scenarios']['batch']
		self.assertEqual(
			sorted(row['id'] for row in batch['counter']),
			sorted(batch['chats_txt'])
		)

	def test_network_fixtures_have_expected_columns(self):
		for name in ('no_edges.csv', 'equal_degree.csv'):
			with open(os.path.join(FIXTURES, 'network', name), encoding='utf-8') as fh:
				header = next(csv.reader(fh))
			self.assertEqual(header, ['id', 'username', 'counter', 'source'])

	def test_every_fix_is_tied_to_fixture_evidence(self):
		for fix in self.collection['fixes']:
			self.assertTrue(fix['fixture_evidence'], fix['id'])


if __name__ == '__main__':
	unittest.main()
