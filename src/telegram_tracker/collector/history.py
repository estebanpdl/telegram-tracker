# -*- coding: utf-8 -*-
'''
Channel history: the HistoryWindow that selects the messages kept, and
paging through a channel's history newest first within that window.
'''

# import modules
from dataclasses import dataclass
from datetime import datetime, timezone

# import local submodules
from telegram_tracker.collector.outcomes import PaginationStalled


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
