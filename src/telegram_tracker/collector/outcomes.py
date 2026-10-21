# -*- coding: utf-8 -*-
'''
How targets and runs end: the per-target and run-ending errors, the run's
CollectionSummary, and the stop reason recorded for a run that ends early.
'''

# import modules
import asyncio

from dataclasses import dataclass, field

# import local submodules
from telethon import errors

from telegram_tracker.request_policy import AccountStopped
from telegram_tracker.storage.records import StorageError


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


class SessionRejected(Exception):
	'''
	Telegram refused the session's authorization during collection.
	Ends the run.
	'''


# authorization/session errors that end the run
SESSION_ERRORS = (errors.UnauthorizedError, errors.AuthKeyError)


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
	replaced although they belonged to another profile.
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
