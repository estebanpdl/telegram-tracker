# -*- coding: utf-8 -*-
'''
Offline dataset builder: reads collected files under a data folder and
writes msgs_dataset.csv and collected_chats.xlsx.

	build_dataset('./output/data')

Inputs: <data>/collected_chats.csv and every <data>/**/*_messages.json.
Outputs:
	- msgs_dataset.csv: one row per message, appended to an existing file
	  (a rerun duplicates rows; the header is written only once);
	- collected_chats.xlsx: collected_chats.csv with per-channel message
	  statistics, replaced on every run.

All inputs are read and checked before anything is written; outputs are
replaced through temporary files, so a malformed input leaves earlier
outputs untouched. No Telethon, config, or session access.
'''

# import modules
import glob
import json
import os
import shutil

from dataclasses import dataclass, field

# import local submodules
from utils import (
	DataInputError, chats_dataset_columns, clean_msg, discard, msg_attrs,
	temp_path_for,
	get_forward_attrs, get_reply_attrs, get_url_attrs, get_document_attrs,
	get_poll_attrs, get_contact_attrs, get_geo_attrs, msgs_dataset_columns
)


MSGS_DATASET = 'msgs_dataset.csv'
COLLECTED_CHATS = 'collected_chats.csv'

# per-channel statistics added to collected_chats before the XLSX export
STAT_COLUMNS = (
	'collected_actions',
	'collected_posts',
	'replies',
	'other_actions',
	'number_views',
	'forwards',
	'replies_received'
)


@dataclass
class DatasetSummary:
	'''
	Outcome of one dataset build.
	'''
	msgs_dataset: str
	chats_xlsx: str
	channels: list = field(default_factory=list)
	rows_written: int = 0


'''

Inputs

'''

def message_files(data_path):
	'''
	Every <data>/**/*_messages.json, sorted for a stable row order.
	'''
	pattern = os.path.join(glob.escape(data_path), '**', '*_messages.json')
	return sorted(glob.glob(pattern, recursive=True))


def channel_name(file_path):
	'''
	Channel username from a <channel>_messages.json path (baseline rule).
	'''
	return file_path.split('.json')[0].replace('\\', '/').split('/')[-1].replace(
		'_messages', ''
	)


def read_collected_chats(chats_path):
	'''
	collected_chats.csv as a DataFrame; every column the XLSX export
	selects must be present.
	'''
	import pandas as pd

	if not os.path.isfile(chats_path):
		raise DataInputError(f'{chats_path} not found; run collection first')

	try:
		data = pd.read_csv(chats_path, encoding='utf-8')
	except (ValueError, UnicodeDecodeError) as exc:
		raise DataInputError(f'{chats_path} is not a readable CSV: {exc}') from None

	required = [c for c in chats_dataset_columns() if c not in STAT_COLUMNS]
	missing = [c for c in required if c not in data.columns]
	if missing:
		raise DataInputError(
			f'{chats_path} is missing columns: {", ".join(missing)}'
		)

	return data


def read_messages(file_path):
	'''
	A <channel>_messages.json payload: an object with `count` and a list
	of `messages`, each an object with a `_` type.
	'''
	try:
		with open(file_path, encoding='utf-8', mode='r') as fl:
			obj = json.load(fl)
	except (ValueError, UnicodeDecodeError) as exc:
		raise DataInputError(f'{file_path} is not valid JSON: {exc}') from None

	if not isinstance(obj, dict) or 'count' not in obj \
			or not isinstance(obj.get('messages'), list):
		raise DataInputError(
			f'{file_path} is not a messages file (needs "count" and "messages")'
		)

	for item in obj['messages']:
		if not isinstance(item, dict) or '_' not in item:
			raise DataInputError(f'{file_path} has a message without a "_" type')

	return obj


'''

Transforms (baseline logic)

'''

def channel_stats(obj):
	'''
	Per-channel statistics for collected_chats.xlsx.
	'''
	messages = obj['messages']
	return {
		'collected_actions': obj['count'],
		'collected_posts': len(
			[
				i for i in messages if 'message' in i.keys()
				and i['reply_to'] == None
			]
		),
		'replies': len(
			[
				i for i in messages if 'message' in i.keys()
				and i['reply_to'] != None
			]
		),
		'other_actions': len(
			[
				i for i in messages if 'action' in i.keys()
			]
		),
		'number_views': sum(
			[
				i['views'] for i in messages
				if 'views' in i.keys() and i['views'] != None
			]
		),
		'forwards': sum(
			[
				i['forwards'] for i in messages
				if 'forwards' in i.keys() and i['forwards'] != None
			]
		),
		'replies_received': sum(
			[
				i['replies']['replies'] for i in messages
				if 'replies' in i.keys() and i['replies'] != None
			]
		)
	}


