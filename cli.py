# -*- coding: utf-8 -*-
'''
Command-line interface for Telegram Tracker.

	python main.py collect --telegram-channel NAME [options]
	python main.py collect --batch-file FILE [options]
	python main.py build-dataset [--data-path DIR]
	python main.py build-network [--data-path DIR]

Legacy forms keep working and reach the same handlers:

	python main.py --telegram-channel NAME | --batch-file FILE [...]
	python build-datasets.py [--data-path DIR]
	python channels-to-network.py [--data-path DIR]

Arguments are parsed and validated before any credentials are read or
services imported; help never needs Telethon, pandas, or plotting
libraries. Exit codes: 0 success, 1 failed targets / login / input data /
missing dependency, 2 argument errors, 130 interrupted.
'''

# import modules
import argparse
import functools
import math
import os
import sys
import time
import warnings

from datetime import datetime, timezone

# import local submodules
from utils import DataInputError, cmd_request_type, data_folder_from


SESSION_FILE = 'session_file'
DEFAULT_OUTPUT = './output/data'

COLLECT = 'collect'
BUILD_DATASET = 'build-dataset'
BUILD_NETWORK = 'build-network'
COMMANDS = (COLLECT, BUILD_DATASET, BUILD_NETWORK)

# collect-only history window flags (argparse dests)
WINDOW_FLAGS = ('since', 'until', 'max_id', 'max_messages')

# names listed by the existing-history warning before "and N more"
WARN_LIST_LIMIT = 10

LEGACY_FORMS = '''\
legacy forms (still supported):
  python main.py --telegram-channel NAME [--limit-download-to-channel-metadata]
                 [--output DIR] [--min-id N]
  python main.py --batch-file FILE [same options]
  python build-datasets.py [--data-path DIR]
  python channels-to-network.py [--data-path DIR]

Run `python main.py <command> --help` for a command's options.'''


'''

Argument types

'''

def bounded_int(low, high=None):
	'''
	argparse type: an integer in [low, high] (no upper bound when None).
	'''
	def parse(text):
		try:
			value = int(text)
		except ValueError:
			raise argparse.ArgumentTypeError(f'invalid integer: {text!r}') from None
		if value < low or (high is not None and value > high):
			span = f'between {low} and {high}' if high is not None else f'at least {low}'
			raise argparse.ArgumentTypeError(f'must be {span}, got {value}')
		return value

	return parse


def bounded_float(low, high):
	'''
	argparse type: a finite number in [low, high].
	'''
	def parse(text):
		try:
			value = float(text)
		except ValueError:
			raise argparse.ArgumentTypeError(f'invalid number: {text!r}') from None
		if not math.isfinite(value) or not low <= value <= high:
			raise argparse.ArgumentTypeError(
				f'must be a finite number between {low:g} and {high:g}, got {text}'
			)
		return value

	return parse


def utc_datetime(text):
	'''
	argparse type: an ISO 8601 date or datetime as an aware UTC datetime.
	A date alone means midnight UTC; a value without an offset is UTC; a
	`Z` or +HH:MM offset is converted to UTC.
	'''
	try:
		value = datetime.fromisoformat(text.strip())
	except ValueError:
		raise argparse.ArgumentTypeError(
			f'invalid date {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS], '
			'optionally with Z or +HH:MM'
		) from None

	if value.tzinfo is None:
		return value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)


'''

Parsers

'''

def add_target_arguments(parser):
	'''
	Arguments shared by `collect` and the legacy collection form.
	'''
	targets = parser.add_mutually_exclusive_group(required=True)
	targets.add_argument(
		'--telegram-channel',
		type=str,
		help='Specifies a Telegram Channel.'
	)
	targets.add_argument(
		'--batch-file',
		type=str,
		help='File containing Telegram Channels, one channel per line.'
	)
	parser.add_argument(
		'--limit-download-to-channel-metadata',
		action='store_true',
		help='Will collect channels metadata only, not posts data.'
	)
	parser.add_argument(
		'--output',
		'-o',
		type=str,
		required=False,
		help=f'Folder to save collected data. Default: `{DEFAULT_OUTPUT}`'
	)
	parser.add_argument(
		'--min-id',
		type=bounded_int(0),
		metavar='N',
		help='Collect only messages with an id above N (exclusive). A rerun '
			'replaces the channel\'s saved messages with the messages collected.'
	)


