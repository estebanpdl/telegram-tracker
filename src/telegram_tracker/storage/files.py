# -*- coding: utf-8 -*-
'''
Output file writes and saved message histories. JSON and text files are
replaced through a sibling temporary file and os.replace, so a reader sees
the old or the new file; exception logs are appended directly. OSError
becomes StorageError.
'''

# import modules
import json
import os

# import local submodules
from telegram_tracker.storage.records import HistoryUnreadable, StorageError
from telegram_tracker.utils import discard, temp_path_for


def to_json_text(data):
	'''
	Compact UTF-8 JSON.
	'''
	return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def replace_file(path, write):
	'''
	Call write(temp_path), then move the temporary file over `path`.
	OSError becomes StorageError; the temporary file never survives.
	'''
	tmp = temp_path_for(path)
	try:
		write(tmp)
		os.replace(tmp, path)
	except OSError as exc:
		raise StorageError(f'cannot write {path}: {exc}') from exc
	finally:
		discard(tmp)


def write_text(path, text):
	def write(tmp):
		with open(tmp, mode='w', encoding='utf-8') as writer:
			writer.write(text)

	replace_file(path, write)


def write_json(path, data):
	write_text(path, to_json_text(data))


def write_json_pretty(path, data):
	write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + '\n')


def append_lines(path, lines):
	if not lines:
		return
	try:
		with open(path, mode='a', encoding='utf-8') as writer:
			for line in lines:
				writer.write(f'{line}\n')
	except OSError as exc:
		raise StorageError(f'cannot write {path}: {exc}') from exc


'''

Message histories (--update)

'''

def history_path(output_folder, target):
	return f'{output_folder}/{target}/{target}_messages.json'


def read_history(path):
	'''
	Load an existing messages file for --update. Raises HistoryUnreadable
	when it cannot be read or does not have the collector's shape
	(messages with integer ids; chats/users lists of objects with ids).
	'''
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise HistoryUnreadable(f'{path} cannot be read: {exc}') from None

	def ids_ok(items):
		return isinstance(items, list) and all(
			isinstance(i, dict) and isinstance(i.get('id'), int)
			and not isinstance(i.get('id'), bool)
			for i in items
		)

	if not isinstance(data, dict) or not ids_ok(data.get('messages')):
		raise HistoryUnreadable(f'{path} has no list of messages with integer ids')
	for key in ('chats', 'users'):
		if key in data and not ids_ok(data[key]):
			raise HistoryUnreadable(f'{path} has a malformed "{key}" list')

	return data


def newest_id(history):
	'''
	Highest message id in a loaded history; 0 when it holds none.
	'''
	return max((m['id'] for m in history['messages']), default=0)


def merge_histories(earlier, fetched):
	'''
	Merge a fetched payload into an earlier history. Returns None when
	nothing new was fetched (the file stays as it is).

	Messages: union by id, fetched copies win, newest (highest id) first.
	chats / users: union by id, fetched copies first and winning.
	Every other field (count, pts, ...) comes from the fetched response.
	'''
	if not fetched['messages']:
		return None

	by_id = {m['id']: m for m in earlier['messages']}
	by_id.update({m['id']: m for m in fetched['messages']})

	def union(key):
		fresh = list(fetched.get(key, []))
		seen = {i['id'] for i in fresh}
		return fresh + [i for i in earlier.get(key, []) if i['id'] not in seen]

	merged = dict(fetched)
	merged['messages'] = sorted(by_id.values(), key=lambda m: m['id'], reverse=True)
	merged['chats'] = union('chats')
	merged['users'] = union('users')
	return merged
