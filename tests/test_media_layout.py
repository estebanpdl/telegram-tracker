# -*- coding: utf-8 -*-
'''
downloaded-media layout: generated names and relative paths, traversal
refusal, link and non-folder refusal at every level (downloaded-media,
channel, type, .partial, .partial/<run>), the final-name inspection, and
the files reader never taking a media file for a history.
'''

# import modules
import hashlib
import os
import stat
import types
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import CHANNEL, RUN_A, SHA_A, MediaTestCase, make_link, remove_link
from telegram_tracker.media import layout
from telegram_tracker.media.layout import ABSENT, DIFFERENT, NOT_REGULAR, SAME, MediaLayout, MediaPathError, \
	PathConflict, check_final, check_relative, file_digest, file_name, inspect_final, partial_name, relative_path
from telegram_tracker.media.ledger import FAIL_REASONS
from telegram_tracker.reader.files import message_files
from telegram_tracker.storage.records import StorageError


def write(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, 'wb') as writer:
		writer.write(data)


def read_bytes(path):
	with open(path, 'rb') as reader:
		return reader.read()


def sha(data):
	return hashlib.sha256(data).hexdigest()


class NameTests(MediaTestCase):

	def test_generated_names(self):
		self.assertEqual(file_name(12, SHA_A, 'jpg'), f'12-{"a" * 16}.jpg')
		self.assertEqual(relative_path(CHANNEL, 'video', 12, SHA_A, 'mp4'), f'1001/videos/12-{"a" * 16}.mp4')
		self.assertEqual(
			[relative_path(1, kind, 2, SHA_A, 'bin').split('/')[1] for kind in ('image', 'video', 'audio', 'file')],
			['images', 'videos', 'audios', 'files']
		)
		self.assertEqual(partial_name(CHANNEL, 12, -5), '1001-12--5.part')
		self.assertRegex(partial_name(CHANNEL, 12, 5), layout.PARTIAL_NAME)
		self.assertRegex(partial_name(CHANNEL, 12, -5), layout.PARTIAL_NAME)

	def test_name_parts_are_checked(self):
		bad = [
			lambda: file_name(0, SHA_A, 'jpg'), lambda: file_name(-1, SHA_A, 'jpg'),
			lambda: file_name(True, SHA_A, 'jpg'), lambda: file_name('12', SHA_A, 'jpg'),
			lambda: file_name(12, 'A' * 64, 'jpg'), lambda: file_name(12, 'a' * 63, 'jpg'),
			lambda: file_name(12, SHA_A, 'exe'), lambda: file_name(12, SHA_A, '../jpg'),
			lambda: relative_path(0, 'image', 1, SHA_A, 'jpg'), lambda: relative_path(1, 'images', 1, SHA_A, 'jpg'),
			lambda: partial_name(1, 1, '5'), lambda: partial_name(-1, 1, 5),
			lambda: layout.check_run_uuid('../x'),
			lambda: layout.check_run_uuid('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'.upper()),
			lambda: layout.check_run_uuid(RUN_A.replace('-', ''))
		]
		for position, call in enumerate(bad):
			with self.subTest(position=position):
				self.assertRaises(ValueError, call)

	def test_relative_paths_are_accepted_back_only_when_generated(self):
		good = relative_path(CHANNEL, 'image', 7, SHA_A, 'jpg')
		self.assertEqual(check_relative(good), good)
		self.assertEqual(self.layout.path_of(good), os.path.join(self.layout.root, '1001', 'images', f'7-{"a" * 16}.jpg'))
		bad = [
			'../outside.jpg', f'1001/images/../../7-{"a" * 16}.jpg', f'/1001/images/7-{"a" * 16}.jpg',
			f'1001\\images\\7-{"a" * 16}.jpg', f'C:/1001/images/7-{"a" * 16}.jpg', f'1001/other/7-{"a" * 16}.jpg',
			f'1001/images/7-{"A" * 16}.jpg', f'1001/images/7-{"a" * 16}.exe', f'0/images/7-{"a" * 16}.jpg',
			f'1001/images/7-{"a" * 16}.jpg/..', f'1001/images/7-{"a" * 15}.jpg', f'1001/images/7-{"a" * 16}.JPG',
			f'1001/images/7-{"a" * 16}', '', None, 7
		]
		for path in bad:
			with self.subTest(path=path):
				self.assertRaises(MediaPathError, check_relative, path)
				self.assertRaises(MediaPathError, self.layout.path_of, path)

	def test_media_files_are_never_read_as_histories(self):
		history = os.path.join(self.out, 'news', 'news_messages.json')
		write(history, b'{"messages": []}')
		for kind, ext in (('image', 'jpg'), ('file', 'json'), ('file', 'bin')):
			write(self.layout.path_of(relative_path(CHANNEL, kind, 3, SHA_A, ext)), b'x')
		write(os.path.join(self.layout.partial_root, RUN_A, partial_name(CHANNEL, 3, 9)), b'x')
		write(self.layout.ledger_path, b'x')
		self.assertEqual(message_files(self.out), [history])


