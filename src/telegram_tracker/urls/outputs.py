# -*- coding: utf-8 -*-
'''
The outputs of build-urls in the data folder, rebuilt whole by every
build:

	urls_dataset.csv   one row per occurrence (COLUMNS)
	urls.sqlite3       the same rows in `occurrences`, with `build`,
	                   `channels`, `domains` and `channel_domains`
	urls-build.json    the build record, written last

The database is staged under a temporary name and the CSV is written
from its `occurrences` table, so the two hold the same rows. The three
files are then replaced in that order. A build that stops between two
replaces leaves files of different builds; publication_problem() finds
that from the record's build UUID and CSV hash.
'''

# import modules
import csv
import hashlib
import json
import os
import pathlib
import sqlite3


URLS_DATASET = 'urls_dataset.csv'
URLS_DATABASE = 'urls.sqlite3'
BUILD_FILE = 'urls-build.json'

# urls.sqlite3 layout version (PRAGMA user_version and build.format)
OUTPUT_FORMAT = 1

# (column, SQL type) of urls_dataset.csv and the occurrences table
COLUMNS = (
	('occurrence_id', 'INTEGER PRIMARY KEY'),
	('channel', 'TEXT NOT NULL'),
	('channel_id', 'INTEGER'),
	('message_id', 'INTEGER'),
	('date', 'TEXT'),
	('msg_link', 'TEXT'),
	('occurrence_index', 'INTEGER NOT NULL'),
	('source', 'TEXT NOT NULL'),
	('evidence', 'TEXT NOT NULL'),
	('text_offset', 'INTEGER'),
	('text_length', 'INTEGER'),
	('label', 'TEXT'),
	('raw_url', 'TEXT'),
	('url_kind', 'TEXT NOT NULL'),
	('url_key', 'TEXT'),
	('normalized_url', 'TEXT'),
	('scheme', 'TEXT'),
	('netloc', 'TEXT'),
	('userinfo', 'TEXT'),
	('host', 'TEXT'),
	('host_unicode', 'TEXT'),
	('port', 'INTEGER'),
	('host_type', 'TEXT'),
	('subdomain', 'TEXT'),
	('suffix', 'TEXT'),
	('is_private_suffix', 'INTEGER'),
	('registered_domain', 'TEXT'),
	('registry_domain', 'TEXT'),
	('domain_key', 'TEXT NOT NULL'),
	('path', 'TEXT'),
	('query', 'TEXT'),
	('fragment', 'TEXT'),
	('preview_type', 'TEXT'),
	('preview_display_url', 'TEXT'),
	('preview_site_name', 'TEXT'),
	('preview_title', 'TEXT'),
	('preview_description', 'TEXT'),
	('preview_matches_text', 'INTEGER'),
	('is_forward', 'INTEGER NOT NULL'),
	('fwd_from_type', 'TEXT'),
	('fwd_from_id', 'INTEGER'),
	('fwd_from_post', 'INTEGER'),
	('fwd_from_date', 'TEXT'),
	('fwd_from_name', 'TEXT'),
	('profile_name', 'TEXT'),
	('profile_id', 'TEXT'),
	('history_run', 'TEXT')
)
COLUMN_NAMES = tuple(name for name, _ in COLUMNS)

# counts shared by the aggregate tables, over the rows of a group
COUNTS = '''
  count(*),
  count(*) FILTER (WHERE source <> 'preview'),
  count(*) FILTER (WHERE source = 'preview'),
  count(DISTINCT json_array(channel, message_id)),
  count(DISTINCT url_key)'''

AGGREGATE_DDL = '''
CREATE TABLE build (
  build_uuid TEXT NOT NULL,
  format INTEGER NOT NULL,
  normalization_version INTEGER NOT NULL,
  built_at TEXT NOT NULL,
  source TEXT NOT NULL,
  storage TEXT NOT NULL,
  collection TEXT NOT NULL,
  suffix_list TEXT NOT NULL
);
CREATE TABLE channels (
  channel TEXT PRIMARY KEY,
  channel_id INTEGER,
  histories INTEGER NOT NULL,
  messages INTEGER NOT NULL,
  occurrences INTEGER NOT NULL,
  text_occurrences INTEGER NOT NULL,
  preview_occurrences INTEGER NOT NULL,
  messages_with_urls INTEGER NOT NULL,
  urls INTEGER NOT NULL,
  domains INTEGER NOT NULL
);
CREATE TABLE domains (
  domain_key TEXT PRIMARY KEY,
  domain_kind TEXT NOT NULL,
  registered_domain TEXT,
  registry_domain TEXT,
  suffix TEXT,
  is_private_suffix INTEGER,
  occurrences INTEGER NOT NULL,
  text_occurrences INTEGER NOT NULL,
  preview_occurrences INTEGER NOT NULL,
  messages INTEGER NOT NULL,
  urls INTEGER NOT NULL,
  channels INTEGER NOT NULL,
  first_date TEXT,
  last_date TEXT
);
CREATE TABLE channel_domains (
  channel TEXT NOT NULL,
  domain_key TEXT NOT NULL,
  occurrences INTEGER NOT NULL,
  text_occurrences INTEGER NOT NULL,
  preview_occurrences INTEGER NOT NULL,
  messages INTEGER NOT NULL,
  urls INTEGER NOT NULL,
  PRIMARY KEY (channel, domain_key)
);
'''


def column_list(names):
	return ', '.join(f'"{name}"' for name in names)


def occurrences_ddl():
	columns = ',\n'.join(f'  "{name}" {kind}' for name, kind in COLUMNS)
	return f'CREATE TABLE occurrences (\n{columns}\n);\n'


