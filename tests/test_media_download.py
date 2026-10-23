# -*- coding: utf-8 -*-
'''
The download adapter's normal paths: a complete download and what it
records, staging and publication, the per-file and per-run limits
(known and unknown sizes), short and oversize streams, blobs reused
through the ledger, conflicts at the final name, and in-flight
duplicates coalesced so a file is requested and reserved once.
'''

# import modules
import asyncio
import os
import unittest

from unittest import mock

import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_download_support import OTHER_DC, DownloadTestCase, any_request, content, sha256
from media_support import RUN_A
from telegram_tracker.media.layout import file_name
from telegram_tracker.media.ledger import COMPLETE, FAILED, REQUESTED, SKIPPED, LedgerError
from telegram_tracker.media.limits import PULL_BYTES, MediaBudget, MediaLimits, whole_pulls


class CompleteTests(DownloadTestCase):

	async def test_a_document_is_stored_and_recorded(self):
		data = content(2 * PULL_BYTES + 123)
		job = self.fake.post(7, self.fake.add_document(900, data))
		self.request(job)
		outcome = await self.downloader.download(job)
		digest = sha256(data)
		self.assertEqual(outcome.path, f'1001/videos/{file_name(7, digest, "mp4")}')
		self.assertEqual((outcome.state, outcome.downloaded), (COMPLETE, True))
		self.assertEqual(self.stored(outcome), data)

		item = self.ledger.item(job.key)
		self.assertEqual((item['state'], item['sha256'], item['actual_bytes']), (COMPLETE, digest, len(data)))
		blob = self.ledger.blob(1001, digest)
		self.assertEqual((blob['path'], blob['bytes'], blob['first_run_uuid']), (outcome.path, len(data), RUN_A))
		[attempt] = self.ledger.attempts(job.key)
		self.assertEqual(
			(attempt['outcome'], attempt['bytes_received'], attempt['app_requests'], attempt['refreshed']),
			(COMPLETE, len(data), 3, 0))
		run = self.ledger.run(RUN_A)
		self.assertEqual(
			(run['app_requests'], run['retries'], run['flood_waits'], run['refreshes'], run['bytes_received']),
			(3, 0, 0, 0, len(data)))
		self.assertEqual(self.policy.stats_record(), {'media': {
			'requests': 3, 'retries': 0, 'flood_waits': 0, 'flood_wait_seconds': 0}})
		self.assertEqual(self.partial_files(), [])
		self.assertAligned()

	async def test_a_photo(self):
		data = content(40000)
		job = self.fake.post(8, self.fake.add_photo(901, data, dc=OTHER_DC))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual(outcome.path, f'1001/images/{file_name(8, sha256(data), "jpg")}')
		self.assertEqual(self.stored(outcome), data)
		self.assertEqual([r.media_id for r in self.fake.get_files()], [901])

	async def test_pulls_are_spaced_by_the_policy(self):
		data = content(3 * PULL_BYTES)
		job = self.fake.post(9, self.fake.add_document(902, data))
		self.request(job)
		times = []
		real_call = self.fake._call

		async def timed(*args, **kwargs):
			times.append(asyncio.get_running_loop().time())
			return await real_call(*args, **kwargs)

		self.fake.client._call = timed
		await self.downloader.download(job)
		self.assertEqual([round(b - a, 6) for a, b in zip(times, times[1:])], [0.25, 0.25])

	async def test_the_staging_file_is_in_the_run_folder_until_published(self):
		data = content(PULL_BYTES + 1)
		job = self.fake.post(10, self.fake.add_document(903, data))
		self.request(job)
		seen = []

		async def look(record):
			seen.append(self.partial_files())

		self.fake.fault(lambda r: r.offset == PULL_BYTES, look)
		await self.downloader.download(job)
		folder = os.path.join(self.layout.partial_root, RUN_A)
		self.assertEqual(seen, [[os.path.join(folder, '1001-10-903.part')]])
		self.assertEqual(os.listdir(folder), [])
		self.downloader.finish()
		self.assertEqual(os.listdir(self.layout.partial_root), [])

	async def test_an_item_that_is_not_requested_is_left_alone(self):
		job = self.fake.post(11, self.fake.add_document(904, content(10)))
		with self.assertRaises(LedgerError) as caught:
			await self.downloader.download(job)
		self.assertIn('no media item', str(caught.exception))
		self.request(job)
		self.ledger.skip(RUN_A, job.key, 'over_file_limit')
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (SKIPPED, 'over_file_limit'))
		self.assertEqual(self.fake.requests, [])


