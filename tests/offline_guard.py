# -*- coding: utf-8 -*-
'''
Process-wide offline guard for the test suite.

Importing this module installs the guard once. It blocks, and records:
	- outbound socket connections (sync and asyncio) and DNS lookups;
	- construction of a real Telethon client and its auth/connect methods;
	- any open of `config.ini` or Telethon `.session` files, through
	  `open`, `io.open`, `os.open`, and `sqlite3.connect`;
	- any access to the user's real application home
	  (`~/.telegram_tracker`) or anything below it, also through relative,
	  case, `..`, `file:` URI, and symlink/junction aliases: opens, stat
	  and existence checks, listing, directory creation and removal,
	  renames, permission changes, and SQLite connections. This covers
	  its profile folders (`profiles/<name>/` with their credentials,
	  sessions, and profile.json) and `account-leases/`.

The real application home is captured from `~` when the guard is
installed, before any test changes the environment. The guard then points
TELEGRAM_TRACKER_HOME (inherited by subprocesses) at a temporary directory
and TELEGRAM_TRACKER_SESSION at a session path inside it, and removes
TELEGRAM_TRACKER_PROFILE, so code under test never resolves the real
home, a session in the working directory, or a profile selected in the
environment the suite was started from.

Loopback connections made inside `socket.socketpair()` stay allowed: on
Windows, asyncio builds its event-loop self-pipe that way.

The guard also makes the temporary directory canonical (links and short
names resolved) for this process and its subprocesses, so temporary paths
compare equal to the resolved paths the code under test reports.

Every blocked attempt is appended to `VIOLATIONS` even when the code under
test swallows the exception, so `OfflineTestCase` fails the test anyway.

Installing the guard also installs the opt-in sync switch (`no_sync`),
which is off unless TGTRACKER_TEST_NO_SYNC=1.
'''

# import modules
import asyncio
import asyncio.base_events
import asyncio.proactor_events
import asyncio.selector_events
import atexit
import builtins
import contextlib
import io
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unicodedata
import unittest

from urllib.parse import unquote

import no_sync
from virtual_time import VirtualTimeLoop


class OfflineViolation(RuntimeError):
	'''
	Raised when code under test attempts network, auth, or secret access.
	'''


VIOLATIONS = []

_PROTECTED_NAMES = ('config.ini',)
_PROTECTED_SUFFIXES = ('.session', '.session-journal')

HOME_ENV = 'TELEGRAM_TRACKER_HOME'
SESSION_ENV = 'TELEGRAM_TRACKER_SESSION'
PROFILE_ENV = 'TELEGRAM_TRACKER_PROFILE'
APP_DIR = '.telegram_tracker'

_state = threading.local()
_installed = False
_guards = {}

# originals used by the home check itself (never the wrapped versions)
_abspath = os.path.abspath
_normcase = os.path.normcase
_lstat = os.lstat
_readlink = os.readlink

# link hops followed when resolving an alias
_MAX_HOPS = 40

# normalized identities of the real application home, set by install()
_home_identities = ()


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


def _fold(path):
	'''
	The form in which paths are compared with the home: normcase, and on
	POSIX also case-folded and NFC-normalized, because macOS file systems
	are usually case- and normalization-insensitive while normcase changes
	nothing there. On a case-sensitive file system this can only refuse
	more paths, never fewer.
	'''
	path = _normcase(path)
	if os.name != 'nt':
		path = unicodedata.normalize('NFC', path).casefold()
	return path


def _identities(path):
	'''
	Comparison forms (_fold) of `path`: lexical, and with Windows short
	names expanded (looked up in each parent directory). Links are not
	followed, so `path` itself is never opened.
	'''
	lexical = _abspath(path)
	drive, rest = os.path.splitdrive(lexical)
	expanded = drive + os.sep
	for part in (p for p in rest.split(os.sep) if p):
		expanded = _long_name(os.path.join(expanded, part))
	return {_fold(lexical), _fold(expanded)}


def _long_name(path):
	'''
	Expand a Windows 8.3 short name in the last component of `path`. This
	looks the name up in its parent directory; `path` itself is not opened.
	'''
	if os.name != 'nt' or '~' not in os.path.basename(path):
		return path
	import ctypes

	buffer = ctypes.create_unicode_buffer(32768)
	if ctypes.windll.kernel32.GetLongPathNameW(path, buffer, len(buffer)):
		return buffer.value
	return path


def _is_link(info):
	import stat

	if stat.S_ISLNK(info.st_mode):
		return True
	tag = getattr(info, 'st_reparse_tag', 0)
	return tag == getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)


def _inside_home(form):
	return any(
		form == home or form.startswith(home.rstrip(os.sep) + os.sep)
		for home in _home_identities
	)


def _resolves_into_home(value, hops=0):
	'''
	Walk `value` one component at a time: check each prefix lexically
	before touching it, expand short names, and follow symlinks/junctions
	by reading the link itself. A prefix inside the home is never opened.
	Prefixes are compared in their _fold form but looked up as spelled, so
	a case-sensitive file system finds them.
	'''
	path = _abspath(value)
	if _inside_home(_fold(path)):
		return True
	if hops > _MAX_HOPS:
		return False

	drive, rest = os.path.splitdrive(path)
	parts = [p for p in rest.split(os.sep) if p]
	current = drive + os.sep
	for index, part in enumerate(parts):
		candidate = _long_name(os.path.join(current, part))
		if _inside_home(_fold(candidate)):
			return True
		try:
			info = _lstat(candidate)
		except (OSError, ValueError):
			current = candidate
			continue
		if _is_link(info):
			try:
				target = _readlink(candidate)
			except (OSError, ValueError):
				return False
			if target.startswith('\\\\?\\'):
				target = target[4:]
			resolved = os.path.join(current, target, *parts[index + 1:])
			return _resolves_into_home(resolved, hops + 1)
		current = candidate
	return False


