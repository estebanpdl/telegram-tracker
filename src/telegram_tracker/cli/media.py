# -*- coding: utf-8 -*-
'''
`download-media`: the media of a collected folder (media.fetch), its
offline --dry-run (media.plan) and its read-only --check (media.check).

A download run checks what it can before credentials, read only: the
folder is not locked, its database is its own, its ledger belongs to it,
and there is something to download (otherwise it ends there, without
logging in). The profile, session and app credentials are then resolved
as for `collect`, and the session lease is held from before the client
exists until the run ends. Telethon and the media modules are loaded
only once the arguments are valid.
'''

# import modules
import functools
import os
import sqlite3
import sys
import time
import warnings

# import local submodules
from telegram_tracker.cli.builders import missing_dependency
from telegram_tracker.cli.collect import print_media
from telegram_tracker.cli.media_parsers import DEFAULT_CONCURRENCY, DEFAULT_FLOOD_WAIT, DEFAULT_INTERVAL, \
	DEFAULT_RETRIES, DOWNLOAD_MEDIA
from telegram_tracker.cli.builder_parsers import AUTO, DEFAULT_OUTPUT
from telegram_tracker.cli.profile_checks import account_check
from telegram_tracker.cli.run_setup import app_client, prepare_session, select_run_session
from telegram_tracker.cli.targets import read_targets
from telegram_tracker.credentials import accounts
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.local_errors import client_failure
from telegram_tracker.utils import DataInputError, data_folder_from


# options --check does not take: (option, argument key)
NOT_WITH_CHECK = (
	('--telegram-channel', 'telegram_channel'), ('--batch-file', 'batch_file'),
	('--media-types', 'media_types'), ('--max-media-size', 'max_media_size'),
	('--max-media-total', 'max_media_total'), ('--concurrency', 'concurrency'),
	('--request-interval', 'request_interval'), ('--max-flood-wait', 'max_flood_wait'),
	('--max-retries', 'max_retries'), ('--profile', 'profile'), ('--session', 'session'),
	('--config', 'config')
)

# states of the --check reference table, in column order
REFERENCE_COLUMNS = (
	('complete', 'complete'), ('failed', 'failed'), ('skipped', 'skipped'), ('requested', 'requested'),
	('downloading', 'downloading'), ('selected_no_row', 'selected, no row'), ('not_selected', 'not selected')
)
LIST_LIMIT = 10


