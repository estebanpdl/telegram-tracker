# -*- coding: utf-8 -*-
'''
Chunk pulls through Telethon's real iter_download: a pull that fails is
retried by reopening iter_download at the offset reached, for files of
2, 3 and 5 chunks (exact and with a short last chunk). Every GetFile is
512 KiB-aligned with a 512 KiB limit, the successful offsets cover the
file once each, and the stored file is byte-identical. Also: data
centers, FILE_MIGRATE, and returning borrowed senders.
'''

# import modules
import unittest

import source_path  # noqa: F401  (imports telegram_tracker from src)

from telethon import types

from media_download_support import HOME_DC, OTHER_DC, DownloadTestCase, at_offset, content
from telegram_tracker.media.ledger import COMPLETE
from telegram_tracker.media.limits import PULL_BYTES
from telegram_tracker.media.location import MediaLocation
from telegram_tracker.media.stream import ChunkStream


class PullTestCase(DownloadTestCase):

	async def download_with_failures(self, media_id, size, fail_at, *, dc=OTHER_DC, known=True):
		'''
		Download a file of `size` whose pulls at the chunk numbers in
		`fail_at` fail once (a dropped connection, which the policy
		retries).
		'''
		data = content(size, seed=media_id)
		if known:
			media = self.fake.add_document(media_id, data, dc=dc)
		else:
			media = self.fake.add_photo(media_id, data, dc=dc, known=False)
		for chunk in fail_at:
			self.fake.fault(lambda r, o=chunk * PULL_BYTES, m=media_id: r.media_id == m and r.offset == o,
				ConnectionError('connection reset'))
		job = self.fake.post(media_id, media)
		self.request(job)
		outcome = await self.downloader.download(job)
		return data, job, outcome

	def assertCovered(self, media_id, data, fail_at, *, past_end=False):
		ok = [r.offset for r in self.fake.get_files('ok') if r.media_id == media_id]
		chunks = -(-len(data) // PULL_BYTES)
		expected = [n * PULL_BYTES for n in range(chunks)]
		if past_end:
			expected.append(chunks * PULL_BYTES)
		self.assertEqual(ok, expected)
		failed = [r.offset for r in self.fake.get_files('fault') if r.media_id == media_id]
		self.assertEqual(failed, [n * PULL_BYTES for n in fail_at])
		self.assertAligned()


class ReopenTests(PullTestCase):

	async def test_reopen_at_every_chunk(self):
		media_id = 100
		for chunks in (2, 3, 5):
			for short in (0, 1000):
				for fail_at in range(1, chunks):
					media_id += 1
					with self.subTest(chunks=chunks, short=short, fail_at=fail_at):
						data, job, outcome = await self.download_with_failures(
							media_id, chunks * PULL_BYTES - short, [fail_at])
						self.assertEqual(outcome.state, COMPLETE)
						self.assertEqual(self.stored(outcome), data)
						self.assertCovered(media_id, data, [fail_at])
						attempt = self.ledger.attempts(job.key)[-1]
						self.assertEqual((attempt['app_requests'], attempt['bytes_received']), (chunks + 1, len(data)))

	async def test_several_reopens_in_one_file(self):
		data, job, outcome = await self.download_with_failures(300, 5 * PULL_BYTES - 7, [1, 3, 4])
		self.assertEqual(self.stored(outcome), data)
		self.assertCovered(300, data, [1, 3, 4])

	async def test_the_first_pull_fails(self):
		data, job, outcome = await self.download_with_failures(301, 2 * PULL_BYTES, [0])
		self.assertEqual(self.stored(outcome), data)
		self.assertCovered(301, data, [0])

	async def test_unknown_size_reopen(self):
		# no size to stop at: an exact multiple ends with one empty pull past the end
		for media_id, size, past_end in ((310, 2 * PULL_BYTES, True), (311, 3 * PULL_BYTES - 5, False)):
			with self.subTest(size=size):
				data, job, outcome = await self.download_with_failures(media_id, size, [1], known=False)
				self.assertEqual(outcome.state, COMPLETE)
				self.assertEqual(self.stored(outcome), data)
				self.assertCovered(media_id, data, [1], past_end=past_end)

	async def test_a_known_size_makes_no_request_past_the_end(self):
		for media_id, size in ((320, 2 * PULL_BYTES), (321, 0)):
			with self.subTest(size=size):
				data, job, outcome = await self.download_with_failures(media_id, size, [])
				self.assertEqual(self.stored(outcome), data)
				self.assertCovered(media_id, data, [])


class DataCenterTests(PullTestCase):

	async def test_a_file_on_another_dc_uses_its_sender(self):
		data = content(PULL_BYTES + 10)
		job = self.fake.post(1, self.fake.add_document(400, data, dc=OTHER_DC))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), data)
		self.assertEqual({r.dc for r in self.fake.get_files()}, {OTHER_DC})
		self.assertEqual(self.fake.borrowed, [OTHER_DC])
		self.assertEqual(self.fake.returned, [OTHER_DC])

	async def test_file_migrate_is_followed_inside_one_pull(self):
		data = content(PULL_BYTES + 10)
		self.fake.add_document(401, data, dc=OTHER_DC)
		# the message says home DC; the server answers FILE_MIGRATE
		job = self.fake.post(1, self.fake.document_media(401, size=len(data), dc=HOME_DC))
		self.request(job)
		outcome = await self.downloader.download(job)
		self.assertEqual(self.stored(outcome), data)
		self.assertEqual([(r.dc, r.offset, r.outcome) for r in self.fake.get_files()], [
			(HOME_DC, 0, 'migrate'), (OTHER_DC, 0, 'ok'), (OTHER_DC, PULL_BYTES, 'ok')])
		# Telethon's re-request is not an application request
		self.assertEqual(self.ledger.attempts(job.key)[0]['app_requests'], 2)
		self.assertEqual(self.policy.stats_record()['media']['requests'], 2)
		self.assertEqual(self.fake.borrowed, self.fake.returned)

	async def test_every_reopened_iterator_returns_its_sender(self):
		data, job, outcome = await self.download_with_failures(402, 3 * PULL_BYTES, [1, 2])
		self.assertEqual(self.stored(outcome), data)
		self.assertEqual(self.fake.borrowed, [OTHER_DC] * 3)
		self.assertEqual(sorted(self.fake.returned), sorted(self.fake.borrowed))


