# -*- coding: utf-8 -*-
'''
The collection-status.json sidecar: the run's state, parameters, and
per-target outcome while collecting, and the builders' gate that reads it.
'''

# import modules
import json
import os

from datetime import datetime, timezone

# import local submodules
from telegram_tracker.storage.files import write_json_pretty
from telegram_tracker.storage.records import INTERRUPTED, RUNNING, STATUS_FILE, STATUS_FORMAT
from telegram_tracker.utils import DataInputError


# names listed in builder notes before "and N more"
NOTE_LIST_LIMIT = 10


def utc_now():
	return datetime.now(timezone.utc).isoformat(timespec='seconds')


class StatusFile:
	'''
	collection-status.json: the run's state, parameters, and per-target
	outcome, without credentials or message text.

	States: running (from the start until finalization completes),
	complete (every target collected), partial (finalized, some targets
	failed), interrupted (stopped before every target was attempted:
	cancellation, account stop, or a storage error).

	`profile` (a RunProfile) gives profile_name and profile_id, null when
	unknown; `account_checks` counts the requests made to identify the
	account before collecting, which are not part of `requests`. Sidecars
	of earlier versions have none of these keys: their profile is unknown.
	'''
	def __init__(self, output_folder, targets, parameters, profile=None, account_checks=0):
		self.path = os.path.join(output_folder, STATUS_FILE)
		self.data = {
			'format': STATUS_FORMAT,
			'state': RUNNING,
			'started_at': utc_now(),
			'finished_at': None,
			'profile_name': profile.name if profile else None,
			'profile_id': profile.id if profile else None,
			'account_checks': {'get_me': account_checks},
			'parameters': parameters,
			'summary': None,
			'stop_reason': None,
			'targets': [
				{'input': t, 'status': 'pending'} for t in targets
			],
			'requests': {}
		}

	def target(self, index):
		return self.data['targets'][index]

	def write(self):
		write_json_pretty(self.path, self.data)

	def finish(self, state, *, summary, stop_reason=None, requests=None):
		self.data.update(
			state=state,
			finished_at=utc_now(),
			summary=summary,
			stop_reason=stop_reason,
			requests=requests or {}
		)
		self.write()


def read_status(data_path):
	'''
	The sidecar of `data_path`, or None when there is none. Raises
	DataInputError when it exists but cannot be read.
	'''
	path = os.path.join(data_path, STATUS_FILE)
	if not os.path.isfile(path):
		return None
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise DataInputError(f'{path} cannot be read: {exc}') from None
	if not isinstance(data, dict) or 'state' not in data:
		raise DataInputError(f'{path} is not a collection status file')
	return data


def check_collection_status(data_path, *, messages=True):
	'''
	Builder gate. Raises DataInputError when the last collection into
	`data_path` is running or was interrupted; returns notes (strings)
	for a finalized partial run and, when `messages`, for a run whose
	message files hold a subset (history window or --update). Folders
	without a sidecar (earlier versions) pass with no notes.
	'''
	status = read_status(data_path)
	if status is None:
		return []

	path = os.path.join(data_path, STATUS_FILE)
	state = status['state']
	recovery = (
		'Keep this folder for inspection and rerun collection into a fresh '
		'--output; completed channel files stay readable.'
	)
	if state == RUNNING:
		raise DataInputError(
			f'{path} says a collection is still running. Build after it '
			f'finishes. If none is running, it stopped without finishing. {recovery}'
		)
	if state == INTERRUPTED:
		reason = status.get('stop_reason') or 'unknown reason'
		raise DataInputError(
			f'{path}: the last collection was interrupted ({reason}), so its '
			f'aggregate files may be incomplete. {recovery}'
		)

	notes = []
	failed = [
		t['input'] for t in status.get('targets', [])
		if t.get('status') not in ('succeeded', 'duplicate')
	]
	if failed:
		names = ', '.join(failed[:NOTE_LIST_LIMIT])
		if len(failed) > NOTE_LIST_LIMIT:
			names += f', and {len(failed) - NOTE_LIST_LIMIT} more'
		notes.append(
			f'the last collection finished with {len(failed)} failed target(s), '
			f'excluded from its output: {names}'
		)

	parameters = status.get('parameters') or {}
	if messages and parameters.get('mode') == 'window':
		notes.append(
			'the last collection used a history window '
			f'({parameters.get("window_description")}); its message files hold '
			'only that selection'
		)
	if messages and parameters.get('mode') == 'update':
		notes.append(
			'the last collection ran with --update; rebuilding appends its '
			'merged histories to msgs_dataset.csv again, duplicating earlier rows'
		)
	return notes
