# -*- coding: utf-8 -*-
'''
Media classification: every rule of the table in docs/media-ledger.md,
rule precedence, the photo size choice, the MIME -> extension table, and
media that has no file to download.
'''

# import modules
import copy
import json
import os
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import attr, document_media, photo_media, ref_of
from telegram_tracker.media.classify import ALLOWED_EXTENSIONS, EXTENSIONS, classify


VARIANTS_PATH = os.path.join(source_path.ROOT, 'tests', 'fixtures', 'sqlite', 'variants.json')


def summary(ref):
	return (ref.type, ref.subtype, ref.ext)


class DocumentRuleTests(offline_guard.OfflineTestCase):

	def classify_document(self, mime, *attributes, size=2000):
		return ref_of(document_media(mime=mime, size=size, attributes=attributes))

	def test_stickers_go_by_mime(self):
		sticker = attr('DocumentAttributeSticker', alt='x')
		self.assertEqual(summary(self.classify_document('image/webp', sticker)), ('image', 'sticker', 'webp'))
		self.assertEqual(summary(self.classify_document('video/webm', sticker)), ('video', 'sticker', 'webm'))
		self.assertEqual(
			summary(self.classify_document('application/x-tgsticker', sticker)), ('file', 'sticker', 'tgs'))

	def test_voice_note(self):
		ref = self.classify_document('audio/ogg', attr('DocumentAttributeAudio', voice=True, duration=3))
		self.assertEqual(summary(ref), ('audio', 'voice_note', 'ogg'))

	def test_video_note(self):
		ref = self.classify_document('video/mp4', attr('DocumentAttributeVideo', round_message=True, duration=5))
		self.assertEqual(summary(ref), ('video', 'video_note', 'mp4'))

	def test_gif_goes_by_mime(self):
		animated = attr('DocumentAttributeAnimated')
		self.assertEqual(summary(self.classify_document('video/mp4', animated)), ('video', 'gif', 'mp4'))
		self.assertEqual(summary(self.classify_document('image/gif', animated)), ('image', 'gif', 'gif'))

	def test_video(self):
		ref = self.classify_document('video/quicktime', attr('DocumentAttributeVideo', round_message=None))
		self.assertEqual(summary(ref), ('video', 'video', 'mov'))

	def test_audio(self):
		for voice in (None, False):
			with self.subTest(voice=voice):
				ref = self.classify_document('audio/mpeg', attr('DocumentAttributeAudio', voice=voice))
				self.assertEqual(summary(ref), ('audio', 'audio', 'mp3'))

	def test_other_documents_go_by_mime(self):
		cases = {
			'application/pdf': ('file', 'document', 'pdf'),
			'image/png': ('image', 'document', 'png'),
			'video/mp4': ('video', 'document', 'mp4'),
			'audio/flac': ('audio', 'document', 'flac'),
			'text/plain': ('file', 'document', 'txt')
		}
		for mime, expected in cases.items():
			with self.subTest(mime=mime):
				self.assertEqual(summary(self.classify_document(mime)), expected)

	def test_first_matching_rule_wins(self):
		sticker = attr('DocumentAttributeSticker')
		animated = attr('DocumentAttributeAnimated')
		voice = attr('DocumentAttributeAudio', voice=True)
		round_video = attr('DocumentAttributeVideo', round_message=True)
		video = attr('DocumentAttributeVideo')
		audio = attr('DocumentAttributeAudio')
		cases = [
			(('video/webm', sticker, animated, video), 'sticker'),
			(('audio/ogg', voice, round_video), 'voice_note'),
			(('video/mp4', round_video, animated), 'video_note'),
			# a Telegram GIF is an animated video document
			(('video/mp4', video, animated), 'gif'),
			(('video/mp4', video, audio), 'video')
		]
		for (mime, *attributes), subtype in cases:
			with self.subTest(subtype=subtype):
				self.assertEqual(self.classify_document(mime, *attributes).subtype, subtype)
				self.assertEqual(self.classify_document(mime, *reversed(attributes)).subtype, subtype)

	def test_identity_and_size(self):
		ref = ref_of(document_media(media_id=-9_000_000_000, size=123456))
		self.assertEqual(
			(ref.media_type, ref.media_kind, ref.media_id, ref.photo_size, ref.expected_bytes),
			('MessageMediaDocument', 'document', -9_000_000_000, None, 123456)
		)

	def test_unknown_or_invalid_size_is_none(self):
		for size in (None, -1, True, '2000', 2.5):
			with self.subTest(size=size):
				self.assertIsNone(self.classify_document('application/pdf', size=size).expected_bytes)
		self.assertEqual(self.classify_document('application/pdf', size=0).expected_bytes, 0)


