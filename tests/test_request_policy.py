# -*- coding: utf-8 -*-
'''
request_policy.RequestPolicy on the virtual-time loop: spacing, the
in-flight bound, the shared flood cooldown, account stops, retries,
backoff, timeouts, permits during sleeps, and per-class statistics.
Times are virtual seconds from the start of each test.
'''

# import modules
import asyncio
import unittest

import offline_guard

from telethon import errors

from request_policy import AccountStopped, RequestFailed, RequestPolicy


def flood(seconds):
	return errors.FloodWaitError(request=None, capture=seconds)


def server_error():
	return errors.ServerError(None, 'INTERNAL')


class Rpc:
	'''
	A fake RPC: records dispatch times; each call takes `latency` and then
	raises or returns the next scripted outcome ('ok' when none is left).
	'''
	def __init__(self, *outcomes, latency=0):
		self.outcomes = list(outcomes)
		self.latency = latency
		self.times = []

	async def __call__(self):
		self.times.append(asyncio.get_running_loop().time())
		if self.latency:
			await asyncio.sleep(self.latency)
		if self.outcomes:
			outcome = self.outcomes.pop(0)
			if isinstance(outcome, BaseException):
				raise outcome
			return outcome
		return 'ok'


class PolicyTestCase(offline_guard.OfflineAsyncTestCase):

	def policy(self, **kwargs):
		kwargs.setdefault('concurrency', 3)
		kwargs.setdefault('interval', 0.25)
		return RequestPolicy(**kwargs)

	def start(self, policy, rpc, method_class='history', delay=0):
		async def run():
			if delay:
				await asyncio.sleep(delay)
			return await policy.call(method_class, rpc)

		return asyncio.ensure_future(run())

	def now(self):
		return asyncio.get_running_loop().time()


class SpacingTests(PolicyTestCase):

	async def test_admissions_are_spaced_across_callers(self):
		policy = self.policy()
		rpcs = [Rpc() for _ in range(5)]
		await asyncio.gather(*(self.start(policy, r) for r in rpcs))
		self.assertEqual([r.times[0] for r in rpcs], [0, 0.25, 0.5, 0.75, 1.0])

	async def test_in_flight_requests_never_exceed_the_bound(self):
		policy = self.policy(concurrency=2, interval=0.125)
		rpcs = [Rpc(latency=1) for _ in range(6)]
		await asyncio.gather(*(self.start(policy, r) for r in rpcs))
		self.assertEqual(policy.peak_active, 2)
		starts = sorted(r.times[0] for r in rpcs)
		for t in starts:
			overlapping = sum(1 for s in starts if s <= t < s + 1)
			self.assertLessEqual(overlapping, 2)
		self.assertEqual(starts, [0, 0.125, 1.0, 1.125, 2.0, 2.125])


class FloodTests(PolicyTestCase):

	async def test_one_flood_pauses_every_admission(self):
		policy = self.policy()
		flooded = Rpc(flood(10), latency=0.875)
		others = [Rpc() for _ in range(4)]
		tasks = [self.start(policy, flooded)] + [self.start(policy, r) for r in others]
		results = await asyncio.gather(*tasks)
		self.assertEqual(results, ['ok'] * 5)
		# slots 0, .25, .5, .75, 1.0; the flood answered at .875 holds the
		# 1.0 slot until the cooldown ends at 10.875, after the retry
		self.assertEqual([r.times[0] for r in others], [0.25, 0.5, 0.75, 11.125])
		self.assertEqual(flooded.times, [0, 10.875])
		self.assertEqual(policy.cooldown_until, 10.875)

	def dispatches(self, *rpcs):
		return sorted(t for r in rpcs for t in r.times)

	async def test_later_flood_extends_the_deadline(self):
		policy = self.policy()
		first = Rpc(flood(10), latency=0.375)
		second = Rpc(flood(30), latency=0.375)
		waiting = Rpc()
		await asyncio.gather(
			self.start(policy, first), self.start(policy, second),
			self.start(policy, waiting, delay=0.75)
		)
		# floods answered at .375 (10 s) and .625 (30 s): deadline 30.625
		self.assertEqual(policy.cooldown_until, 30.625)
		self.assertEqual(self.dispatches(first, second, waiting), [0, 0.25, 30.625, 30.875, 31.125])

	async def test_shorter_later_flood_keeps_the_deadline(self):
		policy = self.policy()
		first = Rpc(flood(30), latency=0.375)
		second = Rpc(flood(5), latency=0.375)
		await asyncio.gather(self.start(policy, first), self.start(policy, second))
		self.assertEqual(policy.cooldown_until, 30.375)
		self.assertEqual(self.dispatches(first, second), [0, 0.25, 30.375, 30.625])

	async def test_reserved_slot_rechecks_the_cooldown(self):
		policy = self.policy(concurrency=2, interval=1.0)
		flooded = Rpc(flood(10), latency=0.5)
		queued = Rpc()
		await asyncio.gather(self.start(policy, flooded), self.start(policy, queued))
		# queued held the 1.0 slot; the flood answered at 0.5 sent it after
		# the cooldown (10.5) and the retry's slot
		self.assertEqual(flooded.times, [0, 10.5])
		self.assertEqual(queued.times, [11.5])

	async def test_too_long_wait_stops_the_account(self):
		policy = self.policy(max_flood_wait=60)
		flooded = Rpc(flood(61), latency=0.5)
		waiting = [Rpc() for _ in range(3)]
		tasks = [self.start(policy, flooded)] + [self.start(policy, r, delay=0.6) for r in waiting]
		results = await asyncio.gather(*tasks, return_exceptions=True)
		self.assertTrue(all(isinstance(r, AccountStopped) for r in results))
		self.assertEqual(results[0].seconds, 61)
		self.assertIn('exceeds --max-flood-wait 60', str(results[0]))
		self.assertEqual([r.times for r in waiting], [[], [], []])
		self.assertEqual(flooded.times, [0])
		with self.assertRaises(AccountStopped):
			await policy.call('resolve', Rpc())

	async def test_zero_cap_stops_on_any_flood(self):
		policy = self.policy(max_flood_wait=0)
		with self.assertRaises(AccountStopped):
			await policy.call('history', Rpc(flood(1)))

	async def test_flood_retries_are_limited(self):
		policy = self.policy(max_retries=2)
		rpc = Rpc(flood(1), flood(1), flood(1))
		with self.assertRaisesRegex(AccountStopped, 'after 2 retries'):
			await policy.call('history', rpc)
		self.assertEqual(rpc.times, [0, 1, 2])
		self.assertEqual(policy.stats['history'].retries, 2)


