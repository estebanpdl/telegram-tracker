# -*- coding: utf-8 -*-
'''
Child process for the dual-sink crash test: collect one fixture channel
with --storage both into OUT and end the process abruptly (os._exit)
right after the channel's files are published and before files_state is
marked written. Offline: the guard is installed and the client is the
fake.

	python dual_crash_child.py OUT
'''

# import modules
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient

from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.collector import run_collection
from telegram_tracker.storage.destination import select_destination
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


def main(out):
	offline_guard.install()
	destination = select_destination(out, 'both')
	client = FakeTelegramClient(page_size=2)

	def crash(self, index):
		print('crashing', flush=True)
		os._exit(9)

	SQLiteWriter.mark_target_written = crash
	asyncio.run(run_collection(
		['beta_fx'], out,
		client_factory=lambda: client,
		authorize=lambda c: ensure_authorized(c, None),
		request_interval=0.1,
		writer_factory=destination.open_writer,
		preflight=destination.check_unlocked
	))
	return 0


if __name__ == '__main__':
	sys.exit(main(sys.argv[1]))
