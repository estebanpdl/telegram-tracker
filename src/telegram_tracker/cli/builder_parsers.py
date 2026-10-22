# -*- coding: utf-8 -*-
'''
Shared data-folder default and builder argument parsers. Parsing loads
only the standard library; it reads no collection or credential files.
'''

import argparse


DEFAULT_OUTPUT = './output/data'

# Mirror reader.records without loading storage or reader modules.
AUTO = 'auto'
SOURCES = (AUTO, 'files', 'sqlite')


def add_builder_arguments(parser):
	parser.add_argument(
		'--data-path',
		'-d',
		type=str,
		required=False,
		help=f'Path where data is located. Will use `{DEFAULT_OUTPUT}` if not given.'
	)
	parser.add_argument(
		'--source',
		choices=SOURCES,
		default=AUTO,
		help='What to read: files (the file outputs), sqlite (the database '
			'telegram-tracker.sqlite3) or auto (default: the database of a folder '
			'collected with --storage sqlite, the files otherwise). The two are '
			'never combined; a source the folder lacks is refused.'
	)


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
