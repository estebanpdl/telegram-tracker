# -*- coding: utf-8 -*-
"""Constructor and synthetic-value checks for the SQLite payload fixtures."""

import json
from pathlib import Path
import unittest

import offline_guard  # noqa: F401 (installs the process-wide offline guard)

from telethon.tl.alltlobjects import tlobjects


VARIANTS_PATH = Path(__file__).parent / 'fixtures' / 'sqlite' / 'variants.json'
VARIANTS = json.loads(VARIANTS_PATH.read_text(encoding='utf-8'))


def walk(value):
	'''
	Yield every (key, value) pair in nested dicts and lists.
	'''
	if isinstance(value, dict):
		for key, item in value.items():
			yield key, item
			yield from walk(item)
	elif isinstance(value, list):
		for item in value:
			yield from walk(item)


def constructors(raw):
	return {item for key, item in walk(raw) if key == '_'} | ({raw['_']} if '_' in raw else set())


class FixtureTests(unittest.TestCase):

	def test_constructors_exist_in_pinned_telethon(self):
		names = {cls.__name__ for cls in tlobjects.values()}
		for group in ('messages', 'peers', 'envelopes'):
			for variant in VARIANTS[group]:
				with self.subTest(variant['name']):
					missing = constructors(variant['raw']) - names
					if variant['telethon']:
						self.assertEqual(missing, set())
					else:
						self.assertTrue(missing, 'a hand-built variant should carry an unknown constructor')
		self.assertEqual(constructors(VARIANTS['channel_full']['raw']) - names, set())

	def test_values_are_synthetic(self):
		for key, value in walk(VARIANTS):
			if value is None:
				continue
			if key == 'username':
				self.assertTrue(value.endswith('_fx'), value)
			elif key in ('phone', 'phone_number'):
				self.assertTrue(value.startswith('99966'), value)
			elif key in ('access_hash', 'id', 'photo_id', 'user_id', 'channel_id', 'chat_id'):
				self.assertLess(value, 10 ** 6, key)

	def test_variant_names_are_unique(self):
		for group in ('messages', 'peers', 'envelopes'):
			names = [v['name'] for v in VARIANTS[group]]
			self.assertEqual(len(names), len(set(names)), group)


if __name__ == '__main__':
	unittest.main()
