# -*- coding: utf-8 -*-
'''
--storage files|sqlite|both: the mode a folder keeps, refusals before any
client exists, the outputs of each mode, the legacy form's files-only
rule, the builders' refusal of sqlite folders, and the same behavior from
every entry point.
'''

# import modules
import os
import re
import unittest

import storage_modes_support

from storage_modes_support import BATCH, DB_FILENAME, MANIFEST_FILE, STATUS_FILE, file_contents, \
	listing, read_json

from telegram_tracker.cli.parsers import COMMAND_PROG, MODULE_PROG, SCRIPT_PROG
from telegram_tracker.storage import manifest as manifests
from telegram_tracker.storage.sqlite.schema import DB_FILENAME as SCHEMA_DB_FILENAME


def snapshot(folder):
	contents = {}
	for name in listing(folder):
		with open(os.path.join(folder, name), 'rb') as fh:
			contents[name] = fh.read()
	return contents


class SelectionTests(storage_modes_support.ModesCase):

	def test_a_new_folder_defaults_to_files(self):
		code, _, err = self.collect()
		self.assertEqual((code, err), (0, ''))
		self.assertNotIn(MANIFEST_FILE, os.listdir(self.out))
		self.assertNotIn(DB_FILENAME, os.listdir(self.out))
		self.assertNotIn('storage', self.status())
		self.assertNoLocks()

	def test_explicit_files_writes_what_the_default_writes(self):
		default = os.path.join(self.tmp, 'default')
		self.assertEqual(self.collect(targets=BATCH, out=default)[0], 1)
		self.assertEqual(self.collect(targets=BATCH, storage='files')[0], 1)
		self.assertEqual(file_contents(self.out), file_contents(default))
		self.assertEqual(listing(self.out), listing(default))

	def test_sqlite_writes_the_database_and_sidecars_only(self):
		code, _, err = self.collect(targets=BATCH, storage='sqlite')
		self.assertEqual(code, 1, err)  # missing_fx and private_fx fail
		self.assertEqual(listing(self.out), [STATUS_FILE, MANIFEST_FILE, DB_FILENAME])
		self.assertEqual(self.rows('SELECT storage, state FROM collection_runs'), [('sqlite', 'partial')])
		self.assertEqual(self.rows('SELECT count(*) FROM histories'), [(3,)])
		self.assertNoLocks()

	def test_both_writes_the_files_of_files_mode_and_the_database(self):
		files = os.path.join(self.tmp, 'files')
		self.collect(targets=BATCH, out=files)
		code, _, err = self.collect(targets=BATCH, storage='both')
		self.assertEqual(code, 1, err)
		self.assertEqual(file_contents(self.out), file_contents(files))
		self.assertEqual(
			sorted(set(listing(self.out)) - set(listing(files))), [MANIFEST_FILE, DB_FILENAME]
		)
		self.assertEqual(self.rows('SELECT storage, state, aggregate_files FROM collection_runs'),
			[('both', 'partial', 'written')])
		self.assertEqual(
			self.rows('SELECT input, files_state FROM run_targets ORDER BY input_index'),
			[('alpha_fx', 'written'), ('beta_fx', 'written'), ('empty_fx', 'written'),
				('missing_fx', None), ('private_fx', None)]
		)
		for target in ('alpha_fx', 'beta_fx'):
			history = read_json(os.path.join(self.out, target, f'{target}_messages.json'))
			channel_id = self.status()['targets'][BATCH.index(target)]['channel_id']
			self.assertEqual(
				[r[0] for r in self.rows(
					'SELECT message_id FROM messages WHERE channel_id = ? ORDER BY message_id DESC',
					channel_id)],
				[m['id'] for m in history['messages']]
			)

	def test_an_omitted_flag_keeps_the_recorded_mode(self):
		for mode in ('sqlite', 'both'):
			out = os.path.join(self.tmp, mode)
			with self.subTest(mode=mode):
				self.assertEqual(self.collect(out=out, storage=mode)[0], 0)
				recorded = self.manifest(out)
				code, stdout, err = self.collect(out=out)
				self.assertEqual((code, err), (0, ''))
				self.assertIn(f'Storage: {mode} (', stdout)
				self.assertEqual(self.manifest(out), recorded)
				self.assertEqual(
					self.rows('SELECT storage FROM collection_runs ORDER BY run_id', out=out), [(mode,), (mode,)])
				self.assertEqual(self.collect(out=out, storage=mode)[0], 0)

	def test_another_explicit_mode_is_refused_before_the_client(self):
		for recorded, requested in (
				('sqlite', 'files'), ('sqlite', 'both'), ('both', 'files'), ('both', 'sqlite')):
			out = os.path.join(self.tmp, f'{recorded}-{requested}')
			with self.subTest(recorded=recorded, requested=requested):
				self.assertEqual(self.collect(out=out, storage=recorded)[0], 0)
				before, calls = snapshot(out), self.factory_calls
				code, _, err = self.collect(out=out, storage=requested)
				self.assertEqual(code, 2)
				self.assertIn(f'was collected with --storage {recorded}; it cannot take --storage {requested}', err)
				self.assertIn(f'Omit --storage to use {recorded}', err)
				self.assertEqual(self.factory_calls, calls)
				self.assertEqual(snapshot(out), before)

	def test_database_modes_need_a_new_or_empty_folder(self):
		self.assertEqual(self.collect()[0], 0)
		before = snapshot(self.out)
		other = os.path.join(self.tmp, 'other')
		os.makedirs(other)
		with open(os.path.join(other, 'notes.txt'), 'w', encoding='utf-8') as fh:
			fh.write('x')
		for out in (self.out, other):
			for mode in ('sqlite', 'both'):
				with self.subTest(out=out, mode=mode):
					code, _, err = self.collect(out=out, storage=mode)
					self.assertEqual(code, 2)
					self.assertIn('needs a new or empty --output', err)
		self.assertEqual(self.factory_calls, 1)
		self.assertEqual(snapshot(self.out), before)
		empty = os.path.join(self.tmp, 'empty')
		os.makedirs(empty)
		self.assertEqual(self.collect(out=empty, storage='both')[0], 0)

	def test_an_invalid_choice_exits_2(self):
		code, _, err = self.collect(storage='json')
		self.assertEqual(code, 2)
		self.assertIn("invalid choice: 'json'", err)
		self.assertEqual(self.factory_calls, 0)

	def test_the_manifest_binds_the_folder_to_its_database(self):
		self.collect(storage='both')
		manifest = self.manifest()
		self.assertEqual(
			{k: manifest[k] for k in ('format', 'storage', 'database')},
			{'format': 1, 'storage': 'both', 'database': DB_FILENAME}
		)
		self.assertEqual(
			self.rows('SELECT dataset_uuid, output_binding FROM schema_meta'),
			[(manifest['dataset_uuid'], os.path.realpath(self.out))]
		)
		self.assertEqual(manifests.DATABASE_FILE, SCHEMA_DB_FILENAME)

	def test_the_sidecar_names_the_storage_and_the_run(self):
		self.collect(storage='both')
		status = self.status()
		self.assertEqual(status['storage'], 'both')
		self.assertEqual(status['database'], {
			'file': DB_FILENAME,
			'schema_version': 1,
			'dataset_uuid': self.manifest()['dataset_uuid'],
			'run_uuid': self.rows('SELECT run_uuid FROM collection_runs')[0][0]
		})
		self.assertEqual(status['publication'], {'pending_targets': [], 'aggregate_files': 'written'})
		self.collect(out=os.path.join(self.tmp, 'sq'), storage='sqlite')
		status = self.status(os.path.join(self.tmp, 'sq'))
		self.assertEqual(status['storage'], 'sqlite')
		self.assertNotIn('publication', status)


