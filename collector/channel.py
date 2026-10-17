# -*- coding: utf-8 -*-
'''
One channel's collection: resolve the target, request its full channel,
enrich every chat it shows, and page through its history (or merge new
posts into the saved history with --update). The result is a
`storage.records.ChannelResult`; nothing is written here.
'''

# import modules
import json
import os

# import local submodules
from api import CHANNEL_ACCESS_ERRORS
from collector.history import HistoryWindow, fetch_history
from collector.outcomes import TargetUnavailable
from collector.rpc import CHANNEL
from storage.files import history_path, merge_histories, newest_id, read_history
from storage.records import ChannelResult
from utils import JSONEncoder


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
