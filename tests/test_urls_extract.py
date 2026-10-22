# -*- coding: utf-8 -*-
'''
URL occurrences of one message (telegram_tracker.urls.extract): UTF-16
entity spans, hidden text-link targets, plain-text URLs, previews, and
the merge rules for overlapping evidence. Offsets are counted by hand.
'''

# import modules
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.urls.extract import message_occurrences, plain_candidates, utf16, utf16_slice
from telegram_tracker.urls.parse import Suffixes


SUFFIXES = Suffixes()


def message(text, entities=(), media=None):
	return {'_': 'Message', 'id': 1, 'message': text, 'entities': list(entities), 'media': media}


def url(offset, length):
	return {'_': 'MessageEntityUrl', 'offset': offset, 'length': length}


def link(offset, length, target):
	return {'_': 'MessageEntityTextUrl', 'offset': offset, 'length': length, 'url': target}


def webpage(page):
	return {'_': 'MessageMediaWebPage', 'webpage': page}


def rows(msg):
	'''
	(source, evidence, offset, length, label, raw, matches_text) per
	occurrence.
	'''
	return [
		(o.source, '+'.join(o.evidence), o.offset, o.length, o.label, o.raw_url, matches)
		for o, _, matches in message_occurrences(msg, SUFFIXES)
	]


class SpanTests(offline_guard.OfflineTestCase):

	def test_utf16_offsets_count_surrogate_pairs(self):
		# 🚀 is 2 units, each Cyrillic letter 1: the URL starts at unit 9
		text = '🚀 тест: https://a.example.com/x'
		self.assertEqual(rows(message(text, [url(9, 23)])), [
			('url_entity', 'url_entity+plain_text', 9, 23, None, 'https://a.example.com/x', None)
		])

	def test_a_span_outside_the_text_or_inside_a_character(self):
		data = utf16('🚀ab')
		self.assertEqual(utf16_slice(data, 2, 2), 'ab')
		for offset, length in ((1, 2), (0, 1), (3, 2), (-1, 2), (0, 0), ('0', 2), (None, None)):
			with self.subTest(offset=offset, length=length):
				self.assertIsNone(utf16_slice(data, offset, length))
		self.assertEqual(rows(message('🚀ab', [url(1, 2)])), [
			('url_entity', 'url_entity', 1, 2, None, None, None)
		])

	def test_a_text_link_with_a_bad_span_keeps_its_target(self):
		self.assertEqual(rows(message('ab', [link(5, 1, 'https://t.example.com/')])), [
			('text_link', 'text_link', 5, 1, None, 'https://t.example.com/', None)
		])


class MergeTests(offline_guard.OfflineTestCase):

	def test_hidden_target_and_a_different_url_in_its_label(self):
		text = 'see https://shown.example.com/ now'
		self.assertEqual(rows(message(text, [link(4, 26, 'https://hidden.example.net/')])), [
			('text_link', 'text_link', 4, 26, 'https://shown.example.com/', 'https://hidden.example.net/', None),
			('plain_text', 'plain_text', 4, 26, None, 'https://shown.example.com/', None)
		])

	def test_a_label_naming_its_own_target_is_one_occurrence(self):
		# same target after normalization: host case and the empty path
		text = 'https://Same.Example.com'
		self.assertEqual(rows(message(text, [link(0, 24, 'https://same.example.com/')])), [
			('text_link', 'text_link+plain_text', 0, 24, text, 'https://same.example.com/', None)
		])

	def test_a_plain_word_label_is_only_the_link(self):
		self.assertEqual(rows(message('click here', [link(0, 10, 'https://x.example.com/')])), [
			('text_link', 'text_link', 0, 10, 'click here', 'https://x.example.com/', None)
		])

	def test_a_url_entity_wins_over_an_overlapping_match(self):
		# the entity stops before the closing bracket the pattern keeps
		text = 'x https://e.example.com/a(b)c'
		self.assertEqual(rows(message(text, [url(2, 25)])), [
			('url_entity', 'url_entity+plain_text', 2, 25, None, 'https://e.example.com/a(b', None)
		])

	def test_repeated_urls_stay_separate(self):
		text = 'https://r.example.com/ https://r.example.com/'
		self.assertEqual(rows(message(text, [url(0, 22)])), [
			('url_entity', 'url_entity+plain_text', 0, 22, None, 'https://r.example.com/', None),
			('plain_text', 'plain_text', 23, 22, None, 'https://r.example.com/', None)
		])

	def test_other_entities_are_ignored(self):
		text = '@name https://m.example.com/'
		entities = [{'_': 'MessageEntityMention', 'offset': 0, 'length': 5}, {'_': 'MessageEntityBold',
			'offset': 6, 'length': 22}]
		self.assertEqual(rows(message(text, entities)), [
			('plain_text', 'plain_text', 6, 22, None, 'https://m.example.com/', None)
		])


class PlainTextTests(offline_guard.OfflineTestCase):

	def found(self, text):
		return [found for _, _, found in plain_candidates(text, SUFFIXES)]

	def test_trailing_punctuation_and_brackets(self):
		self.assertEqual(self.found('(https://a.example.com/x), "https://b.example.com/y".'),
			['https://a.example.com/x', 'https://b.example.com/y'])
		self.assertEqual(self.found('https://w.example.org/wiki/A_(b)!'), ['https://w.example.org/wiki/A_(b)'])
		self.assertEqual(self.found('Look: www.Example.com/a?b=1…'), ['www.Example.com/a?b=1'])

	def test_bare_hosts_need_a_known_suffix_and_a_path(self):
		self.assertEqual(self.found('t.me/channel and пример.рф/путь'), ['t.me/channel', 'пример.рф/путь'])
		for text in ('example.com alone', 'report.unknowntld/x', 'v1.2/3', 'user@example.com/x',
				'www.', 'www.example', 'https://'):
			with self.subTest(text=text):
				self.assertEqual(self.found(text), [])

	def test_any_scheme_with_slashes(self):
		self.assertEqual(self.found('tg://resolve?domain=x ftp://f.example.com mailto:x@example.com'),
			['tg://resolve?domain=x', 'ftp://f.example.com'])


class PreviewTests(offline_guard.OfflineTestCase):

	def test_a_preview_is_its_own_occurrence(self):
		page = {'_': 'WebPage', 'url': 'https://p.example.com/', 'display_url': 'p.example.com',
			'site_name': 'P', 'title': 'T', 'description': 'D'}
		msg = message('https://P.example.com', [url(0, 21)], webpage(page))
		found = message_occurrences(msg, SUFFIXES)
		self.assertEqual([(o.source, matches) for o, _, matches in found],
			[('url_entity', None), ('preview', True)])
		self.assertEqual(found[1][0].preview, {'type': 'WebPage', 'display_url': 'p.example.com',
			'site_name': 'P', 'title': 'T', 'description': 'D'})

	def test_a_preview_of_another_url(self):
		msg = message('no link', media=webpage({'_': 'WebPagePending', 'url': 'https://q.example.com/'}))
		self.assertEqual(rows(msg), [('preview', 'preview', None, None, None, 'https://q.example.com/', False)])

	def test_previews_without_a_url(self):
		for media in (webpage({'_': 'WebPageEmpty', 'url': None}), webpage({'_': 'WebPageNotModified'}),
				{'_': 'MessageMediaPhoto'}, None):
			with self.subTest(media=media):
				self.assertEqual(rows(message('', media=media)), [])


if __name__ == '__main__':
	unittest.main()
