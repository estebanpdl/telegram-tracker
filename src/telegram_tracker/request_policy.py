# -*- coding: utf-8 -*-
'''
One request policy per collection run: channel collection RPCs (entity
resolution, channel and enrichment full-channel requests, history pages)
are admitted through `RequestPolicy.call`. Login and the account check
precede this policy.

- At most `concurrency` RPCs are in flight (semaphore). No permit is held
  while a call sleeps for spacing, a flood cooldown, or a retry backoff.
- Admissions are at least `interval` seconds apart across the account.
- A flood wait from the server pauses every admission until
  max(current deadline, now + seconds). The call is retried after it when
  the wait is within `max_flood_wait` and retries remain; otherwise the
  account stops: every later admission raises AccountStopped.
- Transient failures (server errors, timeouts, dropped connections) are
  retried with exponential backoff (1, 2, 4 ... s, capped at 30). Retries
  are shared by flood and transient failures: `max_retries` additional
  attempts per logical RPC. Any other error is raised at once.
- Each attempt has a `timeout` (60 s), counted as a transient failure.
- Statistics per method class: requests (attempts sent), retries, flood
  waits, and flood-wait seconds.

The clock and sleep are injectable (defaults: the running loop's
monotonic time and asyncio.sleep). A call reserves the next admission
slot without awaiting between reading and advancing it, which on one
event loop is the reservation lock, so slots go out in call order. After
sleeping to its slot and acquiring a permit, the call re-checks the
cooldown and the spacing from the last dispatch just before sending, so
a slot reserved earlier cannot bypass a later flood wait; a failed
re-check reserves a new slot.
'''

# import modules
import asyncio
import math

from dataclasses import asdict, dataclass

from telethon import errors


DEFAULT_TIMEOUT = 60
BACKOFF_CAP = 30

TRANSIENT_ERRORS = (
	errors.ServerError,
	errors.TimedOutError,
	ConnectionError,
	TimeoutError
)


class AccountStopped(Exception):
	'''
	The server asked for a longer wait than allowed, or flood retries ran
	out. Ends the run; `seconds` is the server's last requested wait.
	'''
	def __init__(self, message, *, seconds, method_class):
		super().__init__(message)
		self.seconds = seconds
		self.method_class = method_class


class RequestFailed(Exception):
	'''
	A transient failure persisted after every allowed retry. Fails the
	channel that made the request.
	'''


class SessionRejected(Exception):
	'''
	Telegram refused the session's authorization (one of SESSION_ERRORS,
	which the policy raises at once). Ends the run.
	'''


# authorization/session errors that end the run
SESSION_ERRORS = (errors.UnauthorizedError, errors.AuthKeyError)


def session_rejected(exc):
	'''
	The SessionRejected that ends the run for one of SESSION_ERRORS.
	'''
	return SessionRejected(f'Telegram rejected the session ({type(exc).__name__}); log in again')


def flood_seconds(exc):
	'''
	The server's requested wait for a flood error, else None.
	'''
	if isinstance(exc, errors.FloodError) and hasattr(exc, 'seconds'):
		return int(exc.seconds)
	return None


@dataclass
class MethodStats:
	requests: int = 0
	retries: int = 0
	flood_waits: int = 0
	flood_wait_seconds: int = 0


class RequestPolicy:
	def __init__(
			self,
			*,
			concurrency=1,
			interval=0.25,
			max_flood_wait=60,
			max_retries=3,
			timeout=DEFAULT_TIMEOUT,
			clock=None,
			sleep=None
		):
		self.concurrency = concurrency
		self.interval = interval
		self.max_flood_wait = max_flood_wait
		self.max_retries = max_retries
		self.timeout = timeout
		self._clock = clock
		self._sleep = sleep or asyncio.sleep
		self._semaphore = asyncio.Semaphore(concurrency)
		self._next_slot = -math.inf
		self._last_dispatch = -math.inf
		self._cooldown_until = -math.inf
		self.stopped = None
		self.stats = {}
		self.active = 0
		self.peak_active = 0

	def now(self):
		if self._clock is None:
			return asyncio.get_running_loop().time()
		return self._clock()

	@property
	def cooldown_until(self):
		return self._cooldown_until

	def stats_record(self):
		'''
		Per-class statistics as plain dicts, classes in first-use order.
		'''
		return {name: asdict(stats) for name, stats in self.stats.items()}

	def stop(self, exc):
		if self.stopped is None:
			self.stopped = exc

	def _raise_if_stopped(self):
		if self.stopped is not None:
			raise self.stopped

	async def _admit(self):
		'''
		Reserve the next admission slot (first come, first served), sleep
		until it without a permit, then take a permit and re-check the
		cooldown and the spacing from the last dispatch. A failed re-check
		reserves a new slot. Returns holding the permit.
		'''
		while True:
			self._raise_if_stopped()
			now = self.now()
			slot = max(now, self._next_slot, self._cooldown_until)
			self._next_slot = slot + self.interval
			if slot > now:
				await self._sleep(slot - now)

			await self._semaphore.acquire()
			try:
				self._raise_if_stopped()
			except AccountStopped:
				self._semaphore.release()
				raise

			now = self.now()
			if now >= self._cooldown_until and now >= self._last_dispatch + self.interval:
				self._last_dispatch = now
				return
			self._semaphore.release()

	async def call(self, method_class, request):
		'''
		Run `request()` (a coroutine function performing one RPC) under the
		policy and return its result.
		'''
		stats = self.stats.setdefault(method_class, MethodStats())
		attempt = 0
		while True:
			await self._admit()
			stats.requests += 1
			self.active += 1
			self.peak_active = max(self.peak_active, self.active)
			try:
				return await asyncio.wait_for(request(), self.timeout)
			except Exception as exc:
				error = exc
			finally:
				self.active -= 1
				self._semaphore.release()

			seconds = flood_seconds(error)
			if seconds is not None:
				stats.flood_waits += 1
				stats.flood_wait_seconds += seconds
				if seconds > self.max_flood_wait or attempt >= self.max_retries:
					reason = (
						f'exceeds --max-flood-wait {self.max_flood_wait}'
						if seconds > self.max_flood_wait
						else f'after {attempt} retries (--max-retries {self.max_retries})'
					)
					self.stop(AccountStopped(
						f'Telegram asked to wait {seconds} s before more {method_class} '
						f'requests, {reason}; collection stopped for this account',
						seconds=seconds,
						method_class=method_class
					))
					raise self.stopped from None

				self._cooldown_until = max(self._cooldown_until, self.now() + seconds)
				attempt += 1
				stats.retries += 1
				continue

			if isinstance(error, TRANSIENT_ERRORS):
				if attempt >= self.max_retries:
					raise RequestFailed(
						f'{method_class} request failed after {attempt + 1} attempt(s): '
						f'{type(error).__name__}'
					) from error
				delay = min(BACKOFF_CAP, 2 ** attempt)
				attempt += 1
				stats.retries += 1
				await self._sleep(delay)
				continue

			raise error
