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

# import local submodules
from cli.builders import builder_main
from cli.dispatch import main
