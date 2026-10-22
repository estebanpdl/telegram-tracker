# -*- coding: utf-8 -*-
'''
Synthetic messages with URLs for the build-urls tests, added to the
fixture world before a collection, and the occurrences and counts
expected from them, traced by hand from the rules in
telegram_tracker.urls (UTF-16 offsets counted per character: an emoji is
2 units, a Cyrillic letter 1).
'''

# import local submodules
from fakes import FixtureWorld


ALPHA = 101
BETA = 102

# alpha_fx history message 8 (fixture world): a plain URL and its preview
EXISTING_ALPHA = 8


def url_entity(offset, length):
	return {'_': 'MessageEntityUrl', 'offset': offset, 'length': length}


def text_link(offset, length, url):
	return {'_': 'MessageEntityTextUrl', 'offset': offset, 'length': length, 'url': url}


# (channel ID, message spec): ids above the fixture's, newest first is
# arranged by add_url_messages
MESSAGES = (
	(ALPHA, {
		'id': 11,
		'date': '2026-01-11T11:00:00+00:00',
		'message': '🚀 Привет https://пример.рф/путь?q=1#frag и www.Example.CO.UK/news.',
		'entities': [url_entity(10, 31)]
	}),
	(ALPHA, {
		'id': 12,
		'date': '2026-01-12T12:00:00+00:00',
		'message': 'Read https://example.com/a now; see https://same.example.org/ and '
			'https://t.me/beta_fx/5 https://t.me/beta_fx/5',
		'entities': [
			text_link(5, 21, 'https://evil.example.net/x'),
			text_link(36, 25, 'https://same.example.org/'),
			url_entity(66, 22),
			url_entity(89, 22),
			{'_': 'MessageEntityBold', 'offset': 0, 'length': 4}
		]
	}),
	(ALPHA, {
		'id': 13,
		'date': '2026-01-13T13:00:00+00:00',
		'message': 'tg://resolve?domain=beta_fx ftp://files.example.com/a mailto:x@example.com',
		'entities': [url_entity(54, 20)],
		'media': {
			'_': 'MessageMediaWebPage', 'force_large_media': False, 'force_small_media': False,
			'manual': False, 'safe': False,
			'webpage': {'_': 'WebPagePending', 'id': 6013, 'url': 'http://192.168.1.10:8080/x',
				'date': '2026-01-13T13:00:00+00:00'}
		}
	}),
	(ALPHA, {
		'id': 14,
		'date': '2026-01-14T14:00:00+00:00',
		'message': 'https://Foo.BlogSpot.com/p, http://[::1]:80/ (http://localhost:8000/) '
			'https://site.unknowntld/',
		'fwd_from': {
			'_': 'MessageFwdHeader', 'date': '2026-01-05T05:00:00+00:00', 'imported': False,
			'saved_out': False, 'from_id': {'_': 'PeerChannel', 'channel_id': 201}, 'from_name': None,
			'channel_post': 77, 'post_author': None, 'saved_from_peer': None, 'saved_from_msg_id': None
		}
	}),
	(ALPHA, {
		'id': 15,
		'date': '2026-01-15T15:00:00+00:00',
		'message': '🚀x http://bad_host!/ https://github.io/',
		'entities': [url_entity(1, 2), url_entity(4, 17)],
		'media': {
			'_': 'MessageMediaWebPage', 'force_large_media': False, 'force_small_media': False,
			'manual': False, 'safe': False, 'webpage': {'_': 'WebPageEmpty', 'id': 6015, 'url': None}
		}
	}),
	(BETA, {
		'id': 21,
		'date': '2026-01-21T21:00:00+00:00',
		'message': 'Also www.example.co.uk/other',
		'entities': []
	}),
)


def add_url_messages(world):
	'''
	Put MESSAGES at the top of their channels' histories (newest first).
	'''
	for channel_id, spec in sorted(MESSAGES, key=lambda m: m[1]['id']):
		world.histories[channel_id].insert(0, FixtureWorld._message(world.raw, channel_id, spec))