class MimeTests(offline_guard.OfflineTestCase):

	def test_every_table_row(self):
		for mime, ext in EXTENSIONS.items():
			with self.subTest(mime=mime):
				ref = ref_of(document_media(mime=mime))
				self.assertEqual((ref.mime_type, ref.ext), (mime, ext))

	def test_mime_is_normalized(self):
		ref = ref_of(document_media(mime=' Video/MP4; codecs="avc1"'))
		self.assertEqual((ref.mime_type, ref.type, ref.ext), ('video/mp4', 'video', 'mp4'))

	def test_unknown_and_executable_types_get_bin(self):
		cases = {
			'application/x-msdownload': 'file', 'application/x-sh': 'file', 'text/html': 'file',
			'application/javascript': 'file', 'image/svg+xml': 'image', 'application/msword': 'file',
			'application/vnd.ms-excel.sheet.macroenabled.12': 'file', 'application/x-unknown-fx': 'file'
		}
		for mime, kind in cases.items():
			with self.subTest(mime=mime):
				ref = ref_of(document_media(mime=mime))
				self.assertEqual((ref.ext, ref.type), ('bin', kind))

	def test_missing_mime(self):
		for mime in (None, '', 42):
			with self.subTest(mime=mime):
				ref = ref_of(document_media(mime=mime))
				self.assertEqual((ref.mime_type, ref.type, ref.ext), (None, 'file', 'bin'))

	def test_extensions_are_plain_lower_case(self):
		self.assertIn('bin', ALLOWED_EXTENSIONS)
		self.assertIn('jpg', ALLOWED_EXTENSIONS)
		for ext in ALLOWED_EXTENSIONS:
			with self.subTest(ext=ext):
				self.assertRegex(ext, r'^[a-z0-9]+$')
		self.assertNotIn('exe', ALLOWED_EXTENSIONS)
		self.assertNotIn('html', ALLOWED_EXTENSIONS)


class PhotoTests(offline_guard.OfflineTestCase):

	def test_photo_rule(self):
		ref = ref_of(photo_media(media_id=-42))
		self.assertEqual(
			(ref.media_type, ref.media_kind, ref.media_id, ref.type, ref.subtype, ref.mime_type, ref.ext),
			('MessageMediaPhoto', 'photo', -42, 'image', 'photo', 'image/jpeg', 'jpg')
		)

	def test_largest_size_is_chosen(self):
		ref = ref_of(photo_media())
		self.assertEqual((ref.photo_size, ref.expected_bytes), ('y', 15000))
		ref = ref_of(photo_media(sizes=[
			{'_': 'PhotoSizeProgressive', 'type': 'x', 'sizes': [100, 400]},
			{'_': 'PhotoSize', 'type': 'w', 'size': 5000},
			{'_': 'PhotoSize', 'type': 'm', 'size': 1000}
		]))
		self.assertEqual((ref.photo_size, ref.expected_bytes), ('w', 5000))

	def test_a_later_size_wins_a_tie(self):
		ref = ref_of(photo_media(sizes=[
			{'_': 'PhotoSize', 'type': 'm', 'size': 1000}, {'_': 'PhotoSize', 'type': 'x', 'size': 1000}]))
		self.assertEqual(ref.photo_size, 'x')

	def test_inline_outline_and_video_sizes_are_never_chosen(self):
		ref = ref_of(photo_media(sizes=[
			{'_': 'PhotoSize', 'type': 's', 'size': 10},
			{'_': 'PhotoCachedSize', 'type': 'c', 'bytes': 'x' * 900},
			{'_': 'PhotoStrippedSize', 'type': 'i', 'bytes': 'x' * 900},
			{'_': 'PhotoPathSize', 'type': 'j', 'bytes': 'x' * 900},
			{'_': 'VideoSize', 'type': 'u', 'size': 99999},
			{'_': 'PhotoSizeEmpty', 'type': 'e'}
		]))
		self.assertEqual((ref.photo_size, ref.expected_bytes), ('s', 10))

	def test_unknown_photo_size(self):
		ref = ref_of(photo_media(sizes=[{'_': 'PhotoSize', 'type': 'm'}]))
		self.assertEqual((ref.photo_size, ref.expected_bytes), ('m', None))
		ref = ref_of(photo_media(sizes=[{'_': 'PhotoSize', 'type': 'm'}, {'_': 'PhotoSize', 'type': 's', 'size': 5}]))
		self.assertEqual((ref.photo_size, ref.expected_bytes), ('s', 5))

	def test_photo_without_a_downloadable_size(self):
		only = [
			[{'_': 'PhotoStrippedSize', 'type': 'i', 'bytes': 'x'}],
			[{'_': 'PhotoCachedSize', 'type': 'c', 'w': 90, 'h': 90, 'bytes': 'x'}],
			[{'_': 'PhotoPathSize', 'type': 'j', 'bytes': 'x'}],
			[{'_': 'VideoSize', 'type': 'u', 'w': 800, 'h': 800, 'size': 99999}],
			[{'_': 'PhotoSizeEmpty', 'type': 'e'}]
		]
		for sizes in ([], *only, [{'_': 'PhotoSize', 'size': 9}]):
			with self.subTest(sizes=sizes):
				self.assertIsNone(classify(photo_media(sizes=sizes)))