class SizeTests(DownloadTestCase):

	async def download(self, media_id, data, *, size=None, message_id=None):
		job = self.fake.post(message_id or media_id, self.fake.add_document(media_id, data, size=size))
		self.request(job)
		return job, await self.downloader.download(job)

	async def test_a_short_stream(self):
		job, outcome = await self.download(910, content(2 * PULL_BYTES + 5), size=3 * PULL_BYTES + 10)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'size_mismatch'))
		self.assertEqual(len(self.fake.get_files()), 3)
		self.assertEqual(self.ledger.blob_hint(1001, 'document', 910), None)
		self.assertFalse(os.path.exists(os.path.join(self.layout.root, '1001', 'videos'))
			and os.listdir(os.path.join(self.layout.root, '1001', 'videos')))
		self.assertEqual(self.partial_files(), [])

	async def test_an_oversize_stream_stops_within_its_reservation(self):
		job, outcome = await self.download(911, content(3 * PULL_BYTES), size=2 * PULL_BYTES - 100)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'size_mismatch'))
		self.assertEqual([r.offset for r in self.fake.get_files()], [0, PULL_BYTES])
		self.assertEqual(self.ledger.attempts(job.key)[0]['bytes_received'], 2 * PULL_BYTES)
		self.assertEqual((self.downloader.budget.received, self.downloader.budget.reserved), (2 * PULL_BYTES, 0))

	async def test_a_stream_ending_early_on_a_chunk_boundary(self):
		job, outcome = await self.download(912, content(PULL_BYTES), size=PULL_BYTES + 10)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'size_mismatch'))
		self.assertEqual([r.offset for r in self.fake.get_files()], [0, PULL_BYTES])

	async def test_an_empty_document(self):
		job, outcome = await self.download(913, b'')
		self.assertEqual((outcome.state, self.stored(outcome)), (COMPLETE, b''))
		self.assertEqual(self.fake.requests, [])