class LegacyFormTests(storage_modes_support.ModesCase):

	def legacy(self, out):
		return self.run_cli(['--telegram-channel', 'alpha_fx', '-o', out])

	def test_the_legacy_form_refuses_database_folders(self):
		for mode in ('sqlite', 'both'):
			out = os.path.join(self.tmp, mode)
			with self.subTest(mode=mode):
				self.assertEqual(self.collect(out=out, storage=mode)[0], 0)
				before, calls = snapshot(out), self.factory_calls
				code, _, err = self.legacy(out)
				self.assertEqual(code, 2)
				self.assertIn(f'was collected with --storage {mode}; this form writes files only', err)
				self.assertIn('Use `python main.py collect`', err)
				self.assertEqual(self.factory_calls, calls)
				self.assertEqual(snapshot(out), before)

	def test_the_legacy_form_still_writes_files(self):
		code, _, err = self.legacy(self.out)
		self.assertEqual((code, err), (0, ''))
		self.assertNotIn(MANIFEST_FILE, os.listdir(self.out))
		self.assertEqual(self.legacy(self.out)[0], 0)

	def test_the_legacy_form_has_no_storage_option(self):
		code, _, err = self.run_cli(['--telegram-channel', 'alpha_fx', '--storage', 'both'])
		self.assertEqual(code, 2)
		self.assertIn('unrecognized arguments: --storage both', err)


