# -*- coding: utf-8 -*-
'''
Dispatch for `main.py`, the `tgtracker` command, and `python -m
telegram_tracker`: the canonical subcommands and the legacy collection
form reach the collect and builder handlers.
'''

# import modules
import sys

# import local submodules
from telegram_tracker.cli.builders import run_builder
from telegram_tracker.cli.collect import run_collect
from telegram_tracker.cli.parsers import COLLECT, COMMAND_PROG, SCRIPT_PROG, build_legacy_parser, \
	build_parser, build_parsers, is_legacy_collection


def main(argv=None, *, prog=SCRIPT_PROG, client_factory=None, authorize=None):
	'''
	Parse `argv` (default: sys.argv[1:]) and run the command. Returns the
	exit code; argument errors raise SystemExit(2) before credentials are
	read.

	prog -> program name shown in help and errors
	client_factory / authorize replace the Telethon client and the login
	step for `collect` (tests use fakes). By default credentials come from
	config/config.ini and the session from ./session_file.session.
	'''
	argv = sys.argv[1:] if argv is None else list(argv)

	if not argv:
		build_parser(prog).print_help(sys.stderr)
		return 2

	if is_legacy_collection(argv):
		parser = build_legacy_parser(prog)
		args = vars(parser.parse_args(argv))
		return run_collect(
			args, parser, legacy=True,
			client_factory=client_factory, authorize=authorize
		)

	parser, subparsers = build_parsers(prog)
	args = vars(parser.parse_args(argv))
	command = args.pop('command')
	if command == COLLECT:
		return run_collect(
			args, subparsers[COLLECT], legacy=False,
			client_factory=client_factory, authorize=authorize
		)
	return run_builder(command, args)


def command_main():
	'''
	Entry point of the installed `tgtracker` command.
	'''
	return main(prog=COMMAND_PROG)
