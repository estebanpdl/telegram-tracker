# -*- coding: utf-8 -*-
'''
Media byte limits: size parsing, the per-file rules, and the per-run
reservation counter for known and unknown sizes (reserve, admit, settle,
release), including races between threads and between asyncio tasks.
'''

# import modules
import asyncio
import random
import sys
import threading
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telegram_tracker.media.limits import OVER_FILE_LIMIT, PULL_BYTES, MediaBudget, MediaLimits, parse_size, \
	whole_pulls


PULL = PULL_BYTES


class SizeTests(offline_guard.OfflineTestCase):

	def test_sizes(self):
		cases = {'1': 1, '500000': 500000, '3K': 3072, '20M': 20 * 1024 ** 2, '2g': 2 * 1024 ** 3, ' 7 k ': 7168}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(parse_size(text), expected)

	def test_bad_sizes(self):
		for text in ('0', '0M', '-1', '1.5M', '1T', '1KB', '', 'M', '1 2', None, 10):
			with self.subTest(text=text):
				self.assertRaises(ValueError, parse_size, text)

	def test_whole_pulls(self):
		self.assertEqual(
			[whole_pulls(n) for n in (0, 1, PULL - 1, PULL, PULL + 1, 3 * PULL)],
			[PULL, PULL, PULL, PULL, 2 * PULL, 3 * PULL]
		)


class FileLimitTests(offline_guard.OfflineTestCase):

	def test_known_sizes(self):
		limits = MediaLimits(max_file=1000)
		self.assertEqual([limits.skip_known(n) for n in (None, 0, 1000, 1001)], [None, None, None, OVER_FILE_LIMIT])
		self.assertIsNone(MediaLimits().skip_known(10 ** 12))

	def test_unknown_size_streams(self):
		limits = MediaLimits(max_file=1000)
		self.assertEqual([limits.stream_over(n) for n in (0, 1000, 1001)], [False, False, True])
		self.assertFalse(MediaLimits().stream_over(10 ** 12))


class KnownSizeTests(offline_guard.OfflineTestCase):

	def test_reservation_is_rounded_up_to_whole_pulls(self):
		budget = MediaBudget(total=3 * PULL)
		first = budget.open_file(PULL + 1)
		self.assertEqual((first.held, budget.used), (2 * PULL, 2 * PULL))
		self.assertIsNone(budget.open_file(PULL + 1))
		self.assertEqual(budget.used, 2 * PULL)
		second = budget.open_file(0)
		self.assertEqual((second.held, budget.used), (PULL, 3 * PULL))
		self.assertIsNone(budget.open_file(1))

	def test_a_file_streams_within_its_reservation(self):
		budget = MediaBudget(total=10 * PULL)
		expected = PULL + 300
		file = budget.open_file(expected)
		self.assertTrue(file.admit_pull())
		file.received(PULL)
		self.assertTrue(file.admit_pull())
		file.received(300)
		self.assertEqual((budget.received, budget.reserved), (expected, 0))
		self.assertFalse(file.admit_pull())
		file.close()
		self.assertEqual((budget.received, budget.reserved, file.received_bytes), (expected, 0, expected))

	def test_a_longer_stream_stops_at_its_reservation(self):
		budget = MediaBudget(total=10 * PULL)
		file = budget.open_file(PULL)
		self.assertTrue(file.admit_pull())
		file.received(PULL)
		self.assertFalse(file.admit_pull())
		self.assertEqual(budget.used, PULL)

	def test_a_failed_pull_keeps_its_reservation(self):
		budget = MediaBudget(total=PULL)
		file = budget.open_file(PULL)
		self.assertTrue(file.admit_pull())
		file.pull_failed()
		self.assertEqual(budget.used, PULL)
		self.assertTrue(file.admit_pull())
		file.received(PULL)
		self.assertEqual((budget.received, budget.reserved), (PULL, 0))

	def test_close_releases_what_was_not_received(self):
		budget = MediaBudget(total=4 * PULL)
		file = budget.open_file(4 * PULL)
		file.admit_pull()
		file.received(PULL)
		file.close()
		file.close()
		self.assertEqual((budget.received, budget.reserved), (PULL, 0))
		# a restart counts again: received bytes stay counted
		again = budget.open_file(4 * PULL)
		self.assertIsNone(again)
		self.assertIsNotNone(budget.open_file(3 * PULL))


