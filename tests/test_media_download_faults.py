# -*- coding: utf-8 -*-
'''
The download adapter's failure mapping, on the fake file service: flood
waits within and over the limit, timeouts (the policy's and Telethon's),
persistent transient errors, other RPC errors and a CDN redirect,
cancellation, a full disk, an unusable staging folder, and expired file
references (one refresh per attempt, then the file starts over; a
second expiry, a deleted message, or changed media fails the item).
'''

# import modules
import asyncio
import errno
import os
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from telethon import errors, types

from media_download_support import CHANNEL, DownloadTestCase, any_request, at_offset, content
from media_support import RUN_A, make_link, remove_link
from telegram_tracker.media import staging
from telegram_tracker.media.download import CDN_REDIRECT, MediaStopped
from telegram_tracker.media.ledger import COMPLETE, FAILED, REQUESTED
from telegram_tracker.media.limits import PULL_BYTES
from telegram_tracker.media.staging import is_disk_full
from telegram_tracker.request_policy import AccountStopped


def flood(seconds):
	return errors.FloodWaitError(request=None, capture=seconds)


class FaultTestCase(DownloadTestCase):

	def job(self, media_id, size=2 * PULL_BYTES + 10, message_id=None):
		self.data = content(size, seed=media_id)
		job = self.fake.post(message_id or media_id, self.fake.add_document(media_id, self.data))
		self.request(job)
		return job

	def assertEnded(self, job, state, reason):
		item = self.ledger.item(job.key)
		self.assertEqual((item['state'], item['reason']), (state, reason))
		attempt = self.ledger.attempts(job.key)[-1]
		self.assertEqual((attempt['outcome'], attempt['reason']), (state, reason))
		self.assertIsNotNone(attempt['ended_at'])
		self.assertEqual(self.partial_files(), [])
		self.assertEqual(sorted(self.fake.borrowed), sorted(self.fake.returned))
		self.assertEqual(self.downloader.budget.reserved, 0)


class FloodTests(FaultTestCase):

	async def test_a_flood_wait_within_the_limit_is_waited_out(self):
		job = self.job(1)
		self.fake.fault(at_offset(PULL_BYTES), flood(5))
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), self.data)
		self.assertGreaterEqual(asyncio.get_running_loop().time(), 5)
		self.assertEqual([(r.offset, r.outcome) for r in self.fake.get_files()],
			[(0, 'ok'), (PULL_BYTES, 'fault'), (PULL_BYTES, 'ok'), (2 * PULL_BYTES, 'ok')])
		run = self.ledger.run(RUN_A)
		self.assertEqual((run['app_requests'], run['retries'], run['flood_waits']), (4, 1, 1))
		self.assertEqual(self.ledger.attempts(job.key)[0]['app_requests'], 4)

	async def test_a_flood_wait_over_the_limit_stops_the_account(self):
		job, queued = self.job(2), self.job(3)
		self.fake.fault(at_offset(PULL_BYTES), flood(600))
		with self.assertRaises(AccountStopped):
			await self.downloader.download(job)
		self.assertEnded(job, FAILED, 'flood_stop')
		self.assertEqual(self.ledger.run(RUN_A)['flood_waits'], 1)
		before = len(self.fake.requests)
		with self.assertRaises(AccountStopped):
			await self.downloader.download(queued)
		self.assertEqual(self.ledger.item(queued.key)['state'], REQUESTED)
		self.assertEqual(self.ledger.attempts(queued.key), [])
		self.assertEqual(len(self.fake.requests), before)

	async def test_flood_retries_running_out_stop_the_account(self):
		self.policy = self.make_policy(max_retries=1)
		self.downloader = self.make_downloader()
		job = self.job(4)
		self.fake.fault(at_offset(0), flood(5), times=2)
		with self.assertRaises(AccountStopped):
			await self.downloader.download(job)
		self.assertEnded(job, FAILED, 'flood_stop')


