# -*- coding: utf-8 -*-
'''
Shared helpers for the storage-mode tests: `collect` through cli.main with
the fake client on the virtual-time loop, into temporary output folders,
and readers for the folder, its sidecar, manifest and database.
Synthetic data only.
'''

# import modules
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)
import virtual_time

from fakes import FakeTelegramClient, FixtureWorld, load_expected

from telegram_tracker import cli
from telegram_tracker.storage.manifest import MANIFEST_FILE
from telegram_tracker.storage.records import LOCK_FILE, STATUS_FILE
from telegram_tracker.storage.sqlite.schema import DB_FILENAME


EXPECTED = load_expected('collection.json')
BATCH = EXPECTED['scenarios']['batch']['targets']
PAGE_SIZE = EXPECTED['page_size']

# files whose content holds the run's time
TIMED = (STATUS_FILE, MANIFEST_FILE)


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


# A process killed inside a write transaction whose pages spilled into the
# file: it leaves a hot rollback journal, which only a writer rolls back.
KILLED_WRITER = (
	'import os, sqlite3, sys\n'
	'con = sqlite3.connect(sys.argv[1], isolation_level=None)\n'
	'con.execute("PRAGMA cache_size = 2")\n'
	'con.execute("BEGIN IMMEDIATE")\n'
	'con.execute("CREATE TABLE junk (x BLOB)")\n'
	'for _ in range(300): con.execute("INSERT INTO junk VALUES (randomblob(4000))")\n'
	'os._exit(0)\n'
)


def kill_inside_a_write(path):
	'''
	Leave the SQLite file `path` with a hot journal; returns the
	journal's path.
	'''
	subprocess.run([sys.executable, '-I', '-c', KILLED_WRITER, path], check=True, timeout=120)
	journal = f'{path}-journal'
	if not os.path.isfile(journal) or os.path.getsize(journal) == 0:
		raise AssertionError(f'no hot journal beside {path}')
	return journal


def listing(folder):
	'''
	Every file under `folder`, as sorted '/'-separated relative paths.
	'''
	found = []
	for root, _, files in os.walk(folder):
		for name in files:
			found.append(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, '/'))
	return sorted(found)


def file_contents(folder, *, skip=(DB_FILENAME,) + TIMED):
	'''
	{relative path: bytes} of the file outputs, without the database and
	the timed sidecars; provenance records lose run_started_at.
	'''
	contents = {}
	for name in listing(folder):
		if name in skip:
			continue
		path = os.path.join(folder, name)
		with open(path, 'rb') as fh:
			data = fh.read()
		if name.endswith('_provenance.json'):
			record = json.loads(data)
			record.pop('run_started_at')
			data = json.dumps(record, sort_keys=True).encode()
		contents[name] = data
	return contents


class ModesCase(offline_guard.OfflineTestCase):
	'''
	cli.main with a fake client factory (no credentials are read) and the
	virtual-time loop. `self.out` is a fresh output folder path.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = os.path.realpath(tmp.name)
		self.out = os.path.join(self.tmp, 'data')
		self.factory_calls = 0
		virtual = mock.patch.object(cli.collect, 'asyncio_run', virtual_time.run)
		virtual.start()
		self.addCleanup(virtual.stop)

	def make_client(self, raw=None):
		world = FixtureWorld(raw=raw) if raw is not None else None
		return FakeTelegramClient(world, page_size=PAGE_SIZE)

	def run_cli(self, argv, client=None, **kwargs):
		'''
		(exit code, stdout, stderr); `client` is what the factory returns.
		'''
		client = client or self.make_client()
		self.last_client = client

		def factory():
			self.factory_calls += 1
			return client

		stdout, stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = cli.main(argv, client_factory=factory, **kwargs)
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def collect(self, *extra, targets=('alpha_fx',), out=None, storage=None, **kwargs):
		argv = ['collect', '-o', out or self.out]
		if len(targets) == 1:
			argv += ['--telegram-channel', targets[0]]
		else:
			argv += ['--batch-file', self.batch_file(targets)]
		if storage is not None:
			argv += ['--storage', storage]
		return self.run_cli(argv + list(extra), **kwargs)

	def batch_file(self, targets):
		path = os.path.join(self.tmp, 'channels.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write(''.join(f'{t}\n' for t in targets))
		return path

	def status(self, out=None):
		return read_json(os.path.join(out or self.out, STATUS_FILE))

	def manifest(self, out=None):
		return read_json(os.path.join(out or self.out, MANIFEST_FILE))

	def db(self, out=None):
		return os.path.join(out or self.out, DB_FILENAME)

	def rows(self, sql, *args, out=None):
		con = sqlite3.connect(self.db(out))
		try:
			return con.execute(sql, args).fetchall()
		finally:
			con.close()

	def assertNoLocks(self, out=None):
		names = os.listdir(out or self.out)
		self.assertNotIn(LOCK_FILE, names)
		self.assertEqual([n for n in names if n.endswith('.lock')], [])


class BothCase(ModesCase):
	'''
	--storage both runs into `self.out` (files-mode runs into
	`self.files_out`), and readers for its publication state.
	'''

	def setUp(self):
		super().setUp()
		self.files_out = os.path.join(self.tmp, 'files')

	def both(self, *extra, targets=BATCH, **kwargs):
		return self.collect(*extra, targets=targets, storage='both', **kwargs)

	def pending(self):
		return sorted(self.rows('SELECT kind, run_id FROM destination_pending'))

	def targets(self, run=None):
		run = run or self.rows('SELECT max(run_id) FROM collection_runs')[0][0]
		return self.rows(
			'SELECT input, status, files_state, reason IS NOT NULL FROM run_targets WHERE run_id = ? '
			'ORDER BY input_index', run)

	def run_state(self):
		return self.rows('SELECT state, aggregate_files FROM collection_runs ORDER BY run_id DESC LIMIT 1')[0]

	def assertConsistentTargets(self, failed='beta_fx'):
		'''
		`failed` failed without a files_state; every succeeded target
		(workers may commit later targets first) has 'written'; no target
		is 'pending'.
		'''
		targets = {t[0]: t[1:3] for t in self.targets()}
		self.assertEqual(targets[failed], ('failed', None))
		self.assertTrue(any(status == 'succeeded' for status, _ in targets.values()))
		for status, files_state in targets.values():
			self.assertEqual(files_state, 'written' if status == 'succeeded' else None)

	def assertNoTemporaryFiles(self):
		self.assertEqual([f for f in listing(self.out) if '.tmp' in f or f.endswith('.creating')], [])

	def assertBlocked(self, blocked):
		calls = self.factory_calls
		code, _, err = self.collect(targets=['alpha_fx'])
		if blocked:
			self.assertEqual(code, 1)
			self.assertIn('is blocked', err)
			self.assertEqual(self.factory_calls, calls)
		else:
			self.assertEqual((code, 'is blocked' in err), (0, False), err)
