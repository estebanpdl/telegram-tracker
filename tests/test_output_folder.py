# -*- coding: utf-8 -*-
'''
Output and data folders given on the command line: trailing separators
are dropped without turning a filesystem root into an empty or
drive-relative path, and a root is refused by every command before any
handler runs.

Path cases use ntpath and posixpath, so the Windows and POSIX rules are
checked on every system; the command-line cases use the roots of the
system running the tests.
'''

# import modules
import argparse
import contextlib
import io
import ntpath
import os
import posixpath
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker import cli
from telegram_tracker.cli.argument_types import DATA_FOLDER, OUTPUT_FOLDER
from telegram_tracker.utils import data_folder_from as reexported
from telegram_tracker.utils.folders import data_folder_from, is_filesystem_root, root_folder_refusal


DEFAULT = './output/data'

# (given, folder, is a root) per flavor
POSIX_CASES = (
	('/', '/', True),
	('//', '//', True),
	('///', '/', True),
	('D:/', 'D:', False),
	('D:\\', 'D:\\', False),
	('C:/', 'C:', False),
	('out/', 'out', False),
	('out//', 'out', False),
	('out\\', 'out\\', False),
	('a/b/', 'a/b', False),
	('/data/telegram/', '/data/telegram', False),
	('./', '.', False),
)
WINDOWS_CASES = (
	('/', '/', True),
	('\\', '\\', True),
	('//', '//', True),
	('D:/', 'D:/', True),
	('D:\\', 'D:\\', True),
	('C:/', 'C:/', True),
	('D:/\\', 'D:/', True),
	('D:\\\\', 'D:\\', True),
	('\\\\server\\share\\', '\\\\server\\share\\', True),
	('\\\\server\\share', '\\\\server\\share', True),
	('//server/share/', '//server/share/', True),
	('\\\\?\\D:\\', '\\\\?\\D:\\', True),
	('D:', 'D:', False),
	('D:out\\', 'D:out', False),
	('out/', 'out', False),
	('out\\', 'out', False),
	('out\\/', 'out', False),
	('D:/out/', 'D:/out', False),
	('D:\\data\\telegram\\', 'D:\\data\\telegram', False),
	('\\\\server\\share\\out\\', '\\\\server\\share\\out', False),
	('./', '.', False),
)
FLAVORS = ((posixpath, POSIX_CASES), (ntpath, WINDOWS_CASES))

OUTPUT_REFUSAL = 'refusing to use a filesystem root as the output folder; choose a subfolder, e.g. /data/telegram'
DATA_REFUSAL = 'refusing to use a filesystem root as the data folder; choose a subfolder, e.g. /data/telegram'


def system_roots():
	'''
	Roots of the system running the tests, in the forms a user types.
	'''
	if os.name != 'nt':
		return ['/', '//', '///']
	drive = os.path.splitroot(os.getcwd())[0]
	return ['/', '\\', f'{drive}\\', f'{drive}/', f'{drive}/\\']


class FolderPathTests(unittest.TestCase):

	def test_folders_and_roots(self):
		for flavor, cases in FLAVORS:
			for given, folder, root in cases:
				with self.subTest(flavor=flavor.__name__, given=given):
					self.assertEqual(data_folder_from(given, DEFAULT, flavor=flavor), folder)
					self.assertIs(is_filesystem_root(folder, flavor=flavor), root)
					self.assertIs(is_filesystem_root(given, flavor=flavor), root)

	def test_a_root_never_becomes_empty_or_drive_relative(self):
		# the earlier rule cut one trailing '/': '/' -> '' (the current
		# directory) and 'D:/' -> 'D:' (the current directory on D:)
		for flavor, cases in FLAVORS:
			for given, _, root in cases:
				folder = data_folder_from(given, DEFAULT, flavor=flavor)
				with self.subTest(flavor=flavor.__name__, given=given):
					self.assertTrue(folder)
					if root:
						drive, anchor, tail = flavor.splitroot(folder)
						self.assertEqual(tail, '')
						self.assertTrue(anchor or drive.startswith(('/', '\\')))

	def test_normalizing_twice_changes_nothing(self):
		for flavor, cases in FLAVORS:
			for given, folder, _ in cases:
				with self.subTest(flavor=flavor.__name__, given=given):
					self.assertEqual(data_folder_from(folder, DEFAULT, flavor=flavor), folder)

	def test_missing_path_means_the_default(self):
		for given in (None, ''):
			with self.subTest(given=given):
				self.assertEqual(data_folder_from(given, DEFAULT), DEFAULT)
				self.assertEqual(data_folder_from(given), DEFAULT)

	def test_native_flavor_is_the_default(self):
		path = os.path.join('out', 'data') + os.sep
		self.assertEqual(data_folder_from(path, DEFAULT), os.path.join('out', 'data'))
		self.assertEqual(data_folder_from('/', DEFAULT), '/')
		self.assertIs(is_filesystem_root('/'), True)
		self.assertIs(is_filesystem_root('out'), False)

	def test_utils_reexports_the_helper(self):
		self.assertIs(reexported, data_folder_from)

	def test_root_refusal(self):
		for flavor, cases in FLAVORS:
			for given, folder, root in cases:
				with self.subTest(flavor=flavor.__name__, given=given):
					refusal = root_folder_refusal(folder, 'output folder', flavor=flavor)
					self.assertEqual(refusal, OUTPUT_REFUSAL if root else None)
		self.assertEqual(root_folder_refusal('/', 'data folder', flavor=posixpath), DATA_REFUSAL)


