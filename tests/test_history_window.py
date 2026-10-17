# -*- coding: utf-8 -*-
'''
History windows (collector.HistoryWindow) against the fake client.

Fixture channel alpha_fx (101) holds ids 10..4, newest first, one per day
from 2026-01-10 down to 2026-01-04; the fake serves 2 messages per page.
'''

# import modules
import contextlib
import io
import json
import os
import tempfile
import unittest

from datetime import datetime, timezone

import offline_guard

from api.auth import ensure_authorized
from fakes import FakeTelegramClient, load_expected

import collector

from collector import HistoryWindow


EXPECTED = load_expected('collection.json')
ALPHA = 101


def utc(day, hour=0):
	return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


class WindowTestCase(offline_guard.OfflineAsyncTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = os.path.join(tmp.name, 'data')

	async def collect(self, window=None, *, honor_bounds=True, **kwargs):
		self.client = FakeTelegramClient(
			page_size=EXPECTED['page_size'], honor_bounds=honor_bounds
		)

		with contextlib.redirect_stdout(io.StringIO()):
			return await collector.run_collection(
				['alpha_fx'],
				self.out,
				client_factory=lambda: self.client,
				authorize=lambda c: ensure_authorized(c, None),
				window=window,
				**kwargs
			)

	def history_requests(self):
		return [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']

	def saved(self):
		path = os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json')
		with open(path, encoding='utf-8') as fh:
			return json.load(fh)

	def saved_ids(self):
		return [m['id'] for m in self.saved()['messages']]


class WindowValueTests(offline_guard.OfflineTestCase):

	def test_default_window_keeps_everything(self):
		window = HistoryWindow()
		self.assertFalse(window.is_filtered)
		self.assertEqual(window.describe(), '')
		self.assertEqual(window.page_limit(0), 100)
		self.assertTrue(window.keeps({'id': 1, 'date': utc(1)}))

	def test_bounds_are_exclusive_except_since(self):
		window = HistoryWindow(min_id=4, max_id=8, since=utc(5), until=utc(7))
		kept = [i for i in range(1, 11) if window.keeps({'id': i, 'date': utc(i)})]
		self.assertEqual(kept, [5, 6])

	def test_naive_and_string_dates_are_utc(self):
		window = HistoryWindow(since=utc(5))
		self.assertTrue(window.keeps({'id': 1, 'date': '2026-01-05 00:00:00'}))
		self.assertFalse(window.keeps({'id': 1, 'date': datetime(2026, 1, 4, 23, 59)}))

	def test_page_limit_shrinks_to_the_remaining_cap(self):
		window = HistoryWindow(max_messages=150)
		self.assertEqual((window.page_limit(0), window.page_limit(120)), (100, 30))

	def test_describe(self):
		window = HistoryWindow(min_id=3, max_id=9, since=utc(2), until=utc(8, 12), max_messages=5)
		self.assertEqual(
			window.describe(),
			'id > 3, id < 9, date >= 2026-01-02 00:00:00 UTC, '
			'date < 2026-01-08 12:00:00 UTC, newest 5 messages'
		)


class FetchWindowTests(WindowTestCase):

	async def test_unfiltered_requests_are_unchanged(self):
		await self.collect(HistoryWindow())
		for request in self.history_requests():
			self.assertEqual(
				(request['limit'], request['max_id'], request['min_id'], request['offset_date']),
				(100, 0, 0, 0)
			)
		self.assertEqual(self.saved_ids(), [10, 9, 8, 7, 6, 5, 4])

	async def test_until_is_sent_on_the_first_page_only(self):
		await self.collect(HistoryWindow(until=utc(8)))
		requests = self.history_requests()
		self.assertEqual(requests[0]['offset_date'], utc(8))
		self.assertEqual({r['offset_date'] for r in requests[1:]}, {0})
		# 8 is dated 2026-01-08 08:30, after the bound
		self.assertEqual(self.saved_ids(), [7, 6, 5, 4])

	async def test_max_id_is_sent_on_every_page(self):
		await self.collect(HistoryWindow(max_id=8))
		self.assertEqual({r['max_id'] for r in self.history_requests()}, {8})
		self.assertEqual(self.saved_ids(), [7, 6, 5, 4])

	async def test_cap_stops_requesting_once_reached(self):
		await self.collect(HistoryWindow(max_messages=2))
		requests = self.history_requests()
		self.assertEqual([r['limit'] for r in requests], [2])
		self.assertEqual(self.saved_ids(), [10, 9])

	async def test_cap_shrinks_the_page_limit(self):
		await self.collect(HistoryWindow(max_messages=3))
		self.assertEqual([r['limit'] for r in self.history_requests()], [3, 1])
		self.assertEqual(self.saved_ids(), [10, 9, 8])

	async def test_since_stops_at_the_first_older_page(self):
		await self.collect(HistoryWindow(since=utc(7)))
		# pages [10, 9], [8, 7], [6, 5]: the third reaches before the bound
		self.assertEqual([r['offset_id'] for r in self.history_requests()], [0, 9, 7])
		self.assertEqual(self.saved_ids(), [10, 9, 8, 7])

	async def test_window_bounds_combine(self):
		await self.collect(HistoryWindow(min_id=4, max_id=10, since=utc(6), max_messages=3))
		self.assertEqual(self.saved_ids(), [9, 8, 7])
		self.assertEqual(len(self.history_requests()), 2)

	async def test_empty_selection_saves_an_empty_history(self):
		await self.collect(HistoryWindow(since=datetime(2027, 1, 1, tzinfo=timezone.utc)))
		self.assertEqual(self.saved_ids(), [])
		self.assertEqual(len(self.history_requests()), 1)

	async def test_count_is_the_server_value(self):
		await self.collect(HistoryWindow(max_messages=2))
		self.assertEqual(self.saved()['count'], 7)

	async def test_legacy_min_id_keyword_still_applies(self):
		await self.collect(min_id=6)
		self.assertEqual({r['min_id'] for r in self.history_requests()}, {6})
		self.assertEqual(self.saved_ids(), [10, 9, 8, 7])


class ServerIgnoresBoundsTests(WindowTestCase):
	'''
	The client trims every bound itself, so a server that ignores min_id,
	max_id, or offset_date cannot widen the selection.
	'''

	async def test_id_bounds_are_trimmed_client_side(self):
		await self.collect(HistoryWindow(min_id=5, max_id=9), honor_bounds=False)
		self.assertEqual(self.saved_ids(), [8, 7, 6])
		# the page holding id 5 reaches the lower bound: no further request
		self.assertEqual([r['offset_id'] for r in self.history_requests()], [0, 9, 7])

	async def test_until_is_trimmed_client_side(self):
		await self.collect(HistoryWindow(until=utc(8)), honor_bounds=False)
		self.assertEqual(self.saved_ids(), [7, 6, 5, 4])


if __name__ == '__main__':
	unittest.main()
