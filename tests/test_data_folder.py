# -*- coding: utf-8 -*-
'''
Data folder resolution: trailing slashes are stripped, but a root-only
path stays a root instead of collapsing to an empty folder.
'''

# import modules
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.utils import data_folder_from


class DataFolderTests(offline_guard.OfflineTestCase):

	def test_trailing_slash_is_stripped(self):
		self.assertEqual(data_folder_from('out/'), 'out')

	def test_plain_folder_is_unchanged(self):
		self.assertEqual(data_folder_from('out'), 'out')

	def test_missing_path_uses_default(self):
		self.assertEqual(data_folder_from(''), './output/data')
		self.assertEqual(data_folder_from(None), './output/data')

	def test_root_only_path_is_kept(self):
		self.assertEqual(data_folder_from('/'), '/')


if __name__ == '__main__':
	unittest.main()
