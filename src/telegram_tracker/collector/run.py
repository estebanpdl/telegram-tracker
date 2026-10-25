# -*- coding: utf-8 -*-
'''
Run orchestration. `run_collection` owns one client for the whole run. It
creates the client through an injected factory inside the running event
loop, connects, authorizes once, collects the targets with at most
`concurrency` channel workers (pages of one channel stay sequential),
finalizes the aggregate files, and disconnects exactly once after success,
failure, or cancellation.
'''

# import modules
import asyncio
import contextlib
import functools
import sys

# import local submodules
from telegram_tracker.collector.channel import collect_channel
from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.collector.outcomes import SESSION_ERRORS, CollectionSummary, \
	DuplicateTarget, PaginationStalled, SessionRejected, TargetUnavailable, \
	session_rejected, stop_reason_for
from telegram_tracker.collector.rpc import MetadataCache, Requests
from telegram_tracker.media.collect import METHOD_CLASSES as MEDIA_METHOD_CLASSES, MediaRun
from telegram_tracker.request_policy import AccountStopped, RequestFailed, RequestPolicy
from telegram_tracker.storage.histories import ReplaceNotConfirmed
from telegram_tracker.storage.provenance import HistoryRefused
from telegram_tracker.storage.records import COMPLETE, INTERRUPTED, PARTIAL, \
	HistoryUnreadable, StorageError
from telegram_tracker.storage.status import StatusFile
from telegram_tracker.storage.writer import OutputLock, check_unlocked, open_file_writer
from telegram_tracker.utils import create_dirs


NOT_FOUND_REASON = 'not found or not accessible'


def run_parameters(metadata_only, window, update, controls=None):
	'''
	The run's collection parameters for the sidecar (no credentials).
	'''
	if metadata_only:
		mode = 'metadata_only'
	elif update:
		mode = 'update'
	elif window.is_filtered:
		mode = 'window'
	else:
		mode = 'full'

	parameters = {
		'mode': mode,
		'metadata_only': metadata_only,
		'window': window.as_record() if mode == 'window' else None,
		'window_description': window.describe() if mode == 'window' else None
	}
	parameters.update(controls or {})
	return parameters


