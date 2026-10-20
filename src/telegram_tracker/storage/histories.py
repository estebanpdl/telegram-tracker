# -*- coding: utf-8 -*-
'''
Saved message histories for --update: the contract between the collector
and the run's store, and its file implementation.

A history source tells, for one target, whether a history is saved and
from which message ID to fetch, and merges the fetched messages into it.
`collect_channel` calls, for an --update target:

	before_requests(target) -> SavedHistory or None, before any request
		for the target
	after_resolve(target, channel_id, saved) -> SavedHistory or None,
		after the target resolved and before its first history request;
		`saved` is what before_requests returned
	merge(target, saved, fetched) -> (history, history_mode, messages)
		for a saved history: the payload the store writes (None when it
		writes the fetched messages itself, or nothing), 'merged' or
		'unchanged', and the history's message count after the merge

Both lookups raise HistoryUnreadable for a history that cannot be read
and HistoryRefused for one the run's profile may not update; either
fails only that target.

`FileHistories` reads <target>_messages.json and decides everything by
target name, before any request. The database source
(storage.sqlite.histories) decides by channel ID, after resolution.
'''

# import modules
import os

from dataclasses import dataclass

# import local submodules
from telegram_tracker.storage.files import history_path, merge_histories, newest_id, read_history
from telegram_tracker.storage.provenance import check_update


@dataclass(frozen=True)
class SavedHistory:
	'''
	A saved history as --update sees it: its highest message ID (0 when
	it holds none), its message count and, when looked up by it, its
	channel ID.
	'''
	newest_id: int
	messages: int
	channel_id: int | None = None


class FileHistories:
	'''
	Histories saved as <target>/<target>_messages.json. With a `profile`
	(RunProfile), --update into another profile's history is refused
	(see provenance.check_update); without one, nothing is checked.
	'''
	def __init__(self, output_folder, profile=None):
		self.output_folder = output_folder
		self.profile = profile

	def before_requests(self, target):
		path = history_path(self.output_folder, target)
		if not os.path.exists(path):
			return None
		saved = read_history(path)
		if self.profile is not None:
			check_update(self.output_folder, target, saved, self.profile)
		return SavedHistory(newest_id=newest_id(saved), messages=len(saved['messages']))

	def after_resolve(self, target, channel_id, saved):
		return saved

	def merge(self, target, saved, fetched):
		'''
		Read the history again and merge the fetched payload into it (see
		files.merge_histories). Nothing fetched leaves it unchanged.
		'''
		earlier = read_history(history_path(self.output_folder, target))
		merged = merge_histories(earlier, fetched)
		if merged is None:
			return None, 'unchanged', len(earlier['messages'])
		return merged, 'merged', len(merged['messages'])
