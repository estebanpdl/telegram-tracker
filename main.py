# -*- coding: utf-8 -*-

# import modules
import pandas as pd
import asyncio
import json
import time
import sys
import os

# arguments
from argparse import (
	ArgumentParser, HelpFormatter, RawTextHelpFormatter, SUPPRESS
)

# import Telegram API submodules
from api import *
from utils import (
	get_config_attrs, JSONEncoder, create_dirs, determine_request_type,
	write_collected_chats
)

# api requests
from telegram_requests import RequestFactory


'''
Arguments

'''
formatter = lambda prog: RawTextHelpFormatter(
	prog,
	indent_increment=2,
	max_help_position=52,
	width=None
)
parser = ArgumentParser(
	prog='Telegram Tracker',
	description='Command Line Arguments.',
	formatter_class=formatter,
	add_help=False
)

# help arguments
help_arguments = parser.add_argument_group('Help options')
help_arguments.add_argument(
	'-h',
	'--help',
	action='help',
	default=SUPPRESS,
	help='Show this help message and exit.'
)

# required arguments
required_arguments = parser.add_argument_group('Required options')

# Telegram Channel
required_arguments.add_argument(
	'--telegram-channel',
	type=str,
	required='--multi-channel-file' not in sys.argv and '--search' not in sys.argv,
	help='Specify a single Telegram Channel to process.'
)

# Multi channel File
required_arguments.add_argument(
	'--multi-channel-file',
	type=str,
	required='--telegram-channel' not in sys.argv and '--search' not in sys.argv,
	help='Provide a .txt file with multiple Telegram Channels, one per line, to process.'
)

# Search
required_arguments.add_argument(
	'--search',
	type=str,
	required='--telegram-channel' not in sys.argv and '--multi-channel-file' not in sys.argv,
	help='Run a search across the channels you follow using a query or keyword string.'
)

# optional arguments
optional_arguments = parser.add_argument_group('General options')

# Collects only metadata from channels - no messages
optional_arguments.add_argument(
	'--channel-metadata-only',
	action='store_true',
	help='Collect only metadata from channels, excluding messages.'
)

# Privacy and compliance with data protection regulations
optional_arguments.add_argument(
	'--anonymize-data',
	action='store_true',
	help='Activates anonymization of personal data for privacy compliance.'
)

# Multithreadig
optional_arguments.add_argument(
	'--multithreading',
	action='store_true',
	help='Improves download speeds by concurrently processing multiple data requests.'
)

# Max threads
optional_arguments.add_argument(
	'--max-threads',
	type=int,
	required='--multithreading' in sys.argv,
	help='Sets the maximum number of threads for improved processing speed.'
)


'''
Data storage options

'''
database_storage_arguments = parser.add_argument_group(
	'Data storage options'
)

database_storage_arguments.add_argument(
	'--output',
	type=str,
	required=False,
	help='Specify the output folder for saved data. Default: `./output/data`.'
)
database_storage_arguments.add_argument(
	'--create-database-file',
	action='store_true',
	help='Create a new local SQLite database (.db file) to store data.'
)

'''
Updating database


1. No min-id
2. Confirm update < Update: True >
3. Database path
4. For performance purposes, indicate which channels will be updated via a txt file
5. Maybe jusgt add new channels to database...
	- id --multi-channel-file + update_database: True = add new channels to database
'''

# DEPRECATED
'''
optional_arguments.add_argument(
	'--min-id',
	type=int,
	help='Specifies the offset id. This will update Telegram data with new posts.'
)
'''
database_storage_arguments.add_argument(
	'--update-database-file',
	action='store_true',
	help='Update an existing local SQLite database with new data.'
)

# if < create database > OR < update database >, database file path is required
database_storage_arguments.add_argument(
	'--database-file-path',
	type=str,
	required='--create-database' in sys.argv,
	help='Specify the path for the local SQLite database file.'
)

'''
Data Storage Engines


database_engines_arguments = parser.add_argument_group(
	'Data engines options'
)

database_engines_arguments.add_argument(
	'--postgresql',
	type=str,
	help='Specify a PostgreSQL environment.'
)

'''

# parse arguments
args = vars(parser.parse_args())
config_attrs = get_config_attrs()
args = {**args, **config_attrs}

# required arguments
required_arguments = [
	'telegram_channel', 'multi_channel_file', 'search'
]

# count how many of these keys have non-None values
non_none_count = sum(args.get(key) is not None for key in required_arguments)
if non_none_count != 1:
	e = 'Please provide exactly one option: --telegram-channel, \
		multi-channel-file, or --search. These options are mutually exclusive \
		and cannot be used together.'
	parser.error(
		' '.join(e.split()).strip()
	)

