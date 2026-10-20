# -*- coding: utf-8 -*-
'''
Which profile collected each saved message history: the run's profile,
the per-channel <channel>_provenance.json file, and the rules for
--update and for replacing another profile's history.

	{"format": 1, "channel_id": 102, "profile_name": "alice",
	 "profile_id": "<uuid or null>", "history": "replaced",
	 "run_started_at": "...", "message_count": 9, "newest_id": 12,
	 "takeover": false, "previous_profile_name": null,
	 "previous_profile_id": null}

The file is written after <channel>_messages.json by every run that
writes that history. A full or window run (and --update creating a new
history) records its own profile. --update keeps the recorded profile
and refreshes only `history`, `run_started_at`, `message_count` and
`newest_id`; it never creates the file for a history saved without one,
whose profile stays unknown.

--update merges into a history only when it is the same profile's:
	- the record has a profile_id: the run's must be the same;
	- it has only a profile_name: the names must be the same;
	- no record, or one with neither: only the default profile may merge.
A refused channel fails before any request for it; the other channels go
on. A record whose message count or newest ID does not match the saved
history (a run stopped between the two files) refuses every --update.

A full or window run replaces any history. When the previous record
would have refused this run's --update, the new record has
`takeover: true` and the previous profile, and so does the run's target
entry in collection-status.json.

Two profile names for one Telegram account are still treated as
different. A profile name alone cannot tell apart accounts in different
application homes or on different computers.
'''

# import modules
import json
import os

from dataclasses import dataclass

# import local submodules
from telegram_tracker.storage.files import newest_id, write_json_pretty
from telegram_tracker.storage.records import HistoryUnreadable


PROVENANCE_FORMAT = 1
DEFAULT_PROFILE = 'default'


class HistoryRefused(Exception):
	'''
	--update refused for one channel: its saved history is another
	profile's, or its provenance record does not match it.
	'''


@dataclass(frozen=True)
class RunProfile:
	'''
	The collecting profile as recorded in outputs: its local name and
	opaque ID, either of which may be None (unknown). Never an account
	ID, phone number, or credential.
	'''
	name: str | None = None
	id: str | None = None

	def record(self):
		return {'profile_name': self.name, 'profile_id': self.id}

	def describe(self):
		if self.name is None:
			return 'no profile'
		return self.name + ('' if self.id else ' (no profile ID)')


def provenance_path(output_folder, target):
	return f'{output_folder}/{target}/{target}_provenance.json'


def read_provenance(path):
	'''
	The provenance record at `path`, or None when there is none. Raises
	HistoryUnreadable when it cannot be read or is malformed.
	'''
	if not os.path.lexists(path):
		return None
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise HistoryUnreadable(f'{path} cannot be read: {exc}') from None
	text_or_null = (lambda v: v is None or isinstance(v, str))
	if (
			not isinstance(data, dict)
			or not text_or_null(data.get('profile_name'))
			or not text_or_null(data.get('profile_id'))
			or not all(type(data.get(k)) is int for k in ('message_count', 'newest_id'))):
		raise HistoryUnreadable(f'{path} is not a history provenance record')
	return data


def refusal(previous, run):
	'''
	Why `run` (a RunProfile) may not --update a history whose record is
	`previous` (None when it has none), or None when it may.
	'''
	if previous is not None and previous.get('profile_id'):
		if run.id == previous['profile_id']:
			return None
		return (
			f'its saved history belongs to profile {previous.get("profile_name") or "unknown"}'
			+ ('' if run.name != previous.get('profile_name') else ' with another profile ID')
		)
	if previous is not None and previous.get('profile_name'):
		if run.name == previous['profile_name']:
			return None
		return f'its saved history belongs to profile {previous["profile_name"]}'
	if run.name in (None, DEFAULT_PROFILE):
		return None
	return 'its saved history has no profile record; only the default profile can update it'


def fingerprint(history):
	return {'message_count': len(history['messages']), 'newest_id': newest_id(history)}


def check_update(output_folder, target, history, run):
	'''
	Raise HistoryRefused unless `run` may merge into `history`, the saved
	history of `target`.
	'''
	path = provenance_path(output_folder, target)
	try:
		previous = read_provenance(path)
	except HistoryUnreadable as exc:
		raise HistoryRefused(f'--update refused: {exc}') from None
	if previous is not None and {
			k: previous[k] for k in ('message_count', 'newest_id')} != fingerprint(history):
		raise HistoryRefused(
			f'--update refused: {path} does not match the saved history (a run may '
			'have stopped while writing them); collect the channel without --update'
		)
	reason = refusal(previous, run)
	if reason is not None:
		raise HistoryRefused(
			f'--update refused: {reason}. Collect it without --update (this replaces '
			'the history and records the takeover) or into a fresh --output'
		)


def write_provenance(output_folder, result, run, started_at, had_history):
	'''
	Write the record for a ChannelResult whose history was just written;
	`had_history` tells whether a saved history existed before it.
	Returns the takeover entry ({'takeover': True, 'previous_profile_name':
	..., 'previous_profile_id': ...}) or None.
	'''
	record, takeover = provenance_record(output_folder, result, run, started_at, had_history)
	if record is not None:
		write_json_pretty(provenance_path(output_folder, result.target), record)
	return takeover


def provenance_record(output_folder, result, run, started_at, had_history):
	'''
	The record write_provenance writes (None: nothing to write) and the
	takeover entry, computed without writing.
	'''
	path = provenance_path(output_folder, result.target)
	mode = result.history_mode
	if mode == 'merged':
		try:
			record = read_provenance(path)
		except HistoryUnreadable:
			record = None
		if record is None:
			return None, None
		record.update(history=mode, run_started_at=started_at, **fingerprint(result.history))
		return record, None

	takeover = None
	if mode == 'replaced' and had_history:
		try:
			previous = read_provenance(path)
			refused = refusal(previous, run) is not None
		except HistoryUnreadable:
			previous, refused = None, True
		if refused:
			takeover = {
				'takeover': True,
				'previous_profile_name': (previous or {}).get('profile_name'),
				'previous_profile_id': (previous or {}).get('profile_id')
			}
	return {
		'format': PROVENANCE_FORMAT,
		'channel_id': result.channel_id,
		**run.record(),
		'history': mode,
		'run_started_at': started_at,
		**fingerprint(result.history),
		'takeover': takeover is not None,
		'previous_profile_name': takeover and takeover['previous_profile_name'],
		'previous_profile_id': takeover and takeover['previous_profile_id']
	}, takeover
