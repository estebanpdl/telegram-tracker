# -*- coding: utf-8 -*-
'''
Build Graph.gexf and network.png from collected_chats.csv.

	python channels-to-network.py [--data-path/-d DIR]

Thin wrapper around network.build_network; reads only local files.
'''

# import modules
import argparse
import sys
import time
import warnings


def main(argv=None):
	'''
	Returns the exit code: 0 on success, 1 when the input file is missing
	or malformed, 2 for argument errors.
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

	from network import build_network
	from utils import DataInputError, data_folder_from

	print(f'\nInit program at {time.ctime()}\n\n')
	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			build_network(data_folder_from(args['data_path']))
	except DataInputError as exc:
		print(f'> build-network: {exc}', file=sys.stderr)
		return 1

	return 0


if __name__ == '__main__':
	raise SystemExit(main())
