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

from dataclasses import dataclass, field
from datetime import datetime, timezone

# import local submodules
from utils import DataInputError, chats_dataset_columns, create_dirs, discard, \
	temp_path_for


STATUS_FILE = 'collection-status.json'
LOCK_FILE = '.collection.lock'
STATUS_FORMAT = 1

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

# sidecar states
RUNNING = 'running'
COMPLETE = 'complete'
PARTIAL = 'partial'
INTERRUPTED = 'interrupted'

# names listed in builder notes before "and N more"
NOTE_LIST_LIMIT = 10


class StorageError(Exception):
	'''
	An output file could not be written. Ends the run.
	'''


class OutputLocked(Exception):
	'''
	Another collection holds the output folder, or an interrupted one left
	its lock behind.
	'''


class HistoryUnreadable(Exception):
	'''
	An existing <channel>_messages.json cannot be read or parsed.
	'''


'''

Records

'''

@dataclass
class ChannelResult:
	'''
	Everything one channel's collection produced, before anything is
	written.

	index -> position of the target in the run's input
	observations -> [(req_type, [(chat_id, username), ...]), ...] in the
		order the chats were seen (channel response, then history pages)
	history -> JSON-safe messages payload to write; None writes nothing
	history_mode -> 'replaced' (full or window), 'merged' / 'unchanged' /
		'created' (--update), or 'not_collected' (metadata only)
	'''
	index: int
	target: str
	channel_id: int | None = None
	channel_data: dict | None = None
	history: dict | None = None
	history_mode: str = 'not_collected'
	observations: list = field(default_factory=list)
	messages: int | None = None
	min_id: int | None = None


@dataclass
class EnrichOutcome:
	'''
	Result of enriching one discovered chat (its full-channel response).

	linked -> [(chat_id, username)] public chats listed in the response,
		in order, up to the point of any failure
	rows -> collected_chats rows (public chats with participants_count);
		None when enrichment failed
	error -> None, 'not_found' (logged to the users-not-found file), or a
		'missing field ...' description (reported, not logged)
	'''
	linked: list = field(default_factory=list)
	rows: list | None = None
	error: str | None = None


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
	in a history page (`from_messages`). Returns True for a new chat, whose
	enrichment is then applied.
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
	A repeat observation records the parent in
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


@dataclass
class Aggregates:
	'''
	This run's counters and aggregate rows, built by `reduce_results`.
	'''
	counter: dict = field(default_factory=dict)
	chat_ids: list = field(default_factory=list)
	metadata: list = field(default_factory=list)
	users_not_found: list = field(default_factory=list)
	channels_not_found: list = field(default_factory=list)
	skipped: list = field(default_factory=list)


def reduce_results(results, outcomes, not_found=()):
	'''
	Replay committed channel results in input order and return the run's
	Aggregates. `outcomes` maps a chat id to its EnrichOutcome; it must
	hold every chat observed directly (workers enrich each one). The
	replay applies an outcome only when the chat is new to the run.
	`not_found` is [(index, target)] for targets that could not be resolved
	or accessed.
	'''
	agg = Aggregates()
	agg.channels_not_found = [t for _, t in sorted(not_found)]

	for result in sorted(results, key=lambda r: r.index):
		source = result.target
		for req_type, chats in result.observations:
			for chat_id, username in chats:
				agg.chat_ids.append(chat_id)
				if not observe_chat(agg.counter, chat_id, username, source, req_type):
					continue
				apply_outcome(agg, outcomes[chat_id], chat_id, username, source)

	return agg


def apply_outcome(agg, outcome, chat_id, username, source):
	for linked_id, linked_username in outcome.linked:
		agg.chat_ids.append(linked_id)
		observe_linked_chat(agg.counter, linked_id, linked_username, source, username)

	if outcome.error == 'not_found':
		agg.users_not_found.append(f'ID - {chat_id}')
	elif outcome.error is not None:
		agg.skipped.append((chat_id, outcome.error))
	else:
		agg.metadata.extend(dict(row) for row in outcome.rows)


'''

Files

'''

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


def append_lines(path, lines):
	if not lines:
		return
	try:
		with open(path, mode='a', encoding='utf-8') as writer:
			for line in lines:
				writer.write(f'{line}\n')
	except OSError as exc:
		raise StorageError(f'cannot write {path}: {exc}') from exc


def _has_no_rows(csv_path):
	'''
	True when a CSV holds at most a header line.
	'''
	with open(csv_path, encoding='utf-8') as reader:
		reader.readline()
		return reader.readline() == ''


def empty_collected_chats_columns():
	'''
	Header for a collected_chats.csv with no rows: the selected chat
	columns plus the merged counter columns.
	'''
	chat_columns = [
		c for c in chats_dataset_columns() if c not in DATASET_STAT_COLUMNS
	]
	return chat_columns + COUNTER_COLUMNS[2:]


def counter_frame(counter):
	import pandas as pd

	return pd.DataFrame.from_dict(
		counter,
		orient='index'
	).reset_index().rename(
		columns={
			'index': 'id'
		}
	).reindex(columns=COUNTER_COLUMNS)


def write_aggregates(output_folder, agg):
	'''
	Write chats.txt, counter.csv, collected_chats.csv, and the exception
	logs for this run's Aggregates.

	collected_chats.csv keeps earlier rows as they are and appends this
	run's rows merged with this run's counts. A run that adds no rows
	leaves an existing file untouched; a missing or header-only file with
	no new rows becomes a header-correct empty file.
	'''
	import pandas as pd

	try:
		chats_path = f'{output_folder}/chats.txt'
		earlier = []
		if os.path.isfile(chats_path):
			with open(chats_path, mode='r', encoding='utf-8') as reader:
				earlier = [line.rstrip() for line in reader]
		ids = dict.fromkeys(earlier + [str(i) for i in agg.chat_ids])
		write_text(chats_path, ''.join(f'{c}\n' for c in ids))

		counter_df = counter_frame(agg.counter)
		replace_file(
			f'{output_folder}/counter.csv',
			lambda tmp: counter_df.to_csv(tmp, encoding='utf-8', index=False)
		)

		csv_path = f'{output_folder}/collected_chats.csv'
		has_rows = os.path.isfile(csv_path) and not _has_no_rows(csv_path)
		if agg.metadata:
			df = pd.DataFrame(agg.metadata).merge(
				counter_df.drop(columns='username'), how='left', on='id'
			)
			if has_rows:
				# text as written; this run's counts never touch earlier rows
				earlier_df = pd.read_csv(
					csv_path, encoding='utf-8', dtype=str, keep_default_na=False
				)
				df = pd.concat([earlier_df, df], ignore_index=True)
			replace_file(
				csv_path,
				lambda tmp: df.to_csv(tmp, index=False, encoding='utf-8')
			)
		elif not has_rows:
			empty = pd.DataFrame(columns=empty_collected_chats_columns())
			replace_file(
				csv_path,
				lambda tmp: empty.to_csv(tmp, index=False, encoding='utf-8')
			)
	except OSError as exc:
		raise StorageError(f'cannot update aggregate files in {output_folder}: {exc}') from exc

	append_lines(f'{output_folder}/{CHANNELS_NOT_FOUND}', agg.channels_not_found)
	append_lines(f'{output_folder}/{USERS_NOT_FOUND}', agg.users_not_found)


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


def write_json_pretty(path, data):
	write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + '\n')


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
