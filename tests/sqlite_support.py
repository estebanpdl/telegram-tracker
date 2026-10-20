# -*- coding: utf-8 -*-
'''
Shared helpers for the SQLite backend tests: temporary output folders and
databases, collection through the fake client into the database or the
files, and readers for both. Synthetic data only.
'''

# import modules
import contextlib
import copy
import functools
import io
import json
import os
import sqlite3
import tempfile

from datetime import timedelta

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import FakeTelegramClient, load_expected

from telegram_tracker import collector
from telegram_tracker.api.auth import ensure_authorized
from telegram_tracker.storage.sqlite.connection import open_database
from telegram_tracker.storage.sqlite.schema import DB_FILENAME
from telegram_tracker.storage.sqlite.writer import SQLiteWriter


EXPECTED = load_expected('collection.json')
BATCH = EXPECTED['scenarios']['batch']['targets']
PAGE_SIZE = EXPECTED['page_size']
ALPHA = 101
BETA = 102
EMPTY = 103
PROFILE_ID = '00000000-0000-4000-8000-000000000000'
OTHER_PROFILE_ID = '11111111-1111-4111-8111-111111111111'


def read_bytes(path):
	with open(path, 'rb') as fh:
		return fh.read()


def read_json(path):
	with open(path, encoding='utf-8') as fh:
		return json.load(fh)


def normalized(data):
	'''
	JSON text with sorted keys, to compare payloads as JSON values.
	'''
	return json.dumps(data, sort_keys=True, ensure_ascii=False)


def add_messages(world, channel_id, *ids, user_id=None, forward_from=None):
	'''
	Prepend new messages (copies of the newest one) to a fixture history;
	with `user_id`, each new message is authored by that user, and with
	`forward_from`, forwarded from that channel.
	'''
	history = world.histories[channel_id]
	newest = history[0]
	for offset, msg_id in enumerate(sorted(ids, reverse=True)):
		message = copy.deepcopy(newest)
		message['id'] = msg_id
		message['date'] = newest['date'] + timedelta(days=len(ids) - offset)
		message['message'] = f'Synthetic new post {msg_id}'
		message['views'] = 1000 + msg_id
		if user_id is not None:
			message['from_id'] = {'_': 'PeerUser', 'user_id': user_id}
		if forward_from is not None:
			message['fwd_from'] = {
				'_': 'MessageFwdHeader',
				'from_id': {'_': 'PeerChannel', 'channel_id': forward_from},
				'date': newest['date'],
				'channel_post': 1
			}
		history.insert(0, message)


class SQLiteCase(offline_guard.OfflineAsyncTestCase):
	'''
	A temporary output folder `out` with the database path `db` inside it.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(tmp.name, 'data')
		self.db = os.path.join(self.out, DB_FILENAME)
		self.stderr = io.StringIO()

	def client(self, **kwargs):
		kwargs.setdefault('page_size', PAGE_SIZE)
		return FakeTelegramClient(**kwargs)

	async def collect(self, targets=None, client=None, out=None, *, sqlite=True, writer_factory=None, **kwargs):
		'''
		Run a collection with the fake client into the database (or, with
		sqlite=False, the files). Keeps the client as self.last_client.
		'''
		self.last_client = client or self.client()
		out = out or self.out
		if writer_factory is None and sqlite:
			writer_factory = functools.partial(
				SQLiteWriter.open, os.path.join(out, DB_FILENAME), busy_timeout_ms=200
			)
		with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(self.stderr):
			return await collector.run.run_collection(
				BATCH if targets is None else targets,
				out,
				client_factory=lambda: self.last_client,
				authorize=lambda c: ensure_authorized(c, None),
				writer_factory=writer_factory,
				**kwargs
			)

	def connect(self, path=None):
		'''
		A plain read connection (no schema checks) closed after the test.
		'''
		con = sqlite3.connect(path or self.db)
		self.addCleanup(con.close)
		return con

	def open(self, path=None):
		con = open_database(path or self.db)
		self.addCleanup(con.close)
		return con

	def rows(self, sql, *args, path=None):
		return self.connect(path).execute(sql, args).fetchall()

	def assertIntact(self, path=None):
		con = self.connect(path)
		self.assertEqual(con.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
		self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])

	def history_ids(self, channel_id, path=None):
		return [r[0] for r in self.rows(
			'SELECT message_id FROM messages WHERE channel_id = ? ORDER BY message_id DESC',
			channel_id, path=path
		)]
