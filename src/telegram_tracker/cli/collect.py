# -*- coding: utf-8 -*-
'''
`collect`: validation of the history window, --update, and request
controls, then one collection run and its summary. Telethon, the
collector, and credentials are loaded only after the arguments are valid.
The session and app credentials are resolved as for `auth` (see
telegram_tracker.credentials), and the session lease is held from before
the client exists until the run ends.
'''

# import modules
import functools
import os
import sys
import time
import warnings

# import local submodules
from telegram_tracker.cli.builders import missing_dependency
from telegram_tracker.cli.parsers import COLLECT, DEFAULT_OUTPUT
from telegram_tracker.cli.targets import read_targets
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.credentials.store import StoreError, prepare_home
from telegram_tracker.utils import data_folder_from


# names listed by the existing-history warning before "and N more"
WARN_LIST_LIMIT = 10

# Default channel workers for --batch-file; a single channel runs at 1
BATCH_CONCURRENCY = 3


def window_bounds(args, parser):
	'''
	History window values from parsed arguments, validated. Returns a
	dict of HistoryWindow fields that differ from the default.
	'''
	bounds = {
		'min_id': args.get('min_id') or 0,
		'max_id': args.get('max_id') or 0,
		'since': args.get('since'),
		'until': args.get('until'),
		'max_messages': args.get('max_messages')
	}
	bounds = {k: v for k, v in bounds.items() if v}

	if 'max_id' in bounds and bounds['max_id'] <= bounds.get('min_id', 0) + 1:
		parser.error(
			f'--max-id {bounds["max_id"]} leaves no ids above --min-id '
			f'{bounds.get("min_id", 0)} (both bounds are exclusive)'
		)
	if 'since' in bounds and 'until' in bounds and bounds['since'] >= bounds['until']:
		parser.error('--since must be earlier than --until')

	return bounds


def warn_replaced_histories(targets, output_folder):
	'''
	Before a filtered run: name the channels whose saved message history
	will be replaced by the selection (stderr).
	'''
	existing = [
		t for t in dict.fromkeys(targets)
		if t and os.path.isfile(os.path.join(output_folder, t, f'{t}_messages.json'))
	]
	if not existing:
		return

	names = ', '.join(existing[:WARN_LIST_LIMIT])
	if len(existing) > WARN_LIST_LIMIT:
		names += f', and {len(existing) - WARN_LIST_LIMIT} more'
	print(
		f'> Warning: this filtered run replaces the saved messages of '
		f'{len(existing)} channel(s) in {output_folder}: {names}. '
		'Use a fresh --output to keep them.',
		file=sys.stderr
	)


