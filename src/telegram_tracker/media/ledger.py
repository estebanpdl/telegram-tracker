# -*- coding: utf-8 -*-
'''
The media ledger: runs, items, blobs and attempts (docs/media-ledger.md).

A Ledger wraps one connection, the only one a run uses. Every method is
synchronous and runs at most one transaction, committed or rolled back
before it returns, so an asyncio caller never holds a ledger transaction
across an await and no two of its transactions interleave.

Writers hold the output folder's lock (.collection.lock) while the
ledger is open for writing. start_run relies on that: a run still
recorded as running then belongs to a process that ended, so its
downloads are recorded as interrupted before the new run starts.

Item states (TRANSITIONS; the DDL's triggers enforce the same rules):

	requested   -> requested, downloading, complete, skipped, failed
	downloading -> complete, skipped, failed
	skipped     -> requested
	failed      -> requested
	complete    final
'''

# import modules
import contextlib
import json
import stat

from collections import namedtuple
from dataclasses import dataclass

# import local submodules
from telegram_tracker.media.identity import check_ledger_identity, folder_identity
from telegram_tracker.media.layout import MediaLayout, is_link, lstat_or_none, relative_path
from telegram_tracker.media.ledger_schema import LedgerRefused, classify_ledger, connect_ledger, \
	create_ledger
from telegram_tracker.storage.destination import canonical_folder
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import app_version, interrupted_write, transaction
from telegram_tracker.storage.sqlite.schema import MISSING
from telegram_tracker.storage.status import utc_now


REQUESTED = 'requested'
DOWNLOADING = 'downloading'
COMPLETE = 'complete'
SKIPPED = 'skipped'
FAILED = 'failed'
STATES = (REQUESTED, DOWNLOADING, COMPLETE, SKIPPED, FAILED)

TRANSITIONS = {
	REQUESTED: frozenset({REQUESTED, DOWNLOADING, COMPLETE, SKIPPED, FAILED}),
	DOWNLOADING: frozenset({COMPLETE, SKIPPED, FAILED}),
	SKIPPED: frozenset({REQUESTED}),
	FAILED: frozenset({REQUESTED}),
	COMPLETE: frozenset()
}

SKIP_REASONS = ('over_file_limit', 'over_run_limit')
FAIL_REASONS = (
	'reference_expired', 'message_gone', 'media_changed', 'flood_stop', 'request_failed', 'disk_full',
	'storage_error', 'size_mismatch', 'path_conflict', 'stored_file_differs', 'interrupted'
)
INTERRUPTED_REASON = 'interrupted'

COMMANDS = ('collect', 'download-media')
RUNNING = 'running'
RUN_END_STATES = ('complete', 'partial', 'interrupted')
COUNTERS = ('app_requests', 'retries', 'flood_waits', 'refreshes', 'bytes_received')

ItemKey = namedtuple('ItemKey', 'channel_id message_id media_id')


class LedgerError(StorageError):
	'''
	A ledger write that the ledger's rules do not allow; nothing was
	changed.
	'''


class InvalidTransition(LedgerError):
	'''
	An item state change outside TRANSITIONS.
	'''


@dataclass(frozen=True)
class StartedRun:
	'''
	stale_runs -> run UUIDs found running and recorded as interrupted
	interrupted_items -> items found downloading and recorded as failed
	'''
	stale_runs: tuple
	interrupted_items: int


@dataclass(frozen=True)
class RequestCounts:
	'''
	How request() found each reference: new rows, rows requested again
	(requested, skipped or failed before), already complete, and rows
	this run is downloading.
	'''
	new: int = 0
	again: int = 0
	complete: int = 0
	downloading: int = 0


@dataclass(frozen=True)
class AttemptStats:
	bytes_received: int = 0
	app_requests: int = 0
	refreshed: bool = False


