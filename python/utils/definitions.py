# -*- coding: utf-8 -*-

# Import modules
import pandas as pd
import asyncio
import json
import os

# import submodules
from configparser import ConfigParser
from urllib.parse import urlparse
from datetime import datetime

# Import Telegram API submodules
from api import full_channel_req


'''
Creating functions
'''

# Get config attrs
def get_config_attrs():
	'''
	'''
	path = './config/config.ini'
	
	# config parser
	config = ConfigParser()
	config.read(path)

	# Telegram API credentials
	attrs = config['Telegram API credentials']
	return dict(attrs)


# event loop
loop = asyncio.get_event_loop()

'''

JSON Encoder

'''
class JSONEncoder(json.JSONEncoder):
	'''
	'''
	def default(self, o):
		if isinstance(o, datetime) or isinstance(o, bytes):
			return str(o)

		return json.JSONEncoder.default(self, o)

# Create new folders
def create_dirs(root, subfolders=None):
	'''
	'''
	root = root if subfolders == None else f'{root}/{subfolders}/'
	if not os.path.exists(root):
		os.makedirs(f'{root}', exist_ok=True)
	
	return

# Get user-console request
def cmd_request_type(args):
	'''
	'''
	tm_channel = args['telegram_channel']
	batch_file = args['batch_file']

	req_type = 'channel' if tm_channel != None else 'batch'
	req_input = tm_channel if tm_channel != None else batch_file

	return req_type, req_input

# Process collected chats
def process_participants_count(client, channel_id):
	'''

	Returns:
		Participants count
	'''
	channel_request = loop.run_until_complete(
		full_channel_req(client, channel_id)
	)

	return channel_request.full_chat.participants_count


# Write collected chats
def write_collected_chats(
		chats_object,
		file,
		source,
		counter,
		req_type,
		client,
		output_folder
	):
	'''

	chats_object -> chats metadata from API requests: chats:Vector<Chat>
	file -> a txt file to write chats' data (id, username)
	source -> channel requested by the user through cmd
	counter -> dict object to count mentioned channels
	req_type -> request type (channel request or from messages)
	client -> Telegram API client
	output_folder -> Folder to save collected data

	'''
	metadata = []
	for c in chats_object:
		try:
			id_ = c['id']
			username = c['username']
			if username != None:
				file.write(f'{id_}\n')

				# collect metadata
				if id_ in counter.keys():
					counter[id_]['counter'] += 1
					counter[id_][req_type] += 1
					src = counter[id_]['source']
					if source not in src:
						counter[id_]['source'].append(source)
				else:
					counter[id_] = {
						'username': username,
						'counter': 1,
						'from_messages': 1 \
							if req_type == 'from_messages' else 0,
						'channel_request': 1 \
							if req_type == 'channel_request' else 0,
						'channel_req_targeted_by': {
							'channels': ['self']
						},
						'source': [source]
					}

					# Telegram API -> full channel request
					try:
						channel_request = loop.run_until_complete(
							full_channel_req(client, id_)
						)

						channel_request = channel_request.to_dict()
						collected_chats = channel_request['chats']
					
						for ch in collected_chats:
							if ch['id'] == channel_request['full_chat']['id']:
								ch['participants_count'] = \
									channel_request['full_chat']['participants_count']
							else:
								ch_id = ch['id']
								try:
									ch['participants_count'] = \
										process_participants_count(client, ch_id)
								except TypeError:
									ch['participants_count'] = 0

								# write new id
								if ch['username'] != None:
									file.write(f'{ch_id}\n')

									# process in counter
									if ch_id in counter.keys():
										counter[ch_id]['counter'] += 1
										counter[ch_id]['channel_request'] += 1
										counter[ch_id]['channel_request_targeted_by']['channels'].append(username)
									else:
										counter[ch_id] = {
											'username': ch['username'],
											'counter': 1,
											'from_messages': 0,
											'channel_request': 1,
											'channel_req_targeted_by': {
												'channels': [username]
											},
											'source': [source]
										}

						metadata.extend(
							[
								i for i in collected_chats
								if i['username'] != None 
							]
						)
					except ValueError:
						'''
						Save exceptions to new file
						'''
						_o = output_folder
						exceptions_path = f'{_o}/_exceptions-users-not-found.txt'
						w = open(exceptions_path, encoding='utf-8', mode='a')
						w.write(f'ID - {id_}\n')
						w.close()

		except KeyError:
			pass

	df = pd.DataFrame(metadata)
	csv_path = f'{output_folder}/collected_chats.csv'
	df.to_csv(
		csv_path,
		encoding='utf-8',
		mode='a',
		index=False,
		header=not os.path.isfile(csv_path)
	)

	return counter

