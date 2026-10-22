# -*- coding: utf-8 -*-
'''
Counters and aggregate files: the run's chat observations, replayed in
input order into Aggregates, and the chats.txt, counter.csv,
collected_chats.csv, and exception-log files written from them. pandas
loads only when the aggregates are written.
'''

# import modules
import os

from dataclasses import dataclass, field

# import local submodules
from telegram_tracker.storage.files import append_lines, replace_file, text_writer
from telegram_tracker.storage.records import CHANNELS_NOT_FOUND, COUNTER_COLUMNS, \
	DATASET_STAT_COLUMNS, USERS_NOT_FOUND, StorageError
from telegram_tracker.utils import chats_dataset_columns


'''

Counting (pure)

'''

def new_counter_entry(username, source, req_type, targeted_by='self'):
	'''
	First observation of a chat.
	'''
	return {
		'username': username,
		'counter': 1,
		'from_messages': 1 if req_type == 'from_messages' else 0,
		'channel_request': 1 if req_type == 'channel_request' else 0,
		'channel_req_targeted_by': {
			'channels': [targeted_by]
		},
		'source': [source]
	}


def observe_chat(counter, chat_id, username, source, req_type):
	'''
	Count a chat seen directly in a channel response (`channel_request`) or
	in a history page (`from_messages`). Returns True for a new chat, whose
	enrichment is then applied.
	'''
	entry = counter.get(chat_id)
	if entry is None:
		counter[chat_id] = new_counter_entry(username, source, req_type)
		return True

	entry['counter'] += 1
	entry[req_type] += 1
	if source not in entry['source']:
		entry['source'].append(source)

	return False


def observe_linked_chat(counter, chat_id, username, source, parent_username):
	'''
	Count a chat listed in a newly observed chat's full-channel response.
	A repeat observation records the parent in
	`channel_req_targeted_by` but does not extend `source`.
	'''
	entry = counter.get(chat_id)
	if entry is None:
		counter[chat_id] = new_counter_entry(
			username, source, 'channel_request', targeted_by=parent_username
		)
		return

	entry['counter'] += 1
	entry['channel_request'] += 1
	entry['channel_req_targeted_by']['channels'].append(parent_username)


@dataclass
class Aggregates:
	'''
	This run's counters and aggregate rows, built by `reduce_results`.

	observations -> every observation in replay order, as (input_index,
		kind, page, chat_id, username, parent_id): kind 'channel_request',
		'from_messages' (page: the history page, from 2; the first page's
		chats are not counted) or 'linked' (parent_id: the chat whose
		enrichment listed it); chat_ids lists their chat_id
	errors -> (category, input_index, chat_id, reason) in replay order:
		'target_not_found', 'enrichment_not_found', 'metadata_skipped'
	'''
	counter: dict = field(default_factory=dict)
	chat_ids: list = field(default_factory=list)
	metadata: list = field(default_factory=list)
	users_not_found: list = field(default_factory=list)
	channels_not_found: list = field(default_factory=list)
	skipped: list = field(default_factory=list)
	observations: list = field(default_factory=list)
	errors: list = field(default_factory=list)


# history pages are numbered from 1; the first page's chats are not counted
FIRST_COUNTED_PAGE = 2


def reduce_results(results, outcomes, not_found=()):
	'''
	Replay committed channel results in input order and return the run's
	Aggregates. `outcomes` maps a chat id to its EnrichOutcome; it must
	hold every chat observed directly (workers enrich each one). The
	replay applies an outcome only when the chat is new to the run.
	`not_found` is [(index, target)] for targets that could not be resolved
	or accessed.
	'''
	agg = Aggregates()
	agg.channels_not_found = [t for _, t in sorted(not_found)]
	agg.errors = [('target_not_found', i, None, None) for i, _ in sorted(not_found)]

	for result in sorted(results, key=lambda r: r.index):
		source = result.target
		page = FIRST_COUNTED_PAGE - 1
		for req_type, chats in result.observations:
			if req_type == 'from_messages':
				page += 1
			for chat_id, username in chats:
				agg.chat_ids.append(chat_id)
				agg.observations.append((
					result.index, req_type, page if req_type == 'from_messages' else None,
					chat_id, username, None
				))
				if not observe_chat(agg.counter, chat_id, username, source, req_type):
					continue
				apply_outcome(agg, outcomes[chat_id], chat_id, username, source, result.index)

	return agg


def apply_outcome(agg, outcome, chat_id, username, source, index=None):
	for linked_id, linked_username in outcome.linked:
		agg.chat_ids.append(linked_id)
		agg.observations.append((index, 'linked', None, linked_id, linked_username, chat_id))
		observe_linked_chat(agg.counter, linked_id, linked_username, source, username)

	if outcome.error == 'not_found':
		agg.users_not_found.append(f'ID - {chat_id}')
		agg.errors.append(('enrichment_not_found', index, chat_id, None))
	elif outcome.error is not None:
		agg.skipped.append((chat_id, outcome.error))
		agg.errors.append(('metadata_skipped', index, chat_id, outcome.error))
	else:
		agg.metadata.extend(dict(row) for row in outcome.rows)


