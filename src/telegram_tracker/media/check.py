# -*- coding: utf-8 -*-
'''
`download-media --check`: whether a folder's media ledger, its stored
files and its saved histories agree. Offline and read only: the ledger
is opened read only (an unfinished write is refused, naming the runs
that roll it back), nothing is recovered, removed or created, and no
report file is written. Like collect, it holds the output lock, and the
database lease of a sqlite or both folder, while it reads, so no run
changes the folder under it; their lock files are removed when it ends.

	identity           the ledger belongs to the folder's dataset (the
	                   manifest, or collection-identity.json)
	item -> message    each item's message is in the current history and
	                   carries the item's media_id. One whose stored
	                   message now has other media (an edited message
	                   merged by --update) is superseded; one whose message
	                   is not in the current histories (a later full or
	                   window run replaced them) is not_in_history. Neither
	                   is an orphan: its file stays recorded.
	reference -> state each downloadable reference of the current
	                   histories, by type and ledger state. A reference
	                   with no row is selected_no_row when a collect
	                   --include-media run wrote its message and selected
	                   its type (that run stopped between saving the
	                   history and recording its media), else not_selected.
	                   Database folders record the run of each message;
	                   files record it per history, so a history merged by
	                   --update cannot tell (counted as not_selected).
	file -> hash       each blob's file is a regular file at its generated
	                   path, with its size and SHA-256
	orphans            files under downloaded-media that no blob records
	                   (a run stopped between publishing and recording),
	                   blobs that no complete item uses, entries that are
	                   not generated (links, other names; never followed)
	                   and leftover partial downloads
	unfinished         runs recorded as running and items downloading: a
	                   run whose process ended; the next run that writes
	                   the ledger records them as interrupted
'''

# import modules
import json
import os
import re
import stat

from collections import Counter
from dataclasses import dataclass, field

# import local submodules
from telegram_tracker.media.classify import TYPES, classify
from telegram_tracker.media.collect import COMMAND as COLLECT
from telegram_tracker.media.identity import folder_identity
from telegram_tracker.media.layout import ABSENT, DIFFERENT, LEDGER_FILE, PARTIAL_FOLDER, RELATIVE_PATH, SAME, \
	TYPE_FOLDERS, MediaLayout, MediaPathError, inspect_final, is_link, lstat_or_none
from telegram_tracker.media.ledger import COMPLETE, DOWNLOADING, RUNNING, STATES, ItemKey, open_ledger
from telegram_tracker.media.partials import clean_partials
from telegram_tracker.media.plan import history_channel_id, media_references, messages_of, open_folder
from telegram_tracker.reader.records import FILES
from telegram_tracker.storage.manifest import BOTH, DATABASE_FILE
from telegram_tracker.storage.writer import OutputLock


SELECTED_NO_ROW = 'selected_no_row'
NOT_SELECTED = 'not_selected'
REFERENCE_STATES = STATES + (SELECTED_NO_ROW, NOT_SELECTED)
CHANNEL_FOLDER = re.compile(r'[1-9][0-9]*')


