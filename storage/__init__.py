# -*- coding: utf-8 -*-
'''
Collection output: the single owner of counters, aggregate files, channel
JSON, the output-folder lock, and the collection-status.json sidecar.

Workers hand finished `ChannelResult`s to an `OutputWriter`, which writes
each channel's JSON at once and keeps its chat observations. Aggregates
are computed at finalization by replaying the committed results in input
order, so they do not depend on the order in which channels finished:

	chats.txt            earlier ids + this run's, deduplicated (first occurrence)
	counter.csv          this run's counts
	collected_chats.csv  earlier rows unchanged + this run's rows with this
	                     run's counts (no _x/_y columns on a rerun)
	_exceptions-*.txt    appended

JSON, CSV, and chats.txt files are replaced through sibling temporary
files and os.replace, so a reader sees the old or the new file. Exception
logs are appended directly. Atomicity is per replaced file: a hard crash
can leave files of different generations, and collection-status.json then
still says "running".

Importing this module has no side effects and loads neither Telethon nor
pandas (pandas loads when aggregates are written).
'''

# import modules
import json
import os
import sys

from datetime import datetime, timezone

# import local submodules
from storage.aggregates import reduce_results, write_aggregates
from storage.files import history_path, write_json, write_json_pretty
from storage.records import INTERRUPTED, LOCK_FILE, RUNNING, STATUS_FILE, \
	STATUS_FORMAT, OutputLocked, StorageError
from utils import DataInputError, create_dirs, discard



# names listed in builder notes before "and N more"
NOTE_LIST_LIMIT = 10





'''

Writer

'''

class OutputWriter:
	'''
	Single owner of a run's output files. `commit` writes one channel's
	JSON files; `finalize` writes the aggregates from every committed
	channel.
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


'''

Status sidecar

'''

def utc_now():
	return datetime.now(timezone.utc).isoformat(timespec='seconds')


class StatusFile:
	'''
	collection-status.json: the run's state, parameters, and per-target
	outcome, without credentials or message text.

	States: running (from the start until finalization completes),
	complete (every target collected), partial (finalized, some targets
	failed), interrupted (stopped before every target was attempted:
	cancellation, account stop, or a storage error).
	'''
	def __init__(self, output_folder, targets, parameters):
		self.path = os.path.join(output_folder, STATUS_FILE)
		self.data = {
			'format': STATUS_FORMAT,
			'state': RUNNING,
			'started_at': utc_now(),
			'finished_at': None,
			'parameters': parameters,
			'summary': None,
			'stop_reason': None,
			'targets': [
				{'input': t, 'status': 'pending'} for t in targets
			],
			'requests': {}
		}

	def target(self, index):
		return self.data['targets'][index]

	def write(self):
		write_json_pretty(self.path, self.data)

	def finish(self, state, *, summary, stop_reason=None, requests=None):
		self.data.update(
			state=state,
			finished_at=utc_now(),
			summary=summary,
			stop_reason=stop_reason,
			requests=requests or {}
		)
		self.write()


def read_status(data_path):
	'''
	The sidecar of `data_path`, or None when there is none. Raises
	DataInputError when it exists but cannot be read.
	'''
	path = os.path.join(data_path, STATUS_FILE)
	if not os.path.isfile(path):
		return None
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise DataInputError(f'{path} cannot be read: {exc}') from None
	if not isinstance(data, dict) or 'state' not in data:
		raise DataInputError(f'{path} is not a collection status file')
	return data


def check_collection_status(data_path, *, messages=True):
	'''
	Builder gate. Raises DataInputError when the last collection into
	`data_path` is running or was interrupted; returns notes (strings)
	for a finalized partial run and, when `messages`, for a run whose
	message files hold a subset (history window or --update). Folders
	without a sidecar (earlier versions) pass with no notes.
	'''
	status = read_status(data_path)
	if status is None:
		return []

	path = os.path.join(data_path, STATUS_FILE)
	state = status['state']
	recovery = (
		'Keep this folder for inspection and rerun collection into a fresh '
		'--output; completed channel files stay readable.'
	)
	if state == RUNNING:
		raise DataInputError(
			f'{path} says a collection is still running. Build after it '
			f'finishes. If none is running, it stopped without finishing. {recovery}'
		)
	if state == INTERRUPTED:
		reason = status.get('stop_reason') or 'unknown reason'
		raise DataInputError(
			f'{path}: the last collection was interrupted ({reason}), so its '
			f'aggregate files may be incomplete. {recovery}'
		)

	notes = []
	failed = [
		t['input'] for t in status.get('targets', [])
		if t.get('status') not in ('succeeded', 'duplicate')
	]
	if failed:
		names = ', '.join(failed[:NOTE_LIST_LIMIT])
		if len(failed) > NOTE_LIST_LIMIT:
			names += f', and {len(failed) - NOTE_LIST_LIMIT} more'
		notes.append(
			f'the last collection finished with {len(failed)} failed target(s), '
			f'excluded from its output: {names}'
		)

	parameters = status.get('parameters') or {}
	if messages and parameters.get('mode') == 'window':
		notes.append(
			'the last collection used a history window '
			f'({parameters.get("window_description")}); its message files hold '
			'only that selection'
		)
	if messages and parameters.get('mode') == 'update':
		notes.append(
			'the last collection ran with --update; rebuilding appends its '
			'merged histories to msgs_dataset.csv again, duplicating earlier rows'
		)
	return notes
