# -*- coding: utf-8 -*-
'''
Asynchronous channel collection.

`run_collection` owns one client for the whole run. It creates the client
through an injected factory inside the running event loop, connects,
authorizes once, collects the targets with at most `concurrency` channel
workers (pages of one channel stay sequential), finalizes the aggregate
files, and disconnects exactly once after success, failure, or
cancellation.

Boundaries:
	- network: `Requests` (application RPCs by method class, all admitted
	  through one `request_policy.RequestPolicy`) and `MetadataCache` (one
	  full-channel request per chat per run, shared by every channel that
	  sees the chat);
	- collection: `collect_channel` returns a `storage.ChannelResult` and
	  writes nothing;
	- files: `storage.OutputWriter` alone writes channel JSON, aggregates,
	  the lock, and collection-status.json.

Importing this module is inert: no event loop, argv parsing, config reads,
connections, or writes.
'''

# import modules
import asyncio
import functools
import json
import os
import sys

from dataclasses import dataclass, field
from datetime import datetime, timezone

# import local submodules
from telethon import errors

from api import CHANNEL_ACCESS_ERRORS, full_channel_req, get_entity_attrs, get_posts
from request_policy import AccountStopped, RequestFailed, RequestPolicy
from storage.files import history_path, merge_histories, newest_id, read_history
from storage.records import COMPLETE, INTERRUPTED, PARTIAL, ChannelResult, \
	EnrichOutcome, HistoryUnreadable, StorageError
from storage.status import StatusFile
from storage.writer import OutputLock, OutputWriter, check_unlocked
from utils import JSONEncoder, create_dirs


# method classes of application RPCs
RESOLVE = 'resolve'
CHANNEL = 'channel'
HISTORY = 'history'
ENRICHMENT = 'enrichment'

NOT_FOUND_REASON = 'not found or not accessible'


class PaginationStalled(Exception):
	'''
	History pagination stopped making progress (offset did not decrease).
	'''


class TargetUnavailable(Exception):
	'''
	A requested target does not exist or is not accessible.
	'''


class DuplicateTarget(Exception):
	'''
	A target resolved to the same channel as an earlier target.
	'''
	def __init__(self, alias_of):
		super().__init__(alias_of)
		self.alias_of = alias_of


class SessionRejected(Exception):
	'''
	Telegram refused the session's authorization during collection.
	Ends the run.
	'''


# authorization/session errors that end the run
SESSION_ERRORS = (errors.UnauthorizedError, errors.AuthKeyError)


@dataclass
class CollectionSummary:
	'''
	Outcome of one run. `failed` holds (target, reason) pairs; `cancelled`
	lists targets not finished when the run stopped; `collected_in_full`
	lists --update targets that had no saved history.
	'''
	requested: list
	output_folder: str
	succeeded: list = field(default_factory=list)
	failed: list = field(default_factory=list)
	cancelled: list = field(default_factory=list)
	duplicates: list = field(default_factory=list)
	collected_in_full: list = field(default_factory=list)
	stale_histories: list = field(default_factory=list)
	stop_reason: str | None = None
	concurrency: int = 1
	requests: dict = field(default_factory=dict)
	peak_in_flight: int = 0
	peak_queued_results: int = 0


'''

Requests

'''

def to_json_safe(data):
	'''
	Round-trip through JSONEncoder: datetimes and bytes become str.
	'''
	return json.loads(JSONEncoder().encode(data))


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


'''

History

'''

