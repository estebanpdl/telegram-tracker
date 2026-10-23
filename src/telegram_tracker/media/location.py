# -*- coding: utf-8 -*-
'''
Where a downloadable file lives on Telegram's side.

A MediaLocation keeps only what a download request needs: the photo or
document ID, its access hash, the file reference, the data center that
stores it, and, for a photo, the size chosen by classify. It is taken
from the Telethon media object of a message, never from stored JSON, so
the file reference is the one the account received.
'''

# import modules
from dataclasses import dataclass

from telethon import types

# import local submodules
from telegram_tracker.media.classify import PHOTO, classify


@dataclass(frozen=True)
class MediaLocation:
	'''
	media_kind -> classify.PHOTO or classify.DOCUMENT
	thumb_size -> the photo size type; '' for a document (the file itself)
	'''
	media_kind: str
	media_id: int
	access_hash: int
	file_reference: bytes
	dc_id: int
	thumb_size: str

	def input_location(self):
		'''
		The InputFileLocation for upload.GetFile requests.
		'''
		cls = types.InputPhotoFileLocation if self.media_kind == PHOTO else types.InputDocumentFileLocation
		return cls(
			id=self.media_id, access_hash=self.access_hash, file_reference=self.file_reference,
			thumb_size=self.thumb_size
		)


def location_of(media, photo_size=None):
	'''
	The MediaLocation of a Telethon MessageMediaPhoto or
	MessageMediaDocument, or None when it has no file to download.
	`photo_size` is the size type to fetch for a photo; by default, the
	one classify chooses.
	'''
	ref = classify(media.to_dict()) if media is not None else None
	if ref is None:
		return None
	if ref.media_kind == PHOTO:
		source, thumb_size = media.photo, photo_size or ref.photo_size
	else:
		source, thumb_size = media.document, ''
	return MediaLocation(
		media_kind=ref.media_kind, media_id=source.id, access_hash=source.access_hash,
		file_reference=bytes(source.file_reference), dc_id=source.dc_id, thumb_size=thumb_size
	)


def same_media(location, kind, media_id):
	return location is not None and (location.media_kind, location.media_id) == (kind, media_id)