async def run_collection(
		targets,
		output_folder,
		*,
		client_factory,
		authorize,
		metadata_only=False,
		min_id=0,
		window=None,
		update=False,
		concurrency=1,
		request_interval=0.25,
		max_flood_wait=60,
		max_retries=3,
		requested_concurrency=None,
		targets_given=None,
		policy=None,
		profile=None,
		identify=None,
		writer_factory=None,
		preflight=None,
		confirmed=None,
		media=None
	):
	'''
	Collect `targets` (normalized, without duplicates) with one client.

	client_factory -> zero-argument callable returning an unconnected client;
		called inside the running loop
	authorize -> coroutine function awaited once with the connected client,
		before any worker starts (api.auth.ensure_authorized in production)
	window -> HistoryWindow selecting the messages kept (default: all
		above min_id)
	update -> merge new posts into each channel's saved history
	concurrency -> channel workers, capped at the number of targets; also
		the bound on application RPCs in flight
	request_interval / max_flood_wait / max_retries -> the request policy
		(see request_policy.py); `policy` replaces it (tests)
	requested_concurrency / targets_given -> recorded in the sidecar only
	profile -> RunProfile recorded in the sidecar and in the provenance of
		each history written; None records and checks no provenance
	identify -> coroutine function awaited once with the authorized
		client, before the output folder is locked; returns a context
		manager held until the run ends (the account lease) whose value is
		the run's RunProfile, replacing `profile`. Raising stops the run
		before any channel request.
	writer_factory -> opens the run's store once the output folder is
		locked, before collection-status.json is written (keyword
		arguments output_folder, profile, started_at, targets, parameters;
		see storage.writer.OutputWriter for the writer's methods); default:
		the file outputs. Raising StorageError stops the run before any
		channel request. The writer's status_fields() are added to the
		sidecar when it opens and when the run ends.
	preflight -> called after the output-lock check and before the client
		is created (the database lease check); raising stops the run.
	confirmed -> for a window run that asked before connecting: the
		targets whose replacement was confirmed. Any other target whose
		resolved channel has a history stored under its ID fails, keeping
		that ID in its outcome (see collector.channel). None checks nothing.
	media -> a media.selection.MediaSelection: also download the selected
		media of the messages each channel covers into
		<output>/downloaded-media/ (see media.collect). The ledger is
		opened once the store is open, before any channel request, and
		a channel's media are recorded right after its history is
		committed; downloads start after the last history commit, under
		the same policy, before the aggregates are finalized. A media
		stop (full disk, ledger failure) ends the downloads only; an
		account stop during them ends the run. None (the default)
		downloads nothing.

	Missing/inaccessible targets, stalled pagination, unreadable saved
	histories (--update), --update into another profile's history, and
	requests that keep failing transiently fail only that target; a target resolving to an earlier target's channel
	is skipped as a duplicate. An account stop (flood wait), a rejected
	session, a storage error, any other exception, or cancellation ends the
	run: completed channels stay written, aggregates are finalized from
	them, and collection-status.json says "interrupted". The output folder
	is locked for the run, and the client is disconnected exactly once
	after every worker has finished.
	'''
	window = window or HistoryWindow(min_id=min_id)
	if media is not None and metadata_only:
		raise ValueError('media downloads need message collection, not metadata only')
	summary = CollectionSummary(requested=list(targets), output_folder=output_folder)
	summary.concurrency = max(1, min(concurrency, len(summary.requested)))

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
			try:
				create_dirs(output_folder)
			except OSError as exc:
				raise StorageError(f'cannot create {output_folder}: {exc}') from exc

			policy = policy or RequestPolicy(
				concurrency=summary.concurrency,
				interval=request_interval,
				max_flood_wait=max_flood_wait,
				max_retries=max_retries
			)
			parameters = run_parameters(metadata_only, window, update, {
				'targets_given': len(summary.requested) if targets_given is None else targets_given,
				'concurrency': {
					'requested': requested_concurrency or concurrency,
					'effective': summary.concurrency
				},
				'request_interval': policy.interval,
				'max_flood_wait': policy.max_flood_wait,
				'max_retries': policy.max_retries
			})
			with OutputLock(output_folder):
				await _collect_locked(
					client,
					summary,
					policy,
					parameters,
					metadata_only=metadata_only,
					window=window,
					update=update,
					account_checks=0 if identify is None else 1,
					writer_factory=writer_factory or open_file_writer,
					confirmed=confirmed,
					media=media
				)
	finally:
		await client.disconnect()

	return summary


async def _collect_locked(
		client, summary, policy, parameters, *, metadata_only, window, update, account_checks,
		writer_factory, confirmed, media):
	status = StatusFile(
		summary.output_folder, summary.requested, parameters, summary.profile, account_checks
	)
	writer = writer_factory(
		output_folder=summary.output_folder,
		profile=summary.profile,
		started_at=status.data['started_at'],
		targets=summary.requested,
		parameters=parameters
	)
	try:
		status.data.update(_status_fields(writer))
		status.write()
		await _collect_targets(
			client, summary, policy, status, writer,
			metadata_only=metadata_only, window=window, update=update, confirmed=confirmed, media=media
		)
	finally:
		writer.close()


def _open_media(client, summary, policy, status, writer, media):
	'''
	The run's MediaRun (see media.collect), recorded in the sidecar.
	'''
	database = _status_fields(writer).get('database') or {}
	media_run = MediaRun.open(
		summary.output_folder, media, client=client, policy=policy, profile=summary.profile,
		collection_run=database.get('run_uuid') or status.data['started_at']
	)
	status.data['media'] = media_run.record()
	try:
		status.write()
	except BaseException as exc:
		media_run.finish(stop_reason_for(exc))
		media_run.close()
		raise
	return media_run


