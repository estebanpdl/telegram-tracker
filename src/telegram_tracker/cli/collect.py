# -*- coding: utf-8 -*-
'''
`collect`: validation of the history window, --update, and request
controls, then one collection run and its summary. Telethon, the
collector, and credentials are loaded only after the arguments are valid.
A window run that replaces saved histories asks first, before
credentials are read (confirm_replaced_histories). The profile, session,
and app credentials are resolved as for `auth`
(see telegram_tracker.credentials), and the session lease is held from
before the client exists until the run ends. After login, one get_me
request identifies the account; the account lease is then held until the
run ends, allowing one collection per verified account within one
application home.
'''

# import modules
import functools
import io
import os
import sqlite3
import sys
import time
import warnings

# import local submodules
from telegram_tracker.cli.auth import confirm
from telegram_tracker.cli.builders import missing_dependency
from telegram_tracker.cli.profile_checks import account_check
from telegram_tracker.cli.parsers import COLLECT, DEFAULT_OUTPUT, invocation
from telegram_tracker.cli.targets import read_targets
from telegram_tracker.credentials import accounts, profiles
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.credentials.protection import ProtectionError
from telegram_tracker.local_errors import client_failure
from telegram_tracker.utils import data_folder_from


# names listed by the replaced-history warning and the summary before "and N more"
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


def confirm_replaced_histories(existing, output_folder, *, legacy, assume_yes):
	'''
	Before a filtered run, before credentials: name the channels whose
	saved history the selection replaces (stderr), then ask to continue.
	--yes skips the question; input that is not a terminal is refused
	without it. The legacy form only warns: its arguments cannot take
	--yes. Returns None to go on, or the exit code of a run that stops
	here.
	'''
	if not existing:
		return None

	print(
		f'> Warning: this filtered run replaces the saved messages of '
		f'{len(existing)} channel(s) in {output_folder}: {list_names(existing)}. '
		'Use a fresh --output to keep them.',
		file=sys.stderr
	)
	if legacy or assume_yes:
		return None
	if not interactive():
		print(
			'> Not replaced: input is not a terminal, so the replacement cannot be '
			'confirmed. Pass --yes to replace them, or choose a fresh --output.',
			file=sys.stderr
		)
		return 2
	if not confirm('Continue?'):
		print('> Nothing collected; the saved histories are unchanged.', file=sys.stderr)
		return 1
	return None


def interactive():
	'''
	True when standard input is a terminal that can answer a question.
	On Windows the NUL device also reports isatty(), so a standard input
	with a file descriptor must also be a console there.
	'''
	try:
		if sys.stdin is None or not sys.stdin.isatty():
			return False
		fd = sys.stdin.fileno()
	except io.UnsupportedOperation:
		# a stream without a descriptor: only its isatty() can tell
		return True
	except (OSError, ValueError):
		return False
	return os.name != 'nt' or windows_console(fd)


def windows_console(fd):
	import ctypes
	import msvcrt

	try:
		handle = msvcrt.get_osfhandle(fd)
	except OSError:
		return False
	mode = ctypes.c_ulong()
	return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))


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
		from telegram_tracker.storage.destination import StorageChoiceRefused, select_destination
		from telegram_tracker.storage.provenance import RunProfile
	except ModuleNotFoundError as exc:
		return missing_dependency(COLLECT, exc)

	try:
		destination = select_destination(output_folder, args.get('storage'), legacy=legacy)
	except StorageChoiceRefused as exc:
		if legacy:
			parser.error(f'{exc}. Use `{invocation(parser.prog)} collect`, which keeps the folder\'s mode')
		parser.error(str(exc))
	except StorageError as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1

	window = HistoryWindow(**bounds)

	try:
		home, _ = homes.app_home()
		profile = profiles.select_profile(args.get('profile'), home=home)
		profiles.require(profile)
		session = profiles.select_session(profile, args.get('session'))
		if homes.is_within(session.path, output_folder):
			parser.error('the session file must not be inside the --output folder')
		if homes.is_within(output_folder, home) or homes.is_within(home, output_folder):
			parser.error(f'the --output folder must be outside the application home ({home})')
		destination.protected = (session.path,)
		destination.check()
		confirmed = None
		if bounds and not metadata_only:
			assume_yes = args.get('yes', False)
			existing = destination.saved_histories(targets)
			stop = confirm_replaced_histories(existing, output_folder, legacy=legacy, assume_yes=assume_yes)
			if stop is not None:
				return stop
			if not (legacy or assume_yes):
				# a history found only after resolution was not confirmed
				confirmed = frozenset(existing)
		phone = None
		if client_factory is None:
			from telegram_tracker.api import create_client

			resolution = sources.resolve(
				config=args.get('config'),
				home=profile.root,
				discover_legacy=profile.is_default
			)
			if resolution.missing:
				return missing_credentials(resolution, profile)
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
	except StorageError as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print('\n> Interrupted.', file=sys.stderr)
		return 130

	if session.notice:
		print(session.notice, file=sys.stderr)
	if session.in_home and not os.path.isfile(session.path):
		try:
			profiles.prepare_root(profile)
		except ProtectionError as exc:
			print(
				f'> Cannot create the session in {profile.root}: {exc}. Set '
				'TELEGRAM_TRACKER_HOME' + (' or pass --session.' if profile.is_default else '.'),
				file=sys.stderr
			)
			return 1
	try:
		accounts.prepare(home)
	except accounts.AccountError as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1

	if authorize is None:
		authorize = functools.partial(ensure_authorized, phone=phone)

	print(f'\nInit program at {time.ctime()}\n')
	if window.is_filtered and not metadata_only:
		print(f'History window: {window.describe()}\n')
	if update:
		print('Update: new posts are merged into saved histories\n')
	if destination.uses_database:
		print(f'Storage: {destination.describe()}\n')
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
					identify=account_check(profile, session, home, RunProfile),
					writer_factory=destination.open_writer,
					preflight=destination.check_unlocked,
					confirmed=confirmed,
					**controls
				)
			)
	except AuthenticationError as exc:
		print(f'> Login failed: {exc}', file=sys.stderr)
		return 1
	except (OutputLocked, StorageError, LeaseError, accounts.AccountError) as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1
	except (OSError, sqlite3.Error) as exc:
		# from the client or its session file; the run's own files raise StorageError
		print(f'> {client_failure(exc, session.path)}', file=sys.stderr)
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
	if summary.takeovers:
		print(
			f'> {len(summary.takeovers)} channel(s) replaced a history of another '
			'profile (recorded as a takeover): ' + ', '.join(
				f'{target} (was {previous or "without a profile record"})'
				for target, previous in summary.takeovers
			) + '\n'
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


def missing_credentials(resolution, profile):
	'''
	Explain which app credentials are missing and how to provide them
	(stderr, exit 2). Names sources only, never values.
	'''
	found = [label for label in resolution.labels() if not label.endswith(': missing')]
	login = 'auth login' + ('' if profile.is_default else f' --profile {profile.name}')
	print(
		f'> Missing app credentials: {", ".join(resolution.missing)}'
		+ (f' (found {"; ".join(found)})' if found else '')
		+ f'. Run `{login}` to enter and save them, set '
		+ ', '.join(sources.ENV_VARS.values())
		+ (', or fill in config/config.ini.' if profile.is_default else ', or pass --config.'),
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
	if summary.profile is not None:
		print(f'> Profile: {summary.profile.describe()}; account checked with 1 request (get_me).')
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