# log results
text = f'''
Program starting at {time.ctime()}

'''
print (text)

'''
Variables

'''

# Telegram API credentials

'''

FILL API KEYS
'''
sfile = 'session_file'
api_id = args['api_id']
api_hash = args['api_hash']
phone = args['phone']

# event loop
loop = asyncio.get_event_loop()

'''
> Get Client <API connection>

'''

# get `client` connection
client = loop.run_until_complete(
	get_connection(sfile, api_id, api_hash, phone)
)

'''
Identify request type

- channels
	- single channel
	- multi channel file
- search

'''

# request type
req_type, req_input = determine_request_type(args)
api_request = RequestFactory.create_request(req_type, req_input)

# execute api request

'''
API request

'''
print (api_request)










































# if req_type == 'multi_channel':
# 	req_input = [
# 		i.rstrip() for i in open(
# 			req_input, encoding='utf-8', mode='r'
# 		)
# 	]
# else:
# 	req_input = [req_input]






# # reading | Creating an output folder
# if args['output']:
# 	output_folder = args['output']
# 	if output_folder.endswith('/'):
# 		output_folder = output_folder[:-1]
# 	else:
# 		pass
# else:
# 	output_folder = './output/data'

# # create dirs
# create_dirs(output_folder)











# '''
# 1. Telegram Channel
# 2. Multi channel File
# ---> Have the same method - Historical data

# 3. Global Search
# ---> New method


# I. Both methods should consider updating database - based on min-id argument
# 	a. If update_database, read database path.
# 	b. If user indicated channels, get a list of those channels
# 	c. Read database, get min_id value.
# 	d. Based on min_id value, download new messages.
# '''













# '''

# Methods

# - GetHistoryRequest
# - SearchGlobalRequest

# '''








# '''

# GLOBAL SEARCH Approach
# '''


# # query = 'Telethon'
# # d = loop.run_until_complete(
# # 	global_search(client, query)
# # )


# # data = d.to_dict()

# # # Get offset ID | Get messages
# # offset_id = min([i['id'] for i in data['messages']])
# # offset_rate = data['next_rate']

# # while True:
# # 	print (offset_rate)
# # 	d = loop.run_until_complete(
# # 		global_search(
# # 			client,
# # 			query,
# # 			offset_rate=offset_rate - 1,
# # 			offset_id=offset_id
# # 		)
# # 	)

# # 	# Update data dict
# # 	if d.messages:
# # 		tmp = d.to_dict()
# # 		data['messages'].extend(tmp['messages'])
	
# # 		# Get offset ID
# # 		offset_id = min([i['id'] for i in tmp['messages']])
# # 		offset_rate = tmp['next_rate']

# # 		if len(d.messages) < 100:
# # 			break

# # # JsonEncoder
# # data = JSONEncoder().encode(data)
# # data = json.loads(data)

# # # save data
# # print ('> Writing posts data...')
# # file_path = 'D:/i/dfrlab/messages.json'
# # obj = json.dumps(
# # 	data,
# # 	ensure_ascii=False,
# # 	separators=(',',':')
# # )

# # # writer
# # writer = open(file_path, mode='w', encoding='utf-8')
# # writer.write(obj)
# # writer.close()
# # print ('> done.')
# # print ('')













# # data collection
# counter = {}

# # iterate channels
# for channel in req_input:

# 	'''

# 	Process arguments
# 	-> channels' data

# 	-> Get Entity <Channel's attrs>
# 	-> Get Full Channel request.
# 	-> Get Posts <Request channels' posts>

# 	'''

# 	# new line
# 	print ('')
# 	print (f'> Collecting data from Telegram Channel -> {channel}')
# 	print ('> ...')
# 	print ('')

# 	# Channel's attributes
# 	entity_attrs = loop.run_until_complete(
# 		get_entity_attrs(client, channel)
# 	)

# 	if entity_attrs:

# 		# Get Channel ID | convert output to dict
# 		channel_id = entity_attrs.id
# 		entity_attrs_dict = entity_attrs.to_dict()

# 		# Collect Source -> GetFullChannelRequest
# 		channel_request = loop.run_until_complete(
# 			full_channel_req(client, channel_id)
# 		)

# 		# save full channel request
# 		full_channel_data = channel_request.to_dict()

# 		# JsonEncoder
# 		full_channel_data = JSONEncoder().encode(full_channel_data)
# 		full_channel_data = json.loads(full_channel_data)

# 		# save data
# 		print ('> Writing channel data...')
# 		create_dirs(output_folder, subfolders=channel)
# 		file_path = f'{output_folder}/{channel}/{channel}.json'
# 		channel_obj = json.dumps(
# 			full_channel_data,
# 			ensure_ascii=False,
# 			separators=(',',':')
# 		)
# 		writer = open(file_path, mode='w', encoding='utf-8')
# 		writer.write(channel_obj)
# 		writer.close()
# 		print ('> done.')
# 		print ('')

