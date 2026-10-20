# -*- coding: utf-8 -*-
'''
SQLiteWriter: one collection run's writes to the data database. It has
the methods of storage.writer.OutputWriter, which the collector uses.

	open      connect (creating a missing database) and record the run as
	          running with its targets pending, in one transaction
	commit    one channel in one transaction: channel snapshot, peers,
	          history, and the target's outcome
	finalize  replay the committed results in input order and store the
	          run's observations, in one transaction
	finish    counters, collected-chat rows, errors, request statistics,
	          target outcomes and the run's end state, in one transaction

Before any of a channel is written, `phone` is cleared on every User
marked `is_self` (storage.writer.drop_own_phone), so neither a raw column
nor a computed column holds the collecting account's phone number.

A failed transaction is rolled back and raises StorageError; channels
committed earlier stay. When `finish` fails, the run is marked
interrupted if that still succeeds.

In both mode (storage.dual), the channel transaction also sets the
target's files_state to 'pending', and `finish` sets aggregate_files to
'pending'; the mark_* methods set 'written' once the files are published,
and fail_target / interrupt_finished record a publication failure.
'''

# import modules
import os
import sqlite3
import sys
import uuid

# import local submodules
from telegram_tracker.storage.aggregates import reduce_results
from telegram_tracker.storage.provenance import RunProfile, refusal
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import BUSY_TIMEOUT_MS, app_version, \
	open_database, package_version, storage_error, transaction
from telegram_tracker.storage.sqlite.histories import SQLiteHistories
from telegram_tracker.storage.sqlite.mapping import entities, envelope, json_text, \
	merged_entities, message_rows, participants_count, peer_row
from telegram_tracker.storage.sqlite.schema import SCHEMA_VERSION, SERIALIZER_VERSION
from telegram_tracker.storage.status import utc_now
from telegram_tracker.storage.writer import drop_own_phone


# A min object never replaces a full one; any other copy replaces the
# stored object and the run that supplied it. Every sighting moves
# last_seen_run. (SET expressions read the row as it was.)
UPSERT_PEER = '''
INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, last_seen_run)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (peer_kind, peer_id) DO UPDATE SET
  constructor = CASE WHEN excluded.is_min = 1 AND peers.is_min = 0 THEN peers.constructor ELSE excluded.constructor END,
  raw = CASE WHEN excluded.is_min = 1 AND peers.is_min = 0 THEN peers.raw ELSE excluded.raw END,
  run_id = CASE WHEN excluded.is_min = 1 AND peers.is_min = 0 THEN peers.run_id ELSE excluded.run_id END,
  is_min = CASE WHEN excluded.is_min = 1 AND peers.is_min = 0 THEN 0 ELSE excluded.is_min END,
  last_seen_run = excluded.last_seen_run
'''

UPSERT_MESSAGE = '''
INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, ?, ?, ?)
ON CONFLICT (channel_id, message_id) DO UPDATE SET run_id = excluded.run_id, raw = excluded.raw
'''

UPSERT_SNAPSHOT = '''
INSERT INTO channel_snapshots (channel_id, run_id, observed_at, raw) VALUES (?, ?, ?, ?)
ON CONFLICT (channel_id) DO UPDATE SET
  run_id = excluded.run_id, observed_at = excluded.observed_at, raw = excluded.raw
'''

REPLACE_HISTORY = '''
INSERT INTO histories (channel_id, run_id, profile_name, profile_id, mode, window_bounds, envelope,
  message_count, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
ON CONFLICT (channel_id) DO UPDATE SET
  run_id = excluded.run_id, profile_name = excluded.profile_name, profile_id = excluded.profile_id,
  mode = excluded.mode, window_bounds = excluded.window_bounds, envelope = excluded.envelope,
  updated_at = excluded.updated_at
'''


