# -*- coding: utf-8 -*-
'''
A fake Telegram file service for the download adapter tests.

FakeTelegram answers the requests of Telethon's real iter_download and
get-messages code. Its `client` is a TelegramClient object made without
__init__, so it never builds a connection (the offline guard blocks a
real client); the request seam `_call` and the exported-sender
functions are answered here instead:

- files live on a data center; a GetFile sent to another one gets
  FILE_MIGRATE, as the server does, and Telethon borrows that DC's sender;
- a GetFile with a file reference that is not the current one gets
  FILE_REFERENCE_EXPIRED; expire() issues a new reference, and the
  message fetched again carries it;
- GetFile offset and limit must follow the API's rules (4 KiB multiples,
  a limit dividing 1 MiB, no request crossing a 1 MiB boundary);
- faults are scripted per request (an error, a hang, or an action), and
  every request is recorded with the sender's DC, its offset and limit,
  and whether the ledger was inside a transaction at that moment.

File content is a byte pattern that depends on the position, so a gap
or a repeated chunk changes the result.
'''

# import modules
import asyncio
import datetime
import hashlib
import logging
import os
import tempfile
import types as pytypes

from dataclasses import dataclass, field

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telethon import TelegramClient, errors, types
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.functions.upload import GetFileRequest

from media_support import PROFILE_ID, RUN_A, SELECTION
from telegram_tracker.media.classify import classify
from telegram_tracker.media.download import MediaDownloader, MediaJob
from telegram_tracker.media.identity import ensure_identity
from telegram_tracker.media.layout import MediaLayout
from telegram_tracker.media.ledger import open_ledger
from telegram_tracker.media.limits import PULL_BYTES, MediaLimits
from telegram_tracker.media.location import location_of
from telegram_tracker.request_policy import RequestPolicy


HOME_DC = 2
OTHER_DC = 4
CHANNEL = 1001
ACCESS_HASH = 777
MIB = 1024 * 1024
DATE = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)


