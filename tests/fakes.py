# -*- coding: utf-8 -*-
'''
Test doubles for the Telethon boundary. Nothing here constructs a real
client, opens a socket, or touches credentials or session files.

- `FixtureWorld` loads `fixtures/telegram_world.json` and answers entity,
  full-channel, and history lookups the way Telegram would.
- `FakeTL` mimics a Telethon TLObject: attribute access plus `to_dict()`.
- `FakeTelegramClient` implements the client methods the project awaits
  (`connect`, `is_user_authorized`, `send_code_request`, `sign_in`,
  `get_entity`, `__call__`, `disconnect`) and records every call. Outcomes
  can be scripted per call (exceptions, delays, gates).
- `ScriptedPrompt` stands in for `input`/`getpass` prompts.
'''

# import modules
import asyncio
import copy
import json
import os
import signal

from datetime import datetime

from telethon import errors


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
WORLD_PATH = os.path.join(FIXTURES, 'telegram_world.json')
EXPECTED_DIR = os.path.join(FIXTURES, 'expected')

FIXTURE_PHONE = '+10000000000'
FIXTURE_CODE = '24680'


def load_expected(name):
	'''
	Load a JSON expectation file from fixtures/expected.
	'''
	with open(os.path.join(EXPECTED_DIR, name), encoding='utf-8') as fh:
		return json.load(fh)


def _native(value, key=None):
	'''
	Turn fixture JSON into what Telethon's to_dict() yields: aware
	datetimes for `date` fields and bytes for {"__bytes_hex__": ...}.
	'''
	if isinstance(value, dict):
		if set(value) == {'__bytes_hex__'}:
			return bytes.fromhex(value['__bytes_hex__'])
		return {k: _native(v, k) for k, v in value.items()}
	if isinstance(value, list):
		return [_native(v) for v in value]
	if key == 'date' and isinstance(value, str):
		return datetime.fromisoformat(value)
	return value


class FakeTL:
	'''
	Minimal TLObject stand-in. `to_dict()` returns a fresh deep copy, as
	Telethon builds a new dict on every call.
	'''
	def __init__(self, data):
		self._data = data

	def __getattr__(self, name):
		try:
			value = self._data[name]
		except KeyError:
			raise AttributeError(name) from None
		return _wrap(value)

	def to_dict(self):
		return copy.deepcopy(self._data)

	def __repr__(self):
		return f'FakeTL({self._data.get("_")!r})'


def _wrap(value):
	if isinstance(value, dict):
		return FakeTL(value)
	if isinstance(value, list):
		return [_wrap(v) for v in value]
	return value


