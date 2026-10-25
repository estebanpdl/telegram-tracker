# -*- coding: utf-8 -*-
'''
Reading the file outputs of a collection folder: every
<data>/**/*_messages.json (in sorted path order) and
<data>/collected_chats.csv. Nothing is written or locked; the sidecar
gate (storage.status) has already run.
'''

# import modules
import glob
import json
import os

# import local submodules
from telegram_tracker.reader.records import FILES, ChannelHistory, ChatsInput, HistoryProvenance
from telegram_tracker.storage.provenance import read_provenance
from telegram_tracker.storage.records import HistoryUnreadable
from telegram_tracker.utils import DataInputError


COLLECTED_CHATS = 'collected_chats.csv'


def message_files(data_path):
	'''
	Every <data>/**/*_messages.json, sorted for a stable row order.
	'''
	pattern = os.path.join(glob.escape(data_path), '**', '*_messages.json')
	return sorted(glob.glob(pattern, recursive=True))


def channel_name(file_path):
	'''
	Channel username from a <channel>_messages.json path.
	'''
	return file_path.split('.json')[0].replace('\\', '/').split('/')[-1].replace(
		'_messages', ''
	)


def read_messages_file(file_path):
	'''
	A <channel>_messages.json payload. Raises DataInputError when it is
	not JSON; its shape is checked by the builder.
	'''
	try:
		with open(file_path, encoding='utf-8', mode='r') as fl:
			return json.load(fl)
	except (ValueError, UnicodeDecodeError) as exc:
		raise DataInputError(f'{file_path} is not valid JSON: {exc}') from None


def file_provenance(messages_path):
	'''
	The HistoryProvenance from the <channel>_provenance.json beside a
	messages file (unknown, all None, without a readable record), and
	the channel ID from <channel>.json (None when it cannot be read).
	Both are metadata only: a build does not depend on them.
	'''
	base = messages_path[:-len('_messages.json')]
	try:
		record = read_provenance(base + '_provenance.json')
	except HistoryUnreadable:
		record = None
	provenance = HistoryProvenance() if record is None else HistoryProvenance(
		profile_name=record.get('profile_name'),
		profile_id=record.get('profile_id'),
		mode=record.get('history'),
		takeover=record.get('takeover') if isinstance(record.get('takeover'), bool) else None
	)
	return provenance, snapshot_channel_id(base + '.json')


def snapshot_channel_id(path):
	'''
	full_chat.id of a <channel>.json channel snapshot, or None.
	'''
	try:
		with open(path, encoding='utf-8') as reader:
			channel_id = json.load(reader)['full_chat']['id']
	except (OSError, ValueError, KeyError, TypeError):
		return None
	return channel_id if type(channel_id) is int else None


class FilesCollection:
	'''
	The file outputs of `data_path`; see the module docstring.
	'''
	source = FILES

	def __init__(self, data_path, status=None):
		self.data_path = data_path
		# the last run as collection-status.json records it (None without one)
		self.last_run = None if status is None else {
			'run_uuid': None,
			'state': status.get('state'),
			'mode': (status.get('parameters') or {}).get('mode'),
			'started_at': status.get('started_at'),
			'finished_at': status.get('finished_at'),
			'profile_name': status.get('profile_name'),
			'profile_id': status.get('profile_id')
		}

	def histories(self):
		'''
		Yield a ChannelHistory per messages file, in sorted path order.
		'''
		for path in message_files(self.data_path):
			provenance, channel_id = file_provenance(path)
			yield ChannelHistory(
				name=channel_name(path),
				label=path,
				channel_id=channel_id,
				provenance=provenance,
				load=lambda path=path: read_messages_file(path)
			)

	@staticmethod
	def channel_chats(history):
		'''
		The chats of the channel snapshot beside a ChannelHistory's
		messages file (<channel>.json); empty when it cannot be read.
		'''
		try:
			with open(history.label[:-len('_messages.json')] + '.json', encoding='utf-8') as reader:
				chats = json.load(reader)['chats']
		except (OSError, ValueError, KeyError, TypeError):
			return []
		return [chat for chat in chats if isinstance(chat, dict)] if isinstance(chats, list) else []

	@staticmethod
	def message_runs(history, payload):
		'''
		{message_id: started_at of the collection run that wrote it} for a
		history that its last run replaced or created, from the provenance
		record; None when that cannot be told (merged by --update, or no
		readable record): files do not record which run wrote each
		message.
		'''
		try:
			record = read_provenance(history.label[:-len('_messages.json')] + '_provenance.json')
		except HistoryUnreadable:
			record = None
		if record is None or record.get('history') not in ('replaced', 'created'):
			return None
		return {
			message.get('id'): record.get('run_started_at')
			for message in payload.get('messages') or () if isinstance(message, dict)
		}

	def collected_chats(self):
		path = f'{self.data_path}/{COLLECTED_CHATS}'
		if not os.path.isfile(path):
			raise DataInputError(f'{path} not found; run collection first')
		return ChatsInput(source=path, label=path)

	def describe(self):
		return {'database': None, 'last_run': self.last_run}