def schema_sql():
	'''
	The DDL of urls.sqlite3.
	'''
	return occurrences_ddl() + AGGREGATE_DDL


'''

Staging

'''

def open_staging(path):
	'''
	A new database at `path` (a temporary name) with the output tables,
	inside one open transaction.
	'''
	con = sqlite3.connect(path, isolation_level=None)
	con.execute('PRAGMA journal_mode = MEMORY')
	con.execute(f'PRAGMA user_version = {OUTPUT_FORMAT}')
	con.executescript(schema_sql())
	con.execute('BEGIN')
	return con


def insert_rows(con, rows):
	'''
	Append occurrence rows (dicts keyed by COLUMN_NAMES, without
	occurrence_id: it follows insertion order).
	'''
	names = COLUMN_NAMES[1:]
	sql = f'INSERT INTO occurrences ({column_list(names)}) VALUES ({", ".join("?" for _ in names)})'
	con.executemany(sql, ([row[name] for name in names] for row in rows))


def finish(con, build, channel_messages):
	'''
	Fill the build row and the aggregate tables, then commit and close.

	build -> the build table's values by column
	channel_messages -> {channel: (channel_id, histories, messages read)}
	'''
	con.execute(
		'INSERT INTO build VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
		(build['build_uuid'], OUTPUT_FORMAT, build['normalization_version'], build['built_at'],
			build['source'], build['storage'], json.dumps(build['collection'], sort_keys=True),
			json.dumps(build['suffix_list'], sort_keys=True))
	)
	con.execute(f'''
		INSERT INTO domains
		SELECT domain_key,
		  CASE WHEN max(registered_domain) IS NOT NULL THEN 'domain'
		    WHEN max(url_kind) = 'malformed' THEN 'malformed'
		    WHEN max(host_type) IS NULL THEN 'no_host'
		    ELSE max(host_type) END,
		  max(registered_domain),
		  CASE WHEN max(registered_domain) IS NOT NULL THEN max(registry_domain) END,
		  CASE WHEN max(registered_domain) IS NOT NULL THEN max(suffix) END,
		  CASE WHEN max(registered_domain) IS NOT NULL THEN max(is_private_suffix) END,
		  {COUNTS},
		  count(DISTINCT channel),
		  min(date),
		  max(date)
		FROM occurrences GROUP BY domain_key
	''')
	con.execute(f'''
		INSERT INTO channel_domains
		SELECT channel, domain_key, {COUNTS}
		FROM occurrences GROUP BY channel, domain_key
	''')
	for channel, (channel_id, histories, messages) in channel_messages.items():
		con.execute(
			f'''
			INSERT INTO channels
			SELECT ?, ?, ?, ?, {COUNTS}, count(DISTINCT domain_key)
			FROM occurrences WHERE channel = ?
			''',
			(channel, channel_id, histories, messages, channel)
		)
	con.execute('COMMIT')
	con.close()


def write_csv(database, path):
	'''
	urls_dataset.csv at `path` from the occurrences of `database`, in
	occurrence order. Returns (rows, sha256 of the file).
	'''
	con = sqlite3.connect(pathlib.Path(os.path.abspath(database)).as_uri() + '?mode=ro', uri=True)
	rows = 0
	try:
		cursor = con.execute(
			f'SELECT {column_list(COLUMN_NAMES)} FROM occurrences ORDER BY occurrence_id'
		)
		with open(path, mode='w', encoding='utf-8', newline='') as writer:
			out = csv.writer(writer, lineterminator='\n')
			out.writerow(COLUMN_NAMES)
			while batch := cursor.fetchmany(500):
				out.writerows(batch)
				rows += len(batch)
	finally:
		con.close()
	return rows, file_sha256(path)


def file_sha256(path):
	digest = hashlib.sha256()
	with open(path, 'rb') as reader:
		while chunk := reader.read(1 << 20):
			digest.update(chunk)
	return digest.hexdigest()


'''

Published outputs

'''

def output_paths(data_path):
	return {
		name: os.path.join(data_path, name)
		for name in (URLS_DATASET, URLS_DATABASE, BUILD_FILE)
	}


def database_build_uuid(path):
	'''
	The build_uuid recorded in a urls.sqlite3, or None when unreadable.
	'''
	try:
		con = sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + '?mode=ro', uri=True)
	except sqlite3.Error:
		return None
	try:
		rows = con.execute('SELECT build_uuid FROM build').fetchall()
	except sqlite3.Error:
		return None
	finally:
		con.close()
	return rows[0][0] if len(rows) == 1 else None


def publication_problem(data_path):
	'''
	Why the build-urls outputs in `data_path` are not one complete build
	(a string), or None when they are, or when there are none.
	'''
	paths = output_paths(data_path)
	present = [name for name, path in paths.items() if os.path.lexists(path)]
	if not present:
		return None
	if BUILD_FILE not in present:
		return f'{", ".join(present)} found without {BUILD_FILE}: an earlier build did not finish'
	try:
		with open(paths[BUILD_FILE], encoding='utf-8') as reader:
			record = json.load(reader)
		build_uuid = record['build_uuid']
		csv_sha256 = record['outputs'][URLS_DATASET]['sha256']
	except (OSError, ValueError, KeyError, TypeError):
		return f'{BUILD_FILE} cannot be read'
	if database_build_uuid(paths[URLS_DATABASE]) != build_uuid:
		return f'{URLS_DATABASE} is not from the build {BUILD_FILE} describes'
	try:
		matches = file_sha256(paths[URLS_DATASET]) == csv_sha256
	except OSError:
		matches = False
	if not matches:
		return f'{URLS_DATASET} is not from the build {BUILD_FILE} describes'
	return None
