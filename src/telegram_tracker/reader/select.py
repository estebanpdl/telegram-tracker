# -*- coding: utf-8 -*-
'''
Choosing what a builder reads in a collection folder, and the gates
before it reads.

	auto    files for a folder without a storage manifest (files mode
	        and folders of earlier versions) and for a `both` folder;
	        the database for a `sqlite` folder
	files   the file outputs; refused for a `sqlite` folder
	sqlite  the database; refused for a folder without one

The two copies of a `both` folder are never combined, and a source that
cannot be read is refused, never replaced by the other one.
'''

# import modules
import contextlib

# import local submodules
from telegram_tracker.reader.records import AUTO, FILES, SQLITE
from telegram_tracker.storage.manifest import BOTH, DATABASE_FILE, ManifestError, read_manifest
from telegram_tracker.storage.status import collection_status
from telegram_tracker.utils import DataInputError


@contextlib.contextmanager
def open_collection(data_path, source=AUTO, *, messages=True, interrupted_ok=False):
	'''
	The collection in `data_path` as a reader of the chosen `source`
	(see the module docstring): reader.files.FilesCollection or
	reader.database.DatabaseCollection, with `notes` (strings for the
	user) and `storage` (the folder's recorded mode). Raises
	DataInputError before anything is read when the folder cannot be
	built from. `messages`: the builder reads message histories.
	`interrupted_ok`: an interrupted last collection is read, with a note
	(storage.status.collection_status).

	The database is read under its lease; leave the block before
	writing outputs.
	'''
	try:
		manifest = read_manifest(data_path)
	except ManifestError as exc:
		raise DataInputError(str(exc)) from None
	storage = FILES if manifest is None else manifest['storage']
	selected = select_source(data_path, storage, source or AUTO)

	if selected == FILES:
		from telegram_tracker.reader.files import FilesCollection

		status, notes = collection_status(data_path, messages=messages, interrupted_ok=interrupted_ok)
		collection = FilesCollection(data_path, status)
		collection.notes = notes
		collection.storage = storage
		yield collection
		return

	from telegram_tracker.reader.database import DatabaseCollection

	with DatabaseCollection(data_path, messages=messages, interrupted_ok=interrupted_ok) as collection:
		yield collection


def select_source(data_path, storage, requested):
	'''
	The source to read for a folder of recorded `storage`; raises
	DataInputError for one the folder does not hold.
	'''
	if requested == AUTO:
		return SQLITE if storage == SQLITE else FILES
	if requested == FILES and storage == SQLITE:
		raise DataInputError(
			f'{data_path} was collected with --storage sqlite: it holds only the database '
			f'{DATABASE_FILE}, not the file outputs. Use --source sqlite (or auto).'
		)
	if requested == SQLITE and storage not in (SQLITE, BOTH):
		raise DataInputError(
			f'{data_path} has no database (it was collected with --storage files). '
			'Use --source files (or auto).'
		)
	return requested
