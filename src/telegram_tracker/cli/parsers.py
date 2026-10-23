# -*- coding: utf-8 -*-
'''
Argument types and parsers: `<prog> <command>`, the legacy collection
form, and the legacy builder scripts. `prog` is how the program was
started (`main.py`, the `tgtracker` command, or `python -m
telegram_tracker`); it only changes the program name shown in help and
errors. Building or using a parser reads no credentials and imports no
services.
'''

# import modules
import argparse

# import local submodules
from telegram_tracker.cli.argument_types import bounded_float, bounded_int, media_size, media_types, \
	utc_datetime
from telegram_tracker.cli.builder_parsers import AUTO, DEFAULT_OUTPUT, SOURCES, \
	add_builder_arguments, build_builder_parser


# --storage values (storage.manifest); importing that module would load
# storage code, which parsers avoid
STORAGE_CHOICES = ('files', 'sqlite', 'both')

BUILDER_STORAGE_NOTE = (
	'Reads the file outputs of a files or both folder and the database of a '
	'sqlite folder (--source chooses).'
)

SCRIPT_PROG = 'main.py'
COMMAND_PROG = 'tgtracker'
MODULE_PROG = 'python -m telegram_tracker'

COLLECT = 'collect'
BUILD_DATASET = 'build-dataset'
BUILD_NETWORK = 'build-network'
BUILD_URLS = 'build-urls'
AUTH = 'auth'

# auth actions
LOGIN = 'login'
STATUS = 'status'
LOGOUT = 'logout'
CLEAR_CREDENTIALS = 'clear-credentials'
PROFILES = 'profiles'


def invocation(prog):
	'''
	What a user types to run `prog`: scripts run through `python`.
	'''
	return f'python {prog}' if prog.endswith('.py') else prog


def legacy_forms(prog):
	run = invocation(prog)
	indent = ' ' * (len(run) + 3)
	return (
		'legacy forms (still supported):\n'
		f'  {run} --telegram-channel NAME [--limit-download-to-channel-metadata]\n'
		f'{indent}[--output DIR] [--min-id N]\n'
		f'  {run} --batch-file FILE [same options]\n'
		'  python build-datasets.py [--data-path DIR]\n'
		'  python channels-to-network.py [--data-path DIR]\n'
		'\n'
		f'Run `{run} <command> --help` for a command\'s options.'
	)


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


def add_profile_argument(group):
	'''
	--profile, shared by `collect` and the `auth` actions.
	'''
	group.add_argument(
		'--profile',
		metavar='NAME',
		help='Account profile: its own saved credentials and session '
			'(created by `auth login --profile NAME`). Default: '
			'TELEGRAM_TRACKER_PROFILE if set, else the default profile, which '
			'uses the application home itself.'
	)


def add_session_arguments(group):
	'''
	--profile, --session, and --config, shared by `collect` and `auth`.
	'''
	add_profile_argument(group)
	group.add_argument(
		'--session',
		metavar='PATH',
		help='Telegram session file (default profile only). Default: '
			'TELEGRAM_TRACKER_SESSION if set, else session_file.session in the '
			'application home (~/.telegram_tracker, or TELEGRAM_TRACKER_HOME); an '
			'existing ./session_file.session is used while the home has none. A '
			'named profile always uses its own session.'
	)
	group.add_argument(
		'--config',
		metavar='PATH',
		help='INI file with a [Telegram API credentials] section, used instead of '
			"the profile's saved credentials (and, for the default profile, "
			'./config/config.ini). TELEGRAM_TRACKER_API_ID, '
			'TELEGRAM_TRACKER_API_HASH, and TELEGRAM_TRACKER_PHONE override '
			'single values.'
	)


