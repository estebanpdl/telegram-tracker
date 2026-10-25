# -*- coding: utf-8 -*-
'''
`download-media`: the media of a collected folder, after collection.

fetch_media takes what collect takes, in the same order: with the output
folder and the database lease checked free, the client connects and logs
in and the account lease is taken (`identify`); then the output lock and,
in sqlite and both folders, the database lease are held until the run
ends. Under them the saved histories are read with the --source reader
(media.plan), the ledger is opened (media.collect.MediaRun: dead runs
recorded, partial files of earlier runs removed) and, channel by
channel:

1. a channel whose history another profile collected is refused before
   any request (the --update rule of storage.provenance: file references
   and access hashes belong to the collecting account), and so is one
   without a stored access hash;
2. its selected references without a complete ledger item are recorded
   as requested, each with the collection run that wrote its message
   (media.plan.run_sources) as its source_run;
3. their messages are fetched again in batches of LOOKUP_BATCH
   (channels.GetMessages, method class media_lookup), and each batch's
   files are downloaded before the next batch is fetched, so file
   references are minutes old. A message that is gone fails as
   message_gone, one whose media changed as media_changed.

A lookup that fails ends that channel: the batch's items fail as
request_failed and the channel's later items stay requested. A rejected
session, an account stop and cancellation stop the run; so does a media
stop (full disk, unusable staging folder, ledger write), as downloads
are all the run does. Completed files and rows are never deleted.
'''

# import modules
import contextlib
import sqlite3
import sys

from dataclasses import dataclass, field

from telethon import errors, types

# import local submodules
from telegram_tracker.collector.outcomes import SESSION_ERRORS, SessionRejected, session_rejected, stop_reason_for
from telegram_tracker.media.collect import MediaRun
from telegram_tracker.media.download import MEDIA_CHANGED, MESSAGE_GONE, REQUEST_FAILED, MediaJob
from telegram_tracker.media.ledger import COMPLETE
from telegram_tracker.media.location import location_of, same_media
from telegram_tracker.media.plan import collection_run, open_folder, read_plans, run_sources
from telegram_tracker.request_policy import AccountStopped, RequestFailed, RequestPolicy
from telegram_tracker.storage.provenance import RunProfile, refusal
from telegram_tracker.storage.records import StorageError
from telegram_tracker.storage.writer import OutputLock, check_unlocked


COMMAND = 'download-media'

# messages fetched again per GetMessages request (a history page's size;
# the server's own cap is not known offline)
LOOKUP_BATCH = 100


@dataclass
class FetchSummary:
	'''
	What a download-media run did.

	channels -> saved histories considered (after duplicates)
	complete_before -> selected references already complete
	refused -> (name, reason) of channels refused before any request
	failed -> (name, reason) of channels whose lookup failed
	missing / duplicates -> as media.plan.Plans
	notes -> the reader's notes about the folder
	media -> the media block (MediaRun.record()), None before the ledger
	         was opened
	'''
	output_folder: str
	profile: RunProfile | None = None
	channels: int = 0
	complete_before: int = 0
	refused: list = field(default_factory=list)
	failed: list = field(default_factory=list)
	missing: list = field(default_factory=list)
	duplicates: list = field(default_factory=list)
	notes: list = field(default_factory=list)
	media: dict | None = None
	stop_reason: str | None = None


def profile_refusal(provenance, run_profile):
	'''
	Why `run_profile` may not download the media of a history recorded
	with `provenance` (reader.records.HistoryProvenance), or None when it
	may: the --update rule (storage.provenance.refusal).
	'''
	previous = {'profile_name': provenance.profile_name, 'profile_id': provenance.profile_id}
	run = run_profile or RunProfile()
	if refusal(previous, run) is None:
		return None
	name = provenance.profile_name
	if provenance.profile_id or name:
		return (
			f'its saved history was collected by profile {name or "unknown"}'
			+ (' with another profile ID' if name == run.name else '')
			+ '; download-media needs the same profile, because file references and access hashes '
			'belong to the collecting account'
			+ (f'. Use --profile {name}' if name and name != run.name else '')
		)
	return 'its saved history has no profile record; only the default profile can download its media'