class LimitTests(DownloadTestCase):

	def use(self, limits=MediaLimits(), budget=None):
		self.downloader = self.make_downloader(limits, budget)
		return self.downloader

	async def test_a_known_size_over_the_file_limit_makes_no_request(self):
		self.use(MediaLimits(max_file=PULL_BYTES))
		job = self.fake.post(1, self.fake.add_document(920, content(PULL_BYTES + 1)))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason, outcome.downloaded), (SKIPPED, 'over_file_limit', False))
		self.assertEqual(self.fake.requests, [])
		self.assertEqual(self.ledger.attempts(job.key), [])

	async def test_a_known_size_at_the_file_limit_is_downloaded(self):
		self.use(MediaLimits(max_file=PULL_BYTES + 1))
		job = self.fake.post(1, self.fake.add_document(921, content(PULL_BYTES + 1)))
		self.request(job)
		self.assertEqual((await self.downloader.download(job)).state, COMPLETE)

	async def test_an_unknown_size_stops_one_pull_over_the_file_limit(self):
		self.use(MediaLimits(max_file=PULL_BYTES + 1))
		job = self.fake.post(1, self.fake.add_photo(922, content(5 * PULL_BYTES), known=False))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (SKIPPED, 'over_file_limit'))
		self.assertEqual([r.offset for r in self.fake.get_files()], [0, PULL_BYTES])
		self.assertEqual(self.ledger.attempts(job.key)[0]['bytes_received'], 2 * PULL_BYTES)
		self.assertEqual(self.partial_files(), [])

	async def test_an_unknown_size_within_the_file_limit(self):
		self.use(MediaLimits(max_file=2 * PULL_BYTES))
		data = content(2 * PULL_BYTES - 1)
		job = self.fake.post(1, self.fake.add_photo(923, data, known=False))
		self.request(job)
		self.assertEqual(self.stored(await self.downloader.download(job)), data)

	async def test_a_known_size_that_does_not_fit_the_run_limit(self):
		budget = MediaBudget(PULL_BYTES)
		self.use(MediaLimits(max_total=PULL_BYTES), budget)
		job = self.fake.post(1, self.fake.add_document(924, content(PULL_BYTES + 1)))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (SKIPPED, 'over_run_limit'))
		self.assertEqual(self.fake.requests, [])
		self.assertEqual((budget.received, budget.reserved), (0, 0))

	async def test_the_run_limit_counts_what_was_received(self):
		size = PULL_BYTES + 10
		budget = MediaBudget(3 * PULL_BYTES)
		self.use(MediaLimits(max_total=budget.total), budget)
		first = self.fake.post(1, self.fake.add_document(925, content(size)))
		second = self.fake.post(2, self.fake.add_document(926, content(size, seed=3)))
		third = self.fake.post(3, self.fake.add_document(927, content(10, seed=4)))
		self.request(first, second, third)
		self.assertEqual((await self.downloader.download(first)).state, COMPLETE)
		self.assertEqual((budget.received, budget.reserved), (size, 0))
		# what is left (3 pulls minus the bytes received) cannot hold the 2 pulls it needs
		self.assertEqual((await self.downloader.download(second)).reason, 'over_run_limit')
		self.assertEqual((await self.downloader.download(third)).state, COMPLETE)
		self.assertLessEqual(budget.received, budget.total)

	async def test_an_unknown_size_reserves_pull_by_pull(self):
		budget = MediaBudget(2 * PULL_BYTES)
		self.use(MediaLimits(max_total=budget.total), budget)
		job = self.fake.post(1, self.fake.add_photo(928, content(3 * PULL_BYTES), known=False))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (SKIPPED, 'over_run_limit'))
		self.assertEqual(len(self.fake.get_files()), 2)
		self.assertEqual((budget.received, budget.reserved), (2 * PULL_BYTES, 0))


class ReuseTests(DownloadTestCase):

	async def test_the_same_media_in_another_message_is_reused_with_no_request(self):
		data = content(PULL_BYTES + 5)
		media = self.fake.add_document(930, data)
		first, second = self.fake.post(1, media), self.fake.post(2, media)
		self.request(first, second)
		done = await self.downloader.download(first)
		self.fake.requests.clear()
		reused = await self.downloader.download(second)
		self.assertEqual((reused.state, reused.path, reused.downloaded), (COMPLETE, done.path, False))
		self.assertEqual(self.fake.requests, [])
		self.assertEqual(self.ledger.attempts(second.key), [])
		self.assertEqual(sorted(os.listdir(os.path.join(self.layout.root, '1001', 'videos'))),
			[os.path.basename(done.path)])

	async def test_the_same_content_under_another_media_id_shares_the_blob(self):
		data = content(PULL_BYTES + 5)
		first = self.fake.post(1, self.fake.add_document(931, data))
		second = self.fake.post(2, self.fake.add_document(932, data))
		self.request(first, second)
		done = await self.downloader.download(first)
		other = await self.downloader.download(second)
		self.assertEqual((other.state, other.path, other.downloaded), (COMPLETE, done.path, True))
		self.assertEqual(len(os.listdir(os.path.join(self.layout.root, '1001', 'videos'))), 1)
		self.assertEqual(self.ledger.item(second.key)['sha256'], sha256(data))
		self.assertEqual(self.partial_files(), [])

	async def test_a_changed_stored_file_is_never_reused_or_touched(self):
		data = content(1000)
		media = self.fake.add_document(933, data)
		first, second = self.fake.post(1, media), self.fake.post(2, media)
		self.request(first, second)
		done = await self.downloader.download(first)
		path = self.layout.path_of(done.path)
		with open(path, 'wb') as writer:
			writer.write(b'x' * 1000)
		# a new run verifies the file again
		self.downloader = self.make_downloader()
		self.fake.requests.clear()
		outcome = await self.downloader.download(second)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'stored_file_differs'))
		self.assertEqual(self.fake.requests, [])
		with open(path, 'rb') as reader:
			self.assertEqual(reader.read(), b'x' * 1000)

	async def test_downloaded_content_matching_a_changed_stored_file(self):
		data = content(1000)
		first = self.fake.post(1, self.fake.add_document(935, data))
		second = self.fake.post(2, self.fake.add_document(936, data))
		self.request(first, second)
		path = self.layout.path_of((await self.downloader.download(first)).path)
		with open(path, 'wb') as writer:
			writer.write(b'x' * 1000)
		self.downloader = self.make_downloader()
		outcome = await self.downloader.download(second)
		self.assertEqual((outcome.state, outcome.reason, outcome.downloaded), (FAILED, 'stored_file_differs', True))
		with open(path, 'rb') as reader:
			self.assertEqual(reader.read(), b'x' * 1000)
		self.assertEqual(self.partial_files(), [])

	async def test_a_stored_file_is_verified_once_per_run(self):
		data = content(1000)
		media = self.fake.add_document(934, data)
		jobs = [self.fake.post(n, media) for n in (1, 2, 3)]
		self.request(*jobs)
		await self.downloader.download(jobs[0])
		path = self.layout.path_of(self.ledger.blob(1001, sha256(data))['path'])
		with open(path, 'wb') as writer:
			writer.write(b'y' * 1000)
		# verified when it was written in this run; a new run would see the change
		self.assertEqual((await self.downloader.download(jobs[1])).state, COMPLETE)
		self.downloader = self.make_downloader()
		self.assertEqual((await self.downloader.download(jobs[2])).reason, 'stored_file_differs')


