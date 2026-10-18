# -*- coding: utf-8 -*-
'''
Puts the repository's src directory first on sys.path, so the tests import
the telegram_tracker package from this checkout, not an installed copy.
Test modules import this before any telegram_tracker module.
'''

# import modules
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')

if sys.path[:1] != [SRC]:
	sys.path.insert(0, SRC)