@dataclass
class CheckReport:
	'''
	The findings of check_folder; see the module docstring. Paths are
	relative to downloaded-media, with '/' separators.
	'''
	source: str
	identity: str | None = None
	ledger: bool = False
	references: dict = field(default_factory=lambda: {media_type: Counter() for media_type in TYPES})
	items: int = 0
	matched: int = 0
	superseded: list = field(default_factory=list)
	not_in_history: list = field(default_factory=list)
	blobs: int = 0
	missing_files: list = field(default_factory=list)
	differing_files: list = field(default_factory=list)
	unusable_paths: list = field(default_factory=list)
	orphan_files: list = field(default_factory=list)
	orphan_blobs: list = field(default_factory=list)
	unexpected: list = field(default_factory=list)
	partials: list = field(default_factory=list)
	unfinished_runs: list = field(default_factory=list)
	downloading: list = field(default_factory=list)
	no_row: list = field(default_factory=list)
	notes: list = field(default_factory=list)

	def problems(self):
		'''
		(description, findings) for each kind of problem found, in report
		order; empty when the folder is consistent.
		'''
		found = [
			('recorded files missing', self.missing_files),
			('stored files whose size or SHA-256 differs from the record', self.differing_files),
			('recorded paths that are links, not regular files, or not generated names', self.unusable_paths),
			('blobs that no complete item uses', self.orphan_blobs),
			('files that no blob records (a run stopped after publishing them; download-media adopts one '
				'when it downloads the same content again)', self.orphan_files),
			('other entries under downloaded-media (left as they are, never followed)', self.unexpected),
			('leftover partial downloads (the next run that writes the ledger removes those of the runs '
				'it knows)', self.partials),
			('runs recorded as running (their process ended; the next run that writes the ledger records '
				'them as interrupted)', self.unfinished_runs),
			('items still recorded as downloading (the same)', [tuple(key) for key in self.downloading]),
			('references that a collect --include-media run selected but never recorded (it stopped '
				'before recording them; download-media requests them)', [tuple(key) for key in self.no_row]),
		]
		return [(description, findings) for description, findings in found if findings]


def check_folder(output_folder, source='auto'):
	'''
	The CheckReport of `output_folder`, reading its histories with --source
	`source`. Raises DataInputError when the histories cannot be read and
	StorageError when the ledger cannot be (another dataset, an
	unfinished write, damage) or the folder is locked.
	'''
	with OutputLock(output_folder), open_folder(output_folder, source, lease=True) as collection:
		report = CheckReport(source=collection.source, notes=list(collection.notes))
		identity = folder_identity(output_folder)
		layout = MediaLayout(output_folder)
		ledger = None
		if layout.ensure(create=False) is not None and lstat_or_none(layout.ledger_path) is not None:
			ledger = open_ledger(layout, identity, readonly=True)
		try:
			if ledger is not None:
				report.ledger = True
				report.identity = identity.source
			_check(report, collection, layout, ledger)
		finally:
			if ledger is not None:
				ledger.close()
	return report


def _check(report, collection, layout, ledger):
	items, blobs, runs = (ledger.items(), ledger.blobs(), ledger.runs()) if ledger is not None else ([], [], [])
	by_key = {ItemKey(item['channel_id'], item['message_id'], item['media_id']): item for item in items}

	# the types each collection run with media selected, by that run
	selected_by = {}
	for run in runs:
		if run['command'] == COLLECT and run['collection_run']:
			selected_by.setdefault(run['collection_run'], set()).update(json.loads(run['selection'])['types'])
	if collection.source == FILES and collection.storage == BOTH and selected_by:
		# the files name a run by its started_at; collect recorded its run_uuid
		for run_uuid, started_at in run_starts(layout.output_folder):
			if run_uuid in selected_by:
				selected_by.setdefault(started_at, set()).update(selected_by[run_uuid])

	stored = {}
	for history in collection.histories():
		payload = history.load()
		messages = messages_of(history, payload)
		channel_id = history_channel_id(history, messages)
		if channel_id is None or channel_id in stored:
			continue
		stored[channel_id] = {message.get('id'): media_id_of(message) for message in messages}
		writers = collection.message_runs(history, payload) or {}
		for message_id, ref in media_references(messages):
			item = by_key.get(ItemKey(channel_id, message_id, ref.media_id))
			if item is not None:
				state = item['state']
			elif ref.type in selected_by.get(writers.get(message_id), ()):
				state = SELECTED_NO_ROW
				report.no_row.append(ItemKey(channel_id, message_id, ref.media_id))
			else:
				state = NOT_SELECTED
			report.references[ref.type][state] += 1

	report.items = len(items)
	for key, item in by_key.items():
		messages = stored.get(key.channel_id)
		if messages is None or key.message_id not in messages:
			report.not_in_history.append(key)
		elif messages[key.message_id] != key.media_id:
			report.superseded.append(key)
		else:
			report.matched += 1
		if item['state'] == DOWNLOADING:
			report.downloading.append(key)
	report.unfinished_runs = [run['run_uuid'] for run in runs if run['state'] == RUNNING]

	_check_blobs(report, layout, items, blobs)
	if ledger is not None:
		partials = clean_partials(layout, ledger, None, remove=False)
		report.partials = [relative(layout, path) for path in partials.kept + partials.unknown]
	report.unexpected.extend(_walk(report, layout, {blob['path'] for blob in blobs}))


