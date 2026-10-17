# -*- coding: utf-8 -*-
'''
Build msgs_dataset.csv and collected_chats.xlsx from collected data.

	python build-datasets.py [--data-path/-d DIR]

Legacy form of `python main.py build-dataset`; same handler and exit
codes (cli/builders.py). Reads only local files.
'''

# import local submodules
from cli import builder_main
from cli.parsers import BUILD_DATASET


if __name__ == '__main__':
	raise SystemExit(builder_main(BUILD_DATASET, prog='build-datasets.py'))
