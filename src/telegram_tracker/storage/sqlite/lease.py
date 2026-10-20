# -*- coding: utf-8 -*-
'''
The database lease: `<database>.lock` beside the canonical database path
(symlinks and junctions resolved), held by the one process that writes
the database, a collection or an upgrade. Like the other leases
(credentials.lease), a lease left by a crashed process is reported,
never removed automatically.
'''

# import modules
import os

# import local submodules
from telegram_tracker.credentials.lease import Lease, LeaseError, holder, lease_path
from telegram_tracker.storage.records import StorageError


class DatabaseLocked(StorageError):
	'''
	Another process holds the database lease, or a crashed one left it
	behind.
	'''


def canonical(path):
	return os.path.realpath(os.path.abspath(path))


def database_lease_path(path):
	return lease_path(canonical(path))


def locked_message(path):
	lease = database_lease_path(path)
	detail = holder(canonical(path))
	return (
		f'The database {path} is in use by another collection or upgrade'
		+ (f' ({detail})' if detail else '')
		+ f'. If none is running, an earlier one was interrupted: delete {lease} '
		'and try again.'
	)


def check_free(path):
	'''
	Raise DatabaseLocked when the database lease exists. Takes nothing.
	'''
	if os.path.lexists(database_lease_path(path)):
		raise DatabaseLocked(locked_message(path))


class DatabaseLease:
	'''
	Context manager holding the database lease. Raises DatabaseLocked
	when it is held, StorageError when it cannot be created.
	'''
	def __init__(self, path):
		self.path = path
		self.lease = Lease(canonical(path), f'The database {path}')

	def __enter__(self):
		try:
			self.lease.__enter__()
		except LeaseError as exc:
			if os.path.lexists(self.lease.path):
				raise DatabaseLocked(locked_message(self.path)) from None
			raise StorageError(str(exc)) from None
		return self

	def __exit__(self, *exc_info):
		return self.lease.__exit__(*exc_info)
