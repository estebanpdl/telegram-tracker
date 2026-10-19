# -*- coding: utf-8 -*-
'''
SQLite schema contract: docs/sqlite-schema.sql applied to an empty database
and exercised with the synthetic payloads in fixtures/sqlite/variants.json.

Covers the identity pragmas, typed keys and constraints, the generated
columns derived from unfiltered raw JSON, account-scoped provenance views,
and docs/sqlite-schema.md: every table, view, index, trigger and column is
documented, and every example query runs.
'''

# import modules
import json
import os
import re
import sqlite3
import tempfile
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from telethon.tl.alltlobjects import tlobjects

from telegram_tracker.storage.files import to_json_text


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL_PATH = os.path.join(ROOT, 'docs', 'sqlite-schema.sql')
DOC_PATH = os.path.join(ROOT, 'docs', 'sqlite-schema.md')
VARIANTS_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'sqlite', 'variants.json')

CHANNEL = 1001
PROFILE_ID = '00000000-0000-4000-8000-000000000000'
OTHER_PROFILE_ID = '11111111-1111-4111-8111-111111111111'
PEER_KIND = {'User': 'user', 'UserEmpty': 'user', 'Chat': 'chat', 'ChatForbidden': 'chat',
	'Channel': 'channel', 'ChannelForbidden': 'channel'}


def read_text(path):
	with open(path, encoding='utf-8') as fh:
		return fh.read()


VARIANTS = json.loads(read_text(VARIANTS_PATH))


def walk(value):
	'''
	Yield every (key, value) pair in nested dicts and lists.
	'''
	if isinstance(value, dict):
		for key, item in value.items():
			yield key, item
			yield from walk(item)
	elif isinstance(value, list):
		for item in value:
			yield from walk(item)


def constructors(raw):
	return {item for key, item in walk(raw) if key == '_'} | ({raw['_']} if '_' in raw else set())