def run_collect(args, parser, *, legacy, client_factory=None, authorize=None):
	'''
	Validate, then run one collection. See the cli package docstring for
	exit codes.
	'''
	metadata_only = args['limit_download_to_channel_metadata']
	bounds = window_bounds(args, parser)
	update = args.get('update', False)
	if update and bounds:
		parser.error(
			'--update cannot be combined with the history window options '
			'(--since, --until, --min-id, --max-id, --max-messages): it sets '
			'min_id from each saved history'
		)
	if update and metadata_only:
		parser.error(
			'--update needs message collection; remove '
			'--limit-download-to-channel-metadata'
		)
	if not legacy and metadata_only and bounds:
		parser.error(
			'history window options (--since, --until, --min-id, --max-id, '
			'--max-messages) need message collection; remove '
			'--limit-download-to-channel-metadata'
		)

	targets, given = read_targets(args, parser)
	output_folder = data_folder_from(args['output'], DEFAULT_OUTPUT)
	if bounds and not metadata_only:
		warn_replaced_histories(targets, output_folder)

	batch = args['batch_file'] is not None
	requested = args.get('concurrency') or (BATCH_CONCURRENCY if batch else 1)
	controls = {
		'concurrency': requested if batch else 1,
		'requested_concurrency': requested,
		'request_interval': args.get('request_interval', 0.25),
		'max_flood_wait': args.get('max_flood_wait', 60),
		'max_retries': args.get('max_retries', 3),
		'targets_given': given
	}

	try:
		from telegram_tracker.api.auth import AuthenticationError, ensure_authorized
		from telegram_tracker.collector import HistoryWindow, run_collection
		from telegram_tracker.storage import OutputLocked, StorageError
	except ModuleNotFoundError as exc:
		return missing_dependency(COLLECT, exc)

	window = HistoryWindow(**bounds)

	try:
		home, _ = homes.app_home()
		session = homes.select_session(args.get('session'))
		if homes.is_within(session.path, output_folder):
			parser.error('the session file must not be inside the --output folder')
		if homes.is_within(output_folder, home) or homes.is_within(home, output_folder):
			parser.error(f'the --output folder must be outside the application home ({home})')
		phone = None
		if client_factory is None:
			from telegram_tracker.api import create_client

			resolution = sources.resolve(config=args.get('config'), home=home)
			if resolution.missing:
				return missing_credentials(resolution)
			phone = resolution.values['phone']
			client_factory = functools.partial(
				create_client,
				session.path,
				resolution.values['api_id'],
				resolution.values['api_hash']
			)
	except (homes.HomeError, sources.CredentialError) as exc:
		print(f'> {exc}', file=sys.stderr)
		return 2

	if session.notice:
		print(session.notice, file=sys.stderr)
	if session.in_home and not os.path.isfile(session.path):
		try:
			prepare_home(home)
		except StoreError as exc:
			print(
				f'> Cannot create the session in {home}: {exc}. Set '
				'TELEGRAM_TRACKER_HOME or pass --session.',
				file=sys.stderr
			)
			return 1

	if authorize is None:
		authorize = functools.partial(ensure_authorized, phone=phone)

	print(f'\nInit program at {time.ctime()}\n')
	if window.is_filtered and not metadata_only:
		print(f'History window: {window.describe()}\n')
	if update:
		print('Update: new posts are merged into saved histories\n')
	if batch:
		duplicates = given - len(targets)
		print(
			f'Targets: {len(targets)}'
			+ (f' ({duplicates} duplicate line(s) skipped)' if duplicates else '')
			+ f'; up to {min(requested, max(1, len(targets)))} at once '
			f'(--concurrency {requested})\n'
		)
	print('')

	try:
		with warnings.catch_warnings(), Lease(session.path, 'The session'):
			warnings.simplefilter('ignore')
			summary = asyncio_run(
				run_collection(
					targets,
					output_folder,
					client_factory=client_factory,
					authorize=authorize,
					metadata_only=metadata_only,
					window=window,
					update=update,
					**controls
				)
			)
	except AuthenticationError as exc:
		print(f'> Login failed: {exc}', file=sys.stderr)
		return 1
	except (OutputLocked, StorageError, LeaseError) as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print('> Interrupted.', file=sys.stderr)
		return 130

	print(f'\nEnd program at {time.ctime()}\n')
	print(
		f'> Collected {len(summary.succeeded)} of {len(summary.requested)} '
		f'targets into {output_folder}\n'
	)
	print_summary(summary, given)

	if summary.collected_in_full:
		print(
			f'> {len(summary.collected_in_full)} channel(s) had no saved history and '
			'were collected in full: ' + ', '.join(summary.collected_in_full) + '\n'
		)
	if summary.stale_histories:
		print(
			f'> Metadata only: the saved messages of {len(summary.stale_histories)} '
			'channel(s) are from an earlier run: '
			+ ', '.join(summary.stale_histories) + '\n'
		)

	if summary.stop_reason:
		print(f'> Stopped: {summary.stop_reason}', file=sys.stderr)
	if summary.cancelled:
		print(
			f'> {len(summary.cancelled)} target(s) not collected: '
			+ list_names(summary.cancelled),
			file=sys.stderr
		)
	if summary.failed:
		print(
			f'> {len(summary.failed)} of {len(summary.requested)} targets failed: '
			+ ', '.join(target for target, _ in summary.failed),
			file=sys.stderr
		)
	if summary.failed or summary.stop_reason:
		return 1

	return 0


def missing_credentials(resolution):
	'''
	Explain which app credentials are missing and how to provide them
	(stderr, exit 2). Names sources only, never values.
	'''
	found = [label for label in resolution.labels() if not label.endswith(': missing')]
	print(
		f'> Missing app credentials: {", ".join(resolution.missing)}'
		+ (f' (found {"; ".join(found)})' if found else '')
		+ '. Run `auth login` to enter and save them, set '
		+ ', '.join(sources.ENV_VARS.values())
		+ ', or fill in config/config.ini.',
		file=sys.stderr
	)
	for note in resolution.notes:
		print(f'> Note: {note}', file=sys.stderr)
	return 2


def list_names(names):
	text = ', '.join(names[:WARN_LIST_LIMIT])
	if len(names) > WARN_LIST_LIMIT:
		text += f', and {len(names) - WARN_LIST_LIMIT} more'
	return text


def print_summary(summary, given):
	'''
	Counts and per-method-class request statistics (stdout).
	'''
	print(
		f'> Targets: {given} given, {len(summary.requested)} after removing '
		f'duplicates; {len(summary.succeeded)} succeeded, {len(summary.failed)} '
		f'failed, {len(summary.duplicates)} same channel as an earlier target, '
		f'{len(summary.cancelled)} cancelled. Concurrency {summary.concurrency}.'
	)
	for target, alias_of in summary.duplicates:
		print(f'>   {target}: same channel as {alias_of}, skipped')
	if summary.requests:
		print('> Requests by method class (sent / retries / flood waits / flood wait seconds):')
		for name, stats in summary.requests.items():
			print(
				f'>   {name:<11} {stats["requests"]} / {stats["retries"]} / '
				f'{stats["flood_waits"]} / {stats["flood_wait_seconds"]}'
			)
	print('')


def asyncio_run(coroutine):
	import asyncio

	return asyncio.run(coroutine)