class FolderTests(MediaTestCase):

	def test_folders_are_created_level_by_level(self):
		self.assertIsNone(self.layout.type_folder(CHANNEL, 'image', create=False))
		self.assertFalse(os.path.exists(self.layout.root))
		folder = self.layout.type_folder(CHANNEL, 'image')
		self.assertEqual(folder, os.path.join(self.layout.root, '1001', 'images'))
		self.assertTrue(os.path.isdir(folder))
		self.assertEqual(self.layout.partial_folder(RUN_A), os.path.join(self.layout.root, '.partial', RUN_A))
		final = self.layout.final_path(relative_path(CHANNEL, 'audio', 4, SHA_A, 'ogg'))
		self.assertEqual(final, os.path.join(self.layout.root, '1001', 'audios', f'4-{"a" * 16}.ogg'))
		self.assertFalse(os.path.exists(final))

	def link_cases(self):
		'''
		(label, folder made a link, call that must refuse it)
		'''
		root = self.layout.root
		return [
			('downloaded-media', root, lambda: self.layout.ensure()),
			('channel', os.path.join(root, '1001'), lambda: self.layout.type_folder(CHANNEL, 'image')),
			('type', os.path.join(root, '1001', 'images'), lambda: self.layout.type_folder(CHANNEL, 'image')),
			('final parent', os.path.join(root, '1001', 'videos'),
				lambda: self.layout.final_path(relative_path(CHANNEL, 'video', 1, SHA_A, 'mp4'))),
			('.partial', os.path.join(root, '.partial'), lambda: self.layout.partial_folder(RUN_A)),
			('.partial/<run>', os.path.join(root, '.partial', RUN_A), lambda: self.layout.partial_folder(RUN_A))
		]

	def test_a_link_at_any_level_is_refused(self):
		for position, (label, folder, call) in enumerate(self.link_cases()):
			with self.subTest(level=label):
				target = os.path.join(self.tmp, f'elsewhere-{position}')
				os.mkdir(target)
				os.makedirs(os.path.dirname(folder), exist_ok=True)
				make_link(target, folder)
				try:
					with self.assertRaises(MediaPathError) as caught:
						call()
					self.assertIn('is a link', str(caught.exception))
					# a read-only walk (create=False) refuses it too
					parts = os.path.relpath(folder, self.layout.root).split(os.sep)
					with self.assertRaises(MediaPathError):
						self.layout.ensure(*[p for p in parts if p != '.'], create=False)
					self.assertEqual(os.listdir(target), [])
				finally:
					remove_link(folder)

	def test_a_file_in_place_of_a_folder_is_refused(self):
		for label, folder, call in self.link_cases():
			with self.subTest(level=label):
				os.makedirs(os.path.dirname(folder), exist_ok=True)
				write(folder, b'not a folder')
				with self.assertRaises(MediaPathError) as caught:
					call()
				self.assertIn('is not a folder', str(caught.exception))
				os.remove(folder)

	def test_the_output_folder_may_be_reached_through_a_link(self):
		alias = os.path.join(self.tmp, 'alias')
		self.link(self.out, alias)
		folder = MediaLayout(alias).type_folder(CHANNEL, 'file')
		self.assertTrue(os.path.isdir(os.path.join(self.out, 'downloaded-media', '1001', 'files')))
		self.assertEqual(folder, os.path.join(alias, 'downloaded-media', '1001', 'files'))

	def test_confinement_is_checked_after_the_levels(self):
		with mock.patch.object(layout, 'is_within', return_value=False):
			with self.assertRaises(MediaPathError) as caught:
				self.layout.type_folder(CHANNEL, 'image')
		self.assertIn('resolves outside', str(caught.exception))

	def test_path_errors_are_storage_errors(self):
		self.assertTrue(issubclass(MediaPathError, StorageError))


