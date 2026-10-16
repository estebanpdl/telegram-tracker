# -*- coding: utf-8 -*-
'''
Collect Telegram channel data.

	python main.py --telegram-channel NAME [options]
	python main.py --batch-file FILE [options]

`main()` parses arguments, reads the batch file, loads credentials from
config/config.ini, and runs one asynchronous collection (collector.py). An
unauthorized session logs in interactively: a login code, then the
two-step verification password when the account has one.

Importing this module does nothing; only `python main.py` runs it.
'''

# import modules
import argparse
import asyncio
import functools
import sys
import time
import warnings

# import local submodules
from utils import cmd_request_type


SESSION_FILE = 'session_file'
DEFAULT_OUTPUT = './output/data'


'''

Arguments

'''

def build_parser():
	'''
	Collection arguments. Exactly one of --telegram-channel or --batch-file
	is required.
	'''
	parser = argparse.ArgumentParser(description='Arguments.')
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

	'''

	Output
	'''
	parser.add_argument(
		'--output',
		'-o',
		type=str,
		required=False,
		help='Folder to save collected data. Default: `./output/data`'
	)

	'''

	Updating data
	'''
	parser.add_argument(
		'--min-id',
		type=int,
		help='Specifies the offset id. This will update Telegram data with new posts.'
	)

	return parser


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


def output_folder_from(output):
	'''
	Output folder without a trailing slash; ./output/data by default.
	'''
	if not output:
		return DEFAULT_OUTPUT

	return output[:-1] if output.endswith('/') else output


'''

Entry point

'''

def main(argv=None, *, client_factory=None, authorize=None):
	'''
	Run a collection. Returns the exit code: 0 when every target was
	collected, 1 when a target or the login failed, 130 when interrupted.
	Argument errors exit with 2 before credentials are read.

	client_factory / authorize replace the Telethon client and the login
	step (tests use fakes). By default credentials come from
	config/config.ini and the session from ./session_file.session.
	'''
	parser = build_parser()
	args = vars(parser.parse_args(argv))
	targets = read_targets(args, parser)
	output_folder = output_folder_from(args['output'])

	from api.auth import AuthenticationError, ensure_authorized
	from collector import run_collection

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

	print(f'\nInit program at {time.ctime()}\n\n')

	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			summary = asyncio.run(
				run_collection(
					targets,
					output_folder,
					client_factory=client_factory,
					authorize=authorize,
					metadata_only=args['limit_download_to_channel_metadata'],
					min_id=args['min_id'] or 0
				)
			)
	except AuthenticationError as exc:
		print(f'> Login failed: {exc}', file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print('> Interrupted.', file=sys.stderr)
		return 130

	print(f'\nEnd program at {time.ctime()}\n\n')

	if summary.failed:
		print(
			f'> {len(summary.failed)} of {len(summary.requested)} targets failed: '
			+ ', '.join(target for target, _ in summary.failed),
			file=sys.stderr
		)
		return 1

	return 0


if __name__ == '__main__':
	raise SystemExit(main())
