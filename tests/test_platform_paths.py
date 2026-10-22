# -*- coding: utf-8 -*-
'''
Path forms that differ by platform: spaces, non-ASCII names, relative and
absolute spellings of one output folder, and the case and Unicode
normalization forms that a case- or normalization-insensitive file system
treats as the same folder.

Windows and macOS file systems usually ignore case. On Windows normcase
folds it and realpath returns the stored spelling; on macOS neither
happens, so is_within and same_folder fall back to file identity. The
"identity only" cases disable both text normalizations to exercise that
fallback where the native run would not reach it. Cases that need a
case-insensitive or normalization-insensitive file system are skipped
where the temporary directory's file system is not one.
'''

# import modules
import contextlib
import os
import tempfile
import unicodedata

from unittest import mock

import offline_guard
import storage_modes_support

from telegram_tracker.credentials import home as homes
from telegram_tracker.storage import destination as destinations
from telegram_tracker.storage.sqlite.schema import DB_FILENAME


NAME = 'Données данные run'


def ignores_case(folder):
	probe = os.path.join(folder, 'Case-Probe')
	os.mkdir(probe)
	try:
		return os.path.isdir(os.path.join(folder, 'cASE-pROBE'))
	finally:
		os.rmdir(probe)


def ignores_normalization(folder):
	probe = os.path.join(folder, unicodedata.normalize('NFC', 'café-probe'))
	os.mkdir(probe)
	try:
		return os.path.isdir(os.path.join(folder, unicodedata.normalize('NFD', 'café-probe')))
	finally:
		os.rmdir(probe)


@contextlib.contextmanager
def identity_only():
	'''
	Text comparison without normcase folding or realpath canonicalization:
	what is_within and same_folder see on macOS for a differently spelled
	existing folder.
	'''
	with mock.patch.object(os.path, 'normcase', lambda path: path), \
			mock.patch.object(os.path, 'realpath', os.path.abspath):
		yield


class ContainmentTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.folder = os.path.join(self.tmp, 'Output Folder')
		os.mkdir(self.folder)
		os.mkdir(os.path.join(self.tmp, 'Output Folder2'))

	def require_case_insensitive(self):
		if not ignores_case(self.tmp):
			self.skipTest('case-sensitive file system: another case is another folder')

	def test_other_case_is_the_same_folder(self):
		self.require_case_insensitive()
		other = self.folder.swapcase()
		inside = os.path.join(other, 'sub', 'session_file.session')
		for label, context in (('native', contextlib.nullcontext()), ('identity only', identity_only())):
			with self.subTest(label), context:
				if label == 'identity only':
					# the text alone no longer matches
					self.assertNotEqual(os.path.normcase(os.path.realpath(other)), self.folder)
				self.assertTrue(homes.is_within(inside, self.folder))
				self.assertTrue(homes.is_within(self.folder, other))
				self.assertTrue(destinations.same_folder(other, self.folder))

	def test_other_folders_stay_outside(self):
		for label, context in (('native', contextlib.nullcontext()), ('identity only', identity_only())):
			with self.subTest(label), context:
				sibling = os.path.join(self.tmp, 'Output Folder2', 'session_file.session')
				self.assertFalse(homes.is_within(sibling, self.folder))
				self.assertFalse(homes.is_within(self.tmp, self.folder))
				self.assertFalse(homes.is_within(os.path.join(self.tmp, 'missing', 'x'), self.folder))
				self.assertFalse(homes.is_within(self.folder, os.path.join(self.tmp, 'missing')))
				self.assertFalse(destinations.same_folder(os.path.join(self.tmp, 'Output Folder2'), self.folder))
				self.assertFalse(destinations.same_folder(os.path.join(self.tmp, 'missing'), self.folder))

	def test_other_unicode_form_is_the_same_folder(self):
		if not ignores_normalization(self.tmp):
			self.skipTest('the file system keeps Unicode normalization forms apart')
		nfc = os.path.join(self.tmp, unicodedata.normalize('NFC', NAME))
		os.mkdir(nfc)
		nfd = os.path.join(self.tmp, unicodedata.normalize('NFD', NAME))
		self.assertNotEqual(nfc, nfd)
		self.assertTrue(homes.is_within(os.path.join(nfd, 'session_file.session'), nfc))
		self.assertTrue(destinations.same_folder(nfd, nfc))


class OutputSpellingTests(storage_modes_support.ModesCase):
	'''
	One output folder named with spaces and non-ASCII letters, reached by a
	relative path, its absolute path, and (where the file system ignores
	case) another case, collects into the same database; a session inside
	it under another spelling is refused.
	'''

	def setUp(self):
		super().setUp()
		cwd = os.getcwd()
		os.chdir(self.tmp)
		self.addCleanup(os.chdir, cwd)
		self.out = os.path.join(self.tmp, NAME)

	def test_relative_absolute_and_case_spellings_reach_one_database(self):
		spellings = [NAME, self.out]
		if ignores_case(self.tmp):
			spellings.append(self.out.swapcase())
		for index, spelling in enumerate(spellings):
			with self.subTest(spelling=spelling):
				code, _, err = self.collect(out=spelling, storage='sqlite' if index == 0 else None)
				self.assertEqual((code, err), (0, ''))
		self.assertEqual(os.listdir(self.tmp), [NAME])
		self.assertEqual(
			self.rows('SELECT count(*), min(state), max(state) FROM collection_runs'),
			[(len(spellings), 'complete', 'complete')]
		)
		binding = self.rows('SELECT output_binding FROM schema_meta')[0][0]
		self.assertEqual(binding, os.path.join(self.tmp, NAME))
		self.assertTrue(os.path.isfile(os.path.join(self.tmp, NAME, DB_FILENAME)))

	def test_a_session_inside_the_output_under_another_spelling_is_refused(self):
		os.mkdir(self.out)
		spellings = [os.path.join(NAME, 'session_file.session')]
		if ignores_case(self.tmp):
			spellings.append(os.path.join(self.out.swapcase(), 'session_file.session'))
		for spelling in spellings:
			with self.subTest(spelling=spelling):
				code, _, err = self.collect('--session', spelling)
				self.assertEqual(code, 2)
				self.assertIn('the session file must not be inside the --output folder', err)
				self.assertEqual(os.listdir(self.out), [])
				self.assertEqual(self.factory_calls, 0)

	def test_a_session_folder_with_spaces_and_non_ascii_letters(self):
		folder = os.path.join(self.tmp, 'Séssion folder')
		os.mkdir(folder)
		code, _, err = self.collect('--session', os.path.join(folder, 'my account'))
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(os.listdir(folder), [])
		self.assertEqual(self.status()['state'], 'complete')
