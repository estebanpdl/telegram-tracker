# -*- coding: utf-8 -*-
'''
The fake client must behave like the Telethon boundary it replaces:
server-style pagination, Telethon error types, recorded request arguments,
and a code → password login challenge. No real client is constructed.
'''

# import modules
import asyncio
import datetime
import signal
import unittest

import offline_guard

from telethon import errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetHistoryRequest

from fakes import (
	FIXTURE_CODE, FakeTelegramClient, FakeTL, FixtureWorld, ScriptedPrompt
)


def history_request(peer, offset_id=0, min_id=0):
	return GetHistoryRequest(
		peer=peer, offset_id=offset_id, offset_date=0, add_offset=0,
		limit=100, max_id=0, min_id=min_id, hash=0
	)


class FakeDataTests(offline_guard.OfflineAsyncTestCase):

	async def test_pages_follow_offset_and_min_id(self):
		client = FakeTelegramClient(page_size=2)
		first = await client(history_request(101))
		self.assertEqual([m.id for m in first.messages], [10, 9])
		second = await client(history_request(101, offset_id=9))
		self.assertEqual([m.id for m in second.messages], [8, 7])
		filtered = await client(history_request(101, offset_id=7, min_id=6))
		self.assertEqual(filtered.messages, [])
		self.assertEqual(filtered.to_dict()['count'], 7)

	async def test_page_chats_include_channel_and_forward_sources(self):
		client = FakeTelegramClient(page_size=2)
		page = (await client(history_request(102, offset_id=4))).to_dict()
		self.assertEqual([c['id'] for c in page['chats']], [102, 205, 201])
		self.assertEqual([u['id'] for u in page['users']], [9001])

	async def test_to_dict_returns_native_values_and_fresh_copies(self):
		client = FakeTelegramClient()
		page = await client(history_request(101, offset_id=8))
		data = page.to_dict()
		self.assertIsInstance(data['messages'][0]['date'], datetime.datetime)
		self.assertIsInstance(
			data['messages'][0]['media']['document']['file_reference'], bytes
		)
		data['messages'].clear()
		self.assertEqual(len(page.to_dict()['messages']), 2)

	async def test_requests_record_arguments(self):
		client = FakeTelegramClient()
		await client(history_request(101, offset_id=9, min_id=3))
		await client(GetFullChannelRequest(201))
		self.assertEqual(client.requests[0], {
			'type': 'GetHistoryRequest', 'peer': 101, 'offset_id': 9,
			'offset_date': 0, 'add_offset': 0, 'limit': 100, 'max_id': 0,
			'min_id': 3, 'hash': 0
		})
		self.assertEqual(client.requests[1], {'type': 'GetFullChannelRequest', 'channel': 201})

	async def test_full_channel_payload(self):
		client = FakeTelegramClient()
		response = await client(GetFullChannelRequest(101))
		self.assertEqual(response.full_chat.participants_count, 1500)
		self.assertEqual([c['id'] for c in response.to_dict()['chats']], [101, 106])

	async def test_entity_errors_use_telethon_types(self):
		client = FakeTelegramClient()
		entity = await client.get_entity('alpha_fx')
		self.assertEqual((entity.id, entity.username), (101, 'alpha_fx'))
		with self.assertRaises(ValueError):
			await client.get_entity('missing_fx')
		with self.assertRaises(errors.ChannelPrivateError):
			await client.get_entity('private_fx')
		with self.assertRaises(ValueError):
			await client(GetFullChannelRequest(203))

	async def test_scripted_outcomes_and_gates(self):
		client = FakeTelegramClient()
		gate = asyncio.Event()

		async def wait_for_gate():
			await gate.wait()

		client.script(
			'GetHistoryRequest',
			errors.FloodWaitError(request=None, capture=5),
			wait_for_gate
		)
		with self.assertRaises(errors.FloodWaitError):
			await client(history_request(101))

		task = asyncio.create_task(client(history_request(101)))
		await asyncio.sleep(0)
		self.assertFalse(task.done())
		gate.set()
		page = await task
		self.assertEqual([m.id for m in page.messages], [10, 9])

	async def test_lifecycle_counts_and_loop(self):
		client = FakeTelegramClient()
		await client.connect()
		await client.disconnect()
		self.assertEqual((client.connect_count, client.disconnect_count), (1, 1))
		self.assertEqual(len(set(client.loop_ids)), 1)


class FakeAuthTests(offline_guard.OfflineAsyncTestCase):

	async def test_code_then_password_challenge(self):
		client = FakeTelegramClient(authorized=False, password='pw-sentinel')
		self.assertFalse(await client.is_user_authorized())
		await client.send_code_request('+10000000000')
		with self.assertRaises(errors.SessionPasswordNeededError):
			await client.sign_in('+10000000000', FIXTURE_CODE)
		with self.assertRaises(errors.PasswordHashInvalidError):
			await client.sign_in(password='wrong')
		await client.sign_in(password='pw-sentinel')
		self.assertTrue(client.authorized)
		self.assertEqual(client.call_names(), [
			'is_user_authorized', 'send_code_request', 'sign_in_code',
			'sign_in_password', 'sign_in_password'
		])
		self.assertNotIn('pw-sentinel', repr(client.calls))

	async def test_invalid_code(self):
		client = FakeTelegramClient(authorized=False)
		with self.assertRaises(errors.PhoneCodeInvalidError):
			await client.sign_in('+10000000000', '00000')
		await client.sign_in('+10000000000', FIXTURE_CODE)
		self.assertTrue(client.authorized)


class ScriptedPromptTests(offline_guard.OfflineTestCase):

	def test_answers_raise_and_record(self):
		prompt = ScriptedPrompt('a', EOFError, KeyboardInterrupt())
		self.assertEqual(prompt('first: '), 'a')
		with self.assertRaises(EOFError):
			prompt('second: ')
		with self.assertRaises(KeyboardInterrupt):
			prompt('third: ')
		self.assertEqual(prompt.prompts, ['first: ', 'second: ', 'third: '])
		self.assertIs(prompt.sigint_handlers[0], signal.getsignal(signal.SIGINT))
		with self.assertRaises(AssertionError):
			prompt('unscripted: ')


class FixtureWorldTests(offline_guard.OfflineTestCase):

	def test_unknown_message_fields_are_rejected(self):
		world = FixtureWorld()
		with self.assertRaises(ValueError):
			world._message(world.raw, 101, {'id': 1, 'not_a_field': True})

	def test_fake_tl_attribute_errors(self):
		obj = FakeTL({'_': 'X', 'a': {'b': 1}})
		self.assertEqual(obj.a.b, 1)
		with self.assertRaises(AttributeError):
			obj.missing


if __name__ == '__main__':
	unittest.main()
