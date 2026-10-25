# -*- coding: utf-8 -*-
'''
Byte limits a user sets for media downloads: a per-file limit
(--max-media-size) and a per-run limit (--max-media-total). Neither has
a default.

A size is a whole number of bytes, optionally followed by K, M or G
(KiB, MiB, GiB).

MediaBudget is the run-wide counter for the per-run limit. It counts
every byte received, restarts included, plus outstanding reservations,
and grants a reservation only when that sum stays within the limit, so
the bytes received never exceed it. Downloads reserve before they
request:

	known size    open_file(expected) reserves the expected size rounded
	              up to whole pulls (PULL_BYTES, at least one) before the
	              first request; None means the file does not fit and is
	              skipped. Each pull is admitted against that reservation,
	              so a stream longer than reserved stops before it can
	              take bytes it has not reserved.
	unknown size  each pull reserves PULL_BYTES before it is requested,
	              and the part it did not use is released when it ends.

Closing a FileReservation releases whatever it still holds. Up to one
pull's worth of the limit can stay unused. Every operation takes a lock
and never waits on anything else, so the counter is safe to share
between asyncio tasks on one loop and between threads.
'''

# import modules
import re
import threading

from dataclasses import dataclass


PULL_BYTES = 512 * 1024
UNITS = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3}
SIZE = re.compile(r'^\s*([0-9]+)\s*([KMG]?)\s*$', re.IGNORECASE)

OVER_FILE_LIMIT = 'over_file_limit'
OVER_RUN_LIMIT = 'over_run_limit'


def parse_size(text):
	'''
	Bytes in a size such as `500000`, `20M` or `2G`. Raises ValueError
	for anything else, and for zero.
	'''
	match = SIZE.match(text) if isinstance(text, str) else None
	if match is None:
		raise ValueError(f'{text!r} is not a size (bytes, or a number with K, M or G)')
	value = int(match.group(1)) * UNITS[match.group(2).upper()]
	if value <= 0:
		raise ValueError(f'{text!r} must be larger than zero')
	return value


def whole_pulls(nbytes):
	'''
	`nbytes` rounded up to whole pulls, at least one.
	'''
	return max(1, -(-nbytes // PULL_BYTES)) * PULL_BYTES


@dataclass(frozen=True)
class MediaLimits:
	'''
	max_file -> per-file limit in bytes, None for none
	max_total -> per-run limit in bytes, None for none
	'''
	max_file: int | None = None
	max_total: int | None = None

	def skip_known(self, expected):
		'''
		OVER_FILE_LIMIT when a file's known size is above the per-file
		limit (it is skipped with no request), else None.
		'''
		if expected is not None and self.max_file is not None and expected > self.max_file:
			return OVER_FILE_LIMIT
		return None

	def stream_over(self, received):
		'''
		True once a stream of unknown size has received more than the
		per-file limit.
		'''
		return self.max_file is not None and received > self.max_file


class MediaBudget:
	'''
	The per-run counter; see the module docstring. `total` None counts
	without limiting.
	'''
	def __init__(self, total=None):
		self.total = total
		self.received = 0
		self.reserved = 0
		self._lock = threading.Lock()

	@property
	def used(self):
		return self.received + self.reserved

	def _reserve(self, nbytes):
		with self._lock:
			if self.total is not None and self.received + self.reserved + nbytes > self.total:
				return False
			self.reserved += nbytes
			return True

	def _settle(self, held, received):
		'''
		Turn `held` reserved bytes into `received` received ones (the
		rest is released).
		'''
		with self._lock:
			if not 0 <= received <= held <= self.reserved:
				raise ValueError(f'cannot settle {received} of {held} held bytes ({self.reserved} reserved)')
			self.reserved -= held
			self.received += received

	def open_file(self, expected):
		'''
		A FileReservation for a file of `expected` bytes (None: unknown),
		or None when a known size does not fit in what is left.
		'''
		if expected is None:
			return FileReservation(self, None, 0)
		held = whole_pulls(expected)
		if not self._reserve(held):
			return None
		return FileReservation(self, expected, held)


class FileReservation:
	'''
	One download's share of a MediaBudget. Call admit_pull() before each
	pull's request (False: stop, nothing was requested), then received(n)
	with the bytes it brought (0..PULL_BYTES) or pull_failed() when it
	brought none. close() releases the rest.
	'''
	def __init__(self, budget, expected, held):
		self.budget = budget
		self.expected = expected
		self.held = held
		self.received_bytes = 0
		self._pull_open = False
		self.closed = False

	@property
	def known(self):
		return self.expected is not None

	def admit_pull(self):
		if self.closed or self._pull_open:
			raise RuntimeError('a pull is already admitted, or the reservation is closed')
		if self.known:
			admitted = self.held >= PULL_BYTES
		else:
			admitted = self.budget._reserve(PULL_BYTES)
			if admitted:
				self.held += PULL_BYTES
		self._pull_open = admitted
		return admitted

	def received(self, nbytes):
		if not self._pull_open:
			raise RuntimeError('no pull was admitted')
		if type(nbytes) is not int or not 0 <= nbytes <= PULL_BYTES:
			raise ValueError(f'a pull brings 0 to {PULL_BYTES} bytes, not {nbytes!r}')
		self._pull_open = False
		self.budget._settle(PULL_BYTES, nbytes)
		self.held -= PULL_BYTES
		self.received_bytes += nbytes

	def pull_failed(self):
		'''
		The admitted pull brought nothing (its request failed). An unknown
		size gives its pull back; a known size keeps it for the retry.
		'''
		if not self._pull_open:
			raise RuntimeError('no pull was admitted')
		self._pull_open = False
		if not self.known:
			self.budget._settle(PULL_BYTES, 0)
			self.held -= PULL_BYTES

	def close(self):
		if self.closed:
			return
		self.closed = True
		self._pull_open = False
		if self.held:
			self.budget._settle(self.held, 0)
			self.held = 0
