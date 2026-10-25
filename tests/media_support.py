# -*- coding: utf-8 -*-
'''
Synthetic media payloads and folders for the media tests.

The payloads have the collector's JSON-safe to_dict() shape, as the
history files and the data database store them. Folder links are
directory junctions on Windows and symlinks elsewhere.
'''

# import modules
import os
import tempfile

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.media.classify import classify
from telegram_tracker.media.identity import ensure_identity, folder_identity
from telegram_tracker.media.layout import MediaLayout
from telegram_tracker.media.ledger import open_ledger


RUN_A = '11111111-1111-4111-8111-111111111111'
RUN_B = '22222222-2222-4222-8222-222222222222'
RUN_C = '33333333-3333-4333-8333-333333333333'
PROFILE_ID = '00000000-0000-4000-8000-000000000000'
CHANNEL = 1001
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SELECTION = {'types': ['image', 'video', 'audio', 'file'], 'max_media_size': None, 'max_media_total': None}


def attr(name, **fields):
	return {'_': name, **fields}


def photo_media(media_id=5001, sizes=None):
	if sizes is None:
		sizes = [
			{'_': 'PhotoStrippedSize', 'type': 'i', 'bytes': "b'\\x01'"},
			{'_': 'PhotoSize', 'type': 'm', 'w': 320, 'h': 240, 'size': 1000},
			{'_': 'PhotoSizeProgressive', 'type': 'y', 'w': 1280, 'h': 960, 'sizes': [3000, 9000, 15000]}
		]
	return {
		'_': 'MessageMediaPhoto', 'spoiler': None,
		'photo': {'_': 'Photo', 'id': media_id, 'access_hash': 6001, 'file_reference': "b'\\x01\\x02'",
			'date': '2024-01-01 00:00:00+00:00', 'sizes': sizes, 'dc_id': 2, 'video_sizes': []},
		'ttl_seconds': None
	}


def document_media(media_id=5002, mime='application/pdf', size=2000, attributes=()):
	document = {'_': 'Document', 'id': media_id, 'access_hash': 6002, 'file_reference': "b'\\x04'",
		'date': '2024-01-01 00:00:00+00:00', 'mime_type': mime, 'size': size, 'dc_id': 4,
		'attributes': [attr('DocumentAttributeFilename', file_name='synthetic.bin'), *attributes],
		'thumbs': [], 'video_thumbs': []}
	return {'_': 'MessageMediaDocument', 'document': document, 'alt_documents': [], 'ttl_seconds': None}


def ref_of(media):
	ref = classify(media)
	assert ref is not None, media
	return ref


def make_link(target, link):
	'''
	A folder link at `link` to `target`: a junction on Windows (no
	privilege needed), a symlink elsewhere.
	'''
	if os.name == 'nt':
		import _winapi

		_winapi.CreateJunction(target, link)
	else:
		os.symlink(target, link)


def remove_link(link):
	if os.name == 'nt':
		os.rmdir(link)
	else:
		os.remove(link)


class MediaTestCase(offline_guard.OfflineTestCase):
	'''
	A temporary output folder (`self.out`) with its MediaLayout.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(self.tmp, 'out')
		os.mkdir(self.out)
		self.layout = MediaLayout(self.out)

	def link(self, target, link):
		make_link(target, link)
		self.addCleanup(lambda: os.path.lexists(link) and remove_link(link))

	def files_ledger(self, **options):
		'''
		A files folder's ledger (its identity record written first).
		'''
		ledger = open_ledger(self.layout, ensure_identity(self.out), create=True, **options)
		self.addCleanup(ledger.close)
		return ledger

	def reopen(self, **options):
		ledger = open_ledger(self.layout, folder_identity(self.out), **options)
		self.addCleanup(ledger.close)
		return ledger
