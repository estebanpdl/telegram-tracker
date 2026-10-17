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
from storage.files import append_lines, replace_file, write_text
from storage.records import CHANNELS_NOT_FOUND, COUNTER_COLUMNS, \
	DATASET_STAT_COLUMNS, USERS_NOT_FOUND, StorageError
from utils import chats_dataset_columns


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
	'''
	counter: dict = field(default_factory=dict)
	chat_ids: list = field(default_factory=list)
	metadata: list = field(default_factory=list)
	users_not_found: list = field(default_factory=list)
	channels_not_found: list = field(default_factory=list)
	skipped: list = field(default_factory=list)


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

	for result in sorted(results, key=lambda r: r.index):
		source = result.target
		for req_type, chats in result.observations:
			for chat_id, username in chats:
				agg.chat_ids.append(chat_id)
				if not observe_chat(agg.counter, chat_id, username, source, req_type):
					continue
				apply_outcome(agg, outcomes[chat_id], chat_id, username, source)

	return agg


def apply_outcome(agg, outcome, chat_id, username, source):
	for linked_id, linked_username in outcome.linked:
		agg.chat_ids.append(linked_id)
		observe_linked_chat(agg.counter, linked_id, linked_username, source, username)

	if outcome.error == 'not_found':
		agg.users_not_found.append(f'ID - {chat_id}')
	elif outcome.error is not None:
		agg.skipped.append((chat_id, outcome.error))
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
	import pandas as pd

	try:
		chats_path = f'{output_folder}/chats.txt'
		earlier = []
		if os.path.isfile(chats_path):
			with open(chats_path, mode='r', encoding='utf-8') as reader:
				earlier = [line.rstrip() for line in reader]
		ids = dict.fromkeys(earlier + [str(i) for i in agg.chat_ids])
		write_text(chats_path, ''.join(f'{c}\n' for c in ids))

		counter_df = counter_frame(agg.counter)
		replace_file(
			f'{output_folder}/counter.csv',
			lambda tmp: counter_df.to_csv(tmp, encoding='utf-8', index=False)
		)

		csv_path = f'{output_folder}/collected_chats.csv'
		has_rows = os.path.isfile(csv_path) and not _has_no_rows(csv_path)
		if agg.metadata:
			df = pd.DataFrame(agg.metadata).merge(
				counter_df.drop(columns='username'), how='left', on='id'
			)
			if has_rows:
				# text as written; this run's counts never touch earlier rows
				earlier_df = pd.read_csv(
					csv_path, encoding='utf-8', dtype=str, keep_default_na=False
				)
				df = pd.concat([earlier_df, df], ignore_index=True)
			replace_file(
				csv_path,
				lambda tmp: df.to_csv(tmp, index=False, encoding='utf-8')
			)
		elif not has_rows:
			empty = pd.DataFrame(columns=empty_collected_chats_columns())
			replace_file(
				csv_path,
				lambda tmp: empty.to_csv(tmp, index=False, encoding='utf-8')
			)
	except OSError as exc:
		raise StorageError(f'cannot update aggregate files in {output_folder}: {exc}') from exc

	append_lines(f'{output_folder}/{CHANNELS_NOT_FOUND}', agg.channels_not_found)
	append_lines(f'{output_folder}/{USERS_NOT_FOUND}', agg.users_not_found)
