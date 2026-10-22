# -*- coding: utf-8 -*-
'''
build-urls: every URL occurrence in the collected messages of a data
folder, offline, into urls_dataset.csv, urls.sqlite3 and urls-build.json
(urls.outputs).

The collection is read through telegram_tracker.reader (the same source
selection and gates as build-dataset and build-network); each history is
loaded and extracted while the reader holds the collection database's
lease. The build holds its own lease, urls.sqlite3.lock, from before
reading until the outputs are published, so two builds of one folder
refuse each other whatever the source. Nothing is fetched: no HTTP, DNS,
redirect or preview requests, and the public suffix list is the bundled
copy.
'''

# import modules
import json
import os
import sys
import uuid

from dataclasses import dataclass

# import local submodules
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.datasets import read_messages
from telegram_tracker.reader import AUTO, SQLITE, open_collection
from telegram_tracker.reader.records import build_record
from telegram_tracker.storage.sqlite.lease import canonical
from telegram_tracker.storage.status import utc_now
from telegram_tracker.urls import outputs
from telegram_tracker.urls.extract import message_occurrences, url_key
from telegram_tracker.urls.parse import NORMALIZATION_VERSION, Suffixes
from telegram_tracker.utils import DataInputError, discard, temp_path_for


BUILDER = 'build-urls'

# fwd_from.from_id constructor -> (fwd_from_type, ID field)
FORWARD_PEERS = {
	'PeerChannel': ('channel', 'channel_id'),
	'PeerUser': ('user', 'user_id'),
	'PeerChat': ('chat', 'chat_id')
}


@dataclass
class UrlsSummary:
	'''
	Outcome of one build-urls run.
	'''
	urls_dataset: str
	urls_database: str
	build_record: str
	build_uuid: str = None
	source: str = None
	histories: int = 0
	occurrences: int = 0


'''

Rows

'''

def forward_fields(message):
	'''
	The forward origin of a message as collected; unknown parts are None.
	'''
	forward = message.get('fwd_from')
	if not forward:
		return {
			'is_forward': 0, 'fwd_from_type': None, 'fwd_from_id': None, 'fwd_from_post': None,
			'fwd_from_date': None, 'fwd_from_name': None
		}
	peer = forward.get('from_id') or {}
	kind, id_field = FORWARD_PEERS.get(peer.get('_'), (None, None))
	return {
		'is_forward': 1,
		'fwd_from_type': kind,
		'fwd_from_id': peer.get(id_field) if id_field else None,
		'fwd_from_post': forward.get('channel_post'),
		'fwd_from_date': forward.get('date'),
		'fwd_from_name': forward.get('from_name')
	}


def message_rows(message, history, suffixes):
	'''
	The occurrence rows of one Message (dicts by outputs.COLUMN_NAMES,
	without occurrence_id).
	'''
	peer = message.get('peer_id')
	channel_id = peer.get('channel_id') if isinstance(peer, dict) else history.channel_id
	message_id = message['id']
	context = {
		'channel': history.name,
		'channel_id': channel_id,
		'message_id': message_id,
		'date': message.get('date'),
		'msg_link': f'https://t.me/{history.name}/{message_id}',
		**forward_fields(message),
		'profile_name': history.provenance.profile_name,
		'profile_id': history.provenance.profile_id,
		'history_run': history.provenance.run
	}
	rows = []
	for index, (occurrence, parsed, matches_text) in enumerate(message_occurrences(message, suffixes)):
		preview = occurrence.preview or {}
		rows.append({
			**context,
			'occurrence_index': index,
			'source': occurrence.source,
			'evidence': '+'.join(occurrence.evidence),
			'text_offset': occurrence.offset,
			'text_length': occurrence.length,
			'label': occurrence.label,
			'raw_url': occurrence.raw_url,
			'url_kind': parsed.url_kind,
			'url_key': url_key(occurrence, parsed),
			'normalized_url': parsed.normalized_url,
			'scheme': parsed.scheme,
			'netloc': parsed.netloc,
			'userinfo': parsed.userinfo,
			'host': parsed.host,
			'host_unicode': parsed.host_unicode,
			'port': parsed.port,
			'host_type': parsed.host_type,
			'subdomain': parsed.subdomain,
			'suffix': parsed.suffix,
			'is_private_suffix': None if parsed.is_private_suffix is None else int(parsed.is_private_suffix),
			'registered_domain': parsed.registered_domain,
			'registry_domain': parsed.registry_domain,
			'domain_key': parsed.domain_key,
			'path': parsed.path,
			'query': parsed.query,
			'fragment': parsed.fragment,
			'preview_type': preview.get('type'),
			'preview_display_url': preview.get('display_url'),
			'preview_site_name': preview.get('site_name'),
			'preview_title': preview.get('title'),
			'preview_description': preview.get('description'),
			'preview_matches_text': None if matches_text is None else int(matches_text)
		})
	return rows


