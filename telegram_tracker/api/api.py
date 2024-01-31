# -*- coding: utf-8 -*-

# import Telethon API modules
from telethon import TelegramClient, types
from telethon.tl.functions.channels import GetChannelsRequest, \
	GetFullChannelRequest, GetParticipantsRequest
from telethon.tl.functions.messages import GetHistoryRequest, \
	GetDiscussionMessageRequest, GetWebPageRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.functions.stats import GetBroadcastStatsRequest


'''

Client-side

'''

# get connection
async def get_connection(session_file, api_id, api_hash, phone):
	'''
	Connects to Telegram API
	'''
	client = TelegramClient(session_file, api_id, api_hash)
	await client.connect()
	if await client.is_user_authorized():
		print ('> Authorized!')
	else:
		print ('> Not Authorized! Sending code request...')
		await client.send_code_request(phone)
		await client.sign_in(
			phone,
			input('Enter the code: ')
		)

	return client


'''

Channels

'''

# get telegram channel main attrs 
async def get_entity_attrs(client, source):
	'''
	Source: entity (str | int | Peer | InputPeer)
		More on InputPeer: https://tl.telethon.dev/types/input_peer.html

	Reference:
		Telethon: https://docs.telethon.dev/en/latest/modules/client.html#telethon.client.users.UserMethods.get_entity
		Output attrs: https://core.telegram.org/constructor/channel

	'''
	try:
		value = await client.get_entity(source)
	except ValueError:
		value = False
	
	return value

# get channel request
async def get_channel_req(client, source):
	'''
	Source: <ChannelInput>

	Reference:
		Telethon: https://tl.telethon.dev/methods/channels/get_channels.html
		Output attrs: https://core.telegram.org/constructor/chat
	'''
	if type(source) != list:
		source = [source]

	return await client(
		GetChannelsRequest(source)
	)

# get full channel request
async def full_channel_req(client, source):
	'''
	Source: <ChannelInput>

	Reference:
		Telethon: https://tl.telethon.dev/methods/channels/get_full_channel.html
		Output attrs: https://core.telegram.org/constructor/messages.chatFull
	'''

	return await client(
		GetFullChannelRequest(source)
	)

# get participants request
async def get_participants_request(client, source):
	'''
	'''
	return await client(
		GetParticipantsRequest(
			channel=source,
			filter=types.ChannelParticipantsRecent(),
			offset=1,
			limit=10,
			hash=0
		)
	)


'''

Messages

'''

# get posts
async def get_posts(client, source, min_id=0, offset_id=0):
	'''
	Source: entity (str | int | Peer | InputPeer)
		More on InputPeer: https://tl.telethon.dev/types/input_peer.html

	Reference:
		Telethon: https://tl.telethon.dev/methods/messages/get_history.html
		Output attrs: https://core.telegram.org/constructor/messages.channelMessages
	'''

	return await client(
		GetHistoryRequest(
			peer=source,
			hash=0,
			limit=100,
			max_id=0,
			min_id=min_id,
			offset_id=offset_id,
			add_offset=0,
			offset_date=0,
		)
	)

# get full chat request
async def get_discussion_message(client, source, msg_id):
	'''
	Source: entity (str | int | Peer | InputPeer)
		More on InputPeer: https://tl.telethon.dev/types/input_peer.html
	msg_id: <message id>

	Reference:
		Telethon: https://tl.telethon.dev/methods/messages/get_discussion_message.html
		Output attrs: https://core.telegram.org/constructor/messages.discussionMessage
	'''

	return await client(
		GetDiscussionMessageRequest(
			peer=source,
			msg_id=msg_id
		)
	)

# get webpage
async def get_web_page(client, url, hash):
	'''
	url: <web url>
	hash: <pagination> adding 0 by default.

	Reference:
		Telethon: https://tl.telethon.dev/methods/messages/get_web_page.html
		Output attrs: https://core.telegram.org/constructor/webPage
	'''
	return await client(
		GetWebPageRequest(url, hash)
	)


'''

Users

'''

# get full user request
async def full_user_req(client, source, channel):
	'''
	Source: <InputUser>

	Reference:
		Telethon: https://tl.telethon.dev/methods/users/get_full_user.html
		Output attrs:
	'''
	try:
		user = await client(
			GetFullUserRequest(source)
		)

		return user
	except ValueError:
		users = await client.get_participants(channel, aggressive=True)
		return users


'''

Photos

'''

# photos request
async def photos_request(client, user_input):
	'''
	'''
	return await client(
		GetUserPhotosRequest(
			user_id=user_input,
			offset=0,
			max_id=0,
			limit=5
		)
	)


'''

Stats

'''

async def broadcast_stats_req(client, source):
	'''

	Source: <InputChannel>

	Reference:
		Telethon: https://tl.telethon.dev/methods/stats/get_broadcast_stats.html
		Output attrs: https://core.telegram.org/constructor/stats.broadcastStats
	'''
	return await client(
		GetBroadcastStatsRequest(
			channel=source
		)
	)