class FinalNameTests(DownloadTestCase):

	def final_path(self, message_id, data, ext='mp4'):
		path = os.path.join(self.layout.root, '1001', 'videos', file_name(message_id, sha256(data), ext))
		os.makedirs(os.path.dirname(path), exist_ok=True)
		return path

	async def test_other_content_at_the_final_name_is_kept(self):
		data = content(1000)
		path = self.final_path(1, data)
		with open(path, 'wb') as writer:
			writer.write(b'other')
		job = self.fake.post(1, self.fake.add_document(940, data))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual((outcome.state, outcome.reason), (FAILED, 'path_conflict'))
		with open(path, 'rb') as reader:
			self.assertEqual(reader.read(), b'other')
		self.assertIsNone(self.ledger.blob(1001, sha256(data)))
		self.assertEqual(self.partial_files(), [])

	async def test_the_same_content_at_the_final_name_is_adopted(self):
		data = content(1000)
		path = self.final_path(1, data)
		with open(path, 'wb') as writer:
			writer.write(data)
		before = os.stat(path)
		job = self.fake.post(1, self.fake.add_document(941, data))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual(outcome.state, COMPLETE)
		self.assertTrue(os.path.samestat(os.stat(path), before))

	async def test_a_folder_at_the_final_name_is_a_conflict(self):
		data = content(1000)
		os.mkdir(self.final_path(1, data))
		job = self.fake.post(1, self.fake.add_document(942, data))
		self.request(job)
		self.assertEqual((await self.downloader.download(job)).reason, 'path_conflict')

	async def test_a_file_appearing_during_publication(self):
		from telegram_tracker.media import staging

		real_link = staging.link_new
		# distinct content per case, so neither reuses the other's blob
		first, second = content(1000, seed=1), content(1000, seed=2)
		cases = {'same content': (first, first, COMPLETE), 'other content': (second, b'z' * 1000, FAILED)}
		for n, (label, (data, found, state)) in enumerate(cases.items(), start=1):
			with self.subTest(label):
				def racing(tmp, path, found=found):
					with open(path, 'wb') as writer:
						writer.write(found)
					return real_link(tmp, path)

				job = self.fake.post(n, self.fake.add_document(943 + n, data))
				self.request(job)
				with mock.patch.object(staging, 'link_new', racing):
					outcome = await self.downloader.download(job)
				self.assertEqual(outcome.state, state)
				self.assertEqual(outcome.downloaded, True)
				with open(os.path.join(self.layout.root, '1001', 'videos', file_name(n, sha256(data), 'mp4')),
						'rb') as reader:
					self.assertEqual(reader.read(), found)