class RetryTests(PolicyTestCase):

	async def test_transient_errors_back_off_1_2_4(self):
		policy = self.policy()
		rpc = Rpc(server_error(), errors.TimedOutError(None, 'Timeout'), ConnectionError('reset'))
		self.assertEqual(await policy.call('history', rpc), 'ok')
		self.assertEqual(rpc.times, [0, 1, 3, 7])
		stats = policy.stats['history']
		self.assertEqual((stats.requests, stats.retries, stats.flood_waits), (4, 3, 0))

	async def test_transient_errors_stop_after_the_retries(self):
		policy = self.policy(max_retries=3)
		rpc = Rpc(*[server_error() for _ in range(5)])
		with self.assertRaisesRegex(RequestFailed, 'after 4 attempt'):
			await policy.call('enrichment', rpc)
		self.assertEqual(len(rpc.times), 4)
		self.assertIsNone(policy.stopped)

	async def test_backoff_is_capped_at_30_seconds(self):
		policy = self.policy(max_retries=7)
		rpc = Rpc(*[server_error() for _ in range(7)])
		await policy.call('history', rpc)
		gaps = [b - a for a, b in zip(rpc.times, rpc.times[1:])]
		self.assertEqual(gaps, [1, 2, 4, 8, 16, 30, 30])

	async def test_permanent_errors_are_not_retried(self):
		for error in (errors.ChannelPrivateError(request=None), errors.BadRequestError(None, 'BAD'),
				errors.AuthKeyUnregisteredError(request=None), ValueError('no such user')):
			with self.subTest(error=type(error).__name__):
				policy = self.policy()
				rpc = Rpc(error, 'unreachable')
				with self.assertRaises(type(error)):
					await policy.call('resolve', rpc)
				self.assertEqual(len(rpc.times), 1)
				self.assertEqual(policy.stats['resolve'].retries, 0)

	async def test_attempt_timeout_is_a_transient_failure(self):
		policy = self.policy()
		rpc = Rpc(latency=100)
		with self.assertRaises(RequestFailed):
			await RequestPolicy(max_retries=0).call('history', rpc)
		self.assertEqual(rpc.times, [0])

		started = self.now()
		calls = []

		async def slow_then_fast():
			calls.append(self.now() - started)
			if len(calls) == 1:
				await asyncio.sleep(100)
			return 'ok'

		self.assertEqual(await policy.call('history', slow_then_fast), 'ok')
		self.assertEqual(calls, [0, 61])

	async def test_no_permit_is_held_while_sleeping(self):
		policy = self.policy(concurrency=1)
		backing_off = Rpc(server_error())
		other = Rpc()
		await asyncio.gather(self.start(policy, backing_off), self.start(policy, other))
		# the single permit is free during the 1 s backoff
		self.assertEqual(other.times, [0.25])
		self.assertEqual(backing_off.times, [0, 1])

	async def test_cancelled_call_releases_its_permit(self):
		policy = self.policy(concurrency=1)
		stuck = self.start(policy, Rpc(latency=50))
		await asyncio.sleep(1)
		stuck.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await stuck
		self.assertEqual(policy.active, 0)
		self.assertEqual(await policy.call('history', Rpc()), 'ok')


class StatsTests(PolicyTestCase):

	async def test_statistics_follow_the_event_sequence(self):
		policy = self.policy()
		await asyncio.gather(
			self.start(policy, Rpc(flood(3)), 'resolve'),
			self.start(policy, Rpc(server_error(), server_error()), 'history'),
			self.start(policy, Rpc(), 'enrichment'),
			self.start(policy, Rpc(flood(2), server_error()), 'enrichment'),
		)
		self.assertEqual(policy.stats_record(), {
			'resolve': {'requests': 2, 'retries': 1, 'flood_waits': 1, 'flood_wait_seconds': 3},
			'history': {'requests': 3, 'retries': 2, 'flood_waits': 0, 'flood_wait_seconds': 0},
			'enrichment': {'requests': 4, 'retries': 2, 'flood_waits': 1, 'flood_wait_seconds': 2},
		})


if __name__ == '__main__':
	unittest.main()
