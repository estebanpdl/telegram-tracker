# -*- coding: utf-8 -*-
'''
The download adapter: one requested ledger item to a stored file.

MediaDownloader.download(job) takes an item the run has recorded as
requested and ends it complete, skipped or failed:

1. A complete item elsewhere in the channel with the same media (the
   ledger's blob hint) is reused with no request, once its stored file
   is verified (size and SHA-256, once per run per blob).
2. A known size over the per-file limit is skipped with no request; one
   that does not fit in the per-run limit is skipped too.
3. The item moves to downloading (one ledger attempt). Its chunks
   stream into .partial/<run_uuid>/, each pull admitted by the request
   policy (method class `media`), hashed as written, and synced. A pull
   that fails is retried by the policy at the same offset.
4. An expired file reference is refreshed once per attempt by fetching
   the message again (method class `media_lookup`); the file then starts
   over from zero. A second expiry fails the item.
5. The content is stored once per channel: a blob with the same SHA-256
   is reused (verified), else the file is published at its generated
   name without replacing anything. The blob, the item and the attempt
   are recorded in one ledger transaction, with the run's counters.

Failures map to the ledger's reasons:

	AccountStopped (flood over the limit)  flood_stop, then raised: the run stops
	RequestFailed, other RPC errors, CDN   request_failed
	disk full                              disk_full, then MediaStopped
	staging folder unusable                storage_error, then MediaStopped
	other local file errors                storage_error
	cancellation                           interrupted, then raised

The partial file is removed whatever happens. Once the account or media
has stopped, download() raises at once and leaves the item requested.

Concurrent downloads of the same media in a channel (the same document
in two messages) are coalesced: the second waits for the first, then
reuses its blob through step 1, so the file is requested and reserved
once. Every ledger write is synchronous and complete before the next
await.
'''

# import modules
import asyncio
import os

from dataclasses import dataclass

from telethon import errors, types
from telethon.client import downloads as telethon_downloads
from telethon.tl import functions

# import local submodules
from telegram_tracker.media.classify import MediaRef
from telegram_tracker.media.layout import SAME, MediaPathError, PathConflict, inspect_final, partial_name
from telegram_tracker.media.ledger import COMPLETE, FAILED, REQUESTED, SKIPPED, AttemptStats, ItemKey, \
	LedgerError
from telegram_tracker.media.limits import OVER_FILE_LIMIT, OVER_RUN_LIMIT, PULL_BYTES, MediaBudget, MediaLimits
from telegram_tracker.media.location import MediaLocation, location_of, same_media
from telegram_tracker.media.staging import PartialFile, is_disk_full, publish
from telegram_tracker.media.stream import ChunkStream
from telegram_tracker.request_policy import AccountStopped, RequestFailed, flood_seconds
from telegram_tracker.storage.records import StorageError


MEDIA = 'media'
MEDIA_LOOKUP = 'media_lookup'

REFERENCE_EXPIRED = 'reference_expired'
MESSAGE_GONE = 'message_gone'
MEDIA_CHANGED = 'media_changed'
FLOOD_STOP = 'flood_stop'
REQUEST_FAILED = 'request_failed'
DISK_FULL = 'disk_full'
STORAGE_ERROR = 'storage_error'
SIZE_MISMATCH = 'size_mismatch'
PATH_CONFLICT = 'path_conflict'
STORED_FILE_DIFFERS = 'stored_file_differs'
INTERRUPTED = 'interrupted'

REFERENCE_ERRORS = (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError)
# Telethon raises this private error for a CDN redirect; requests here
# never ask for CDN files, so it is not expected
CDN_REDIRECT = getattr(telethon_downloads, '_CdnRedirect', ())
REQUEST_ERRORS = (RequestFailed, errors.RPCError, CDN_REDIRECT)


@dataclass(frozen=True)
class MediaJob:
	'''
	One selected file of a message.

	ref -> its classify.MediaRef
	location -> its MediaLocation, from the collected message
	peer -> the InputChannel to fetch the message again with
	'''
	channel_id: int
	message_id: int
	ref: MediaRef
	location: MediaLocation
	peer: object

	@property
	def key(self):
		return ItemKey(self.channel_id, self.message_id, self.ref.media_id)

	@property
	def media_key(self):
		return (self.channel_id, self.ref.media_kind, self.ref.media_id)


