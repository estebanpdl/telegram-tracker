# -*- coding: utf-8 -*-
'''
Child process for the media signal tests: `collect --include-media` of
alpha_fx, or `download-media`, through cli.main into OUT with the media
fakes (media_collect_support.standard_world). It stops inside the second
pull of the two-pull video, so the first pull's bytes are in its partial
file, prints "READY <pid>", then
	- HOW=self: raises SIGINT in this process, the real signal path that
	  Ctrl+C takes (see signal_child.py);
	- HOW=wait: waits for the parent, which kills the process.
Prints "EXIT <code>" when cli.main returns. Offline: the guard is
installed and the client is the fake.

	python media_signal_child.py OUT STORAGE COMMAND HOW
'''

# import modules
import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import offline_guard  # noqa: F401  (installs the guard)
import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_collect_support import KIB, standard_world

from telegram_tracker import cli
from telegram_tracker.api.auth import ensure_authorized


TARGET = 'alpha_fx'
# upper bound on a stop; the parent acts well within it
HOLD = 60
# the video of alpha_fx's message 2 and the offset of its second pull
VIDEO = 7001
SECOND_PULL = 512 * KIB


def main(out, storage, command, how):
	client = standard_world()

	async def stop(request):
		print('READY', os.getpid(), flush=True)
		if how == 'self':
			signal.raise_signal(signal.SIGINT)
		await asyncio.sleep(HOLD)

	client.files.fault(lambda r: r.kind == 'GetFile' and r.media_id == VIDEO and r.offset == SECOND_PULL, stop)
	if command == 'collect':
		argv = ['collect', '--include-media', '--telegram-channel', TARGET, '-o', out, '--storage', storage]
	else:
		argv = ['download-media', '-o', out]
	code = cli.main(argv, client_factory=lambda: client, authorize=lambda c: ensure_authorized(c, None))
	print('EXIT', code, flush=True)
	return code


if __name__ == '__main__':
	sys.exit(main(*sys.argv[1:]))
