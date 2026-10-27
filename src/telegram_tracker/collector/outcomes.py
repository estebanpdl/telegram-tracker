# -*- coding: utf-8 -*-
'''
Run records: collection parameters, per-target and run-ending errors,
CollectionSummary, and the stop reason recorded for a run that ends early.
'''

# import modules
import asyncio

from dataclasses import dataclass, field

# import local submodules
# the run-ending session outcome lives with the request policy, which the
# media downloads use too; it is part of this module's interface
from telegram_tracker.request_policy import SESSION_ERRORS, AccountStopped, SessionRejected, \
	session_rejected  # noqa: F401
from telegram_tracker.storage.records import StorageError


def _status_fields(writer):
	'''
	The writer's sidecar keys; none for a writer without status_fields.
	'''
	fields = getattr(writer, 'status_fields', None)
	return fields() if fields is not None else {}


def run_parameters(metadata_only, window, update, controls=None):
	'''
	The run's collection parameters for the sidecar (no credentials).
	'''
	if metadata_only:
		mode = 'metadata_only'
	elif update:
		mode = 'update'
	elif window.is_filtered:
		mode = 'window'
	else:
		mode = 'full'

	parameters = {
		'mode': mode,
		'metadata_only': metadata_only,
		'window': window.as_record() if mode == 'window' else None,
		'window_description': window.describe() if mode == 'window' else None
	}
	parameters.update(controls or {})
	return parameters


class PaginationStalled(Exception):
	'''
	History pagination stopped making progress (offset did not decrease).
	'''


class TargetUnavailable(Exception):
	'''
	A requested target does not exist or is not accessible.
	'''


class DuplicateTarget(Exception):
	'''
	A target resolved to the same channel as an earlier target.
	'''
	def __init__(self, alias_of):
		super().__init__(alias_of)
		self.alias_of = alias_of




@dataclass
class CollectionSummary:
	'''
	Outcome of one run. `failed` holds (target, reason) pairs; `cancelled`
	lists targets not finished when the run stopped; `collected_in_full`
	lists --update targets that had no saved history; `saved_messages`
	holds (target, history message count, messages added by --update or
	None) for each succeeded target with messages. `profile` is the
	run's RunProfile (None when the caller gave none); `takeovers` holds
	(target, previous profile name or None) for histories this run
	replaced although they belonged to another profile. `media` is the
	sidecar's media block of a run with media downloads, else None.
	'''
	requested: list
	output_folder: str
	succeeded: list = field(default_factory=list)
	failed: list = field(default_factory=list)
	cancelled: list = field(default_factory=list)
	duplicates: list = field(default_factory=list)
	collected_in_full: list = field(default_factory=list)
	saved_messages: list = field(default_factory=list)
	stale_histories: list = field(default_factory=list)
	stop_reason: str | None = None
	concurrency: int = 1
	requests: dict = field(default_factory=dict)
	peak_in_flight: int = 0
	peak_queued_results: int = 0
	profile: object = None
	takeovers: list = field(default_factory=list)
	media: dict | None = None


def stop_reason_for(exc):
	if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
		return 'cancelled'
	if isinstance(exc, AccountStopped):
		return f'account stopped: {exc}'
	if isinstance(exc, SessionRejected):
		return f'session rejected: {exc}'
	if isinstance(exc, StorageError):
		return f'storage error: {exc}'
	return f'{type(exc).__name__}: {exc}'