@dataclass(frozen=True)
class HistoryWindow:
	'''
	Which messages a history collection keeps. The default keeps all.

	min_id / max_id -> exclusive message-id bounds (0 = none)
	since -> keep messages dated at or after it (aware datetime)
	until -> keep messages dated before it (aware datetime)
	max_messages -> keep at most this many, newest first

	The server receives min_id, max_id, and (first page only) until as
	offset_date; every bound is also applied to the returned messages.
	'''
	min_id: int = 0
	max_id: int = 0
	since: datetime | None = None
	until: datetime | None = None
	max_messages: int | None = None

	@property
	def is_filtered(self):
		return self != HistoryWindow()

	def keeps(self, message):
		if message['id'] <= self.min_id:
			return False
		if self.max_id and message['id'] >= self.max_id:
			return False
		if self.since is None and self.until is None:
			return True

		date = message_date(message)
		if self.since is not None and date < self.since:
			return False
		return self.until is None or date < self.until

	def passed(self, page_messages):
		'''
		True when a page reached messages older than the window: the next
		page (lower ids) can hold nothing more. For `since` this assumes
		dates follow id order.
		'''
		if self.min_id and any(m['id'] <= self.min_id for m in page_messages):
			return True
		return self.since is not None and any(
			message_date(m) < self.since for m in page_messages
		)

	def page_limit(self, kept):
		if self.max_messages is None:
			return 100
		return min(100, self.max_messages - kept)

	def describe(self):
		'''
		The active bounds, for the run header; '' when unfiltered.
		'''
		parts = []
		if self.min_id:
			parts.append(f'id > {self.min_id}')
		if self.max_id:
			parts.append(f'id < {self.max_id}')
		if self.since is not None:
			parts.append(f'date >= {self.since:%Y-%m-%d %H:%M:%S} UTC')
		if self.until is not None:
			parts.append(f'date < {self.until:%Y-%m-%d %H:%M:%S} UTC')
		if self.max_messages is not None:
			parts.append(f'newest {self.max_messages} messages')
		return ', '.join(parts)

	def as_record(self):
		'''
		The bounds as JSON values (dates in ISO 8601), for the sidecar.
		'''
		return {
			'min_id': self.min_id,
			'max_id': self.max_id,
			'since': self.since.isoformat() if self.since else None,
			'until': self.until.isoformat() if self.until else None,
			'max_messages': self.max_messages
		}


def message_date(message):
	'''
	A message's date as an aware datetime (UTC when the value is naive).
	'''
	date = message['date']
	if isinstance(date, str):
		date = datetime.fromisoformat(date)
	return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


async def fetch_history(get_page, *, window, on_page):
	'''
	Page through a channel's history, newest first, 100 messages per
	request, until an empty page. `get_page(**arguments)` performs one
	GetHistoryRequest. Returns the first page's payload with messages,
	chats, and users from later pages appended (deduplicated by id).

	`window` selects the messages kept. Pagination stops early once the
	cap is reached (no further request) or a page reaches below `since` /
	`min_id`. Pages are requested and their chats counted whole, so the
	boundary page's chats and users may belong to trimmed messages.

	`on_page(chats)` is awaited for every non-empty page after the first;
	the first page's chats are stored but not counted.
	Raises PaginationStalled if the offset stops decreasing.
	'''
	posts = await get_page(
		min_id=window.min_id,
		max_id=window.max_id,
		offset_date=window.until or 0,
		limit=window.page_limit(0)
	)
	data = posts.to_dict()
	page_messages = data['messages']
	if not page_messages:
		return data

	seen = {
		key: {i['id'] for i in data[key]}
		for key in ('messages', 'chats', 'users')
	}
	data['messages'] = [m for m in page_messages if window.keeps(m)]
	if window.max_messages is not None:
		del data['messages'][window.max_messages:]
	offset_id = min(seen['messages'])

	while True:
		if window.passed(page_messages) or (
				window.max_messages is not None
				and len(data['messages']) >= window.max_messages):
			return data

		posts = await get_page(
			min_id=window.min_id,
			offset_id=offset_id,
			max_id=window.max_id,
			limit=window.page_limit(len(data['messages']))
		)
		page = posts.to_dict()
		page_messages = page['messages']
		if not page_messages:
			return data

		await on_page(page['chats'])

		for key in ('chats', 'users'):
			for item in page[key]:
				if item['id'] not in seen[key]:
					seen[key].add(item['id'])
					data[key].append(item)

		for item in page_messages:
			if item['id'] in seen['messages']:
				continue
			seen['messages'].add(item['id'])
			if window.keeps(item) and (
					window.max_messages is None
					or len(data['messages']) < window.max_messages):
				data['messages'].append(item)

		next_offset = min(i['id'] for i in page_messages)
		if next_offset >= offset_id:
			raise PaginationStalled(
				f'history offset did not advance below message {offset_id}'
			)
		offset_id = next_offset


'''

Channels

'''

def public_chats(chats):
	'''
	(id, username) of the chats in a response that count: basic groups and
	forbidden chats have no username field.
	'''
	return [
		(c['id'], c['username']) for c in chats
		if 'id' in c and 'username' in c and c['username'] != None
	]