def run_download_media(args, parser, *, client_factory=None, authorize=None):
	'''
	Validate, then download, list (--dry-run) or check (--check). See
	the parser's epilog for exit codes.
	'''
	output_folder = data_folder_from(args['output'], DEFAULT_OUTPUT)
	source = args.get('source') or AUTO
	if args.get('check'):
		given = [option for option, key in NOT_WITH_CHECK if args.get(key) is not None]
		if given:
			parser.error(f'--check takes only --output and --source, not {", ".join(given)}')
	targets = None
	if args.get('telegram_channel') is not None or args.get('batch_file') is not None:
		targets, _ = read_targets(args, parser)

	try:
		from telegram_tracker.media.limits import MediaLimits
		from telegram_tracker.media.selection import MediaSelection
		from telegram_tracker.storage import OutputLocked, StorageError
	except ModuleNotFoundError as exc:
		return missing_dependency(DOWNLOAD_MEDIA, exc)

	if not os.path.isdir(output_folder):
		print(f'> {DOWNLOAD_MEDIA}: {output_folder} does not exist; nothing was collected there.', file=sys.stderr)
		return 1
	try:
		if args.get('check'):
			return run_check(output_folder, source)
		selection = MediaSelection(
			args.get('media_types') or MediaSelection().types,
			MediaLimits(args.get('max_media_size'), args.get('max_media_total'))
		)
		if args.get('dry_run'):
			return run_dry_run(output_folder, source, selection, targets)
		return run_fetch(
			args, parser, output_folder, source, selection, targets,
			client_factory=client_factory, authorize=authorize
		)
	except ModuleNotFoundError as exc:
		return missing_dependency(DOWNLOAD_MEDIA, exc)
	except (DataInputError, OutputLocked, StorageError) as exc:
		print(f'> {DOWNLOAD_MEDIA}: {exc}', file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print('> Interrupted.', file=sys.stderr)
		return 130


'''

Download

'''


def run_fetch(args, parser, output_folder, source, selection, targets, *, client_factory, authorize):
	'''
	The checks before credentials, then one download run and its summary.
	'''
	from telegram_tracker.api.auth import AuthenticationError, ensure_authorized
	from telegram_tracker.media.fetch import fetch_media
	from telegram_tracker.media.ledger import check_ledger
	from telegram_tracker.storage import OutputLocked, StorageError
	from telegram_tracker.storage.destination import select_destination
	from telegram_tracker.storage.provenance import RunProfile
	from telegram_tracker.storage.writer import check_unlocked

	destination = select_destination(output_folder)
	try:
		home, profile, session = select_run_session(args, parser, output_folder)
		destination.protected = (session.path,)
		check_unlocked(output_folder)
		destination.check()
		note = check_ledger(output_folder)
		if note is not None:
			print(f'> Note: {note}', file=sys.stderr)
		plans, pending, _ = read_pending(output_folder, source, selection, targets)
		print_missing(plans)
		if not pending:
			print(f'> Nothing to download: every selected file in {output_folder} is stored.')
			return 1 if plans.missing else 0
		phone = None
		if client_factory is None:
			client_factory, phone = app_client(args, profile, session)
			if client_factory is None:
				return 2
	except (homes.HomeError, sources.CredentialError) as exc:
		print(f'> {exc}', file=sys.stderr)
		return 2

	stop = prepare_session(session, profile, home)
	if stop is not None:
		return stop
	if authorize is None:
		authorize = functools.partial(ensure_authorized, phone=phone)

	print(f'\nInit program at {time.ctime()}\n')
	print(f'Media: {selection.describe()}\n')
	print(f'Source: {source}; {pending} file(s) to download in {output_folder}\n')

	try:
		with warnings.catch_warnings(), Lease(session.path, 'The session'):
			warnings.simplefilter('ignore')
			summary = asyncio_run(fetch_media(
				output_folder,
				selection,
				client_factory=client_factory,
				authorize=authorize,
				source=source,
				targets=targets,
				identify=account_check(profile, session, home, RunProfile),
				preflight=destination.check_unlocked,
				concurrency=args.get('concurrency') or DEFAULT_CONCURRENCY,
				request_interval=args.get('request_interval') or DEFAULT_INTERVAL,
				max_flood_wait=DEFAULT_FLOOD_WAIT if args.get('max_flood_wait') is None else args['max_flood_wait'],
				max_retries=DEFAULT_RETRIES if args.get('max_retries') is None else args['max_retries']
			))
	except AuthenticationError as exc:
		print(f'> Login failed: {exc}', file=sys.stderr)
		return 1
	except (OutputLocked, StorageError, DataInputError, LeaseError, accounts.AccountError) as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1
	except (OSError, sqlite3.Error) as exc:
		# from the client or its session file; the run's own files raise StorageError
		print(f'> {client_failure(exc, session.path)}', file=sys.stderr)
		return 1

	print(f'\nEnd program at {time.ctime()}\n')
	return print_fetch(summary)


def read_pending(output_folder, source, selection, targets):
	'''
	(Plans, references without a complete ledger item, the complete
	ItemKeys) from the saved histories and the ledger, read only. A ledger
	with an unfinished write cannot be read: its references all count.
	'''
	from telegram_tracker.media.plan import open_folder, read_plans

	with open_folder(output_folder, source, lease=False) as collection:
		plans = read_plans(collection, selection, targets)
		for note in collection.notes:
			print(f'> Note: {note}', file=sys.stderr)
	complete = stored_keys(output_folder)
	pending = sum(
		plan.key(message_id, ref) not in complete for plan in plans.plans for message_id, ref in plan.references)
	return plans, pending, complete


def stored_keys(output_folder):
	'''
	The ItemKeys of the complete items in the folder's ledger, read only;
	empty without a ledger, or with one that has an unfinished write.
	'''
	from telegram_tracker.media.identity import folder_identity
	from telegram_tracker.media.layout import MediaLayout, lstat_or_none
	from telegram_tracker.media.ledger import open_ledger
	from telegram_tracker.media.plan import complete_keys
	from telegram_tracker.storage.sqlite.connection import interrupted_write

	layout = MediaLayout(output_folder)
	if layout.ensure(create=False) is None or lstat_or_none(layout.ledger_path) is None:
		return set()
	if interrupted_write(layout.ledger_path) is not None:
		return set()
	with open_ledger(layout, folder_identity(output_folder), readonly=True) as ledger:
		return complete_keys(ledger)


def print_missing(plans):
	if plans.missing:
		print(
			f'> {len(plans.missing)} target(s) have no saved history in this folder: ' + list_names(plans.missing),
			file=sys.stderr
		)
	for name, earlier in plans.duplicates:
		print(f'> {name}: same channel as {earlier}, skipped')


def print_fetch(summary):
	'''
	The run's summary (stdout; problems on stderr). Returns the exit code.
	'''
	if summary.profile is not None:
		print(f'> Profile: {summary.profile.describe()}; account checked with 1 request (get_me).')
	for note in summary.notes:
		print(f'> Note: {note}', file=sys.stderr)
	print_missing(summary)
	print(
		f'> Channels: {summary.channels} with a saved history; {len(summary.refused)} refused, '
		f'{len(summary.failed)} whose messages could not be fetched again. {summary.complete_before} '
		'selected file(s) were already stored.'
	)
	media_problems = print_media(summary.media, summary.stop_reason)
	if summary.stop_reason:
		print(f'> Stopped: {summary.stop_reason}', file=sys.stderr)
	for label, entries in (('refused', summary.refused), ('not fetched again', summary.failed)):
		if entries:
			print(
				f'> {len(entries)} channel(s) {label}: ' + list_names([name for name, _ in entries]),
				file=sys.stderr
			)
	if summary.refused or summary.failed or summary.missing or summary.stop_reason or media_problems:
		return 1
	return 0


'''

Dry run

'''


def run_dry_run(output_folder, source, selection, targets):
	'''
	Offline: per channel and type, what a run would request.
	'''
	from telegram_tracker.media.plan import dry_run_counts, type_totals
	from telegram_tracker.media.selection import describe_size

	plans, pending, complete = read_pending(output_folder, source, selection, targets)
	counts, stored = dry_run_counts(plans.plans, complete, selection.limits)
	print(f'> Dry run, nothing requested or written. Media: {selection.describe()}')
	print(f'> {len(plans.plans)} saved histories; {stored} selected file(s) already stored; {pending} not stored.')
	print('> To download, per channel and type (files / known bytes / files of unknown size / over the size limit):')
	for name, per_type in counts.items():
		for media_type, entry in per_type.items():
			if entry.files or entry.over_file_limit:
				print(
					f'>   {name} {media_type:<5} {entry.files} / {entry.known_bytes} / {entry.unknown_size} / '
					f'{entry.over_file_limit}'
				)
	totals = type_totals(counts)
	known = sum(entry.known_bytes for entry in totals.values())
	unknown = sum(entry.unknown_size for entry in totals.values())
	files = sum(entry.files for entry in totals.values())
	print(
		f'> Total: {files} file(s), {known} bytes known'
		+ (f' ({describe_size(known)})' if known else '')
		+ (f', and {unknown} of unknown size' if unknown else '')
		+ '. The account check and each channel\'s profile are checked when downloading.'
	)
	print_missing(plans)
	return 1 if plans.missing else 0


'''

Check

'''


def run_check(output_folder, source):
	'''
	Offline and read only: media.check.check_folder, printed.
	'''
	from telegram_tracker.media.check import check_folder
	from telegram_tracker.media.classify import TYPES
	from telegram_tracker.media.layout import MEDIA_FOLDER

	report = check_folder(output_folder, source)
	for note in report.notes:
		print(f'> Note: {note}', file=sys.stderr)
	print(f'> Check of {os.path.join(output_folder, MEDIA_FOLDER)} against the saved histories ({report.source})')
	if report.ledger:
		print(f'> Identity: the ledger belongs to the dataset recorded in {report.identity}.')
	else:
		print('> No media ledger: nothing was downloaded into this folder.')
	print(
		'> Media references in the saved histories, by type ('
		+ ' / '.join(label for _, label in REFERENCE_COLUMNS) + '):'
	)
	for media_type in TYPES:
		counts = report.references[media_type]
		print(f'>   {media_type:<5} ' + ' / '.join(str(counts[state]) for state, _ in REFERENCE_COLUMNS))
	print(
		f'> Ledger items: {report.items}; {report.matched} match their saved message, '
		f'{len(report.superseded)} superseded (the message was merged later with other media), '
		f'{len(report.not_in_history)} whose message is not in the current histories.'
	)
	print(f'> Stored files: {report.blobs} checked (size and SHA-256).')
	problems = report.problems()
	if not problems:
		print('> No problem found.')
		return 0
	print(f'> {len(problems)} kind(s) of problem found:', file=sys.stderr)
	for description, findings in problems:
		print(f'>   {len(findings)} {description}: ' + list_names(findings), file=sys.stderr)
	return 1


def list_names(names):
	names = [str(name) for name in names]
	text = ', '.join(names[:LIST_LIMIT])
	if len(names) > LIST_LIMIT:
		text += f', and {len(names) - LIST_LIMIT} more'
	return text


def asyncio_run(coroutine):
	import asyncio

	return asyncio.run(coroutine)