def open_ledger(layout, identity, *, create=False, readonly=False):
	'''
	The Ledger of a MediaLayout, checked against the folder's
	FolderIdentity (identity.check_ledger_identity). A missing ledger is
	created when `create` is set, bound to the identity's dataset UUID;
	otherwise it is refused. Links are refused at every level.
	'''
	writable = create and not readonly
	layout.ensure(create=writable)
	path = layout.ledger_path
	info = lstat_or_none(path)
	if info is not None and (is_link(info) or not stat.S_ISREG(info.st_mode)):
		raise LedgerRefused(f'{path} is not a regular file; it was left untouched.')
	if classify_ledger(path).kind == MISSING:
		if not writable:
			raise LedgerRefused(f'{path} does not exist')
		if identity.dataset_uuid is None:
			raise LedgerRefused(f'{layout.output_folder} has no dataset identity to bind {path} to')
		create_ledger(
			path, dataset_uuid=identity.dataset_uuid, storage=identity.storage,
			output_binding=canonical_folder(layout.output_folder), created_at=utc_now()
		)
	con, meta = connect_ledger(path, readonly=readonly)
	try:
		check_ledger_identity(meta, identity, path)
	except BaseException:
		con.close()
		raise
	return Ledger(con, path, meta)


def check_ledger(output_folder):
	'''
	Read only, before a run that will write the ledger of
	`output_folder`: the downloaded-media folder, when there is one, has
	no link, and its ledger, when there is one, belongs to the folder's
	data (open_ledger's refusals). Nothing is created. Returns None, or a
	note when the ledger has an unfinished write: a read-only connection
	cannot roll it back, so the check is left to the run's own open,
	which rolls it back once the output folder is locked.
	'''
	layout = MediaLayout(output_folder)
	if layout.ensure(create=False) is None or lstat_or_none(layout.ledger_path) is None:
		return None
	if interrupted_write(layout.ledger_path) is not None:
		return (
			f'{layout.ledger_path} has an unfinished write (a run stopped while writing it); it is '
			'checked and recovered once the output folder is locked.'
		)
	open_ledger(layout, folder_identity(output_folder), readonly=True).close()
	return None


def rows(cursor):
	names = [d[0] for d in cursor.description]
	return [dict(zip(names, row)) for row in cursor.fetchall()]


def check_counters(deltas):
	'''
	The non-zero entries of a COUNTERS mapping; ValueError for an unknown
	name or a value that is not a non-negative int.
	'''
	for name, value in (deltas or {}).items():
		if name not in COUNTERS or type(value) is not int or value < 0:
			raise ValueError(f'bad counter {name}={value!r}')
	return {name: value for name, value in (deltas or {}).items() if value}


def check_reason(state, reason):
	allowed = {SKIPPED: SKIP_REASONS, FAILED: FAIL_REASONS}.get(state, (None,))
	if reason not in allowed:
		raise ValueError(f'{reason!r} is not a reason for {state}')