def content(size, seed=0):
	'''
	`size` bytes whose value depends on the position (and `seed`).
	'''
	block = bytes((i * 7 + seed) % 251 for i in range(251 * 4))
	data = block * (size // len(block) + 1)
	shift = seed % len(block)
	return (data[shift:] + data[:shift])[:size] if size else b''


def sha256(data):
	return hashlib.sha256(data).hexdigest()


class Loggers(dict):
	'''
	Telethon's per-module loggers, kept quiet (its timeout warning would
	print during the tests).
	'''
	def __missing__(self, name):
		logger = logging.getLogger(f'media-tests.{name}')
		logger.propagate = False
		logger.addHandler(logging.NullHandler())
		self[name] = logger
		return logger


class Sender:
	def __init__(self, dc):
		self.dc = dc


@dataclass
class StoredFile:
	data: bytes
	dc: int
	reference: bytes = b'ref-1'
	generation: int = 1


@dataclass
class Request:
	'''
	One request received: `kind` GetFile or GetMessages.
	'''
	kind: str
	dc: int
	media_id: int | None = None
	offset: int | None = None
	limit: int | None = None
	reference: bytes | None = None
	in_transaction: bool | None = None
	outcome: str = 'ok'


@dataclass
class Fault:
	'''
	`when(request)` selects the request; `action` is an exception to
	raise, 'hang' (never answer), or an async function of the request
	whose result, when not None, is returned instead of the answer.
	`times` limits how often it fires.
	'''
	when: object
	action: object
	times: int = 1
	fired: int = field(default=0)


def at_offset(offset, kind='GetFile'):
	return lambda r: r.kind == kind and r.offset == offset


def any_request(kind='GetFile'):
	return lambda r: r.kind == kind


class FakeTelegram:

	def __init__(self):
		client = TelegramClient.__new__(TelegramClient)
		client._log = Loggers()
		client.session = pytypes.SimpleNamespace(dc_id=HOME_DC)
		client._sender = Sender(HOME_DC)
		client._call = self._call
		client._borrow_exported_sender = self._borrow
		client._return_exported_sender = self._give_back
		self.client = client
		self.files = {}
		self.messages = {}
		self.faults = []
		self.requests = []
		self.borrowed = []
		self.returned = []
		self.ledger = None

	'''

	Content

	'''

	def add_document(self, media_id, data, *, dc=HOME_DC, size=None, mime='video/mp4', attributes=None):
		'''
		A stored document and its MessageMediaDocument (`size` is what the
		document reports, the data length by default).
		'''
		self.files[('document', media_id)] = StoredFile(data, dc)
		return self.document_media(media_id, size=len(data) if size is None else size, mime=mime,
			attributes=attributes)

	def document_media(self, media_id, *, size, mime='video/mp4', attributes=None, dc=None):
		stored = self.files[('document', media_id)]
		if attributes is None:
			attributes = [types.DocumentAttributeVideo(duration=1, w=1, h=1)]
		document = types.Document(
			id=media_id, access_hash=ACCESS_HASH, file_reference=stored.reference, date=DATE, mime_type=mime,
			size=size, dc_id=stored.dc if dc is None else dc, attributes=attributes)
		return types.MessageMediaDocument(document=document)

	def add_photo(self, media_id, data, *, dc=HOME_DC, known=True):
		'''
		A stored photo whose largest size is progressive 'y'; with `known`
		False, that size reports no byte counts.
		'''
		self.files[('photo', media_id)] = StoredFile(data, dc)
		return self.photo_media(media_id, known=known)

	def photo_media(self, media_id, *, known=True):
		stored = self.files[('photo', media_id)]
		largest = types.PhotoSizeProgressive(type='y', w=1280, h=960, sizes=[100, len(stored.data)] if known else [])
		# a smaller size with a byte count would win over a size without one
		sizes = [types.PhotoSize(type='m', w=320, h=240, size=1000), largest] if known else [largest]
		photo = types.Photo(
			id=media_id, access_hash=ACCESS_HASH, file_reference=stored.reference, date=DATE, sizes=sizes,
			dc_id=stored.dc)
		return types.MessageMediaPhoto(photo=photo)

	def post(self, message_id, media, channel_id=CHANNEL):
		'''
		The channel message the adapter can fetch again; returns a
		MediaJob for it.
		'''
		self.messages[(channel_id, message_id)] = types.Message(
			id=message_id, peer_id=types.PeerChannel(channel_id), date=DATE, message='', media=media)
		return self.job(message_id, media, channel_id)

	def job(self, message_id, media, channel_id=CHANNEL):
		ref = classify(media.to_dict())
		return MediaJob(channel_id, message_id, ref, location_of(media),
			types.InputChannel(channel_id=channel_id, access_hash=ACCESS_HASH))

	def expire(self, kind, media_id):
		'''
		Issue a new file reference: requests with the old one now fail,
		and the stored messages carry the new one.
		'''
		stored = self.files[(kind, media_id)]
		stored.generation += 1
		stored.reference = f'ref-{stored.generation}'.encode()
		for key, message in self.messages.items():
			media = message.media
			if kind == 'document' and isinstance(media, types.MessageMediaDocument) and media.document.id == media_id:
				media.document.file_reference = stored.reference
			if kind == 'photo' and isinstance(media, types.MessageMediaPhoto) and media.photo.id == media_id:
				media.photo.file_reference = stored.reference

	def fault(self, when, action, times=1):
		self.faults.append(Fault(when, action, times))

	'''

	Requests

	'''

	def get_files(self, outcome=None):
		return [r for r in self.requests if r.kind == 'GetFile' and (outcome is None or r.outcome == outcome)]

	def lookups(self):
		return [r for r in self.requests if r.kind == 'GetMessages']

	async def _borrow(self, dc_id):
		self.borrowed.append(dc_id)
		return Sender(dc_id)

	async def _give_back(self, sender):
		self.returned.append(sender.dc)

	async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
		if isinstance(request, GetFileRequest):
			loc = request.location
			kind = 'photo' if isinstance(loc, types.InputPhotoFileLocation) else 'document'
			record = Request('GetFile', sender.dc, loc.id, request.offset, request.limit, loc.file_reference)
		elif isinstance(request, GetMessagesRequest):
			kind = None
			record = Request('GetMessages', sender.dc)
		else:
			raise AssertionError(f'unexpected request {type(request).__name__}')
		if self.ledger is not None:
			record.in_transaction = self.ledger.con.in_transaction
		self.requests.append(record)

		for fault in self.faults:
			if fault.fired < fault.times and fault.when(record):
				fault.fired += 1
				record.outcome = 'fault'
				if fault.action == 'hang':
					await asyncio.Event().wait()
				if isinstance(fault.action, BaseException):
					raise fault.action
				result = await fault.action(record)
				if result is not None:
					return result
				record.outcome = 'ok'
				break

		if record.kind == 'GetMessages':
			return self._messages(request)
		return self._file(kind, request, record)

	def _file(self, kind, request, record):
		stored = self.files[(kind, record.media_id)]
		if record.dc != stored.dc:
			record.outcome = 'migrate'
			raise errors.FileMigrateError(request=None, capture=stored.dc)
		if record.reference != stored.reference:
			record.outcome = 'expired'
			raise errors.FileReferenceExpiredError(request=None)
		offset, limit = record.offset, record.limit
		if offset % 4096 or limit % 4096 or limit <= 0 or MIB % limit:
			raise errors.LimitInvalidError(request=None)
		if offset // MIB != (offset + limit - 1) // MIB:
			raise errors.OffsetInvalidError(request=None)
		return types.upload.File(
			type=types.storage.FileUnknown(), mtime=0, bytes=stored.data[offset:offset + limit])

	def _messages(self, request):
		channel_id = request.channel.channel_id
		found = []
		for wanted in request.id:
			message = self.messages.get((channel_id, wanted.id))
			found.append(message if message is not None else types.MessageEmpty(
				id=wanted.id, peer_id=types.PeerChannel(channel_id)))
		return types.messages.ChannelMessages(pts=1, count=len(found), messages=found, chats=[], users=[], topics=[])


class DownloadTestCase(offline_guard.OfflineAsyncTestCase):
	'''
	A files output folder with its ledger open and RUN_A running, a fake
	service, a policy on virtual time, and a MediaDownloader.
	'''
	limits = MediaLimits()

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(self.tmp, 'out')
		os.mkdir(self.out)
		self.layout = MediaLayout(self.out)
		self.ledger = open_ledger(self.layout, ensure_identity(self.out), create=True)
		self.addCleanup(self.ledger.close)
		self.ledger.start_run(RUN_A, 'download-media', selection=SELECTION, profile_name='work', profile_id=PROFILE_ID)
		self.fake = FakeTelegram()
		self.fake.ledger = self.ledger

	async def asyncSetUp(self):
		await super().asyncSetUp()
		self.policy = self.make_policy()
		self.downloader = self.make_downloader()

	def make_policy(self, **options):
		options.setdefault('concurrency', 3)
		options.setdefault('interval', 0.25)
		options.setdefault('max_flood_wait', 60)
		options.setdefault('max_retries', 3)
		options.setdefault('timeout', 30)
		return RequestPolicy(**options)

	def make_downloader(self, limits=None, budget=None):
		return MediaDownloader(
			self.fake.client, self.policy, self.ledger, self.layout, RUN_A, limits=limits or self.limits,
			budget=budget)

	def request(self, *jobs):
		'''
		Record the jobs' items as requested, as the run does after the
		history commit.
		'''
		by_channel = {}
		for job in jobs:
			by_channel.setdefault(job.channel_id, []).append((job.message_id, job.ref))
		for channel_id, refs in by_channel.items():
			self.ledger.request(RUN_A, channel_id, refs)

	def stored(self, outcome):
		with open(self.layout.path_of(outcome.path), 'rb') as reader:
			return reader.read()

	def partial_files(self):
		root = self.layout.partial_root
		found = []
		for folder, _, names in os.walk(root):
			found.extend(os.path.join(folder, n) for n in names)
		return found

	def assertAligned(self):
		for record in self.fake.get_files():
			self.assertEqual((record.offset % PULL_BYTES, record.limit), (0, PULL_BYTES), record)
		self.assertFalse(any(r.in_transaction for r in self.fake.requests))
