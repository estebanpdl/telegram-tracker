# -*- coding: utf-8 -*-
'''
Opening, creating and writing a data database.

`open_database` classifies the file first (schema.classify) and connects
only to a missing path (creating it when asked) or a current database.
After connecting it checks the SQLite version and JSON1, the identity
pragmas, every schema object against the frozen DDL, and the schema_meta
row. Each connection enables foreign keys and a busy timeout and runs in
autocommit mode; writes go through `transaction`.

A new database is built in a sibling temporary file and moved into place
only when complete and checked, without replacing a file that appeared
meanwhile, so an interrupted creation never leaves a half-made database
at the path.
'''

# import modules
import contextlib
import os
import pathlib
import sqlite3
import uuid

from importlib import metadata

# import local submodules
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.schema import APPLICATION_ID, CURRENT, MIN_SQLITE_VERSION, \
	MISSING, SCHEMA_VERSION, DatabaseRefused, check_path, classify, expected_objects, \
	refusal_message, schema_objects, schema_sql
from telegram_tracker.storage.status import utc_now
from telegram_tracker.utils import discard


BUSY_TIMEOUT_MS = 5000
# os.rename refuses an existing destination on Windows, but may replace it on Unix.
_RENAME_REFUSES_EXISTING = os.name == 'nt'


class DatabaseBusy(StorageError):
	'''
	Another connection held the database longer than the busy timeout.
	'''


class InterruptedWrite(StorageError):
	'''
	A read-only connection was refused: the file has an unfinished write
	(see interrupted_write), which only a writer can roll back.
	'''


def interrupted_write(path):
	'''
	The rollback journal beside the SQLite file `path` when it is not
	empty, else None. A process killed inside a write transaction leaves
	one (a hot journal), and so does a write still in progress. A
	read-only connection cannot roll it back and fails to read the file,
	so checks that only read leave such a file to the next writer, which
	rolls it back once it holds the folder.
	'''
	journal = f'{path}-journal'
	try:
		return journal if os.lstat(journal).st_size > 0 else None
	except OSError:
		return None


def refuse_interrupted_write(path, recovered_by='the next collect run into this folder'):
	'''
	Raise InterruptedWrite when `path` has an unfinished write; called
	before a read-only connection. `recovered_by` names the runs that roll
	such a write back.
	'''
	journal = interrupted_write(path)
	if journal is not None:
		raise InterruptedWrite(
			f'{path} has an unfinished write ({os.path.basename(journal)}): a run stopped while '
			'writing it, or one is writing it now. Reading it needs that write rolled back, which '
			f'{recovered_by} does. It was left untouched.'
		)


def package_version(name):
	try:
		return metadata.version(name)
	except metadata.PackageNotFoundError:
		return 'unknown'


def app_version():
	return package_version('telegram-tracker')


def storage_error(exc, action):
	'''
	StorageError for an sqlite3 error raised while doing `action`. The
	message holds SQLite's error text, never bound values.
	'''
	name = getattr(exc, 'sqlite_errorname', '')
	if name.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED')):
		return DatabaseBusy(f'cannot {action}: the database is in use by another process ({exc})')
	if name.startswith(('SQLITE_CORRUPT', 'SQLITE_NOTADB')):
		return DatabaseRefused(f'cannot {action}: the database is damaged ({exc})')
	return StorageError(f'cannot {action}: {exc}')


def connect(path, busy_timeout_ms=BUSY_TIMEOUT_MS, *, readonly=False):
	'''
	Connect to `path` (already checked by the caller) in autocommit mode
	with foreign keys on and the busy timeout set; with `readonly`,
	through a read-only URI built here from the checked path.
	'''
	target, uri = path, False
	if readonly:
		target, uri = pathlib.Path(os.path.abspath(path)).as_uri() + '?mode=ro', True
	try:
		con = sqlite3.connect(target, timeout=busy_timeout_ms / 1000, isolation_level=None, uri=uri)
	except sqlite3.Error as exc:
		raise storage_error(exc, f'open {path}') from None
	try:
		con.execute('PRAGMA foreign_keys = ON')
		con.execute(f'PRAGMA busy_timeout = {int(busy_timeout_ms)}')
		if con.execute('PRAGMA foreign_keys').fetchone() != (1,):
			raise DatabaseRefused('this SQLite library does not enforce foreign keys')
	except BaseException as exc:
		con.close()
		if isinstance(exc, sqlite3.Error):
			raise storage_error(exc, f'open {path}') from None
		raise
	return con


def check_library(con=None):
	'''
	Refuse an SQLite older than 3.31 (generated columns) or without JSON1.
	The probe runs on `con` or an in-memory database, never on a data
	file, so a busy or damaged file is not mistaken for a missing JSON1.
	'''
	if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
		raise DatabaseRefused(
			f'SQLite {sqlite3.sqlite_version} is too old; the database needs 3.31 or newer'
		)
	probe = con if con is not None else sqlite3.connect(':memory:')
	try:
		probe.execute("SELECT json_valid('{}'), json_extract('[1]', '$[0]'), json_type('[]')").fetchone()
	except sqlite3.OperationalError:
		raise DatabaseRefused('this SQLite library has no JSON functions (JSON1)') from None
	finally:
		if con is None:
			probe.close()


