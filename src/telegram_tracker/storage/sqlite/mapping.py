# -*- coding: utf-8 -*-
'''
Payloads to rows and stored rows back to payloads, without a database.

Raw columns hold strict JSON text of the collector's JSON-safe payloads,
unfiltered except for the own-phone rule the writer applies first
(storage.writer.drop_own_phone). Peers are keyed by (peer_kind,
peer_id), because a user, a basic group and a channel can share an ID.
'''

# import modules
import json

# import local submodules
from telegram_tracker.utils import JSONEncoder


HISTORY_LISTS = ('messages', 'chats', 'users')
ENTITY_LISTS = ('chats', 'users')

PEER_KINDS = {
	'User': 'user',
	'UserEmpty': 'user',
	'Chat': 'chat',
	'ChatEmpty': 'chat',
	'ChatForbidden': 'chat',
	'Channel': 'channel',
	'ChannelForbidden': 'channel'
}


def json_text(data):
	'''
	Compact strict JSON (the files' format, without NaN or Infinity, which
	json_valid refuses). Datetimes and bytes become str, as in the files.
	Raises ValueError or TypeError for data JSON cannot hold.
	'''
	return json.dumps(
		data, cls=JSONEncoder, ensure_ascii=False, separators=(',', ':'), allow_nan=False
	)


def peer_kind(entity, list_name):
	'''
	'user', 'chat' or 'channel' for an entity of a payload's `list_name`
	('chats' or 'users'). A constructor this mapping does not know is
	typed by its list and name.
	'''
	kind = PEER_KINDS.get(entity.get('_'))
	if kind is not None:
		return kind
	if list_name == 'users':
		return 'user'
	return 'channel' if str(entity.get('_', '')).startswith('Channel') else 'chat'


def peer_key(entity, list_name):
	'''
	(peer_kind, peer_id). Raises ValueError for an entity without a
	positive integer id.
	'''
	peer_id = entity.get('id') if isinstance(entity, dict) else None
	if type(peer_id) is not int or peer_id <= 0:
		raise ValueError(f'a {list_name} entry has no positive integer id')
	return peer_kind(entity, list_name), peer_id


def entities(payload):
	'''
	[(list_name, (peer_kind, peer_id), entity)] of a payload's chats and
	users, in payload order, each typed key once (the first copy).
	'''
	found = []
	seen = set()
	for list_name in ENTITY_LISTS:
		for entity in payload.get(list_name) or ():
			key = peer_key(entity, list_name)
			if key not in seen:
				seen.add(key)
				found.append((list_name, key, entity))
	return found


def peer_row(key, entity, run_id):
	'''
	Values for an upsert into peers (see writer.UPSERT_PEER).
	'''
	kind, peer_id = key
	return (
		kind, peer_id, str(entity.get('_')), 1 if entity.get('min') is True else 0,
		json_text(entity), run_id, run_id, run_id
	)


def message_rows(channel_id, messages, run_id):
	'''
	Values for an upsert into messages. Raises ValueError for a message
	without a positive integer id.
	'''
	rows = []
	for message in messages:
		message_id = message.get('id') if isinstance(message, dict) else None
		if type(message_id) is not int or message_id <= 0:
			raise ValueError('a message has no positive integer id')
		rows.append((channel_id, message_id, run_id, json_text(message)))
	return rows


def envelope(history):
	'''
	A history response without its messages, chats and users.
	'''
	return {k: v for k, v in history.items() if k not in HISTORY_LISTS}


def merged_entities(fetched_keys, earlier):
	'''
	The entity membership after --update: per list, the fetched entities
	first, then the earlier ones not fetched again, each typed key once.
	`fetched_keys` and `earlier` are [(list_name, (peer_kind, peer_id))]
	in their order.
	'''
	seen = set()
	merged = []
	for list_name in ENTITY_LISTS:
		for source in (fetched_keys, earlier):
			for name, key in source:
				if name == list_name and key not in seen:
					seen.add(key)
					merged.append((name, key))
	return merged


def history_payload(envelope_text, messages, entity_rows):
	'''
	A stored history as a payload: the envelope's fields, `messages` (raw
	JSON texts, newest first) and the chats and users from `entity_rows`
	([(list_name, raw)] in order).
	'''
	payload = json.loads(envelope_text)
	payload['messages'] = [json.loads(raw) for raw in messages]
	for list_name in ENTITY_LISTS:
		payload[list_name] = [json.loads(raw) for name, raw in entity_rows if name == list_name]
	return payload


def participants_count(channel_data):
	full_chat = (channel_data or {}).get('full_chat')
	count = full_chat.get('participants_count') if isinstance(full_chat, dict) else None
	return count if type(count) is int else None