# Expected occurrences of the alpha_fx and beta_fx histories once
# MESSAGES are collected, in output order (alpha_fx newest message
# first, then beta_fx):
# (channel, message_id, occurrence_index, source, evidence, text_offset,
#  text_length, label, raw_url, url_kind, normalized_url, host_type,
#  registered_domain, domain_key)
OCCURRENCES = (
	('alpha_fx', 15, 0, 'url_entity', 'url_entity', 1, 2, None, None, 'malformed', None, None, None,
		'[malformed]'),
	('alpha_fx', 15, 1, 'url_entity', 'url_entity+plain_text', 4, 17, None, 'http://bad_host!/',
		'malformed', None, None, None, '[malformed]'),
	('alpha_fx', 15, 2, 'plain_text', 'plain_text', 22, 18, None, 'https://github.io/', 'web',
		'https://github.io/', 'public_suffix', None, '[public-suffix]'),
	('alpha_fx', 14, 0, 'plain_text', 'plain_text', 0, 26, None, 'https://Foo.BlogSpot.com/p', 'web',
		'https://foo.blogspot.com/p', 'domain', 'foo.blogspot.com', 'foo.blogspot.com'),
	('alpha_fx', 14, 1, 'plain_text', 'plain_text', 28, 16, None, 'http://[::1]:80/', 'web',
		'http://[::1]/', 'ipv6', None, '[ipv6]'),
	('alpha_fx', 14, 2, 'plain_text', 'plain_text', 46, 22, None, 'http://localhost:8000/', 'web',
		'http://localhost:8000/', 'local', None, '[local]'),
	('alpha_fx', 14, 3, 'plain_text', 'plain_text', 70, 24, None, 'https://site.unknowntld/', 'web',
		'https://site.unknowntld/', 'unknown_suffix', None, '[unknown-suffix]'),
	('alpha_fx', 13, 0, 'plain_text', 'plain_text', 0, 27, None, 'tg://resolve?domain=beta_fx',
		'other_scheme', 'tg://resolve?domain=beta_fx', None, None, '[no-host]'),
	('alpha_fx', 13, 1, 'plain_text', 'plain_text', 28, 25, None, 'ftp://files.example.com/a',
		'other_scheme', 'ftp://files.example.com/a', 'domain', 'example.com', 'example.com'),
	('alpha_fx', 13, 2, 'url_entity', 'url_entity', 54, 20, None, 'mailto:x@example.com',
		'other_scheme', 'mailto:x@example.com', None, None, '[no-host]'),
	('alpha_fx', 13, 3, 'preview', 'preview', None, None, None, 'http://192.168.1.10:8080/x', 'web',
		'http://192.168.1.10:8080/x', 'ipv4', None, '[ipv4]'),
	('alpha_fx', 12, 0, 'text_link', 'text_link', 5, 21, 'https://example.com/a',
		'https://evil.example.net/x', 'web', 'https://evil.example.net/x', 'domain', 'example.net',
		'example.net'),
	('alpha_fx', 12, 1, 'plain_text', 'plain_text', 5, 21, None, 'https://example.com/a', 'web',
		'https://example.com/a', 'domain', 'example.com', 'example.com'),
	('alpha_fx', 12, 2, 'text_link', 'text_link+plain_text', 36, 25, 'https://same.example.org/',
		'https://same.example.org/', 'web', 'https://same.example.org/', 'domain', 'example.org',
		'example.org'),
	('alpha_fx', 12, 3, 'url_entity', 'url_entity+plain_text', 66, 22, None, 'https://t.me/beta_fx/5',
		'web', 'https://t.me/beta_fx/5', 'domain', 't.me', 't.me'),
	('alpha_fx', 12, 4, 'url_entity', 'url_entity+plain_text', 89, 22, None, 'https://t.me/beta_fx/5',
		'web', 'https://t.me/beta_fx/5', 'domain', 't.me', 't.me'),
	('alpha_fx', 11, 0, 'url_entity', 'url_entity+plain_text', 10, 31, None,
		'https://пример.рф/путь?q=1#frag', 'web', 'https://xn--e1afmkfd.xn--p1ai/путь?q=1#frag', 'domain',
		'xn--e1afmkfd.xn--p1ai', 'xn--e1afmkfd.xn--p1ai'),
	('alpha_fx', 11, 1, 'plain_text', 'plain_text', 44, 22, None, 'www.Example.CO.UK/news', 'schemeless',
		'//www.example.co.uk/news', 'domain', 'example.co.uk', 'example.co.uk'),
	('alpha_fx', 8, 0, 'plain_text', 'plain_text', 10, 25, None, 'https://www.example.org/a', 'web',
		'https://www.example.org/a', 'domain', 'example.org', 'example.org'),
	('alpha_fx', 8, 1, 'preview', 'preview', None, None, None, 'https://www.example.org/a', 'web',
		'https://www.example.org/a', 'domain', 'example.org', 'example.org'),
	('beta_fx', 21, 0, 'plain_text', 'plain_text', 5, 23, None, 'www.example.co.uk/other', 'schemeless',
		'//www.example.co.uk/other', 'domain', 'example.co.uk', 'example.co.uk'),
)

OCCURRENCE_FIELDS = (
	'channel', 'message_id', 'occurrence_index', 'source', 'evidence', 'text_offset', 'text_length',
	'label', 'raw_url', 'url_kind', 'normalized_url', 'host_type', 'registered_domain', 'domain_key'
)

# domains: domain_key -> (domain_kind, occurrences, text_occurrences,
# preview_occurrences, messages, urls, channels)
DOMAINS = {
	'example.co.uk': ('domain', 2, 2, 0, 2, 2, 2),
	'example.org': ('domain', 3, 2, 1, 2, 2, 1),
	'example.com': ('domain', 2, 2, 0, 2, 2, 1),
	'example.net': ('domain', 1, 1, 0, 1, 1, 1),
	't.me': ('domain', 2, 2, 0, 1, 1, 1),
	'xn--e1afmkfd.xn--p1ai': ('domain', 1, 1, 0, 1, 1, 1),
	'foo.blogspot.com': ('domain', 1, 1, 0, 1, 1, 1),
	'[no-host]': ('no_host', 2, 2, 0, 1, 2, 1),
	'[ipv4]': ('ipv4', 1, 0, 1, 1, 1, 1),
	'[ipv6]': ('ipv6', 1, 1, 0, 1, 1, 1),
	'[local]': ('local', 1, 1, 0, 1, 1, 1),
	'[unknown-suffix]': ('unknown_suffix', 1, 1, 0, 1, 1, 1),
	'[public-suffix]': ('public_suffix', 1, 1, 0, 1, 1, 1),
	# the URL of a span splitting a character is unknown: no URL to count
	'[malformed]': ('malformed', 2, 2, 0, 1, 1, 1),
}

# channels: channel -> (occurrences, text_occurrences,
# preview_occurrences, messages_with_urls, urls, domains)
CHANNELS = {
	'alpha_fx': (20, 18, 2, 6, 17, 14),
	'beta_fx': (1, 1, 0, 1, 1, 1),
	'empty_fx': (0, 0, 0, 0, 0, 0),
}