def read_histories(collection, con, suffixes):
	'''
	Extract every history of `collection` into the staging database.
	Returns (build records of the histories, {channel: (channel_id,
	histories, messages)}, occurrences).
	'''
	records = []
	channels = {}
	total = 0
	for history in collection.histories():
		print(f'Reading data from channel -> {history.name}')
		payload = read_messages(history)
		messages = payload['messages']
		rows = []
		try:
			for message in messages:
				if message['_'] == 'Message':
					rows.extend(message_rows(message, history, suffixes))
		except (KeyError, TypeError, ValueError, AttributeError) as exc:
			raise DataInputError(
				f'{history.label}: cannot read message fields ({type(exc).__name__}: {exc})'
			) from None
		outputs.insert_rows(con, rows)
		total += len(rows)

		channel_id, count, read = channels.get(history.name, (history.channel_id, 0, 0))
		channels[history.name] = (channel_id, count + 1, read + len(messages))
		records.append(history.record(messages=len(messages)))
	return records, channels, total


'''

Build

'''

def output_lease(data_path):
	'''
	The lease on this folder's build-urls outputs (urls.sqlite3.lock).
	'''
	database = os.path.join(data_path, outputs.URLS_DATABASE)
	return Lease(canonical(database), f'The URL outputs {database}')


def build_urls(data_path, *, source=AUTO):
	'''
	Build urls_dataset.csv, urls.sqlite3 and urls-build.json (all
	replaced) from the collection in `data_path`, read from `source`
	(reader.select: auto, files or sqlite). Returns a UrlsSummary.

	Raises DataInputError, before replacing any output, when the folder
	is missing or cannot be read from that source, another build-urls
	holds its outputs, or a history is malformed. Notes about the
	collection and about outputs of an unfinished earlier build go to
	stderr.
	'''
	if not os.path.isdir(data_path):
		raise DataInputError(f'{data_path} not found; run collection first')
	paths = outputs.output_paths(data_path)
	summary = UrlsSummary(
		urls_dataset=paths[outputs.URLS_DATASET], urls_database=paths[outputs.URLS_DATABASE],
		build_record=paths[outputs.BUILD_FILE]
	)
	try:
		lease = output_lease(data_path).__enter__()
	except LeaseError as exc:
		raise DataInputError(str(exc)) from None
	try:
		publish(data_path, source, paths, summary)
	finally:
		lease.__exit__(None, None, None)
	return summary


def publish(data_path, source, paths, summary):
	'''
	The build under the output lease: read and extract into a staged
	database, write the staged CSV and record, then replace the outputs.
	'''
	problem = outputs.publication_problem(data_path)
	if problem is not None:
		print(f'> Note: {problem}; this build replaces all three outputs.', file=sys.stderr)

	suffixes = Suffixes()
	suffix_list = suffixes.describe()
	staged = {name: temp_path_for(path) for name, path in paths.items()}
	journal = staged[outputs.URLS_DATABASE] + '-journal'
	for path in (*staged.values(), journal):
		discard(path)
	con = None
	try:
		con = outputs.open_staging(staged[outputs.URLS_DATABASE])
		with open_collection(data_path, source) as collection:
			for note in collection.notes:
				print(f'> Note: {note}.', file=sys.stderr)
			if collection.source == SQLITE:
				print(f'Reading the database of {data_path}')
			histories, channels, summary.occurrences = read_histories(collection, con, suffixes)

		summary.build_uuid = str(uuid.uuid4())
		summary.source = collection.source
		summary.histories = len(histories)
		built_at = utc_now()
		outputs.finish(con, {
			'build_uuid': summary.build_uuid,
			'normalization_version': NORMALIZATION_VERSION,
			'built_at': built_at,
			'source': collection.source,
			'storage': collection.storage,
			'collection': collection.describe(),
			'suffix_list': suffix_list
		}, channels)
		con = None
		rows, csv_sha256 = outputs.write_csv(staged[outputs.URLS_DATABASE], staged[outputs.URLS_DATASET])

		record = build_record(BUILDER, collection, built_at, histories=histories, outputs={
			outputs.URLS_DATASET: {'rows': rows, 'sha256': csv_sha256},
			outputs.URLS_DATABASE: {'format': outputs.OUTPUT_FORMAT}
		})
		record.update({
			'build_uuid': summary.build_uuid,
			'normalization_version': NORMALIZATION_VERSION,
			'suffix_list': suffix_list
		})
		with open(staged[outputs.BUILD_FILE], mode='w', encoding='utf-8') as writer:
			json.dump(record, writer, ensure_ascii=False, indent=1)
			writer.write('\n')

		for name in (outputs.URLS_DATASET, outputs.URLS_DATABASE, outputs.BUILD_FILE):
			os.replace(staged[name], paths[name])
	finally:
		if con is not None:
			con.close()
		for path in (*staged.values(), journal):
			discard(path)