class FolderArgumentTypeTests(unittest.TestCase):

	def test_roots_are_refused(self):
		for kind, message in ((OUTPUT_FOLDER, OUTPUT_REFUSAL), (DATA_FOLDER, DATA_REFUSAL)):
			for root in system_roots():
				with self.subTest(message=message, root=root):
					with self.assertRaises(argparse.ArgumentTypeError) as caught:
						kind(root)
					self.assertEqual(str(caught.exception), message)

	def test_other_folders_are_returned_as_given(self):
		for kind in (OUTPUT_FOLDER, DATA_FOLDER):
			for given in ('out/', 'out', 'a/b//', '', os.path.join(os.getcwd(), 'out') + os.sep):
				with self.subTest(given=given):
					self.assertEqual(kind(given), given)


class RootFolderCliTests(offline_guard.OfflineTestCase):
	'''
	Every command refuses a root with exit 2 before its handler runs, so no
	collection, folder read, credential read or Telegram import is
	attempted. The handlers are replaced by recorders; the same recorders
	see an ordinary folder, which shows they are the ones the commands
	call.
	'''

	HANDLERS = {
		'collect': ('dispatch', 'run_collect'),
		'download-media': ('dispatch', 'run_download_media'),
		'builder': ('dispatch', 'run_builder'),
		'legacy builder': ('builders', 'run_builder'),
	}

	def setUp(self):
		super().setUp()
		self.calls = []
		for name, (module, attribute) in self.HANDLERS.items():
			def record(*args, name=name, **kwargs):
				self.calls.append((name, args, kwargs))
				return 0

			patcher = mock.patch.object(getattr(cli, module), attribute, record)
			patcher.start()
			self.addCleanup(patcher.stop)

	def run_cli(self, argv, entry=None):
		entry = entry or (lambda a: cli.main(a))
		stderr = io.StringIO()
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
			try:
				code = entry(argv)
			except SystemExit as exc:
				code = exc.code
		return code, stderr.getvalue()

	def forms(self, folder):
		'''
		(handler, argv, entry, refusal) for every option that names a folder.
		'''
		forms = [
			('collect', ['collect', '--telegram-channel', 'a_fx', '--output', folder], None, OUTPUT_REFUSAL),
			('collect', ['collect', '--batch-file', 'list.txt', '-o', folder], None, OUTPUT_REFUSAL),
			('collect', ['collect', '--telegram-channel', 'a_fx', f'--output={folder}'], None, OUTPUT_REFUSAL),
			('collect', ['--telegram-channel', 'a_fx', '--output', folder], None, OUTPUT_REFUSAL),
			('download-media', ['download-media', '--output', folder], None, OUTPUT_REFUSAL),
			('download-media', ['download-media', '-o', folder, '--check'], None, OUTPUT_REFUSAL),
			('download-media', ['download-media', '-o', folder, '--dry-run'], None, OUTPUT_REFUSAL),
		]
		for command in ('build-dataset', 'build-network', 'build-urls'):
			forms.append(('builder', [command, '--data-path', folder], None, DATA_REFUSAL))
			forms.append(('builder', [command, '-d', folder], None, DATA_REFUSAL))
		for command, prog in (('build-dataset', 'build-datasets.py'), ('build-network', 'channels-to-network.py')):
			entry = lambda a, command=command, prog=prog: cli.builder_main(command, a, prog=prog)
			forms.append(('legacy builder', ['--data-path', folder], entry, DATA_REFUSAL))
		return forms

	def test_a_root_is_refused_before_any_handler_runs(self):
		for root in system_roots():
			for handler, argv, entry, refusal in self.forms(root):
				with self.subTest(argv=argv, handler=handler):
					code, err = self.run_cli(argv, entry)
					self.assertEqual(code, 2, err)
					self.assertIn(f'error: argument {argv_option(argv)}: {refusal}\n', err)
					self.assertEqual(self.calls, [])

	def test_a_subfolder_reaches_the_handler_as_given(self):
		folder = os.path.join(os.path.splitroot(os.getcwd())[0] + os.sep, 'data', 'telegram') + os.sep
		for handler, argv, entry, _ in self.forms(folder):
			with self.subTest(argv=argv, handler=handler):
				del self.calls[:]
				code, err = self.run_cli(argv, entry)
				self.assertEqual((code, err), (0, ''))
				self.assertEqual(len(self.calls), 1)
				name, args, _ = self.calls[0]
				self.assertEqual(name, handler)
				values = args[1] if 'builder' in name else args[0]
				key = 'data_path' if 'builder' in name else 'output'
				self.assertEqual(values[key], folder)


def argv_option(argv):
	'''
	The option as argparse names it in errors: --output/-o, --data-path/-d.
	'''
	if any(arg.startswith(('--data-path', '-d')) for arg in argv):
		return '--data-path/-d'
	return '--output/-o'


if __name__ == '__main__':
	unittest.main()
