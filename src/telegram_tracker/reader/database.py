# -*- coding: utf-8 -*-
'''
Reading a collection folder's database (<data>/telegram-tracker.sqlite3)
for the builders, offline and read only.

The reader holds the database lease (storage.sqlite.lease) while it
reads, so no collection or upgrade writes meanwhile, and reads through
one read-only connection inside one read transaction: one consistent
snapshot. It never creates, repairs or upgrades a database: a missing,
older, newer, foreign or damaged file is refused as it is.

Under the lease it checks again what the folder's manifest and sidecar
say: the database is the manifest's (dataset UUID), its latest run is
the one collection-status.json describes and finished (complete or
partial), and no file publication is pending. A folder moved or copied
since collection is readable; a note says so.

Histories are the current ones (`histories`, `messages`): run tables
never add rows. Messages are fetched in batches of BATCH_SIZE; one
history is held in memory at a time. collected_chats.csv is rebuilt
from the stored runs (`collected_chat_rows` and `run_counters`), each
run appended through storage.aggregates.collected_chats_update as the
file writer appends it; that text is held in memory.
'''

# import modules
import io
import json
import os

# import local submodules
from telegram_tracker.reader.records import SQLITE, ChannelHistory, ChatsInput, HistoryProvenance
from telegram_tracker.storage.destination import same_folder
from telegram_tracker.storage.manifest import DATABASE_FILE, MANIFEST_FILE
from telegram_tracker.storage.records import COMPLETE, PARTIAL, STATUS_FILE, StorageError
from telegram_tracker.utils import DataInputError


BATCH_SIZE = 500

# Each history with the input of the run target that last wrote it: the
# name its files are saved under.
HISTORIES = '''
SELECT h.channel_id, h.message_count, h.mode, h.profile_name, h.profile_id, r.run_uuid,
  (SELECT t.input FROM run_targets t WHERE t.run_id = h.run_id AND t.channel_id = h.channel_id
     AND t.status = 'succeeded' ORDER BY t.input_index LIMIT 1),
  (SELECT t.takeover FROM run_targets t WHERE t.run_id = h.run_id AND t.channel_id = h.channel_id
     AND t.status = 'succeeded' ORDER BY t.input_index LIMIT 1)
FROM histories h JOIN collection_runs r ON r.run_id = h.run_id
ORDER BY h.channel_id
'''


def history_order(data_path, name):
	'''
	The key the files builder sorts its messages files by.
	'''
	return os.path.join(data_path, name, f'{name}_messages.json')


