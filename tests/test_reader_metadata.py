# -*- coding: utf-8 -*-
'''
Builders check folder metadata after acquiring the database lease. A
collection finishing before that point is readable; a sidecar or
manifest that became invalid is refused without changing prior outputs.
'''

import contextlib
import os
import unittest

from unittest import mock

from builder_parity_support import BATCH, collect, inputs, read_bytes
from test_reader import ReaderCase, leases, quiet, read_json, write_json

from telegram_tracker.datasets import build_dataset
from telegram_tracker.network import build_network
from telegram_tracker.storage.manifest import MANIFEST_FILE
from telegram_tracker.storage.records import STATUS_FILE
from telegram_tracker.storage.sqlite.lease import DatabaseLease
from telegram_tracker.utils import DataInputError


BUILDERS = ((build_dataset, {'progress': False}), (build_network, {'image': False}))


@contextlib.contextmanager
def before_lease(change):
	# The callback may itself collect, taking the same kind of lease.
	original = DatabaseLease.__enter__
	changed = False

	def enter(lease):
		nonlocal changed
		if not changed:
			changed = True
			change()
		return original(lease)

	with mock.patch.object(DatabaseLease, '__enter__', enter):
		yield


class MetadataTests(ReaderCase):

	def test_collection_finishing_before_lease_uses_its_metadata(self):
		for builder, options in BUILDERS:
			with self.subTest(builder=builder.__name__):
				out = os.path.join(self.tmp, builder.__name__)
				collect(out, 'sqlite', BATCH)
				earlier = read_json(os.path.join(out, STATUS_FILE))['database']['run_uuid']
				with before_lease(lambda: collect(out, 'sqlite', BATCH, update=True)), quiet():
					builder(out, **options)
				latest = read_json(os.path.join(out, STATUS_FILE))['database']['run_uuid']
				name = 'dataset-build.json' if builder is build_dataset else 'network-build.json'
				record = read_json(os.path.join(out, name))
				self.assertNotEqual(latest, earlier)
				self.assertEqual(record['last_run']['run_uuid'], latest)
				self.assertEqual(record['last_run']['mode'], 'update')
				self.assertEqual(leases(out), [])

	def assertChangedRefused(self, change, pattern):
		for builder, options in BUILDERS:
			with self.subTest(builder=builder.__name__):
				out = os.path.join(self.tmp, builder.__name__)
				collect(out, 'sqlite', BATCH)
				earlier = os.path.join(out, 'msgs_dataset.csv')
				with open(earlier, 'wb') as fh:
					fh.write(b'earlier\n')
				expected = {}

				def change_and_record():
					change(out)
					expected.update(inputs(out))

				with before_lease(change_and_record), quiet():
					with self.assertRaisesRegex(DataInputError, pattern):
						builder(out, **options)
				self.assertEqual(inputs(out), expected)
				self.assertEqual(read_bytes(earlier), b'earlier\n')
				for name in ('collected_chats.xlsx', 'Graph.gexf', 'dataset-build.json', 'network-build.json'):
					self.assertFalse(os.path.exists(os.path.join(out, name)), name)
				self.assertEqual(leases(out), [])

	def test_sidecar_removed_before_lease(self):
		self.assertChangedRefused(lambda out: os.remove(os.path.join(out, STATUS_FILE)), 'is missing')

	def test_sidecar_interrupted_before_lease(self):
		def change(out):
			path = os.path.join(out, STATUS_FILE)
			data = read_json(path)
			data.update(state='interrupted', stop_reason='cancelled')
			write_json(path, data)

		self.assertChangedRefused(change, 'was interrupted')

	def test_sidecar_run_changed_before_lease(self):
		def change(out):
			path = os.path.join(out, STATUS_FILE)
			data = read_json(path)
			data['database']['run_uuid'] = '00000000-0000-4000-8000-000000000000'
			write_json(path, data)

		self.assertChangedRefused(change, 'describe different runs')

	def test_manifest_removed_before_lease(self):
		self.assertChangedRefused(lambda out: os.remove(os.path.join(out, MANIFEST_FILE)), 'is missing')

	def test_manifest_dataset_changed_before_lease(self):
		def change(out):
			path = os.path.join(out, MANIFEST_FILE)
			data = read_json(path)
			data['dataset_uuid'] = '00000000-0000-4000-8000-000000000000'
			write_json(path, data)

		self.assertChangedRefused(change, 'another dataset')


if __name__ == '__main__':
	unittest.main()