class FixtureWorld:
	'''
	Synthetic Telegram state with server-like lookups.
	'''
	def __init__(self, path=WORLD_PATH):
		with open(path, encoding='utf-8') as fh:
			raw = json.load(fh)

		self.raw = raw
		self.chats = {
			int(k): self._chat(raw['channel_defaults'], v)
			for k, v in raw['chats'].items()
		}
		self.users = {int(k): _native(v) for k, v in raw['users'].items()}
		self.entities = raw['entities']
		self.full_channels = {int(k): v for k, v in raw['full_channels'].items()}
		self.histories = {
			int(cid): [self._message(raw, int(cid), m) for m in msgs]
			for cid, msgs in raw['histories'].items()
		}

	@staticmethod
	def _chat(defaults, spec):
		if spec.get('_', 'Channel') != 'Channel':
			return _native(dict(spec))
		chat = dict(defaults)
		chat.update(spec)
		return _native(chat)

	@staticmethod
	def _message(raw, channel_id, spec):
		spec = dict(spec)
		service = spec.pop('_service', False)
		message = dict(raw['service_defaults' if service else 'message_defaults'])
		unknown = set(spec) - set(message)
		if unknown:
			raise ValueError(f'unknown message fields {sorted(unknown)}')
		message.update(spec)
		message['peer_id'] = {'_': 'PeerChannel', 'channel_id': channel_id}
		return _native(message)

	def chat(self, chat_id):
		return copy.deepcopy(self.chats[chat_id])

	def entity(self, target):
		'''
		Resolve an input string the way `client.get_entity` would.
		'''
		spec = self.entities.get(target)
		if spec is None or spec.get('error') == 'ValueError':
			raise ValueError(f'No user has "{target}" as username')
		if 'error' in spec:
			raise getattr(errors, spec['error'])(request=None)
		return FakeTL(self.chat(spec['chat']))

	def full_channel(self, channel_id):
		'''
		Build a messages.ChatFull payload for GetFullChannelRequest.
		'''
		spec = self.full_channels.get(channel_id)
		if spec is None or spec.get('error') == 'ValueError':
			raise ValueError(
				f'Could not find the input entity for PeerChannel(channel_id={channel_id})'
			)
		return FakeTL({
			'_': 'messages.ChatFull',
			'full_chat': {
				'_': 'ChannelFull',
				'id': channel_id,
				'about': spec['about'],
				'participants_count': spec['participants_count'],
				'linked_chat_id': spec['linked_chat_id'],
			},
			'chats': [self.chat(c) for c in spec['chats']],
			'users': [],
		})

	def page(self, channel_id, *, offset_id=0, min_id=0, page_size=2):
		'''
		Return one messages.ChannelMessages page: ids below `offset_id`
		(when set) and above `min_id`, newest first, at most `page_size`.
		`chats` holds the channel plus forwarded-from chats in message
		order; `users` holds message authors.
		'''
		history = self.histories.get(channel_id, [])
		selected = [
			m for m in history
			if (not offset_id or m['id'] < offset_id) and m['id'] > (min_id or 0)
		][:page_size]

		chat_ids, user_ids = [], []
		if selected:
			chat_ids.append(channel_id)
		for m in selected:
			fwd = m.get('fwd_from')
			if fwd and fwd.get('from_id'):
				peer = fwd['from_id']
				cid = peer.get('channel_id', peer.get('chat_id'))
				if cid is not None and cid not in chat_ids:
					chat_ids.append(cid)
			author = m.get('from_id')
			if author and author['_'] == 'PeerUser' and author['user_id'] not in user_ids:
				user_ids.append(author['user_id'])

		return FakeTL({
			'_': 'messages.ChannelMessages',
			'pts': 1,
			'count': len(history),
			'messages': copy.deepcopy(selected),
			'topics': [],
			'chats': [self.chat(c) for c in chat_ids],
			'users': [copy.deepcopy(self.users[u]) for u in user_ids],
			'inexact': False,
			'offset_id_offset': None,
		})