def verify(con, path):
	'''
	Check an open database against schema version 1: identity pragmas,
	every table, index, view and trigger exactly as the DDL creates them,
	and one schema_meta row of version 1.
	'''
	check_library()
	try:
		identity = (
			con.execute('PRAGMA application_id').fetchone()[0],
			con.execute('PRAGMA user_version').fetchone()[0]
		)
		objects = schema_objects(con)
		meta = con.execute('SELECT schema_version FROM schema_meta').fetchall() if (
			('table', 'schema_meta') in objects) else []
	except sqlite3.Error as exc:
		raise storage_error(exc, f'read {path}') from None

	if identity != (APPLICATION_ID, SCHEMA_VERSION):
		raise DatabaseRefused(f'{path} changed while it was opened; it was left untouched.')
	if objects != expected_objects():
		raise DatabaseRefused(
			f'{path} is damaged: its tables do not match schema version {SCHEMA_VERSION}. '
			'It was left untouched.'
		)
	if meta != [(SCHEMA_VERSION,)]:
		raise DatabaseRefused(f'{path} is damaged: its schema_meta row is missing or wrong.')


def open_database(
		path, *, create=False, protected=(), busy_timeout_ms=BUSY_TIMEOUT_MS, output_binding=None,
		readonly=False):
	'''
	Connect to the data database at `path`. A missing file is created
	when `create` is set (only once the destination is locked; it records
	`output_binding`) and refused otherwise. Raises DatabaseRefused for
	any other file that is not a current data database, leaving it
	untouched, and StorageError when it cannot be read. With `readonly`,
	the connection cannot write (a check before the destination is
	locked).
	'''
	found = classify(path, protected)
	if found.kind == MISSING:
		if not create or readonly:
			raise DatabaseRefused(f'{path} does not exist')
		create_database(path, output_binding=output_binding, busy_timeout_ms=busy_timeout_ms)
	elif found.kind != CURRENT:
		raise DatabaseRefused(refusal_message(path, found))
	if readonly:
		refuse_interrupted_write(path)

	con = connect(path, busy_timeout_ms, readonly=readonly)
	try:
		verify(con, path)
	except BaseException:
		con.close()
		raise
	return con


def create_database(path, *, output_binding=None, busy_timeout_ms=BUSY_TIMEOUT_MS):
	'''
	Create a schema version 1 database at `path`, which must not exist:
	build it in a sibling temporary file, check it, then link it into
	place (a file that appeared meanwhile is refused, never replaced).
	'''
	check_path(path)
	parent = os.path.dirname(os.path.abspath(path))
	tmp = os.path.join(parent, f'.{os.path.basename(path)}.{uuid.uuid4().hex}.creating')
	try:
		con = connect(tmp, busy_timeout_ms)
		try:
			check_library()
			con.executescript(schema_sql())
			now = utc_now()
			with transaction(con, f'create {path}'):
				con.execute(
					'INSERT INTO schema_meta (singleton, schema_version, dataset_uuid, created_at, '
					'updated_at, app_version, output_binding) VALUES (1, ?, ?, ?, ?, ?, ?)',
					(SCHEMA_VERSION, str(uuid.uuid4()), now, now, app_version(), output_binding)
				)
			verify(con, tmp)
		except sqlite3.Error as exc:
			raise storage_error(exc, f'create {path}') from None
		finally:
			con.close()
		link_new(tmp, path)
	finally:
		discard(tmp)


def link_new(tmp, path):
	'''
	Publish the finished file `tmp` at `path` without replacing anything
	there: a hard link, else a rename that refuses an existing file
	(Windows). Raises DatabaseRefused when `path` exists; `tmp` is left
	for the caller to remove.
	'''
	try:
		os.link(tmp, path)
	except FileExistsError:
		raise DatabaseRefused(f'{path} appeared while it was being created; it was left untouched.') from None
	except OSError:
		# Without hard links, only a non-replacing rename can publish safely.
		if os.path.lexists(path):
			raise DatabaseRefused(f'{path} appeared while it was being created; it was left untouched.') from None
		if not _RENAME_REFUSES_EXISTING:
			raise StorageError(
				f'cannot create {path}: hard-link publication failed and this platform has no '
				'safe non-replacing rename; choose an output on a filesystem supporting hard links'
			) from None
		try:
			os.rename(tmp, path)
		except OSError as exc:
			raise StorageError(f'cannot create {path}: {exc}') from None


@contextlib.contextmanager
def transaction(con, action):
	'''
	One write transaction (BEGIN IMMEDIATE ... COMMIT). Any exception rolls
	it back; an sqlite3 error becomes StorageError (DatabaseBusy for a
	busy database).
	'''
	try:
		con.execute('BEGIN IMMEDIATE')
	except sqlite3.Error as exc:
		raise storage_error(exc, action) from None
	try:
		yield con
		con.execute('COMMIT')
	except BaseException as exc:
		if con.in_transaction:
			with contextlib.suppress(sqlite3.Error):
				con.execute('ROLLBACK')
		if isinstance(exc, sqlite3.Error):
			raise storage_error(exc, action) from None
		raise