class EntryPointTests(storage_modes_support.ModesCase):

	def test_every_entry_point_selects_and_refuses_alike(self):
		results = {}
		for prog in (SCRIPT_PROG, COMMAND_PROG, MODULE_PROG):
			out = os.path.join(self.tmp, prog.replace(' ', '_'))
			first = self.collect(out=out, storage='sqlite', prog=prog)
			again = self.collect(out=out, prog=prog)
			refused = self.collect(out=out, storage='both', prog=prog)
			results[prog] = (
				first[0], again[0], refused[0], listing(out),
				refused[2].split('error: ', 1)[1].replace(out, 'OUT'),
				self.rows('SELECT storage, state FROM collection_runs ORDER BY run_id', out=out)
			)
		self.assertEqual(results[SCRIPT_PROG][:3], (0, 0, 2))
		self.assertEqual(results[COMMAND_PROG], results[SCRIPT_PROG])
		self.assertEqual(results[MODULE_PROG], results[SCRIPT_PROG])


class BuilderTests(storage_modes_support.ModesCase):

	def build(self, command, out):
		return self.run_cli([command, '--data-path', out])

	def test_builders_read_a_sqlite_folder(self):
		self.collect(targets=BATCH, storage='sqlite')
		before = snapshot(self.out)
		for command, output in (('build-dataset', 'msgs_dataset.csv'), ('build-network', 'Graph.gexf')):
			with self.subTest(command=command):
				code, out, err = self.build(command, self.out)
				self.assertEqual(code, 0, err)
				self.assertIn('Reading the database of', out)
				self.assertTrue(os.path.isfile(os.path.join(self.out, output)))
		# the inputs are unchanged and no lease is left
		after = snapshot(self.out)
		self.assertEqual({k: v for k, v in after.items() if k in before}, before)
		self.assertNoLocks()

	def test_builders_refuse_a_source_the_folder_lacks(self):
		self.collect(storage='sqlite')
		code, _, err = self.run_cli(['build-dataset', '-d', self.out, '--source', 'files'])
		self.assertEqual(code, 1)
		self.assertIn('was collected with --storage sqlite', err)
		self.assertIn('Use --source sqlite (or auto)', err)
		files_out = os.path.join(self.tmp, 'files')
		self.collect(storage='files', out=files_out)
		code, _, err = self.run_cli(['build-network', '-d', files_out, '--source', 'sqlite'])
		self.assertEqual(code, 1)
		self.assertIn('has no database', err)

	def test_builders_read_a_both_folder(self):
		self.collect(targets=BATCH, storage='both')
		for command in ('build-dataset', 'build-network'):
			with self.subTest(command=command):
				code, _, err = self.build(command, self.out)
				self.assertEqual(code, 0, err)
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'msgs_dataset.csv')))
		self.assertTrue(os.path.isfile(os.path.join(self.out, 'Graph.gexf')))

	def test_builders_refuse_an_unreadable_manifest(self):
		self.collect(storage='both')
		with open(os.path.join(self.out, MANIFEST_FILE), 'w', encoding='utf-8') as fh:
			fh.write('{')
		code, _, err = self.build('build-dataset', self.out)
		self.assertEqual(code, 1)
		self.assertIn(f'{MANIFEST_FILE} cannot be read', err)
		code, _, err = self.collect()
		self.assertEqual(code, 1)
		self.assertIn(f'{MANIFEST_FILE} cannot be read', err)
		self.assertEqual(self.factory_calls, 1)


class HelpTests(storage_modes_support.ModesCase):

	def test_collect_help_explains_the_modes(self):
		code, out, _ = self.run_cli(['collect', '--help'])
		self.assertEqual(code, 0)
		# argparse may break a line after a hyphen
		text = re.sub(r'(\w)- (\w)', r'\1-\2', ' '.join(out.split()))
		for phrase in ('--storage {files,sqlite,both}', 'keeps the mode it was first collected with',
				'<output>/telegram-tracker.sqlite3', 'build-dataset and build-network read it',
				'the builders read the files unless given --source sqlite',
				'need a new or empty --output'):
			self.assertIn(phrase, text)
		self.assertNotIn('--database', text)

	def test_builder_help_names_the_storage_modes(self):
		for command in ('build-dataset', 'build-network'):
			code, out, _ = self.run_cli([command, '--help'])
			self.assertEqual(code, 0)
			text = ' '.join(out.split())
			self.assertIn('the database of a sqlite folder (--source chooses)', text)
			self.assertIn('--source {auto,files,sqlite}', text)

	def test_legacy_help_is_unchanged(self):
		code, out, _ = self.run_cli(['--telegram-channel', 'x', '--help'])
		self.assertEqual(code, 0)
		self.assertNotIn('--storage', out)


if __name__ == '__main__':
	unittest.main()
