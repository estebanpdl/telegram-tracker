# -*- coding: utf-8 -*-
'''
Real signals and process ends during `collect`, in child processes
(signal_child.py) with the fake client:

- SIGINT raised by the child itself while a history request is in flight
  or while a channel is being written (every platform);
- SIGINT sent by another process (POSIX);
- Ctrl+C typed on the child's terminal, a pseudo-terminal, during the run
  and at the confirmation prompt (POSIX);
- a killed process: its leases and lock stay behind, each refuses the next
  run and names the dead process until it is deleted, and the run after
  that records the killed run as interrupted (every platform).

An interrupted run exits 130 and prints "> Interrupted.", releases the
session lease, the account lease, the output lock and the database lease,
records the run as interrupted (stop_reason "cancelled"), keeps a channel
whose write had begun, and the next run completes. Windows has no simple
way for a test to send SIGINT or Ctrl+C to one chosen process without
signalling its own console, so those cases are skipped there.
'''

# import modules
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import storage_modes_support

from telegram_tracker.credentials import accounts
from telegram_tracker.credentials import home as homes
from telegram_tracker.storage.records import LOCK_FILE


TESTS = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(TESTS, 'signal_child.py')
TARGET = 'alpha_fx'
TIMEOUT = 120
POSIX_ONLY = unittest.skipIf(
	os.name == 'nt', 'Windows cannot send SIGINT or Ctrl+C to one chosen process from a test'
)
REMEDY = re.compile(r'delete (.+?)(?: and try again| to reuse it)')


class SignalCase(storage_modes_support.ModesCase):

	def setUp(self):
		super().setUp()
		self.session = os.environ[homes.SESSION_ENV]

	def start(self, storage, point, how, *extra):
		'''
		The child, with stdout piped and stderr in a file.
		'''
		errors = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
		self.addCleanup(errors.close)
		process = subprocess.Popen(
			[sys.executable, '-I', CHILD, self.out, storage, point, how, *extra],
			stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8'
		)
		process.errors = errors
		self.addCleanup(self.end, process)
		return process

	def end(self, process):
		if process.poll() is None:
			process.kill()
		process.communicate(timeout=TIMEOUT)

	def ready(self, process):
		'''
		The pid the child reported once stopped; earlier lines are kept.
		'''
		process.seen = []
		deadline = time.monotonic() + TIMEOUT
		while time.monotonic() < deadline:
			line = process.stdout.readline()
			if not line:
				break
			process.seen.append(line)
			if line.startswith('READY '):
				return int(line.split()[1])
		process.kill()
		process.errors.seek(0)
		self.fail(f'the child never stopped: {"".join(process.seen)}{process.errors.read()[-3000:]}')

	def finish(self, process):
		'''
		(exit code, all stdout, stderr) once the child ended. The rest of
		stdout is read through process.stdout: on POSIX communicate() reads
		the pipe itself and misses lines that ready()'s readline() buffered.
		'''
		watchdog = threading.Timer(TIMEOUT, process.kill)
		watchdog.start()
		try:
			out = process.stdout.read()
			process.wait()
		finally:
			watchdog.cancel()
		process.errors.seek(0)
		return process.returncode, ''.join(process.seen) + out, process.errors.read()

	def leftovers(self, storage):
		'''
		Lease and lock files that exist after a run.
		'''
		paths = [self.session + '.lock', os.path.join(self.out, LOCK_FILE)]
		if storage != 'files':
			paths.append(self.db() + '.lock')
		found = [p for p in paths if os.path.lexists(p)]
		leases = accounts.leases_dir(os.environ[homes.HOME_ENV])
		if os.path.isdir(leases):
			found += [os.path.join(leases, name) for name in os.listdir(leases)]
		return found

	def messages_file(self):
		return os.path.join(self.out, TARGET, f'{TARGET}_messages.json')

	def assert_interrupted(self, code, out, err, storage, *, written):
		self.assertEqual(code, 130, err)
		self.assertIn('EXIT 130', out)
		self.assertIn('> Interrupted.', err)
		self.assertNotIn('Traceback', err)
		self.assertEqual(self.leftovers(storage), [])
		status = self.status()
		self.assertEqual((status['state'], status['stop_reason']), ('interrupted', 'cancelled'))
		self.assertEqual(status['targets'][0]['status'], 'succeeded' if written else 'cancelled')
		if storage != 'sqlite':
			self.assertEqual(os.path.isfile(self.messages_file()), written)
		if storage != 'files':
			self.assertEqual(
				self.rows('SELECT state, stop_reason FROM collection_runs'), [('interrupted', 'cancelled')]
			)
			self.assertEqual(self.rows('SELECT count(*) FROM histories'), [(1 if written else 0,)])

	def assert_next_run_completes(self, storage):
		code, _, err = self.collect()
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.status()['state'], 'complete')
		self.assertEqual(self.leftovers(storage), [])
		if storage != 'files':
			self.assertEqual(
				self.rows('SELECT state FROM collection_runs ORDER BY run_id'), [('interrupted',), ('complete',)]
			)


class SelfRaisedSigintTests(SignalCase):
	'''
	The child raises SIGINT itself: the real signal path on every platform.
	'''

	def interrupt(self, storage, point):
		process = self.start(storage, point, 'self')
		self.ready(process)
		return self.finish(process)

	def test_during_a_request(self):
		for storage in ('files', 'sqlite', 'both'):
			with self.subTest(storage=storage):
				self.out = os.path.join(self.tmp, f'request-{storage}')
				code, out, err = self.interrupt(storage, 'request')
				self.assert_interrupted(code, out, err, storage, written=False)
				self.assert_next_run_completes(storage)

	def test_during_a_write(self):
		for storage in ('files', 'sqlite', 'both'):
			with self.subTest(storage=storage):
				self.out = os.path.join(self.tmp, f'write-{storage}')
				code, out, err = self.interrupt(storage, 'write')
				self.assertIn('SIGNAL HANDLED', out, err)
				self.assert_interrupted(code, out, err, storage, written=True)
				self.assert_next_run_completes(storage)