# 		# collect chats
# 		chats_path = f'{output_folder}/chats.txt'
# 		chats_file = open(chats_path, mode='a', encoding='utf-8')

# 		# channel chats
# 		counter = write_collected_chats(
# 			full_channel_data['chats'],
# 			chats_file,
# 			channel,
# 			counter,
# 			'channel_request',
# 			client,
# 			output_folder
# 		)

# 		if not args['limit_download_to_channel_metadata']:

# 			# Collect posts
# 			if not args['min_id']:
# 				posts = loop.run_until_complete(
# 					get_posts(client, channel_id)
# 				)

# 			else:
# 				min_id = args['min_id']
# 				posts = loop.run_until_complete(
# 					get_posts(client, channel_id, min_id=min_id)
# 				)

# 			data = posts.to_dict()

# 			# Get offset ID | Get messages
# 			offset_id = min([i['id'] for i in data['messages']])

# 			while len(posts.messages) > 0:
				
# 				if args['min_id']:
# 					posts = loop.run_until_complete(
# 						get_posts(
# 							client,
# 							channel_id,
# 							min_id=min_id,
# 							offset_id=offset_id
# 						)
# 					)	
# 				else:
# 					posts = loop.run_until_complete(
# 						get_posts(
# 							client,
# 							channel_id,
# 							offset_id=offset_id
# 						)
# 					)

# 				# Update data dict
# 				if posts.messages:
# 					tmp = posts.to_dict()
# 					data['messages'].extend(tmp['messages'])

# 					# Adding unique chats objects
# 					all_chats = [i['id'] for i in data['chats']]
# 					chats = [
# 						i for i in tmp['chats']
# 						if i['id'] not in all_chats
# 					]

# 					# channel chats in posts
# 					counter = write_collected_chats(
# 						tmp['chats'],
# 						chats_file,
# 						channel,
# 						counter,
# 						'from_messages',
# 						client,
# 						output_folder
# 					)

# 					# Adding unique users objects
# 					all_users = [i['id'] for i in data['users']]
# 					users = [
# 						i for i in tmp['users']
# 						if i['id'] not in all_users
# 					]

# 					# extend UNIQUE chats & users
# 					data['chats'].extend(chats)
# 					data['users'].extend(users)

# 					# Get offset ID
# 					offset_id = min([i['id'] for i in tmp['messages']])

# 			# JsonEncoder
# 			data = JSONEncoder().encode(data)
# 			data = json.loads(data)

# 			# save data
# 			print ('> Writing posts data...')
# 			file_path = f'{output_folder}/{channel}/{channel}_messages.json'
# 			obj = json.dumps(
# 				data,
# 				ensure_ascii=False,
# 				separators=(',',':')
# 			)
			
# 			# writer
# 			writer = open(file_path, mode='w', encoding='utf-8')
# 			writer.write(obj)
# 			writer.close()
# 			print ('> done.')
# 			print ('')

# 		# sleep program for a few seconds
# 		if len(req_input) > 1:
# 			time.sleep(2)
# 	else:
# 		'''

# 		Channels not found
# 		'''
# 		exceptions_path = f'{output_folder}/_exceptions-channels-not-found.txt'
# 		w = open(exceptions_path, encoding='utf-8', mode='a')
# 		w.write(f'{channel}\n')
# 		w.close()

# '''

# Clean generated chats text file

# '''

# # close chat file
# chats_file.close()





# # get collected chats
# collected_chats = list(set([
# 	i.rstrip() for i in open(chats_path, mode='r', encoding='utf-8')
# ]))

# # re write collected chats
# chats_file = open(chats_path, mode='w', encoding='utf-8')
# for c in collected_chats:
# 	chats_file.write(f'{c}\n')

# # close file
# chats_file.close()


# # Process counter
# counter_df = pd.DataFrame.from_dict(
# 	counter,
# 	orient='index'
# ).reset_index().rename(
# 	columns={
# 		'index': 'id'
# 	}
# )

# # save counter
# counter_df.to_csv(
# 	f'{output_folder}/counter.csv',
# 	encoding='utf-8',
# 	index=False
# )

# # merge dataframe
# df = pd.read_csv(
# 	f'{output_folder}/collected_chats.csv',
# 	encoding='utf-8'
# )

# del counter_df['username']
# df = df.merge(counter_df, how='left', on='id')
# df.to_csv(
# 	f'{output_folder}/collected_chats.csv',
# 	index=False,
# 	encoding='utf-8'
# )













# log results
text = f'''
End program at {time.ctime()}

'''
print (text)
