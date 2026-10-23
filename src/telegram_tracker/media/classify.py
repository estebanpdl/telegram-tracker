# -*- coding: utf-8 -*-
'''
Which file a message's media stands for, and how it is stored.

`classify` reads the JSON-safe form of a message's `media` (the
collector's to_dict() output, as stored in the history files and the
database) and returns a MediaRef for a downloadable photo or document,
or None. The first matching rule wins:

	photo        MessageMediaPhoto with a Photo           image  photo
	sticker      DocumentAttributeSticker                 by MIME
	voice note   DocumentAttributeAudio with voice        audio
	video note   DocumentAttributeVideo with round_message video
	GIF          DocumentAttributeAnimated                by MIME
	video        DocumentAttributeVideo                   video
	audio        DocumentAttributeAudio                   audio
	document     anything else                            by MIME

"By MIME" is image, video or audio for those MIME prefixes, else file.
Every other kind of media (web page previews, games, invoices, paid
media, stories, polls, contacts, locations, dice) has no file to
download here.

The extension comes from EXTENSIONS only; the remote file name is never
used. Photos are always `jpg`. A MIME type not in the table is saved as
`.bin`: formats whose usual viewer runs embedded code (executables,
scripts, HTML, SVG, macro-capable Office formats) are left out on
purpose.
'''

# import modules
from dataclasses import dataclass


IMAGE = 'image'
VIDEO = 'video'
AUDIO = 'audio'
FILE = 'file'
TYPES = (IMAGE, VIDEO, AUDIO, FILE)

PHOTO = 'photo'
DOCUMENT = 'document'

# subtypes, in the order of the rules above
SUBTYPES = ('photo', 'sticker', 'voice_note', 'video_note', 'gif', 'video', 'audio', 'document')

PHOTO_MIME = 'image/jpeg'
PHOTO_EXTENSION = 'jpg'
UNKNOWN_EXTENSION = 'bin'

# MIME type -> extension
EXTENSIONS = {
	'image/jpeg': 'jpg',
	'image/png': 'png',
	'image/gif': 'gif',
	'image/webp': 'webp',
	'image/heic': 'heic',
	'image/heif': 'heif',
	'image/avif': 'avif',
	'image/bmp': 'bmp',
	'image/tiff': 'tif',
	'video/mp4': 'mp4',
	'video/webm': 'webm',
	'video/quicktime': 'mov',
	'video/x-matroska': 'mkv',
	'video/mpeg': 'mpg',
	'video/3gpp': '3gp',
	'video/x-msvideo': 'avi',
	'audio/ogg': 'ogg',
	'audio/opus': 'opus',
	'audio/mpeg': 'mp3',
	'audio/mp4': 'm4a',
	'audio/x-m4a': 'm4a',
	'audio/aac': 'aac',
	'audio/flac': 'flac',
	'audio/x-flac': 'flac',
	'audio/wav': 'wav',
	'audio/x-wav': 'wav',
	'audio/webm': 'weba',
	'application/x-tgsticker': 'tgs',
	'application/pdf': 'pdf',
	'application/epub+zip': 'epub',
	'application/zip': 'zip',
	'application/x-7z-compressed': '7z',
	'application/vnd.rar': 'rar',
	'application/x-rar-compressed': 'rar',
	'application/gzip': 'gz',
	'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
	'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
	'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
	'application/vnd.oasis.opendocument.text': 'odt',
	'text/plain': 'txt',
	'application/json': 'json'
}
ALLOWED_EXTENSIONS = frozenset(EXTENSIONS.values()) | {PHOTO_EXTENSION, UNKNOWN_EXTENSION}

# photo sizes with a downloadable file; cached and stripped sizes carry
# inline bytes, path sizes an outline, and video sizes an animation
PHOTO_SIZES = ('PhotoSize', 'PhotoSizeProgressive')


@dataclass(frozen=True)
class MediaRef:
	'''
	A downloadable file attached to a message.

	media_type -> the raw media constructor (messages.media_type)
	media_kind -> PHOTO or DOCUMENT
	media_id -> the photo or document ID (messages.media_id)
	photo_size -> the chosen size's type for a photo, else None
	type -> one of TYPES (also the folder)
	subtype -> one of SUBTYPES
	mime_type -> normalized MIME type, None when the document has none
	ext -> extension without the dot, from ALLOWED_EXTENSIONS
	expected_bytes -> size the API reports, None when unknown
	'''
	media_type: str
	media_kind: str
	media_id: int
	photo_size: str | None
	type: str
	subtype: str
	mime_type: str | None
	ext: str
	expected_bytes: int | None


