# -*- coding: utf-8 -*-
'''
`python -m telegram_tracker`: the same commands as `tgtracker`.
'''

# import local submodules
from telegram_tracker.cli import main
from telegram_tracker.cli.parsers import MODULE_PROG


if __name__ == '__main__':
	raise SystemExit(main(prog=MODULE_PROG))