class FakeTelegramClient:
	'''
	Records every call and serves FixtureWorld data.

	- `authorized`: initial authorization state.
	- `password`: when set, the account has two-step verification and a
	  correct code raises SessionPasswordNeededError.
	- `script(kind, *outcomes)`: per-call outcomes for `kind` (a method
	  name or a request type name such as 'GetHistoryRequest'). An outcome
	  is None (normal), an exception (raised), or an async callable
	  (awaited; its non-None result replaces the response).
	'''
	def __init__(
			self,
			world=None,
			*,
			authorized=True,
			password=None,
			code=FIXTURE_CODE,
			page_size=2
		):
		self.world = world or FixtureWorld()
		self.authorized = authorized
		self.page_size = page_size
		self.flood_sleep_threshold = 60
		self._password = password
		self._code = code
		self._scripts = {}
		self.calls = []
		self.requests = []
		self.loop_ids = []
		self.connected = False
		self.connect_count = 0
		self.disconnect_count = 0

	def script(self, kind, *outcomes):
		self._scripts.setdefault(kind, []).extend(outcomes)
		return self

	async def _apply_script(self, kind):
		outcomes = self._scripts.get(kind)
		if not outcomes:
			return None
		outcome = outcomes.pop(0)
		if outcome is None:
			return None
		if isinstance(outcome, BaseException):
			raise outcome
		return await outcome()

	def _record(self, name, **details):
		self.calls.append((name, details))

	def call_names(self):
		return [name for name, _ in self.calls]

	# lifecycle
	async def connect(self):
		self.loop_ids.append(id(asyncio.get_running_loop()))
		self._record('connect')
		self.connect_count += 1
		await self._apply_script('connect')
		self.connected = True

	async def disconnect(self):
		self.loop_ids.append(id(asyncio.get_running_loop()))
		self._record('disconnect')
		self.disconnect_count += 1
		self.connected = False

	# auth
	async def is_user_authorized(self):
		self._record('is_user_authorized')
		await self._apply_script('is_user_authorized')
		return self.authorized

	async def send_code_request(self, phone, *, force_sms=False):
		self._record(
			'send_code_request',
			phone=phone,
			flood_sleep_threshold=self.flood_sleep_threshold
		)
		await self._apply_script('send_code_request')
		return FakeTL({'_': 'auth.SentCode', 'phone_code_hash': 'fake-hash'})

	async def sign_in(
			self,
			phone=None,
			code=None,
			*,
			password=None,
			bot_token=None,
			phone_code_hash=None
		):
		if password is not None:
			matched = password == self._password
			self._record(
				'sign_in_password',
				password_matched=matched,
				flood_sleep_threshold=self.flood_sleep_threshold
			)
			await self._apply_script('sign_in_password')
			if not matched:
				raise errors.PasswordHashInvalidError(request=None)
			self.authorized = True
			return FakeTL({'_': 'User', 'id': 1, 'is_self': True})

		self._record(
			'sign_in_code',
			phone=phone,
			code=code,
			flood_sleep_threshold=self.flood_sleep_threshold
		)
		await self._apply_script('sign_in_code')
		if code != self._code:
			raise errors.PhoneCodeInvalidError(request=None)
		if self._password is not None:
			raise errors.SessionPasswordNeededError(request=None)
		self.authorized = True
		return FakeTL({'_': 'User', 'id': 1, 'is_self': True})

	# data
	async def get_entity(self, target):
		self._record('get_entity', target=target)
		replaced = await self._apply_script('get_entity')
		if replaced is not None:
			return replaced
		return self.world.entity(target)

	async def __call__(self, request, ordered=False):
		kind = type(request).__name__
		if kind == 'GetFullChannelRequest':
			details = {'channel': request.channel}
		elif kind == 'GetHistoryRequest':
			details = {
				'peer': request.peer,
				'offset_id': request.offset_id,
				'offset_date': request.offset_date,
				'add_offset': request.add_offset,
				'limit': request.limit,
				'max_id': request.max_id,
				'min_id': request.min_id,
				'hash': request.hash,
			}
		else:
			raise AssertionError(f'unexpected request type {kind}')

		self.requests.append({'type': kind, **details})
		self._record(kind, **details)
		replaced = await self._apply_script(kind)
		if replaced is not None:
			return replaced

		if kind == 'GetFullChannelRequest':
			return self.world.full_channel(request.channel)
		return self.world.page(
			request.peer,
			offset_id=request.offset_id,
			min_id=request.min_id,
			page_size=self.page_size
		)


class ScriptedPrompt:
	'''
	Callable prompt that returns scripted answers in order. An answer that
	is an exception (instance or class) is raised instead. Records each
	prompt text and the SIGINT handler active during the call.
	'''
	def __init__(self, *answers):
		self.answers = list(answers)
		self.prompts = []
		self.sigint_handlers = []

	def __call__(self, prompt=''):
		self.prompts.append(prompt)
		self.sigint_handlers.append(signal.getsignal(signal.SIGINT))
		if not self.answers:
			raise AssertionError(f'unscripted prompt: {prompt!r}')
		answer = self.answers.pop(0)
		if isinstance(answer, BaseException) or (
			isinstance(answer, type) and issubclass(answer, BaseException)
		):
			raise answer
		return answer

	@property
	def count(self):
		return len(self.prompts)
