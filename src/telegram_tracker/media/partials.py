# -*- coding: utf-8 -*-
'''
Leftover partial downloads of earlier runs.

A run downloads into downloaded-media/.partial/<run_uuid>/ and removes
its own files when it stops; a run that was killed leaves them. With the
output folder locked, so that no other run is live, the folders of every
other run the ledger knows are leftovers. clean_partials removes the
generated `.part` files in them, then each folder once it is empty.
Anything else found there (another name, a folder, a link) is kept and
reported, and links are never followed. Folders named after runs the
ledger does not know are reported, never removed. Complete files live
outside .partial and are never touched.

With remove=False nothing is removed: every leftover is reported.
'''

# import modules
import os
import stat

from dataclasses import dataclass

# import local submodules
from telegram_tracker.media.layout import PARTIAL_NAME, is_link, lstat_or_none


@dataclass(frozen=True)
class PartialReport:
	'''
	removed -> partial files removed
	kept -> leftovers left in place (not ours, links, or not removable)
	unknown -> .partial entries that are not the folder of a known run
	'''
	removed: tuple = ()
	kept: tuple = ()
	unknown: tuple = ()


def is_plain_folder(info):
	return info is not None and not is_link(info) and stat.S_ISDIR(info.st_mode)


def clean_partials(layout, ledger, current_run_uuid=None, *, remove=True):
	'''
	Remove (or, without `remove`, report) the partial files of every run
	in `ledger` except `current_run_uuid`. Call it with the output folder
	locked. Returns a PartialReport.
	'''
	root = layout.partial_root
	info = lstat_or_none(root)
	if info is None:
		return PartialReport()
	if not is_plain_folder(info):
		return PartialReport(kept=(root,))

	known = {run['run_uuid'] for run in ledger.runs()} - {current_run_uuid}
	removed, kept, unknown = [], [], []
	for name in sorted(os.listdir(root)):
		folder = os.path.join(root, name)
		if name == current_run_uuid:
			continue
		if name not in known:
			unknown.append(folder)
			continue
		if not is_plain_folder(lstat_or_none(folder)):
			kept.append(folder)
			continue
		left = False
		for entry in sorted(os.listdir(folder)):
			path = os.path.join(folder, entry)
			entry_info = lstat_or_none(path)
			ours = (
				entry_info is not None and not is_link(entry_info) and stat.S_ISREG(entry_info.st_mode)
				and PARTIAL_NAME.match(entry) is not None
			)
			if remove and ours:
				try:
					os.remove(path)
					removed.append(path)
					continue
				except OSError:
					pass
			kept.append(path)
			left = True
		if remove and not left:
			try:
				os.rmdir(folder)
			except OSError:
				kept.append(folder)
		elif not remove and not left:
			kept.append(folder)
	return PartialReport(tuple(removed), tuple(kept), tuple(unknown))
