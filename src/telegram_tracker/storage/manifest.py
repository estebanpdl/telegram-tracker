# -*- coding: utf-8 -*-
'''
collection-storage.json: the storage mode an output folder was created
with, for folders that hold the database. A folder without it holds
files only (the default, and every folder written before storage modes
existed).

	{"format": 1, "storage": "both", "database": "telegram-tracker.sqlite3",
	 "dataset_uuid": "<the database's schema_meta.dataset_uuid>",
	 "created_at": "..."}

The database is always <output>/telegram-tracker.sqlite3. The manifest
is written once, right after the database is created, and never
changed; `dataset_uuid` binds the folder to that database. Reading it
loads nothing beyond the standard library, so the legacy collection form
and the builders can check a folder without touching the database.
'''

# import modules
import json
import os

# import local submodules
from telegram_tracker.storage.files import write_json_pretty
from telegram_tracker.storage.records import StorageError


MANIFEST_FILE = 'collection-storage.json'
MANIFEST_FORMAT = 1
# the same name as sqlite.schema.DB_FILENAME (that module is not loaded
# here: the builders read manifests and never load database code)
DATABASE_FILE = 'telegram-tracker.sqlite3'

FILES = 'files'
SQLITE = 'sqlite'
BOTH = 'both'
STORAGE_MODES = (FILES, SQLITE, BOTH)


class ManifestError(StorageError):
	'''
	collection-storage.json exists but cannot be read or is not a
	storage manifest of a known format.
	'''


def manifest_path(output_folder):
	return os.path.join(output_folder, MANIFEST_FILE)


def read_manifest(output_folder):
	'''
	The folder's manifest as a dict, or None when it has none. Raises
	ManifestError for one that cannot be used; the file is left as it is.
	'''
	path = manifest_path(output_folder)
	if not os.path.lexists(path):
		return None
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise ManifestError(f'{path} cannot be read: {exc}') from None
	if not isinstance(data, dict) or type(data.get('format')) is not int:
		raise ManifestError(f'{path} is not a storage manifest')
	if data['format'] > MANIFEST_FORMAT:
		raise ManifestError(
			f'{path} has format {data["format"]}, newer than this version of Telegram '
			'Tracker supports; use a newer version'
		)
	if (
			data['format'] != MANIFEST_FORMAT
			or data.get('storage') not in (SQLITE, BOTH)
			or data.get('database') != DATABASE_FILE
			or not isinstance(data.get('dataset_uuid'), str)):
		raise ManifestError(f'{path} is not a valid storage manifest')
	return data


def recorded_storage(output_folder):
	'''
	The folder's recorded storage mode: FILES without a manifest.
	'''
	manifest = read_manifest(output_folder)
	return FILES if manifest is None else manifest['storage']


def write_manifest(output_folder, storage, dataset_uuid, created_at):
	write_json_pretty(manifest_path(output_folder), {
		'format': MANIFEST_FORMAT,
		'storage': storage,
		'database': DATABASE_FILE,
		'dataset_uuid': dataset_uuid,
		'created_at': created_at
	})
