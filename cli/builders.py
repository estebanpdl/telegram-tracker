# -*- coding: utf-8 -*-
'''
`build-dataset` and `build-network`: one handler for `main.py <command>`
and the legacy builder scripts. datasets and network are imported only
when a build runs.
'''

# import modules
import sys
import time
import warnings

# import local submodules
from cli.parsers import BUILD_DATASET, DEFAULT_OUTPUT, build_builder_parser
from utils import DataInputError, data_folder_from


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


def builder_main(command, argv=None, *, prog):
	'''
	Entry for the legacy builder scripts: their own `--data-path/-d`
	parser, the same handler as `main.py <command>`.
	'''
	parser = build_builder_parser(command, prog)
	args = vars(parser.parse_args(sys.argv[1:] if argv is None else argv))
	return run_builder(command, args)
