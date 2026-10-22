# -*- coding: utf-8 -*-
'''
One URL at a time (telegram_tracker.urls.parse): classification, the
conservative normalization, registered domains from the bundled public
suffix list (private suffixes included), and the buckets of URLs without
one. Expectations are written out by hand.
'''

# import modules
import os
import re
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.urls.parse import MALFORMED_URL, Suffixes, find_scheme, parse_url


SUFFIXES = Suffixes()


def parse(raw):
	return parse_url(raw, SUFFIXES)


class DomainTests(offline_guard.OfflineTestCase):

	def assertFields(self, raw, **expected):
		parsed = parse(raw)
		got = {name: getattr(parsed, name) for name in expected}
		self.assertEqual(got, expected, raw)

	def test_multi_label_public_suffix(self):
		self.assertFields(
			'https://www.bbc.co.uk/news', url_kind='web', host='www.bbc.co.uk', subdomain='www',
			suffix='co.uk', is_private_suffix=False, registered_domain='bbc.co.uk',
			registry_domain='bbc.co.uk', domain_key='bbc.co.uk', host_type='domain'
		)

	def test_private_suffixes_are_included(self):
		self.assertFields(
			'https://foo.blogspot.com/', suffix='blogspot.com', is_private_suffix=True,
			registered_domain='foo.blogspot.com', registry_domain='blogspot.com', subdomain=None,
			domain_key='foo.blogspot.com'
		)
		self.assertFields(
			'https://a.b.github.io/', suffix='github.io', registered_domain='b.github.io',
			subdomain='a', registry_domain='github.io'
		)

	def test_a_host_that_is_a_suffix_has_no_registered_domain(self):
		self.assertFields(
			'https://github.io/', host_type='public_suffix', registered_domain=None, suffix='github.io',
			is_private_suffix=True, domain_key='[public-suffix]'
		)
		self.assertFields('http://co.uk/', host_type='public_suffix', registered_domain=None)

	def test_idna_hosts_in_both_forms(self):
		for raw in ('https://пример.рф/', 'https://xn--e1afmkfd.xn--p1ai/', 'https://ПРИМЕР.РФ/'):
			with self.subTest(raw=raw):
				self.assertFields(
					raw, host='xn--e1afmkfd.xn--p1ai', host_unicode='пример.рф',
					registered_domain='xn--e1afmkfd.xn--p1ai', suffix='xn--p1ai',
					normalized_url='https://xn--e1afmkfd.xn--p1ai/'
				)

	def test_decomposed_characters_are_composed(self):
		# e + combining acute accent, then the precomposed é
		for raw in ('https://café.com/', 'https://café.com/'):
			with self.subTest(raw=raw):
				self.assertFields(raw, host='xn--caf-dma.com', host_unicode='café.com')

	def test_undecodable_punycode_is_malformed(self):
		# valid host name characters, invalid punycode
		for raw in ('https://xn--0.com/', 'https://www.xn--99999999999.com/'):
			with self.subTest(raw=raw):
				self.assertEqual(parse(raw), MALFORMED_URL)

	def test_ip_and_local_hosts(self):
		self.assertFields('http://192.168.0.1:8080/x', host_type='ipv4', port=8080,
			registered_domain=None, domain_key='[ipv4]', normalized_url='http://192.168.0.1:8080/x')
		self.assertFields('http://[2001:DB8::1]/', host_type='ipv6', host='2001:db8::1',
			domain_key='[ipv6]', normalized_url='http://[2001:db8::1]/')
		self.assertFields('http://localhost/', host_type='local', domain_key='[local]',
			registered_domain=None)
		self.assertFields('https://site.unknowntld/', host_type='unknown_suffix',
			domain_key='[unknown-suffix]', registered_domain=None, suffix=None)

	def test_bad_hosts_and_ports_are_malformed(self):
		for raw in ('http://', 'http:///path', 'http://bad_host!/', 'http://a..b/', 'http://[::1/',
				'http://[zz::1]/', 'http://example.com:99999/', 'http://example.com:8a/',
				'http://-lead.example.com/', 'https:example.com', '', '   ', None):
			with self.subTest(raw=raw):
				self.assertEqual(parse(raw), MALFORMED_URL)