# Time - date attributes
def timestamp_attrs(data, col='date'):
	'''
	'''
	# process dates
	t = pd.to_datetime(
		data[col],
		infer_datetime_format=True,
		yearfirst=True
	)

	# timestamp attributes
	data[f'{col}'] = t.dt.strftime('%Y-%m-%d %H:%M:%S')
	data[f'{col}_string'] = t.dt.strftime('%Y-%m-%d')
	data[f'{col}_year'] = t.dt.year
	data[f'{col}_month_name'] = t.dt.month_name()
	data[f'{col}_day'] = t.dt.day
	data[f'{col}_day_name'] = t.dt.day_name()
	data[f'{col}_time_hour'] = t.dt.strftime('%H:%M:%S')
	data[f'{col}_quarter'] = t.dt.quarter
	data[f'{col}_dayofyear'] = t.dt.dayofyear
	data[f'{col}_weekofyear'] = t.dt.isocalendar().week

	return data

# Clean msg
def clean_msg(text):
	'''
	'''
	return ' '.join(text.split()).strip()

# Message attributes
def msg_attrs(msg, res):
	'''
	'''
	t = msg['from_id']
	if t:
		# main peer attr
		attr = t['_']

		# parser
		parser = {
			'PeerUser': 'user_id',
			'PeerChat': 'chat_id',
			'PeerChannel': 'channel_id'
		}

		# get attr id
		attr_id = parser[attr]

		# assign attr
		res['msg_from_peer'] = attr
		res['msg_from_id'] = t[attr_id]

	return res

# Get channel name
def get_channel_name(channel_id, channels):
	'''
	'''
	channel_name = None
	try:
		channel_name = channels[
			channels['id'] == channel_id
		]['username'].iloc[0]
	except IndexError:
		pass

	return channel_name

# Get forward attrs
def get_forward_attrs(msg, res, channels_data):
	'''
	'''
	date = msg['date']
	msg_id = msg['channel_post']

	# get from id value
	from_id = msg['from_id']
	if from_id:

		# parser
		parser = {
			'PeerUser': 'user_id',
			'PeerChat': 'chat_id',
			'PeerChannel': 'channel_id'
		}

		attr = from_id['_']
		attr_id = parser[attr]
		attr_id_value = from_id[attr_id]

		channel_name = get_channel_name(attr_id_value, channels_data)
	else:
		attr = None
		attr_id_value = None
		channel_name = None

	# process dates
	t = pd.to_datetime(
		date,
		infer_datetime_format=True,
		yearfirst=True
	)

	date = t.strftime('%Y-%m-%d %H:%M:%S')
	date_string = t.strftime('%Y-%m-%d')

	res['forward_msg_from_peer_type'] = attr
	res['forward_msg_from_peer_id'] = attr_id_value
	res['forward_msg_from_peer_name'] = channel_name
	res['forward_msg_date'] = date
	res['forward_msg_date_string'] = date_string

	if channel_name != None and msg_id != None:
		n = channel_name
		res['forward_msg_link'] = f'https://t.me/{n}/{msg_id}'

	return res

# Get reply attrs
def get_reply_attrs(msg, res, username):
	'''
	'''
	reply = msg['reply_to']
	if reply:
		reply_to_msg_id = msg['reply_to']['reply_to_msg_id']
		res['is_reply'] = 1
		res['reply_to_msg_id'] = reply_to_msg_id
		res['reply_msg_link'] = f'https://t.me/{username}/{reply_to_msg_id}'

	return res

# Get URL domains < netloc >
def get_netloc(value):
	'''
	'''
	N = urlparse(value).netloc
	return N.replace('www.', '')

# Get URL attrs
def get_url_attrs(media, res):
	'''
	Type WebPage

	Source: https://core.telegram.org/constructor/messageMediaWebPage
	Telethon: https://tl.telethon.dev/constructors/web_page.html
	
	'''
	has_url = 0
	url = None
	domain = None
	url_title = None
	url_description = None
	if res['media_type'] == 'MessageMediaWebPage':
		_type = media['webpage']['_']
		if _type == 'WebPage':
			url = media['webpage']['url']
			if url:
				has_url = 1

				# get domain
				domain = get_netloc(url)

				# title
				url_title = media['webpage']['title']
				url_description = media['webpage']['description']

	res['has_url'] = has_url
	res['url'] = url
	res['domain'] = domain
	res['url_title'] = url_title
	res['url_description'] = url_description

	return res

