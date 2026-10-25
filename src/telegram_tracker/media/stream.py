# -*- coding: utf-8 -*-
'''
The chunks of one file, pulled through Telethon's iter_download.

iter_download is lazy: each pull sends one upload.GetFile request of
PULL_BYTES. ChunkStream.pull is the request function handed to
RequestPolicy.call, so every pull is admitted by the account's policy,
and the policy's retries are retries of that one pull.

A failed pull drops its iterator. The next pull reopens iter_download at
the offset already received, with the same file reference, so a retry
continues where the file stopped. Offsets are always whole pulls, and no
file size or chunk count is passed: Telethon then always takes its
direct path, whose requests are the pulls themselves (with a size, a
reopened iterator can switch to a path that requests past the end).
Stopping at a known size is the caller's job.

Telethon's own traffic inside a pull (borrowing the sender of another
data center, the FILE_MIGRATE re-request, its one retry of a timeout)
is not admitted by the policy.
'''

# import modules
import contextlib

# import local submodules
from telegram_tracker.media.limits import PULL_BYTES


class ChunkStream:
	'''
	Pulls of one MediaLocation from offset 0.
	'''
	def __init__(self, client, location):
		self.client = client
		self.location = location
		self.offset = 0
		self._iterator = None
		self._stale = []

	async def pull(self):
		'''
		The next chunk: PULL_BYTES, fewer for the last one, b'' past the
		end. Any error drops the iterator and is raised.
		'''
		await self._close_stale()
		if self._iterator is None:
			self._iterator = self.client.iter_download(
				self.location.input_location(), offset=self.offset, request_size=PULL_BYTES,
				dc_id=self.location.dc_id
			)
		try:
			chunk = bytes(await self._iterator.__anext__())
		except StopAsyncIteration:
			chunk = b''
		except BaseException:
			self._stale.append(self._iterator)
			self._iterator = None
			raise
		self.offset += len(chunk)
		return chunk

	async def close(self):
		'''
		Close every iterator opened, which returns a borrowed sender.
		'''
		if self._iterator is not None:
			self._stale.append(self._iterator)
			self._iterator = None
		await self._close_stale()

	async def _close_stale(self):
		while self._stale:
			iterator = self._stale.pop()
			# an iterator whose first pull failed before it set up its sender
			# has nothing to return, and its close() raises AttributeError
			with contextlib.suppress(AttributeError):
				await iterator.close()
