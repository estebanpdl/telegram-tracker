# -*- coding: utf-8 -*-
'''
Which dataset an output folder holds, and the guard that binds a media
ledger to it.

sqlite and both folders: the dataset UUID in collection-storage.json,
which is the database's schema_meta.dataset_uuid (storage.destination
checks that the two agree before a run, and the builders' gates do the
same before reading).

files folders have no manifest, so collection-identity.json gives them a
dataset UUID:

	{"format": 1, "dataset_uuid": "<uuid4>", "created_at": "..."}

It is written once, before the folder's ledger is created, and never
changed. It is not a storage manifest: a files folder stays a files
folder. Copying or moving the whole folder keeps the record and the
ledger together; a downloaded-media folder copied anywhere else meets a
missing or different record.

A ledger is refused when its dataset_uuid or storage differs from the
folder's, or when a files folder has a ledger but no record.
'''

# import modules
import json
import os
import uuid

from dataclasses import dataclass

# import local submodules
from telegram_tracker.media.layout import is_link, lstat_or_none
from telegram_tracker.media.ledger_schema import LedgerRefused
from telegram_tracker.storage.files import pretty_json_text
from telegram_tracker.storage.manifest import FILES, MANIFEST_FILE, read_manifest
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.sqlite.connection import link_new
from telegram_tracker.storage.sqlite.schema import DatabaseRefused
from telegram_tracker.storage.status import utc_now
from telegram_tracker.utils import discard


IDENTITY_FILE = 'collection-identity.json'
IDENTITY_FORMAT = 1


class IdentityError(StorageError):
	'''
	collection-identity.json exists but cannot be used. It was left as
	it is.
	'''


@dataclass(frozen=True)
class FolderIdentity:
	'''
	storage -> the folder's storage mode
	dataset_uuid -> its dataset UUID; None for a files folder without a
	                record yet
	source -> the file it was read from (MANIFEST_FILE or IDENTITY_FILE),
	          None when there is none
	'''
	storage: str
	dataset_uuid: str | None
	source: str | None


def canonical_uuid(value):
	try:
		return isinstance(value, str) and str(uuid.UUID(value)) == value
	except ValueError:
		return False


def identity_path(output_folder):
	return os.path.join(output_folder, IDENTITY_FILE)


def read_identity_record(output_folder):
	'''
	The folder's collection-identity.json as a dict, or None when it has
	none. Raises IdentityError for one that cannot be used.
	'''
	path = identity_path(output_folder)
	info = lstat_or_none(path)
	if info is None:
		return None
	if is_link(info):
		raise IdentityError(f'{path} is a link; refusing to use it')
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise IdentityError(f'{path} cannot be read: {exc}') from None
	if not isinstance(data, dict) or type(data.get('format')) is not int:
		raise IdentityError(f'{path} is not a collection identity record')
	if data['format'] > IDENTITY_FORMAT:
		raise IdentityError(
			f'{path} has format {data["format"]}, newer than this version of Telegram Tracker '
			'supports; use a newer version'
		)
	if data['format'] != IDENTITY_FORMAT or not canonical_uuid(data.get('dataset_uuid')):
		raise IdentityError(f'{path} is not a valid collection identity record')
	return data


def folder_identity(output_folder):
	'''
	The FolderIdentity of `output_folder`, read only. A manifest decides
	for sqlite/both folders; a record there is not consulted.
	'''
	manifest = read_manifest(output_folder)
	if manifest is not None:
		return FolderIdentity(manifest['storage'], manifest['dataset_uuid'], MANIFEST_FILE)
	record = read_identity_record(output_folder)
	if record is None:
		return FolderIdentity(FILES, None, None)
	return FolderIdentity(FILES, record['dataset_uuid'], IDENTITY_FILE)


def ensure_identity(output_folder):
	'''
	The folder's FolderIdentity, writing collection-identity.json first
	for a files folder that has none. Called with the output folder
	locked, before its ledger is created; an existing record is never
	replaced.
	'''
	identity = folder_identity(output_folder)
	if identity.dataset_uuid is not None:
		return identity
	path = identity_path(output_folder)
	tmp = os.path.join(output_folder, f'.{IDENTITY_FILE}.{uuid.uuid4().hex}.creating')
	record = {'format': IDENTITY_FORMAT, 'dataset_uuid': str(uuid.uuid4()), 'created_at': utc_now()}
	try:
		with open(tmp, mode='x', encoding='utf-8') as writer:
			writer.write(pretty_json_text(record))
		link_new(tmp, path)
	except DatabaseRefused:
		# written meanwhile: the existing record stands
		pass
	except OSError as exc:
		raise IdentityError(f'cannot write {path}: {exc}') from None
	finally:
		discard(tmp)
	return folder_identity(output_folder)


def check_ledger_identity(meta, identity, ledger_path):
	'''
	Refuse (LedgerRefused) a ledger whose LedgerMeta `meta` does not
	belong to the folder's FolderIdentity `identity`.
	'''
	if identity.dataset_uuid is None:
		raise LedgerRefused(
			f'{ledger_path} has no {IDENTITY_FILE} beside it, so it cannot be tied to the '
			'collected data here (a downloaded-media folder copied from another output?). '
			'It was left untouched.'
		)
	if meta.dataset_uuid != identity.dataset_uuid:
		raise LedgerRefused(
			f'{ledger_path} belongs to another dataset than {identity.source} here '
			'(a downloaded-media folder copied from another output?). It was left untouched.'
		)
	if meta.storage != identity.storage:
		raise LedgerRefused(
			f'{ledger_path} was created for a {meta.storage} folder, but this folder is '
			f'{identity.storage}. It was left untouched.'
		)