async def fetch_media(
		output_folder,
		selection,
		*,
		client_factory,
		authorize,
		source='auto',
		targets=None,
		identify=None,
		profile=None,
		preflight=None,
		concurrency=1,
		request_interval=0.25,
		max_flood_wait=60,
		max_retries=3,
		policy=None
	):
	'''
	Download the selected media of the saved histories in `output_folder`
	(see the module docstring); returns a FetchSummary.

	selection -> media.selection.MediaSelection
	targets -> names of the histories to cover (None: all)
	client_factory / authorize / identify / profile -> as for
		collector.run_collection
	preflight -> called after the output-lock check and before the client
		is created (the database lease check)
	concurrency -> downloads at a time, also the bound on requests in
		flight; request_interval / max_flood_wait / max_retries -> the
		request policy, which `policy` replaces (tests)

	Raises StorageError, DataInputError, OutputLocked, the login's errors
	and cancellation; an account stop or a rejected session ends the run
	with the summary's stop_reason.
	'''
	summary = FetchSummary(output_folder)
	check_unlocked(output_folder)
	if preflight is not None:
		preflight()
	client = client_factory()
	try:
		await client.connect()
		await authorize(client)
		account = contextlib.nullcontext(profile)
		if identify is not None:
			account = await identify(client)
		with account as run_profile:
			summary.profile = run_profile
			policy = policy or RequestPolicy(
				concurrency=concurrency, interval=request_interval, max_flood_wait=max_flood_wait,
				max_retries=max_retries
			)
			with OutputLock(output_folder), open_folder(output_folder, source, lease=True) as collection:
				await _fetch_locked(client, policy, collection, summary, selection, targets, concurrency)
	finally:
		await client.disconnect()
	return summary


async def _fetch_locked(client, policy, collection, summary, selection, targets, concurrency):
	found = read_plans(collection, selection, targets, sources=run_sources(collection, summary.output_folder))
	summary.channels = len(found.plans)
	summary.missing, summary.duplicates = found.missing, found.duplicates
	summary.notes = list(collection.notes)
	media_run = MediaRun.open(
		summary.output_folder, selection, client=client, policy=policy, profile=summary.profile,
		collection_run=collection_run(summary.output_folder), command=COMMAND
	)
	try:
		try:
			for plan in found.plans:
				if media_run.stop_reason is not None:
					break
				await _fetch_channel(media_run, plan, summary, concurrency)
		except BaseException as exc:
			summary.stop_reason = stop_reason_for(exc)
			summary.media = media_run.finish(summary.stop_reason)
			if isinstance(exc, (AccountStopped, SessionRejected)):
				return
			raise
		summary.media = media_run.finish()
	finally:
		media_run.close()


async def _fetch_channel(media_run, plan, summary, concurrency):
	'''
	One ChannelPlan: the profile and access hash checks, the requested
	rows, then lookups and downloads batch by batch.
	'''
	reason = profile_refusal(plan.provenance, summary.profile)
	if reason is None and plan.access_hash is None:
		reason = (
			f'no access hash of channel {plan.channel_id} is stored with its history, so its messages '
			'cannot be fetched again; collect it again'
		)
	if reason is not None:
		summary.refused.append((plan.name, reason))
		print(f'> {plan.name}: {reason}; its media were not downloaded.', file=sys.stderr)
		return

	pending = []
	for message_id, ref in plan.references:
		item = media_run.ledger.item(plan.key(message_id, ref))
		if item is not None and item['state'] == COMPLETE:
			summary.complete_before += 1
		else:
			pending.append((message_id, ref))
	# each item names the collection run that wrote its message, not the
	# folder's last run (None when the folder cannot tell)
	if not media_run.record_requested(plan.channel_id, pending, source_runs=plan.runs or {}):
		return

	peer = types.InputChannel(channel_id=plan.channel_id, access_hash=plan.access_hash)
	for start in range(0, len(pending), LOOKUP_BATCH):
		if media_run.stop_reason is not None:
			return
		batch = pending[start:start + LOOKUP_BATCH]
		try:
			messages = await media_run.downloader.lookup(peer, [message_id for message_id, _ in batch])
		except SESSION_ERRORS as exc:
			raise session_rejected(exc) from None
		except (RequestFailed, errors.RPCError) as exc:
			for message_id, ref in batch:
				if not _fail(media_run, plan.key(message_id, ref), REQUEST_FAILED):
					return
			later = len(pending) - start - len(batch)
			reason = (
				f'fetching its messages again failed ({exc.__class__.__name__}); {len(batch)} file(s) '
				f'failed and {later} were not attempted'
			)
			summary.failed.append((plan.name, reason))
			print(f'> {plan.name}: {reason}.', file=sys.stderr)
			return
		jobs = []
		for message_id, ref in batch:
			message = messages.get(message_id)
			location = location_of(message.media, ref.photo_size) if message is not None and message.media else None
			if message is not None and same_media(location, ref.media_kind, ref.media_id):
				jobs.append(MediaJob(plan.channel_id, message_id, ref, location, peer))
			elif not _fail(media_run, plan.key(message_id, ref), MESSAGE_GONE if message is None else MEDIA_CHANGED):
				return
		await media_run.download(concurrency, jobs)


def _fail(media_run, key, reason):
	'''
	Record a requested item failed before any download; a ledger that
	cannot be written stops the media downloads (returns False).
	'''
	try:
		media_run.ledger.fail(media_run.run_uuid, key, reason)
	except (StorageError, sqlite3.Error) as exc:
		media_run.stop(f'media downloads stopped: storage error: {exc}')
		return False
	return True
