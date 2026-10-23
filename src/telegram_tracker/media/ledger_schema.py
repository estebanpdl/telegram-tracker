# -*- coding: utf-8 -*-
'''
The media ledger file: identity, creation and checks.

ledger-v1.sql in this package is docs/media-ledger.sql. A file is
classified from its SQLite header before any connection (application_id
'TGTM', user_version 1), so a file that is not a ledger is never opened
for writing. A new ledger is built in a sibling temporary file with its
ledger_meta row, checked, and published without replacing anything at
the path. Every connection, the creating one included, turns on foreign
keys and recursive triggers; it is then checked against the DDL, object
by object, and must hold exactly one ledger_meta row.
'''

# import modules
import functools
import os
import sqlite3
import uuid

from dataclasses import dataclass
from importlib import resources

# import local submodules
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import app_version, check_library, connect, link_new, \
	refuse_interrupted_write, storage_error, transaction
from telegram_tracker.storage.sqlite.schema import CURRENT, MISSING, NEWER, OLDER, DatabaseRefused, \
	classify, schema_objects
from telegram_tracker.utils import discard


LEDGER_APPLICATION_ID = 1413960781  # 0x5447544D, 'TGTM'
LEDGER_VERSION = 1


class LedgerRefused(StorageError):
	'''
	The ledger cannot be used: not a media ledger, another version,
	damaged, or the ledger of another dataset. It was left untouched.
	'''


@dataclass(frozen=True)
class LedgerMeta:
	'''
	The ledger_meta row.
	'''
	ledger_uuid: str
	dataset_uuid: str
	storage: str
	output_binding: str | None
	created_at: str
	app_version: str


META_COLUMNS = ('ledger_uuid', 'dataset_uuid', 'storage', 'output_binding', 'created_at', 'app_version')


@functools.cache
def ledger_sql():
	'''
	The ledger version 1 DDL (docs/media-ledger.sql).
	'''
	return resources.files(__package__).joinpath('ledger-v1.sql').read_text(encoding='utf-8')


@functools.cache
def expected_objects():
	con = sqlite3.connect(':memory:')
	try:
		con.executescript(ledger_sql())
		return schema_objects(con)
	finally:
		con.close()


def ledger_connection(path, *, readonly=False):
	'''
	connect() with PRAGMA recursive_triggers on as well. Without it,
	REPLACE conflict resolution deletes the old row without firing the
	DELETE triggers, so INSERT OR REPLACE could rewrite a blob or a
	complete item that the triggers otherwise keep.
	'''
	con = connect(path, readonly=readonly)
	try:
		con.execute('PRAGMA recursive_triggers = ON')
		if con.execute('PRAGMA recursive_triggers').fetchone() != (1,):
			raise LedgerRefused('this SQLite library does not support recursive triggers')
	except BaseException as exc:
		con.close()
		if isinstance(exc, sqlite3.Error):
			raise storage_error(exc, f'open {path}') from None
		raise
	return con


def classify_ledger(path):
	'''
	The schema.Classification of the file at `path` as a media ledger.
	'''
	try:
		return classify(path, application_id=LEDGER_APPLICATION_ID, schema_version=LEDGER_VERSION)
	except DatabaseRefused as exc:
		raise LedgerRefused(str(exc)) from None


def refusal(path, found):
	if found.kind == NEWER:
		return (
			f'{path} is media ledger version {found.version}, newer than this version of Telegram '
			f'Tracker supports ({LEDGER_VERSION}); use a newer version. It was left untouched.'
		)
	if found.kind == OLDER:
		return f'{path} is media ledger version {found.version}, which is not supported. It was left untouched.'
	return f'{path} is not a Telegram Tracker media ledger: {found.detail}. It was left untouched.'


def read_meta(con, path):
	rows = con.execute(f'SELECT {", ".join(META_COLUMNS)} FROM ledger_meta').fetchall()
	if len(rows) != 1:
		raise LedgerRefused(f'{path} is damaged: its ledger_meta row is missing. It was left untouched.')
	return LedgerMeta(*rows[0])


def verify(con, path):
	'''
	Check an open ledger: identity pragmas, every object exactly as the
	DDL creates it, and one ledger_meta row. Returns the LedgerMeta.
	'''
	# probed in memory: a busy or unreadable file is not a missing JSON1
	check_library()
	try:
		identity = (
			con.execute('PRAGMA application_id').fetchone()[0],
			con.execute('PRAGMA user_version').fetchone()[0]
		)
		objects = schema_objects(con)
		if identity != (LEDGER_APPLICATION_ID, LEDGER_VERSION):
			raise LedgerRefused(f'{path} changed while it was opened; it was left untouched.')
		if objects != expected_objects():
			raise LedgerRefused(
				f'{path} is damaged: its tables do not match media ledger version {LEDGER_VERSION}. '
				'It was left untouched.'
			)
		return read_meta(con, path)
	except sqlite3.Error as exc:
		raise storage_error(exc, f'read {path}') from None


def create_ledger(path, *, dataset_uuid, storage, output_binding, created_at):
	'''
	Create a ledger at `path`, which must not exist, bound to
	`dataset_uuid`. A file that appeared meanwhile is refused, never
	replaced.
	'''
	parent = os.path.dirname(os.path.abspath(path))
	tmp = os.path.join(parent, f'.{os.path.basename(path)}.{uuid.uuid4().hex}.creating')
	try:
		con = ledger_connection(tmp)
		try:
			check_library(con)
			# one transaction for the whole DDL, not one per statement
			con.executescript(f'BEGIN IMMEDIATE;\n{ledger_sql()}\nCOMMIT;')
			with transaction(con, f'create {path}'):
				con.execute(
					f'INSERT INTO ledger_meta (singleton, ledger_version, {", ".join(META_COLUMNS)}) '
					'VALUES (1, ?, ?, ?, ?, ?, ?, ?)',
					(LEDGER_VERSION, str(uuid.uuid4()), dataset_uuid, storage, output_binding, created_at,
						app_version())
				)
			verify(con, tmp)
		except sqlite3.Error as exc:
			raise storage_error(exc, f'create {path}') from None
		finally:
			con.close()
		try:
			link_new(tmp, path)
		except DatabaseRefused as exc:
			raise LedgerRefused(str(exc)) from None
	finally:
		discard(tmp)


def connect_ledger(path, *, readonly=False):
	'''
	(connection, LedgerMeta) for the current ledger at `path`; a missing
	file is refused here (see ledger.open_ledger for creation).
	'''
	found = classify_ledger(path)
	if found.kind == MISSING:
		raise LedgerRefused(f'{path} does not exist')
	if found.kind != CURRENT:
		raise LedgerRefused(refusal(path, found))
	if readonly:
		refuse_interrupted_write(path)
	con = ledger_connection(path, readonly=readonly)
	try:
		return con, verify(con, path)
	except BaseException:
		con.close()
		raise
