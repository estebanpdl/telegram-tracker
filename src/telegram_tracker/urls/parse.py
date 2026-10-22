# -*- coding: utf-8 -*-
'''
Parsing, classifying and normalizing one URL as written in a message,
offline. Raw text is never fetched, resolved or rewritten beyond the
normalization below (NORMALIZATION_VERSION):

	- scheme and host lowercased; the host converted to ASCII label by
	  label (NFC, then punycode for non-ASCII labels, no other mapping),
	  with `host_unicode` for display; one trailing dot dropped;
	- the default port dropped for http (80) and https (443); an empty
	  http(s) path becomes `/`;
	- userinfo, path, query and fragment kept exactly as written;
	- a URL without a scheme keeps none: it normalizes to the
	  scheme-relative form `//host/...`.

Registered domains come from the public suffix list bundled with
tldextract, with private suffixes included and online updates off
(Suffixes).
'''

# import modules
import hashlib
import ipaddress
import re
import unicodedata

from dataclasses import dataclass


NORMALIZATION_VERSION = 1

# url_kind
WEB = 'web'
SCHEMELESS = 'schemeless'
OTHER_SCHEME = 'other_scheme'
MALFORMED = 'malformed'

# host_type
DOMAIN = 'domain'
IPV4 = 'ipv4'
IPV6 = 'ipv6'
LOCAL = 'local'
UNKNOWN_SUFFIX = 'unknown_suffix'
PUBLIC_SUFFIX = 'public_suffix'

# domain_key of URLs without a registered domain
BUCKETS = {
	IPV4: '[ipv4]',
	IPV6: '[ipv6]',
	LOCAL: '[local]',
	UNKNOWN_SUFFIX: '[unknown-suffix]',
	PUBLIC_SUFFIX: '[public-suffix]'
}
NO_HOST = '[no-host]'
MALFORMED_KEY = '[malformed]'

# schemes whose `//authority` names a network host
NETWORK_SCHEMES = frozenset(('http', 'https', 'ftp', 'ftps', 'sftp', 'ws', 'wss'))
WEB_SCHEMES = ('http', 'https')
DEFAULT_PORTS = {'http': 80, 'https': 443}

SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*):')
AUTHORITY = re.compile(r'([^/?#]*)(.*)', re.S)
HOST_LABEL = re.compile(r'[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?')


@dataclass(frozen=True)
class ParsedUrl:
	'''
	One URL's components; see the module docstring. Every field but
	`url_kind` and `domain_key` may be None.
	'''
	url_kind: str
	domain_key: str
	scheme: str | None = None
	netloc: str | None = None
	userinfo: str | None = None
	host: str | None = None
	host_unicode: str | None = None
	port: int | None = None
	host_type: str | None = None
	subdomain: str | None = None
	suffix: str | None = None
	is_private_suffix: bool | None = None
	registered_domain: str | None = None
	registry_domain: str | None = None
	path: str | None = None
	query: str | None = None
	fragment: str | None = None
	normalized_url: str | None = None


MALFORMED_URL = ParsedUrl(url_kind=MALFORMED, domain_key=MALFORMED_KEY)


class Suffixes:
	'''
	The public suffix list bundled with tldextract: no download, no
	cache file, private suffixes (blogspot.com, github.io) included.
	'''
	def __init__(self):
		import tldextract

		self.version = tldextract.__version__
		self.extract = tldextract.TLDExtract(
			suffix_list_urls=(),
			cache_dir=None,
			fallback_to_snapshot=True,
			include_psl_private_domains=True
		)

	def split(self, host):
		'''
		tldextract's ExtractResult for an ASCII lowercase host name.
		'''
		return self.extract.extract_str(host)

	def describe(self):
		'''
		What the build used, for its records.
		'''
		suffixes = sorted(self.extract.tlds)
		return {
			'library': 'tldextract',
			'version': self.version,
			'suffix_list': 'bundled snapshot',
			'online_updates': False,
			'private_suffixes': 'included',
			'suffixes': len(suffixes),
			'sha256': hashlib.sha256('\n'.join(suffixes).encode('utf-8')).hexdigest()
		}


'''

Hosts

'''

def ascii_host(host):
	'''
	(ASCII host, Unicode host) for a host name as written, or None when a
	label is empty, holds characters a host name cannot, or is
	undecodable punycode.
	'''
	if host.endswith('.'):
		host = host[:-1]
	if not host:
		return None
	ascii_labels = []
	unicode_labels = []
	for label in unicodedata.normalize('NFC', host).lower().split('.'):
		if label.isascii():
			ascii_label = label
			if label.startswith('xn--'):
				try:
					unicode_label = label[4:].encode('ascii').decode('punycode')
				except UnicodeError:
					return None
			else:
				unicode_label = label
		else:
			ascii_label = 'xn--' + label.encode('punycode').decode('ascii')
			unicode_label = label
		if not HOST_LABEL.fullmatch(ascii_label):
			return None
		ascii_labels.append(ascii_label)
		unicode_labels.append(unicode_label)
	return '.'.join(ascii_labels), '.'.join(unicode_labels)


def is_ipv4(host):
	try:
		ipaddress.IPv4Address(host)
	except ValueError:
		return False
	return True


