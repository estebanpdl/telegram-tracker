# -*- coding: utf-8 -*-
'''
Opt-in switch that skips disk syncs in the test process, for local runs on
slow disks. Off by default; set TGTRACKER_TEST_NO_SYNC=1 to turn it on.

When on, `install()` (called by the offline guard) replaces:
	- `sqlite3.connect` with a version that sets `PRAGMA synchronous = OFF`
	  on each new connection;
	- `os.fsync` with a version that checks the descriptor and returns
	  without flushing.

Data still reaches the operating system, so everything a test process can
observe is unchanged: rollback journals are still written, and a killed
process leaves the same files behind. Only power-loss durability is lost,
which no test can observe.

The pragma reads the database header, so on a damaged, foreign, or locked
file it fails. It runs with the busy timeout at zero and its errors are
ignored. The connection's own busy timeout is then restored, so the code
under test meets the same error, after the same wait, at its own first
statement.

Tests that patch `sqlite3.connect` or `os.fsync` themselves (fault
injection, sync counting) replace these versions for as long as their
patch lasts. The variable is removed from the environment once read, so
child processes that tests start keep real syncs.
'''

# import modules
import os
import sqlite3


ENV = 'TGTRACKER_TEST_NO_SYNC'

ENABLED = False


def connect_without_sync(connect):
	'''
	Wrap `connect` so that new connections skip syncs where SQLite allows it.
	'''
	def no_sync_connect(*args, **kwargs):
		con = connect(*args, **kwargs)
		timeout = con.execute('PRAGMA busy_timeout').fetchone()[0]
		con.execute('PRAGMA busy_timeout = 0')
		try:
			con.execute('PRAGMA synchronous = OFF')
		except sqlite3.Error:
			pass
		finally:
			con.execute(f'PRAGMA busy_timeout = {int(timeout)}')
		return con

	no_sync_connect.__wrapped__ = connect
	return no_sync_connect


def fsync_without_flush(fd):
	'''
	Accept what `os.fsync` accepts (a descriptor or an object with
	`fileno()`) and fail on a closed or invalid one, without flushing.
	'''
	if not isinstance(fd, int):
		fd = fd.fileno()
	os.fstat(fd)


def install(environ=None):
	'''
	Read and remove ENV from `environ` (default `os.environ`). When it is
	'1', install the replacements and return True. An unset or empty
	variable, or '0', leaves syncs on; any other value is an error.
	'''
	global ENABLED
	environ = os.environ if environ is None else environ
	value = environ.pop(ENV, '')
	if value not in ('', '0', '1'):
		raise ValueError(f'{ENV} must be 1 (skip syncs) or 0 (keep them), not {value!r}')
	if value != '1':
		return False

	sqlite3.connect = connect_without_sync(sqlite3.connect)
	os.fsync = fsync_without_flush
	ENABLED = True
	return True
