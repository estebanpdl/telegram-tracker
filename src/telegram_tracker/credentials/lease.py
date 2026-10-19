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


class LeaseError(Exception):
	'''
	The lease could not be taken.
	'''


class LeaseHeld(LeaseError):
	'''
	Another process holds the lease, or a crashed one left it behind.
	'''


def lease_path(path):
	return f'{path}{LEASE_SUFFIX}'


def holder(path):
	'''
	"pid N, started T[, profile P]" of the lease on `path`, or '' when
	unreadable.
	'''
	try:
		with open(lease_path(path), encoding='utf-8') as reader:
			info = json.load(reader)
		text = f'pid {info.get("pid")}, started {info.get("started_at")}'
		if isinstance(info.get('profile'), str):
			text += f', profile {info["profile"]}'
		return text
	except (OSError, ValueError, AttributeError):
		return ''


class Lease:
	'''
	Context manager holding the lease on `path`. `what` names the guarded
	file in messages ("the session", "the credentials file"); `profile`,
	when given, is recorded so a refused process can name the holder's
	profile.
	'''
	def __init__(self, path, what, profile=None):
		self.path = lease_path(path)
		self.guarded = path
		self.what = what
		self.profile = profile

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
		except OSError as exc:
			raise LeaseError(
				f'cannot lock {self.what.lower()} ({self.path}: '
				f'{exc.strerror or type(exc).__name__})'
			) from None
		info = {
			'pid': os.getpid(),
			'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')
		}
		if self.profile is not None:
			info['profile'] = self.profile
		with os.fdopen(fd, 'w', encoding='utf-8') as writer:
			json.dump(info, writer)
		return self

	def __exit__(self, *exc_info):
		try:
			os.remove(self.path)
		except FileNotFoundError:
			pass
		return False