class Ledger:
	'''
	An open media ledger; see the module docstring.
	'''
	def __init__(self, con, path, meta):
		self.con = con
		self.path = path
		self.meta = meta

	def close(self):
		self.con.close()

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()

	@contextlib.contextmanager
	def _write(self, action):
		with transaction(self.con, f'{action} in {self.path}'):
			yield self.con

	def _one(self, sql, *args):
		found = rows(self.con.execute(sql, args))
		return found[0] if found else None

	def _running(self, run_uuid):
		run = self._one('SELECT * FROM media_runs WHERE run_uuid = ?', run_uuid)
		if run is None or run['state'] != RUNNING:
			raise LedgerError(f'run {run_uuid} is not running in {self.path}')
		return run

	'''

	Runs

	'''

	def start_run(
			self, run_uuid, command, *, selection, profile_name=None, profile_id=None,
			collection_run=None, started_at=None):
		'''
		Record a new running run. Runs still recorded as running are first
		recorded as interrupted, with their downloading items failed
		('interrupted') and open attempts ended, in the same transaction.
		'''
		if command not in COMMANDS:
			raise ValueError(f'unknown command {command!r}')
		started_at = started_at or utc_now()
		with self._write('record the media run') as con:
			stale = tuple(r[0] for r in con.execute(
				"SELECT run_uuid FROM media_runs WHERE state = 'running' ORDER BY started_at"))
			now = utc_now()
			con.execute(
				"UPDATE attempts SET ended_at = ?, outcome = 'failed', reason = ? WHERE ended_at IS NULL",
				(now, INTERRUPTED_REASON)
			)
			interrupted = con.execute(
				"UPDATE items SET state = 'failed', reason = ?, updated_at = ? WHERE state = 'downloading'",
				(INTERRUPTED_REASON, now)
			).rowcount
			con.execute(
				"UPDATE media_runs SET state = 'interrupted', stop_reason = ? WHERE state = 'running'",
				(f'stale: its process ended without finishing (found by the run started {started_at})',)
			)
			con.execute(
				'INSERT INTO media_runs (run_uuid, command, collection_run, profile_name, profile_id, state, '
				"started_at, selection, app_version) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
				(run_uuid, command, collection_run, profile_name, profile_id, started_at,
					json.dumps(selection, sort_keys=True), app_version())
			)
		return StartedRun(stale, interrupted)

	def finish_run(self, run_uuid, state, *, stop_reason=None, finished_at=None):
		'''
		Record the run's end. Its items still downloading are failed
		('interrupted') and their attempts ended first.
		'''
		if state not in RUN_END_STATES:
			raise ValueError(f'{state!r} is not a final run state')
		with self._write('finish the media run') as con:
			self._running(run_uuid)
			now = finished_at or utc_now()
			self._interrupt_downloads(run_uuid, now)
			con.execute(
				'UPDATE media_runs SET state = ?, stop_reason = ?, finished_at = ? WHERE run_uuid = ?',
				(state, stop_reason, now, run_uuid)
			)

	def _interrupt_downloads(self, run_uuid, now):
		self.con.execute(
			"UPDATE attempts SET ended_at = ?, outcome = 'failed', reason = ? "
			'WHERE run_uuid = ? AND ended_at IS NULL',
			(now, INTERRUPTED_REASON, run_uuid)
		)
		self.con.execute(
			"UPDATE items SET state = 'failed', reason = ?, updated_at = ? "
			"WHERE state = 'downloading' AND last_run_uuid = ?",
			(INTERRUPTED_REASON, now, run_uuid)
		)

	def add_counters(self, run_uuid, **deltas):
		'''
		Add non-negative amounts to the run's COUNTERS.
		'''
		deltas = check_counters(deltas)
		if not deltas:
			return
		with self._write('count the media run'):
			self._running(run_uuid)
			self._count(run_uuid, deltas)

	def _count(self, run_uuid, deltas):
		if deltas:
			assignments = ', '.join(f'{name} = {name} + ?' for name in deltas)
			self.con.execute(f'UPDATE media_runs SET {assignments} WHERE run_uuid = ?', (*deltas.values(), run_uuid))

	'''

	Items

	'''

	def request(self, run_uuid, channel_id, refs, *, source_run=None):
		'''
		Record the selected media of one channel as requested, and the
		channel as covered by the run, in one transaction. `refs` holds
		(message_id, classify.MediaRef) pairs. New references get a row;
		requested, skipped and failed rows are requested again; complete
		rows and rows this run is downloading are left as they are.
		'''
		counts = dict.fromkeys(('new', 'again', 'complete', 'downloading'), 0)
		with self._write('record requested media') as con:
			run = self._running(run_uuid)
			now = utc_now()
			for message_id, ref in refs:
				key = ItemKey(channel_id, message_id, ref.media_id)
				found = self._one(
					'SELECT state FROM items WHERE channel_id = ? AND message_id = ? AND media_id = ?', *key)
				if found is None:
					con.execute(
						'INSERT INTO items (channel_id, message_id, media_id, media_type, media_kind, photo_size, '
						'type, subtype, mime_type, ext, expected_bytes, state, source_run, first_run_uuid, '
						'last_run_uuid, profile_name, profile_id, updated_at) '
						"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?)",
						(*key, ref.media_type, ref.media_kind, ref.photo_size, ref.type, ref.subtype,
							ref.mime_type, ref.ext, ref.expected_bytes, source_run, run_uuid, run_uuid,
							run['profile_name'], run['profile_id'], now)
					)
					counts['new'] += 1
				elif found['state'] in (REQUESTED, SKIPPED, FAILED):
					con.execute(
						"UPDATE items SET state = 'requested', reason = NULL, expected_bytes = ?, source_run = ?, "
						'last_run_uuid = ?, profile_name = ?, profile_id = ?, updated_at = ? '
						'WHERE channel_id = ? AND message_id = ? AND media_id = ?',
						(ref.expected_bytes, source_run, run_uuid, run['profile_name'], run['profile_id'], now,
							*key)
					)
					counts['again'] += 1
				else:
					counts[found['state']] += 1
			covered = set(json.loads(run['channels']))
			covered.add(channel_id)
			con.execute(
				'UPDATE media_runs SET channels = ? WHERE run_uuid = ?',
				(json.dumps(sorted(covered)), run_uuid)
			)
		return RequestCounts(**counts)

	def begin_download(self, run_uuid, key):
		'''
		Move a requested item to downloading and record its attempt;
		returns the attempt number.
		'''
		key = ItemKey(*key)
		with self._write('start a media download') as con:
			self._running(run_uuid)
			item = self._transition_check(key, DOWNLOADING)
			n = item['attempts'] + 1
			now = utc_now()
			con.execute(
				"UPDATE items SET state = 'downloading', attempts = ?, last_run_uuid = ?, updated_at = ? "
				'WHERE channel_id = ? AND message_id = ? AND media_id = ?',
				(n, run_uuid, now, *key)
			)
			con.execute(
				'INSERT INTO attempts (channel_id, message_id, media_id, n, run_uuid, started_at) '
				'VALUES (?, ?, ?, ?, ?, ?)',
				(*key, n, run_uuid, now)
			)
		return n

	def blob_path(self, key, sha256):
		'''
		Where content `sha256` of item `key` is stored: the existing blob's
		path in that channel, else the generated path a new blob gets.
		'''
		key = ItemKey(*key)
		blob = self.blob(key.channel_id, sha256)
		if blob is not None:
			return blob['path']
		item = self.item(key)
		if item is None:
			raise LedgerError(f'no media item {tuple(key)} in {self.path}')
		return relative_path(key.channel_id, item['type'], key.message_id, sha256, item['ext'])

	def complete(self, run_uuid, key, *, sha256, actual_bytes, attempt=None, stats=AttemptStats(), counters=None):
		'''
		Record the item complete with content `sha256`: a downloading item
		ends `attempt`; a requested one can only reuse a stored blob (no
		attempt). A new blob is recorded at blob_path(). `counters` are
		added to the run's COUNTERS in the same transaction. Returns the
		blob's path.
		'''
		key = ItemKey(*key)
		counters = check_counters(counters)
		with self._write('record a complete media item') as con:
			self._running(run_uuid)
			item = self._transition_check(key, COMPLETE)
			blob = self.blob(key.channel_id, sha256)
			if blob is None and item['state'] == REQUESTED:
				raise LedgerError(
					f'media item {tuple(key)} was not downloaded; it can only reuse a stored file')
			if blob is not None:
				if blob['bytes'] != actual_bytes:
					raise LedgerError(f'{blob["path"]} holds {blob["bytes"]} bytes, not {actual_bytes}')
				path = blob['path']
			else:
				path = relative_path(key.channel_id, item['type'], key.message_id, sha256, item['ext'])
				con.execute(
					'INSERT INTO blobs (channel_id, sha256, bytes, type, mime_type, ext, path, first_run_uuid, '
					'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
					(key.channel_id, sha256, actual_bytes, item['type'], item['mime_type'], item['ext'], path,
						run_uuid, utc_now())
				)
			self._settle(run_uuid, key, item, COMPLETE, None, attempt, stats, sha256=sha256,
				actual_bytes=actual_bytes)
			self._count(run_uuid, counters)
		return path

	def skip(self, run_uuid, key, reason, *, attempt=None, stats=AttemptStats(), counters=None):
		self._end(run_uuid, key, SKIPPED, reason, attempt, stats, counters)

	def fail(self, run_uuid, key, reason, *, attempt=None, stats=AttemptStats(), counters=None):
		self._end(run_uuid, key, FAILED, reason, attempt, stats, counters)

	def _end(self, run_uuid, key, state, reason, attempt, stats, counters):
		check_reason(state, reason)
		key = ItemKey(*key)
		counters = check_counters(counters)
		with self._write(f'record a {state} media item'):
			self._running(run_uuid)
			item = self._transition_check(key, state)
			self._settle(run_uuid, key, item, state, reason, attempt, stats)
			self._count(run_uuid, counters)

	def _transition_check(self, key, state):
		item = self.item(key)
		if item is None:
			raise LedgerError(f'no media item {tuple(key)} in {self.path}')
		if state not in TRANSITIONS[item['state']]:
			raise InvalidTransition(f'media item {tuple(key)} cannot go from {item["state"]} to {state}')
		return item

	def _settle(self, run_uuid, key, item, state, reason, attempt, stats, *, sha256=None, actual_bytes=None):
		'''
		Inside a write: end the item's open attempt (a downloading item
		must name it; a requested one has none) and set its final state.
		'''
		now = utc_now()
		if item['state'] == DOWNLOADING:
			if attempt != item['attempts'] or item['last_run_uuid'] != run_uuid:
				raise LedgerError(f'attempt {attempt} of {tuple(key)} is not the open one of this run')
			self.con.execute(
				'UPDATE attempts SET ended_at = ?, outcome = ?, reason = ?, bytes_received = ?, '
				'app_requests = ?, refreshed = ? '
				'WHERE channel_id = ? AND message_id = ? AND media_id = ? AND n = ? AND ended_at IS NULL',
				(now, state, reason, stats.bytes_received, stats.app_requests, int(stats.refreshed), *key, attempt)
			)
		elif attempt is not None:
			raise LedgerError(f'{tuple(key)} is not downloading; it has no open attempt')
		self.con.execute(
			'UPDATE items SET state = ?, reason = ?, sha256 = ?, actual_bytes = ?, last_run_uuid = ?, '
			'profile_name = (SELECT profile_name FROM media_runs WHERE run_uuid = ?), '
			'profile_id = (SELECT profile_id FROM media_runs WHERE run_uuid = ?), updated_at = ? '
			'WHERE channel_id = ? AND message_id = ? AND media_id = ?',
			(state, reason, sha256, actual_bytes, run_uuid, run_uuid, run_uuid, now, *key)
		)

	'''

	Reads

	'''

	def item(self, key):
		return self._one(
			'SELECT * FROM items WHERE channel_id = ? AND message_id = ? AND media_id = ?', *ItemKey(*key))

	def items(self, channel_id=None):
		if channel_id is None:
			return rows(self.con.execute('SELECT * FROM items ORDER BY channel_id, message_id, media_id'))
		return rows(self.con.execute(
			'SELECT * FROM items WHERE channel_id = ? ORDER BY message_id, media_id', (channel_id,)))

	def attempts(self, key):
		return rows(self.con.execute(
			'SELECT * FROM attempts WHERE channel_id = ? AND message_id = ? AND media_id = ? ORDER BY n',
			tuple(ItemKey(*key))))

	def blob(self, channel_id, sha256):
		return self._one('SELECT * FROM blobs WHERE channel_id = ? AND sha256 = ?', channel_id, sha256)

	def blob_hint(self, channel_id, media_kind, media_id):
		'''
		The blob of a complete item with the same media in the channel, or
		None. Only a hint: reuse needs the stored file to match its size
		and SHA-256.
		'''
		return self._one(
			'SELECT b.* FROM items i JOIN blobs b ON b.channel_id = i.channel_id AND b.sha256 = i.sha256 '
			"WHERE i.channel_id = ? AND i.media_kind = ? AND i.media_id = ? AND i.state = 'complete' "
			'ORDER BY b.created_at, b.path LIMIT 1',
			channel_id, media_kind, media_id
		)

	def run(self, run_uuid):
		return self._one('SELECT * FROM media_runs WHERE run_uuid = ?', run_uuid)

	def runs(self):
		return rows(self.con.execute('SELECT * FROM media_runs ORDER BY started_at, run_uuid'))
