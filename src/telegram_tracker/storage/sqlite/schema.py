# -*- coding: utf-8 -*-
'''
Identity of a Telegram Tracker data database and the schema it must hold.

A file is classified from its 100-byte SQLite header alone (magic string,
`user_version` at offset 60, `application_id` at offset 68), so a file
that is not ours is never connected to, let alone changed. Paths that
name a Telethon session or a credentials config are refused before even
that read.

schema-v1.sql in this package is docs/sqlite-schema.sql, the frozen
schema version 1.
'''

# import modules
import functools
import os
import re
import sqlite3

from dataclasses import dataclass
from importlib import resources

# import local submodules
from telegram_tracker.storage.records import StorageError


DB_FILENAME = 'telegram-tracker.sqlite3'
APPLICATION_ID = 1413960786  # 0x54475452, 'TGTR'
SCHEMA_VERSION = 1
SERIALIZER_VERSION = 1
MIN_SQLITE_VERSION = (3, 31, 0)

SQLITE_MAGIC = b'SQLite format 3\x00'
HEADER_SIZE = 100
USER_VERSION_OFFSET = 60
APPLICATION_ID_OFFSET = 68

# classifications
MISSING = 'missing'
CURRENT = 'current'
OLDER = 'older'
NEWER = 'newer'
FOREIGN = 'foreign'
CORRUPT = 'corrupt'

# Telethon session files and their journals, and the credentials config
_PROTECTED_NAME = re.compile(r'(\.session(-journal|-wal|-shm)?|^config\.ini)$', re.IGNORECASE)


class DatabaseRefused(StorageError):
	'''
	The database path or file cannot be used: not a data database, an
	unsupported schema version, damaged, or a protected path. The file is
	left as it was.
	'''


@dataclass(frozen=True)
class Classification:
	'''
	kind -> MISSING, CURRENT, OLDER, NEWER, FOREIGN or CORRUPT
	version -> the header's user_version (None unless the file is ours)
	detail -> why a FOREIGN or CORRUPT file is not usable
	'''
	kind: str
	version: int | None = None
	detail: str | None = None


def check_path(path, protected=()):
	'''
	Refuse paths that are not a plain database file: empty, `:memory:`,
	`file:` URIs, `.sql` text dumps, Telethon session files (and their
	journals), config.ini, directories, and `protected` paths (or a path
	to the same file). Nothing is opened.
	'''
	value = os.fspath(path)
	name = os.path.basename(value)
	if not value or value == ':memory:' or value.lower().startswith('file:'):
		raise DatabaseRefused(f'{value!r} is not a database file path')
	if _PROTECTED_NAME.search(name):
		raise DatabaseRefused(f'{name} is a session or credentials file, not a data database')
	if name.lower().endswith('.sql'):
		raise DatabaseRefused(f'{name} names SQL text, not an SQLite database')
	if os.path.isdir(value):
		raise DatabaseRefused(f'{value} is a directory')
	for other in protected:
		if _same_file(value, other):
			raise DatabaseRefused(f'{value} is a protected file, not a data database')


def _same_file(path, other):
	if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(other)):
		return True
	try:
		return os.path.samefile(path, other)
	except OSError:
		return False


def classify(path, protected=(), *, application_id=APPLICATION_ID, schema_version=SCHEMA_VERSION):
	'''
	Classify the file at `path` from its header (see Classification),
	as a file of `application_id` at `schema_version`. Raises
	DatabaseRefused for a refused path or an unreadable file.
	'''
	check_path(path, protected)
	try:
		with open(path, 'rb') as reader:
			header = reader.read(HEADER_SIZE)
	except FileNotFoundError:
		return Classification(MISSING)
	except OSError as exc:
		raise DatabaseRefused(f'cannot read {path}: {exc.strerror or exc}') from None

	if not header:
		return Classification(FOREIGN, detail='it is an empty file')
	if len(header) < HEADER_SIZE or not header.startswith(SQLITE_MAGIC):
		return Classification(CORRUPT, detail='it is not an SQLite database')

	found_id = int.from_bytes(header[APPLICATION_ID_OFFSET:APPLICATION_ID_OFFSET + 4], 'big')
	if found_id != application_id:
		return Classification(FOREIGN, detail='it is another SQLite database (a Telethon session is one)')

	version = int.from_bytes(header[USER_VERSION_OFFSET:USER_VERSION_OFFSET + 4], 'big', signed=True)
	if version == schema_version:
		return Classification(CURRENT, version)
	return Classification(OLDER if version < schema_version else NEWER, version)


def refusal_message(path, classification):
	'''
	Why a classified file cannot be used for collection, or None when it
	can (CURRENT) or will be created (MISSING).
	'''
	kind, version = classification.kind, classification.version
	if kind in (CURRENT, MISSING):
		return None
	if kind == NEWER:
		return (
			f'{path} has schema version {version}, newer than this version of Telegram '
			f'Tracker supports ({SCHEMA_VERSION}); use a newer version. It was left untouched.'
		)
	if kind == OLDER:
		return (
			f'{path} has schema version {version}; this version of Telegram Tracker uses '
			f'{SCHEMA_VERSION} and collection does not upgrade databases. It was left untouched.'
		)
	if kind == FOREIGN:
		return f'{path} is not a Telegram Tracker database: {classification.detail}. It was left untouched.'
	return f'{path} is damaged: {classification.detail}. It was left untouched.'


@functools.cache
def schema_sql():
	'''
	The schema version 1 DDL (docs/sqlite-schema.sql).
	'''
	return resources.files(__package__).joinpath('schema-v1.sql').read_text(encoding='utf-8')


@functools.cache
def expected_objects():
	'''
	{(type, name): (table, sql)} of every schema object the DDL creates,
	from applying it to an in-memory database.
	'''
	con = sqlite3.connect(':memory:')
	try:
		con.executescript(schema_sql())
		return schema_objects(con)
	finally:
		con.close()


def schema_objects(con):
	return {
		(kind, name): (table, sql)
		for kind, name, table, sql in con.execute(
			"SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
		)
	}