@POSIX_ONLY
class ExternalSigintTests(SignalCase):
	'''
	Another process sends SIGINT, as `kill -INT` does.
	'''

	def interrupt(self, storage, point):
		process = self.start(storage, point, 'wait')
		os.kill(self.ready(process), signal.SIGINT)
		return self.finish(process)

	def test_during_a_request(self):
		code, out, err = self.interrupt('both', 'request')
		self.assert_interrupted(code, out, err, 'both', written=False)
		self.assert_next_run_completes('both')

	def test_during_a_write(self):
		code, out, err = self.interrupt('both', 'write')
		self.assertIn('SIGNAL HANDLED', out, err)
		self.assert_interrupted(code, out, err, 'both', written=True)
		self.assert_next_run_completes('both')


@POSIX_ONLY
class TerminalCtrlCTests(SignalCase):
	'''
	Ctrl+C typed on the child's controlling terminal: the terminal sends
	SIGINT to its foreground process group.
	'''

	def start_on_terminal(self, *args):
		import fcntl
		import pty
		import termios

		master, slave = pty.openpty()

		def take_terminal():
			fcntl.ioctl(0, termios.TIOCSCTTY, 0)

		self.addCleanup(os.close, master)
		try:
			process = subprocess.Popen(
				[sys.executable, '-I', CHILD, self.out, *args],
				stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=take_terminal
			)
		finally:
			os.close(slave)
		self.addCleanup(lambda: process.poll() is not None or (process.kill(), process.wait(TIMEOUT)))
		return process, master

	def read_until(self, master, marker=None):
		'''
		Terminal output up to `marker`, or to the end when it is None.
		'''
		import select

		buffer = b''
		deadline = time.monotonic() + TIMEOUT
		while marker is None or marker not in buffer:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				self.fail(f'no {marker!r} on the terminal: {buffer[-3000:]!r}')
			readable, _, _ = select.select([master], [], [], remaining)
			if not readable:
				continue
			try:
				chunk = os.read(master, 4096)
			except OSError:
				# EIO: every process holding the terminal has ended
				chunk = b''
			if not chunk:
				if marker is None:
					break
				self.fail(f'the terminal closed before {marker!r}: {buffer[-3000:]!r}')
			buffer += chunk
		return buffer.decode('utf-8', 'replace')

	def test_during_a_request(self):
		process, master = self.start_on_terminal('both', 'request', 'wait')
		self.read_until(master, b'READY ')
		os.write(master, b'\x03')
		text = self.read_until(master)
		self.assertEqual(process.wait(TIMEOUT), 130)
		self.assertIn('EXIT 130', text)
		self.assertIn('> Interrupted.', text)
		self.assertNotIn('Traceback', text)
		self.assertEqual(self.leftovers('both'), [])
		self.assertEqual(self.status()['state'], 'interrupted')
		self.assertEqual(self.rows('SELECT state, stop_reason FROM collection_runs'), [('interrupted', 'cancelled')])
		self.assert_next_run_completes('both')

	def test_at_the_confirmation_prompt(self):
		self.assertEqual(self.collect()[0], 0)
		before = storage_modes_support.file_contents(self.out)
		process, master = self.start_on_terminal('files', 'prompt', 'wait', '--max-messages', '1')
		self.read_until(master, b'Continue? [y/N]')
		os.write(master, b'\x03')
		text = self.read_until(master)
		self.assertEqual(process.wait(TIMEOUT), 130)
		self.assertIn('> Interrupted.', text)
		self.assertNotIn('Traceback', text)
		self.assertEqual(storage_modes_support.file_contents(self.out), before)
		self.assertEqual(self.leftovers('files'), [])


class KilledProcessTests(SignalCase):
	'''
	A killed collection cannot clean up: every lease and lock it held stays,
	and each one refuses the next run, naming the dead process, until the
	user deletes the file the message names. None is removed automatically.
	'''

	def test_leftovers_refuse_until_deleted(self):
		process = self.start('both', 'request', 'wait')
		pid = self.ready(process)
		process.kill()
		process.communicate(timeout=TIMEOUT)
		self.assertNotEqual(process.returncode, 0)
		left = self.leftovers('both')
		self.assertEqual(len(left), 4, left)

		refusals = []
		for _ in range(len(left)):
			code, _, err = self.collect()
			self.assertEqual(code, 1, err)
			self.assertIn(f'(pid {pid},', err)
			named = REMEDY.search(err)
			self.assertIsNotNone(named, err)
			path = named.group(1)
			self.assertIn(path, left)
			refusals.append(path)
			os.remove(path)
		# the session lease, then the output lock and the database lease
		# (checked before connecting), then the account lease (after login)
		self.assertEqual(
			refusals[:3], [self.session + '.lock', os.path.join(self.out, LOCK_FILE), self.db() + '.lock']
		)
		self.assertEqual(os.path.dirname(refusals[3]), accounts.leases_dir(os.environ[homes.HOME_ENV]))

		code, _, err = self.collect()
		self.assertEqual(code, 0, err)
		self.assertIn('1 earlier run(s)', err)
		self.assertEqual(
			self.rows('SELECT state, substr(stop_reason, 1, 6) FROM collection_runs ORDER BY run_id'),
			[('interrupted', 'stale:'), ('complete', None)]
		)
		self.assertEqual(self.leftovers('both'), [])