class SQLiteWriter:
	'''
	The run's writer for the database at `path`; see the module docstring.
	`profile` (RunProfile or None) is recorded on the run and on every
	history the run replaces or creates. `storage` is the run's storage
	mode: 'sqlite', or 'both' when storage.dual.DualWriter also writes the
	files and records their publication (files_state, aggregate_files).
	'''
	def __init__(self, con, path, run_id, profile=None, parameters=None, *, storage='sqlite', run_uuid=None,
			stale_runs=0, lease=None, dataset_uuid=None):
		self.con = con
		self.path = path
		self.run_id = run_id
		self.run_uuid = run_uuid
		self.storage = storage
		self.stale_runs = stale_runs
		self.lease = lease
		self.dataset_uuid = dataset_uuid
		self.profile = profile or RunProfile()
		self.parameters = parameters or {}
		self.histories = SQLiteHistories(con, profile)
		self.committed = []
		self.not_found = []
		self.aggregates = None
		self.finalized = False

	@classmethod
	def open(
			cls, path, *, output_folder=None, profile=None, started_at=None, targets=(),
			parameters=None, protected=(), busy_timeout_ms=BUSY_TIMEOUT_MS, storage='sqlite',
			stale_reason=None, output_binding=None, lease=None):
		'''
		Open the database (see connection.open_database; a missing one is
		created, recording `output_binding`) and record the run. Use as a
		collector writer factory: functools.partial(SQLiteWriter.open, path).

		stale_reason -> when given, runs still recorded as running are first
			marked interrupted with this stop reason, in the transaction that
			records this run; their number is `stale_runs`. Only a caller that
			holds the database lease may pass it (storage.destination): then
			no other process is writing, and a running run is one whose
			process ended without finishing.
		lease -> the held database lease (sqlite.lease.DatabaseLease),
			released by `close`; on a failure here the caller still holds it
		'''
		parameters = parameters or {}
		con = open_database(
			path, create=True, protected=protected, busy_timeout_ms=busy_timeout_ms,
			output_binding=output_binding
		)
		try:
			record = (profile or RunProfile()).record()
			run_uuid = str(uuid.uuid4())
			with transaction(con, 'record the run'):
				dataset_uuid = con.execute('SELECT dataset_uuid FROM schema_meta').fetchone()[0]
				stale = 0
				if stale_reason is not None:
					stale = con.execute(
						"UPDATE collection_runs SET state = 'interrupted', stop_reason = ? WHERE state = 'running'",
						(stale_reason,)
					).rowcount
				run_id = con.execute(
					'INSERT INTO collection_runs (run_uuid, profile_name, profile_id, state, mode, '
					'storage, started_at, parameters, app_version, telethon_version, serializer_version) '
					"VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
					(
						run_uuid, record['profile_name'], record['profile_id'],
						parameters.get('mode', 'full'), storage, started_at or utc_now(), json_text(parameters),
						app_version(), package_version('telethon'), SERIALIZER_VERSION
					)
				).lastrowid
				con.executemany(
					"INSERT INTO run_targets (run_id, input_index, input, status) VALUES (?, ?, ?, 'pending')",
					[(run_id, index, target) for index, target in enumerate(targets)]
				)
		except BaseException as exc:
			con.close()
			if isinstance(exc, (ValueError, TypeError)):
				raise StorageError(f'cannot record the run in {path}: {exc}') from None
			raise
		return cls(
			con, path, run_id, profile, parameters, storage=storage, run_uuid=run_uuid, stale_runs=stale,
			lease=lease, dataset_uuid=dataset_uuid
		)

	def status_fields(self):
		'''
		What collection-status.json records about this database (see
		storage.writer.OutputWriter.status_fields).
		'''
		return {
			'storage': self.storage,
			'database': {
				'file': os.path.basename(self.path),
				'schema_version': SCHEMA_VERSION,
				'dataset_uuid': self.dataset_uuid,
				'run_uuid': self.run_uuid
			}
		}

	'''

	Channels

	'''

	def commit(self, result, *, files_state=None):
		'''
		Store one ChannelResult in one transaction. Returns the takeover
		entry of a replaced history (as storage.provenance) or None.
		`files_state` ('pending' in both mode) is set on the target's row
		in the same transaction.
		'''
		for payload in (result.channel_data, result.history, result.fetched):
			if payload is not None:
				drop_own_phone(payload)
		try:
			with transaction(self.con, f'save {result.target}'):
				takeover = self._write_channel(result, files_state)
		except (ValueError, TypeError) as exc:
			raise StorageError(f'cannot save {result.target}: {exc}') from None
		self.committed.append(result)
		return takeover

	def _write_channel(self, result, files_state=None):
		channel_id = result.channel_id
		self.con.execute(
			UPSERT_SNAPSHOT, (channel_id, self.run_id, utc_now(), json_text(result.channel_data))
		)
		self._upsert_peers(entities(result.channel_data))

		takeover = None
		if result.history_mode in ('replaced', 'created'):
			takeover = self._replace_history(channel_id, result.history, result.history_mode)
		elif result.history_mode == 'merged':
			self._merge_history(channel_id, result.fetched)

		takeover_record = takeover or {}
		updated = self.con.execute(
			"UPDATE run_targets SET status = 'succeeded', channel_id = ?, history_mode = ?, min_id = ?, "
			'messages = ?, participants_count = ?, reason = NULL, takeover = ?, '
			'previous_profile_name = ?, previous_profile_id = ?, files_state = ? '
			'WHERE run_id = ? AND input_index = ?',
			(
				channel_id, result.history_mode, result.min_id, result.messages,
				participants_count(result.channel_data), 1 if takeover else 0,
				takeover_record.get('previous_profile_name'), takeover_record.get('previous_profile_id'),
				files_state, self.run_id, result.index
			)
		).rowcount
		if updated != 1:
			raise ValueError(f'the run has no target {result.index}')
		return takeover

	def _replace_history(self, channel_id, history, mode):
		'''
		Full, window or created: the channel's messages and entity
		membership are replaced by `history`, recorded with this run's
		profile. A replace of a history this run could not have updated is
		a takeover.
		'''
		previous = self.con.execute(
			'SELECT profile_name, profile_id FROM histories WHERE channel_id = ?', (channel_id,)
		).fetchone()
		takeover = None
		if mode == 'replaced' and previous is not None:
			if refusal({'profile_name': previous[0], 'profile_id': previous[1]}, self.profile) is not None:
				takeover = {
					'takeover': True,
					'previous_profile_name': previous[0],
					'previous_profile_id': previous[1]
				}

		window = self.parameters.get('window') if mode == 'replaced' else None
		self.con.execute('DELETE FROM history_entities WHERE channel_id = ?', (channel_id,))
		self.con.execute('DELETE FROM messages WHERE channel_id = ?', (channel_id,))
		self.con.execute(REPLACE_HISTORY, (
			channel_id, self.run_id, self.profile.name, self.profile.id, mode,
			None if window is None else json_text(window), json_text(envelope(history)), utc_now()
		))
		self.con.executemany(UPSERT_MESSAGE, message_rows(channel_id, history['messages'], self.run_id))
		found = entities(history)
		self._upsert_peers(found)
		self._set_entities(channel_id, [(name, key) for name, key, _ in found])
		self._count_messages(channel_id)
		return takeover

	def _merge_history(self, channel_id, fetched):
		'''
		--update: upsert the fetched messages (fetched copies win), put the
		fetched entities first in the membership, take the fetched
		envelope. The history's profile and window stay.
		'''
		self.con.executemany(UPSERT_MESSAGE, message_rows(channel_id, fetched['messages'], self.run_id))
		found = entities(fetched)
		self._upsert_peers(found)
		earlier = [
			(name, (kind, peer_id)) for name, kind, peer_id in self.con.execute(
				'SELECT list, peer_kind, peer_id FROM history_entities WHERE channel_id = ? '
				'ORDER BY list, ordinal', (channel_id,)
			)
		]
		self._set_entities(channel_id, merged_entities([(name, key) for name, key, _ in found], earlier))
		self.con.execute(
			"UPDATE histories SET run_id = ?, mode = 'merged', envelope = ?, updated_at = ? "
			'WHERE channel_id = ?',
			(self.run_id, json_text(envelope(fetched)), utc_now(), channel_id)
		)
		self._count_messages(channel_id)

	def _upsert_peers(self, found):
		self.con.executemany(UPSERT_PEER, [peer_row(key, entity, self.run_id) for _, key, entity in found])

	def _set_entities(self, channel_id, members):
		self.con.execute('DELETE FROM history_entities WHERE channel_id = ?', (channel_id,))
		ordinals = {}
		rows = []
		for name, (kind, peer_id) in members:
			ordinal = ordinals.get(name, 0)
			ordinals[name] = ordinal + 1
			rows.append((channel_id, name, ordinal, kind, peer_id))
		self.con.executemany(
			'INSERT INTO history_entities (channel_id, list, ordinal, peer_kind, peer_id) '
			'VALUES (?, ?, ?, ?, ?)', rows
		)

	def _count_messages(self, channel_id):
		self.con.execute(
			'UPDATE histories SET message_count = (SELECT count(*) FROM messages WHERE channel_id = ?) '
			'WHERE channel_id = ?', (channel_id, channel_id)
		)

	def has_history(self, target, channel_id):
		if channel_id is None:
			return False
		try:
			return self.con.execute(
				'SELECT 1 FROM histories WHERE channel_id = ?', (channel_id,)
			).fetchone() is not None
		except sqlite3.Error as exc:
			raise storage_error(exc, f'read the history of {target}') from None

	def record_not_found(self, index, target):
		self.not_found.append((index, target))

	'''

	Run

	'''

	def finalize(self, outcomes):
		'''
		Replay the committed results in input order and store the run's
		observations. Returns the Aggregates; `finish` stores the rest.
		'''
		agg = reduce_results(self.committed, outcomes, self.not_found)
		self.finalized = True
		self.record_observations(agg)
		for chat_id, reason in agg.skipped:
			print(f'> Skipped metadata for chat {chat_id}: {reason}', file=sys.stderr)
		return agg

	def record_observations(self, agg):
		'''
		Store the observations of `agg`, the run's Aggregates, in one
		transaction; `finish` then stores the rest of it.
		'''
		with transaction(self.con, "save the run's observations"):
			self.con.executemany(
				'INSERT INTO run_observations (run_id, seq, input_index, kind, page, channel_id, '
				'username, parent_channel_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
				[(self.run_id, seq, *observation) for seq, observation in enumerate(agg.observations)]
			)
		self.aggregates = agg

	def finish(self, state, *, summary, stop_reason, requests, targets):
		'''
		Store the aggregates (when finalize succeeded), the request
		statistics, the outcome of every target not committed, and the
		run's end state, in one transaction. `targets` are the sidecar's
		target entries. In both mode, stored aggregates set
		aggregate_files to 'pending' in that transaction.
		'''
		try:
			with transaction(self.con, 'finish the run'):
				if self.aggregates is not None:
					self._write_aggregates(self.aggregates)
					if self.storage == 'both':
						self.con.execute(
							"UPDATE collection_runs SET aggregate_files = 'pending' WHERE run_id = ?",
							(self.run_id,)
						)
				self.con.executemany(
					'INSERT INTO run_requests (run_id, method_class, requests, retries, flood_waits, '
					'flood_wait_seconds) VALUES (?, ?, ?, ?, ?, ?)',
					[
						(self.run_id, name, s['requests'], s['retries'], s['flood_waits'], s['flood_wait_seconds'])
						for name, s in (requests or {}).items()
					]
				)
				self.con.executemany(
					'UPDATE run_targets SET status = ?, reason = ?, alias_of = ? '
					'WHERE run_id = ? AND input_index = ?',
					[
						(entry['status'], entry.get('reason'), entry.get('alias_of'), self.run_id, index)
						for index, entry in enumerate(targets) if entry['status'] != 'succeeded'
					]
				)
				self.con.execute(
					'UPDATE collection_runs SET state = ?, finished_at = ?, summary = ?, stop_reason = ? '
					'WHERE run_id = ?',
					(state, utc_now(), json_text(summary), stop_reason, self.run_id)
				)
		except (StorageError, ValueError, TypeError) as exc:
			error = exc if isinstance(exc, StorageError) else StorageError(f'cannot finish the run: {exc}')
			self._mark_interrupted(stop_reason or f'storage error: {error}')
			raise error from None

	def _write_aggregates(self, agg):
		run = self.run_id
		self.con.executemany(
			'INSERT INTO run_counters (run_id, channel_id, row_order, username, counter, from_messages, '
			'channel_request, targeted_by, sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
			[
				(
					run, chat_id, order, entry['username'], entry['counter'], entry['from_messages'],
					entry['channel_request'], json_text(entry['channel_req_targeted_by']['channels']),
					json_text(entry['source'])
				)
				for order, (chat_id, entry) in enumerate(agg.counter.items())
			]
		)
		rows = []
		for index, row in enumerate(agg.metadata):
			drop_own_phone(row)
			rows.append((run, index, row['id'], json_text(row)))
		self.con.executemany(
			'INSERT INTO collected_chat_rows (run_id, row_index, channel_id, metadata) VALUES (?, ?, ?, ?)',
			rows
		)
		self.con.executemany(
			'INSERT INTO collection_errors (run_id, error_index, category, input_index, peer_kind, '
			'peer_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?)',
			[
				(run, index, category, input_index, None if chat_id is None else 'channel', chat_id, reason)
				for index, (category, input_index, chat_id, reason) in enumerate(agg.errors)
			]
		)

	def _mark_interrupted(self, reason, *, finished=False):
		'''
		Best effort: record the run as interrupted, while it is still
		running or, with `finished`, also after its end state was stored.
		'''
		try:
			with transaction(self.con, 'mark the run interrupted'):
				self.con.execute(
					"UPDATE collection_runs SET state = 'interrupted', stop_reason = ?, finished_at = ? "
					"WHERE run_id = ? AND (state = 'running' OR ?)",
					(reason, utc_now(), self.run_id, finished)
				)
		except StorageError:
			pass

	'''

	Publication state (both mode)

	'''

	def mark_target_written(self, index):
		'''
		The files of target `index` are published: files_state 'written'.
		'''
		with transaction(self.con, 'record the published files'):
			self.con.execute(
				"UPDATE run_targets SET files_state = 'written' WHERE run_id = ? AND input_index = ?",
				(self.run_id, index)
			)

	def mark_aggregates_written(self):
		with transaction(self.con, 'record the published aggregate files'):
			self.con.execute(
				"UPDATE collection_runs SET aggregate_files = 'written' WHERE run_id = ?", (self.run_id,)
			)

	def fail_target(self, index, reason):
		'''
		Best effort after a committed channel's files could not be
		published: its outcome becomes failed with `reason`, its
		files_state stays 'pending', and it leaves `committed`, so the
		run's aggregates leave it out as the files do.
		'''
		self.committed = [r for r in self.committed if r.index != index]
		try:
			with transaction(self.con, 'record the failed publication'):
				self.con.execute(
					"UPDATE run_targets SET status = 'failed', reason = ? WHERE run_id = ? AND input_index = ?",
					(reason, self.run_id, index)
				)
		except StorageError:
			pass

	def interrupt_finished(self, reason):
		'''
		Best effort after the aggregate files could not be published
		although the run's end state was stored.
		'''
		self._mark_interrupted(reason, finished=True)

	def close(self):
		try:
			self.con.close()
		finally:
			if self.lease is not None:
				self.lease.__exit__(None, None, None)
				self.lease = None