async def collect_channel(
		requests,
		cache,
		index,
		target,
		*,
		output_folder,
		metadata_only=False,
		window=None,
		update=False,
		after_resolve=None
	):
	'''
	Collect one target and return its ChannelResult; nothing is written.

	Every chat seen directly is enriched (through the shared cache) the
	first time this channel sees it, so the writer can replay observations
	in input order whatever the completion order.

	With `update`, an existing <target>_messages.json sets min_id to its
	newest id (checked before any request) and the fetched posts are merged
	into it, read again after the fetch; without one the history is
	collected in full. `after_resolve(entity)` is awaited right after entity
	resolution (entity is False when missing) and may raise to stop.
	Raises TargetUnavailable, PaginationStalled, HistoryUnreadable, or
	RequestFailed for a target that fails on its own.
	'''
	window = window or HistoryWindow()
	result = ChannelResult(index=index, target=target)

	path = history_path(output_folder, target)
	has_history = False
	if update and not metadata_only:
		if os.path.exists(path):
			has_history = True
			window = HistoryWindow(min_id=newest_id(read_history(path)))
		result.min_id = window.min_id

	print('')
	print(f'> Collecting data from Telegram Channel -> {target}')
	print('> ...')
	print('')

	entity_attrs = await requests.resolve(target)
	if after_resolve is not None:
		await after_resolve(entity_attrs)
	if not entity_attrs:
		raise TargetUnavailable(target)

	channel_id = entity_attrs.id
	try:
		channel_request = await cache.full_channel(channel_id, CHANNEL)
	except CHANNEL_ACCESS_ERRORS:
		raise TargetUnavailable(target) from None

	result.channel_id = channel_id
	result.channel_data = to_json_safe(channel_request.to_dict())

	enriched = set()

	async def observe(req_type, chats):
		found = public_chats(chats)
		result.observations.append((req_type, found))
		for chat_id, _ in found:
			if chat_id not in enriched:
				enriched.add(chat_id)
				await cache.enrich(chat_id)

	await observe('channel_request', result.channel_data['chats'])
	if metadata_only:
		return result

	async def get_page(**arguments):
		return await requests.history(channel_id, **arguments)

	async def on_page(chats):
		await observe('from_messages', chats)

	data = to_json_safe(
		await fetch_history(get_page, window=window, on_page=on_page)
	)

	earlier = None
	if not update:
		result.history, result.history_mode = data, 'replaced'
	elif not has_history:
		result.history, result.history_mode = data, 'created'
	else:
		earlier = read_history(path)
		result.history = merge_histories(earlier, data)
		result.history_mode = 'unchanged' if result.history is None else 'merged'

	result.messages = len((result.history or earlier)['messages'])
	return result


'''

Orchestration

'''

def run_parameters(metadata_only, window, update, controls=None):
	'''
	The run's collection parameters for the sidecar (no credentials).
	'''
	if metadata_only:
		mode = 'metadata_only'
	elif update:
		mode = 'update'
	elif window.is_filtered:
		mode = 'window'
	else:
		mode = 'full'

	parameters = {
		'mode': mode,
		'metadata_only': metadata_only,
		'window': window.as_record() if mode == 'window' else None,
		'window_description': window.describe() if mode == 'window' else None
	}
	parameters.update(controls or {})
	return parameters


