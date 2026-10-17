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
import functools
import os
import sys

# import local submodules
from collector.channel import collect_channel
from collector.history import HistoryWindow
from collector.outcomes import SESSION_ERRORS, CollectionSummary, \
	DuplicateTarget, PaginationStalled, SessionRejected, TargetUnavailable, \
	stop_reason_for
from collector.rpc import MetadataCache, Requests
from request_policy import AccountStopped, RequestFailed, RequestPolicy
from storage.files import history_path
from storage.records import COMPLETE, INTERRUPTED, PARTIAL, \
	HistoryUnreadable, StorageError
from storage.status import StatusFile
from storage.writer import OutputLock, OutputWriter, check_unlocked
from utils import create_dirs


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
		policy=None
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

	Missing/inaccessible targets, stalled pagination, unreadable saved
	histories (--update), and requests that keep failing transiently fail
	only that target; a target resolving to an earlier target's channel
	is skipped as a duplicate. An account stop (flood wait), a rejected
	session, a storage error, any other exception, or cancellation ends the
	run: completed channels stay written, aggregates are finalized from
	them, and collection-status.json says "interrupted". The output folder
	is locked for the run, and the client is disconnected exactly once
	after every worker has finished.
	'''
	window = window or HistoryWindow(min_id=min_id)
	summary = CollectionSummary(requested=list(targets), output_folder=output_folder)
	summary.concurrency = max(1, min(concurrency, len(summary.requested)))

	check_unlocked(output_folder)
	client = client_factory()
	try:
		await client.connect()
		await authorize(client)

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
				update=update
			)
	finally:
		await client.disconnect()

	return summary


async def _collect_locked(client, summary, policy, parameters, *, metadata_only, window, update):
	targets = summary.requested
	status = StatusFile(summary.output_folder, targets, parameters)
	status.write()

	writer = OutputWriter(summary.output_folder)
	cache = MetadataCache(Requests(client, policy))
	queue = asyncio.Queue(maxsize=summary.concurrency)
	resolved = {}
	resolved_events = [asyncio.Event() for _ in targets]

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
				after_resolve=functools.partial(after_resolve, index)
			)
			outcome = ('result', result)
		except TargetUnavailable:
			outcome = ('not_found', NOT_FOUND_REASON)
		except DuplicateTarget as exc:
			outcome = ('duplicate', exc.alias_of)
		except (PaginationStalled, HistoryUnreadable, RequestFailed) as exc:
			print(f'> {target}: {exc}; messages not saved.', file=sys.stderr)
			outcome = ('failed', str(exc))
		except SESSION_ERRORS as exc:
			raise SessionRejected(
				f'Telegram rejected the session ({type(exc).__name__}); log in again'
			) from None
		finally:
			resolved_events[index].set()

		await queue.put((index, outcome))
		summary.peak_queued_results = max(summary.peak_queued_results, queue.qsize())

	def record(index, kind, payload):
		'''
		Writer side: commit a result or record a failure. Only this
		function touches output files and target statuses.
		'''
		entry = status.target(index)
		target = targets[index]
		if kind == 'result':
			try:
				writer.commit(payload)
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
			if payload.history_mode == 'not_collected' and os.path.isfile(
					history_path(summary.output_folder, target)):
				entry['stale_history'] = True
		elif kind == 'duplicate':
			entry.update(status='duplicate', alias_of=payload)
		else:
			if kind == 'not_found':
				writer.record_not_found(index, target)
			entry.update(status='failed', reason=payload)

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

	workers = [asyncio.create_task(worker()) for _ in range(summary.concurrency if targets else 0)]
	writer_task = asyncio.create_task(write_results())
	try:
		await _supervise(workers, writer_task, queue)
	except BaseException as exc:
		summary.stop_reason = stop_reason_for(exc)
		await _cancel_all(workers + [writer_task])
		await _close_quietly(cache)
		if not isinstance(exc, StorageError):
			# results already collected and waiting for the writer
			_drain(queue, record)
		_finish(writer, cache, status, summary, policy, failing=True)
		if isinstance(exc, (AccountStopped, SessionRejected)):
			# a planned stop: the summary carries stop_reason
			return
		raise

	await cache.close()
	_finish(writer, cache, status, summary, policy, failing=False)


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


def _finish(writer, cache, status, summary, policy, *, failing):
	'''
	Finalize aggregates from the committed channels, fill the summary from
	the target statuses (input order), and close the sidecar. A storage
	error while finishing is raised unless the run is already failing with
	another error.
	'''
	for index, entry in enumerate(status.data['targets']):
		target = summary.requested[index]
		if entry['status'] == 'pending':
			entry['status'] = 'cancelled'
			summary.cancelled.append(target)
		elif entry['status'] == 'succeeded':
			summary.succeeded.append(target)
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

	try:
		status.finish(
			state,
			summary={
				'given': status.data['parameters'].get('targets_given'),
				'requested': len(summary.requested),
				'succeeded': len(summary.succeeded),
				'failed': len(summary.failed),
				'duplicates': len(summary.duplicates),
				'cancelled': len(summary.cancelled)
			},
			stop_reason=summary.stop_reason,
			requests=summary.requests
		)
	except StorageError as exc:
		error = error or exc

	if error is not None and not failing:
		raise error
