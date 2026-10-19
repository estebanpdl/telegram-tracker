# -*- coding: utf-8 -*-
'''
The run's output writer and the output-folder lock. `OutputWriter` is the
single owner of channel JSON and aggregate output; `OutputLock` keeps a
second collection out of the same folder.
'''

# import modules
import json
import os
import sys

# import local submodules
from telegram_tracker.storage.aggregates import reduce_results, write_aggregates
from telegram_tracker.storage.files import history_path, write_json
from telegram_tracker.storage.provenance import write_provenance
from telegram_tracker.storage.records import LOCK_FILE, OutputLocked, StorageError
from telegram_tracker.storage.status import utc_now
from telegram_tracker.utils import create_dirs, discard


'''

Writer

'''

def drop_own_phone(payload):
	'''
	Set `phone` to None in every User object of `payload` (nested
	dicts and lists, changed in place) that is the collecting account
	itself (`is_self`). The rest of that object and every other user stay
	as collected. Returns the number of phone numbers removed.

	A User whose phone the API did not return has `phone: None` too, so a
	redacted object keeps the usual shape.
	'''
	removed = 0
	pending = [payload]
	while pending:
		item = pending.pop()
		if isinstance(item, dict):
			if item.get('_') == 'User' and item.get('is_self') is True and item.get('phone') is not None:
				item['phone'] = None
				removed += 1
			pending.extend(item.values())
		elif isinstance(item, list):
			pending.extend(item)
	return removed


class OutputWriter:
	'''
	Single owner of channel JSON and aggregate output. `commit` writes one
	channel's JSON files; `finalize` writes the aggregates from every
	committed channel. Channel files never keep the collecting account's
	own phone number (see drop_own_phone).

	profile -> the run's RunProfile; with one, every written history gets
		its provenance record (see provenance.py), stamped `started_at`
	'''
	def __init__(self, output_folder, profile=None, started_at=None):
		self.output_folder = output_folder
		self.profile = profile
		self.started_at = started_at
		self.committed = []
		self.not_found = []
		self.finalized = False

	def commit(self, result):
		'''
		Write <target>.json and, when the result carries one,
		<target>_messages.json and its provenance record. The channel counts
		as collected only after all are in place. Returns the takeover entry
		of a replaced history (see provenance.write_provenance) or None.
		'''
		target = result.target
		try:
			create_dirs(self.output_folder, subfolders=target)
		except OSError as exc:
			raise StorageError(f'cannot create {self.output_folder}/{target}: {exc}') from exc

		drop_own_phone(result.channel_data)
		write_json(f'{self.output_folder}/{target}/{target}.json', result.channel_data)
		takeover = None
		if result.history is not None:
			path = history_path(self.output_folder, target)
			had_history = os.path.isfile(path)
			drop_own_phone(result.history)
			write_json(path, result.history)
			if self.profile is not None:
				takeover = write_provenance(
					self.output_folder, result, self.profile, self.started_at, had_history
				)

		self.committed.append(result)
		return takeover

	def record_not_found(self, index, target):
		self.not_found.append((index, target))

	def finalize(self, outcomes):
		'''
		Replay committed results and write the aggregate files. Returns the
		Aggregates. The caller must invoke this only once per run.
		'''
		agg = reduce_results(self.committed, outcomes, self.not_found)
		self.finalized = True
		write_aggregates(self.output_folder, agg)
		for chat_id, reason in agg.skipped:
			print(f'> Skipped metadata for chat {chat_id}: {reason}', file=sys.stderr)
		return agg


'''

Lock

'''

def lock_path(output_folder):
	return os.path.join(output_folder, LOCK_FILE)


def lock_message(output_folder):
	path = lock_path(output_folder)
	detail = ''
	try:
		with open(path, encoding='utf-8') as reader:
			info = json.load(reader)
		detail = f' (pid {info.get("pid")}, started {info.get("started_at")})'
	except (OSError, ValueError, AttributeError):
		pass
	return (
		f'{output_folder} is in use by another collection{detail}. If none is '
		f'running, an earlier run was interrupted: keep the folder for '
		f'inspection and collect into a fresh --output, or delete {path} to '
		'reuse it.'
	)


def check_unlocked(output_folder):
	'''
	Raise OutputLocked when the folder holds a collection lock.
	'''
	if os.path.exists(lock_path(output_folder)):
		raise OutputLocked(lock_message(output_folder))


class OutputLock:
	'''
	Exclusive lock on an output folder: a lock file created with O_EXCL,
	removed on exit. Never touches the session file. A lock left by a
	crashed run is reported, not removed.
	'''
	def __init__(self, output_folder):
		self.path = lock_path(output_folder)
		self.output_folder = output_folder

	def __enter__(self):
		try:
			fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
		except FileExistsError:
			raise OutputLocked(lock_message(self.output_folder)) from None
		except OSError as exc:
			raise StorageError(f'cannot create {self.path}: {exc}') from exc

		with os.fdopen(fd, 'w', encoding='utf-8') as writer:
			json.dump({'pid': os.getpid(), 'started_at': utc_now()}, writer)
		return self

	def __exit__(self, *exc_info):
		discard(self.path)
		return False