@dataclass(frozen=True)
class DownloadOutcome:
	'''
	How download() left an item. path -> the blob's relative path when
	complete; downloaded -> its bytes were requested in this call.
	'''
	key: ItemKey
	state: str
	reason: str | None = None
	path: str | None = None
	downloaded: bool = False


class MediaStopped(Exception):
	'''
	Downloads cannot go on in this run (disk full, or the staging folder
	cannot be used). Collection itself is not affected. `reason` is the
	ledger reason of the item that found it.
	'''
	def __init__(self, message, reason):
		super().__init__(message)
		self.reason = reason


class _Settle(Exception):
	'''
	Ends an attempt as (state, reason); `stop` also stops media.
	'''
	def __init__(self, state, reason, stop=None):
		super().__init__(reason)
		self.state = state
		self.reason = reason
		self.stop = stop


class _Counts:
	'''
	One attempt: its number, the budget reservation in use (a restart
	replaces it), and its application requests for the attempt row and
	the run's counters.
	'''
	def __init__(self, reservation=None):
		self.n = None
		self.reservation = reservation
		self.requests = 0
		self.retries = 0
		self.flood_waits = 0
		self.refreshes = 0
		self.bytes_received = 0

	def stats(self):
		return AttemptStats(self.bytes_received, self.requests, self.refreshes > 0)

	def counters(self):
		return {
			'app_requests': self.requests, 'retries': self.retries, 'flood_waits': self.flood_waits,
			'refreshes': self.refreshes, 'bytes_received': self.bytes_received
		}


def local_failure(exc):
	'''
	The _Settle for a local file error while staging or publishing.
	'''
	if isinstance(exc, PathConflict):
		return _Settle(FAILED, PATH_CONFLICT)
	if is_disk_full(exc):
		return _Settle(FAILED, DISK_FULL, stop=f'the disk is full ({exc.strerror or exc})')
	return _Settle(FAILED, STORAGE_ERROR)