def add_collect_arguments(parser):
	'''
	`collect`: the legacy arguments plus history window and request
	controls.
	'''
	add_target_arguments(parser)

	window = parser.add_argument_group(
		'history window',
		'Select which messages to collect; bounds combine (AND). A filtered run '
		'replaces each channel\'s saved messages with the selection, so write it '
		'to a fresh --output to keep earlier histories. Not allowed with '
		'--limit-download-to-channel-metadata.'
	)
	window.add_argument(
		'--since',
		type=utc_datetime,
		metavar='DATE',
		help='Only messages dated at or after DATE (UTC unless an offset is '
			'given; a date alone means midnight). Example: --since 2026-09-01'
	)
	window.add_argument(
		'--until',
		type=utc_datetime,
		metavar='DATE',
		help='Only messages dated before DATE (exclusive). Example: '
			'--until 2026-10-01 excludes 1 October (UTC).'
	)
	window.add_argument(
		'--max-id',
		type=bounded_int(1),
		metavar='N',
		help='Only messages with an id below N (exclusive).'
	)
	window.add_argument(
		'--max-messages',
		type=bounded_int(1),
		metavar='N',
		help='At most N messages per channel, newest first.'
	)

	update = parser.add_argument_group(
		'update',
		'Add new posts to saved histories instead of replacing them. Not '
		'allowed with the history window options or '
		'--limit-download-to-channel-metadata.'
	)
	update.add_argument(
		'--update',
		action='store_true',
		help='For each channel, collect only posts newer than the newest one in '
			'its saved <channel>_messages.json and merge them into that file '
			'(fetched copies win). A channel without a saved history is collected '
			'in full. Posts deleted or edited on Telegram since the earlier run '
			'are not reflected, and build-dataset appends the merged history '
			'again, duplicating earlier rows.'
	)

	controls = parser.add_argument_group(
		'request controls',
		'Validated now; not applied yet. This version collects channels one '
		'at a time.'
	)
	controls.add_argument(
		'--concurrency',
		type=bounded_int(1, 10),
		metavar='N',
		help='Channels collected at once, 1-10 (default: 3 for --batch-file, '
			'1 for --telegram-channel).'
	)
	controls.add_argument(
		'--request-interval',
		type=bounded_float(0.1, 60),
		default=0.25,
		metavar='SECONDS',
		help='Minimum gap between Telegram requests, 0.1-60 (default: 0.25).'
	)
	controls.add_argument(
		'--max-flood-wait',
		type=bounded_int(0, 3600),
		default=60,
		metavar='SECONDS',
		help='Longest server flood wait to sit out, 0-3600; 0 stops on any '
			'flood wait (default: 60).'
	)
	controls.add_argument(
		'--max-retries',
		type=bounded_int(0, 10),
		default=3,
		metavar='N',
		help='Additional attempts per request, 0-10 (default: 3).'
	)


def add_builder_arguments(parser):
	parser.add_argument(
		'--data-path',
		'-d',
		type=str,
		required=False,
		help=f'Path where data is located. Will use `{DEFAULT_OUTPUT}` if not given.'
	)


def build_parser():
	'''
	The canonical parser: `main.py <command> ...`.
	'''
	return build_parsers()[0]


def build_parsers():
	'''
	The canonical parser and its subcommand parsers by name.
	'''
	parser = argparse.ArgumentParser(
		prog='main.py',
		description='Collect Telegram channel data, then build a messages '
			'dataset and a channel network from it.',
		epilog=LEGACY_FORMS,
		formatter_class=argparse.RawDescriptionHelpFormatter
	)
	commands = parser.add_subparsers(dest='command', metavar='<command>', required=True)

	subparsers = {}
	subparsers[COLLECT] = commands.add_parser(
		COLLECT,
		help='Collect channel metadata and messages from Telegram.',
		description='Collect channel metadata and messages from Telegram. '
			'Exactly one of --telegram-channel or --batch-file is required. '
			'An unauthorized session asks for a login code (and the two-step '
			'verification password, if set) in the terminal.'
	)
	subparsers[BUILD_DATASET] = commands.add_parser(
		BUILD_DATASET,
		help='Build msgs_dataset.csv and collected_chats.xlsx from collected data.',
		description='Build msgs_dataset.csv (appended to) and '
			'collected_chats.xlsx (replaced) from collected data. Offline.'
	)
	subparsers[BUILD_NETWORK] = commands.add_parser(
		BUILD_NETWORK,
		help='Build Graph.gexf and network.png from collected_chats.csv.',
		description='Build Graph.gexf and network.png from collected_chats.csv. Offline.'
	)

	add_collect_arguments(subparsers[COLLECT])
	add_builder_arguments(subparsers[BUILD_DATASET])
	add_builder_arguments(subparsers[BUILD_NETWORK])
	return parser, subparsers


def build_legacy_parser():
	'''
	`main.py --telegram-channel ... | --batch-file ...`: the legacy
	collection arguments only.
	'''
	parser = argparse.ArgumentParser(
		prog='main.py',
		description='Arguments. (Legacy form of `python main.py collect`.)'
	)
	add_target_arguments(parser)
	return parser


def build_builder_parser(command, prog):
	'''
	Standalone parser for a legacy builder script.
	'''
	parser = argparse.ArgumentParser(
		prog=prog,
		description=f'Arguments. (Same as `python main.py {command}`.)'
	)
	add_builder_arguments(parser)
	return parser


def is_legacy_collection(argv):
	'''
	Legacy collection: the first token is an option other than help.
	'''
	return bool(argv) and argv[0].startswith('-') and argv[0] not in ('-h', '--help')


'''

Collection

'''

