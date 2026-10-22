# -*- coding: utf-8 -*-
'''
URL occurrences in one collected message, from what the payload holds:

	url_entity  a MessageEntityUrl: the span of the text is the URL
	text_link   a MessageEntityTextUrl: a hidden target behind a label
	plain_text  a URL found in the text by pattern (PLAIN_URL)
	preview     the link preview (MessageMediaWebPage) of the message

Entity offsets and lengths are UTF-16 code units, as Telegram sends
them; plain-text occurrences are given in the same units.

Overlapping evidence is merged only for the same span and target: a
plain-text match overlapping a URL entity is that entity (Telegram's
own parse wins); one inside a text-link label is merged only when it
names the link's target, and stays its own occurrence otherwise. A
preview is always its own occurrence. `evidence` lists every kind merged
into an occurrence. Repeated URLs and distinct targets stay separate.
'''

# import modules
import re

from dataclasses import dataclass

# import local submodules
from telegram_tracker.urls.parse import parse_url, known_suffix_host


URL_ENTITY = 'url_entity'
TEXT_LINK = 'text_link'
PLAIN_TEXT = 'plain_text'
PREVIEW = 'preview'

ENTITY_KINDS = {'MessageEntityUrl': URL_ENTITY, 'MessageEntityTextUrl': TEXT_LINK}

# scheme://..., www.host..., or host.suffix/path (the suffix is checked
# against the public suffix list afterwards)
PLAIN_URL = re.compile(
	r'(?P<scheme>(?<![\w+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"]+)'
	r'|(?P<www>(?<![\w@./-])www\d{0,3}\.[^\s<>"]+)'
	r'|(?P<bare>(?<![\w@./:-])[^\W_](?:[\w-]*[^\W_])?(?:\.[^\W_](?:[\w-]*[^\W_])?)+/[^\s<>"]*)',
	re.IGNORECASE
)
TRAILING = '.,;:!?\'"»…'
CLOSING = {')': '(', ']': '[', '}': '{'}


@dataclass
class Occurrence:
	'''
	One URL occurrence of a message, before parsing.
	'''
	source: str
	evidence: list
	raw_url: str | None
	offset: int | None = None
	length: int | None = None
	label: str | None = None
	preview: dict | None = None


'''

UTF-16 spans

'''

def utf16(text):
	return text.encode('utf-16-le', 'surrogatepass')


def utf16_slice(data, offset, length):
	'''
	The text of a UTF-16 span of `data` (utf16() bytes), or None when the
	span is out of range or splits a character.
	'''
	if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 \
			or 2 * (offset + length) > len(data):
		return None
	try:
		return data[2 * offset:2 * (offset + length)].decode('utf-16-le')
	except UnicodeDecodeError:
		return None


def utf16_index(text, index):
	'''
	The UTF-16 offset of the Python string index `index`.
	'''
	return len(utf16(text[:index])) // 2


'''

Plain text

'''

def trim(candidate):
	'''
	`candidate` without trailing punctuation and unbalanced closing
	brackets.
	'''
	while candidate:
		last = candidate[-1]
		if last in TRAILING:
			candidate = candidate[:-1]
		elif last in CLOSING and candidate.count(last) > candidate.count(CLOSING[last]):
			candidate = candidate[:-1]
		else:
			break
	return candidate


def plain_candidates(text, suffixes):
	'''
	[(start, end, url)] of the URLs found by pattern, as Python string
	indices.
	'''
	found = []
	for match in PLAIN_URL.finditer(text):
		url = trim(match.group(0))
		if match.group('scheme') is not None:
			if url.endswith('://'):
				continue
		elif match.group('bare') is not None:
			if not known_suffix_host(url.split('/', 1)[0], suffixes):
				continue
		elif '.' not in url[url.index('.') + 1:]:
			# `www.` alone, or `www.name` without a suffix
			continue
		found.append((match.start(), match.start() + len(url), url))
	return found


'''

Messages

'''

def url_key(occurrence, parsed):
	return parsed.normalized_url if parsed.normalized_url is not None else occurrence.raw_url


def text_occurrences(message, suffixes):
	'''
	[(Occurrence, ParsedUrl)] from the text and entities of a message, in
	text order.
	'''
	text = message.get('message') or ''
	data = utf16(text)
	spans = []
	for entity in message.get('entities') or ():
		kind = ENTITY_KINDS.get(entity.get('_'))
		if kind is None:
			continue
		offset, length = entity.get('offset'), entity.get('length')
		span = utf16_slice(data, offset, length)
		if kind == URL_ENTITY:
			occurrence = Occurrence(URL_ENTITY, [URL_ENTITY], span, offset, length)
		else:
			occurrence = Occurrence(TEXT_LINK, [TEXT_LINK], entity.get('url'), offset, length, label=span)
		spans.append((occurrence, parse_url(occurrence.raw_url, suffixes)))

	plain = []
	for start, end, url in plain_candidates(text, suffixes):
		begin = utf16_index(text, start)
		occurrence = Occurrence(PLAIN_TEXT, [PLAIN_TEXT], url, begin, utf16_index(text, end) - begin)
		parsed = parse_url(url, suffixes)
		if not merge_plain(occurrence, parsed, spans):
			plain.append((occurrence, parsed))

	found = spans + plain
	found.sort(key=lambda item: (
		item[0].offset if type(item[0].offset) is int else -1,
		item[0].length if type(item[0].length) is int else -1
	))
	return found


def merge_plain(candidate, parsed, spans):
	'''
	Merge a plain-text match into an entity of the same span: any URL
	entity it overlaps, or a text link it overlaps whose target it names.
	Returns True when merged.
	'''
	start, end = candidate.offset, candidate.offset + candidate.length
	overlapping = [
		(occurrence, entity_parsed) for occurrence, entity_parsed in spans
		if type(occurrence.offset) is int and type(occurrence.length) is int
		and occurrence.offset < end and start < occurrence.offset + occurrence.length
	]
	for occurrence, entity_parsed in overlapping:
		same_target = url_key(occurrence, entity_parsed) == url_key(candidate, parsed)
		if occurrence.source == URL_ENTITY or same_target:
			if PLAIN_TEXT not in occurrence.evidence:
				occurrence.evidence.append(PLAIN_TEXT)
			return True
	return False


def preview_occurrence(message):
	'''
	The Occurrence of a message's link preview, or None.
	'''
	media = message.get('media')
	if not isinstance(media, dict) or media.get('_') != 'MessageMediaWebPage':
		return None
	webpage = media.get('webpage')
	if not isinstance(webpage, dict) or not webpage.get('url'):
		return None
	return Occurrence(PREVIEW, [PREVIEW], webpage['url'], preview={
		'type': webpage.get('_'),
		'display_url': webpage.get('display_url'),
		'site_name': webpage.get('site_name'),
		'title': webpage.get('title'),
		'description': webpage.get('description')
	})


def message_occurrences(message, suffixes):
	'''
	[(Occurrence, ParsedUrl, preview_matches_text)] of a message: the text
	occurrences in text order, then the preview. preview_matches_text is
	None for text occurrences.
	'''
	found = [(occurrence, parsed, None) for occurrence, parsed in text_occurrences(message, suffixes)]
	preview = preview_occurrence(message)
	if preview is not None:
		parsed = parse_url(preview.raw_url, suffixes)
		keys = {url_key(occurrence, p) for occurrence, p, _ in found}
		found.append((preview, parsed, url_key(preview, parsed) in keys))
	return found