class DatabaseCollection:
	'''
	The database of `data_path`, for use as a context manager; see the
	module docstring. `manifest` and `status` are the folder's storage
	manifest and sidecar, read before the lease was taken.
	'''
	source = SQLITE

	def __init__(self, data_path, manifest, status):
		self.data_path = data_path
		self.manifest = manifest
		self.status = status
		self.path = os.path.join(data_path, DATABASE_FILE)
		self.notes = []
		self.lease = None
		self.con = None
		self.meta = {}

	def __enter__(self):
		from telegram_tracker.storage.sqlite.lease import DatabaseLease

		lease = DatabaseLease(self.path)
		try:
			lease.__enter__()
		except StorageError as exc:
			raise DataInputError(str(exc)) from None
		self.lease = lease
		try:
			self._open()
			self._check()
		except BaseException:
			self.__exit__(None, None, None)
			raise
		return self

	def __exit__(self, *exc_info):
		try:
			if self.con is not None:
				self.con.close()
				self.con = None
		finally:
			if self.lease is not None:
				self.lease.__exit__(None, None, None)
				self.lease = None
		return False

	def _open(self):
		from telegram_tracker.storage.sqlite.connection import open_database

		if not os.path.lexists(self.path):
			raise DataInputError(
				f'{self.data_path} was collected with --storage {self.manifest["storage"]}, but '
				f'{self.path} is missing. Keep the folder for inspection.'
			)
		try:
			self.con = open_database(self.path, readonly=True)
		except StorageError as exc:
			raise DataInputError(str(exc)) from None
		# a deferred transaction: the snapshot starts at the first read
		self.con.execute('BEGIN')

	def _rows(self, sql, *args):
		try:
			return self.con.execute(sql, args).fetchall()
		except Exception as exc:
			raise DataInputError(f'cannot read {self.path}: {exc}') from None

	def _check(self):
		(dataset_uuid, binding), = self._rows('SELECT dataset_uuid, output_binding FROM schema_meta')
		if dataset_uuid != self.manifest['dataset_uuid']:
			raise DataInputError(
				f'{self.path} is not the database {self.data_path}/{MANIFEST_FILE} was created '
				'with (another dataset). It was left untouched.'
			)
		if binding is not None and not same_folder(binding, self.data_path):
			self.notes.append(
				f'{self.path} was collected into another folder ({binding}); reading this copy'
			)

		latest = self._rows(
			'SELECT run_uuid, state, mode, started_at, finished_at, profile_name, profile_id '
			'FROM collection_runs ORDER BY run_id DESC LIMIT 1'
		)
		if not latest:
			raise DataInputError(f'{self.path} holds no collection run; run collection first')
		run_uuid, state, mode, started_at, finished_at, profile_name, profile_id = latest[0]
		recorded = (self.status.get('database') or {}).get('run_uuid')
		if recorded != run_uuid:
			raise DataInputError(
				f'{self.data_path}/{STATUS_FILE} and {self.path} describe different runs, so '
				'the folder may have changed after collection. Keep it for inspection.'
			)
		if state not in (COMPLETE, PARTIAL):
			raise DataInputError(
				f'{self.path} says its last run is {state}, not finished. Build after a '
				'collection into this folder finishes, or keep the folder for inspection.'
			)

		pending = dict(self._rows('SELECT kind, count(*) FROM destination_pending GROUP BY kind'))
		if pending:
			raise DataInputError(
				f'{self.data_path} is blocked: an earlier run stopped after writing the database '
				'and before publishing the files, so the files and the database may disagree. '
				'Keep the folder for inspection.'
			)

		from telegram_tracker.storage.sqlite.schema import SCHEMA_VERSION

		self.meta = {
			'database': {
				'file': DATABASE_FILE,
				'dataset_uuid': dataset_uuid,
				'schema_version': SCHEMA_VERSION
			},
			'last_run': {
				'run_uuid': run_uuid,
				'state': state,
				'mode': mode,
				'started_at': started_at,
				'finished_at': finished_at,
				'profile_name': profile_name,
				'profile_id': profile_id
			}
		}

	'''

	Histories

	'''

	def histories(self):
		'''
		Yield a ChannelHistory per stored history, in the files builder's
		order (then by channel ID).
		'''
		found = []
		for channel_id, count, mode, profile_name, profile_id, run_uuid, name, takeover in \
				self._rows(HISTORIES):
			if name is None:
				raise DataInputError(
					f'{self.path}: the history of channel {channel_id} has no collection target '
					'that wrote it. Keep the folder for inspection.'
				)
			found.append((history_order(self.data_path, name), channel_id, name, HistoryProvenance(
				profile_name=profile_name, profile_id=profile_id, run=run_uuid, mode=mode,
				takeover=None if takeover is None else bool(takeover)
			)))

		for _, channel_id, name, provenance in sorted(found, key=lambda f: (f[0], f[1])):
			yield ChannelHistory(
				name=name,
				label=f'{self.path} (history of {name}, channel {channel_id})',
				channel_id=channel_id,
				provenance=provenance,
				load=lambda channel_id=channel_id: self.load_history(channel_id)
			)

	def load_history(self, channel_id):
		'''
		A stored history as a payload, shaped like a messages file: the
		stored response fields, the messages, newest first, fetched in
		batches, and the history's chats and users in stored order. Each
		chat or user is the latest stored copy of that peer (a full object
		is never replaced by a min one), not necessarily the copy the
		history's own response carried.
		'''
		try:
			envelope, = self.con.execute(
				'SELECT envelope FROM histories WHERE channel_id = ?', (channel_id,)
			).fetchone()
			payload = json.loads(envelope)
			messages = []
			cursor = self.con.execute(
				'SELECT raw FROM messages WHERE channel_id = ? ORDER BY message_id DESC', (channel_id,)
			)
			while batch := cursor.fetchmany(BATCH_SIZE):
				messages.extend(json.loads(raw) for raw, in batch)
			entities = self.con.execute(
				'SELECT e.list, p.raw FROM history_entities e '
				'JOIN peers p ON p.peer_kind = e.peer_kind AND p.peer_id = e.peer_id '
				'WHERE e.channel_id = ? ORDER BY e.list, e.ordinal', (channel_id,)
			).fetchall()
		except Exception as exc:
			raise DataInputError(f'cannot read {self.path}: {exc}') from None
		payload['messages'] = messages
		for list_name in ('chats', 'users'):
			payload[list_name] = [json.loads(raw) for name, raw in entities if name == list_name]
		return payload

	'''

	Collected chats

	'''

	def collected_chats(self):
		return ChatsInput(
			source=io.StringIO(self.collected_chats_text()),
			label=f'{self.path} (collected chats)'
		)

	def collected_chats_text(self):
		'''
		collected_chats.csv as the file writer would hold it after every
		stored run: each run's rows, merged with that run's counts, appended
		in run order. A run whose end was never stored has no rows.
		'''
		from telegram_tracker.storage.aggregates import collected_chats_update, counter_frame, \
			earlier_collected_chats, holds_no_rows

		text = None
		for run_id, in self._rows('SELECT DISTINCT run_id FROM collected_chat_rows ORDER BY run_id'):
			metadata = self._run_rows(run_id)
			has_rows = text is not None and not holds_no_rows(io.StringIO(text))
			df = collected_chats_update(
				metadata, counter_frame(self._run_counter(run_id)), has_rows,
				lambda: earlier_collected_chats(io.StringIO(text))
			)
			if df is not None:
				text = df.to_csv(index=False)
		if text is None:
			text = collected_chats_update([], None, False, None).to_csv(index=False)
		return text

	def _run_rows(self, run_id):
		rows = []
		try:
			cursor = self.con.execute(
				'SELECT metadata FROM collected_chat_rows WHERE run_id = ? ORDER BY row_index', (run_id,)
			)
			while batch := cursor.fetchmany(BATCH_SIZE):
				rows.extend(json.loads(raw) for raw, in batch)
		except Exception as exc:
			raise DataInputError(f'cannot read {self.path}: {exc}') from None
		return rows

	def _run_counter(self, run_id):
		'''
		The run's counter, as storage.aggregates builds it.
		'''
		counter = {}
		for channel_id, username, count, from_messages, channel_request, targeted_by, sources in self._rows(
				'SELECT channel_id, username, counter, from_messages, channel_request, targeted_by, sources '
				'FROM run_counters WHERE run_id = ? ORDER BY row_order', run_id):
			counter[channel_id] = {
				'username': username,
				'counter': count,
				'from_messages': from_messages,
				'channel_request': channel_request,
				'channel_req_targeted_by': {'channels': json.loads(targeted_by)},
				'source': json.loads(sources)
			}
		return counter

	def describe(self):
		return dict(self.meta)
