# -*- coding: utf-8 -*-
'''
Build Graph.gexf and network.png from collected_chats.csv.

	python channels-to-network.py [--data-path/-d DIR]

Legacy form of `python main.py build-network`; same handler and exit
codes (cli.py). Reads only local files.
'''

# import local submodules
from cli import BUILD_NETWORK, builder_main


if __name__ == '__main__':
	raise SystemExit(builder_main(BUILD_NETWORK, prog='channels-to-network.py'))