class TransientTests(FaultTestCase):

	async def test_a_pull_that_times_out_is_retried(self):
		job = self.job(10)
		self.fake.fault(at_offset(PULL_BYTES), 'hang')
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), self.data)
		self.assertEqual(self.ledger.run(RUN_A)['retries'], 1)
		self.assertEqual(sorted(self.fake.borrowed), sorted(self.fake.returned))

	async def test_telethon_timeouts_after_its_own_retry(self):
		job = self.job(11)
		self.fake.fault(at_offset(PULL_BYTES), errors.TimedOutError(None, 'Timeout'), times=2)
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), self.data)
		# Telethon retried once inside the pull; the policy then retried the pull
		self.assertEqual([r.outcome for r in self.fake.get_files() if r.offset == PULL_BYTES], ['fault', 'fault', 'ok'])
		self.assertEqual(self.ledger.attempts(job.key)[0]['app_requests'], 4)

	async def test_a_persistent_error_fails_the_item_and_the_run_goes_on(self):
		job, later = self.job(12), self.job(13)
		self.fake.fault(lambda r: r.media_id == 12 and r.offset == PULL_BYTES, ConnectionError('reset'), times=10)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'request_failed'))
		self.assertEnded(job, FAILED, 'request_failed')
		self.assertEqual(self.ledger.attempts(job.key)[0]['app_requests'], 1 + 4)
		self.assertEqual((await self.downloader.download(later)).state, COMPLETE)

	async def test_other_rpc_errors_and_a_cdn_redirect(self):
		cases = {
			'file id invalid': errors.FileIdInvalidError(request=None),
			'cdn redirect': CDN_REDIRECT(None),
			'location invalid': errors.LocationInvalidError(request=None)
		}
		for n, (label, error) in enumerate(cases.items(), start=20):
			with self.subTest(label):
				job = self.job(n)
				self.fake.fault(lambda r, m=n: r.media_id == m, error)
				outcome = await self.downloader.download(job)
				self.assertEqual((outcome.state, outcome.reason), (FAILED, 'request_failed'))
				self.assertEnded(job, FAILED, 'request_failed')
				self.assertEqual(self.ledger.attempts(job.key)[0]['app_requests'], 1)


class CancelTests(FaultTestCase):

	async def test_cancel_during_a_pull(self):
		job = self.job(30)
		started = asyncio.Event()

		async def hang(record):
			started.set()
			await asyncio.Event().wait()

		self.fake.fault(at_offset(PULL_BYTES), hang)
		task = asyncio.ensure_future(self.downloader.download(job))
		await started.wait()
		self.assertEqual(len(self.partial_files()), 1)
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertEnded(job, FAILED, 'interrupted')
		self.assertEqual(self.ledger.attempts(job.key)[0]['bytes_received'], PULL_BYTES)

	async def test_cancel_while_waiting_for_a_flood_cooldown(self):
		job = self.job(31)
		self.fake.fault(at_offset(0), flood(30))
		task = asyncio.ensure_future(self.downloader.download(job))
		while not self.fake.requests:
			await asyncio.sleep(0)
		await asyncio.sleep(1)
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertEnded(job, FAILED, 'interrupted')


class DiskTests(FaultTestCase):

	def test_disk_full_errors(self):
		class WindowsDiskFull(OSError):
			winerror = 112

		class WindowsHandleDiskFull(OSError):
			winerror = 39

		for exc in (OSError(errno.ENOSPC, 'No space left on device'), WindowsDiskFull('full'),
				WindowsHandleDiskFull('full')):
			self.assertTrue(is_disk_full(exc), exc)
		for exc in (OSError(errno.EACCES, 'denied'), ValueError('x'), OSError('other')):
			self.assertFalse(is_disk_full(exc), exc)

	async def test_a_full_disk_stops_media(self):
		job, queued = self.job(40), self.job(41)
		real_write = staging.PartialFile.write
		calls = []

		def write(partial, chunk):
			calls.append(len(chunk))
			if len(calls) == 2:
				raise OSError(errno.ENOSPC, 'No space left on device')
			return real_write(partial, chunk)

		with mock.patch.object(staging.PartialFile, 'write', write):
			with self.assertRaises(MediaStopped) as caught:
				await self.downloader.download(job)
		self.assertEqual(caught.exception.reason, 'disk_full')
		self.assertEnded(job, FAILED, 'disk_full')
		with self.assertRaises(MediaStopped):
			await self.downloader.download(queued)
		self.assertEqual(self.ledger.item(queued.key)['state'], REQUESTED)

	async def test_a_full_disk_when_syncing(self):
		job = self.job(42)
		with mock.patch.object(staging.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
			with self.assertRaises(MediaStopped):
				await self.downloader.download(job)
		self.assertEnded(job, FAILED, 'disk_full')

	async def test_another_write_error_fails_only_the_item(self):
		job, later = self.job(43), self.job(44)
		with mock.patch.object(staging.PartialFile, 'write', side_effect=OSError(errno.EIO, 'I/O error')):
			outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'storage_error'))
		self.assertEnded(job, FAILED, 'storage_error')
		self.assertEqual((await self.downloader.download(later)).state, COMPLETE)

	async def test_a_linked_staging_folder_stops_media(self):
		job, queued = self.job(45), self.job(46)
		elsewhere = os.path.join(self.tmp, 'elsewhere')
		os.mkdir(elsewhere)
		make_link(elsewhere, self.layout.partial_root)
		self.addCleanup(remove_link, self.layout.partial_root)
		with self.assertRaises(MediaStopped) as caught:
			await self.downloader.download(job)
		self.assertEqual(caught.exception.reason, 'storage_error')
		self.assertEqual(self.ledger.item(job.key)['reason'], 'storage_error')
		self.assertEqual(os.listdir(elsewhere), [])
		self.assertEqual(self.fake.requests, [])
		with self.assertRaises(MediaStopped):
			await self.downloader.download(queued)
		self.assertEqual(self.ledger.item(queued.key)['state'], REQUESTED)


