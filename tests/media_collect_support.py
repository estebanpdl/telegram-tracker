# -*- coding: utf-8 -*-
'''
`collect --include-media` against fakes, through cli.main on the
virtual-time loop (storage_modes_support.ModesCase).

MediaWorld is the fixture world whose channel histories are pages of
real Telethon messages: the posts of a FakeTelegram file service
(media_download_support), so the collector's to_dict path and the media
capture read the objects Telegram returns. MediaClient is the
collector's FakeTelegramClient plus that service's iter_download and
message lookups (its request seam answers GetFile and GetMessages).

Every request goes on one timeline, `client.timeline`. record_events()
adds the writer's commits and the ledger's writes to it, with whether
the output folder was locked at each ledger write.

Synthetic data only.
'''

# import modules
import contextlib
import os
import sqlite3

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from telethon import types
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.functions.upload import GetFileRequest

from fakes import FakeTelegramClient, FixtureWorld
from media_download_support import DATE, FakeTelegram, content
from storage_modes_support import PAGE_SIZE, ModesCase

from telegram_tracker.media.collect import MediaRun
from telegram_tracker.media.layout import LEDGER_FILE, MEDIA_FOLDER
from telegram_tracker.media.ledger import Ledger
from telegram_tracker.storage.destination import Destination
from telegram_tracker.storage.records import LOCK_FILE


ALPHA = 101
BETA = 102
KIB = 1024
PDF = [types.DocumentAttributeFilename(file_name='report.pdf')]
AUDIO = [types.DocumentAttributeAudio(duration=3, title='t')]


class MediaWorld(FixtureWorld):
	'''
	The fixture world (entities, full channels) with histories served
	from `files.messages`, newest first, as real ChannelMessages.
	`served` lists the message IDs of each page.
	'''
	def __init__(self, files):
		super().__init__()
		self.files = files
		self.served = []

	def page(
			self, channel_id, *, offset_id=0, min_id=0, max_id=0, offset_date=None, limit=0,
			page_size=PAGE_SIZE):
		history = sorted(
			(m for (channel, _), m in self.files.messages.items() if channel == channel_id),
			key=lambda m: m.id, reverse=True
		)
		selected = [
			m for m in history
			if (not offset_id or m.id < offset_id) and m.id > (min_id or 0) and (not max_id or m.id < max_id)
			and (not offset_date or m.date < offset_date)
		][:min(page_size, limit) if limit else page_size]
		self.served.append([m.id for m in selected])
		return types.messages.ChannelMessages(
			pts=1, count=len(history), messages=selected, chats=[], users=[], topics=[])


class MediaClient(FakeTelegramClient):
	'''
	FakeTelegramClient with a FakeTelegram file service (`files`).
	'''
	def __init__(self, files=None, **kwargs):
		self.files = files or FakeTelegram()
		kwargs.setdefault('page_size', PAGE_SIZE)
		super().__init__(MediaWorld(self.files), **kwargs)
		self.timeline = []
		answer = self.files._call

		async def recorded(sender, request, ordered=False, flood_sleep_threshold=None):
			if isinstance(request, GetFileRequest):
				self.timeline.append(('GetFile', request.location.id, request.offset))
			elif isinstance(request, GetMessagesRequest):
				self.timeline.append(('GetMessages', request.channel.channel_id))
			return await answer(sender, request, ordered, flood_sleep_threshold)

		self.files.client._call = recorded

	def iter_download(self, *args, **kwargs):
		return self.files.client.iter_download(*args, **kwargs)

	async def __call__(self, request, ordered=False):
		if isinstance(request, GetMessagesRequest):
			return await self.files.client(request)
		kind = type(request).__name__
		self.timeline.append((kind, getattr(request, 'peer', getattr(request, 'channel', None))))
		return await super().__call__(request, ordered)

	def kinds(self, *names):
		return [event for event in self.timeline if event[0] in names]

	def index(self, kind, *, last=False):
		found = [i for i, event in enumerate(self.timeline) if event[0] == kind]
		return (found[-1] if last else found[0]) if found else None


'''

Content

'''


def video(files, media_id, size, seed=0):
	return files.add_document(media_id, content(size, seed), mime='video/mp4')


def photo(files, media_id, size, seed=0):
	return files.add_photo(media_id, content(size, seed))


def document(files, media_id, size, seed=0, *, mime='application/pdf', attributes=PDF):
	return files.add_document(media_id, content(size, seed), mime=mime, attributes=attributes)


