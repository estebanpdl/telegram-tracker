# -*- coding: utf-8 -*-
'''
Command-line interface for Telegram Tracker.

	tgtracker collect --telegram-channel NAME [options]
	tgtracker collect --batch-file FILE [options]
	tgtracker build-dataset [--data-path DIR]
	tgtracker build-network [--data-path DIR]
	tgtracker build-urls [--data-path DIR]
	tgtracker download-media [--output DIR] [--dry-run | --check] [options]

`python -m telegram_tracker` and, from a source checkout, `python main.py`
take the same arguments. Legacy forms keep working and reach the same
handlers:

	python main.py --telegram-channel NAME | --batch-file FILE [...]
	python build-datasets.py [--data-path DIR]
	python channels-to-network.py [--data-path DIR]

Arguments are parsed and validated before any credentials are read or
services imported; help never needs Telethon, pandas, or plotting
libraries. Exit codes: 0 success, 1 failed targets / login / input data /
missing dependency, 2 argument errors, 130 interrupted.
'''

# import local submodules
from telegram_tracker.cli.builders import builder_main
from telegram_tracker.cli.dispatch import command_main, main