async def run_collection(
		targets,
		output_folder,
		*,
		client_factory,
		authorize,
		metadata_only=False,
		min_id=0,
		window=None,
		update=False,
		concurrency=1,
		request_interval=0.25,
		max_flood_wait=60,
		max_retries=3,
		requested_concurrency=None,
		targets_given=None,
		policy=None
	):
	'''
	Collect `targets` (normalized, without duplicates) with one client.

	client_factory -> zero-argument callable returning an unconnected client;
		called inside the running loop
	authorize -> coroutine function awaited once with the connected client,
		before any worker starts (api.auth.ensure_authorized in production)
	window -> HistoryWindow selecting the messages kept (default: all
		above min_id)
	update -> merge new posts into each channel's saved history
	concurrency -> channel workers, capped at the number of targets; also
		the bound on application RPCs in flight
	request_interval / max_flood_wait / max_retries -> the request policy
		(see request_policy.py); `policy` replaces it (tests)
	requested_concurrency / targets_given -> recorded in the sidecar only

	Missing/inaccessible targets, stalled pagination, unreadable saved
	histories (--update), and requests that keep failing transiently fail
	only that target; a target resolving to an earlier target's channel
	is skipped as a duplicate. An account stop (flood wait), a rejected
	session, a storage error, any other exception, or cancellation ends the
	run: completed channels stay written, aggregates are finalized from
	them, and collection-status.json says "interrupted". The output folder
	is locked for the run, and the client is disconnected exactly once
	after every worker has finished.
	'''
	window = window or HistoryWindow(min_id=min_id)
	summary = CollectionSummary(requested=list(targets), output_folder=output_folder)
	summary.concurrency = max(1, min(concurrency, len(summary.requested)))

	check_unlocked(output_folder)
	client = client_factory()
	try:
		await client.connect()
		await authorize(client)

		try:
			create_dirs(output_folder)
		except OSError as exc:
			raise StorageError(f'cannot create {output_folder}: {exc}') from exc

		policy = policy or RequestPolicy(
			concurrency=summary.concurrency,
			interval=request_interval,
			max_flood_wait=max_flood_wait,
			max_retries=max_retries
		)
		parameters = run_parameters(metadata_only, window, update, {
			'targets_given': len(summary.requested) if targets_given is None else targets_given,
			'concurrency': {
				'requested': requested_concurrency or concurrency,
				'effective': summary.concurrency
			},
			'request_interval': policy.interval,
			'max_flood_wait': policy.max_flood_wait,
			'max_retries': policy.max_retries
		})
		with OutputLock(output_folder):
			await _collect_locked(
				client,
				summary,
				policy,
				parameters,
				metadata_only=metadata_only,
				window=window,
				update=update
			)
	finally:
		await client.disconnect()

	return summary


async def _collect_locked(client, summary, policy, parameters, *, metadata_only, window, update):
	targets = summary.requested
	status = StatusFile(summary.output_folder, targets, parameters)
	status.write()

	writer = OutputWriter(summary.output_folder)
	cache = MetadataCache(Requests(client, policy))
	queue = asyncio.Queue(maxsize=summary.concurrency)
	resolved = {}
	resolved_events = [asyncio.Event() for _ in targets]

	async def after_resolve(index, entity):
		'''
		Record the resolved channel; skip the target when an earlier target
		(lower index) resolved to the same channel. Earlier targets started
		first and record their resolution before collecting, so this waits
		at most for their resolution.
		'''
		resolved[index] = entity.id if entity else None
		resolved_events[index].set()
		if not entity:
			return
		for earlier in range(index):
			await resolved_events[earlier].wait()
			if resolved.get(earlier) == entity.id:
				raise DuplicateTarget(targets[earlier])

	async def process(index):
		target = targets[index]
		try:
			result = await collect_channel(
				cache.requests,
				cache,
				index,
				target,
				output_folder=summary.output_folder,
				metadata_only=metadata_only,
				window=window,
				update=update,
				after_resolve=functools.partial(after_resolve, index)
			)
			outcome = ('result', result)
		except TargetUnavailable:
			outcome = ('not_found', NOT_FOUND_REASON)
		except DuplicateTarget as exc:
			outcome = ('duplicate', exc.alias_of)
		except (PaginationStalled, HistoryUnreadable, RequestFailed) as exc:
			print(f'> {target}: {exc}; messages not saved.', file=sys.stderr)
			outcome = ('failed', str(exc))
		except SESSION_ERRORS as exc:
			raise SessionRejected(
				f'Telegram rejected the session ({type(exc).__name__}); log in again'
			) from None
		finally:
			resolved_events[index].set()

		await queue.put((index, outcome))
		summary.peak_queued_results = max(summary.peak_queued_results, queue.qsize())

	def record(index, kind, payload):
		'''
		Writer side: commit a result or record a failure. Only this
		function touches output files and target statuses.
		'''
		entry = status.target(index)
		target = targets[index]
		if kind == 'result':
			try:
				writer.commit(payload)
			except StorageError as exc:
				entry.update(status='failed', reason=f'storage error: {exc}')
				raise
			print(f'> Saved {target}')
			entry.update(
				status='succeeded',
				channel_id=payload.channel_id,
				history=payload.history_mode,
				messages=payload.messages
			)
			if payload.min_id is not None:
				entry['min_id'] = payload.min_id
			if payload.history_mode == 'not_collected' and os.path.isfile(
					history_path(summary.output_folder, target)):
				entry['stale_history'] = True
		elif kind == 'duplicate':
			entry.update(status='duplicate', alias_of=payload)
		else:
			if kind == 'not_found':
				writer.record_not_found(index, target)
			entry.update(status='failed', reason=payload)

	async def write_results():
		while True:
			item = await queue.get()
			if item is None:
				return
			record(item[0], *item[1])

	order = iter(range(len(targets)))

	async def worker():
		for index in order:
			await process(index)

	workers = [asyncio.create_task(worker()) for _ in range(summary.concurrency if targets else 0)]
	writer_task = asyncio.create_task(write_results())
	try:
		await _supervise(workers, writer_task, queue)
	except BaseException as exc:
		summary.stop_reason = stop_reason_for(exc)
		await _cancel_all(workers + [writer_task])
		await _close_quietly(cache)
		if not isinstance(exc, StorageError):
			# results already collected and waiting for the writer
			_drain(queue, record)
		_finish(writer, cache, status, summary, policy, failing=True)
		if isinstance(exc, (AccountStopped, SessionRejected)):
			# a planned stop: the summary carries stop_reason
			return
		raise

	await cache.close()
	_finish(writer, cache, status, summary, policy, failing=False)


