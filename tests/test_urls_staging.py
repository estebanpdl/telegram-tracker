# -*- coding: utf-8 -*-
'''
Injected SQLite setup errors on real scratch connections: the staging
connection must close, previous outputs must stay intact, and a failed
build must leave no temporary file or lease. No disk is filled.
'''

import os
import sqlite3
import unittest

from unittest import mock

from builder_parity_support import BATCH, ParityCase, collect
from url_build_support import inputs, leftovers, output_bytes, run_build
from url_data import add_url_messages

from telegram_tracker.urls import outputs


ERROR = 'database or disk is full'
REAL_CONNECT = sqlite3.connect


def failing_connect(step, connections):
	class Connection(sqlite3.Connection):
		def execute(self, sql, *args):
			if sql.startswith(step):
				raise sqlite3.OperationalError(ERROR)
			return super().execute(sql, *args)

		def executescript(self, sql):
			if step == 'schema':
				super().executescript('CREATE TABLE partial_setup (value)')
				raise sqlite3.OperationalError(ERROR)
			return super().executescript(sql)

	def connect(*args, **kwargs):
		con = REAL_CONNECT(*args, **kwargs, factory=Connection)
		connections.append(con)
		return con

	return connect


class StagingTests(ParityCase):

	def test_setup_errors_close_the_connection(self):
		for step in ('PRAGMA journal_mode', 'PRAGMA user_version', 'schema', 'BEGIN'):
			with self.subTest(step=step):
				connections = []
				path = os.path.join(self.tmp, step.replace(' ', '-') + '.sqlite3')
				try:
					with mock.patch.object(outputs.sqlite3, 'connect', failing_connect(step, connections)):
						with self.assertRaisesRegex(sqlite3.OperationalError, ERROR):
							outputs.open_staging(path)
					with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
						connections[0].execute('SELECT 1')
					os.remove(path)
				finally:
					for con in connections:
						con.close()

	def test_schema_error_keeps_outputs_and_removes_staging(self):
		out = self.folders['files']
		collect(out, 'files', BATCH, world_change=add_url_messages)
		run_build(out)
		earlier, before = output_bytes(out), inputs(out)
		connections = []
		original = outputs.open_staging

		def fail(path):
			with mock.patch.object(outputs.sqlite3, 'connect', failing_connect('schema', connections)):
				return original(path)

		try:
			with mock.patch.object(outputs, 'open_staging', fail):
				with self.assertRaisesRegex(sqlite3.OperationalError, ERROR):
					run_build(out)
			self.assertEqual(output_bytes(out), earlier)
			self.assertEqual(inputs(out), before)
			self.assertEqual(leftovers(out), [])
		finally:
			for con in connections:
				con.close()


if __name__ == '__main__':
	unittest.main()
