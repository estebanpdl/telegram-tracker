# -*- coding: utf-8 -*-
'''
What `download-media` works from: the media references of a collected
folder's saved histories, read through the builders' readers
(reader.select), and nothing else. Nothing is requested or written here.

A ChannelPlan is one saved history: its name and channel, the profile
recorded for it, the channel's stored access hash, and its references:
(message_id, MediaRef) for each stored message whose media is of a
selected type, newest first, as stored.

The channel ID is the reader's (the database, or the channel snapshot
<channel>.json of the files), else the stored messages' own. The access
hash is the channel's in the history's chats, else in the channel as
the collection stored it (its snapshot, or the database's peers); only
a full (not min) channel object counts. Both belong to the account that
collected the history.
'''

# import modules
import contextlib
import os

from collections import Counter, defaultdict
from dataclasses import dataclass, field

# import local submodules
from telegram_tracker.media.classify import TYPES, classify
from telegram_tracker.media.ledger import COMPLETE, ItemKey
from telegram_tracker.reader.records import FILES
from telegram_tracker.reader.select import open_collection, select_source
from telegram_tracker.storage.manifest import BOTH, DATABASE_FILE, SQLITE, read_manifest
from telegram_tracker.storage.status import read_status
from telegram_tracker.utils import DataInputError


@dataclass
class ChannelPlan:
	'''
	One saved history; see the module docstring.

	provenance -> its reader.records.HistoryProvenance
	references -> [(message_id, classify.MediaRef)] of the selected types
	'''
	name: str
	channel_id: int
	provenance: object
	access_hash: int | None
	references: list = field(default_factory=list)

	def key(self, message_id, ref):
		return ItemKey(self.channel_id, message_id, ref.media_id)


@dataclass
class Plans:
	'''
	plans -> ChannelPlans, in the reader's order, one per channel
	missing -> named targets with no saved history
	duplicates -> (name, name of the earlier history of the same channel)
	'''
	plans: list
	missing: list
	duplicates: list


@contextlib.contextmanager
def open_folder(output_folder, source, *, lease):
	'''
	The collection reader of `output_folder` for --source `source`, with
	an interrupted last collection readable (its saved histories are
	whole). With `lease`, a sqlite or both folder is read under the
	database lease whatever the source (the database reader holds it
	itself), as a collection holds it, and a both folder read from its
	files is checked again under the lease. Raises DataInputError or
	StorageError.
	'''
	from telegram_tracker.storage.manifest import ManifestError

	try:
		manifest = read_manifest(output_folder)
	except ManifestError as exc:
		raise DataInputError(str(exc)) from None
	storage = FILES if manifest is None else manifest['storage']
	selected = select_source(output_folder, storage, source)
	with contextlib.ExitStack() as stack:
		if lease and storage in (SQLITE, BOTH) and selected == FILES:
			from telegram_tracker.storage.destination import select_destination
			from telegram_tracker.storage.sqlite.lease import DatabaseLease

			stack.enter_context(DatabaseLease(os.path.join(output_folder, DATABASE_FILE)))
			select_destination(output_folder).check()
		yield stack.enter_context(open_collection(output_folder, selected, messages=False, interrupted_ok=True))


def collection_run(output_folder):
	'''
	The folder's last collection run as collect records it in the ledger:
	its database run_uuid in a sqlite or both folder, its started_at in a
	files folder (collection-status.json); None without a sidecar.
	'''
	status = read_status(output_folder) or {}
	return (status.get('database') or {}).get('run_uuid') or status.get('started_at')


def stored_access_hash(chats, channel_id):
	'''
	The access hash of the full Channel `channel_id` among stored chats,
	or None.
	'''
	for chat in chats or ():
		if (
				isinstance(chat, dict) and chat.get('_') == 'Channel' and chat.get('id') == channel_id
				and not chat.get('min') and type(chat.get('access_hash')) is int):
			return chat['access_hash']
	return None


