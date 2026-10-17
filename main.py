# -*- coding: utf-8 -*-
'''
Telegram Tracker entry point.

	python main.py collect --telegram-channel NAME [options]
	python main.py collect --batch-file FILE [options]
	python main.py build-dataset [--data-path DIR]
	python main.py build-network [--data-path DIR]

The legacy collection form `python main.py --telegram-channel NAME ...`
(or `--batch-file FILE`) keeps working. The parser, validation, and
dispatch live in the cli package; importing this module does nothing.
'''

# import local submodules
from cli import main


if __name__ == '__main__':
	raise SystemExit(main())
