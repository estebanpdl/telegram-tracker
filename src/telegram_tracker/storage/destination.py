# -*- coding: utf-8 -*-
'''
A collection's destination: the output folder, its storage mode, and,
for `sqlite` and `both`, the database <output>/telegram-tracker.sqlite3.

	select_destination  the mode: --storage, or the one the folder
	                    recorded (storage.manifest); files for a folder
	                    without a manifest. A different explicit mode, or
	                    sqlite/both for a folder that already holds outputs,
	                    is refused (StorageChoiceRefused).
	check               before credentials, read only: the database is the
	                    folder's own (manifest dataset_uuid, output binding)
	                    and nothing is left unpublished ('pending').
	saved_histories     before credentials, read only: which targets already
	                    have a saved history that a window run replaces.
	check_unlocked      just before connecting: no database lease is held.
	open_writer         the collector's writer factory, called once the
	                    output folder is locked: for files, refuses a folder
	                    that gained a manifest meanwhile; otherwise takes the
	                    database lease,
	                    checks again, creates the database and the manifest
	                    for a new folder, and opens the run's writer. The
	                    writer releases the lease when it closes.

Lock order: output lock (.collection.lock), then the database lease. A
'pending' marker blocks collection in every mode until the folder is
replaced by a fresh one; deleting the lock files does not clear it. A run
still recorded as running when the lease is free is one whose process
ended (it was killed, or the computer stopped): the next run records it
as interrupted, keeps its rows, and goes on.
'''

# import modules
import os
import sys

# import local submodules
from telegram_tracker.storage.manifest import BOTH, DATABASE_FILE, FILES, MANIFEST_FILE, SQLITE, \
	read_manifest, write_manifest
from telegram_tracker.storage.records import LOCK_FILE, StorageError
from telegram_tracker.storage.status import utc_now


# A stored history for a target name (both parameters): a channel an earlier
# run resolved from that input, or a channel with that username. Usernames
# and targets compare case-insensitively, like Telegram usernames.
SAVED_HISTORY = (
	'SELECT 1 FROM histories WHERE channel_id IN ('
	'SELECT channel_id FROM run_targets WHERE input = ? COLLATE NOCASE AND channel_id IS NOT NULL '
	"UNION SELECT peer_id FROM peers WHERE peer_kind = 'channel' AND username = ? COLLATE NOCASE"
	') LIMIT 1'
)


class StorageChoiceRefused(Exception):
	'''
	The requested storage mode cannot be used for this output folder
	(an argument error: exit 2). `recorded` is the folder's mode.
	'''
	def __init__(self, message, recorded):
		super().__init__(message)
		self.recorded = recorded


class DestinationError(StorageError):
	'''
	The folder's database cannot be used: missing, not the folder's own,
	or not a current data database. Nothing was changed.
	'''


class DestinationBlocked(StorageError):
	'''
	An earlier run left file outputs unpublished ('pending'): the files
	and the database may disagree.
	'''


def canonical_folder(path):
	return os.path.realpath(os.path.abspath(path))


def same_folder(a, b):
	'''
	True when `a` and `b` are the same folder: equal canonical paths or,
	where the file system ignores case or Unicode normalization but the
	text differs (macOS), the same existing directory.
	'''
	if os.path.normcase(canonical_folder(a)) == os.path.normcase(canonical_folder(b)):
		return True
	try:
		return os.path.samefile(a, b)
	except OSError:
		return False


def select_destination(output_folder, requested=None, *, legacy=False):
	'''
	The Destination for `output_folder` with the --storage value
	`requested` (None: not given). Raises StorageChoiceRefused for a mode
	the folder cannot take and storage.manifest.ManifestError for an
	unreadable manifest. Reads the manifest and the folder listing only.
	'''
	manifest = read_manifest(output_folder)
	recorded = FILES if manifest is None else manifest['storage']
	if legacy and recorded != FILES:
		raise StorageChoiceRefused(
			f'{output_folder} was collected with --storage {recorded}; this form writes files only',
			recorded
		)
	if manifest is not None:
		if requested not in (None, recorded):
			raise StorageChoiceRefused(
				f'{output_folder} was collected with --storage {recorded}; it cannot take '
				f'--storage {requested}. Omit --storage to use {recorded}, or choose a fresh --output',
				recorded
			)
		return Destination(output_folder, recorded, manifest)

	storage = requested or FILES
	if storage != FILES and folder_entries(output_folder):
		raise StorageChoiceRefused(
			f'{output_folder} already holds files and has no {MANIFEST_FILE}: --storage '
			f'{storage} needs a new or empty --output (existing outputs are not converted)',
			recorded
		)
	return Destination(output_folder, storage, None)


