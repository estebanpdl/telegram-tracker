# -*- coding: utf-8 -*-
'''
Process-wide offline guard for the test suite.

Importing this module installs the guard once. It blocks, and records:
	- outbound socket connections (sync and asyncio) and DNS lookups;
	- construction of a real Telethon client and its auth/connect methods;
	- any open of `config.ini` or Telethon `.session` files, through
	  `open`, `io.open`, `os.open`, and `sqlite3.connect`.

Loopback connections made inside `socket.socketpair()` stay allowed: on
Windows, asyncio builds its event-loop self-pipe that way.

Every blocked attempt is appended to `VIOLATIONS` even when the code under
test swallows the exception, so `OfflineTestCase` fails the test anyway.
'''

# import modules
import asyncio
import asyncio.base_events
import asyncio.proactor_events
import asyncio.selector_events
import builtins
import contextlib
import io
import os
import socket
import sqlite3
import threading
import unittest


class OfflineViolation(RuntimeError):
	'''
	Raised when code under test attempts network, auth, or secret access.
	'''


VIOLATIONS = []

_PROTECTED_NAMES = ('config.ini',)
_PROTECTED_SUFFIXES = ('.session', '.session-journal')

_state = threading.local()
_installed = False
_guards = {}


def _violation(kind, detail):
	'''
	Record and raise a violation.
	'''
	message = f'{kind}: {detail}'
	VIOLATIONS.append(message)
	raise OfflineViolation(message)


def _is_protected(path):
	'''
	True when `path` names a credentials config or a Telethon session file.
	'''
	try:
		value = os.fspath(path)
	except TypeError:
		return False

	if isinstance(value, bytes):
		value = os.fsdecode(value)

	name = os.path.basename(value).lower()
	return name in _PROTECTED_NAMES or name.endswith(_PROTECTED_SUFFIXES)


def _wrap_file_opener(real, label):
	'''
	Return an opener that refuses protected paths.
	'''
	def guarded(file, *args, **kwargs):
		if _is_protected(file):
			_violation('file', f'{label} of protected file {os.path.basename(os.fspath(file))!r}')
		return real(file, *args, **kwargs)

	guarded.__wrapped__ = real
	return guarded


def _blocked(label):
	'''
	Return a callable (sync) that always records a network violation.
	'''
	def blocked(*args, **kwargs):
		_violation('network', label)

	return blocked


def _blocked_async(label):
	'''
	Return a coroutine function that always records a network violation.
	'''
	async def blocked(*args, **kwargs):
		_violation('network', label)

	return blocked


def _install_socket_guards():
	'''
	Block sync socket connects except loopback inside socketpair().
	'''
	real_connect = socket.socket.connect
	real_connect_ex = socket.socket.connect_ex
	real_socketpair = socket.socketpair

	def allowed():
		return getattr(_state, 'socketpair_depth', 0) > 0

	def guarded_connect(self, address):
		if allowed():
			return real_connect(self, address)
		_violation('network', f'socket.connect to {address!r}')

	def guarded_connect_ex(self, address):
		if allowed():
			return real_connect_ex(self, address)
		_violation('network', f'socket.connect_ex to {address!r}')

	def guarded_socketpair(*args, **kwargs):
		_state.socketpair_depth = getattr(_state, 'socketpair_depth', 0) + 1
		try:
			return real_socketpair(*args, **kwargs)
		finally:
			_state.socketpair_depth -= 1

	socket.socket.connect = guarded_connect
	socket.socket.connect_ex = guarded_connect_ex
	socket.socketpair = guarded_socketpair
	socket.create_connection = _blocked('socket.create_connection')
	socket.getaddrinfo = _blocked('socket.getaddrinfo')
	_guards['socket.connect'] = guarded_connect


def _install_asyncio_guards():
	'''
	Block asyncio connection helpers; proactor connects bypass socket.connect.
	'''
	for cls in (
		asyncio.base_events.BaseEventLoop,
		asyncio.selector_events.BaseSelectorEventLoop,
		asyncio.proactor_events.BaseProactorEventLoop,
	):
		for name in ('create_connection', 'create_datagram_endpoint', 'sock_connect'):
			if name in vars(cls):
				setattr(cls, name, _blocked_async(f'{cls.__name__}.{name}'))

	_guards['asyncio.create_connection'] = asyncio.base_events.BaseEventLoop.create_connection


def _install_telethon_guards():
	'''
	Make a real Telethon client unusable: construction or auth is a violation.
	'''
	from telethon import TelegramClient

	def blocked_init(self, *args, **kwargs):
		_violation('telethon', 'real TelegramClient constructed')

	TelegramClient.__init__ = blocked_init
	for name in ('connect', 'send_code_request', 'sign_in', 'sign_up', 'start'):
		setattr(TelegramClient, name, _blocked(f'TelegramClient.{name}'))

	_guards['telethon.init'] = blocked_init


def _install_file_guards():
	'''
	Refuse config/session paths through every common opener.
	'''
	guarded_open = _wrap_file_opener(builtins.open, 'open')
	builtins.open = guarded_open
	io.open = guarded_open
	os.open = _wrap_file_opener(os.open, 'os.open')
	sqlite3.connect = _wrap_file_opener(sqlite3.connect, 'sqlite3.connect')
	_guards['open'] = guarded_open


def install():
	'''
	Install all guards once per process.
	'''
	global _installed
	if _installed:
		return

	_install_socket_guards()
	_install_asyncio_guards()
	_install_telethon_guards()
	_install_file_guards()
	_installed = True


def is_installed():
	'''
	True when every guard is still in place (nothing restored the originals).
	'''
	from telethon import TelegramClient

	return (
		_installed
		and socket.socket.connect is _guards['socket.connect']
		and asyncio.base_events.BaseEventLoop.create_connection
			is _guards['asyncio.create_connection']
		and TelegramClient.__init__ is _guards['telethon.init']
		and builtins.open is _guards['open']
	)


@contextlib.contextmanager
def expected_violations():
	'''
	Collect violations raised inside the block so a test can assert on them
	without failing the post-test violation check.
	'''
	start = len(VIOLATIONS)
	caught = []
	try:
		yield caught
	finally:
		caught.extend(VIOLATIONS[start:])
		del VIOLATIONS[start:]


class _OfflineChecks:
	'''
	Fail a test when the guard is missing or a violation was recorded.
	'''
	def _start_offline_checks(self):
		self.assertTrue(is_installed(), 'offline guard is not installed')
		start = len(VIOLATIONS)

		def check():
			new = VIOLATIONS[start:]
			del VIOLATIONS[start:]
			if new:
				self.fail(f'offline guard violations: {new}')

		self.addCleanup(check)


class OfflineTestCase(_OfflineChecks, unittest.TestCase):
	'''
	Synchronous test case running under the offline guard.
	'''
	def setUp(self):
		super().setUp()
		self._start_offline_checks()


class OfflineAsyncTestCase(_OfflineChecks, unittest.IsolatedAsyncioTestCase):
	'''
	Async test case running under the offline guard.
	'''
	def setUp(self):
		super().setUp()
		self._start_offline_checks()


install()
