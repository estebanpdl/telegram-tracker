# -*- coding: utf-8 -*-
'''
Build msgs_dataset.csv and collected_chats.xlsx from collected data.

	python build-datasets.py [--data-path/-d DIR]

Thin wrapper around datasets.build_dataset; reads only local files.
'''

# import modules
import argparse
import sys
import time
import warnings


def main(argv=None):
	'''
	Returns the exit code: 0 on success, 1 when the input files are
	missing or malformed, 2 for argument errors.
	'''
	parser = argparse.ArgumentParser(description='Arguments.')
	parser.add_argument(
		'--data-path',
		'-d',
		type=str,
		required=False,
		help='Path where data is located. Will use `./output/data` if not given.'
	)
	args = vars(parser.parse_args(argv))

	from datasets import build_dataset
	from utils import DataInputError, data_folder_from

	print(f'\nInit program at {time.ctime()}\n\n')
	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			build_dataset(data_folder_from(args['data_path']))
	except DataInputError as exc:
		print(f'> build-dataset: {exc}', file=sys.stderr)
		return 1

	return 0


if __name__ == '__main__':
	raise SystemExit(main())
