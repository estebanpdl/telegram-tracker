# -*- coding: utf-8 -*-
'''
Telegram Tracker entry point for a source checkout; the same commands as
the installed `tgtracker` command.

	python main.py collect --telegram-channel NAME [options]
	python main.py collect --batch-file FILE [options]
	python main.py build-dataset [--data-path DIR]
	python main.py build-network [--data-path DIR]
	python main.py build-urls [--data-path DIR]

The legacy collection form `python main.py --telegram-channel NAME ...`
(or `--batch-file FILE`) keeps working. The parser, validation, and
dispatch live in telegram_tracker.cli; importing this module only puts
this checkout's src directory first on sys.path.
'''

# import modules
import os
import sys

# runs without installation: import the package from this checkout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

# import local submodules
from telegram_tracker.cli import main  # noqa: E402


if __name__ == '__main__':
	raise SystemExit(main())