async def _collect_targets(
		client, summary, policy, status, writer, *, metadata_only, window, update, confirmed, media):
	targets = summary.requested
	cache = MetadataCache(Requests(client, policy))
	queue = asyncio.Queue(maxsize=summary.concurrency)
	resolved = {}
	resolved_events = [asyncio.Event() for _ in targets]
	media_run = None

	async def after_resolve(index, entity):
		'''
		Record the resolved channel; skip the target when an earlier target
		(lower index) resolved to the same channel. Earlier targets started
		first and record their resolution before collecting, so this waits
		at most for their resolution.
		'''
		resolved[index] = entity.id if entity else None
		resolved_events[index].set()
		if not entity:
			return
		for earlier in range(index):
			await resolved_events[earlier].wait()
			if resolved.get(earlier) == entity.id:
				raise DuplicateTarget(targets[earlier])

	async def process(index):
		target = targets[index]
		try:
			result = await collect_channel(
				cache.requests,
				cache,
				index,
				target,
				output_folder=summary.output_folder,
				metadata_only=metadata_only,
				window=window,
				update=update,
				after_resolve=functools.partial(after_resolve, index),
				profile=summary.profile,
				histories=writer.histories,
				confirmed=confirmed,
				media=media
			)
			outcome = ('result', result)
		except TargetUnavailable:
			outcome = ('not_found', NOT_FOUND_REASON)
		except DuplicateTarget as exc:
			outcome = ('duplicate', exc.alias_of)
		except ReplaceNotConfirmed as exc:
			print(f'> {target}: {exc}.', file=sys.stderr)
			outcome = ('failed', str(exc), exc.channel_id)
		except (PaginationStalled, HistoryUnreadable, HistoryRefused, RequestFailed) as exc:
			print(f'> {target}: {exc}; messages not saved.', file=sys.stderr)
			outcome = ('failed', str(exc))
		except SESSION_ERRORS as exc:
			raise session_rejected(exc) from None
		finally:
			resolved_events[index].set()

		await queue.put((index, outcome))
		summary.peak_queued_results = max(summary.peak_queued_results, queue.qsize())

	def record(index, kind, payload, channel_id=None):
		'''
		Writer side: commit a result or record a failure. Only this
		function touches output files and target statuses.
		'''
		entry = status.target(index)
		target = targets[index]
		if kind == 'result':
			try:
				takeover = writer.commit(payload)
			except StorageError as exc:
				entry.update(status='failed', reason=f'storage error: {exc}')
				raise
			print(f'> Saved {target}')
			entry.update(
				status='succeeded',
				channel_id=payload.channel_id,
				history=payload.history_mode,
				messages=payload.messages
			)
			if payload.min_id is not None:
				entry['min_id'] = payload.min_id
			if payload.new_messages is not None:
				entry['new_messages'] = payload.new_messages
			if takeover:
				entry.update(takeover)
				summary.takeovers.append((target, takeover['previous_profile_name']))
			if payload.history_mode == 'not_collected' and writer.has_history(
					target, payload.channel_id):
				entry['stale_history'] = True
			if media_run is not None:
				# only now that the history is committed
				media_run.request(payload)
		elif kind == 'duplicate':
			entry.update(status='duplicate', alias_of=payload)
		else:
			if kind == 'not_found':
				writer.record_not_found(index, target)
			entry.update(status='failed', reason=payload)
			if channel_id is not None:
				entry['channel_id'] = channel_id

	async def write_results():
		while True:
			item = await queue.get()
			if item is None:
				return
			record(item[0], *item[1])

	order = iter(range(len(targets)))

	async def worker():
		for index in order:
			await process(index)

	tasks = []
	try:
		if media is not None:
			# before any channel request: a ledger that cannot be used stops the run
			media_run = _open_media(client, summary, policy, status, writer, media)
		workers = [asyncio.create_task(worker()) for _ in range(summary.concurrency if targets else 0)]
		writer_task = asyncio.create_task(write_results())
		tasks = workers + [writer_task]
		await _supervise(workers, writer_task, queue)
		if media_run is not None:
			# every history is committed: media cannot delay or cost any of them
			await media_run.download(summary.concurrency)
	except BaseException as exc:
		summary.stop_reason = stop_reason_for(exc)
		await _cancel_all(tasks)
		await _close_quietly(cache)
		if not isinstance(exc, StorageError):
			# results already collected and waiting for the writer
			_drain(queue, record)
		_finish(writer, cache, status, summary, policy, media_run, failing=True)
		if isinstance(exc, (AccountStopped, SessionRejected)):
			# a planned stop: the summary carries stop_reason
			return
		raise
	else:
		await cache.close()
		_finish(writer, cache, status, summary, policy, media_run, failing=False)
	finally:
		if media_run is not None:
			media_run.close()