def add_auth_parsers(commands):
	'''Return the `auth` parser and its action parsers by name.'''
	auth = commands.add_parser(
		AUTH,
		help='Log in to Telegram, check or revoke the session, and manage saved credentials.',
		description='Log in to Telegram, check or revoke the session, and manage '
			'the app credentials saved in the application home and its profiles.'
	)
	actions = auth.add_subparsers(dest='auth_command', metavar='<action>', required=True)

	login = actions.add_parser(
		LOGIN,
		help='Log in and save the app credentials.',
		description='Log in to Telegram with the selected session. Missing app '
			'credentials (api_id, api_hash, phone) are asked for; after a '
			'successful login, a complete set you typed is saved to the '
			"profile's credentials.json when nothing was saved before "
			'(plaintext, readable only by your account). '
			'Values from the environment or an INI file are saved only if you '
			'agree, as are replacements. An authorized session skips the login '
			'code and two-step verification password prompts. The logged-in '
			'account is checked and recorded in profile.json; --profile NAME '
			'creates a named profile.'
	)
	add_session_arguments(login)
	save = login.add_mutually_exclusive_group()
	save.add_argument(
		'--save',
		action='store_true',
		help='Save the credentials used without asking, replacing saved ones.'
	)
	save.add_argument(
		'--no-save',
		action='store_true',
		help='Do not save credentials.'
	)

	status = actions.add_parser(
		STATUS,
		help='Show which session and credential sources are used.',
		description='Show the application home, the profile, the selected session, '
			'and where each app credential comes from. Values are never shown. Notes a '
			'./config/config.ini that saved credentials hide. Offline: a session file does '
			'not prove that it is logged in.'
	)
	add_session_arguments(status)
	status.add_argument(
		'--check',
		action='store_true',
		help='Connect to Telegram to check that the session is logged in. Never '
			'asks for a code; Telethon may update the session file.'
	)

	logout = actions.add_parser(
		LOGOUT,
		help='Revoke the session on Telegram.',
		description='Log the selected session out on Telegram, which revokes it '
			'and deletes the session file. Saved credentials stay.'
	)
	add_session_arguments(logout)
	logout.add_argument('--yes', action='store_true', help='Do not ask for confirmation.')

	clear = actions.add_parser(
		CLEAR_CREDENTIALS,
		help='Delete the saved credentials.',
		description="Delete the profile's credentials.json. The session, the "
			'profile record (profile.json), environment variables, and INI files '
			'are not touched.'
	)
	add_profile_argument(clear)
	clear.add_argument('--yes', action='store_true', help='Do not ask for confirmation.')

	profiles = actions.add_parser(
		PROFILES,
		help='List the account profiles.',
		description='List the default profile and every named profile in the '
			'application home, with whether each has a profile record, saved '
			'credentials, and a session file. Offline; reads no values and '
			'creates nothing.'
	)

	return auth, {
		LOGIN: login, STATUS: status, LOGOUT: logout, CLEAR_CREDENTIALS: clear,
		PROFILES: profiles
	}


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