class MediaDownloader:
	'''
	Downloads of one media run; see the module docstring. The ledger is
	open for writing with the run started, and the output folder locked.
	'''
	def __init__(self, client, policy, ledger, layout, run_uuid, *, limits=None, budget=None):
		self.client = client
		self.policy = policy
		self.ledger = ledger
		self.layout = layout
		self.run_uuid = run_uuid
		self.limits = limits or MediaLimits()
		self.budget = budget or MediaBudget(self.limits.max_total)
		self.stopped = None
		self._inflight = {}
		self._verified = {}

	def _raise_if_stopped(self):
		if self.policy.stopped is not None:
			raise self.policy.stopped
		if self.stopped is not None:
			raise self.stopped

	async def download(self, job):
		'''
		Download one MediaJob; returns its DownloadOutcome. Raises
		AccountStopped or MediaStopped when downloads cannot go on, and
		CancelledError when cancelled.
		'''
		self._raise_if_stopped()
		while (running := self._inflight.get(job.media_key)) is not None:
			# shielded: a cancelled waiter must not cancel the shared future
			await asyncio.shield(running)
			self._raise_if_stopped()
		done = asyncio.get_running_loop().create_future()
		self._inflight[job.media_key] = done
		try:
			return await self._download(job)
		finally:
			del self._inflight[job.media_key]
			done.set_result(None)

	def finish(self):
		'''
		Remove this run's empty staging folder, if any.
		'''
		try:
			folder = self.layout.partial_folder(self.run_uuid, create=False)
			if folder is not None:
				os.rmdir(folder)
		except (MediaPathError, OSError):
			pass

	'''

	One item

	'''

	async def _download(self, job):
		key = job.key
		item = self.ledger.item(key)
		if item is None:
			raise LedgerError(f'no media item {tuple(key)} in {self.ledger.path}')
		if item['state'] != REQUESTED:
			return DownloadOutcome(key, item['state'], item['reason'])
		reused = self._reuse(job)
		if reused is not None:
			return reused
		expected = item['expected_bytes']
		over = self.limits.skip_known(expected)
		reservation = None if over else self.budget.open_file(expected)
		if over or reservation is None:
			reason = over or OVER_RUN_LIMIT
			self.ledger.skip(self.run_uuid, key, reason)
			return DownloadOutcome(key, SKIPPED, reason)
		return await self._attempt(job, expected, reservation)

	def _reuse(self, job):
		'''
		Complete the item from the blob of the same media, verified; fail
		it when that blob's file differs; None when there is no such blob.
		'''
		blob = self.ledger.blob_hint(job.channel_id, job.ref.media_kind, job.ref.media_id)
		if blob is None:
			return None
		if not self._blob_intact(blob):
			self.ledger.fail(self.run_uuid, job.key, STORED_FILE_DIFFERS)
			return DownloadOutcome(job.key, FAILED, STORED_FILE_DIFFERS)
		path = self.ledger.complete(self.run_uuid, job.key, sha256=blob['sha256'], actual_bytes=blob['bytes'])
		return DownloadOutcome(job.key, COMPLETE, path=path)

	def _blob_intact(self, blob):
		'''
		Whether a blob's file is a regular file with its size and SHA-256,
		checked once per run.
		'''
		key = (blob['channel_id'], blob['sha256'])
		if key not in self._verified:
			try:
				path = self.layout.final_path(blob['path'], create=False)
				intact = path is not None and inspect_final(path, blob['sha256'], blob['bytes']) == SAME
			except (MediaPathError, OSError):
				intact = False
			self._verified[key] = intact
		return self._verified[key]

	async def _attempt(self, job, expected, reservation):
		key = job.key
		counts = _Counts(reservation)
		partial = None
		try:
			counts.n = self.ledger.begin_download(self.run_uuid, key)
			try:
				folder = self.layout.partial_folder(self.run_uuid)
				partial = PartialFile(os.path.join(folder, partial_name(*key)))
			except (MediaPathError, OSError) as exc:
				settle = local_failure(exc)
				settle.stop = settle.stop or f'the staging folder cannot be used ({exc})'
				raise settle from None
			await self._fetch(job, partial, expected, counts)
			size, sha256 = partial.finish()
			path = self._store(job, partial.path, size, sha256)
			self.ledger.complete(
				self.run_uuid, key, sha256=sha256, actual_bytes=size, attempt=counts.n, stats=counts.stats(),
				counters=counts.counters()
			)
			return DownloadOutcome(key, COMPLETE, path=path, downloaded=True)
		except _Settle as settle:
			return self._end(job, counts, settle)
		except AccountStopped:
			self._record(key, counts, FAILED, FLOOD_STOP)
			raise
		except asyncio.CancelledError:
			if counts.n is not None:
				self._record(key, counts, FAILED, INTERRUPTED)
			raise
		except REQUEST_ERRORS:
			return self._end(job, counts, _Settle(FAILED, REQUEST_FAILED))
		except (OSError, MediaPathError) as exc:
			return self._end(job, counts, local_failure(exc))
		except StorageError as exc:
			if isinstance(exc, LedgerError) or counts.n is None:
				raise
			# publication refused by the file system (no hard links, no safe rename)
			return self._end(job, counts, _Settle(FAILED, STORAGE_ERROR))
		finally:
			if partial is not None:
				partial.remove()
			counts.reservation.close()

	async def _fetch(self, job, partial, expected, counts):
		'''
		Receive the whole file into `partial`, refreshing an expired
		reference once; the file then starts over from zero, and a known
		size takes a new reservation.
		'''
		location = job.location
		while True:
			stream = ChunkStream(self.client, location)
			try:
				await self._receive(stream, partial, expected, counts)
				return
			except REFERENCE_ERRORS:
				if counts.refreshes:
					raise _Settle(FAILED, REFERENCE_EXPIRED) from None
			finally:
				await stream.close()
			counts.refreshes += 1
			location = await self._refresh(job, counts)
			partial.restart()
			if counts.reservation.known:
				counts.reservation.close()
				counts.reservation = self.budget.open_file(expected) or counts.reservation
				if counts.reservation.closed:
					raise _Settle(SKIPPED, OVER_RUN_LIMIT)

	async def _receive(self, stream, partial, expected, counts):
		'''
		Pull chunks into `partial` until the file ends: a short chunk, or
		the known size reached (no request past it). A known size reserved
		every pull it can need, and Telethon requests PULL_BYTES at most,
		so the two size_mismatch guards inside the loop only meet a server
		breaking those rules.
		'''
		reservation = counts.reservation
		while expected is None or partial.size < expected:
			if not reservation.admit_pull():
				raise _Settle(FAILED, SIZE_MISMATCH) if reservation.known else _Settle(SKIPPED, OVER_RUN_LIMIT)
			try:
				chunk = await self._call(counts, MEDIA, stream.pull)
			except BaseException:
				reservation.pull_failed()
				raise
			counts.bytes_received += len(chunk)
			reservation.received(min(len(chunk), PULL_BYTES))
			if len(chunk) > PULL_BYTES:
				raise _Settle(FAILED, SIZE_MISMATCH)
			partial.write(chunk)
			if expected is None and self.limits.stream_over(partial.size):
				raise _Settle(SKIPPED, OVER_FILE_LIMIT)
			if len(chunk) < PULL_BYTES:
				break
		if expected is not None and partial.size != expected:
			raise _Settle(FAILED, SIZE_MISMATCH)

	async def _refresh(self, job, counts):
		'''
		The MediaLocation of the message fetched again, with its new file
		reference.
		'''
		request = functions.channels.GetMessagesRequest(
			channel=job.peer, id=[types.InputMessageID(job.message_id)])
		try:
			result = await self._call(counts, MEDIA_LOOKUP, lambda: self.client(request))
		except (RequestFailed, errors.RPCError):
			raise _Settle(FAILED, REQUEST_FAILED) from None
		message = next(
			(m for m in getattr(result, 'messages', None) or () if getattr(m, 'id', None) == job.message_id), None)
		if not isinstance(message, types.Message):
			raise _Settle(FAILED, MESSAGE_GONE)
		location = location_of(message.media, job.ref.photo_size) if message.media is not None else None
		if not same_media(location, job.ref.media_kind, job.ref.media_id):
			raise _Settle(FAILED, MEDIA_CHANGED)
		return location

	async def _call(self, counts, method_class, request):
		'''
		policy.call, counting each request sent, its retries and flood
		waits.
		'''
		sent = 0

		async def counted():
			nonlocal sent
			sent += 1
			counts.requests += 1
			try:
				return await request()
			except BaseException as exc:
				if flood_seconds(exc) is not None:
					counts.flood_waits += 1
				raise

		try:
			return await self.policy.call(method_class, counted)
		finally:
			counts.retries += max(0, sent - 1)

	def _store(self, job, partial_path, size, sha256):
		'''
		The blob path for the finished content: an existing blob of the
		channel (verified), else the partial file published at its
		generated name.
		'''
		relative = self.ledger.blob_path(job.key, sha256)
		blob = self.ledger.blob(job.channel_id, sha256)
		if blob is not None:
			if blob['bytes'] != size or not self._blob_intact(blob):
				raise _Settle(FAILED, STORED_FILE_DIFFERS)
			return relative
		publish(partial_path, self.layout.final_path(relative), sha256, size)
		self._verified[(job.channel_id, sha256)] = True
		return relative

	def _end(self, job, counts, settle):
		self._record(job.key, counts, settle.state, settle.reason)
		if settle.stop:
			self.stopped = MediaStopped(f'media downloads stopped: {settle.stop}', settle.reason)
			raise self.stopped
		return DownloadOutcome(job.key, settle.state, settle.reason, downloaded=counts.requests > 0)

	def _record(self, key, counts, state, reason):
		end = self.ledger.skip if state == SKIPPED else self.ledger.fail
		end(self.run_uuid, key, reason, attempt=counts.n, stats=counts.stats(), counters=counts.counters())
