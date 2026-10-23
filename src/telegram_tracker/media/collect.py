# -*- coding: utf-8 -*-
'''
Media downloads of a collect run (`collect --include-media`).

While a channel's history is paged, ChannelMedia keeps where each
selected file lives (its MediaLocation, taken from the Telethon message,
never the raw object) and turns the messages the run covers into
MediaJobs:

	full or window run   the messages kept
	--update             the messages it merged in (all of them for a
	                     history it created; none when nothing was new)

MediaRun is the run's media side, opened once the output folder is
locked and the run's store is open, and closed before the lock is
released:

1. open: a files folder gets its collection-identity.json, then the
   ledger is opened (created when missing) and checked against the
   folder's data; the run is recorded (runs that died are recorded as
   interrupted) and their partial files are removed.
2. request(result), the writer's duty right after a channel's history
   is committed: the channel's selected media are recorded as requested.
3. download(concurrency), after the run's last history commit, so a
   media flood or a slow file never delays a channel's history: one
   MediaDownloader for the run, `concurrency` downloads at a time.
4. finish(stop_reason): the ledger run ends and the sidecar's `media`
   block is filled.

MediaStopped (a full disk, an unusable staging folder) and a ledger
write that fails stop the media downloads only; the collection goes on
and the block says why. AccountStopped and cancellation stop the run.
Completed files and rows are never deleted.
'''

# import modules
import asyncio
import sqlite3
import sys
import uuid

from telethon import types

# import local submodules
from telegram_tracker.media.classify import classify
from telegram_tracker.media.download import MEDIA, MEDIA_LOOKUP, MediaDownloader, MediaJob, MediaStopped
from telegram_tracker.media.identity import ensure_identity
from telegram_tracker.media.layout import LEDGER_FILE, MEDIA_FOLDER, MediaLayout, MediaPathError
from telegram_tracker.media.ledger import COMPLETE, FAILED, REQUESTED, SKIPPED, STATES, open_ledger
from telegram_tracker.media.location import location_of, same_media
from telegram_tracker.media.partials import clean_partials
from telegram_tracker.storage.records import StorageError


COMMAND = 'collect'
RUNNING = 'running'

# the policy's method classes of media requests, reported in the media
# block rather than with the collection's requests
METHOD_CLASSES = (MEDIA, MEDIA_LOOKUP)


def input_channel(entity):
	'''
	The InputChannel of a resolved channel, to fetch its messages again.
	A channel resolved without an access hash gets 0, which the server
	refuses: only a reference refresh needs it, and fails then.
	'''
	return types.InputChannel(channel_id=entity.id, access_hash=getattr(entity, 'access_hash', None) or 0)


def covered_messages(result):
	'''
	The stored messages whose media a ChannelResult covers: the history
	it replaced or created, or the fetched messages it merged; none when
	an --update found nothing new (it carries neither).
	'''
	payload = result.fetched if result.history_mode == 'merged' else result.history
	return (payload or {}).get('messages') or []


class ChannelMedia:
	'''
	The selected media seen while paging one channel.
	'''
	def __init__(self, selection):
		self.selection = selection
		self.locations = {}

	def capture(self, page):
		'''
		Keep the MediaLocation of each selected file on a history page
		(Telethon objects); the first copy of a message wins.
		'''
		for message in getattr(page, 'messages', None) or ():
			media = getattr(message, 'media', None)
			message_id = getattr(message, 'id', None)
			if media is None or message_id in self.locations:
				continue
			ref = classify(media.to_dict())
			if self.selection.selects(ref):
				self.locations[message_id] = location_of(media, ref.photo_size)

	def jobs(self, channel_id, peer, messages):
		'''
		A MediaJob for each stored message (JSON-safe) whose media were
		captured (selected), in message order. The job's MediaRef comes
		from the stored message, so its keys are the stored ones.
		'''
		jobs = []
		for message in messages:
			location = self.locations.get(message.get('id'))
			if location is None:
				continue
			ref = classify(message.get('media'))
			if ref is not None and same_media(location, ref.media_kind, ref.media_id):
				jobs.append(MediaJob(channel_id, message['id'], ref, location, peer))
		return jobs