def folder_entries(output_folder, ignore=()):
	try:
		return [name for name in os.listdir(output_folder) if name not in ignore]
	except FileNotFoundError:
		return []
	except NotADirectoryError:
		return [output_folder]


class Destination:
	'''
	One output folder and its storage mode; see the module docstring.
	`protected` paths (the session file) can never be opened as the
	database.
	'''
	def __init__(self, output_folder, storage, manifest, protected=()):
		self.output_folder = output_folder
		self.storage = storage
		self.manifest = manifest
		self.protected = tuple(protected)

	@property
	def uses_database(self):
		return self.storage in (SQLITE, BOTH)

	@property
	def database_path(self):
		return os.path.join(self.output_folder, DATABASE_FILE)

	def describe(self):
		if self.storage == FILES:
			return 'files'
		if self.storage == SQLITE:
			return f'sqlite ({self.database_path})'
		return f'both (files and {self.database_path})'

	'''

	Checks

	'''

	def check(self):
		'''
		Before credentials: the database checks below, read only. Nothing
		is created.
		'''
		if self.uses_database and self.manifest is not None:
			self._check_database()

	def saved_histories(self, targets):
		'''
		The targets, in order and once each, that already have a saved
		history here: <target>/<target>_messages.json for files; for sqlite,
		a history in the database of a channel an earlier run collected
		under that target name or whose stored username it is (histories
		are keyed by channel ID, which is only known after connecting);
		either for both. Read only, after `check`; nothing is created.
		'''
		targets = [t for t in dict.fromkeys(targets) if t]
		found = set()
		if self.storage in (FILES, BOTH):
			found.update(
				t for t in targets
				if os.path.isfile(os.path.join(self.output_folder, t, f'{t}_messages.json'))
			)
		if self.uses_database and self.manifest is not None:
			found.update(self._database_histories([t for t in targets if t not in found]))
		return [t for t in targets if t in found]

	def _database_histories(self, targets):
		from telegram_tracker.storage.sqlite.connection import open_database
		from telegram_tracker.storage.sqlite.schema import DatabaseRefused

		if not targets:
			return set()
		path = self.database_path
		try:
			con = open_database(path, protected=self.protected, readonly=True)
		except DatabaseRefused as exc:
			raise DestinationError(str(exc)) from None
		try:
			return {t for t in targets if con.execute(SAVED_HISTORY, (t, t)).fetchone()}
		except Exception as exc:
			raise DestinationError(f'cannot read {path}: {exc}') from None
		finally:
			con.close()

	def check_unlocked(self):
		'''
		Just before connecting: refuse a held database lease, with its own
		message (the output lock is checked by the collector).
		'''
		if self.uses_database:
			from telegram_tracker.storage.sqlite.lease import check_free

			check_free(self.database_path)

	def _check_database(self):
		'''
		The recorded database: present and current, created for this
		folder with the manifest's dataset UUID, and no 'pending' marker.
		'''
		from telegram_tracker.storage.sqlite.connection import open_database
		from telegram_tracker.storage.sqlite.schema import DatabaseRefused

		path = self.database_path
		if not os.path.lexists(path):
			raise DestinationError(
				f'{self.output_folder} was collected with --storage {self.storage}, but {path} '
				'is missing. Keep the folder for inspection and collect into a fresh --output.'
			)
		try:
			con = open_database(path, protected=self.protected, readonly=True)
		except DatabaseRefused as exc:
			raise DestinationError(str(exc)) from None
		try:
			dataset_uuid, binding = con.execute(
				'SELECT dataset_uuid, output_binding FROM schema_meta').fetchone()
			pending = dict(con.execute(
				'SELECT kind, count(*) FROM destination_pending GROUP BY kind').fetchall())
		except Exception as exc:
			raise DestinationError(f'cannot read {path}: {exc}') from None
		finally:
			con.close()

		if dataset_uuid != self.manifest['dataset_uuid']:
			raise DestinationError(
				f'{path} is not the database {self.output_folder}/{MANIFEST_FILE} was created '
				'with (another dataset). It was left untouched; collect into a fresh --output.'
			)
		if binding is not None and not same_folder(binding, self.output_folder):
			raise DestinationError(
				f'{path} was created for the folder {binding}, not {canonical_folder(self.output_folder)} '
				'(a moved or copied folder). It was left untouched; collect into a fresh --output.'
			)
		if pending:
			targets = pending.get('target', 0)
			parts = [f'{targets} channel(s)'] if targets else []
			if pending.get('aggregates'):
				parts.append('the aggregate files')
			raise DestinationBlocked(
				f'{self.output_folder} is blocked: an earlier run stopped after writing the '
				f'database and before publishing the files ({" and ".join(parts)}), so the files '
				'and the database may disagree. Keep the folder for inspection and collect into '
				f'a fresh --output. Deleting {LOCK_FILE} or the database lease does not clear this.'
			)

	'''

	Writer

	'''

	def open_writer(self, *, output_folder, profile, started_at, targets, parameters):
		'''
		The collector's writer factory (see collector.run_collection).
		'''
		from telegram_tracker.storage.writer import OutputWriter, open_file_writer

		if not self.uses_database:
			# a sqlite or both run may have set the folder up since `check`
			manifest = read_manifest(self.output_folder)
			if manifest is not None:
				raise DestinationError(
					f'{self.output_folder} was set up for --storage {manifest["storage"]} by another '
					'run meanwhile; nothing was written.'
				)
			return open_file_writer(
				output_folder=output_folder, profile=profile, started_at=started_at,
				targets=targets, parameters=parameters
			)

		from telegram_tracker.storage.dual import DualWriter
		from telegram_tracker.storage.sqlite.lease import DatabaseLease
		from telegram_tracker.storage.sqlite.writer import SQLiteWriter

		lease = DatabaseLease(self.database_path)
		lease.__enter__()
		try:
			database = self._open_leased(SQLiteWriter, lease, profile, started_at, targets, parameters)
		except BaseException:
			lease.__exit__(None, None, None)
			raise
		if database.stale_runs:
			print(
				f'> Note: {database.stale_runs} earlier run(s) in {self.database_path} never finished '
				'(the process ended); they are now recorded as interrupted.',
				file=sys.stderr
			)
		if self.storage == SQLITE:
			return database
		return DualWriter(OutputWriter(output_folder, profile, started_at), database)

	def _open_leased(self, writer_class, lease, profile, started_at, targets, parameters):
		'''
		Under the database lease: check again (the folder may have changed
		since `check`), then open the writer; a new folder gets its database
		and then its manifest.
		'''
		from telegram_tracker.storage.sqlite.lease import database_lease_path

		manifest = read_manifest(self.output_folder)
		if manifest is None:
			if self.manifest is not None:
				raise DestinationError(f'{self.output_folder}/{MANIFEST_FILE} disappeared; nothing was written.')
			own = (LOCK_FILE, os.path.basename(database_lease_path(self.database_path)))
			if folder_entries(self.output_folder, ignore=own):
				raise DestinationError(
					f'{self.output_folder} is no longer empty; nothing was written. Collect into a '
					'fresh --output.'
				)
		else:
			if manifest['storage'] != self.storage:
				raise DestinationError(
					f'{self.output_folder} was set up for --storage {manifest["storage"]} by another '
					'run meanwhile; nothing was written.'
				)
			self.manifest = manifest
			self._check_database()

		database = writer_class.open(
			self.database_path,
			profile=profile,
			started_at=started_at,
			targets=targets,
			parameters=parameters,
			protected=self.protected,
			storage=self.storage,
			stale_reason=f'stale: its process ended without finishing (found by the run started {started_at})',
			output_binding=canonical_folder(self.output_folder),
			lease=lease
		)
		if manifest is None:
			try:
				dataset_uuid = database.status_fields()['database']['dataset_uuid']
				write_manifest(self.output_folder, self.storage, dataset_uuid, utc_now())
				self.manifest = read_manifest(self.output_folder)
			except BaseException:
				# the caller releases the lease
				database.con.close()
				raise
		return database
