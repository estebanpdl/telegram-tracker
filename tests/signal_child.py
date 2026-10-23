# -*- coding: utf-8 -*-
'''
Child process for the signal tests: `collect` one fixture channel through
cli.main into OUT, stop at POINT, print "READY <pid>" (the pid of this
interpreter, which may differ from the pid of the process the parent
started), then
	- HOW=self: raise SIGINT in this process. This is a real signal: the C
	  runtime delivers it to the handler asyncio.run installed, as for
	  Ctrl+C. It is the only real SIGINT a test can deliver on Windows.
	- HOW=wait: wait for the parent, which sends SIGINT, presses Ctrl+C on
	  the child's terminal, or kills the process.
POINT is `request` (a history request is in flight) or `write` (inside
the writer's commit of the channel, a synchronous step that a signal
must not cut short), or `prompt` (no stop: extra arguments make collect
ask for confirmation). Prints "EXIT <code>" when cli.main returns.
Offline: the guard is installed and the client is the fake.

	python signal_child.py OUT STORAGE POINT HOW [collect arguments...]
'''

# import modules
import asyncio
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import offline_guard  # noqa: F401  (installs the guard)
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient

from telegram_tracker import cli
from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.storage.dual import DualWriter
from telegram_tracker.storage.sqlite.writer import SQLiteWriter
from telegram_tracker.storage.writer import OutputWriter


TARGET = 'alpha_fx'
# upper bound on a stop; the parent acts well within it
HOLD = 60
WRITERS = {'files': OutputWriter, 'sqlite': SQLiteWriter, 'both': DualWriter}


def ready(how):
	print('READY', os.getpid(), flush=True)
	if how == 'self':
		signal.raise_signal(signal.SIGINT)


def run_task():
	'''
	The task asyncio.run is running (run_collection); its cancelling()
	count rises when asyncio's SIGINT handler has run.
	'''
	for task in asyncio.all_tasks():
		if task.get_coro().__qualname__ == 'run_collection':
			return task
	raise RuntimeError('run_collection is not running')


def stop_in_request(client, how):
	async def block():
		ready(how)
		await asyncio.sleep(HOLD)

	client.script('GetHistoryRequest', block)


def stop_in_write(storage, how):
	writer = WRITERS[storage]
	commit = writer.commit

	def blocking_commit(self, result, *args, **kwargs):
		task = run_task()
		ready(how)
		# Synchronous: the signal handler runs between these sleeps, and the
		# commit then completes before the cancellation is delivered.
		deadline = time.monotonic() + HOLD
		while not task.cancelling() and time.monotonic() < deadline:
			time.sleep(0.05)
		print('SIGNAL HANDLED' if task.cancelling() else 'NO SIGNAL', flush=True)
		return commit(self, result, *args, **kwargs)

	writer.commit = blocking_commit


def main(out, storage, point, how, *extra):
	client = FakeTelegramClient(page_size=2)
	if point == 'request':
		stop_in_request(client, how)
	elif point == 'write':
		stop_in_write(storage, how)
	code = cli.main(
		['collect', '--telegram-channel', TARGET, '-o', out, '--storage', storage, *extra],
		client_factory=lambda: client,
		authorize=lambda c: ensure_authorized(c, None)
	)
	print('EXIT', code, flush=True)
	return code


if __name__ == '__main__':
	sys.exit(main(*sys.argv[1:]))
