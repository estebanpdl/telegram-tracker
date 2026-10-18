# -*- coding: utf-8 -*-
'''
Fake-latency scheduler experiment: the same synthetic batch collected with
--concurrency 1, 3 and 10 on the virtual-time loop. Every fake request
takes LATENCY virtual seconds; the request policy keeps its 0.25 s
spacing. It shows how the scheduler overlaps requests under these
assumptions only; it says nothing about Telegram's real latency,
throughput, or safe request rates.

	python tests/scheduler_experiment.py        (from the repository root)
'''

# import modules
import contextlib
import copy
import io
import os
import sys
import tempfile

from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import offline_guard  # noqa: F401  (blocks network, config, and session access)
import source_path  # noqa: F401  (imports telegram_tracker from src)
import virtual_time

from fakes import FakeTelegramClient, FixtureWorld, load_world_raw


LATENCY = 1.0
INTERVAL = 0.25
CHANNELS = 10
PAGES = 3
PAGE_SIZE = 2

SHARED_CHAT = 201
PRIVATE_CHAT = 401
PRIVATE_FORWARDERS = (2, 5)

# batch: generated channels, an alias of the first, a missing and a private
# target from the base fixture
ALIAS = 'gen01_old_fx'


def channel_name(k):
	return f'gen{k:02d}_fx'


def scheduler_world(channels=CHANNELS, pages=PAGES):
	'''
	The base fixture world plus `channels` generated channels (ids 301...),
	each with pages * PAGE_SIZE messages. Every third message forwards the
	shared chat 201; channels in PRIVATE_FORWARDERS also forward the
	private chat 401 (discovered, inaccessible). ALIAS resolves to channel 301.
	'''
	raw = load_world_raw()
	raw['chats'][str(PRIVATE_CHAT)] = {'id': PRIVATE_CHAT, 'title': 'Locked FX', 'username': 'locked_fx'}
	raw['full_channels'][str(PRIVATE_CHAT)] = {'error': 'ChannelPrivateError'}
	forward = copy.deepcopy(next(m for m in raw['histories']['101'] if m.get('fwd_from'))['fwd_from'])
	start = datetime(2026, 2, 1, tzinfo=timezone.utc)

	for k in range(1, channels + 1):
		cid = 300 + k
		raw['chats'][str(cid)] = {'id': cid, 'title': f'Generated {k}', 'username': channel_name(k)}
		raw['entities'][channel_name(k)] = {'chat': cid}
		raw['full_channels'][str(cid)] = {
			'participants_count': 100 + k, 'about': f'Generated channel {k}',
			'linked_chat_id': None, 'chats': [cid]
		}
		history = []
		for msg_id in range(pages * PAGE_SIZE, 0, -1):
			message = {
				'id': msg_id,
				'date': (start + timedelta(days=k, hours=msg_id)).isoformat(),
				'message': f'Generated post {k}/{msg_id}',
				'views': 10 * msg_id
			}
			source = None
			if msg_id % 3 == 0:
				source = SHARED_CHAT
			elif msg_id == 2 and k in PRIVATE_FORWARDERS:
				source = PRIVATE_CHAT
			if source is not None:
				message['fwd_from'] = dict(forward, from_id={'_': 'PeerChannel', 'channel_id': source})
			history.append(message)
		raw['histories'][str(cid)] = history

	raw['entities'][ALIAS] = {'chat': 301}
	return raw


def batch_targets(channels=CHANNELS):
	return [channel_name(k) for k in range(1, channels + 1)] + [ALIAS, 'missing_fx', 'private_fx']


def collect(concurrency, out, *, raw=None, latency=LATENCY, targets=None, client=None, **kwargs):
	'''
	Run one collection on the virtual-time loop. Returns (summary, client,
	virtual seconds elapsed).
	'''
	from telegram_tracker import collector
	from telegram_tracker.api.auth import ensure_authorized

	client = client or FakeTelegramClient(
		FixtureWorld(raw=raw or scheduler_world()), page_size=PAGE_SIZE, latency=latency
	)
	clock = {}
	authorize = kwargs.pop('authorize', None) or (lambda c: ensure_authorized(c, None))

	async def main():
		import asyncio

		loop = asyncio.get_running_loop()
		start = loop.time()
		try:
			return await collector.run.run_collection(
				targets if targets is not None else batch_targets(),
				out,
				client_factory=lambda: client,
				authorize=authorize,
				concurrency=concurrency,
				request_interval=INTERVAL,
				**kwargs
			)
		finally:
			clock['elapsed'] = loop.time() - start

	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		summary = virtual_time.run(main())
	return summary, client, clock['elapsed']


def output_files(root):
	'''
	{relative path: bytes} of a collection folder, without the sidecar.
	'''
	files = {}
	for d, _, names in os.walk(root):
		for name in names:
			path = os.path.join(d, name)
			rel = os.path.relpath(path, root).replace('\\', '/')
			if rel != 'collection-status.json':
				with open(path, 'rb') as fh:
					files[rel] = fh.read()
	return files


def run_experiment(levels=(1, 3, 10)):
	'''
	Collect the batch at each concurrency level. Returns one row per level
	and whether every level wrote byte-identical files (sidecar excluded).
	'''
	rows, outputs = [], []
	with tempfile.TemporaryDirectory() as tmp:
		for n in levels:
			out = os.path.join(tmp, f'n{n}')
			summary, client, elapsed = collect(n, out)
			stats = summary.requests
			rows.append({
				'concurrency': n,
				'effective': summary.concurrency,
				'peak_in_flight': client.peak_active,
				'virtual_seconds': elapsed,
				'requests': sum(s['requests'] for s in stats.values()),
				'retries': sum(s['retries'] for s in stats.values()),
				'flood_waits': sum(s['flood_waits'] for s in stats.values()),
				'succeeded': len(summary.succeeded),
				'failed': len(summary.failed),
				'duplicates': len(summary.duplicates),
				'connects': client.connect_count,
				'disconnects': client.disconnect_count
			})
			outputs.append(output_files(out))
	return rows, all(o == outputs[0] for o in outputs)


def main():
	rows, identical = run_experiment()
	print(f'Fake latency {LATENCY} s per request, --request-interval {INTERVAL} s, '
		f'{CHANNELS} channels x {PAGES} pages + alias, missing, private targets')
	header = ('concurrency', 'peak_in_flight', 'virtual_seconds', 'requests',
		'retries', 'succeeded', 'failed', 'duplicates', 'connects', 'disconnects')
	print(' | '.join(header))
	for row in rows:
		print(' | '.join(
			f'{row[h]:.2f}' if isinstance(row[h], float) else str(row[h]) for h in header
		))
	print(f'identical output files across levels: {identical}')


if __name__ == '__main__':
	main()
