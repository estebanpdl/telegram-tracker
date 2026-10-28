# -*- coding: utf-8 -*-
'''
A download in progress and its publication.

A PartialFile is written under downloaded-media/.partial/<run_uuid>/,
which is on the same volume as the final folders. It is hashed (SHA-256)
as it is written and synced to disk before it is published.

publish() never replaces anything: the final name is inspected with
lstat first (layout.check_final), then the partial file is linked there
with storage.sqlite.connection.link_new, which refuses a name that
appeared meanwhile. A file already there with the same content is
adopted (a leftover of a crash between publication and the ledger
write); anything else is a PathConflict and is left as it is.
'''

# import modules
import hashlib
import os
import stat

# import local submodules
from telegram_tracker.media.layout import ABSENT, SAME, MediaPathError, PathConflict, check_final, is_link, \
	lstat_or_none, sync_folder
from telegram_tracker.storage.sqlite.connection import link_new
from telegram_tracker.storage.sqlite.schema import DatabaseRefused


class PartialFile:
	'''
	One file being downloaded. size and sha256 cover the bytes written
	since the last restart().
	'''
	def __init__(self, path):
		self.path = path
		info = lstat_or_none(path)
		if info is not None:
			# a leftover of an earlier attempt of this run
			if is_link(info) or not stat.S_ISREG(info.st_mode):
				raise MediaPathError(f'{path} is not a regular file')
			os.remove(path)
		flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
		self._file = os.fdopen(os.open(path, flags, 0o600), 'wb')
		self._digest = hashlib.sha256()
		self.size = 0

	def write(self, chunk):
		self._file.write(chunk)
		self._digest.update(chunk)
		self.size += len(chunk)

	def restart(self):
		'''
		Back to zero bytes, for a download that starts over.
		'''
		self._file.seek(0)
		self._file.truncate()
		self._digest = hashlib.sha256()
		self.size = 0

	def finish(self):
		'''
		Flush, sync and close; returns (size, sha256).
		'''
		self._file.flush()
		os.fsync(self._file.fileno())
		self._file.close()
		return self.size, self._digest.hexdigest()

	def remove(self):
		'''
		Close (if still open) and delete the file; never raises.
		'''
		try:
			self._file.close()
		except OSError:
			pass
		try:
			os.remove(self.path)
		except OSError:
			pass


def publish(partial_path, final_path, sha256, size):
	'''
	Publish the finished partial file at `final_path`. Returns ABSENT when
	it was linked there, SAME when that content was already there (it is
	adopted). Raises PathConflict for anything else at `final_path`.
	'''
	found = check_final(final_path, sha256, size)
	if found != ABSENT:
		return found
	try:
		link_new(partial_path, final_path)
	except DatabaseRefused:
		# appeared meanwhile: adopt it only if it is this content
		if check_final(final_path, sha256, size) != SAME:
			raise PathConflict(f'{final_path} changed while it was being published; it was left as it is') from None
		return SAME
	sync_folder(os.path.dirname(final_path))
	return ABSENT