def is_int(value):
	return type(value) is int


def normalize_mime(value):
	'''
	Lower-case MIME type without parameters, or None.
	'''
	if not isinstance(value, str):
		return None
	value = value.split(';', 1)[0].strip().lower()
	return value or None


def extension_for(mime_type):
	return EXTENSIONS.get(mime_type, UNKNOWN_EXTENSION)


def type_for(mime_type):
	prefix = (mime_type or '').split('/', 1)[0]
	return prefix if prefix in (IMAGE, VIDEO, AUDIO) else FILE


def classify(media):
	'''
	The MediaRef of a message's raw `media` dict, or None when it has no
	file to download.
	'''
	if not isinstance(media, dict):
		return None
	constructor = media.get('_')
	if constructor == 'MessageMediaPhoto':
		return classify_photo(constructor, media.get('photo'))
	if constructor == 'MessageMediaDocument':
		return classify_document(constructor, media.get('document'))
	return None


def classify_photo(constructor, photo):
	if not isinstance(photo, dict) or photo.get('_') != 'Photo' or not is_int(photo.get('id')):
		return None
	chosen = largest_size(photo.get('sizes'))
	if chosen is None:
		return None
	size_type, expected = chosen
	return MediaRef(
		media_type=constructor, media_kind=PHOTO, media_id=photo['id'], photo_size=size_type,
		type=IMAGE, subtype='photo', mime_type=PHOTO_MIME, ext=PHOTO_EXTENSION, expected_bytes=expected
	)


def largest_size(sizes):
	'''
	(type, bytes) of the largest downloadable photo size; bytes is None
	when the size does not say. A later entry wins a tie (sizes are
	listed smallest first). None when no size can be downloaded.
	'''
	best, best_key = None, None
	for size in sizes if isinstance(sizes, list) else ():
		if not isinstance(size, dict) or size.get('_') not in PHOTO_SIZES:
			continue
		if not isinstance(size.get('type'), str) or not size['type']:
			continue
		if size['_'] == 'PhotoSize':
			nbytes = size.get('size') if is_int(size.get('size')) else None
		else:
			progressive = [n for n in size.get('sizes') or () if is_int(n)]
			nbytes = max(progressive) if progressive else None
		if nbytes is not None and nbytes < 0:
			nbytes = None
		key = -1 if nbytes is None else nbytes
		if best_key is None or key >= best_key:
			best, best_key = (size['type'], nbytes), key
	return best


def classify_document(constructor, document):
	if not isinstance(document, dict) or document.get('_') != 'Document' or not is_int(document.get('id')):
		return None
	mime = normalize_mime(document.get('mime_type'))
	size = document.get('size')
	expected = size if is_int(size) and size >= 0 else None
	attributes = {
		a['_']: a for a in document.get('attributes') or ()
		if isinstance(a, dict) and isinstance(a.get('_'), str)
	}
	audio = attributes.get('DocumentAttributeAudio')
	video = attributes.get('DocumentAttributeVideo')

	if 'DocumentAttributeSticker' in attributes:
		kind, subtype = type_for(mime), 'sticker'
	elif audio is not None and audio.get('voice') is True:
		kind, subtype = AUDIO, 'voice_note'
	elif video is not None and video.get('round_message') is True:
		kind, subtype = VIDEO, 'video_note'
	elif 'DocumentAttributeAnimated' in attributes:
		kind, subtype = type_for(mime), 'gif'
	elif video is not None:
		kind, subtype = VIDEO, 'video'
	elif audio is not None:
		kind, subtype = AUDIO, 'audio'
	else:
		kind, subtype = type_for(mime), 'document'
	return MediaRef(
		media_type=constructor, media_kind=DOCUMENT, media_id=document['id'], photo_size=None,
		type=kind, subtype=subtype, mime_type=mime, ext=extension_for(mime), expected_bytes=expected
	)