def messages_of(history, payload):
	'''
	The stored messages of a loaded history (JSON objects only). Raises
	DataInputError for a payload that is not a history.
	'''
	if not isinstance(payload, dict) or not isinstance(payload.get('messages', []), list):
		raise DataInputError(f'{history.label} is not a saved message history')
	return [message for message in payload.get('messages') or () if isinstance(message, dict)]


def history_channel_id(history, messages):
	'''
	The reader's channel ID, else the channel of the stored messages; None
	when neither tells.
	'''
	if type(history.channel_id) is int:
		return history.channel_id
	for message in messages:
		channel_id = (message.get('peer_id') or {}).get('channel_id')
		if type(channel_id) is int:
			return channel_id
	return None


def media_references(messages, selection=None):
	'''
	[(message_id, MediaRef)] of the stored messages whose media are a
	downloadable file, of a selected type when `selection` is given.
	'''
	found = []
	for message in messages:
		message_id = message.get('id')
		if type(message_id) is not int or message_id <= 0:
			continue
		ref = classify(message.get('media'))
		if ref is not None and (selection is None or selection.selects(ref)):
			found.append((message_id, ref))
	return found


def read_plans(collection, selection, targets=None):
	'''
	The Plans of an open collection reader: every saved history, or those
	saved under the names in `targets` (compared case-insensitively, like
	Telegram usernames). A second history of the same channel is skipped
	as a duplicate.
	'''
	wanted = None if targets is None else {target.casefold() for target in targets}
	plans, duplicates, seen, found = [], [], {}, set()
	for history in collection.histories():
		if wanted is not None and history.name.casefold() not in wanted:
			continue
		found.add(history.name.casefold())
		payload = history.load()
		messages = messages_of(history, payload)
		channel_id = history_channel_id(history, messages)
		if channel_id is None:
			continue
		if channel_id in seen:
			duplicates.append((history.name, seen[channel_id]))
			continue
		seen[channel_id] = history.name
		access_hash = stored_access_hash(payload.get('chats'), channel_id)
		if access_hash is None:
			access_hash = stored_access_hash(collection.channel_chats(history), channel_id)
		plans.append(ChannelPlan(
			history.name, channel_id, history.provenance, access_hash, media_references(messages, selection)))
	missing = [target for target in targets or () if target.casefold() not in found]
	return Plans(plans, missing, duplicates)


'''

Dry run

'''


@dataclass
class TypeCounts:
	'''
	Files of one type a run would request: how many, the bytes of those
	whose size is known, how many have no known size, and how many the
	per-file limit skips (known sizes only).
	'''
	files: int = 0
	known_bytes: int = 0
	unknown_size: int = 0
	over_file_limit: int = 0


def dry_run_counts(plans, complete, limits):
	'''
	{channel name: {type: TypeCounts}} of the references without a
	complete ledger item (`complete`, a set of ItemKeys), and the number
	of references already complete.
	'''
	counts = defaultdict(lambda: {media_type: TypeCounts() for media_type in TYPES})
	stored = 0
	for plan in plans:
		for message_id, ref in plan.references:
			if plan.key(message_id, ref) in complete:
				stored += 1
				continue
			entry = counts[plan.name][ref.type]
			if limits.skip_known(ref.expected_bytes):
				entry.over_file_limit += 1
				continue
			entry.files += 1
			if ref.expected_bytes is None:
				entry.unknown_size += 1
			else:
				entry.known_bytes += ref.expected_bytes
	return dict(counts), stored


def complete_keys(ledger):
	'''
	The ItemKeys of the complete items of an open Ledger.
	'''
	return {
		ItemKey(item['channel_id'], item['message_id'], item['media_id'])
		for item in ledger.items() if item['state'] == COMPLETE
	}


def type_totals(counts):
	'''
	The TypeCounts of every channel summed per type.
	'''
	totals = {media_type: Counter() for media_type in TYPES}
	for per_type in counts.values():
		for media_type, entry in per_type.items():
			totals[media_type].update(vars(entry))
	return {media_type: TypeCounts(**total) for media_type, total in totals.items()}
