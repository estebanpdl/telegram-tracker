# -*- coding: utf-8 -*-
'''
Asynchronous, sequential channel collection.

`run_collection` owns one client for the whole run. It creates the client
through an injected factory inside the running event loop, connects,
authorizes, collects each target in order, finalizes the aggregate files,
and disconnects exactly once after success, failure, or cancellation.

Boundaries:
	- network: `api` request helpers awaited on the injected client;
	- counting: `observe_chat` / `observe_linked_chat` are pure updates of
	  the run's counter dict;
	- files: channel JSON, chats.txt, collected_chats.csv, counter.csv, and
	  the exception logs, written under the output folder.

Importing this module is inert: no event loop, argv parsing, config reads,
connections, or writes.
'''

# import modules
import asyncio
import json
import os
import sys

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

# import local submodules
from api import full_channel_req, get_entity_attrs, get_posts
from utils import JSONEncoder, chats_dataset_columns, create_dirs


# pause between resolved channels of a batch (baseline behavior)
BATCH_PAUSE_SECONDS = 2

CHANNELS_NOT_FOUND = '_exceptions-channels-not-found.txt'
USERS_NOT_FOUND = '_exceptions-users-not-found.txt'

COUNTER_COLUMNS = [
	'id',
	'username',
	'counter',
	'from_messages',
	'channel_request',
	'channel_req_targeted_by',
	'source'
]

# columns build-datasets.py adds; not part of collection output
DATASET_STAT_COLUMNS = (
	'collected_actions',
	'collected_posts',
	'replies',
	'other_actions',
	'number_views',
	'forwards',
	'replies_received'
)


class PaginationStalled(Exception):
	'''
	History pagination stopped making progress (offset did not decrease).
	'''


@dataclass
class CollectionSummary:
	'''
	Outcome of one run. `failed` holds (target, reason) pairs.
	'''
	requested: list
	output_folder: str
	succeeded: list = field(default_factory=list)
	failed: list = field(default_factory=list)


'''

Counting (pure)

'''

def new_counter_entry(username, source, req_type, targeted_by='self'):
	'''
	First observation of a chat.
	'''
	return {
		'username': username,
		'counter': 1,
		'from_messages': 1 if req_type == 'from_messages' else 0,
		'channel_request': 1 if req_type == 'channel_request' else 0,
		'channel_req_targeted_by': {
			'channels': [targeted_by]
		},
		'source': [source]
	}


def observe_chat(counter, chat_id, username, source, req_type):
	'''
	Count a chat seen directly in a channel response (`channel_request`) or
	in a history page (`from_messages`). Returns True for a new chat, which
	the caller then enriches.
	'''
	entry = counter.get(chat_id)
	if entry is None:
		counter[chat_id] = new_counter_entry(username, source, req_type)
		return True

	entry['counter'] += 1
	entry[req_type] += 1
	if source not in entry['source']:
		entry['source'].append(source)

	return False


def observe_linked_chat(counter, chat_id, username, source, parent_username):
	'''
	Count a chat listed in a newly observed chat's full-channel response.
	Baseline policy: a repeat observation records the parent in
	`channel_req_targeted_by` but does not extend `source`.
	'''
	entry = counter.get(chat_id)
	if entry is None:
		counter[chat_id] = new_counter_entry(
			username, source, 'channel_request', targeted_by=parent_username
		)
		return

	entry['counter'] += 1
	entry['channel_request'] += 1
	entry['channel_req_targeted_by']['channels'].append(parent_username)


'''

Files

'''

def to_json_safe(data):
	'''
	Round-trip through JSONEncoder: datetimes and bytes become str.
	'''
	return json.loads(JSONEncoder().encode(data))


def write_json(file_path, data):
	'''
	Write compact UTF-8 JSON (baseline format).
	'''
	obj = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
	with open(file_path, mode='w', encoding='utf-8') as writer:
		writer.write(obj)


def append_line(file_path, text):
	with open(file_path, mode='a', encoding='utf-8') as writer:
		writer.write(f'{text}\n')


def _has_no_rows(csv_path):
	'''
	True when a CSV holds at most a header line.
	'''
	with open(csv_path, encoding='utf-8') as reader:
		reader.readline()
		return reader.readline() == ''


def append_collected_chats(output_folder, metadata):
	'''
	Append enriched chat rows to collected_chats.csv. A header-only file
	(from an earlier empty run) is replaced so the header matches the rows.
	'''
	if not metadata:
		return

	csv_path = f'{output_folder}/collected_chats.csv'
	exists = os.path.isfile(csv_path)
	replace = exists and _has_no_rows(csv_path)
	pd.DataFrame(metadata).to_csv(
		csv_path,
		encoding='utf-8',
		mode='w' if replace else 'a',
		index=False,
		header=replace or not exists
	)