def add_collect_arguments(parser, prog=SCRIPT_PROG):
	'''
	`collect`: the legacy arguments plus history window and request
	controls.
	'''
	add_target_arguments(parser)

	window = parser.add_argument_group(
		'history window',
		'Select which messages to collect; bounds combine (AND). A filtered run '
		'replaces each channel\'s saved history (files or database) with the '
		'selection, so write it to a fresh --output to keep earlier histories. '
		'Before connecting, it names the channels that already have a saved '
		'history and asks to continue; without a terminal to answer, it stops '
		'unless --yes is given. Not allowed with '
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
	window.add_argument(
		'--yes',
		action='store_true',
		help='Replace saved histories without asking (for scripts).'
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
			'its saved history and merge them into it (fetched copies win). The '
			'saved history is <channel>_messages.json in files, the database in '
			'sqlite, and both copies in both, which must agree. A channel without '
			'a saved history is collected in full. Posts deleted or edited on '
			'Telegram since the earlier run are not reflected, and build-dataset '
			'appends the merged history again, duplicating earlier rows.'
	)

	storage = parser.add_argument_group(
		'storage',
		'Where collected data is saved. A folder keeps the mode it was first '
		'collected with: without --storage, a run uses that mode (files for a '
		'folder collected before); another mode is refused. sqlite and both '
		'need a new or empty --output.'
	)
	storage.add_argument(
		'--storage',
		choices=STORAGE_CHOICES,
		help='files: JSON, CSV and TXT files (default for a new folder). sqlite: '
			'only the database <output>/telegram-tracker.sqlite3; build-dataset and '
			'build-network read it. both: the same data in the files and the '
			'database; the builders read the files unless given --source sqlite.'
	)

	media = parser.add_argument_group(
		'media',
		'Download the files attached to the collected messages. Off by default: '
		'without --include-media no file is requested. The other options need '
		'--include-media, and none is allowed with '
		'--limit-download-to-channel-metadata.'
	)
	media.add_argument(
		'--include-media',
		action='store_true',
		help='Also download the photos, videos, audio and documents attached to '
			'the collected messages into <output>/downloaded-media/, recorded in its '
			'media.sqlite3. No size limit unless --max-media-size or '
			'--max-media-total is given. With --update, only the new messages\' '
			'files. Downloads start once every channel\'s messages are saved and use '
			'the request controls below. Files are saved, never opened.'
	)
	media.add_argument(
		'--media-types',
		type=media_types,
		metavar='TYPES',
		help='Only these kinds, separated by commas: image, video, audio, file '
			'(default: all).'
	)
	media.add_argument(
		'--max-media-size',
		type=media_size,
		metavar='SIZE',
		help='Skip a file larger than SIZE (bytes, or a number with K, M or G for '
			'KiB, MiB, GiB). A file of unknown size is stopped once it passes SIZE, '
			'and its partial copy is removed.'
	)
	media.add_argument(
		'--max-media-total',
		type=media_size,
		metavar='SIZE',
		help='Receive at most SIZE in this run, counting every byte received, '
			'restarts included. Files that do not fit are skipped and recorded.'
	)

	controls = parser.add_argument_group(
		'request controls',
		'Shared by channel collection requests after the account check, '
		'including metadata lookups and media downloads (one request per 512 KiB '
		'of a file). Login and the account check are outside '
		'these controls; Telethon may retry inside an attempt. These are project '
		'limits, not a safe rate guaranteed by '
		f'Telegram. The earlier `{invocation(prog)} --batch-file ...` form uses the '
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
		help='Minimum gap between policy attempts, 0.1-60 (default: 0.25).'
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

	login = parser.add_argument_group(
		'session and credentials',
		f'Which Telegram session and app credentials to use; '
		f'`{invocation(prog)} auth status` shows the current choice.'
	)
	add_session_arguments(login)


def build_parser(prog=SCRIPT_PROG):
	'''
	The canonical parser: `<prog> <command> ...`.
	'''
	return build_parsers(prog)[0]


def build_parsers(prog=SCRIPT_PROG):
	'''
	The canonical parser and its subcommand parsers by name.
	'''
	parser = argparse.ArgumentParser(
		prog=prog,
		description='Collect Telegram channel data, then build a messages '
			'dataset and a channel network from it.',
		epilog=legacy_forms(prog),
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
			'collected_chats.xlsx (replaced) from collected data. Offline. '
			+ BUILDER_STORAGE_NOTE
	)
	subparsers[BUILD_NETWORK] = commands.add_parser(
		BUILD_NETWORK,
		help='Build Graph.gexf and network.png from collected_chats.csv.',
		description='Build Graph.gexf and network.png from collected_chats.csv. Offline. '
			+ BUILDER_STORAGE_NOTE
	)
	subparsers[BUILD_URLS] = commands.add_parser(
		BUILD_URLS,
		help='Build urls_dataset.csv and urls.sqlite3: URL occurrences from collected messages.',
		description='Build urls_dataset.csv, urls.sqlite3 and urls-build.json (all '
			'replaced): one row per detected URL occurrence (URL entities, hidden '
			'text links, URLs in plain text, link previews) with its parsed parts '
			'and registered domain, and counts per domain and channel. Offline: no '
			'link is opened, resolved or expanded, and the public suffix list is the '
			'copy bundled with tldextract. '
			+ BUILDER_STORAGE_NOTE
	)

	subparsers[AUTH], actions = add_auth_parsers(commands)
	subparsers.update({f'{AUTH} {name}': action for name, action in actions.items()})

	add_collect_arguments(subparsers[COLLECT], prog)
	add_builder_arguments(subparsers[BUILD_DATASET])
	add_builder_arguments(subparsers[BUILD_NETWORK])
	add_builder_arguments(subparsers[BUILD_URLS])
	return parser, subparsers


def build_legacy_parser(prog=SCRIPT_PROG):
	'''
	`<prog> --telegram-channel ... | --batch-file ...`: the legacy
	collection arguments only.
	'''
	run = invocation(prog)
	parser = argparse.ArgumentParser(
		prog=prog,
		description=f'Arguments. (Legacy form of `{run} collect`.)'
	)
	add_target_arguments(parser)
	window = parser.add_argument_group(
		'history window',
		'Select which messages to collect. A run with --min-id replaces each '
		'channel\'s saved messages with the selection, so write it to a fresh '
		'--output to keep earlier histories. It names the channels that already '
		f'have one and replaces them without asking; `{run} collect` asks first. '
		f'The other history window options and --update need `{run} collect`.'
	)
	add_min_id_argument(window)
	return parser


def is_legacy_collection(argv):
	'''
	Legacy collection: the first token is an option other than help.
	'''
	return bool(argv) and argv[0].startswith('-') and argv[0] not in ('-h', '--help')
