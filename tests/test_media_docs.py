# -*- coding: utf-8 -*-
'''
docs/media-ledger.md against the code: the DDL it names is the packaged
one; every table, column, index and trigger is documented and every
documented one exists; the identity, classification, MIME, transition
and reason tables match the code and the DDL; and every example runs:
the ATTACH query on a schema version 1 database, the files-mode Python
join on a files folder, and the ledger queries on a ledger.
'''

# import modules
import contextlib
import json
import os
import pathlib
import re
import sqlite3
import tempfile
import unittest

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from media_support import RUN_A, RUN_B, RUN_C, SELECTION, document_media, photo_media, ref_of
from test_sqlite_schema import SchemaTestCase
from telegram_tracker.media import ledger_schema
from telegram_tracker.media.classify import EXTENSIONS, SUBTYPES
from telegram_tracker.media.identity import IDENTITY_FILE, FolderIdentity, ensure_identity, read_identity_record
from telegram_tracker.media.layout import LEDGER_FILE, MEDIA_FOLDER, PARTIAL_FOLDER, TYPE_FOLDERS, MediaLayout
from telegram_tracker.media.ledger import FAIL_REASONS, SKIP_REASONS, TRANSITIONS, open_ledger
from telegram_tracker.storage.files import to_json_text


ROOT = source_path.ROOT
DDL_PATH = os.path.join(ROOT, 'docs', 'media-ledger.sql')
DOC_PATH = os.path.join(ROOT, 'docs', 'media-ledger.md')
CHANNEL = 1001


def read_text(path):
	with open(path, encoding='utf-8') as reader:
		return reader.read()


DOC = read_text(DOC_PATH)
DDL = read_text(DDL_PATH)


def section(title, level='##'):
	'''
	The text under a heading, up to the next heading of any level.
	'''
	match = re.search(rf'^{level} {re.escape(title)}\n(.*?)(?=^#{{1,3}} |\Z)', DOC, re.S | re.M)
	assert match, f'no section {title}'
	return match.group(1)


def code_blocks(language):
	return re.findall(rf'```{language}\n(.*?)```', DOC, re.S)


def statements(script):
	'''
	The statements of an SQL block, in order.
	'''
	found, current = [], ''
	for line in script.splitlines(keepends=True):
		current += line
		if sqlite3.complete_statement(current):
			found.append(current.strip())
			current = ''
	assert not current.strip(), current
	return found


@contextlib.contextmanager
def working_directory(path):
	before = os.getcwd()
	os.chdir(path)
	try:
		yield
	finally:
		os.chdir(before)


def fill_ledger(ledger):
	'''
	Message 1 (photo) complete, message 2 (document) failed, message 3
	(document 5003) not selected.
	'''
	ledger.start_run(RUN_A, 'collect', selection=SELECTION, profile_name='work',
		profile_id='00000000-0000-4000-8000-000000000000')
	ledger.request(RUN_A, CHANNEL, [(1, ref_of(photo_media(5001))), (2, ref_of(document_media(5002)))])
	attempt = ledger.begin_download(RUN_A, (CHANNEL, 1, 5001))
	path = ledger.complete(RUN_A, (CHANNEL, 1, 5001), sha256='c' * 64, actual_bytes=15000, attempt=attempt)
	ledger.fail(RUN_A, (CHANNEL, 2, 5002), 'request_failed')
	ledger.finish_run(RUN_A, 'partial')
	return path


MESSAGES = [
	{'_': 'Message', 'id': 1, 'message': 'photo', 'media': photo_media(5001)},
	{'_': 'Message', 'id': 2, 'message': 'document', 'media': document_media(5002)},
	{'_': 'Message', 'id': 3, 'message': 'not selected', 'media': document_media(5003)},
	{'_': 'Message', 'id': 4, 'message': 'no media', 'media': None}
]