async def _supervise(workers, writer_task, queue):
	'''
	Wait for every worker, then let the writer finish the queue. A failing
	worker or writer raises at once (the caller cancels the rest).
	'''
	gathered = asyncio.gather(*workers)
	# a cancelled run leaves gathered with CancelledError: mark it retrieved
	gathered.add_done_callback(lambda f: f.cancelled() or f.exception())
	pending = {gathered, writer_task}
	while gathered in pending:
		done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
		if writer_task in done:
			writer_task.result()
			raise RuntimeError('result writer stopped before the workers')
	gathered.result()
	await queue.put(None)
	await writer_task


async def _cancel_all(tasks):
	for task in tasks:
		task.cancel()
	await asyncio.gather(*tasks, return_exceptions=True)


def _drain(queue, record):
	while not queue.empty():
		item = queue.get_nowait()
		if item is None:
			continue
		try:
			record(item[0], *item[1])
		except StorageError:
			return


async def _close_quietly(cache):
	try:
		await cache.close()
	except BaseException:
		pass


def _status_fields(writer):
	'''
	The writer's sidecar keys; none for a writer without status_fields.
	'''
	fields = getattr(writer, 'status_fields', None)
	return fields() if fields is not None else {}


def _finish(writer, cache, status, summary, policy, media_run=None, *, failing):
	'''
	End the media run (its downloads are over), finalize aggregates from
	the committed channels, fill the summary from the target statuses
	(input order), and close the sidecar. A storage error while finishing
	is raised unless the run is already failing with another error.
	'''
	for index, entry in enumerate(status.data['targets']):
		target = summary.requested[index]
		if entry['status'] == 'pending':
			entry['status'] = 'cancelled'
			summary.cancelled.append(target)
		elif entry['status'] == 'succeeded':
			summary.succeeded.append(target)
			if entry.get('messages') is not None:
				summary.saved_messages.append((target, entry['messages'], entry.get('new_messages')))
			if entry.get('history') == 'created':
				summary.collected_in_full.append(target)
			if entry.get('stale_history'):
				summary.stale_histories.append(target)
		elif entry['status'] == 'duplicate':
			summary.duplicates.append((target, entry['alias_of']))
		else:
			summary.failed.append((target, entry.get('reason')))

	summary.requests = policy.stats_record()
	summary.peak_in_flight = policy.peak_active
	if media_run is not None:
		summary.media = status.data['media'] = media_run.finish(summary.stop_reason)
		# media requests are counted in the media block: the collection's
		# statistics (sidecar, database) keep their own method classes
		summary.requests = {
			name: stats for name, stats in summary.requests.items() if name not in MEDIA_METHOD_CLASSES
		}

	error = None
	try:
		writer.finalize(cache.outcomes)
	except StorageError as exc:
		error = exc
		summary.stop_reason = summary.stop_reason or f'storage error: {exc}'

	if summary.stop_reason is not None:
		state = INTERRUPTED
	elif summary.failed:
		state = PARTIAL
	else:
		state = COMPLETE

	counts = {
		'given': status.data['parameters'].get('targets_given'),
		'requested': len(summary.requested),
		'succeeded': len(summary.succeeded),
		'failed': len(summary.failed),
		'duplicates': len(summary.duplicates),
		'cancelled': len(summary.cancelled)
	}
	try:
		writer.finish(
			state,
			summary=counts,
			stop_reason=summary.stop_reason,
			requests=summary.requests,
			targets=status.data['targets']
		)
	except StorageError as exc:
		error = error or exc
		summary.stop_reason = summary.stop_reason or f'storage error: {exc}'
		state = INTERRUPTED

	try:
		status.data.update(_status_fields(writer))
		status.finish(
			state,
			summary=counts,
			stop_reason=summary.stop_reason,
			requests=summary.requests
		)
	except StorageError as exc:
		error = error or exc

	if error is not None and not failing:
		raise error