async def _supervise(workers, writer_task, queue):
	'''
	Wait for every worker, then let the writer finish the queue. A failing
	worker or writer raises at once (the caller cancels the rest).
	'''
	gathered = asyncio.gather(*workers)
	# a cancelled run leaves gathered with CancelledError: mark it retrieved
	gathered.add_done_callback(lambda f: f.cancelled() or f.exception())
	pending = {gathered, writer_task}
	while gathered in pending:
		done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
		if writer_task in done:
			writer_task.result()
			raise RuntimeError('result writer stopped before the workers')
	gathered.result()
	await queue.put(None)
	await writer_task


async def _cancel_all(tasks):
	for task in tasks:
		task.cancel()
	await asyncio.gather(*tasks, return_exceptions=True)


def _drain(queue, record):
	while not queue.empty():
		item = queue.get_nowait()
		if item is None:
			continue
		try:
			record(item[0], *item[1])
		except StorageError:
			return


def stop_reason_for(exc):
	if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
		return 'cancelled'
	if isinstance(exc, AccountStopped):
		return f'account stopped: {exc}'
	if isinstance(exc, SessionRejected):
		return f'session rejected: {exc}'
	if isinstance(exc, StorageError):
		return f'storage error: {exc}'
	return f'{type(exc).__name__}: {exc}'


async def _close_quietly(cache):
	try:
		await cache.close()
	except BaseException:
		pass


def _finish(writer, cache, status, summary, policy, *, failing):
	'''
	Finalize aggregates from the committed channels, fill the summary from
	the target statuses (input order), and close the sidecar. A storage
	error while finishing is raised unless the run is already failing with
	another error.
	'''
	for index, entry in enumerate(status.data['targets']):
		target = summary.requested[index]
		if entry['status'] == 'pending':
			entry['status'] = 'cancelled'
			summary.cancelled.append(target)
		elif entry['status'] == 'succeeded':
			summary.succeeded.append(target)
			if entry.get('history') == 'created':
				summary.collected_in_full.append(target)
			if entry.get('stale_history'):
				summary.stale_histories.append(target)
		elif entry['status'] == 'duplicate':
			summary.duplicates.append((target, entry['alias_of']))
		else:
			summary.failed.append((target, entry.get('reason')))

	summary.requests = policy.stats_record()
	summary.peak_in_flight = policy.peak_active

	error = None
	try:
		writer.finalize(cache.outcomes)
	except StorageError as exc:
		error = exc
		summary.stop_reason = summary.stop_reason or f'storage error: {exc}'

	if summary.stop_reason is not None:
		state = INTERRUPTED
	elif summary.failed:
		state = PARTIAL
	else:
		state = COMPLETE

	try:
		status.finish(
			state,
			summary={
				'given': status.data['parameters'].get('targets_given'),
				'requested': len(summary.requested),
				'succeeded': len(summary.succeeded),
				'failed': len(summary.failed),
				'duplicates': len(summary.duplicates),
				'cancelled': len(summary.cancelled)
			},
			stop_reason=summary.stop_reason,
			requests=summary.requests
		)
	except StorageError as exc:
		error = error or exc

	if error is not None and not failing:
		raise error