class ContractTests(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		self.con = sqlite3.connect(':memory:')
		self.addCleanup(self.con.close)
		self.con.executescript(DDL)

	def objects(self, kind):
		return [r[0] for r in self.con.execute(
			"SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,))]

	def test_packaged_ddl_is_the_documented_one(self):
		self.assertEqual(ledger_schema.ledger_sql().replace('\r\n', '\n'), DDL.replace('\r\n', '\n'))
		self.assertIn('[media-ledger.sql](media-ledger.sql)', DOC)

	def test_identity_matches_its_spelling(self):
		value, hex_value, name = re.search(
			r"PRAGMA application_id = (\d+);\s+-- 0x([0-9A-F]{8}), '(\w{4})'", DDL).groups()
		self.assertEqual(name, 'TGTM')
		self.assertEqual(int(value), int(hex_value, 16))
		self.assertEqual(int(value), int.from_bytes(name.encode('ascii'), 'big'))
		self.assertEqual(int(value), ledger_schema.LEDGER_APPLICATION_ID)
		self.assertEqual(self.con.execute('PRAGMA application_id').fetchone(), (int(value),))
		self.assertEqual(self.con.execute('PRAGMA user_version').fetchone(), (ledger_schema.LEDGER_VERSION,))
		requirements = section('Requirements and identity')
		self.assertIn(f"`{value}` (`0x{hex_value}`, 'TGTM')", requirements)
		self.assertIn('`PRAGMA user_version` holds the ledger version, `1`', requirements)

	def test_every_table_and_column_is_documented(self):
		for table in self.objects('table'):
			text = section(table, '###')
			for column in [r[1] for r in self.con.execute(f'PRAGMA table_xinfo({table})')]:
				with self.subTest(table=table, column=column):
					self.assertIn(f'`{column}`', text)

	def test_every_index_and_trigger_is_documented_in_its_table_section(self):
		for kind in ('index', 'trigger'):
			for name, table in self.con.execute(
					"SELECT name, tbl_name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)):
				with self.subTest(name=name):
					text = section(table, '###')
					if name.startswith('items_') and kind == 'trigger' and name != 'items_kept':
						text = section('Item states')
					self.assertIn(f'`{name}`', text)

	def test_documented_objects_exist(self):
		names = set(self.objects('table') + self.objects('index') + self.objects('trigger'))
		tables = re.findall(r'^### (\w+)$', DOC, re.M)
		self.assertEqual(sorted(tables), sorted(self.objects('table')))
		mentioned = re.findall(r'(?:trigger|triggers|Index|Indexes:?|index)\s+`(\w+)`', DOC)
		mentioned += re.findall(r'`(\w+_(?:kept|final|by_\w+|transition|requested))`', DOC)
		self.assertGreaterEqual(len(set(mentioned)), 12)
		for name in set(mentioned):
			with self.subTest(name=name):
				self.assertIn(name, names)

	def test_layout_names(self):
		layout = section('Folder layout')
		for name in (MEDIA_FOLDER, LEDGER_FILE, PARTIAL_FOLDER, *TYPE_FOLDERS.values()):
			with self.subTest(name=name):
				self.assertIn(name, layout)

	def test_mime_table(self):
		rows = re.findall(r'^\| `([^`]+/[^`]+)` \| `(\w+)` \|$', section('Classification'), re.M)
		self.assertEqual(rows, list(EXTENSIONS.items()))

	def test_classification_rules(self):
		rows = re.findall(r'^\| ([^|]+) \| [^|]+ \| ([^|]+) \| `(\w+)` \|$', section('Classification'), re.M)
		rows = [(kind.strip(), subtype) for media, kind, subtype in rows if media != 'Media']
		self.assertEqual([subtype for _, subtype in rows], list(SUBTYPES))
		self.assertEqual(
			[kind for kind, _ in rows],
			['`image`', 'by MIME', '`audio`', '`video`', 'by MIME', '`video`', '`audio`', 'by MIME']
		)

	def test_transition_table(self):
		table = section('Item states').split('| From | To |', 1)[1].split('| State | Reasons |', 1)[0]
		rows = re.findall(r'^\| `(\w+)` \| (.+) \|$', table, re.M)
		self.assertEqual(len(rows), len(TRANSITIONS))
		documented = {
			state: frozenset(re.findall(r'`(\w+)`', targets)) for state, targets in rows if state in TRANSITIONS}
		self.assertEqual(documented, TRANSITIONS)

	def test_reasons_match_the_code_and_the_ddl(self):
		rows = dict(re.findall(r'^\| `(skipped|failed)` \| (.+) \|$', section('Item states'), re.M))
		self.assertEqual(tuple(re.findall(r'`(\w+)`', rows['skipped'])), SKIP_REASONS)
		self.assertEqual(tuple(re.findall(r'`(\w+)`', rows['failed'])), FAIL_REASONS)
		skip_ddl = re.search(r"WHEN 'skipped' THEN .*?IN \(([^)]*)\)", DDL, re.S).group(1)
		fail_ddl = re.search(r"WHEN 'failed' THEN .*?IN \(([^)]*)\)", DDL, re.S).group(1)
		self.assertEqual(tuple(re.findall(r"'(\w+)'", skip_ddl)), SKIP_REASONS)
		self.assertEqual(tuple(re.findall(r"'(\w+)'", fail_ddl)), FAIL_REASONS)


class IdentityRecordDocTests(offline_guard.OfflineTestCase):

	def test_documented_record_keys(self):
		with tempfile.TemporaryDirectory() as tmp:
			ensure_identity(tmp)
			record = read_identity_record(tmp)
		documented = re.search(r'\{"format": 1, "dataset_uuid": [^}]*\}', DOC).group(0)
		self.assertEqual(re.findall(r'"(\w+)":', documented), list(record))
		self.assertIn(f'`<output>/{IDENTITY_FILE}`', DOC)


class AttachQueryTests(SchemaTestCase):
	'''
	The ATTACH example on a schema version 1 database with its ledger.
	'''
	def test_attach_query(self):
		self.add_history(channel_id=CHANNEL)
		for raw in MESSAGES:
			self.add_message(json.loads(to_json_text(raw)), channel_id=CHANNEL)
		output = os.path.dirname(self.path)
		identity = FolderIdentity('sqlite', '00000000-0000-4000-8000-000000000000', 'collection-storage.json')
		with open_ledger(MediaLayout(output), identity, create=True) as ledger:
			path = fill_ledger(ledger)

		[query] = [q for q in code_blocks('sql') if 'ATTACH' in q]
		con = sqlite3.connect(pathlib.Path(self.path).as_uri() + '?mode=ro', uri=True)
		self.addCleanup(con.close)
		with working_directory(output):
			attach, select = statements(query)
			con.execute(attach)
			found = con.execute(select).fetchall()
		self.assertEqual(found, [
			(CHANNEL, 1, 'MessageMediaPhoto', 5001, 'complete', None, path, 15000, 'c' * 64, RUN_A, 'work'),
			(CHANNEL, 2, 'MessageMediaDocument', 5002, 'failed', 'request_failed', None, None, None, RUN_A, 'work'),
			(CHANNEL, 3, 'MessageMediaDocument', 5003, 'not selected', None, None, None, None, None, None)
		])
		self.assertEqual(path, f'{CHANNEL}/images/1-{"c" * 16}.jpg')


class FilesExampleTests(offline_guard.OfflineTestCase):
	'''
	The files-mode Python example on a files folder.
	'''
	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.out = tmp.name
		channel = os.path.join(self.out, 'news')
		os.mkdir(channel)
		with open(os.path.join(channel, 'news_messages.json'), 'w', encoding='utf-8') as writer:
			writer.write(to_json_text({'messages': MESSAGES, 'chats': [], 'users': []}))
		self.channel = channel
		with open_ledger(MediaLayout(self.out), ensure_identity(self.out), create=True) as ledger:
			self.path = fill_ledger(ledger)

	def run_example(self):
		[example] = code_blocks('python')
		namespace = {}
		exec(compile(example, DOC_PATH, 'exec'), namespace)
		return list(namespace['media_of'](self.out, 'news'))

	def expected(self):
		return [(1, 5001, 'complete', self.path), (2, 5002, 'failed', None), (3, 5003, 'not selected', None)]

	def test_with_a_provenance_record(self):
		with open(os.path.join(self.channel, 'news_provenance.json'), 'w', encoding='utf-8') as writer:
			writer.write(to_json_text({'format': 1, 'channel_id': CHANNEL}))
		self.assertEqual(self.run_example(), self.expected())

	def test_with_the_channel_snapshot(self):
		with open(os.path.join(self.channel, 'news.json'), 'w', encoding='utf-8') as writer:
			writer.write(to_json_text({'full_chat': {'id': CHANNEL}}))
		self.assertEqual(self.run_example(), self.expected())


class LedgerQueryTests(offline_guard.OfflineTestCase):
	'''
	The other example queries on a ledger.
	'''
	def test_ledger_queries(self):
		with tempfile.TemporaryDirectory() as out:
			with open_ledger(MediaLayout(out), ensure_identity(out), create=True) as ledger:
				path = fill_ledger(ledger)
				# run B stores message 5 by reuse, then dies downloading message 2
				ledger.start_run(RUN_B, 'download-media', selection=SELECTION, started_at='2024-03-03T00:00:00')
				ledger.request(RUN_B, CHANNEL, [(5, ref_of(photo_media(5001))), (2, ref_of(document_media(5002)))])
				ledger.complete(RUN_B, (CHANNEL, 5, 5001), sha256='c' * 64, actual_bytes=15000)
				ledger.begin_download(RUN_B, (CHANNEL, 2, 5002))
				ledger.start_run(RUN_C, 'download-media', selection=SELECTION)
				stop_reason = ledger.run(RUN_B)['stop_reason']
				queries = [q for q in code_blocks('sql') if 'ATTACH' not in q]
				self.assertEqual(len(queries), 3)
				shared, by_state, interrupted = [ledger.con.execute(q).fetchall() for q in queries]
		self.assertEqual(shared, [(path, 15000, 2)])
		self.assertEqual(by_state, [
			(RUN_A, 'complete', None, 1), (RUN_B, 'complete', None, 1), (RUN_B, 'failed', 'interrupted', 1)])
		self.assertEqual(interrupted, [(RUN_B, '2024-03-03T00:00:00', stop_reason, 1)])


if __name__ == '__main__':
	unittest.main()
