# -*- coding: utf-8 -*-
'''
Leftover partial files: removal of the generated .part files of earlier
runs the ledger knows, and what is kept and reported instead: the
current run's folder, other names, folders, links, unknown runs, and
everything when removal is turned off.
'''

# import modules
import os
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import CHANNEL, RUN_A, RUN_B, RUN_C, SELECTION, SHA_A, MediaTestCase
from telegram_tracker.media import partials
from telegram_tracker.media.layout import partial_name, relative_path
from telegram_tracker.media.partials import PartialReport, clean_partials


def touch(path, data=b'partial'):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, 'wb') as writer:
		writer.write(data)


class PartialTestCase(MediaTestCase):

	def setUp(self):
		super().setUp()
		self.ledger = self.files_ledger()
		self.ledger.start_run(RUN_A, 'collect', selection=SELECTION)
		self.ledger.finish_run(RUN_A, 'interrupted')
		# RUN_B's process ended without finishing; RUN_C is the current run
		self.ledger.start_run(RUN_B, 'collect', selection=SELECTION)
		self.ledger.start_run(RUN_C, 'collect', selection=SELECTION)

	def part(self, run, message_id=1, media_id=5):
		path = os.path.join(self.layout.partial_root, run, partial_name(CHANNEL, message_id, media_id))
		touch(path)
		return path

	def clean(self, **options):
		return clean_partials(self.layout, self.ledger, RUN_C, **options)


class RemovalTests(PartialTestCase):

	def test_nothing_to_clean(self):
		self.assertEqual(self.clean(), PartialReport())

	def test_leftovers_of_known_runs_are_removed(self):
		a1, a2 = self.part(RUN_A, 1, 5), self.part(RUN_A, 2, -6)
		b1 = self.part(RUN_B)
		current = self.part(RUN_C)
		final = self.layout.final_path(relative_path(CHANNEL, 'image', 9, SHA_A, 'jpg'))
		touch(final, b'complete')
		report = self.clean()
		self.assertEqual(sorted(report.removed), sorted([a1, a2, b1]))
		self.assertEqual((report.kept, report.unknown), ((), ()))
		self.assertEqual(os.listdir(self.layout.partial_root), [RUN_C])
		self.assertTrue(os.path.isfile(current))
		self.assertTrue(os.path.isfile(final))

	def test_other_entries_are_kept(self):
		removed = self.part(RUN_A)
		strangers = [
			os.path.join(self.layout.partial_root, RUN_A, 'notes.txt'),
			os.path.join(self.layout.partial_root, RUN_A, f'{CHANNEL}-1-5.part.bak'),
			os.path.join(self.layout.partial_root, RUN_A, '0-1-5.part')
		]
		for path in strangers:
			touch(path)
		folder = os.path.join(self.layout.partial_root, RUN_A, f'{CHANNEL}-2-5.part')
		os.mkdir(folder)
		report = self.clean()
		self.assertEqual(report.removed, (removed,))
		self.assertEqual(sorted(report.kept), sorted(strangers + [folder]))
		self.assertTrue(all(os.path.exists(p) for p in strangers + [folder]))

	def test_links_are_never_followed(self):
		target = os.path.join(self.tmp, 'elsewhere')
		os.mkdir(target)
		touch(os.path.join(target, partial_name(CHANNEL, 1, 5)))
		os.makedirs(self.layout.partial_root, exist_ok=True)
		folder_link = os.path.join(self.layout.partial_root, RUN_A)
		self.link(target, folder_link)
		entry_link = os.path.join(self.layout.partial_root, RUN_B, partial_name(CHANNEL, 2, 5))
		os.makedirs(os.path.dirname(entry_link))
		self.link(target, entry_link)
		report = self.clean()
		self.assertEqual(report.removed, ())
		self.assertEqual(sorted(report.kept), sorted([folder_link, entry_link]))
		self.assertEqual(os.listdir(target), [partial_name(CHANNEL, 1, 5)])

	def test_a_linked_partial_root_is_reported_and_left(self):
		target = os.path.join(self.tmp, 'elsewhere')
		os.makedirs(os.path.join(target, RUN_A))
		touch(os.path.join(target, RUN_A, partial_name(CHANNEL, 1, 5)))
		self.link(target, self.layout.partial_root)
		self.assertEqual(self.clean(), PartialReport(kept=(self.layout.partial_root,)))
		self.assertEqual(os.listdir(os.path.join(target, RUN_A)), [partial_name(CHANNEL, 1, 5)])

	def test_unknown_runs_are_reported_only(self):
		stranger = '44444444-4444-4444-8444-444444444444'
		path = self.part(stranger)
		odd = os.path.join(self.layout.partial_root, 'not-a-run')
		os.mkdir(odd)
		report = self.clean()
		self.assertEqual(sorted(report.unknown), sorted([os.path.dirname(path), odd]))
		self.assertTrue(os.path.isfile(path))

	def test_a_file_that_cannot_be_removed_is_kept(self):
		path = self.part(RUN_A)
		with mock.patch.object(partials.os, 'remove', side_effect=PermissionError(13, 'in use')):
			report = self.clean()
		self.assertEqual((report.removed, report.kept), ((), (path,)))
		self.assertTrue(os.path.isfile(path))


class ReportOnlyTests(PartialTestCase):

	def test_nothing_is_removed(self):
		paths = [self.part(RUN_A), self.part(RUN_B)]
		report = self.clean(remove=False)
		self.assertEqual(report.removed, ())
		self.assertEqual(sorted(report.kept), sorted(paths))
		self.assertTrue(all(os.path.isfile(p) for p in paths))

	def test_an_empty_leftover_folder_is_reported(self):
		folder = os.path.join(self.layout.partial_root, RUN_A)
		os.makedirs(folder)
		self.assertEqual(self.clean(remove=False), PartialReport(kept=(folder,)))
		self.assertTrue(os.path.isdir(folder))
		self.assertEqual(self.clean(), PartialReport())
		self.assertFalse(os.path.exists(folder))


if __name__ == '__main__':
	unittest.main()
