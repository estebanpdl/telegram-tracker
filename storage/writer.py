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
from storage.aggregates import reduce_results, write_aggregates
from storage.files import history_path, write_json
from storage.records import LOCK_FILE, OutputLocked, StorageError
from storage.status import utc_now
from utils import create_dirs, discard


'''

Writer

'''

class OutputWriter:
	'''
	Single owner of channel JSON and aggregate output. `commit` writes one
	channel's JSON files; `finalize` writes the aggregates from every
	committed channel.
	'''
	def __init__(self, output_folder):
		self.output_folder = output_folder
		self.committed = []
		self.not_found = []
		self.finalized = False

	def commit(self, result):
		'''
		Write <target>.json and, when the result carries one,
		<target>_messages.json. The channel counts as collected only after
		both are in place.
		'''
		target = result.target
		try:
			create_dirs(self.output_folder, subfolders=target)
		except OSError as exc:
			raise StorageError(f'cannot create {self.output_folder}/{target}: {exc}') from exc

		write_json(f'{self.output_folder}/{target}/{target}.json', result.channel_data)
		if result.history is not None:
			write_json(history_path(self.output_folder, target), result.history)

		self.committed.append(result)

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
