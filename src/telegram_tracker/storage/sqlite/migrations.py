# -*- coding: utf-8 -*-
'''
Schema versions and the explicit, offline upgrade path.

Collection never upgrades: connection.open_database refuses any version
but SCHEMA_VERSION. `migrate` is the only code that changes a database's
version, and only upward, one step at a time:

	1. back the database up through SQLite's backup API to
	   <name>.v<N>-<UTC time>.bak.sqlite3 beside it, and check the copy
	   (integrity_check, application ID, version);
	2. in one transaction, run the step from N to N+1, set user_version,
	   update schema_meta, record the step in `migrations` (with the
	   backup's path), and check foreign keys.

A failed step rolls back to version N; its backup stays. Backups are never
deleted. A version without a step to the next one, a newer version, and
any file that is not a data database are refused untouched. Schema
version 1 is the first: no step leads to it, so UPGRADES is empty until a
version 2 exists.
'''

# import modules
import os
import sqlite3

from datetime import datetime, timezone

# import local submodules
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import BUSY_TIMEOUT_MS, app_version, connect, \
	check_library, storage_error, transaction
from telegram_tracker.storage.sqlite.schema import APPLICATION_ID, CURRENT, MISSING, NEWER, OLDER, \
	SCHEMA_VERSION, DatabaseRefused, classify, refusal_message
from telegram_tracker.storage.sqlite.lease import DatabaseLease
from telegram_tracker.storage.status import utc_now
from telegram_tracker.utils import discard


# {version N: step(con)} upgrading N to N + 1 inside the caller's transaction
UPGRADES = {}


class MigrationFailed(StorageError):
	'''
	An upgrade step failed and was rolled back; the database keeps its
	earlier version and the backup stays.
	'''


def backup_path(path, version, now=None):
	'''
	<name>.v<version>-<UTC time>.bak.sqlite3 beside `path`.
	'''
	now = now or datetime.now(timezone.utc)
	base = os.path.basename(path)
	name = base[:-len('.sqlite3')] if base.endswith('.sqlite3') else base
	return os.path.join(os.path.dirname(path), f'{name}.v{version}-{now:%Y%m%dT%H%M%S.%fZ}.bak.sqlite3')


def backup_database(con, path, version):
	'''
	Copy the open database to a new backup file and check the copy.
	Returns the backup's path. A failed or unverifiable copy is removed.
	'''
	target = backup_path(path, version)
	if os.path.lexists(target):
		raise StorageError(f'cannot back up {path}: {target} exists')
	try:
		copy = sqlite3.connect(target)
		try:
			con.backup(copy)
			checks = (
				copy.execute('PRAGMA integrity_check').fetchall(),
				copy.execute('PRAGMA application_id').fetchone()[0],
				copy.execute('PRAGMA user_version').fetchone()[0]
			)
		finally:
			copy.close()
	except sqlite3.Error as exc:
		discard(target)
		raise storage_error(exc, f'back up {path}') from None
	if checks != ([('ok',)], APPLICATION_ID, version):
		discard(target)
		raise StorageError(f'cannot back up {path}: the copy did not verify')
	return target


def migrate(path, *, upgrades=None, target_version=SCHEMA_VERSION, busy_timeout_ms=BUSY_TIMEOUT_MS):
	'''
	Upgrade the data database at `path` to `target_version`, backing it
	up before every step (see the module docstring). Returns the list of
	backups made; an empty list when it already has that version. Holds
	the database lease (see lease.py) from before the first read to the
	last step, so no collection writes between the checks, the backup and
	the step transaction; a held lease raises DatabaseLocked.
	'''
	if classify(path).kind == MISSING:
		raise DatabaseRefused(f'{path} does not exist')
	with DatabaseLease(path):
		return _migrate_leased(path, UPGRADES if upgrades is None else upgrades, target_version, busy_timeout_ms)


def _migrate_leased(path, upgrades, target_version, busy_timeout_ms):
	found = classify(path)
	if found.kind == MISSING:
		raise DatabaseRefused(f'{path} does not exist')
	if found.kind not in (CURRENT, OLDER, NEWER):
		raise DatabaseRefused(refusal_message(path, found))
	version = found.version
	if version > target_version:
		raise DatabaseRefused(
			f'{path} has schema version {version}, newer than {target_version}; '
			'there are no downgrades. It was left untouched.'
		)
	missing = [v for v in range(version, target_version) if v not in upgrades]
	if missing:
		raise DatabaseRefused(
			f'{path} has schema version {version}, and no upgrade leads from version '
			f'{missing[0]}. It was left untouched.'
		)
	if version == target_version:
		return []

	con = connect(path, busy_timeout_ms)
	try:
		check_library()
		_check_before(con, path, version)
		backups = []
		while version < target_version:
			backup = backup_database(con, path, version)
			backups.append(backup)
			_apply(con, path, version, upgrades[version], backup)
			version += 1
		return backups
	finally:
		con.close()


def _check_before(con, path, version):
	try:
		state = (
			con.execute('PRAGMA application_id').fetchone()[0],
			con.execute('PRAGMA user_version').fetchone()[0],
			con.execute('PRAGMA integrity_check').fetchall()
		)
	except sqlite3.Error as exc:
		raise storage_error(exc, f'read {path}') from None
	if state != (APPLICATION_ID, version, [('ok',)]):
		raise DatabaseRefused(f'{path} did not pass its checks before the upgrade; it was left untouched.')


def _apply(con, path, version, step, backup):
	try:
		with transaction(con, f'upgrade {path} to version {version + 1}'):
			step(con)
			con.execute(f'PRAGMA user_version = {int(version) + 1}')
			now = utc_now()
			con.execute(
				'UPDATE schema_meta SET schema_version = ?, updated_at = ?, app_version = ?',
				(version + 1, now, app_version())
			)
			con.execute(
				'INSERT INTO migrations (version, applied_at, app_version, backup_path) VALUES (?, ?, ?, ?)',
				(version + 1, now, app_version(), backup)
			)
			if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
				raise MigrationFailed(f'upgrading {path} to version {version + 1} broke foreign keys')
	except MigrationFailed:
		raise
	except StorageError as exc:
		raise MigrationFailed(f'{exc}; {path} keeps version {version}, backup {backup}') from None
	except Exception as exc:
		raise MigrationFailed(
			f'upgrading {path} to version {version + 1} failed ({type(exc).__name__}: {exc}); '
			f'it keeps version {version}, backup {backup}'
		) from None