class StreamTests(DownloadTestCase):

	def location(self, media_id, dc=OTHER_DC):
		return MediaLocation('document', media_id, 777, self.fake.files[('document', media_id)].reference, dc, '')

	async def test_pulls_and_the_end(self):
		data = content(PULL_BYTES + 3)
		self.fake.add_document(500, data, dc=OTHER_DC)
		stream = ChunkStream(self.fake.client, self.location(500))
		chunks = [await stream.pull(), await stream.pull(), await stream.pull()]
		await stream.close()
		self.assertEqual(chunks, [data[:PULL_BYTES], data[PULL_BYTES:], b''])
		self.assertEqual(stream.offset, len(data))
		self.assertEqual(self.fake.returned, [OTHER_DC])

	async def test_close_after_a_first_pull_that_never_started(self):
		self.fake.add_document(501, content(10), dc=OTHER_DC)

		async def refuse(dc_id):
			raise ConnectionError('cannot reach the data center')

		self.fake.client._borrow_exported_sender = refuse
		stream = ChunkStream(self.fake.client, self.location(501))
		with self.assertRaises(ConnectionError):
			await stream.pull()
		await stream.close()
		self.assertEqual(self.fake.returned, [])

	async def test_a_failed_pull_keeps_the_offset(self):
		data = content(2 * PULL_BYTES)
		self.fake.add_document(502, data, dc=HOME_DC)
		self.fake.fault(at_offset(PULL_BYTES), ConnectionError('reset'))
		stream = ChunkStream(self.fake.client, self.location(502, dc=HOME_DC))
		first = await stream.pull()
		with self.assertRaises(ConnectionError):
			await stream.pull()
		self.assertEqual(stream.offset, PULL_BYTES)
		second = await stream.pull()
		await stream.close()
		self.assertEqual(first + second, data)
		self.assertEqual([r.offset for r in self.fake.get_files()], [0, PULL_BYTES, PULL_BYTES])

	async def test_photo_location(self):
		data = content(1000)
		media = self.fake.add_photo(503, data)
		job = self.fake.job(1, media)
		self.assertEqual(job.location.input_location(), types.InputPhotoFileLocation(
			id=503, access_hash=777, file_reference=b'ref-1', thumb_size='y'))
		stream = ChunkStream(self.fake.client, job.location)
		self.assertEqual(await stream.pull(), data)
		await stream.close()


if __name__ == '__main__':
	unittest.main()
