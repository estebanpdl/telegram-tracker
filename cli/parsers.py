# -*- coding: utf-8 -*-
'''
Argument types and parsers: `main.py <command>`, the legacy collection
form, and the legacy builder scripts. Building or using a parser reads no
credentials and imports no services.
'''

# import modules
import argparse
import math

from datetime import datetime, timezone


DEFAULT_OUTPUT = './output/data'

COLLECT = 'collect'
BUILD_DATASET = 'build-dataset'
BUILD_NETWORK = 'build-network'
COMMANDS = (COLLECT, BUILD_DATASET, BUILD_NETWORK)

# collect-only history window flags (argparse dests)
WINDOW_FLAGS = ('since', 'until', 'max_id', 'max_messages')

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
	Target, metadata-only, and output arguments shared by `collect` and the
	legacy collection form.
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


def add_min_id_argument(group):
	'''
	--min-id, accepted by `collect` and the legacy collection form. Each
	parser lists it under its own "history window" group.
	'''
	group.add_argument(
		'--min-id',
		type=bounded_int(0),
		metavar='N',
		help='Only messages with an id above N (exclusive).'
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
	add_min_id_argument(window)
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
		'Shared by every Telegram request of the run, including metadata '
		'lookups. These are project limits, not a safe rate guaranteed by '
		'Telegram. The earlier `python main.py --batch-file ...` form uses the '
		'defaults.'
	)
	controls.add_argument(
		'--concurrency',
		type=bounded_int(1, 10),
		metavar='N',
		help='Channels collected at once, 1-10; also the most requests in '
			'flight (default: 3 for --batch-file; --telegram-channel always runs '
			'at 1). Use 1 to collect one channel at a time.'
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
		help='Longest server flood wait to sit out, 0-3600; a longer one stops '
			'the whole run, keeping completed channels. 0 stops on any flood wait '
			'(default: 60).'
	)
	controls.add_argument(
		'--max-retries',
		type=bounded_int(0, 10),
		default=3,
		metavar='N',
		help='Additional attempts per request after a flood wait or a transient '
			'error, 0-10 (default: 3).'
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
	window = parser.add_argument_group(
		'history window',
		'Select which messages to collect. A run with --min-id replaces each '
		'channel\'s saved messages with the selection, so write it to a fresh '
		'--output to keep earlier histories. The other history window options '
		'and --update need `python main.py collect`.'
	)
	add_min_id_argument(window)
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