class FinalNameTests(MediaTestCase):

	def setUp(self):
		super().setUp()
		self.data = b'synthetic media bytes'
		self.final = self.layout.final_path(relative_path(CHANNEL, 'image', 5, sha(self.data), 'jpg'))

	def inspect(self):
		return inspect_final(self.final, sha(self.data), len(self.data))

	def test_absent(self):
		self.assertEqual(self.inspect(), ABSENT)

	def test_same_content_is_adoptable(self):
		write(self.final, self.data)
		self.assertEqual(self.inspect(), SAME)

	def test_different_content(self):
		write(self.final, b'other bytes')
		self.assertEqual(self.inspect(), DIFFERENT)
		write(self.final, b'Synthetic media bytes')  # same size
		self.assertEqual(self.inspect(), DIFFERENT)

	def test_a_folder_or_folder_link_is_not_regular(self):
		os.mkdir(self.final)
		self.assertEqual(self.inspect(), NOT_REGULAR)
		os.rmdir(self.final)
		target = os.path.join(self.tmp, 'target')
		os.mkdir(target)
		self.link(target, self.final)
		self.assertEqual(self.inspect(), NOT_REGULAR)

	def test_a_file_link_with_the_same_content_is_never_adopted(self):
		target = os.path.join(self.tmp, 'same.jpg')
		write(target, self.data)
		try:
			os.symlink(target, self.final)
		except OSError as exc:
			self.skipTest(f'file symlinks are not available here ({exc})')
		self.addCleanup(os.remove, self.final)
		self.assertEqual(self.inspect(), NOT_REGULAR)
		with self.assertRaises(MediaPathError):
			file_digest(self.final)

	def test_link_and_reparse_reports_are_not_regular(self):
		# what lstat reports for a file symlink (POSIX) and a file reparse
		# point (Windows); the bytes behind the name are identical
		write(self.final, self.data)
		real_lstat = os.lstat
		reports = {
			'symlink': types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=len(self.data)),
			'reparse point': types.SimpleNamespace(
				st_mode=stat.S_IFREG | 0o644, st_size=len(self.data), st_reparse_tag=0xA000000C)
		}
		for label, report in reports.items():
			with self.subTest(label):
				def fake_lstat(path, report=report):
					return report if path == self.final else real_lstat(path)

				with mock.patch.object(layout.os, 'lstat', fake_lstat):
					self.assertRaises(MediaPathError, file_digest, self.final)
					# never read through: the verdict comes from lstat alone
					with mock.patch.object(layout, 'file_digest', side_effect=AssertionError('read through a link')):
						self.assertEqual(self.inspect(), NOT_REGULAR)
		self.assertEqual(self.inspect(), SAME)

	def test_check_final_verdicts(self):
		check = lambda: check_final(self.final, sha(self.data), len(self.data))  # noqa: E731
		self.assertEqual(check(), ABSENT)
		write(self.final, self.data)
		self.assertEqual(check(), SAME)
		write(self.final, b'other bytes')
		with self.assertRaises(PathConflict) as caught:
			check()
		self.assertIn('other content', str(caught.exception))
		self.assertEqual(read_bytes(self.final), b'other bytes')
		os.remove(self.final)
		os.mkdir(self.final)
		with self.assertRaises(PathConflict) as caught:
			check()
		self.assertIn('a link or a non-regular file', str(caught.exception))
		self.assertEqual(caught.exception.reason, 'path_conflict')
		self.assertIn(caught.exception.reason, FAIL_REASONS)


class DigestTests(MediaTestCase):

	def test_digest(self):
		path = os.path.join(self.tmp, 'blob')
		data = os.urandom(layout.READ_BLOCK + 17)
		write(path, data)
		self.assertEqual(file_digest(path), (len(data), sha(data)))

	def test_missing_file(self):
		with self.assertRaises(FileNotFoundError):
			file_digest(os.path.join(self.tmp, 'missing'))

	def test_a_file_replaced_between_check_and_open_is_refused(self):
		path = os.path.join(self.tmp, 'blob')
		other = os.path.join(self.tmp, 'other')
		write(path, b'one')
		write(other, b'two')
		other_info = os.stat(other)
		with mock.patch.object(layout.os, 'fstat', lambda fd: other_info):
			with self.assertRaises(MediaPathError) as caught:
				file_digest(path)
		self.assertIn('changed while it was being read', str(caught.exception))


if __name__ == '__main__':
	unittest.main()
