# -*- coding: utf-8 -*-
'''
The run's application RPCs: `Requests` tags each call with its method
class and admits it through the request policy; `MetadataCache` makes one
full-channel request per chat per run, shared by every channel that sees
the chat, and holds the chats' enrichment outcomes.
'''

# import modules
import asyncio

# import local submodules
from api import CHANNEL_ACCESS_ERRORS, full_channel_req, get_entity_attrs, get_posts
from request_policy import RequestPolicy
from storage.records import EnrichOutcome


# method classes of application RPCs
RESOLVE = 'resolve'
CHANNEL = 'channel'
HISTORY = 'history'
ENRICHMENT = 'enrichment'


class Requests:
	'''
	The run's application RPCs, each tagged with its method class and
	admitted through the request policy (a default one when none is given).
	'''
	def __init__(self, client, policy=None):
		self.client = client
		self.policy = policy or RequestPolicy()

	async def resolve(self, target):
		'''
		The entity for `target`, or False when it is missing/inaccessible.
		'''
		return await self.policy.call(
			RESOLVE, lambda: get_entity_attrs(self.client, target)
		)

	async def full_channel(self, chat_id, method_class):
		return await self.policy.call(
			method_class, lambda: full_channel_req(self.client, chat_id)
		)

	async def history(self, channel_id, **kwargs):
		return await self.policy.call(
			HISTORY, lambda: get_posts(self.client, channel_id, **kwargs)
		)


class MetadataCache:
	'''
	Per-run full-channel responses and enrichment outcomes by chat id.

	A request already in flight is shared by every caller (single flight).
	Successful responses and permanent access errors are kept for the run;
	any other failure is raised to the callers waiting for it and not
	kept, so the next caller asks again.
	'''
	def __init__(self, requests):
		self.requests = requests
		self.outcomes = {}
		self._full = {}
		self._full_tasks = {}
		self._enrich_tasks = {}
		self._tasks = []

	def _task(self, coroutine):
		task = asyncio.ensure_future(coroutine)
		self._tasks.append(task)
		return task

	async def full_channel(self, chat_id, method_class):
		'''
		The chat's GetFullChannelRequest response, requested at most once
		per run while it succeeds.
		'''
		if chat_id in self._full:
			cached = self._full[chat_id]
			if isinstance(cached, BaseException):
				raise cached.with_traceback(None)
			return cached

		task = self._full_tasks.get(chat_id)
		if task is None:
			task = self._task(self._fetch_full(chat_id, method_class))
			self._full_tasks[chat_id] = task
		return await asyncio.shield(task)

	async def _fetch_full(self, chat_id, method_class):
		try:
			response = await self.requests.full_channel(chat_id, method_class)
		except CHANNEL_ACCESS_ERRORS as exc:
			self._full[chat_id] = exc
			raise
		else:
			self._full[chat_id] = response
			return response
		finally:
			self._full_tasks.pop(chat_id, None)

	async def participants_count(self, chat_id):
		'''
		Participants count for a chat (its full-channel response).
		'''
		response = await self.full_channel(chat_id, ENRICHMENT)
		return response.full_chat.participants_count

	async def enrich(self, chat_id):
		'''
		Make sure `outcomes[chat_id]` holds the chat's EnrichOutcome.
		'''
		if chat_id in self.outcomes:
			return

		task = self._enrich_tasks.get(chat_id)
		if task is None:
			task = self._task(self._enrich(chat_id))
			self._enrich_tasks[chat_id] = task
		await asyncio.shield(task)

	async def _enrich(self, chat_id):
		try:
			self.outcomes[chat_id] = await enrichment_outcome(self, chat_id)
		finally:
			self._enrich_tasks.pop(chat_id, None)

	async def close(self):
		'''
		Cancel and await every request task still running.
		'''
		pending = [t for t in self._tasks if not t.done()]
		for task in pending:
			task.cancel()
		await asyncio.gather(*self._tasks, return_exceptions=True)
		self._tasks.clear()


async def enrichment_outcome(cache, chat_id):
	'''
	Enrich a discovered chat: its full-channel response's public chats,
	with participants_count filled. Each other chat listed costs one more
	full-channel request (shared through the cache). A missing or
	inaccessible chat (requested or listed) ends the enrichment as
	'not_found', keeping the linked chats seen before it.
	'''
	outcome = EnrichOutcome()
	try:
		response = await cache.full_channel(chat_id, ENRICHMENT)
	except CHANNEL_ACCESS_ERRORS:
		outcome.error = 'not_found'
		return outcome

	channel_request = response.to_dict()
	try:
		full_chat = channel_request['full_chat']
		collected_chats = channel_request['chats']

		for ch in collected_chats:
			if ch['id'] == full_chat['id']:
				ch['participants_count'] = full_chat['participants_count']
				continue

			try:
				ch['participants_count'] = await cache.participants_count(ch['id'])
			except TypeError:
				ch['participants_count'] = 0
			except CHANNEL_ACCESS_ERRORS:
				outcome.error = 'not_found'
				return outcome

			if ch['username'] != None:
				outcome.linked.append((ch['id'], ch['username']))

		outcome.rows = [i for i in collected_chats if i['username'] != None]
	except KeyError as exc:
		outcome.error = f'missing field {exc}'

	return outcome
