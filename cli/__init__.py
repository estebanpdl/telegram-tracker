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
import sys

# import local submodules
from cli.builders import builder_main, run_builder
from cli.collect import run_collect
from cli.parsers import COLLECT, build_legacy_parser, build_parser, build_parsers, \
	is_legacy_collection


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