# Get document attrs
def get_document_attrs(media, res):
	'''
	Type Document
	
	Source: https://core.telegram.org/constructor/messageMediaDocument
	Telethon: https://tl.telethon.dev/constructors/document.html
	
	'''
	if res['media_type'] == 'MessageMediaDocument':
		res['document_id'] = media['document']['id']
		res['document_type'] = media['document']['mime_type']

		# get duration
		attrs = media['document']['attributes']
		for i in attrs:
			if i['_'] == 'DocumentAttributeVideo':
				res['document_video_duration'] = i['duration']
			
			if i['_'] == 'DocumentAttributeFilename':
				res['document_filename'] = i['file_name']

	return res

# Get poll attrs
def get_poll_attrs(media, res):
	'''

	Type Poll
	
	Source: https://core.telegram.org/constructor/messageMediaPoll
	Telethon: https://tl.telethon.dev/constructors/poll.html
	
	'''
	if res['media_type'] == 'MessageMediaPoll':
		res['poll_id'] = media['poll']['id']
		res['poll_question'] = media['poll']['question']
		res['poll_total_voters'] = media['results']['total_voters']
		res['poll_results'] = media['results']['results']

	return res

# Get contact attrs
def get_contact_attrs(media, res):
	'''
	Type Contact

	Source: https://core.telegram.org/constructor/messageMediaContact
	Telethon: https://tl.telethon.dev/constructors/message_media_contact.html

	'''
	if res['media_type'] == 'MessageMediaContact':
		res['contact_phone_number'] = media['phone_number']
		res['contact_name'] = media['first_name'] + ' ' + media['last_name']
		res['contact_userid'] = media['user_id']

	return res

# Get geo attrs
def get_geo_attrs(media, res):
	'''

	Type GeoPoint

	Source: https://core.telegram.org/constructor/messageMediaGeo
	Telethon:
	>	https://tl.telethon.dev/constructors/geo_point.html
	>	https://tl.telethon.dev/constructors/message_media_venue.html

	'''
	if media != None:
		if 'geo' in media.keys():
			res['geo_type'] = media['_']
			res['lat'] = media['geo']['lat']
			res['lng'] = media['geo']['long']
		
		if 'venue_id' in media.keys():
			res['venue_id'] = media['venue_id']
			res['venue_type'] = media['venue_type']
			res['venue_title'] = media['title']
			res['venue_address'] = media['address']
			res['venue_provider'] = media['provider']

	return res

# Chats dataset -> columns
def chats_dataset_columns():
	'''
	'''
	return [
		'_',
		'id',
		'username',
		'title',
		'date',
		'left',
		'broadcast',
		'verified',
		'megagroup',
		'restricted',
		'signatures',
		'min',
		'scam',
		'has_link',
		'has_geo',
		'slowmode_enabled',
		'call_active',
		'call_not_empty',
		'fake',
		'gigagroup',
		'restriction_reason',
		'admin_rights',
		'banned_rights',
		'default_banned_rights',
		'participants_count',
		'collected_actions',
		'collected_posts',
		'replies',
		'other_actions',
		'number_views',
		'forwards',
		'replies_received'
	]

# Msgs dataset -> columns
def msgs_dataset_columns():
	'''
	'''
	return [
		'signature',
		'channel_id',
		'channel_name',
		'msg_id',
		'message',
		'cleaned_message',
		'date',
		'msg_link',
		'msg_from_peer',
		'msg_from_id',
		'views',
		'number_replies',
		'number_forwards',
		'is_forward',
		'forward_msg_from_peer_type',
		'forward_msg_from_peer_id',
		'forward_msg_from_peer_name',
		'forward_msg_date',
		'forward_msg_date_string',
		'forward_msg_link',
		'is_reply',
		'reply_to_msg_id',
		'reply_msg_link',
		'contains_media',
		'media_type',
		'has_url',
		'url',
		'domain',
		'url_title',
		'url_description',
		'document_type',
		'document_id',
		'document_video_duration',
		'document_filename',
		'poll_id',
		'poll_question',
		'poll_total_voters',
		'poll_results',
		'contact_phone_number',
		'contact_name',
		'contact_userid',
		'geo_type',
		'lat',
		'lng',
		'venue_id',
		'venue_type',
		'venue_title',
		'venue_address',
		'venue_provider'
	]


'''

Network
'''
def normalize_values(data):
	'''

	data: a list of tuples -> based on G.degree
	'''
	max_v = max([v for i, v in data])
	min_v = min([v for i, v in data])

	return [
		int((i - (min_v)) / (max_v - min_v) * 450) + 50
		for l, i in data 
	]
