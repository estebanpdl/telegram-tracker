# -*- coding: utf-8 -*-
'''
Media ledger states, transition rules and value records.

The database operations live in ledger.py, which also exports these
names for callers using its public interface.
'''

from collections import namedtuple
from dataclasses import dataclass

from telegram_tracker.storage.records import StorageError


REQUESTED = 'requested'
DOWNLOADING = 'downloading'
COMPLETE = 'complete'
SKIPPED = 'skipped'
FAILED = 'failed'
STATES = (REQUESTED, DOWNLOADING, COMPLETE, SKIPPED, FAILED)

TRANSITIONS = {
	REQUESTED: frozenset({REQUESTED, DOWNLOADING, COMPLETE, SKIPPED, FAILED}),
	DOWNLOADING: frozenset({COMPLETE, SKIPPED, FAILED}),
	SKIPPED: frozenset({REQUESTED}),
	FAILED: frozenset({REQUESTED}),
	COMPLETE: frozenset()
}

SKIP_REASONS = ('over_file_limit', 'over_run_limit')
FAIL_REASONS = (
	'reference_expired', 'message_gone', 'media_changed', 'flood_stop', 'request_failed', 'disk_full',
	'storage_error', 'size_mismatch', 'path_conflict', 'stored_file_differs', 'interrupted'
)
INTERRUPTED_REASON = 'interrupted'

COMMANDS = ('collect', 'download-media')
RUNNING = 'running'
RUN_END_STATES = ('complete', 'partial', 'interrupted')
COUNTERS = ('app_requests', 'retries', 'flood_waits', 'refreshes', 'bytes_received')

ItemKey = namedtuple('ItemKey', 'channel_id message_id media_id')


class LedgerError(StorageError):
	'''
	A ledger write that the ledger's rules do not allow; nothing was
	changed.
	'''


class InvalidTransition(LedgerError):
	'''
	An item state change outside TRANSITIONS.
	'''


@dataclass(frozen=True)
class StartedRun:
	'''
	stale_runs -> run UUIDs found running and recorded as interrupted
	interrupted_items -> items found downloading and recorded as failed
	'''
	stale_runs: tuple
	interrupted_items: int


@dataclass(frozen=True)
class RequestCounts:
	'''
	How request() found each reference: new rows, rows requested again
	(requested, skipped or failed before), already complete, and rows
	this run is downloading.
	'''
	new: int = 0
	again: int = 0
	complete: int = 0
	downloading: int = 0


@dataclass(frozen=True)
class AttemptStats:
	bytes_received: int = 0
	app_requests: int = 0
	refreshed: bool = False