'''

Aggregate files

'''

def _has_no_rows(csv_path):
	'''
	True when a CSV holds at most a header line.
	'''
	with open(csv_path, encoding='utf-8') as reader:
		return holds_no_rows(reader)


def holds_no_rows(reader):
	'''
	True when a CSV text stream holds at most a header line.
	'''
	reader.readline()
	return reader.readline() == ''


def empty_collected_chats_columns():
	'''
	Header for a collected_chats.csv with no rows: the selected chat
	columns plus the merged counter columns.
	'''
	chat_columns = [
		c for c in chats_dataset_columns() if c not in DATASET_STAT_COLUMNS
	]
	return chat_columns + COUNTER_COLUMNS[2:]


def counter_frame(counter):
	import pandas as pd

	return pd.DataFrame.from_dict(
		counter,
		orient='index'
	).reset_index().rename(
		columns={
			'index': 'id'
		}
	).reindex(columns=COUNTER_COLUMNS)


def write_aggregates(output_folder, agg):
	'''
	Write chats.txt, counter.csv, collected_chats.csv, and the exception
	logs for this run's Aggregates.

	collected_chats.csv keeps earlier rows as they are and appends this
	run's rows merged with this run's counts. A run that adds no rows
	leaves an existing file untouched; a missing or header-only file with
	no new rows becomes a header-correct empty file.
	'''
	try:
		for path, write in aggregate_files(output_folder, agg):
			replace_file(path, write)
	except OSError as exc:
		raise StorageError(f'cannot update aggregate files in {output_folder}: {exc}') from exc

	append_logs(output_folder, agg)


def aggregate_files(output_folder, agg):
	'''
	Yield (path, write) for each aggregate file write_aggregates
	replaces, in order; write(temp_path) writes one file. The earlier
	contents a file builds on are read just before it is yielded. The
	exception logs are appended separately (append_logs).
	'''
	chats_path = f'{output_folder}/chats.txt'
	earlier = []
	if os.path.isfile(chats_path):
		with open(chats_path, mode='r', encoding='utf-8') as reader:
			earlier = [line.rstrip() for line in reader]
	ids = dict.fromkeys(earlier + [str(i) for i in agg.chat_ids])
	yield (chats_path, text_writer(''.join(f'{c}\n' for c in ids)))

	counter_df = counter_frame(agg.counter)
	yield (
		f'{output_folder}/counter.csv',
		lambda tmp: counter_df.to_csv(tmp, encoding='utf-8', index=False)
	)

	csv_path = f'{output_folder}/collected_chats.csv'
	has_rows = os.path.isfile(csv_path) and not _has_no_rows(csv_path)
	df = collected_chats_update(
		agg.metadata, counter_df, has_rows, lambda: earlier_collected_chats(csv_path)
	)
	if df is not None:
		yield (csv_path, lambda tmp: df.to_csv(tmp, index=False, encoding='utf-8'))


def earlier_collected_chats(source):
	'''
	The rows of an existing collected_chats.csv (a path or a text
	buffer) as text, exactly as written.
	'''
	import pandas as pd

	return pd.read_csv(source, encoding='utf-8', dtype=str, keep_default_na=False)


def collected_chats_update(metadata, counter_df, has_rows, read_earlier):
	'''
	The collected_chats.csv content after one run, as a DataFrame to
	write, or None when the file stays as it is.

	metadata -> the run's chat rows (Aggregates.metadata)
	counter_df -> the run's counter.csv frame (counter_frame)
	has_rows -> whether the existing file holds rows
	read_earlier -> returns those rows (earlier_collected_chats); called
		only when they are kept

	Earlier rows stay as text; this run's rows are merged with this
	run's counts. A run without rows writes a header-only file when there
	are no earlier rows. The database reader replays stored runs through
	this function, so both sources give the same file.
	'''
	import pandas as pd

	if metadata:
		df = pd.DataFrame(metadata).merge(
			counter_df.drop(columns='username'), how='left', on='id'
		)
		if has_rows:
			# text as written; this run's counts never touch earlier rows
			df = pd.concat([read_earlier(), df], ignore_index=True)
		return df
	if not has_rows:
		return pd.DataFrame(columns=empty_collected_chats_columns())
	return None


def append_logs(output_folder, agg):
	'''
	Append this run's not-found entries to the exception logs (text
	mode, so lines end as the platform's other text files do).
	'''
	append_lines(f'{output_folder}/{CHANNELS_NOT_FOUND}', agg.channels_not_found)
	append_lines(f'{output_folder}/{USERS_NOT_FOUND}', agg.users_not_found)