def _path_text(path):
	'''
	`path` as text, or None for file descriptors and non-paths. A SQLite
	`file:` URI becomes its file path.
	'''
	if isinstance(path, int):
		return None
	try:
		value = os.fspath(path)
	except TypeError:
		return None
	if isinstance(value, bytes):
		value = os.fsdecode(value)
	if value.startswith('file:'):
		value = unquote(value[len('file:'):].split('?', 1)[0])
		if value.startswith('///'):
			value = value[2:]
		if len(value) > 2 and value[0] == '/' and value[2] == ':':
			value = value[1:]
	return value or None


def in_real_home(path):
	'''
	True when `path` is the real application home or below it, under any
	alias the guard can resolve.
	'''
	if getattr(_state, 'checking_home', False):
		return False
	value = _path_text(path)
	if value is None or value == ':memory:':
		return False

	_state.checking_home = True
	try:
		return _resolves_into_home(value)
	finally:
		_state.checking_home = False


@contextlib.contextmanager
def pretend_real_home(path):
	'''
	Treat `path` (a temporary directory) as the real application home inside
	the block, so tests can exercise alias detection without the real one.
	'''
	global _home_identities
	saved = _home_identities
	_home_identities = tuple(_identities(path))
	try:
		yield
	finally:
		_home_identities = saved


def _check_home(label, *paths):
	for path in paths:
		if in_real_home(path):
			_violation('home', f'{label} inside the real application home')


def _wrap_file_opener(real, label):
	'''
	Return an opener that refuses protected paths and the real home.
	'''
	def guarded(file, *args, **kwargs):
		if _is_protected(file):
			_violation('file', f'{label} of protected file {os.path.basename(os.fspath(file))!r}')
		_check_home(label, file)
		return real(file, *args, **kwargs)

	guarded.__wrapped__ = real
	return guarded


def _wrap_path_call(owner, name, path_args=1):
	'''
	Replace owner.name with a version that refuses the real home in its
	first `path_args` positional arguments.
	'''
	real = getattr(owner, name)

	def guarded(*args, **kwargs):
		_check_home(f'{owner.__name__}.{name}', *args[:path_args])
		return real(*args, **kwargs)

	guarded.__wrapped__ = real
	setattr(owner, name, guarded)


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


def _install_home_guards():
	'''
	Capture the real application home, refuse filesystem calls on it, and
	give this process (and its subprocesses) a temporary home and session.
	'''
	global _home_identities
	real_home = os.path.join(os.path.expanduser('~'), APP_DIR)
	_home_identities = tuple(_identities(real_home))

	for name in (
		'stat', 'lstat', 'listdir', 'scandir', 'mkdir', 'rmdir', 'remove',
		'unlink', 'chmod', 'access', 'utime', 'readlink'
	):
		_wrap_path_call(os, name)
	for name in ('rename', 'replace', 'link', 'symlink'):
		_wrap_path_call(os, name, path_args=2)
	for name in ('exists', 'lexists', 'isfile', 'isdir', 'islink', 'isjunction'):
		if hasattr(os.path, name):
			_wrap_path_call(os.path, name)
	_guards['os.stat'] = os.stat

	home = os.environ.get(HOME_ENV)
	if not home or not os.path.isabs(home) or in_real_home(home):
		home = tempfile.mkdtemp(prefix='tgtracker-home-')
		atexit.register(shutil.rmtree, home, ignore_errors=True)
		os.environ[HOME_ENV] = home
	session = os.environ.get(SESSION_ENV)
	if not session or in_real_home(session):
		os.environ[SESSION_ENV] = os.path.join(home, 'session_file.session')
	os.environ.pop(PROFILE_ENV, None)


def _use_canonical_tempdir():
	'''
	Point tempfile (and subprocesses, through TMPDIR) at the canonical form
	of the temporary directory. The code under test resolves links and
	short names (realpath), so paths built from a non-canonical temporary
	directory would not compare equal: macOS's /var is a link to
	/private/var, and a Windows TEMP can hold an 8.3 short name.
	'''
	canonical = os.path.realpath(tempfile.gettempdir())
	tempfile.tempdir = canonical
	os.environ['TMPDIR'] = canonical


def install():
	'''
	Install all guards once per process.
	'''
	global _installed
	if _installed:
		return

	_use_canonical_tempdir()
	_install_socket_guards()
	_install_asyncio_guards()
	_install_telethon_guards()
	_install_file_guards()
	_install_home_guards()
	no_sync.install()
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
		and os.stat is _guards['os.stat']
		and bool(_home_identities)
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
	Async test case running under the offline guard, on a virtual-time
	loop: request spacing and other sleeps cost no wall time.
	'''
	loop_factory = VirtualTimeLoop

	def setUp(self):
		super().setUp()
		self._start_offline_checks()


install()
