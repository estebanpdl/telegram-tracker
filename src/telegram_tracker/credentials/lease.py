# -*- coding: utf-8 -*-
'''
Exclusive leases: a lock file created with O_EXCL beside the thing it
guards (a session file, credentials.json), holding the owner's pid and
start time, removed on exit. A lease left by a crashed process is
reported, never removed automatically: only the user can tell that no
other process still uses the guarded file.
'''

# import modules
import json
import os

from datetime import datetime, timezone


LEASE_SUFFIX = '.lock'


class LeaseHeld(Exception):
	'''
	Another process holds the lease, or a crashed one left it behind.
	'''


def lease_path(path):
	return f'{path}{LEASE_SUFFIX}'


def holder(path):
	'''
	"pid N, started T" of the lease on `path`, or '' when unreadable.
	'''
	try:
		with open(lease_path(path), encoding='utf-8') as reader:
			info = json.load(reader)
		return f'pid {info.get("pid")}, started {info.get("started_at")}'
	except (OSError, ValueError, AttributeError):
		return ''


class Lease:
	'''
	Context manager holding the lease on `path`. `what` names the guarded
	file in messages ("the session", "the credentials file").
	'''
	def __init__(self, path, what):
		self.path = lease_path(path)
		self.guarded = path
		self.what = what

	def __enter__(self):
		try:
			fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
		except FileExistsError:
			detail = holder(self.guarded)
			raise LeaseHeld(
				f'{self.what} is in use by another process'
				+ (f' ({detail})' if detail else '')
				+ f'. If none is running, an earlier run was interrupted: delete '
				f'{self.path} and try again.'
			) from None
		with os.fdopen(fd, 'w', encoding='utf-8') as writer:
			json.dump({
				'pid': os.getpid(),
				'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')
			}, writer)
		return self

	def __exit__(self, *exc_info):
		try:
			os.remove(self.path)
		except FileNotFoundError:
			pass
		return False