class NotDownloadableTests(offline_guard.OfflineTestCase):

	def test_other_media_has_no_file(self):
		photo = photo_media()['photo']
		cases = [
			None, 'MessageMediaPhoto', [], {},
			{'_': 'MessageMediaWebPage', 'webpage': {'_': 'WebPage', 'photo': photo}},
			{'_': 'MessageMediaGame', 'game': {'_': 'Game', 'photo': photo}},
			{'_': 'MessageMediaInvoice', 'photo': {'_': 'WebDocument'}},
			{'_': 'MessageMediaPaidMedia', 'extended_media': []},
			{'_': 'MessageMediaStory', 'id': 3},
			{'_': 'MessageMediaPoll'}, {'_': 'MessageMediaContact'}, {'_': 'MessageMediaGeo'},
			{'_': 'MessageMediaDice'}, {'_': 'MessageMediaUnsupported'},
			{'_': 'MessageMediaPhoto', 'photo': None},
			{'_': 'MessageMediaPhoto', 'photo': {'_': 'PhotoEmpty', 'id': 1}},
			{'_': 'MessageMediaDocument', 'document': None},
			{'_': 'MessageMediaDocument', 'document': {'_': 'DocumentEmpty', 'id': 1}}
		]
		for media in cases:
			with self.subTest(media=media):
				self.assertIsNone(classify(media))

	def test_ids_must_be_integers(self):
		for bad in (None, True, '5001', 5001.0):
			with self.subTest(id=bad):
				media = photo_media()
				media['photo']['id'] = bad
				self.assertIsNone(classify(media))
				media = document_media()
				media['document']['id'] = bad
				self.assertIsNone(classify(media))

	def test_input_is_not_changed(self):
		media = document_media(attributes=[attr('DocumentAttributeVideo')])
		before = copy.deepcopy(media)
		classify(media)
		self.assertEqual(media, before)


class StoredShapeTests(offline_guard.OfflineTestCase):
	'''
	The schema fixtures hold media in the stored JSON form.
	'''
	def test_schema_fixture_variants(self):
		with open(VARIANTS_PATH, encoding='utf-8') as reader:
			variants = {v['name']: v['raw'] for v in json.load(reader)['messages']}
		photo = classify(variants['photo']['media'])
		self.assertEqual((photo.media_id, photo.photo_size, photo.expected_bytes), (5001, 'm', 1000))
		document = classify(variants['document']['media'])
		# no DocumentAttributeVideo in this fixture, so the MIME type decides
		self.assertEqual((document.media_id, document.type, document.subtype, document.ext), (5002, 'video', 'document', 'mp4'))
		for name in ('poll', 'geo', 'venue', 'contact', 'dice', 'story', 'game', 'unsupported media', 'unknown nested field'):
			with self.subTest(name=name):
				self.assertIsNone(classify(variants[name]['media']))


if __name__ == '__main__':
	unittest.main()