def classify_host(host, suffixes):
	'''
	The host fields of ParsedUrl for an ASCII host name.
	'''
	if is_ipv4(host):
		return {'host_type': IPV4, 'domain_key': BUCKETS[IPV4]}
	if '.' not in host:
		return {'host_type': LOCAL, 'domain_key': BUCKETS[LOCAL]}

	parts = suffixes.split(host)
	fields = {
		'suffix': parts.suffix or None,
		'is_private_suffix': bool(parts.is_private) if parts.suffix else None,
		'registry_domain': parts.top_domain_under_registry_suffix or None
	}
	if not parts.suffix:
		return {'host_type': UNKNOWN_SUFFIX, 'domain_key': BUCKETS[UNKNOWN_SUFFIX]}
	if not parts.domain:
		return {**fields, 'host_type': PUBLIC_SUFFIX, 'domain_key': BUCKETS[PUBLIC_SUFFIX]}
	registered = parts.top_domain_under_public_suffix
	return {
		**fields,
		'host_type': DOMAIN,
		'domain_key': registered,
		'registered_domain': registered,
		'subdomain': parts.subdomain or None
	}


def known_suffix_host(host, suffixes):
	'''
	True when `host` (as written) is a domain under a known suffix: the
	test plain text must pass for a link without a scheme or `www.`.
	'''
	converted = ascii_host(host)
	if converted is None or '.' not in converted[0] or is_ipv4(converted[0]):
		return False
	parts = suffixes.split(converted[0])
	return bool(parts.suffix and parts.domain)


'''

URLs

'''

def split_rest(rest):
	'''
	(path, query, fragment) of what follows the authority; query and
	fragment are None when absent, '' when present but empty.
	'''
	rest, hash_mark, fragment = rest.partition('#')
	path, question_mark, query = rest.partition('?')
	return path, (query if question_mark else None), (fragment if hash_mark else None)


def split_hostport(hostport):
	'''
	(host, ipv6, port text) of an authority without userinfo, or None for
	unbalanced brackets.
	'''
	if hostport.startswith('['):
		end = hostport.find(']')
		if end < 0:
			return None
		after = hostport[end + 1:]
		if after and not after.startswith(':'):
			return None
		return hostport[1:end], True, after[1:] if after else None
	if ']' in hostport or '[' in hostport:
		return None
	host, colon, port = hostport.rpartition(':')
	if not colon:
		return hostport, False, None
	return host, False, port


def parse_port(text):
	'''
	The port as an int, None for an absent or empty one; raises
	ValueError for anything else.
	'''
	if not text:
		return None
	if not text.isascii() or not text.isdigit() or int(text) > 65535:
		raise ValueError(text)
	return int(text)


def find_scheme(text):
	'''
	The scheme of `text` as written, or None. `host:port` and
	`example.com:8080/x` are not schemes: a scheme is followed by `//`, or
	has no dot and is not followed by a digit.
	'''
	match = SCHEME.match(text)
	if match is None:
		return None
	scheme = match.group(1)
	after = text[match.end():]
	if after.startswith('//'):
		return scheme
	if '.' in scheme or after[:1].isdigit():
		return None
	return scheme


def parse_url(raw, suffixes):
	'''
	The ParsedUrl of `raw` (a URL as written; surrounding whitespace is
	ignored).
	'''
	if not isinstance(raw, str) or not raw.strip():
		return MALFORMED_URL
	text = raw.strip()
	written_scheme = find_scheme(text)
	scheme = written_scheme.lower() if written_scheme else None

	if scheme is None:
		after = text[2:] if text.startswith('//') else text
	elif scheme in NETWORK_SCHEMES:
		after = text[len(written_scheme) + 1:]
		if not after.startswith('//'):
			return MALFORMED_URL
		after = after[2:]
	else:
		# mailto:, tg:, ... : no network host
		rest = text[len(written_scheme) + 1:]
		return ParsedUrl(
			url_kind=OTHER_SCHEME, domain_key=NO_HOST, scheme=scheme,
			normalized_url=f'{scheme}:{rest}'
		)

	authority, rest = AUTHORITY.match(after).groups()
	userinfo, at, hostport = authority.rpartition('@')
	userinfo = userinfo if at else None
	split = split_hostport(hostport)
	if split is None:
		return MALFORMED_URL
	host, ipv6, port_text = split
	try:
		port = parse_port(port_text)
	except ValueError:
		return MALFORMED_URL

	if ipv6:
		try:
			host_ascii = str(ipaddress.IPv6Address(host))
		except ValueError:
			return MALFORMED_URL
		host_unicode = host_ascii
		host_fields = {'host_type': IPV6, 'domain_key': BUCKETS[IPV6]}
		host_text = f'[{host_ascii}]'
	else:
		converted = ascii_host(host)
		if converted is None:
			return MALFORMED_URL
		host_ascii, host_unicode = converted
		host_fields = classify_host(host_ascii, suffixes)
		host_text = host_ascii

	path, query, fragment = split_rest(rest)
	if scheme in WEB_SCHEMES and not path:
		normalized_path = '/'
	else:
		normalized_path = path
	shown_port = '' if port is None or DEFAULT_PORTS.get(scheme) == port else f':{port}'
	normalized = (
		(f'{scheme}://' if scheme else '//')
		+ (f'{userinfo}@' if userinfo is not None else '')
		+ host_text + shown_port + normalized_path
		+ (f'?{query}' if query is not None else '')
		+ (f'#{fragment}' if fragment is not None else '')
	)
	if scheme is None:
		kind = SCHEMELESS
	elif scheme in WEB_SCHEMES:
		kind = WEB
	else:
		kind = OTHER_SCHEME
	return ParsedUrl(
		url_kind=kind, scheme=scheme, netloc=authority, userinfo=userinfo, host=host_ascii,
		host_unicode=host_unicode, port=port, path=path, query=query, fragment=fragment,
		normalized_url=normalized, **host_fields
	)
