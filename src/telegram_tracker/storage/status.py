# -*- coding: utf-8 -*-
'''
The collection-status.json sidecar: the run's state, parameters, and
per-target outcome while collecting, and the builders' gate that reads it.
'''

# import modules
import json
import os
import time

from datetime import datetime, timezone

# import local submodules
from telegram_tracker.storage.files import write_json_pretty
from telegram_tracker.storage.manifest import SQLITE, ManifestError, recorded_storage
from telegram_tracker.storage.records import INTERRUPTED, RUNNING, STATUS_FILE, STATUS_FORMAT
from telegram_tracker.utils import DataInputError


# names listed in builder notes before "and N more"
NOTE_LIST_LIMIT = 10

# wait_for_later_start: the clock is read every STEP seconds, for at most
# LIMIT seconds
START_WAIT_STEP = 0.05
START_WAIT_LIMIT = 1.0


def utc_now():
	return datetime.now(timezone.utc).isoformat(timespec='seconds')


def wait_for_later_start(output_folder):
	'''
	Called with the output folder locked, before a run records its
	started_at: wait until utc_now() is later than the started_at of the
	folder's last run, at most START_WAIT_LIMIT seconds.

	The files of a files or both folder name the run that wrote a history
	by its started_at, to the second, so two runs started in the same
	second would share a name. A sqlite folder names runs by run_uuid and
	never waits, nor does a folder without a readable sidecar. A clock set
	back by more than the limit is not waited for.
	'''
	try:
		status = read_status(output_folder)
	except DataInputError:
		return
	if not status or status.get('storage') == SQLITE:
		return
	previous = status.get('started_at')
	if not isinstance(previous, str):
		return
	for _ in range(round(START_WAIT_LIMIT / START_WAIT_STEP)):
		if utc_now() > previous:
			return
		time.sleep(START_WAIT_STEP)


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
	Builder gate for the file outputs; see collection_status. Returns
	the notes.
	'''
	return collection_status(data_path, messages=messages)[1]


def collection_status(
		data_path, *, messages=True, required=False, histories='message files', interrupted_ok=False):
	'''
	Builder gate. Raises DataInputError when the folder's storage
	manifest cannot be used, or the last collection into `data_path` is
	running or was interrupted. Returns (sidecar or None, notes): notes
	(strings) for a finalized partial run and, when `messages`, for a run
	whose `histories` hold a subset (history window or --update).

	Folders without a sidecar (earlier versions) pass with no notes,
	unless `required`: a reader of the database never infers a finished
	collection from a missing sidecar.

	`interrupted_ok`: an interrupted collection passes with a note, for
	readers of the saved histories alone (each history is saved whole),
	which do not depend on the aggregate files. A running one is still
	refused, and the remedy named is a collection into the same folder.
	'''
	try:
		recorded_storage(data_path)
	except ManifestError as exc:
		raise DataInputError(str(exc)) from None

	path = os.path.join(data_path, STATUS_FILE)
	status = read_status(data_path)
	if status is None:
		if required:
			raise DataInputError(
				f'{path} is missing, so it is unknown whether the last collection into '
				f'{data_path} finished. Keep this folder for inspection and rerun collection '
				'into a fresh --output.'
			)
		return None, []

	state = status['state']
	recovery = (
		'Keep this folder for inspection and rerun collection into a fresh '
		'--output; completed channel files stay readable.'
	)
	if state == RUNNING and interrupted_ok:
		# a stopped run's saved histories are whole, but only a collection
		# into this folder can tell a stopped run from one still running
		raise DataInputError(
			f'{path} says a collection is still running. Try again after it finishes. If none is '
			'running, it stopped without finishing: run the collection into this folder again first.'
		)
	if state == RUNNING:
		raise DataInputError(
			f'{path} says a collection is still running. Build after it '
			f'finishes. If none is running, it stopped without finishing. {recovery}'
		)
	notes = []
	if state == INTERRUPTED:
		reason = status.get('stop_reason') or 'unknown reason'
		if not interrupted_ok:
			raise DataInputError(
				f'{path}: the last collection was interrupted ({reason}), so its '
				f'aggregate files may be incomplete. {recovery}'
			)
		notes.append(
			f'the last collection was interrupted ({reason}); the {histories} it saved are read as they are'
		)
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
			f'({parameters.get("window_description")}); its {histories} hold '
			'only that selection'
		)
	if messages and parameters.get('mode') == 'update':
		notes.append(
			'the last collection ran with --update; rebuilding appends its '
			'merged histories to msgs_dataset.csv again, duplicating earlier rows'
		)
	return status, notes