def read_targets(args, parser):
	'''
	Targets from --telegram-channel, or one per line from --batch-file.
	An unreadable batch file is an argument error (exit 2).
	'''
	req_type, req_input = cmd_request_type(args)
	if req_type != 'batch':
		return [req_input]

	try:
		with open(req_input, encoding='utf-8', mode='r') as reader:
			return [i.rstrip() for i in reader]
	except OSError as exc:
		parser.error(f'cannot read --batch-file {req_input!r}: {exc.strerror}')


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
	Validate, then run one collection. See main() for exit codes.
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

	targets = read_targets(args, parser)
	output_folder = data_folder_from(args['output'], DEFAULT_OUTPUT)
	if bounds and not metadata_only:
		warn_replaced_histories(targets, output_folder)

	try:
		from api.auth import AuthenticationError, ensure_authorized
		from collector import HistoryWindow, run_collection
		from storage import OutputLocked, StorageError
	except ModuleNotFoundError as exc:
		return missing_dependency(COLLECT, exc)

	window = HistoryWindow(**bounds)

	phone = None
	if client_factory is None:
		from api import create_client
		from utils import get_config_attrs

		config_attrs = get_config_attrs()
		phone = config_attrs['phone']
		client_factory = functools.partial(
			create_client,
			SESSION_FILE,
			config_attrs['api_id'],
			config_attrs['api_hash']
		)

	if authorize is None:
		authorize = functools.partial(ensure_authorized, phone=phone)

	print(f'\nInit program at {time.ctime()}\n')
	if window.is_filtered and not metadata_only:
		print(f'History window: {window.describe()}\n')
	if update:
		print('Update: new posts are merged into saved histories\n')
	print('')

	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			summary = asyncio_run(
				run_collection(
					targets,
					output_folder,
					client_factory=client_factory,
					authorize=authorize,
					metadata_only=metadata_only,
					window=window,
					update=update
				)
			)
	except AuthenticationError as exc:
		print(f'> Login failed: {exc}', file=sys.stderr)
		return 1
	except (OutputLocked, StorageError) as exc:
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

	if summary.failed:
		print(
			f'> {len(summary.failed)} of {len(summary.requested)} targets failed: '
			+ ', '.join(target for target, _ in summary.failed),
			file=sys.stderr
		)
		return 1

	return 0


def asyncio_run(coroutine):
	import asyncio

	return asyncio.run(coroutine)


'''

Builders

'''

def run_builder(command, args):
	'''
	Run build-dataset or build-network on --data-path.
	'''
	data_path = data_folder_from(args['data_path'], DEFAULT_OUTPUT)

	print(f'\nInit program at {time.ctime()}\n\n')
	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			if command == BUILD_DATASET:
				from datasets import build_dataset

				summary = build_dataset(data_path)
				outcome = (
					f'> Wrote {summary.rows_written} rows to {summary.msgs_dataset} '
					f'and {summary.chats_xlsx}'
				)
			else:
				from network import build_network

				summary = build_network(data_path)
				outcome = (
					f'> Wrote {summary.nodes} nodes and {summary.edges} edges to '
					f'{summary.gexf} and {summary.png}'
				)
	except ModuleNotFoundError as exc:
		return missing_dependency(command, exc)
	except DataInputError as exc:
		print(f'> {command}: {exc}', file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print('> Interrupted.', file=sys.stderr)
		return 130

	print(outcome)
	return 0


def missing_dependency(command, exc):
	print(
		f'> {command}: missing dependency {exc.name!r}. Install requirements.txt '
		'in the telegram-tracker environment.',
		file=sys.stderr
	)
	return 1


'''

Entry points

'''

def main(argv=None, *, client_factory=None, authorize=None):
	'''
	Parse `argv` (default: sys.argv[1:]) and run the command. Returns the
	exit code; argument errors raise SystemExit(2) before credentials are
	read.

	client_factory / authorize replace the Telethon client and the login
	step for `collect` (tests use fakes). By default credentials come from
	config/config.ini and the session from ./session_file.session.
	'''
	argv = sys.argv[1:] if argv is None else list(argv)

	if not argv:
		build_parser().print_help(sys.stderr)
		return 2

	if is_legacy_collection(argv):
		parser = build_legacy_parser()
		args = vars(parser.parse_args(argv))
		return run_collect(
			args, parser, legacy=True,
			client_factory=client_factory, authorize=authorize
		)

	parser, subparsers = build_parsers()
	args = vars(parser.parse_args(argv))
	command = args.pop('command')
	if command == COLLECT:
		return run_collect(
			args, subparsers[COLLECT], legacy=False,
			client_factory=client_factory, authorize=authorize
		)
	return run_builder(command, args)


def builder_main(command, argv=None, *, prog):
	'''
	Entry for the legacy builder scripts: their own `--data-path/-d`
	parser, the same handler as `main.py <command>`.
	'''
	parser = build_builder_parser(command, prog)
	args = vars(parser.parse_args(sys.argv[1:] if argv is None else argv))
	return run_builder(command, args)