class NormalizationTests(offline_guard.OfflineTestCase):

	def assertNormalized(self, raw, normalized, kind='web'):
		parsed = parse(raw)
		self.assertEqual((parsed.normalized_url, parsed.url_kind), (normalized, kind), raw)

	def test_scheme_and_host_are_lowercased_and_the_rest_kept(self):
		self.assertNormalized('HTTPS://Sub.Example.COM/A/%2f/../B?Q=1&q=2#Frag',
			'https://sub.example.com/A/%2f/../B?Q=1&q=2#Frag')

	def test_default_ports_and_empty_paths(self):
		self.assertNormalized('https://example.com:443', 'https://example.com/')
		self.assertNormalized('http://example.com:80/x', 'http://example.com/x')
		self.assertNormalized('http://example.com:443/x', 'http://example.com:443/x')
		self.assertNormalized('https://example.com:/x', 'https://example.com/x')
		parsed = parse('https://example.com:443')
		self.assertEqual((parsed.port, parsed.path), (443, ''))

	def test_empty_query_and_fragment_are_kept(self):
		self.assertNormalized('https://example.com/?#', 'https://example.com/?#')
		parsed = parse('https://example.com/a?#')
		self.assertEqual((parsed.path, parsed.query, parsed.fragment), ('/a', '', ''))
		parsed = parse('https://example.com/a')
		self.assertEqual((parsed.query, parsed.fragment), (None, None))

	def test_userinfo_is_kept(self):
		parsed = parse('https://user:pw@Example.com:443/')
		self.assertEqual((parsed.netloc, parsed.userinfo, parsed.host), ('user:pw@Example.com:443', 'user:pw',
			'example.com'))
		self.assertEqual(parsed.normalized_url, 'https://user:pw@example.com/')

	def test_one_trailing_dot_is_dropped(self):
		self.assertNormalized('https://example.com./', 'https://example.com/')

	def test_no_scheme_stays_without_one(self):
		self.assertNormalized('www.Example.com/a', '//www.example.com/a', 'schemeless')
		self.assertNormalized('example.com:8080/x', '//example.com:8080/x', 'schemeless')
		self.assertNormalized('localhost:8000', '//localhost:8000', 'schemeless')
		# no http default for a URL without a scheme
		self.assertNormalized('example.com:80', '//example.com:80', 'schemeless')

	def test_other_schemes(self):
		self.assertNormalized('MAILTO:x@example.com', 'mailto:x@example.com', 'other_scheme')
		self.assertNormalized('tg://resolve?domain=x', 'tg://resolve?domain=x', 'other_scheme')
		self.assertEqual(parse('mailto:x@example.com').domain_key, '[no-host]')
		parsed = parse('ftp://files.example.com/a')
		self.assertEqual((parsed.url_kind, parsed.registered_domain), ('other_scheme', 'example.com'))

	def test_what_counts_as_a_scheme(self):
		for text, scheme in (('https://x', 'https'), ('mailto:x', 'mailto'), ('tg://x', 'tg'),
				('host:80', None), ('example.com:8080/x', None), ('a.b://x', 'a.b'), ('x', None)):
			with self.subTest(text=text):
				self.assertEqual(find_scheme(text), scheme)


class SuffixListTests(offline_guard.OfflineTestCase):

	def test_the_pinned_version_is_the_one_recorded(self):
		root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		with open(os.path.join(root, 'requirements.txt'), encoding='utf-8') as fh:
			pinned, = re.findall(r'^tldextract==(\S+)$', fh.read(), re.M)
		described = SUFFIXES.describe()
		self.assertEqual(described['version'], pinned)
		self.assertEqual(
			{k: described[k] for k in ('library', 'suffix_list', 'online_updates', 'private_suffixes')},
			{'library': 'tldextract', 'suffix_list': 'bundled snapshot', 'online_updates': False,
				'private_suffixes': 'included'}
		)
		self.assertRegex(described['sha256'], '^[0-9a-f]{64}$')
		self.assertGreater(described['suffixes'], 1000)

	def test_the_extractor_never_downloads_or_caches(self):
		extract = SUFFIXES.extract
		self.assertEqual(tuple(extract.suffix_list_urls), ())
		self.assertFalse(extract._cache.enabled)
		self.assertTrue(extract.include_psl_private_domains)


if __name__ == '__main__':
	unittest.main()