def message_rows(messages, username, chats):
	'''
	Yield one msgs_dataset row (dict) per `Message`; service messages are
	skipped. `chats` (collected_chats) resolves forward source names.

	As in the baseline, one response dict is reused across messages and a
	copy is yielded for each row.
	'''
	response = {
		'channel_name': username
	}

	for idx, item in enumerate(messages):
		if item['_'] != 'Message':
			continue

		# channel id
		response['channel_id'] = item['peer_id']['channel_id']

		# message id
		msg_id = item['id']
		response['msg_id'] = msg_id

		# add attrs
		response['message'] = item['message']

		# clean message
		response['cleaned_message'] = clean_msg(item['message'])

		# timestamp
		response['date'] = item['date']

		# signature and message link
		response['signature'] = \
			f'msg_iteration.{idx}.user.{username}.post.{msg_id}'
		response['msg_link'] = f'https://t.me/{username}/{msg_id}'

		# check peer
		response['msg_from_peer'] = None
		response['msg_from_id'] = None
		response = msg_attrs(item, response)

		# reactions
		response['views'] = 0 if item['views'] == None else item['views']
		response['number_replies'] = \
			item['replies']['replies'] if item['replies'] != None else 0
		response['number_forwards'] = 0 if item['forwards'] == None \
			else item['forwards']

		# Forward attrs
		forward_attrs = item['fwd_from']
		response['is_forward'] = 1 if forward_attrs != None else 0

		response['forward_msg_from_peer_type'] = None
		response['forward_msg_from_peer_id'] = None
		response['forward_msg_from_peer_name'] = None
		response['forward_msg_date'] = None
		response['forward_msg_date_string'] = None
		response['forward_msg_link'] = None
		if forward_attrs:
			response = get_forward_attrs(forward_attrs, response, chats)

		# Reply attrs
		response['is_reply'] = 0
		response['reply_to_msg_id'] = None
		response['reply_msg_link'] = None
		response = get_reply_attrs(item, response, username)

		# Media
		response['contains_media'] = 1 if item['media'] != None else 0
		if 'media' in item.keys():
			response['media_type'] = None if item['media'] == None \
				else item['media']['_']

		# URLs -> Constructor MessageMediaWebPage
		# https://core.telegram.org/constructor/messageMediaWebPage
		response = get_url_attrs(item['media'], response)

		# Media Document -> Constructor MessageMediaDocument
		# https://core.telegram.org/constructor/messageMediaDocument
		response['document_type'] = None
		response['document_id'] = None
		response['document_video_duration'] = None
		response['document_filename'] = None
		response = get_document_attrs(item['media'], response)

		# Polls -> https://core.telegram.org/constructor/messageMediaPoll
		response['poll_id'] = None
		response['poll_question'] = None
		response['poll_total_voters'] = None
		response['poll_results'] = None
		response = get_poll_attrs(item['media'], response)

		# Contact -> https://core.telegram.org/constructor/messageMediaContact
		response['contact_phone_number'] = None
		response['contact_name'] = None
		response['contact_userid'] = None
		response = get_contact_attrs(item['media'], response)

		# Geo -> https://core.telegram.org/constructor/messageMediaGeo
		# and https://tl.telethon.dev/constructors/message_media_venue.html
		response['geo_type'] = None
		response['lat'] = None
		response['lng'] = None
		response['venue_id'] = None
		response['venue_type'] = None
		response['venue_title'] = None
		response['venue_address'] = None
		response['venue_provider'] = None
		response = get_geo_attrs(item['media'], response)

		yield dict(response)


'''

Outputs

'''

def build_dataset(data_path, *, progress=True):
	'''
	Build msgs_dataset.csv (append) and collected_chats.xlsx (replace)
	from the collected files in `data_path`. Returns a DatasetSummary.

	Raises DataInputError, before writing anything, when
	collected_chats.csv is missing or lacks columns, or a messages file is
	malformed or produces an incomplete row.
	'''
	import pandas as pd
	from tqdm import tqdm

	chats_path = f'{data_path}/{COLLECTED_CHATS}'
	msgs_path = f'{data_path}/{MSGS_DATASET}'
	xlsx_path = chats_path.replace('.csv', '.xlsx')

	data = read_collected_chats(chats_path)
	for column in STAT_COLUMNS:
		data[column] = 0

	columns = msgs_dataset_columns()
	summary = DatasetSummary(msgs_dataset=msgs_path, chats_xlsx=xlsx_path)

	# Read and transform every channel before writing
	channels = []
	for f in message_files(data_path):
		username = channel_name(f)
		print(f'Reading data from channel -> {username}')
		obj = read_messages(f)

		for column, value in channel_stats(obj).items():
			data.loc[data['username'] == username, column] = value

		messages = obj['messages']
		rows = []
		with tqdm(total=len(messages), disable=not progress) as pbar:
			pbar.set_description('Reading posts')
			try:
				for row in message_rows(messages, username, data):
					rows.append(row)
					pbar.update(1)
			except (KeyError, TypeError, ValueError) as exc:
				raise DataInputError(
					f'{f}: cannot read message fields ({type(exc).__name__}: {exc})'
				) from None

		channels.append((username, rows))
		print('-- END --')
		print('')

	# Append rows to a copy of msgs_dataset.csv, then replace it
	tmp_msgs = temp_path_for(msgs_path)
	tmp_xlsx = temp_path_for(xlsx_path)
	try:
		header = not os.path.isfile(msgs_path)
		if not header:
			shutil.copyfile(msgs_path, tmp_msgs)

		with open(tmp_msgs, mode='a', encoding='utf-8', newline='') as writer:
			for username, rows in channels:
				summary.channels.append(username)
				for row in rows:
					# one frame per row keeps the baseline value formatting
					df = pd.DataFrame.from_dict([row])[columns]
					df.to_csv(writer, header=header, index=False)
					header = False
					summary.rows_written += 1

			if header:
				# no rows: a header-only dataset
				pd.DataFrame(columns=columns).to_csv(writer, index=False)

		data[chats_dataset_columns()].to_excel(tmp_xlsx, index=False)

		os.replace(tmp_msgs, msgs_path)
		os.replace(tmp_xlsx, xlsx_path)
	finally:
		discard(tmp_msgs)
		discard(tmp_xlsx)

	return summary
