# -*- coding: utf-8 -*-
'''
Shared helpers for the builder reader tests: one collection sequence run
into a files, a sqlite and a both folder (through storage.destination,
as `collect --storage` does, with the fake client on the virtual-time
loop), the builders run on each, and their outputs in comparable form.
Synthetic data only.
'''

# import modules
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)
import virtual_time

from fakes import FakeTelegramClient, load_expected

from telegram_tracker import collector
from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.storage.destination import select_destination
from telegram_tracker.storage.provenance import RunProfile


EXPECTED = load_expected('collection.json')
BATCH = EXPECTED['scenarios']['batch']['targets']
PAGE_SIZE = EXPECTED['page_size']
MODES = ('files', 'sqlite', 'both')

ALICE = RunProfile('alice', '11111111-1111-4111-8111-111111111111')
BOB = RunProfile('bob', '33333333-3333-4333-8333-333333333333')
ALPHA = 101

# (label, folder mode, --source): the four ways a folder is built from
BUILDS = (
	('files', 'files', 'auto'),
	('sqlite', 'sqlite', 'auto'),
	('both', 'both', 'auto'),
	('both-sqlite', 'both', 'sqlite')
)

OUTPUTS = ('msgs_dataset.csv', 'collected_chats.xlsx', 'Graph.gexf', 'dataset-build.json',
	'network-build.json')


def add_post(world, channel_id=ALPHA):
	'''
	A new newest message in a channel's history.
	'''
	history = world.histories[channel_id]
	post = copy.deepcopy(history[0])
	post['id'] = history[0]['id'] + 1
	history.insert(0, post)


def rename(world, channel_id, new_name):
	'''
	The channel's username becomes `new_name`, which then resolves to it.
	'''
	old = world.chats[channel_id].get('username')
	world.chats[channel_id]['username'] = new_name
	world.entities[new_name] = world.entities[old]


def collect(out, mode, targets, *, world_change=None, **kwargs):
	'''
	One collection into `out` with a fresh fake client (after
	`world_change(world)`), through the destination of `mode`.
	'''
	client = FakeTelegramClient(page_size=PAGE_SIZE)
	if world_change is not None:
		world_change(client.world)
	destination = select_destination(out, mode)
	destination.check()
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		return virtual_time.run(collector.run.run_collection(
			list(targets),
			out,
			client_factory=lambda: client,
			authorize=lambda c: ensure_authorized(c, None),
			writer_factory=destination.open_writer,
			**kwargs
		))


def build(out, source='auto', *, image=False):
	'''
	build-dataset then build-network on `out`. Returns (dataset summary,
	network summary, stdout, stderr).
	'''
	from telegram_tracker.datasets import build_dataset
	from telegram_tracker.network import build_network

	stdout, stderr = io.StringIO(), io.StringIO()
	with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
		dataset = build_dataset(out, source=source, progress=False)
		network = build_network(out, source=source, image=image)
	return dataset, network, stdout.getvalue(), stderr.getvalue()


def read_bytes(path):
	with open(path, 'rb') as fh:
		return fh.read()


def workbook(path):
	'''
	collected_chats.xlsx as (columns, rows of cell values).
	'''
	import pandas as pd

	frame = pd.read_excel(path, dtype=object)
	rows = [[None if pd.isna(v) else v for v in row] for row in frame.itertuples(index=False)]
	return list(frame.columns), rows


def graph(path):
	'''
	Graph.gexf as (sorted nodes, sorted directed edges); layout and
	attributes are not compared.
	'''
	import networkx as nx

	G = nx.read_gexf(path)
	return sorted(G.nodes), sorted(G.edges)


def build_record(path, *, provenance=True):
	'''
	A build record without its time and, unless `provenance`, without
	the source-specific fields (source, storage, database, last_run, and
	each history's run, mode and takeover).
	'''
	with open(path, encoding='utf-8') as fh:
		record = json.load(fh)
	record.pop('built_at')
	if not provenance:
		for key in ('source', 'storage', 'database', 'last_run'):
			record.pop(key)
		for history in record['histories'] or ():
			for key in ('run', 'mode', 'takeover'):
				history.pop(key)
	return record


def outputs(out):
	'''
	The builders' outputs in `out`, in comparable form.
	'''
	return {
		'msgs_dataset.csv': read_bytes(os.path.join(out, 'msgs_dataset.csv')),
		'collected_chats.xlsx': workbook(os.path.join(out, 'collected_chats.xlsx')),
		'Graph.gexf': graph(os.path.join(out, 'Graph.gexf')),
		'dataset-build.json': build_record(os.path.join(out, 'dataset-build.json'), provenance=False),
		'network-build.json': build_record(os.path.join(out, 'network-build.json'), provenance=False)
	}


def inputs(out):
	'''
	{relative path: bytes} of every file in `out` that is not a builder output.
	'''
	contents = {}
	for root, _, files in os.walk(out):
		for name in files:
			rel = os.path.relpath(os.path.join(root, name), out).replace(os.sep, '/')
			if rel not in OUTPUTS:
				contents[rel] = read_bytes(os.path.join(root, name))
	return contents


class ParityCase(offline_guard.OfflineTestCase):
	'''
	`self.folders[mode]`: a folder per storage mode under a temporary
	directory, each given the same collection sequence by `sequence`.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.folders = {mode: os.path.join(self.tmp, mode) for mode in MODES}

	def sequence(self, *steps):
		'''
		Run every step (targets, kwargs) into each folder, in order.
		'''
		for mode, out in self.folders.items():
			for targets, kwargs in steps:
				collect(out, mode, targets, **kwargs)

	def build_copies(self, *, repeat=1):
		'''
		{build label: comparable outputs after each of `repeat` builds},
		each label built in its own copy of its folder (a moved copy reads
		like the original). Also checks every input is unchanged.
		'''
		results = {}
		for label, mode, source in BUILDS:
			out = os.path.join(self.tmp, f'build-{label}')
			shutil.copytree(self.folders[mode], out)
			before = inputs(out)
			results[label] = []
			for _ in range(repeat):
				build(out, source)
				results[label].append(outputs(out))
			self.assertEqual(inputs(out), before, label)
		return results

	def assertParity(self, results, labels=None):
		'''
		Every build label gives what the files build gives.
		'''
		expected = results['files']
		for label in labels or results:
			for index, (got, want) in enumerate(zip(results[label], expected)):
				for name in want:
					with self.subTest(build=label, repeat=index, output=name):
						self.assertEqual(got[name], want[name])
