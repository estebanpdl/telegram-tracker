# -*- coding: utf-8 -*-
'''
`download-media` arguments, and the media selection arguments it shares
with `collect`. Parsing reads no credentials and loads no services.
'''

# import local submodules
from telegram_tracker.cli.argument_types import bounded_float, bounded_int, media_size, media_types
from telegram_tracker.cli.builder_parsers import AUTO, DEFAULT_OUTPUT, SOURCES


DOWNLOAD_MEDIA = 'download-media'

# request control defaults (the parser leaves them None, so that the
# options given with --check can be told apart)
DEFAULT_CONCURRENCY = 1
DEFAULT_INTERVAL = 0.25
DEFAULT_FLOOD_WAIT = 60
DEFAULT_RETRIES = 3

DOWNLOAD_MEDIA_HELP = 'Download the media of a collected folder, or check what is stored.'

DOWNLOAD_MEDIA_DESCRIPTION = (
	'Download the photos, videos, audio and documents attached to the saved messages of a '
	'collected folder into <output>/downloaded-media/, recorded in its media.sqlite3: every '
	'file of a selected type that is not stored yet, including files that failed or were '
	'skipped by a size limit before. Works in files, sqlite and both folders. It needs the '
	'profile that collected each channel, because file references belong to that account. '
	"Each channel's messages are fetched again for fresh file references (one request per "
	'100 messages, then one per 512 KiB of a file). No size limit unless --max-media-size or '
	'--max-media-total is given. Files are saved, never opened.'
)

DOWNLOAD_MEDIA_EPILOG = (
	'Exit codes: 0 every selected file is stored or skipped by a size limit (--dry-run: '
	'listed; --check: no problem found); 1 a file failed, a channel was refused or not found, '
	'the downloads stopped, or the folder cannot be read (--check: problems found, listed on '
	'stderr); 2 argument errors; 130 interrupted.'
)


def add_media_selection_arguments(group):
	'''
	--media-types, --max-media-size and --max-media-total.
	'''
	group.add_argument(
		'--media-types',
		type=media_types,
		metavar='TYPES',
		help='Only these kinds, separated by commas: image, video, audio, file '
			'(default: all).'
	)
	group.add_argument(
		'--max-media-size',
		type=media_size,
		metavar='SIZE',
		help='Skip a file larger than SIZE (bytes, or a number with K, M or G for '
			'KiB, MiB, GiB). A file of unknown size is stopped once it passes SIZE, '
			'and its partial copy is removed.'
	)
	group.add_argument(
		'--max-media-total',
		type=media_size,
		metavar='SIZE',
		help='Receive at most SIZE in this run, counting every byte received, '
			'restarts included. Files that do not fit are skipped and recorded.'
	)


def add_download_media_arguments(parser):
	'''
	`download-media` without the session and credential options, which
	the caller adds.
	'''
	targets = parser.add_mutually_exclusive_group()
	targets.add_argument(
		'--telegram-channel',
		type=str,
		help='Only the saved history of this channel (the name it was collected under). '
			'Default: every saved history in the folder.'
	)
	targets.add_argument(
		'--batch-file',
		type=str,
		help='Only the saved histories named in this file, one per line.'
	)
	parser.add_argument(
		'--output',
		'-o',
		type=str,
		required=False,
		help=f'The collected folder. Default: `{DEFAULT_OUTPUT}`'
	)
	parser.add_argument(
		'--source',
		choices=SOURCES,
		default=AUTO,
		help='Which saved histories to read: files, sqlite (the database '
			'telegram-tracker.sqlite3) or auto (default: the database of a folder '
			'collected with --storage sqlite, the files otherwise), as for the builders.'
	)

	modes = parser.add_mutually_exclusive_group()
	modes.add_argument(
		'--dry-run',
		action='store_true',
		help='Offline, no login: list per channel and type the files a run would request, '
			'with their known sizes, from the saved messages and the ledger. Session and '
			'request options are not used. Nothing is written.'
	)
	modes.add_argument(
		'--check',
		action='store_true',
		help='Offline, no login, read only: check that the ledger belongs to this folder, '
			'that each stored file has its recorded size and SHA-256, and that ledger items '
			'match the saved messages (an item whose message was edited and merged with other '
			'media is reported as superseded); count each saved reference by its state; and '
			'list files and records without a counterpart and leftover partial downloads. '
			'Holds the output lock (and the database lease) while it reads, like collect; '
			'nothing else is written. Takes only --output and --source.'
	)

	media = parser.add_argument_group(
		'media',
		'Which files to download (default: all four kinds, no size limit).'
	)
	add_media_selection_arguments(media)

	controls = parser.add_argument_group(
		'request controls',
		'Shared by the message lookups and the file requests after the account check. '
		'Login and the account check are outside these controls; Telethon may retry inside '
		'an attempt. These are project limits, not a safe rate guaranteed by Telegram.'
	)
	controls.add_argument(
		'--concurrency',
		type=bounded_int(1, 10),
		metavar='N',
		help=f'Files downloaded at once, 1-10; also the most requests in flight '
			f'(default: {DEFAULT_CONCURRENCY}).'
	)
	controls.add_argument(
		'--request-interval',
		type=bounded_float(0.1, 60),
		metavar='SECONDS',
		help=f'Minimum gap between policy attempts, 0.1-60 (default: {DEFAULT_INTERVAL}).'
	)
	controls.add_argument(
		'--max-flood-wait',
		type=bounded_int(0, 3600),
		metavar='SECONDS',
		help='Longest server flood wait to sit out, 0-3600; a longer one stops the run, '
			f'keeping the files stored. 0 stops on any flood wait (default: {DEFAULT_FLOOD_WAIT}).'
	)
	controls.add_argument(
		'--max-retries',
		type=bounded_int(0, 10),
		metavar='N',
		help='Additional attempts per request after a flood wait or a transient error, '
			f'0-10 (default: {DEFAULT_RETRIES}).'
	)
