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
from telegram_tracker.storage.histories import FileHistories
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


def open_file_writer(*, output_folder, profile, started_at, targets, parameters):
	'''
	The run's writer for the file outputs (the default store). The
	collector opens its writer through such a factory, after locking the
	output folder; see OutputWriter for the methods it uses.
	'''
	return OutputWriter(output_folder, profile, started_at)


class OutputWriter:
	'''
	Single owner of channel JSON and aggregate output. `commit` writes one
	channel's JSON files; `finalize` writes the aggregates from every
	committed channel. Before writing channel payloads, clear `phone` on
	User objects marked `is_self` (see drop_own_phone).

	The collector's contract with a run's store, which this class
	implements for the files and storage.sqlite.writer for the database:

		histories                    the history source for --update
		                             (storage.histories)
		commit(result)               store one ChannelResult; returns a
		                             takeover entry or None
		has_history(target, channel_id)
		record_not_found(index, target)
		finalize(outcomes)           replay committed results; returns the
		                             Aggregates
		finish(state, *, summary, stop_reason, requests, targets)
		                             record the run's end (files: nothing,
		                             the sidecar holds it)
		close()

	A failed write raises StorageError, which ends the run.

	profile -> the run's RunProfile; with one, every written history gets
		its provenance record (see provenance.py), stamped `started_at`
	'''
	def __init__(self, output_folder, profile=None, started_at=None):
		self.output_folder = output_folder
		self.profile = profile
		self.started_at = started_at
		self.histories = FileHistories(output_folder, profile)
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

	def has_history(self, target, channel_id):
		return os.path.isfile(history_path(self.output_folder, target))

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

	def finish(self, state, *, summary, stop_reason, requests, targets):
		pass

	def close(self):
		pass


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