class UnknownSizeTests(offline_guard.OfflineTestCase):

	def test_each_pull_reserves_and_releases(self):
		budget = MediaBudget(total=2 * PULL)
		file = budget.open_file(None)
		self.assertEqual((file.held, budget.used), (0, 0))
		self.assertTrue(file.admit_pull())
		self.assertEqual(budget.reserved, PULL)
		file.received(100)
		self.assertEqual((budget.received, budget.reserved), (100, 0))
		self.assertTrue(file.admit_pull())
		file.received(PULL)
		self.assertFalse(file.admit_pull())
		self.assertEqual((budget.received, budget.reserved, file.received_bytes), (PULL + 100, 0, PULL + 100))

	def test_a_failed_pull_gives_its_reservation_back(self):
		budget = MediaBudget(total=PULL)
		file = budget.open_file(None)
		self.assertTrue(file.admit_pull())
		file.pull_failed()
		self.assertEqual(budget.used, 0)
		other = budget.open_file(PULL)
		self.assertIsNotNone(other)
		self.assertFalse(file.admit_pull())

	def test_close_releases_an_open_pull(self):
		budget = MediaBudget(total=PULL)
		file = budget.open_file(None)
		file.admit_pull()
		file.close()
		self.assertEqual(budget.used, 0)

	def test_no_limit_counts_without_refusing(self):
		budget = MediaBudget()
		big = budget.open_file(10 ** 12)
		self.assertIsNotNone(big)
		file = budget.open_file(None)
		for _ in range(3):
			self.assertTrue(file.admit_pull())
			file.received(PULL)
		self.assertEqual(budget.received, 3 * PULL)


class MisuseTests(offline_guard.OfflineTestCase):

	def test_misuse_is_refused(self):
		budget = MediaBudget(total=10 * PULL)
		for expected in (PULL, None):
			with self.subTest(expected=expected):
				file = budget.open_file(expected)
				self.assertRaises(RuntimeError, file.received, 1)
				self.assertRaises(RuntimeError, file.pull_failed)
				file.admit_pull()
				self.assertRaises(RuntimeError, file.admit_pull)
				for bad in (-1, PULL + 1, 1.0, True):
					self.assertRaises(ValueError, file.received, bad)
				file.received(1)
				file.close()
				self.assertRaises(RuntimeError, file.admit_pull)
		self.assertEqual(budget.reserved, 0)


class InvariantTests(offline_guard.OfflineTestCase):

	def test_random_operations_never_exceed_the_limit(self):
		rng = random.Random(7)
		for total in (PULL, 3 * PULL + 5, 40 * PULL):
			with self.subTest(total=total):
				budget = MediaBudget(total=total)
				files = []
				for _ in range(3000):
					action = rng.random()
					if action < 0.2:
						expected = rng.choice([None, rng.randrange(0, 6 * PULL)])
						file = budget.open_file(expected)
						if file is not None:
							files.append(file)
					elif files:
						file = rng.choice(files)
						if action < 0.85 and file.admit_pull():
							if rng.random() < 0.2:
								file.pull_failed()
							else:
								file.received(rng.randrange(0, PULL + 1))
						elif action >= 0.85:
							file.close()
							files.remove(file)
					self.assertLessEqual(budget.used, total)
					self.assertEqual(budget.reserved, sum(f.held for f in files))
				for file in files:
					file.close()
				self.assertEqual(budget.reserved, 0)
				self.assertLessEqual(budget.received, total)


class RaceTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		interval = sys.getswitchinterval()
		sys.setswitchinterval(1e-6)
		self.addCleanup(sys.setswitchinterval, interval)

	def run_threads(self, count, work):
		barrier = threading.Barrier(count)
		results = []

		def run():
			barrier.wait()
			results.append(work())

		threads = [threading.Thread(target=run) for _ in range(count)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
		return results

	def test_threads_racing_for_known_sizes(self):
		for _ in range(20):
			budget = MediaBudget(total=10 * PULL)
			granted = [r for r in self.run_threads(32, lambda: budget.open_file(PULL)) if r is not None]
			self.assertEqual(len(granted), 10)
			self.assertEqual(budget.used, 10 * PULL)

	def test_threads_racing_for_pulls(self):
		for _ in range(20):
			budget = MediaBudget(total=10 * PULL)
			files = [budget.open_file(None) for _ in range(32)]
			slots = iter(files)
			lock = threading.Lock()

			def admit():
				with lock:
					file = next(slots)
				return file.admit_pull()

			self.assertEqual(sum(self.run_threads(32, admit)), 10)
			self.assertEqual(budget.reserved, 10 * PULL)

	def test_tasks_racing_on_one_loop(self):
		budget = MediaBudget(total=7 * PULL + 100)
		rng = random.Random(3)
		peaks = []

		async def download(expected, length):
			file = budget.open_file(expected)
			if file is None:
				return 'skipped'
			try:
				while file.received_bytes < length:
					await asyncio.sleep(0)
					if not file.admit_pull():
						return 'stopped'
					peaks.append(budget.used)
					await asyncio.sleep(0)
					file.received(min(PULL, length - file.received_bytes))
					peaks.append(budget.used)
				return 'done'
			finally:
				file.close()

		async def main():
			jobs = []
			for _ in range(40):
				length = rng.randrange(1, 3 * PULL)
				expected = rng.choice([None, length])
				jobs.append(download(expected, length))
			return await asyncio.gather(*jobs)

		outcomes = asyncio.run(main())
		self.assertLessEqual(max(peaks), budget.total)
		self.assertLessEqual(budget.received, budget.total)
		self.assertEqual(budget.reserved, 0)
		# the limit was reached: some downloads finished, others did not fit
		self.assertIn('done', outcomes)
		self.assertTrue('skipped' in outcomes or 'stopped' in outcomes)


if __name__ == '__main__':
	unittest.main()