def empty_collected_chats_columns():
	'''
	Header for a collected_chats.csv with no rows: the selected chat
	columns plus the merged counter columns.
	'''
	chat_columns = [
		c for c in chats_dataset_columns() if c not in DATASET_STAT_COLUMNS
	]
	return chat_columns + COUNTER_COLUMNS[2:]


def finalize_outputs(output_folder, counter):
	'''
	Deduplicate chats.txt (first occurrence order), write counter.csv for
	this run, and merge it into collected_chats.csv. Runs with no data get
	header-correct empty files.
	'''
	chats_path = f'{output_folder}/chats.txt'
	with open(chats_path, mode='r', encoding='utf-8') as reader:
		collected_chats = list(dict.fromkeys(line.rstrip() for line in reader))
	with open(chats_path, mode='w', encoding='utf-8') as writer:
		for c in collected_chats:
			writer.write(f'{c}\n')

	counter_df = pd.DataFrame.from_dict(
		counter,
		orient='index'
	).reset_index().rename(
		columns={
			'index': 'id'
		}
	).reindex(columns=COUNTER_COLUMNS)

	counter_df.to_csv(
		f'{output_folder}/counter.csv',
		encoding='utf-8',
		index=False
	)

	csv_path = f'{output_folder}/collected_chats.csv'
	if not os.path.isfile(csv_path) or _has_no_rows(csv_path):
		pd.DataFrame(columns=empty_collected_chats_columns()).to_csv(
			csv_path,
			encoding='utf-8',
			index=False
		)
		return

	df = pd.read_csv(csv_path, encoding='utf-8')
	counter_df = counter_df.drop(columns='username').astype(
		{'id': df['id'].dtype}
	)
	df = df.merge(counter_df, how='left', on='id')
	df.to_csv(
		csv_path,
		index=False,
		encoding='utf-8'
	)


'''

Network enrichment

'''

async def participants_count(client, channel_id):
	'''
	Participants count for a chat (one GetFullChannelRequest).
	'''
	channel_request = await full_channel_req(client, channel_id)
	return channel_request.full_chat.participants_count


async def enrich_chat(client, chat_id, username, *, source, counter, chats_file):
	'''
	Fetch a newly observed chat's full-channel response and return its
	public chats with participants_count filled. Each linked chat costs one
	more request and is counted as a channel_request observation targeted
	by `username`.
	'''
	channel_request = (await full_channel_req(client, chat_id)).to_dict()
	full_chat = channel_request['full_chat']
	collected_chats = channel_request['chats']

	for ch in collected_chats:
		if ch['id'] == full_chat['id']:
			ch['participants_count'] = full_chat['participants_count']
			continue

		try:
			ch['participants_count'] = await participants_count(client, ch['id'])
		except TypeError:
			ch['participants_count'] = 0

		if ch['username'] != None:
			chats_file.write(f'{ch["id"]}\n')
			observe_linked_chat(counter, ch['id'], ch['username'], source, username)

	return [i for i in collected_chats if i['username'] != None]


async def record_collected_chats(
		client,
		chats_object,
		*,
		source,
		req_type,
		counter,
		chats_file,
		output_folder
	):
	'''
	Count the public chats in a response, enrich the new ones, and append
	their metadata to collected_chats.csv.

	chats_object -> chats from an API response (Vector<Chat> as dicts)
	source -> channel requested by the user
	req_type -> 'channel_request' or 'from_messages'
	counter -> the run's counter dict, updated in place
	chats_file -> open chats.txt handle
	'''
	metadata = []
	for c in chats_object:
		# basic groups and forbidden chats have no username field
		if 'id' not in c or 'username' not in c or c['username'] == None:
			continue

		id_ = c['id']
		chats_file.write(f'{id_}\n')
		if not observe_chat(counter, id_, c['username'], source, req_type):
			continue

		try:
			metadata.extend(
				await enrich_chat(
					client,
					id_,
					c['username'],
					source=source,
					counter=counter,
					chats_file=chats_file
				)
			)
		except ValueError:
			append_line(f'{output_folder}/{USERS_NOT_FOUND}', f'ID - {id_}')
		except KeyError as exc:
			print(f'> Skipped metadata for chat {id_}: missing field {exc}', file=sys.stderr)

	append_collected_chats(output_folder, metadata)


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


