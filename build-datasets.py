# -*- coding: utf-8 -*-

# import modules
import pandas as pd
import argparse
import json
import glob
import time
import os

# import submodules
from tqdm import tqdm

# import local submodules
from utils import (
	msgs_dataset_columns, chats_dataset_columns, clean_msg,
	msg_attrs, get_forward_attrs, get_reply_attrs, get_url_attrs,
	get_document_attrs, get_poll_attrs, get_contact_attrs,
	get_geo_attrs, timestamp_attrs
)

'''

Arguments

'''

parser = argparse.ArgumentParser(description='Arguments.')
parser.add_argument(
	'--data-path',
	'-d',
	type=str,
	required=False,
	help='Path where data is located. Will use `./output/data` if not given.'
)

# Parse arguments
args = vars(parser.parse_args())

# get main path
if args['data_path']:
	main_path = args['data_path']
	if main_path.endswith('/'):
		main_path = main_path[:-1]
else:
	main_path = './output/data'

# log results
text = f'''
Init program at {time.ctime()}

'''
print (text)

# Collect JSON files
json_files_path = f'{main_path}/**/*_messages.json'
json_files = glob.glob(
	os.path.join(json_files_path),
	recursive=True
)

# Collected channels
chats_file_path = f'{main_path}/collected_chats.csv'
data = pd.read_csv(chats_file_path, encoding='utf-8')

# Init values
data['collected_actions'] = 0
data['collected_posts'] = 0
data['replies'] = 0
data['other_actions'] = 0
data['number_views'] = 0
data['forwards'] = 0
data['replies_received'] = 0

# Process posts < init empty dataset >
dataset_columns = msgs_dataset_columns()

# Save dataset
msgs_file_path = f'{main_path}/msgs_dataset.csv'

# JSON files
for f in json_files:
	'''

	Iterate JSON files
	'''
	#  Get channel name
	username = f.split('.json')[0].replace('\\', '/').split('/')[-1].replace(
		'_messages', ''
	)

	# Echo
	print (f'Reading data from channel -> {username}')

	# read JSON file
	with open(f, encoding='utf-8', mode='r') as fl:
		obj = json.load(fl)
		fl.close()

	'''

	Get actions
	'''
	actions = obj['count']
	posts = len(
		[
			i for i in obj['messages'] if 'message' in i.keys()
			and i['reply_to'] == None
		]
	)
	replies = len(
		[
			i for i in obj['messages'] if 'message' in i.keys()
			and i['reply_to'] != None
		]
	)
	other = len(
		[
			i for i in obj['messages'] if 'action' in i.keys()
		]
	)

	'''

	Attrs: views, forwards, replies
	'''
	views = sum(
		[
			i['views'] for i in obj['messages']
			if 'views' in i.keys() and i['views'] != None
		]
	)
	forwards = sum(
		[
			i['forwards'] for i in obj['messages']
			if 'forwards' in i.keys() and i['forwards'] != None
		]
	)
	replies_received = sum(
		[
			i['replies']['replies'] for i in obj['messages']
			if 'replies' in i.keys() and i['replies'] != None
		]
	)

	# Add values to dataset
	data.loc[data['username'] == username, 'collected_actions'] = actions
	data.loc[data['username'] == username, 'collected_posts'] = posts
	data.loc[data['username'] == username, 'replies'] = replies
	data.loc[data['username'] == username, 'other_actions'] = other
	data.loc[data['username'] == username, 'number_views'] = views
	data.loc[data['username'] == username, 'forwards'] = forwards
	data.loc[data['username'] == username, 'replies_received'] = replies_received

	'''

	Reading posts
	'''
	messages = obj['messages']
	pbar = tqdm(total=len(messages))
	pbar.set_description(f'Reading posts')

	# main object
	response = {
		'username': username
	}

	for idx, item in enumerate(messages):
		'''

		Iterate posts
		'''
		try:
			response['channel_id'] = item['peer_id']['channel_id']
			msg = clean_msg(item['message'])
			date = item['date']

			# Add attrs
			response['message'] = msg
			response['date'] = date

			# Signature and Message link
			msg_id = item['id']
			response['signature'] = \
				f'msg_iteration.{idx}.user.{username}.post.{msg_id}'
			response['msg_link'] = f'https://t.me/{username}/{msg_id}'

			# Check peer
			response = msg_attrs(item, response)

			# Reactions
			response['views'] = item['views']
			response['number_replies'] = \
				item['replies']['replies'] if item['replies'] != None else 0
			response['number_forwards'] = item['forwards']

			# Forward attrs
			forward_attrs = item['fwd_from']
			response['is_forward'] = 1 if forward_attrs != None else 0
			response['forward_msg_date'] = None
			response['forward_msg_date_string'] = None
			response['forward_msg_link'] = None
			response['from_channel_id'] = None
			response['from_channel_name'] = None
			if forward_attrs:
				response = get_forward_attrs(
					forward_attrs,
					response,
					data
				)

			# Reply attrs
			response = get_reply_attrs(
				item,
				response,
				username
			)

			# Media
			response['contains_media'] = 1 if item['media'] != None else 0
			response['media_type'] = None if item['media'] == None \
				else item['media']['_']

			# URLs
			response = get_url_attrs(item['media'], response)
			response['document_type'], response['video_duration_secs'] = \
				get_document_attrs(item['media'], response)

			# Polls
			response['poll_question'], response['poll_number_results'] = \
				get_poll_attrs(item['media'], response)

			# Contact
			response['contact_phone_number'], response['contact_name'], \
				response['contact_userid'] = get_contact_attrs(
					item['media'],
					response
				)

			# Geo attrs
			response = get_geo_attrs(item['media'], response)

			# create dataframe
			df = pd.json_normalize(response)
			
			# Update CSV file
			df.to_csv(
				msgs_file_path,
				encoding='utf-8',
				header=not os.path.isfile(msgs_file_path),
				index=False,
				mode='a'
			)

		except KeyError:
			pass
		
		# Update pbar
		pbar.update(1)

	# Close pbar connection
	pbar.close()

	print ('-- END --')
	print ('')

# Save data
chats_columns = chats_dataset_columns()
data = data[chats_columns].copy()

data.to_excel(
	chats_file_path.replace('.csv', '.xlsx'),
	index=False
)