class CoalesceTests(DownloadTestCase):

	async def test_concurrent_duplicates_request_and_reserve_once(self):
		data = content(2 * PULL_BYTES + 1)
		size = whole_pulls(len(data))
		budget = MediaBudget(size)
		self.downloader = self.make_downloader(MediaLimits(max_total=size), budget)
		media = self.fake.add_document(950, data)
		jobs = [self.fake.post(n, media) for n in (1, 2, 3)]
		self.request(*jobs)
		outcomes = await asyncio.gather(*(self.downloader.download(job) for job in jobs))
		self.assertEqual([o.state for o in outcomes], [COMPLETE] * 3)
		self.assertEqual({o.path for o in outcomes}, {outcomes[0].path})
		self.assertEqual([o.downloaded for o in outcomes], [True, False, False])
		self.assertEqual(len(self.fake.get_files()), 3)
		self.assertEqual(budget.received, len(data))

	async def test_other_media_are_not_held_back(self):
		# four spaced pulls: the small file's one pull is admitted between them
		first = self.fake.post(1, self.fake.add_document(951, content(4 * PULL_BYTES)))
		second = self.fake.post(2, self.fake.add_document(952, content(10)))
		self.request(first, second)
		order = []

		async def note(job):
			await self.downloader.download(job)
			order.append(job.message_id)

		await asyncio.gather(note(first), note(second))
		self.assertEqual(order, [2, 1])

	async def test_a_follower_downloads_itself_when_the_first_fails(self):
		data = content(1000)
		media = self.fake.add_document(953, data)
		jobs = [self.fake.post(n, media) for n in (1, 2)]
		self.request(*jobs)
		self.fake.fault(any_request(), ConnectionError('reset'), times=4)
		first, second = await asyncio.gather(*(self.downloader.download(job) for job in jobs))
		self.assertEqual((first.state, first.reason), (FAILED, 'request_failed'))
		self.assertEqual((second.state, second.downloaded), (COMPLETE, True))

	async def test_a_cancelled_follower_does_not_cancel_the_download(self):
		data = content(2 * PULL_BYTES)
		media = self.fake.add_document(954, data)
		leader_job, follower_job = self.fake.post(1, media), self.fake.post(2, media)
		self.request(leader_job, follower_job)
		leader = asyncio.ensure_future(self.downloader.download(leader_job))
		await asyncio.sleep(0)
		follower = asyncio.ensure_future(self.downloader.download(follower_job))
		await asyncio.sleep(0)
		follower.cancel()
		outcome = await leader
		with self.assertRaises(asyncio.CancelledError):
			await follower
		self.assertEqual(outcome.state, COMPLETE)
		self.assertEqual(self.ledger.item(follower_job.key)['state'], REQUESTED)


class CounterTests(DownloadTestCase):

	async def test_run_counters_add_up_over_items(self):
		jobs = [self.fake.post(n, self.fake.add_document(960 + n, content(PULL_BYTES * n + 1, seed=n)))
			for n in (1, 2)]
		self.request(*jobs)
		for job in jobs:
			await self.downloader.download(job)
		attempts = [self.ledger.attempts(job.key)[0] for job in jobs]
		run = self.ledger.run(RUN_A)
		self.assertEqual(run['app_requests'], sum(a['app_requests'] for a in attempts))
		self.assertEqual(run['bytes_received'], sum(a['bytes_received'] for a in attempts))
		self.assertEqual(run['app_requests'], 2 + 3)

	async def test_the_ledger_is_never_in_a_transaction_at_a_request(self):
		jobs = [self.fake.post(n, self.fake.add_document(970 + n, content(PULL_BYTES + n))) for n in (1, 2, 3)]
		self.request(*jobs)
		await asyncio.gather(*(self.downloader.download(job) for job in jobs))
		self.assertEqual({r.in_transaction for r in self.fake.requests}, {False})
		self.assertEqual(len(self.fake.requests), 6)


if __name__ == '__main__':
	unittest.main()
