# -*- coding: utf-8 -*-
"""Synthetic fixtures and CLI helpers for history-window confirmation tests."""

import io
import os
from unittest import mock

import offline_guard
import storage_modes_support
from storage_modes_support import read_json
from telegram_tracker.cli import collect as collect_cli


MODES = ('files', 'sqlite', 'both')

# keeps only the newest message, so a replaced history is visible
WINDOW = ('--max-messages', '1')

PROMPT = 'Continue? [y/N]'
REFUSED = 'Pass --yes to replace them, or choose a fresh --output.'
DECLINED = '> Nothing collected; the saved histories are unchanged.'


class Terminal(io.StringIO):
	'''
	Standard input that is a terminal holding the given answer.
	'''
	def isatty(self):
		return True


class Unasked(Terminal):
	'''
	A terminal that fails the test when the run reads an answer.
	'''
	def readline(self, *args):
		raise AssertionError('the run asked a question')


class Interrupting(Terminal):
	'''
	A terminal where Ctrl+C is pressed at the question.
	'''
	def readline(self, *args):
		raise KeyboardInterrupt


class ClosedInput(io.StringIO):
	def isatty(self):
		raise ValueError('I/O operation on closed file')


def snapshot(folder):
	'''
	{relative path: bytes} of every file under `folder`.
	'''
	found = {}
	for name in storage_modes_support.listing(folder):
		with open(os.path.join(folder, name), 'rb') as fh:
			found[name] = fh.read()
	return found


def home_listing():
	'''
	Every folder and file in the application home, with file sizes.
	'''
	home = os.environ[offline_guard.HOME_ENV]
	found = []
	for root, folders, files in os.walk(home):
		for name in folders:
			found.append((os.path.relpath(os.path.join(root, name), home), None))
		for name in files:
			path = os.path.join(root, name)
			found.append((os.path.relpath(path, home), os.path.getsize(path)))
	return sorted(found)


class ConfirmCase(storage_modes_support.ModesCase):
	'''
	ModesCase with an application home of its own, so a run that stops
	before connecting can be shown to leave it as it was.
	'''

	def setUp(self):
		super().setUp()
		home = os.path.join(self.tmp, 'home')
		os.mkdir(home)
		environ = mock.patch.dict(os.environ, {
			offline_guard.HOME_ENV: home,
			offline_guard.SESSION_ENV: os.path.join(home, 'session_file.session')
		})
		environ.start()
		self.addCleanup(environ.stop)

		# session leases taken by collect
		self.leases = 0
		real_lease = collect_cli.Lease

		def counting_lease(*args, **kwargs):
			self.leases += 1
			return real_lease(*args, **kwargs)

		lease = mock.patch.object(collect_cli, 'Lease', counting_lease)
		lease.start()
		self.addCleanup(lease.stop)

	def folder(self, name):
		return os.path.join(self.tmp, name)

	def save(self, storage, out, targets=('alpha_fx',), **kwargs):
		'''
		A full run: the saved histories a window run would replace.
		'''
		code, _, err = self.collect(targets=targets, storage=storage, out=out, **kwargs)
		self.assertEqual(code, 0 if 'missing_fx' not in targets else 1, err)

	def window(self, stdin, out, *extra, targets=('alpha_fx',), **kwargs):
		'''
		A window run with `stdin` as standard input; the folder keeps its
		recorded mode.
		'''
		with mock.patch('sys.stdin', stdin):
			return self.collect(*WINDOW, *extra, targets=targets, out=out, **kwargs)

	def history_size(self, storage, out, channel='alpha_fx', channel_id=101):
		if storage == 'sqlite':
			return self.rows('SELECT count(*) FROM messages WHERE channel_id = ?', channel_id, out=out)[0][0]
		path = os.path.join(out, channel, f'{channel}_messages.json')
		return len(read_json(path)['messages'])

	def state(self, out):
		'''
		What a run that stops before connecting leaves as it was: the
		output folder, the application home, clients created and session
		leases taken.
		'''
		return snapshot(out), home_listing(), self.factory_calls, self.leases

	def assertStoppedBeforeConnecting(self, out, before):
		self.assertEqual(self.state(out), before)