def text_post(files, channel_id, message_id):
	files.messages[(channel_id, message_id)] = types.Message(
		id=message_id, peer_id=types.PeerChannel(channel_id), date=DATE, message=f'post {message_id}')


def standard_world(**options):
	'''
	alpha_fx (101): 1 photo, 2 video (two pulls), 3 text, 4 audio,
	5 PDF, 6 the video of 2 again. beta_fx (102): 1 video, 2 photo.
	Returns the MediaClient (`options` as for FakeTelegramClient).
	'''
	client = MediaClient(**options)
	files = client.files
	files.post(1, photo(files, 9001, 3000, 1), ALPHA)
	files.post(2, video(files, 7001, 600 * KIB, 2), ALPHA)
	text_post(files, ALPHA, 3)
	files.post(4, document(files, 7002, 5000, 3, mime='audio/mpeg', attributes=AUDIO), ALPHA)
	files.post(5, document(files, 7003, 7000, 4), ALPHA)
	files.post(6, files.document_media(7001, size=600 * KIB), ALPHA)
	files.post(1, video(files, 7101, 9000, 5), BETA)
	files.post(2, photo(files, 9101, 2000, 6), BETA)
	return client


'''

Events

'''


@contextlib.contextmanager
def record_events(client, output_folder):
	'''
	Put the writer's commits ('commit', channel_id), the ledger's
	requested rows ('ledger_request', channel_id) and every ledger write
	('ledger_write', action, locked) on client.timeline.
	'''
	timeline = client.timeline
	lock = os.path.join(output_folder, LOCK_FILE)
	open_writer = Destination.open_writer
	request = MediaRun.request
	write = Ledger._write

	def wrapped_open_writer(self, **kwargs):
		writer = open_writer(self, **kwargs)
		commit = writer.commit

		def recorded(result):
			takeover = commit(result)
			timeline.append(('commit', result.channel_id))
			return takeover

		writer.commit = recorded
		return writer

	def wrapped_request(self, result):
		timeline.append(('ledger_request', result.channel_id))
		return request(self, result)

	@contextlib.contextmanager
	def wrapped_write(self, action):
		timeline.append(('ledger_write', action, os.path.exists(lock)))
		with write(self, action) as con:
			yield con

	with mock.patch.object(Destination, 'open_writer', wrapped_open_writer), \
			mock.patch.object(MediaRun, 'request', wrapped_request), \
			mock.patch.object(Ledger, '_write', wrapped_write):
		yield timeline


'''

Case

'''


class MediaCase(ModesCase):
	'''
	ModesCase with a MediaClient (standard_world unless given) and
	readers for the folder's ledger.
	'''

	def make_client(self, raw=None):
		return standard_world()

	def media(self, *extra, **kwargs):
		return self.collect('--include-media', *extra, **kwargs)

	def ledger_path(self, out=None):
		return os.path.join(out or self.out, MEDIA_FOLDER, LEDGER_FILE)

	def ledger_rows(self, sql, *args, out=None):
		con = sqlite3.connect(f'file:{self.ledger_path(out)}?mode=ro', uri=True)
		con.row_factory = sqlite3.Row
		try:
			return [dict(row) for row in con.execute(sql, args)]
		finally:
			con.close()

	def items(self, out=None):
		return self.ledger_rows('SELECT * FROM items ORDER BY channel_id, message_id', out=out)

	def states(self, out=None):
		return {
			(i['channel_id'], i['message_id']): (i['state'], i['reason']) for i in self.items(out)
		}

	def runs(self, out=None):
		return self.ledger_rows('SELECT * FROM media_runs ORDER BY started_at, rowid', out=out)

	def stored(self, relative, out=None):
		with open(os.path.join(out or self.out, MEDIA_FOLDER, *relative.split('/')), 'rb') as reader:
			return reader.read()

	def media_files(self, out=None):
		'''
		Relative paths of the files under downloaded-media, without the
		ledger and partial files.
		'''
		root = os.path.join(out or self.out, MEDIA_FOLDER)
		found = []
		for folder, _, names in os.walk(root):
			for name in names:
				path = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, '/')
				if not path.startswith('.partial/') and not path.startswith(LEDGER_FILE):
					found.append(path)
		return sorted(found)

	def partial_files(self, out=None):
		root = os.path.join(out or self.out, MEDIA_FOLDER, '.partial')
		return sorted(
			os.path.relpath(os.path.join(folder, name), root).replace(os.sep, '/')
			for folder, _, names in os.walk(root) for name in names
		)