class ReferenceTests(FaultTestCase):

	def expire_at(self, offset, media_id, times=1):
		async def expire(record):
			self.fake.expire('document', media_id)

		self.fake.fault(lambda r: r.media_id == media_id and r.offset == offset and r.outcome != 'expired', expire,
			times=times)

	async def test_an_expired_reference_is_refreshed_and_the_file_starts_over(self):
		job = self.job(50)
		self.expire_at(PULL_BYTES, 50)
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), self.data)
		self.assertEqual([(r.offset, r.reference, r.outcome) for r in self.fake.get_files()], [
			(0, b'ref-1', 'ok'), (PULL_BYTES, b'ref-1', 'expired'),
			(0, b'ref-2', 'ok'), (PULL_BYTES, b'ref-2', 'ok'), (2 * PULL_BYTES, b'ref-2', 'ok')])
		self.assertEqual(len(self.fake.lookups()), 1)
		attempt = self.ledger.attempts(job.key)[0]
		self.assertEqual((attempt['refreshed'], attempt['app_requests'], attempt['bytes_received']),
			(1, 6, PULL_BYTES + len(self.data)))
		run = self.ledger.run(RUN_A)
		self.assertEqual((run['refreshes'], run['app_requests']), (1, 6))
		self.assertEqual(self.policy.stats_record()['media_lookup']['requests'], 1)
		self.assertEqual(self.downloader.budget.received, PULL_BYTES + len(self.data))

	async def test_a_second_expiry_in_the_attempt_fails_it(self):
		job = self.job(51)
		self.expire_at(PULL_BYTES, 51, times=2)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'reference_expired'))
		self.assertEnded(job, FAILED, 'reference_expired')
		self.assertEqual(len(self.fake.lookups()), 1)

	async def test_the_restart_needs_room_in_the_run_limit(self):
		from telegram_tracker.media.limits import MediaBudget, MediaLimits, whole_pulls

		job = self.job(52)
		budget = MediaBudget(whole_pulls(len(self.data)) + PULL_BYTES - 1)
		self.downloader = self.make_downloader(MediaLimits(max_total=budget.total), budget)
		self.expire_at(PULL_BYTES, 52)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), ('skipped', 'over_run_limit'))
		self.assertEnded(job, 'skipped', 'over_run_limit')
		self.assertEqual((budget.received, budget.reserved), (PULL_BYTES, 0))

	async def test_refresh_outcomes(self):
		other = self.fake.add_document(99, content(10))
		cases = {
			'message deleted': (lambda key: self.fake.messages.pop(key), 'message_gone'),
			'media replaced': (lambda key: setattr(self.fake.messages[key], 'media', other), 'media_changed'),
			'media removed': (lambda key: setattr(self.fake.messages[key], 'media', None), 'media_changed'),
			'now a photo': (lambda key: setattr(self.fake.messages[key], 'media', self.fake.add_photo(53, b'p')),
				'media_changed'),
			'service message': (lambda key: self.fake.messages.__setitem__(key, types.MessageService(
				id=key[1], peer_id=types.PeerChannel(CHANNEL), date=None, action=types.MessageActionPinMessage())),
				'message_gone')
		}
		for n, (label, (change, reason)) in enumerate(cases.items(), start=60):
			with self.subTest(label):
				job = self.job(n)
				change((CHANNEL, n))
				self.expire_at(PULL_BYTES, n)
				outcome = await self.downloader.download(job)
				self.assertEqual((outcome.state, outcome.reason), (FAILED, reason))
				self.assertEnded(job, FAILED, reason)

	async def test_a_lookup_that_fails(self):
		cases = {
			'channel private': (errors.ChannelPrivateError(request=None), 'request_failed'),
			'dropped connections': (ConnectionError('reset'), 'request_failed')
		}
		for n, (label, (error, reason)) in enumerate(cases.items(), start=70):
			with self.subTest(label):
				job = self.job(n)
				self.expire_at(PULL_BYTES, n)
				self.fake.fault(any_request('GetMessages'), error, times=10)
				outcome = await self.downloader.download(job)
				self.assertEqual((outcome.state, outcome.reason), (FAILED, reason))
				self.assertEnded(job, FAILED, reason)
				self.fake.faults.clear()

	async def test_a_flood_on_the_lookup_stops_the_account(self):
		job = self.job(80)
		self.expire_at(PULL_BYTES, 80)
		self.fake.fault(any_request('GetMessages'), flood(600))
		with self.assertRaises(AccountStopped) as caught:
			await self.downloader.download(job)
		self.assertEqual(caught.exception.method_class, 'media_lookup')
		self.assertEnded(job, FAILED, 'flood_stop')


if __name__ == '__main__':
	unittest.main()
