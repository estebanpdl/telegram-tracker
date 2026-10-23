# -*- coding: utf-8 -*-
'''
Ctrl+C and a killed process while a media file downloads, in child
processes (media_signal_child.py) with the media fakes, for
`collect --include-media` and `download-media` in every storage mode.

- SIGINT (raised by the child itself, the real signal path that Ctrl+C
  takes, on every platform): the run exits 130 and releases every lease
  and lock; the download in flight fails as interrupted and its partial
  file is removed; the media run and the collection are recorded as
  interrupted (cancelled), the saved history stays, and the next
  download-media stores every file.
- A killed process: its partial file, its downloading item and its
  running media run stay, and so do its leases and lock (test_signals
  covers how those refuse the next run). --check reports what the run
  left without changing it; the next run that writes the ledger records
  the dead run as interrupted, removes its partial file and stores every
  file.
'''

# import modules
import os
import subprocess
import sys
import tempfile
import unittest

import source_path  # noqa: F401  (imports telegram_tracker from src)
import test_signals

from media_collect_support import ALPHA, MediaCase

from telegram_tracker.storage.records import LOCK_FILE


TESTS = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(TESTS, 'media_signal_child.py')
MODES = ('files', 'sqlite', 'both')
# the two-pull video the child stops in (alpha_fx's messages 2 and 6)
VIDEO = 7001
# alpha_fx's media items (message 6 holds the video of message 2 again)
ALPHA_ITEMS = [(ALPHA, 1), (ALPHA, 2), (ALPHA, 4), (ALPHA, 5), (ALPHA, 6)]


class MediaSignalCase(test_signals.SignalCase, MediaCase):

	def start(self, storage, command, how):
		'''
		The child, with stdout piped and stderr in a file.
		'''
		errors = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
		self.addCleanup(errors.close)
		process = subprocess.Popen(
			[sys.executable, '-I', CHILD, self.out, storage, command, how],
			stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8'
		)
		process.errors = errors
		self.addCleanup(self.end, process)
		return process

	def prepare(self, storage, command):
		'''
		A fresh output folder; download-media needs a collected one.
		'''
		self.out = os.path.join(self.tmp, f'{command}-{storage}')
		if command == 'download-media':
			code, _, err = self.collect(storage=storage)
			self.assertEqual(code, 0, err)

	def media_run(self, command):
		run, = [run for run in self.runs() if run['command'] == command]
		return run

	def video_items(self):
		'''
		{(channel, message): (state, reason)} of the items holding the
		video: the download in flight (jobs go newest message first) and
		the other message with the same video, which waits for it.
		'''
		return {
			(i['channel_id'], i['message_id']): (i['state'], i['reason'])
			for i in self.items() if i['media_id'] == VIDEO
		}

	def assertAllStored(self):
		self.assertEqual(self.states(), {key: ('complete', None) for key in ALPHA_ITEMS})
		self.assertEqual(self.partial_files(), [])


class InterruptTests(MediaSignalCase):

	def interrupt(self, storage, command):
		self.prepare(storage, command)
		process = self.start(storage, command, 'self')
		self.ready(process)
		return self.finish(process)

	def assert_media_interrupted(self, command):
		run = self.media_run(command)
		self.assertEqual((run['state'], run['stop_reason']), ('interrupted', 'cancelled'))
		self.assertIn(('failed', 'interrupted'), self.video_items().values())
		self.assertNotIn('downloading', {state for state, _ in self.states().values()})
		self.assertEqual(self.partial_files(), [])
		# nothing of the video was published
		self.assertFalse([path for path in self.media_files() if path.startswith(f'{ALPHA}/videos/')])

	def test_ctrl_c_while_collect_downloads(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				code, out, err = self.interrupt(storage, 'collect')
				# the history was committed before the downloads began
				self.assert_interrupted(code, out, err, storage, written=True)
				self.assertEqual(self.status()['media']['state'], 'interrupted')
				self.assert_media_interrupted('collect')

				code, _, err = self.download()
				self.assertEqual(code, 0, err)
				self.assertAllStored()
				self.assertEqual(self.leftovers(storage), [])

	def test_ctrl_c_while_download_media_downloads(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				code, out, err = self.interrupt(storage, 'download-media')
				self.assertEqual(code, 130, err)
				self.assertIn('EXIT 130', out)
				self.assertIn('> Interrupted.', err)
				self.assertNotIn('Traceback', err)
				self.assertEqual(self.leftovers(storage), [])
				# the collection is not changed
				self.assertEqual(self.status()['state'], 'complete')
				self.assert_media_interrupted('download-media')

				code, _, err = self.download()
				self.assertEqual(code, 0, err)
				self.assertAllStored()


class KilledProcessTests(MediaSignalCase):

	def kill(self, storage, command):
		self.prepare(storage, command)
		process = self.start(storage, command, 'wait')
		self.ready(process)
		process.kill()
		process.communicate(timeout=test_signals.TIMEOUT)
		# the leases and the lock stay; deleting them is the user's step
		left = self.leftovers(storage)
		for path in left:
			os.remove(path)
		self.assertNotEqual(process.returncode, 0)
		self.assertIn(os.path.join(self.out, LOCK_FILE), left)
		# what a killed run leaves of its media
		run = self.media_run(command)
		self.assertEqual(run['state'], 'running')
		self.assertIn(('downloading', None), self.video_items().values())
		self.assertEqual(len(self.partial_files()), 1, self.partial_files())
		self.assertTrue(self.partial_files()[0].startswith(f'{run["run_uuid"]}/'))
		return run

	def assert_recovered(self, run, err):
		self.assertIn('1 earlier media run(s) never finished', err)
		self.assertIn('removed 1 partial download(s) of earlier runs', err)
		dead = {r['run_uuid']: r for r in self.runs()}[run['run_uuid']]
		self.assertEqual(dead['state'], 'interrupted')
		self.assertAllStored()

	def test_a_killed_download_media_is_checked_then_recovered(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				run = self.kill(storage, 'download-media')
				ledger = self.ledger_rows('SELECT * FROM items ORDER BY channel_id, message_id')

				code, stdout, err = self.download('--check')
				self.assertEqual(code, 1, err)
				report = stdout + err
				for problem in (
						'runs recorded as running', 'items still recorded as downloading',
						'leftover partial downloads'):
					self.assertIn(problem, report)
				# read only
				self.assertEqual(self.ledger_rows('SELECT * FROM items ORDER BY channel_id, message_id'), ledger)
				self.assertEqual(len(self.partial_files()), 1)

				code, _, err = self.download()
				self.assertEqual(code, 0, err)
				self.assert_recovered(run, err)

	def test_a_killed_collect_is_recovered_by_the_next_collect(self):
		for storage in MODES:
			with self.subTest(storage=storage):
				run = self.kill(storage, 'collect')
				code, _, err = self.media('--yes')
				self.assertEqual(code, 0, err)
				self.assert_recovered(run, err)


if __name__ == '__main__':
	unittest.main()
