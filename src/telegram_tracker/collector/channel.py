# -*- coding: utf-8 -*-
'''
One channel's collection: resolve the target, request its full channel,
enrich every chat it shows, and page through its history (or merge new
posts into the saved history with --update). The result is a
`storage.records.ChannelResult`; nothing is written here.
'''

# import modules
import json

# import local submodules
from telegram_tracker.api import CHANNEL_ACCESS_ERRORS
from telegram_tracker.collector.history import HistoryWindow, fetch_history
from telegram_tracker.collector.outcomes import TargetUnavailable
from telegram_tracker.collector.rpc import CHANNEL
from telegram_tracker.storage.histories import FileHistories, ReplaceNotConfirmed
from telegram_tracker.storage.records import ChannelResult
from telegram_tracker.utils import JSONEncoder


def to_json_safe(data):
	'''
	Round-trip through JSONEncoder: datetimes and bytes become str.
	'''
	return json.loads(JSONEncoder().encode(data))


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
		after_resolve=None,
		profile=None,
		histories=None,
		confirmed=None
	):
	'''
	Collect one target and return its ChannelResult; nothing is written.

	Every chat seen directly is enriched (through the shared cache) the
	first time this channel sees it, so the writer can replay observations
	in input order whatever the completion order.

	With `update`, the saved history (looked up through `histories`, see
	storage.histories; default: the files in `output_folder`) sets min_id
	to its newest id before the first history request, and the fetched
	posts are merged into it; without one the history is collected in
	full. The file source checks before any request and reads the history
	again after the fetch. With a `profile` (RunProfile), --update into a
	history another profile collected raises HistoryRefused before any
	history request (see storage.provenance). With `confirmed` (a window
	run: the targets whose replacement was confirmed by name before
	connecting), a target outside it whose channel has a history stored
	under its ID raises ReplaceNotConfirmed before any history request.
	`after_resolve(entity)` is awaited
	right after entity resolution (entity is False when missing) and may
	raise to stop. Raises TargetUnavailable, PaginationStalled,
	HistoryUnreadable, HistoryRefused, or RequestFailed for a target that
	fails on its own.
	'''
	window = window or HistoryWindow()
	result = ChannelResult(index=index, target=target)
	if histories is None:
		histories = FileHistories(output_folder, profile)
	# The output lock is held now; login may have outlasted the earlier scan.
	if confirmed is not None and target not in confirmed and histories.stored_file(target):
		raise ReplaceNotConfirmed(appeared=True)
	updating = update and not metadata_only

	saved = None
	if updating:
		saved = histories.before_requests(target)

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
	if updating:
		saved = histories.after_resolve(target, channel_id, saved)
		if saved is not None:
			window = HistoryWindow(min_id=saved.newest_id)
		result.min_id = window.min_id
	if confirmed is not None and target not in confirmed and histories.stored(channel_id):
		raise ReplaceNotConfirmed(channel_id)

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

	if not update:
		result.history, result.history_mode = data, 'replaced'
	elif saved is None:
		result.history, result.history_mode = data, 'created'
	else:
		result.history, result.history_mode, result.messages = histories.merge(target, saved, data)
		if result.history_mode == 'merged':
			result.fetched = data
		return result

	result.messages = len(data['messages'])
	return result
