# -*- coding: utf-8 -*-
'''
Build Graph.gexf and network.png from collected_chats.csv.

	python channels-to-network.py [--data-path/-d DIR]

Legacy form of `python main.py build-network`; same handler and exit
codes (telegram_tracker.cli.builders). Reads only local files.
'''

# import modules
import os
import sys

# runs without installation: import the package from this checkout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

# import local submodules
from telegram_tracker.cli import builder_main  # noqa: E402
from telegram_tracker.cli.parsers import BUILD_NETWORK  # noqa: E402


if __name__ == '__main__':
	raise SystemExit(builder_main(BUILD_NETWORK, prog='channels-to-network.py'))