def run_starts(output_folder):
	'''
	(run_uuid, started_at) of the collection runs in the folder's
	database, read only.
	'''
	from telegram_tracker.storage.sqlite.connection import open_database

	con = open_database(os.path.join(output_folder, DATABASE_FILE), readonly=True)
	try:
		return con.execute('SELECT run_uuid, started_at FROM collection_runs').fetchall()
	finally:
		con.close()


def media_id_of(message):
	'''
	The media_id of a stored message's downloadable media, or None.
	'''
	ref = classify(message.get('media'))
	return None if ref is None else ref.media_id


def relative(layout, path):
	return os.path.relpath(path, layout.root).replace(os.sep, '/')


def _check_blobs(report, layout, items, blobs):
	'''
	file -> hash for every blob, and blobs that no complete item uses.
	'''
	used = {(item['channel_id'], item['sha256']) for item in items if item['state'] == COMPLETE}
	report.blobs = len(blobs)
	for blob in blobs:
		path = blob['path']
		if (blob['channel_id'], blob['sha256']) not in used:
			report.orphan_blobs.append(path)
		try:
			if not path.startswith(f'{blob["channel_id"]}/'):
				raise MediaPathError(f'{path} is not in the folder of channel {blob["channel_id"]}')
			final = layout.final_path(path, create=False)
			found = ABSENT if final is None else inspect_final(final, blob['sha256'], blob['bytes'])
		except (MediaPathError, OSError):
			report.unusable_paths.append(path)
			continue
		if found == ABSENT:
			report.missing_files.append(path)
		elif found == DIFFERENT:
			report.differing_files.append(path)
		elif found != SAME:
			report.unusable_paths.append(path)


def plain_folder(info):
	return info is not None and not is_link(info) and stat.S_ISDIR(info.st_mode)


def _walk(report, layout, recorded):
	'''
	Entries under downloaded-media that are not the ledger, the partial
	folder, or a generated file; generated files that no blob records go
	to report.orphan_files. Links are listed, never followed.
	'''
	unexpected = []
	root = layout.root
	if not plain_folder(lstat_or_none(root)):
		return [] if lstat_or_none(root) is None else ['.']
	for name in sorted(os.listdir(root)):
		path = os.path.join(root, name)
		if name in (LEDGER_FILE, f'{LEDGER_FILE}-journal', PARTIAL_FOLDER):
			continue
		if not (CHANNEL_FOLDER.fullmatch(name) and plain_folder(lstat_or_none(path))):
			unexpected.append(name)
			continue
		for folder in sorted(os.listdir(path)):
			type_path = os.path.join(path, folder)
			if folder not in TYPE_FOLDERS.values() or not plain_folder(lstat_or_none(type_path)):
				unexpected.append(f'{name}/{folder}')
				continue
			for entry in sorted(os.listdir(type_path)):
				info = lstat_or_none(os.path.join(type_path, entry))
				rel = f'{name}/{folder}/{entry}'
				if info is None or is_link(info) or not stat.S_ISREG(info.st_mode):
					unexpected.append(rel)
				elif rel not in recorded:
					(report.orphan_files if RELATIVE_PATH.match(rel) else unexpected).append(rel)
	return unexpected
