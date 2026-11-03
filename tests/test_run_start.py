# -*- coding: utf-8 -*-
'''
The run start guard: a collection into a files or both folder starts in a
later second than the folder's last run, because the files name runs by
their started_at, to the second. A sqlite folder never waits. Through
cli.main with the fake client and a fake wall clock (synthetic data).
'''

# import modules
import os
import unittest

import storage_modes_support

from storage_modes_support import STATUS_FILE, SameSecondClock, read_json

from telegram_tracker.storage import status as status_module
from telegram_tracker.storage.provenance import provenance_path


FIRST, NEXT = SameSecondClock.FIRST, SameSecondClock.NEXT
LIMIT_STEPS = round(status_module.START_WAIT_LIMIT / status_module.START_WAIT_STEP)


class RunStartTests(storage_modes_support.ModesCase):

	def two_runs(self, clock, storage=None):
		'''
		Two collections of alpha_fx into `self.out` on `clock`; returns the
		two started_at values.
		'''
		started = []
		with clock.patch():
			for _ in range(2):
				code, _, err = self.collect(storage=storage)
				self.assertEqual(code, 0, err)
				started.append(self.status()['started_at'])
		return started

	def test_a_files_run_in_the_same_second_starts_in_the_next_one(self):
		clock = SameSecondClock(steps=3)
		self.assertEqual(self.two_runs(clock), [FIRST, NEXT])
		self.assertEqual(clock.sleeps, [status_module.START_WAIT_STEP] * 3)
		# the history names the second run
		provenance = read_json(provenance_path(self.out, 'alpha_fx'))
		self.assertEqual(provenance['run_started_at'], NEXT)

	def test_a_both_run_in_the_same_second_starts_in_the_next_one(self):
		clock = SameSecondClock(steps=2)
		self.assertEqual(self.two_runs(clock, storage='both'), [FIRST, NEXT])
		self.assertEqual(len(clock.sleeps), 2)
		self.assertEqual(self.rows('SELECT started_at FROM collection_runs ORDER BY run_id'), [(FIRST,), (NEXT,)])
		self.assertEqual(read_json(provenance_path(self.out, 'alpha_fx'))['run_started_at'], NEXT)

	def test_a_sqlite_run_does_not_wait(self):
		clock = SameSecondClock(steps=1)
		self.assertEqual(self.two_runs(clock, storage='sqlite'), [FIRST, FIRST])
		self.assertEqual(clock.sleeps, [])
		# runs are told apart by run_uuid
		self.assertEqual(self.rows('SELECT count(DISTINCT run_uuid) FROM collection_runs'), [(2,)])

	def test_the_wait_is_bounded(self):
		clock = SameSecondClock(steps=None)
		self.assertEqual(self.two_runs(clock), [FIRST, FIRST])
		self.assertEqual(len(clock.sleeps), LIMIT_STEPS)
		self.assertAlmostEqual(sum(clock.sleeps), status_module.START_WAIT_LIMIT)

	def test_a_run_in_a_later_second_does_not_wait(self):
		clock = SameSecondClock(steps=None)
		with clock.patch():
			self.assertEqual(self.collect()[0], 0)
			clock.now = NEXT
			self.assertEqual(self.collect()[0], 0)
		self.assertEqual(self.status()['started_at'], NEXT)
		self.assertEqual(clock.sleeps, [])

	def test_an_unreadable_sidecar_does_not_wait(self):
		os.makedirs(self.out)
		with open(os.path.join(self.out, STATUS_FILE), 'w', encoding='utf-8') as fh:
			fh.write('{not json')
		clock = SameSecondClock(steps=None)
		with clock.patch():
			code, _, err = self.collect()
		self.assertEqual(code, 0, err)
		self.assertEqual(self.status()['started_at'], FIRST)
		self.assertEqual(clock.sleeps, [])


if __name__ == '__main__':
	unittest.main()