class MediaRun:
	'''
	The media side of one collect run; see the module docstring.
	'''
	def __init__(self, ledger, downloader, selection, source_run):
		self.ledger = ledger
		self.downloader = downloader
		self.selection = selection
		self.source_run = source_run
		self.run_uuid = downloader.run_uuid
		self.jobs = []
		self.channels = set()
		self.downloaded = 0
		self.state = RUNNING
		# stop_reason: the downloads stopped on their own; reason: why the
		# media run ended early, as the ledger records it
		self.stop_reason = None
		self.reason = None
		self.items = None

	@classmethod
	def open(cls, output_folder, selection, *, client, policy, profile=None, collection_run=None):
		'''
		Open the ledger of `output_folder` and start the run (see the
		module docstring). `collection_run` is the collection's run UUID
		in a database folder, its started_at in a files folder. Raises
		StorageError when the ledger cannot be used.
		'''
		try:
			identity = ensure_identity(output_folder)
			layout = MediaLayout(output_folder)
			ledger = open_ledger(layout, identity, create=True)
		except (OSError, sqlite3.Error) as exc:
			raise StorageError(f'cannot open the media ledger of {output_folder}: {exc}') from None
		try:
			run_uuid = str(uuid.uuid4())
			started = ledger.start_run(
				run_uuid, COMMAND, selection=selection.record(),
				profile_name=profile.name if profile else None, profile_id=profile.id if profile else None,
				collection_run=collection_run
			)
			downloader = MediaDownloader(client, policy, ledger, layout, run_uuid, limits=selection.limits)
		except BaseException:
			ledger.close()
			raise
		report_recovery(started, layout, ledger, run_uuid)
		return cls(ledger, downloader, selection, collection_run)

	def close(self):
		self.ledger.close()

	def stop(self, reason):
		'''
		Stop the media downloads (not the collection); the first reason
		stands.
		'''
		if self.stop_reason is None:
			self.stop_reason = reason
			print(f'> {reason}. The collected data is not affected.', file=sys.stderr)

	def request(self, result):
		'''
		Writer side, right after `result`'s history was committed: record
		its selected media as requested and queue them. A ledger that
		cannot be written stops the media downloads only.
		'''
		jobs = result.media or []
		if self.stop_reason is not None or not jobs:
			return
		try:
			self.ledger.request(
				self.run_uuid, result.channel_id, [(job.message_id, job.ref) for job in jobs],
				source_run=self.source_run
			)
		except (StorageError, sqlite3.Error) as exc:
			self.stop(f'media downloads stopped: storage error: {exc}')
			return
		self.channels.add(result.channel_id)
		self.jobs.extend(jobs)

	async def download(self, concurrency):
		'''
		Download the queued jobs, `concurrency` at a time. Media stops end
		the downloads only. AccountStopped and other errors are raised
		once every download in flight has ended; cancelling cancels them.
		'''
		pending = iter(self.jobs)

		async def worker():
			for job in pending:
				if self.stop_reason is not None:
					return
				try:
					outcome = await self.downloader.download(job)
				except MediaStopped as exc:
					self.stop(str(exc))
					return
				except (StorageError, sqlite3.Error) as exc:
					self.stop(f'media downloads stopped: storage error: {exc}')
					return
				if outcome.state == COMPLETE and outcome.downloaded:
					self.downloaded += 1

		if self.stop_reason is not None or not self.jobs:
			return
		workers = [asyncio.create_task(worker()) for _ in range(max(1, min(concurrency, len(self.jobs))))]
		# a failing download does not cancel the others: an account stop
		# reaches each of them at its next request, which records it
		results = await asyncio.gather(*workers, return_exceptions=True)
		for result in results:
			if isinstance(result, BaseException):
				raise result

	def finish(self, stop_reason=None):
		'''
		Record the end of the media run: interrupted when media or the run
		(`stop_reason`) stopped early, partial when a file failed, else
		complete. Returns the sidecar block (record()).
		'''
		reason = self.reason = self.stop_reason or stop_reason
		try:
			if reason is None:
				# every download has ended: none is left downloading
				self.items = self.count_items()
				state = 'partial' if self.items[FAILED] else 'complete'
				self.ledger.finish_run(self.run_uuid, state)
			else:
				state = 'interrupted'
				self.ledger.finish_run(self.run_uuid, state, stop_reason=reason)
				# with the downloads it interrupted recorded as failed
				self.items = self.count_items()
			self.state = state
		except (StorageError, sqlite3.Error) as exc:
			self.stop(f'media downloads stopped: storage error: {exc}')
			self.state = 'interrupted'
		self.downloader.finish()
		return self.record()

	def count_items(self):
		'''
		This run's selected items by their state in the ledger now.
		'''
		counts = dict.fromkeys(STATES, 0)
		for job in self.jobs:
			item = self.ledger.item(job.key)
			if item is not None:
				counts[item['state']] += 1
		return counts

	def record(self):
		'''
		The `media` block of collection-status.json.
		'''
		record = {
			'run_uuid': self.run_uuid,
			'ledger': f'{MEDIA_FOLDER}/{LEDGER_FILE}',
			**self.selection.record(),
			'state': self.state,
			'stop_reason': self.stop_reason or self.reason,
			'channels': len(self.channels),
			'items': None,
			'bytes_received': self.downloader.budget.received,
			'requests': {
				name: stats for name, stats in self.downloader.policy.stats_record().items()
				if name in METHOD_CLASSES
			}
		}
		if self.items is not None:
			record['items'] = {
				'selected': len(self.jobs),
				'complete': self.items[COMPLETE],
				'downloaded': self.downloaded,
				'skipped': self.items[SKIPPED],
				'failed': self.items[FAILED],
				'not_attempted': self.items[REQUESTED]
			}
		return record


def report_recovery(started, layout, ledger, run_uuid):
	'''
	Say what an earlier media run left (stderr), and remove the partial
	files of runs that died. A folder that cannot be cleaned is reported
	and the run goes on.
	'''
	if started.stale_runs:
		print(
			f'> Note: {len(started.stale_runs)} earlier media run(s) never finished (the process ended); '
			f'they are now recorded as interrupted, with {started.interrupted_items} download(s) failed.',
			file=sys.stderr
		)
	try:
		leftovers = clean_partials(layout, ledger, run_uuid)
	except (OSError, MediaPathError) as exc:
		print(f'> Note: leftover partial downloads were not cleaned: {exc}', file=sys.stderr)
		return
	if leftovers.removed:
		print(f'> Note: removed {len(leftovers.removed)} partial download(s) of earlier runs.', file=sys.stderr)
	kept = leftovers.kept + leftovers.unknown
	if kept:
		print(
			f'> Note: {len(kept)} other item(s) in {layout.partial_root} were left in place: '
			+ ', '.join(kept[:3]) + (', ...' if len(kept) > 3 else ''),
			file=sys.stderr
		)
