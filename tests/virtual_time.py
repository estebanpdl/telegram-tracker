# -*- coding: utf-8 -*-
'''
An asyncio event loop with a virtual clock. When nothing is ready to run,
the loop jumps its clock to the next scheduled timer instead of waiting,
so asyncio.sleep, wait_for timeouts, and the request policy's spacing and
cooldowns cost no wall time and are exactly reproducible.

	asyncio.run(main(), loop_factory=VirtualTimeLoop)

or `loop_factory = VirtualTimeLoop` on an IsolatedAsyncioTestCase. With no
timer scheduled the loop blocks on its selector as usual (thread
wake-ups still work).
'''

# import modules
import asyncio
import selectors


class _VirtualSelector(selectors.SelectSelector):

	def __init__(self):
		super().__init__()
		self.now = 0.0

	def select(self, timeout=None):
		events = super().select(0)
		if events or timeout is None:
			return events or super().select(None)
		if timeout > 0:
			self.now += timeout
		return []


class VirtualTimeLoop(asyncio.SelectorEventLoop):
	'''
	SelectorEventLoop whose time() starts at 0 and advances only when the
	loop would otherwise sleep.
	'''
	def __init__(self):
		self._virtual_selector = _VirtualSelector()
		super().__init__(selector=self._virtual_selector)

	def time(self):
		return self._virtual_selector.now


def run(coroutine):
	'''
	asyncio.run on a fresh VirtualTimeLoop.
	'''
	return asyncio.run(coroutine, loop_factory=VirtualTimeLoop)