def message_date(message):
	'''
	A message's date as an aware datetime (UTC when the value is naive).
	'''
	date = message['date']
	if isinstance(date, str):
		date = datetime.fromisoformat(date)
	return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


async def fetch_history(client, channel_id, *, min_id=0, window=None, on_page):
	'''
	Page through a channel's history, newest first, 100 messages per
	request, until an empty page. Returns the first page's payload with
	messages, chats, and users from later pages appended (deduplicated by
	id).

	`window` (default: everything above `min_id`) selects the messages
	kept. Pagination stops early once the cap is reached (no further
	request) or a page reaches below `since` / `min_id`. Pages are
	requested and their chats counted whole, so the boundary page's chats
	and users may belong to trimmed messages.

	`on_page(chats)` is awaited for every non-empty page after the first;
	the first page's chats are stored but not counted (baseline policy).
	Raises PaginationStalled if the offset stops decreasing.
	'''
	window = window or HistoryWindow(min_id=min_id)

	posts = await get_posts(
		client,
		channel_id,
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

		posts = await get_posts(
			client,
			channel_id,
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

Orchestration

'''

async def collect_channel(
		client,
		channel,
		*,
		output_folder,
		chats_file,
		counter,
		metadata_only=False,
		min_id=0,
		window=None
	):
	'''
	Collect one target: channel JSON, chat observations, and (unless
	metadata_only) the message history JSON. Returns False when the target
	is missing or inaccessible; it is then logged to the channels-not-found
	file.
	'''
	print('')
	print(f'> Collecting data from Telegram Channel -> {channel}')
	print('> ...')
	print('')

	entity_attrs = await get_entity_attrs(client, channel)
	if not entity_attrs:
		append_line(f'{output_folder}/{CHANNELS_NOT_FOUND}', channel)
		return False

	channel_id = entity_attrs.id
	channel_request = await full_channel_req(client, channel_id)
	full_channel_data = to_json_safe(channel_request.to_dict())

	print('> Writing channel data...')
	create_dirs(output_folder, subfolders=channel)
	write_json(f'{output_folder}/{channel}/{channel}.json', full_channel_data)
	print('> done.')
	print('')

	async def record(chats, req_type):
		await record_collected_chats(
			client,
			chats,
			source=channel,
			req_type=req_type,
			counter=counter,
			chats_file=chats_file,
			output_folder=output_folder
		)

	await record(full_channel_data['chats'], 'channel_request')
	if metadata_only:
		return True

	async def on_page(chats):
		await record(chats, 'from_messages')

	data = await fetch_history(
		client,
		channel_id,
		min_id=min_id,
		window=window,
		on_page=on_page
	)

	print('> Writing posts data...')
	write_json(
		f'{output_folder}/{channel}/{channel}_messages.json',
		to_json_safe(data)
	)
	print('> done.')
	print('')

	return True


async def run_collection(
		targets,
		output_folder,
		*,
		client_factory,
		authorize,
		metadata_only=False,
		min_id=0,
		window=None,
		pause=asyncio.sleep
	):
	'''
	Collect `targets` sequentially with one client.

	client_factory -> zero-argument callable returning an unconnected client;
		called inside the running loop
	authorize -> coroutine function awaited with the connected client
		(api.auth.ensure_authorized in production)
	window -> HistoryWindow selecting the messages kept (default: all
		above min_id)
	pause -> coroutine function used for the pause between batch channels

	Missing/inaccessible targets and stalled pagination fail only that
	target. Any other exception, AuthenticationError, or cancellation ends
	the run; the client is disconnected exactly once in every case.
	'''
	summary = CollectionSummary(requested=list(targets), output_folder=output_folder)
	client = client_factory()
	try:
		await client.connect()
		await authorize(client)

		create_dirs(output_folder)
		counter = {}
		with open(f'{output_folder}/chats.txt', mode='a', encoding='utf-8') as chats_file:
			for channel in summary.requested:
				try:
					found = await collect_channel(
						client,
						channel,
						output_folder=output_folder,
						chats_file=chats_file,
						counter=counter,
						metadata_only=metadata_only,
						min_id=min_id,
						window=window
					)
				except PaginationStalled as exc:
					print(f'> {channel}: {exc}; messages not saved.', file=sys.stderr)
					summary.failed.append((channel, str(exc)))
					continue

				if not found:
					summary.failed.append((channel, 'not found or not accessible'))
					continue

				summary.succeeded.append(channel)
				if len(summary.requested) > 1:
					await pause(BATCH_PAUSE_SECONDS)

		finalize_outputs(output_folder, counter)
	finally:
		await client.disconnect()

	return summary