class SchemaTestCase(offline_guard.OfflineTestCase):
	'''
	A fresh database with the schema applied and two runs: run 1 without a
	profile, run 2 under a named profile.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.path = os.path.join(tmp.name, 'synthetic.sqlite3')
		self.con = sqlite3.connect(self.path, isolation_level=None)
		self.addCleanup(self.con.close)
		self.con.execute('PRAGMA foreign_keys = ON')
		self.con.executescript(read_text(DDL_PATH))
		self.con.execute(
			"INSERT INTO schema_meta (singleton, schema_version, dataset_uuid, created_at, updated_at, app_version) "
			"VALUES (1, 1, ?, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', '0.1.0')", (PROFILE_ID,))
		self.add_run(1)
		self.add_run(2, profile_name='work', profile_id=PROFILE_ID)

	def add_run(self, run_id, profile_name=None, profile_id=None, storage='sqlite', mode='full'):
		self.con.execute(
			'INSERT INTO collection_runs (run_id, run_uuid, profile_name, profile_id, state, mode, storage, '
			'started_at, parameters, app_version, telethon_version, serializer_version) '
			"VALUES (?, ?, ?, ?, 'complete', ?, ?, '2024-01-01T00:00:00+00:00', '{}', '0.1.0', '1.45.0', 1)",
			(run_id, f'run-{run_id}', profile_name, profile_id, mode, storage))

	def add_history(self, channel_id=CHANNEL, run_id=1, envelope=None, count=0):
		self.con.execute(
			'INSERT INTO histories (channel_id, run_id, mode, envelope, message_count, updated_at) '
			"VALUES (?, ?, 'replaced', ?, ?, '2024-01-01T00:00:00+00:00')",
			(channel_id, run_id, to_json_text(envelope or {'_': 'ChannelMessages', 'count': count}), count))

	def add_message(self, raw, channel_id=CHANNEL, run_id=1):
		self.con.execute('INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, ?, ?, ?)',
			(channel_id, raw['id'], run_id, to_json_text(raw)))

	def add_peer(self, raw, run_id=1):
		self.con.execute(
			'INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, last_seen_run) '
			'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
			(PEER_KIND[raw['_']], raw['id'], raw['_'], int(bool(raw.get('min'))), to_json_text(raw), run_id, run_id,
				run_id))

	def load_variants(self):
		self.add_history(count=len(VARIANTS['messages']))
		for variant in VARIANTS['messages']:
			self.add_message(variant['raw'])
		for variant in VARIANTS['peers']:
			self.add_peer(variant['raw'])
		snapshot = VARIANTS['channel_full']['raw']
		self.con.execute(
			"INSERT INTO channel_snapshots (channel_id, run_id, observed_at, raw) VALUES (?, 1, 't', ?)",
			(CHANNEL, to_json_text(snapshot)))

	def one(self, sql, *args):
		return self.con.execute(sql, args).fetchone()

	def assertRejected(self, sql, *args):
		with self.assertRaises(sqlite3.IntegrityError):
			self.con.execute(sql, args)


class IdentityTests(SchemaTestCase):

	def test_application_id_matches_its_documented_spelling(self):
		value, hex_value, name = re.search(
			r"PRAGMA application_id = (\d+);\s+-- 0x([0-9A-F]{8}), '(\w{4})'", read_text(DDL_PATH)).groups()
		self.assertEqual(int(value), int(hex_value, 16))
		self.assertEqual(int(value), int.from_bytes(name.encode('ascii'), 'big'))
		self.assertEqual(name, 'TGTR')
		self.assertEqual(self.one('PRAGMA application_id'), (int(value),))

	def test_schema_version_one(self):
		self.assertEqual(self.one('PRAGMA user_version'), (1,))
		self.assertEqual(self.one('SELECT schema_version FROM schema_meta'), (1,))

	def test_sqlite_floor_and_json1(self):
		self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 31, 0))
		self.assertEqual(self.one("SELECT json_valid('{}'), json_type('[]')"), (1, 'array'))

	def test_single_storage_mode_without_field_filters(self):
		triggers = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
		self.assertEqual(sorted(triggers), ['run_targets_files_state_insert', 'run_targets_files_state_update'])
		columns = {r[1] for t in self.tables() for r in self.con.execute(f'PRAGMA table_xinfo({t})')}
		self.assertNotIn('user_fields', columns)

	def tables(self):
		return [r[0] for r in self.con.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]


class FixtureTests(unittest.TestCase):

	def test_constructors_exist_in_pinned_telethon(self):
		names = {cls.__name__ for cls in tlobjects.values()}
		for group in ('messages', 'peers', 'envelopes'):
			for variant in VARIANTS[group]:
				with self.subTest(variant['name']):
					missing = constructors(variant['raw']) - names
					if variant['telethon']:
						self.assertEqual(missing, set())
					else:
						self.assertTrue(missing, 'a hand-built variant should carry an unknown constructor')
		self.assertEqual(constructors(VARIANTS['channel_full']['raw']) - names, set())

	def test_values_are_synthetic(self):
		for key, value in walk(VARIANTS):
			if value is None:
				continue
			if key == 'username':
				self.assertTrue(value.endswith('_fx'), value)
			elif key in ('phone', 'phone_number'):
				self.assertTrue(value.startswith('99966'), value)
			elif key in ('access_hash', 'id', 'photo_id', 'user_id', 'channel_id', 'chat_id'):
				self.assertLess(value, 10 ** 6, key)

	def test_variant_names_are_unique(self):
		for group in ('messages', 'peers', 'envelopes'):
			names = [v['name'] for v in VARIANTS[group]]
			self.assertEqual(len(names), len(set(names)), group)


class RawRetentionTests(SchemaTestCase):

	def test_messages_keep_raw_and_derive_columns(self):
		self.load_variants()
		for variant in VARIANTS['messages']:
			with self.subTest(variant['name']):
				raw = variant['raw']
				stored = self.one('SELECT raw FROM messages WHERE channel_id = ? AND message_id = ?', CHANNEL, raw['id'])
				self.assertEqual(stored, (to_json_text(raw),))
				columns = sorted(variant['expect'])
				row = self.one(f'SELECT {", ".join(columns)} FROM messages WHERE message_id = ?', raw['id'])
				self.assertEqual(dict(zip(columns, row)), variant['expect'])

	def test_peers_keep_raw_and_derive_columns(self):
		self.load_variants()
		for variant in VARIANTS['peers']:
			with self.subTest(variant['name']):
				raw = variant['raw']
				key = (variant['kind'], raw['id'])
				self.assertEqual(self.one('SELECT raw FROM peers WHERE peer_kind = ? AND peer_id = ?', *key),
					(to_json_text(raw),))
				columns = sorted(variant['expect'])
				row = self.one(f'SELECT {", ".join(columns)} FROM peers WHERE peer_kind = ? AND peer_id = ?', *key)
				self.assertEqual(dict(zip(columns, row)), variant['expect'])

	def test_personal_and_credential_fields_survive(self):
		self.load_variants()
		self.assertEqual(self.one(
			"SELECT json_extract(raw, '$.photo.stripped_thumb'), json_extract(raw, '$.status.was_online') "
			"FROM peers WHERE peer_kind = 'user' AND peer_id = 2001"),
			("b'\\x01\\x08'", '2024-01-06 12:00:00+00:00'))
		self.assertEqual(self.one(
			"SELECT json_extract(raw, '$.migrated_to.access_hash') FROM peers WHERE peer_kind = 'chat' AND peer_id = 1001"),
			(8006,))
		self.assertEqual(self.one(
			"SELECT json_extract(raw, '$.media.geo.access_hash') FROM messages WHERE message_id = 23"), (6003,))
		self.assertEqual(self.one(
			"SELECT json_extract(raw, '$.users[0].access_hash') FROM channel_snapshots WHERE channel_id = ?", CHANNEL),
			(8010,))

	def test_equal_ids_stay_distinct_across_peer_kinds(self):
		self.load_variants()
		self.assertEqual(self.con.execute(
			'SELECT peer_kind FROM peers WHERE peer_id = ? ORDER BY peer_kind', (CHANNEL,)).fetchall(),
			[('channel',), ('chat',), ('user',)])

	def test_unknown_fields_are_queryable(self):
		self.load_variants()
		self.assertEqual(self.one(
			"SELECT json_extract(raw, '$.future_field._'), media_type FROM messages "
			"WHERE json_extract(raw, '$.future_field') IS NOT NULL"),
			('FutureConstructorFx', 'MessageMediaFutureFx'))

	def test_channel_snapshot_projections(self):
		self.load_variants()
		expect = VARIANTS['channel_full']['expect']
		columns = sorted(expect)
		row = self.one(f'SELECT {", ".join(columns)} FROM channel_snapshots WHERE channel_id = ?', CHANNEL)
		self.assertEqual(dict(zip(columns, row)), expect)

	def test_envelope_variants(self):
		for offset, variant in enumerate(VARIANTS['envelopes']):
			raw = variant['raw']
			envelope = {k: v for k, v in raw.items() if k not in ('messages', 'chats', 'users')}
			self.add_history(channel_id=4000 + offset, envelope=envelope, count=len(raw['messages']))
		self.assertEqual(self.one(
			"SELECT json_extract(envelope, '$.inexact'), json_extract(envelope, '$.offset_id_offset'), "
			"json_extract(envelope, '$.count') FROM histories WHERE channel_id = 4000"), (1, 40, 500))
		self.assertEqual(self.one(
			"SELECT json_array_length(envelope, '$.topics'), json_extract(envelope, '$.topics[0]._') "
			"FROM histories WHERE channel_id = 4001"), (1, 'ForumTopic'))

	def test_empty_history_differs_from_absent(self):
		self.add_history(channel_id=4002)
		self.assertEqual(self.one('SELECT message_count FROM histories WHERE channel_id = 4002'), (0,))
		self.assertIsNone(self.one('SELECT message_count FROM histories WHERE channel_id = 4003'))

	def test_generated_columns_are_read_only(self):
		self.load_variants()
		with self.assertRaises(sqlite3.OperationalError):
			self.con.execute("UPDATE peers SET phone = '99966600009' WHERE peer_id = 2001")


class ProvenanceTests(SchemaTestCase):

	def test_access_hash_follows_the_run_that_supplied_raw(self):
		user = next(v['raw'] for v in VARIANTS['peers'] if v['name'] == 'full user')
		self.add_peer(user, run_id=1)
		self.assertEqual(self.one(
			"SELECT access_hash, run_id, profile_name, profile_id FROM peer_access_hashes WHERE peer_id = 2001"),
			(8001, 1, None, None))
		# a later sighting that keeps raw (such as a min copy) moves last_seen_run only
		self.con.execute("UPDATE peers SET last_seen_run = 2 WHERE peer_kind = 'user' AND peer_id = 2001")
		self.assertEqual(self.one(
			"SELECT access_hash, run_id, profile_name, profile_id FROM peer_access_hashes WHERE peer_id = 2001"),
			(8001, 1, None, None))
		replaced = dict(user, access_hash=8101)
		self.con.execute("UPDATE peers SET raw = ?, run_id = 2 WHERE peer_kind = 'user' AND peer_id = 2001",
			(to_json_text(replaced),))
		self.assertEqual(self.one(
			"SELECT access_hash, run_id, profile_name, profile_id FROM peer_access_hashes WHERE peer_id = 2001"),
			(8101, 2, 'work', PROFILE_ID))

	def test_min_hashes_are_marked(self):
		self.load_variants()
		self.assertEqual(self.con.execute(
			'SELECT peer_kind, peer_id FROM peer_access_hashes WHERE is_min = 1 ORDER BY peer_kind').fetchall(),
			[('channel', 1003), ('user', 2002)])
		self.assertIsNone(self.one("SELECT 1 FROM peer_access_hashes WHERE peer_kind = 'user' AND peer_id = 2005"))

	def test_media_references_carry_the_writing_profile(self):
		self.add_history(run_id=2)
		for variant in VARIANTS['messages']:
			if variant['name'] in ('photo', 'document', 'poll'):
				self.add_message(variant['raw'], run_id=2)
		self.assertEqual(self.con.execute(
			'SELECT message_id, media_id, media_access_hash, media_file_reference, profile_name, profile_id '
			'FROM message_media_references ORDER BY message_id').fetchall(),
			[(20, 5001, 6001, "b'\\x01\\x02\\x03'", 'work', PROFILE_ID),
				(21, 5002, 6002, "b'\\x04\\x05'", 'work', PROFILE_ID)])

	def test_profile_fields_are_checked(self):
		# runs, histories and takeover records share one profile format
		def history(key, name, profile_id):
			self.con.execute(
				'INSERT INTO histories (channel_id, run_id, profile_name, profile_id, mode, envelope, message_count, '
				"updated_at) VALUES (?, 1, ?, ?, 'replaced', '{}', 0, 't')", (5000 + key, name, profile_id))

		def takeover(key, name, profile_id):
			self.con.execute(
				'INSERT INTO run_targets (run_id, input_index, input, status, takeover, previous_profile_name, '
				"previous_profile_id) VALUES (1, ?, 'synthetic_fx', 'succeeded', 1, ?, ?)", (key, name, profile_id))

		def run(key, name, profile_id):
			self.add_run(10 + key, profile_name=name, profile_id=profile_id)

		for place in (run, history, takeover):
			for key, (name, profile_id) in enumerate((('Bad Name', None), ('1abc', None), ('work.2', None),
					('w' * 33, None), ('default', '12345'))):
				with self.subTest(place=place.__name__, name=name, profile_id=profile_id):
					with self.assertRaises(sqlite3.IntegrityError):
						place(key, name, profile_id)
			with self.subTest(place=place.__name__, valid=True):
				place(9, 'work-2_b', OTHER_PROFILE_ID)

	def test_previous_profile_only_with_a_takeover(self):
		self.assertRejected(
			"INSERT INTO run_targets (run_id, input_index, input, status, takeover, previous_profile_name) "
			"VALUES (2, 0, 'synthetic_fx', 'succeeded', 0, 'work')")
		self.con.execute(
			"INSERT INTO run_targets (run_id, input_index, input, status, takeover, previous_profile_name, "
			"previous_profile_id) VALUES (2, 0, 'synthetic_fx', 'succeeded', 1, 'other', ?)", (OTHER_PROFILE_ID,))


class ConstraintTests(SchemaTestCase):

	def test_message_needs_a_history_and_valid_json(self):
		message = VARIANTS['messages'][1]['raw']
		with self.assertRaises(sqlite3.IntegrityError):
			self.add_message(message)
		self.add_history()
		# indexed generated columns evaluate json_extract before the CHECK runs
		with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
			self.con.execute('INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, 99, 1, ?)',
				(CHANNEL, 'not json'))
		self.assertRejected("INSERT INTO histories (channel_id, run_id, mode, envelope, message_count, updated_at) "
			"VALUES (4004, 1, 'replaced', 'not json', 0, 't')")
		self.assertRejected('INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, 0, 1, ?)',
			CHANNEL, '{}')

	def test_raw_must_be_strict_json(self):
		# json_extract reads JSON5 since SQLite 3.45 (and raises before it); only
		# json_valid keeps raw to strict JSON
		json5 = "{_:'Chat',id:6}"
		self.add_history()
		for sql, args in (
				("INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, "
					"last_seen_run) VALUES ('chat', 6, 'Chat', 0, ?, 1, 1, 1)", (json5,)),
				('INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, 6, 1, ?)', (CHANNEL, json5))):
			with self.subTest(sql.split(' (')[0]):
				with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
					self.con.execute(sql, args)

	def test_message_key_upserts_the_fetched_copy(self):
		self.add_history()
		raw = VARIANTS['messages'][1]['raw']
		self.add_message(raw)
		self.con.execute(
			'INSERT INTO messages (channel_id, message_id, run_id, raw) VALUES (?, ?, 2, ?) '
			'ON CONFLICT (channel_id, message_id) DO UPDATE SET raw = excluded.raw, run_id = excluded.run_id',
			(CHANNEL, raw['id'], to_json_text(dict(raw, views=99))))
		self.assertEqual(self.one('SELECT count(*), max(views), max(run_id) FROM messages'), (1, 99, 2))

	def test_peer_key_and_kind(self):
		user = VARIANTS['peers'][0]['raw']
		self.add_peer(user)
		with self.assertRaises(sqlite3.IntegrityError):
			self.add_peer(user)
		self.assertRejected(
			"INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, last_seen_run) "
			"VALUES ('group', 5, 'Chat', 0, '{}', 1, 1, 1)")
		self.assertRejected(
			"INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, last_seen_run) "
			"VALUES ('chat', 5, 'Chat', 2, '{}', 1, 1, 1)")
		# indexed generated columns evaluate json_extract before the CHECK runs
		with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
			self.con.execute(
				"INSERT INTO peers (peer_kind, peer_id, constructor, is_min, raw, run_id, first_seen_run, last_seen_run) "
				"VALUES ('chat', 6, 'Chat', 0, 'not json', 1, 1, 1)")

	def test_history_entities_reference_peers(self):
		self.add_history()
		self.assertRejected(
			"INSERT INTO history_entities VALUES (?, 'users', 0, 'user', 2001)", CHANNEL)
		self.add_peer(VARIANTS['peers'][0]['raw'])
		self.con.execute("INSERT INTO history_entities VALUES (?, 'users', 0, 'user', 2001)", (CHANNEL,))
		self.assertRejected("INSERT INTO history_entities VALUES (?, 'users', 1, 'user', 2001)", CHANNEL)

	def test_publication_state_requires_both_storage(self):
		self.assertRejected("UPDATE collection_runs SET aggregate_files = 'pending' WHERE run_id = 1")
		self.assertRejected(
			"INSERT INTO run_targets (run_id, input_index, input, status, files_state) "
			"VALUES (1, 0, 'synthetic_fx', 'succeeded', 'pending')")
		self.con.execute("INSERT INTO run_targets (run_id, input_index, input, status) VALUES (1, 0, 'synthetic_fx', 'succeeded')")
		self.assertRejected("UPDATE run_targets SET files_state = 'written' WHERE run_id = 1")

	def test_pending_rows_block_through_partial_indexes(self):
		self.add_run(3, storage='both')
		self.con.execute(
			"INSERT INTO run_targets (run_id, input_index, input, status, files_state) "
			"VALUES (3, 0, 'synthetic_fx', 'succeeded', 'pending')")
		self.con.execute("UPDATE collection_runs SET aggregate_files = 'pending' WHERE run_id = 3")
		self.assertEqual(self.con.execute('SELECT kind, run_id FROM destination_pending ORDER BY kind').fetchall(),
			[('aggregates', 3), ('target', 3)])
		plan = ' '.join(r[3] for r in self.con.execute('EXPLAIN QUERY PLAN SELECT * FROM destination_pending'))
		self.assertIn('run_targets_files_pending', plan)
		self.assertIn('runs_aggregate_pending', plan)
		self.con.execute("UPDATE run_targets SET files_state = 'written' WHERE run_id = 3")
		self.con.execute("UPDATE collection_runs SET aggregate_files = 'written' WHERE run_id = 3")
		self.assertEqual(self.con.execute('SELECT * FROM destination_pending').fetchall(), [])
		self.assertRejected("UPDATE run_targets SET files_state = 'done' WHERE run_id = 3")

	def test_aggregate_views_keep_append_semantics(self):
		self.con.execute("INSERT INTO run_targets (run_id, input_index, input, status) VALUES (1, 0, 'synthetic_fx', 'succeeded')")
		self.con.execute("INSERT INTO run_targets (run_id, input_index, input, status) VALUES (2, 0, 'synthetic_fx', 'succeeded')")
		self.con.executemany(
			'INSERT INTO run_observations (run_id, seq, input_index, kind, channel_id, username) VALUES (?, ?, 0, ?, ?, ?)',
			[(1, 0, 'channel_request', 7, 'a_fx'), (1, 1, 'from_messages', 8, 'b_fx'), (1, 2, 'from_messages', 7, 'a_fx'),
				(2, 0, 'from_messages', 9, 'c_fx'), (2, 1, 'from_messages', 8, 'b_fx')])
		self.assertEqual([r[0] for r in self.con.execute('SELECT channel_id FROM chat_ids_all')], [7, 8, 9])
		self.assertRejected(
			"INSERT INTO run_observations (run_id, seq, input_index, kind, channel_id) VALUES (1, 9, 5, 'linked', 1)")
		self.assertRejected("INSERT INTO run_counters VALUES (1, 7, 0, 'a_fx', 2, 1, 1, '{}', '[]')")
		self.con.execute("INSERT INTO run_counters VALUES (1, 7, 0, 'a_fx', 2, 1, 1, '[\"self\"]', '[\"synthetic_fx\"]')")
		metadata = to_json_text({'id': 7, 'username': 'a_fx', 'access_hash': 8011})
		self.con.executemany('INSERT INTO collected_chat_rows VALUES (?, ?, 7, ?)', [(1, 0, metadata), (1, 1, metadata)])
		self.assertEqual(self.con.execute(
			"SELECT row_index, counter, json_extract(metadata, '$.access_hash') FROM collected_chats_all").fetchall(),
			[(0, 2, 8011), (1, 2, 8011)])

	def test_integrity(self):
		self.load_variants()
		self.assertEqual(self.one('PRAGMA integrity_check'), ('ok',))
		self.assertEqual(self.con.execute('PRAGMA foreign_key_check').fetchall(), [])


class DocumentTests(SchemaTestCase):

	def setUp(self):
		super().setUp()
		self.doc = read_text(DOC_PATH)

	def section(self, table):
		match = re.search(rf'^### {table}\n(.*?)(?=^##)', self.doc, re.S | re.M)
		self.assertIsNotNone(match, f'no section for {table}')
		return match.group(1)

	def test_every_schema_object_is_documented(self):
		for kind, name, table in self.con.execute(
				"SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'").fetchall():
			with self.subTest(name):
				if kind == 'table':
					self.assertIn(f'### {name}\n', self.doc)
				elif kind == 'view':
					self.assertIn(f'| `{name}` |', self.doc)
				else:
					self.assertIn(f'`{name}`', self.section(table))

	def test_documented_objects_exist(self):
		names = {r[0] for r in self.con.execute('SELECT name FROM sqlite_master')}
		sections = re.findall(r'^### (\w+)$', self.doc, re.M)
		views = re.findall(r'^\| `(\w+)` \|', self.doc.split('## Views', 1)[1].split('\n## ', 1)[0], re.M)
		objects = re.findall(r'`(\w+_by_\w+|\w+_pending|\w+_(?:insert|update))`', self.doc)
		self.assertTrue(sections and views and objects)
		for name in sections + views + objects:
			with self.subTest(name):
				self.assertIn(name, names)

	def test_every_column_is_documented_in_its_table_section(self):
		tables = [r[0] for r in self.con.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
		for table in tables:
			section = self.section(table)
			for column in [r[1] for r in self.con.execute(f'PRAGMA table_xinfo({table})')]:
				with self.subTest(table=table, column=column):
					self.assertIn(f'`{column}`', section)

	def test_documented_identity_matches(self):
		app_id = self.one('PRAGMA application_id')[0]
		self.assertIn(f'`{app_id}` (`0x{app_id:08X}`', self.doc)
		self.assertIn('`PRAGMA user_version` holds the schema version, `1`', self.doc)

	def test_example_queries_run(self):
		self.load_variants()
		queries = re.findall(r'```sql\n(.*?)```', self.doc, re.S)
		self.assertGreaterEqual(len(queries), 5)
		for query in queries:
			with self.subTest(query.splitlines()[0]):
				self.con.execute(query).fetchall()


if __name__ == '__main__':
	unittest.main()
