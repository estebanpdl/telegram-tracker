# -*- coding: utf-8 -*-
'''
A window run checks saved files/SQL before credentials and asks before replacement.
--yes skips the question; without a terminal, exit 2 prevents replacement.
Declined/refused runs write nothing; the legacy form only warns. Synthetic data only.
'''

# import modules
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

from unittest import mock

import offline_guard
import source_path  # noqa: F401  (imports telegram_tracker from src)

from fakes import load_world_raw
from storage_modes_support import BATCH, read_json
from test_entry_points import ENTRY_PROBE, ROOT, SRC, TESTS

from telegram_tracker.cli import collect as collect_cli
from telegram_tracker.storage.destination import select_destination
from window_confirm_support import (
	MODES, PROMPT, REFUSED, DECLINED, Terminal, Unasked, Interrupting,
	ClosedInput, snapshot, home_listing, ConfirmCase,
)


class PromptTests(ConfirmCase):
	'''
	The question at a terminal, in each storage mode.
	'''

	def test_yes_replaces_the_saved_history(self):
		self.assertEqual(home_listing(), [])
		for storage in MODES:
			for answer in ('y\n', 'YES\n', ' y \n'):
				with self.subTest(storage=storage, answer=answer):
					out = self.folder(f'{storage}-{answer.strip()}')
					self.save(storage, out)
					# the state check sees what a run leaves in the home
					self.assertNotEqual(home_listing(), [])
					full, calls, leases = self.history_size(storage, out), self.factory_calls, self.leases

					code, stdout, err = self.window(Terminal(answer), out)
					self.assertEqual(code, 0, err)
					self.assertIn(PROMPT, stdout)
					self.assertIn(
						'> Warning: this filtered run replaces the saved messages of 1 channel(s) '
						f'in {out}: alpha_fx. Use a fresh --output to keep them.', err
					)
					self.assertEqual((self.factory_calls, self.leases), (calls + 1, leases + 1))
					self.assertGreater(full, 1)
					self.assertEqual(self.history_size(storage, out), 1)
					if storage == 'both':
						self.assertEqual(self.history_size('sqlite', out), 1)

	def test_any_other_answer_collects_nothing(self):
		for storage in MODES:
			out = self.folder(storage)
			self.save(storage, out)
			for answer in ('n\n', 'no\n', '\n', 'yy\n', 'oui\n', ''):
				with self.subTest(storage=storage, answer=answer):
					before = self.state(out)
					code, stdout, err = self.window(Terminal(answer), out)
					self.assertEqual(code, 1, err)
					self.assertIn(PROMPT, stdout)
					self.assertIn('alpha_fx', err)
					self.assertIn(DECLINED, err)
					self.assertStoppedBeforeConnecting(out, before)

	def test_ctrl_c_at_the_question_collects_nothing(self):
		out = self.folder('sqlite')
		self.save('sqlite', out)
		before = self.state(out)
		try:
			code, _, err = self.window(Interrupting(), out)
		except KeyboardInterrupt:
			self.fail('Ctrl+C at the question was not handled')
		self.assertEqual(code, 130)
		self.assertIn('> Interrupted.', err)
		self.assertStoppedBeforeConnecting(out, before)


class NonInteractiveTests(ConfirmCase):
	'''
	Without a terminal to answer, --yes is required.
	'''

	def test_input_that_is_not_a_terminal_is_refused(self):
		for storage in MODES:
			out = self.folder(storage)
			self.save(storage, out)
			for label, stdin in (('piped yes', io.StringIO('y\n')), ('empty', io.StringIO('')),
					('none', None), ('closed', ClosedInput())):
				with self.subTest(storage=storage, stdin=label):
					before = self.state(out)
					code, stdout, err = self.window(stdin, out)
					self.assertEqual(code, 2, err)
					self.assertNotIn(PROMPT, stdout)
					self.assertIn(': alpha_fx. Use a fresh --output', err)
					self.assertIn('> Not replaced: input is not a terminal', err)
					self.assertIn(REFUSED, err)
					self.assertStoppedBeforeConnecting(out, before)

	def test_yes_flag_replaces_without_asking(self):
		for storage in MODES:
			for label, stdin in (('terminal', Unasked()), ('not a terminal', io.StringIO(''))):
				with self.subTest(storage=storage, stdin=label):
					out = self.folder(f'{storage}-{label}')
					self.save(storage, out)
					code, stdout, err = self.window(stdin, out, '--yes')
					self.assertEqual(code, 0, err)
					self.assertNotIn(PROMPT, stdout)
					self.assertIn('replaces the saved messages of 1 channel(s)', err)
					self.assertEqual(self.history_size(storage, out), 1)

	def test_runs_that_replace_nothing_never_ask(self):
		for storage in MODES:
			with self.subTest(storage=storage, case='new folder'):
				out = self.folder(f'new-{storage}')
				code, stdout, err = self.window(Unasked(), out, '--storage', storage)
				self.assertEqual(code, 0, err)
				self.assertNotIn('Warning', err)
				self.assertNotIn(PROMPT, stdout)

			out = self.folder(storage)
			self.save(storage, out)
			with self.subTest(storage=storage, case='another channel'):
				code, stdout, err = self.window(io.StringIO(''), out, targets=('beta_fx',))
				self.assertEqual(code, 0, err)
				self.assertNotIn('Warning', err)
				self.assertEqual(self.history_size(storage, out, 'beta_fx', 102), 1)

	def test_runs_without_a_window_never_ask(self):
		for storage in MODES:
			out = self.folder(storage)
			self.save(storage, out)
			for extra in ([], ['--update'], ['--limit-download-to-channel-metadata']):
				with self.subTest(storage=storage, extra=extra):
					with mock.patch('sys.stdin', io.StringIO('')):
						code, stdout, err = self.collect(*extra, out=out)
					self.assertEqual(code, 0, err)
					self.assertNotIn('Warning', err)
					self.assertNotIn(PROMPT, stdout)


class BatchTests(ConfirmCase):

	def test_the_warning_counts_and_names_the_first_ten(self):
		out = self.folder('files')
		names = [f'c{i:02d}_fx' for i in range(12)]
		for name in names:
			os.makedirs(os.path.join(out, name))
			with open(os.path.join(out, name, f'{name}_messages.json'), 'w', encoding='utf-8') as fh:
				fh.write('{"count": 0, "messages": []}')
		before = self.state(out)

		code, _, err = self.window(io.StringIO(''), out, targets=names + ['new_fx'])
		self.assertEqual(code, 2, err)
		self.assertIn(f'saved messages of 12 channel(s) in {out}: ', err)
		self.assertIn(', '.join(names[:10]) + ', and 2 more.', err)
		self.assertNotIn('new_fx', err)
		self.assertStoppedBeforeConnecting(out, before)

	def test_only_channels_with_a_saved_history_are_named(self):
		# missing_fx and private_fx fail in the full run and have no history;
		# empty_fx has an empty one, which a window run replaces too
		for storage in MODES:
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, targets=BATCH)
				before = self.state(out)
				code, _, err = self.window(Terminal('n\n'), out, targets=BATCH + ['new_fx'])
				self.assertEqual(code, 1, err)
				self.assertIn(
					f'replaces the saved messages of 3 channel(s) in {out}: alpha_fx, beta_fx, empty_fx. ',
					err
				)
				self.assertStoppedBeforeConnecting(out, before)


class DetectionTests(ConfirmCase):
	'''
	Destination.saved_histories: per mode, read only.
	'''

	def found(self, out, targets):
		destination = select_destination(out)
		destination.check()
		return destination.saved_histories(targets)

	def alias_world(self):
		raw = load_world_raw()
		raw['entities']['alpha_alias'] = {'chat': 101}
		return raw

	def test_the_database_matches_earlier_inputs_and_stored_usernames(self):
		out = self.folder('sqlite')
		self.save('sqlite', out, targets=('alpha_alias',), client=self.make_client(self.alias_world()))
		self.assertEqual(self.rows('SELECT DISTINCT input FROM run_targets', out=out), [('alpha_alias',)])
		self.assertEqual(
			self.rows("SELECT username FROM peers WHERE peer_kind = 'channel' AND peer_id = 101", out=out),
			[('alpha_fx',)]
		)
		# an earlier input (any case), the stored username (any case); not an
		# unrelated name, nor another channel
		self.assertEqual(
			self.found(out, ['ALPHA_ALIAS', 'beta_fx', 'Alpha_FX', 'unknown_fx']),
			['ALPHA_ALIAS', 'Alpha_FX']
		)

	def test_the_database_needs_a_stored_history(self):
		out = self.folder('sqlite')
		self.save('sqlite', out, targets=('alpha_fx',))
		code, _, err = self.collect('--limit-download-to-channel-metadata', targets=('beta_fx',), out=out)
		self.assertEqual(code, 0, err)
		code, _, _ = self.collect(targets=('missing_fx',), out=out)
		self.assertEqual(code, 1)
		# beta_fx is resolved and stored as a peer, but has no history
		self.assertEqual(
			self.rows('SELECT count(*) FROM run_targets WHERE input = ? AND channel_id = 102', 'beta_fx', out=out),
			[(1,)]
		)
		self.assertEqual(self.found(out, ['beta_fx', 'missing_fx', 'alpha_fx']), ['alpha_fx'])

	def test_both_matches_either_copy(self):
		out = self.folder('both')
		self.save('both', out, targets=('alpha_fx',))
		os.remove(os.path.join(out, 'alpha_fx', 'alpha_fx_messages.json'))
		os.makedirs(os.path.join(out, 'beta_fx'))
		with open(os.path.join(out, 'beta_fx', 'beta_fx_messages.json'), 'w', encoding='utf-8') as fh:
			fh.write('{"count": 0, "messages": []}')
		self.assertEqual(self.rows('SELECT channel_id FROM histories', out=out), [(101,)])
		self.assertEqual(self.found(out, ['beta_fx', 'alpha_fx', 'empty_fx']), ['beta_fx', 'alpha_fx'])

	def test_files_matches_the_messages_file_only(self):
		out = self.folder('files')
		self.save('files', out, targets=('alpha_fx',))
		self.assertEqual(self.found(out, ['alpha_fx', 'beta_fx']), ['alpha_fx'])
		os.remove(os.path.join(out, 'alpha_fx', 'alpha_fx_messages.json'))
		self.assertEqual(self.found(out, ['alpha_fx']), [])

	def test_the_lookup_is_read_only(self):
		out = self.folder('sqlite')
		self.save('sqlite', out, targets=('alpha_fx',))
		before = snapshot(out)
		self.assertEqual(self.found(out, ['alpha_fx']), ['alpha_fx'])
		self.assertEqual(snapshot(out), before)

		# a writer holding its write lock does not stop the lookup, and the
		# lookup cannot write
		writer = sqlite3.connect(self.db(out), isolation_level=None)
		self.addCleanup(writer.close)
		writer.execute('BEGIN IMMEDIATE')
		self.assertEqual(self.found(out, ['alpha_fx']), ['alpha_fx'])
		writer.execute('ROLLBACK')

		from telegram_tracker.storage.sqlite.connection import open_database

		with mock.patch('telegram_tracker.storage.sqlite.connection.open_database',
				wraps=open_database) as opened:
			self.found(out, ['alpha_fx'])
		self.assertTrue(opened.call_args_list)
		self.assertTrue(all(call.kwargs.get('readonly') for call in opened.call_args_list))
		self.assertEqual(snapshot(out), before)

	def test_a_new_database_folder_is_not_opened(self):
		out = self.folder('new')
		destination = select_destination(out, 'sqlite')
		self.assertEqual(destination.saved_histories(['alpha_fx']), [])
		self.assertFalse(os.path.exists(out))


class RenamedChannelTests(ConfirmCase):
	'''
	A channel given under a name the check before connecting cannot know
	(`alpha_renamed`: never an input, not the stored username) is found
	after resolution: without --yes its stored history is kept and the
	target fails; the next run asks.
	'''

	def setUp(self):
		super().setUp()
		raw = load_world_raw()
		raw['entities']['alpha_renamed'] = {'chat': 101}
		self.raw = raw

	def run_window(self, stdin, out, *extra, targets=('alpha_renamed',)):
		return self.window(stdin, out, *extra, targets=targets, client=self.make_client(self.raw))

	def stored_history(self, out):
		return (
			self.rows('SELECT run_id, mode, message_count FROM histories WHERE channel_id = 101', out=out),
			self.rows('SELECT message_id FROM messages WHERE channel_id = 101 ORDER BY 1', out=out)
		)

	def test_an_unconfirmed_stored_history_fails_the_target_and_is_kept(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, client=self.make_client(self.raw))
				history, files = self.stored_history(out), snapshot(os.path.join(out, 'alpha_fx'))
				self.assertGreater(len(history[1]), 1)

				code, stdout, err = self.run_window(io.StringIO(''), out)
				self.assertEqual(code, 1, err)
				self.assertNotIn('Warning', err)
				self.assertNotIn(PROMPT, stdout)
				self.assertIn('> alpha_renamed: a saved history of its channel was found under another name', err)
				self.assertIn('run again to be asked first, or pass --yes', err)
				self.assertEqual(self.stored_history(out), history)
				self.assertEqual(snapshot(os.path.join(out, 'alpha_fx')), files)
				self.assertFalse(os.path.exists(os.path.join(out, 'alpha_renamed')))
				entry = self.status(out)['targets'][0]
				self.assertEqual((entry['status'], entry['channel_id']), ('failed', 101))
				self.assertIn('found under another name', entry['reason'])
				self.assertEqual(
					self.rows('SELECT status, channel_id FROM run_targets WHERE input = ? ORDER BY run_id DESC LIMIT 1',
						'alpha_renamed', out=out),
					[('failed', 101)]
				)

	def test_other_targets_of_the_batch_continue(self):
		out = self.folder('sqlite')
		self.save('sqlite', out, client=self.make_client(self.raw))
		history = self.stored_history(out)
		code, _, err = self.run_window(io.StringIO(''), out, targets=('alpha_renamed', 'beta_fx'))
		self.assertEqual(code, 1, err)
		statuses = [t['status'] for t in self.status(out)['targets']]
		self.assertEqual(statuses, ['failed', 'succeeded'])
		self.assertEqual(self.history_size('sqlite', out, 'beta_fx', 102), 1)
		self.assertEqual(self.stored_history(out), history)

	def test_the_next_run_asks_and_yes_replaces(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, client=self.make_client(self.raw))
				history = self.stored_history(out)
				self.assertEqual(self.run_window(io.StringIO(''), out)[0], 1)

				# the failed run recorded the channel under the new name
				destination = select_destination(out)
				destination.check()
				self.assertEqual(destination.saved_histories(['alpha_renamed']), ['alpha_renamed'])

				code, stdout, err = self.run_window(Terminal('n\n'), out)
				self.assertEqual(code, 1, err)
				self.assertIn(PROMPT, stdout)
				self.assertIn(': alpha_renamed. Use a fresh --output', err)
				self.assertEqual(self.stored_history(out), history)

				code, _, err = self.run_window(Terminal('y\n'), out)
				self.assertEqual(code, 0, err)
				self.assertEqual(self.history_size('sqlite', out), 1)

	def test_yes_replaces_without_the_check(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, client=self.make_client(self.raw))
				code, stdout, err = self.run_window(io.StringIO(''), out, '--yes')
				self.assertEqual(code, 0, err)
				self.assertNotIn('found under another name', err)
				self.assertEqual(self.history_size('sqlite', out), 1)
				self.assertEqual(self.status(out)['targets'][0]['status'], 'succeeded')

	def test_files_keep_a_renamed_channel_apart(self):
		# file histories are named after the target, so nothing is replaced
		out = self.folder('files')
		self.save('files', out, client=self.make_client(self.raw))
		files = snapshot(os.path.join(out, 'alpha_fx'))
		code, _, err = self.run_window(io.StringIO(''), out)
		self.assertEqual(code, 0, err)
		self.assertEqual(snapshot(os.path.join(out, 'alpha_fx')), files)
		self.assertEqual(self.history_size('files', out, 'alpha_renamed'), 1)


class LegacyFormTests(ConfirmCase):
	'''
	The legacy form cannot take --yes: it warns and goes on, as before.
	'''

	def test_the_legacy_form_warns_and_replaces(self):
		for label, stdin in (('terminal', Unasked()), ('not a terminal', io.StringIO(''))):
			with self.subTest(stdin=label):
				out = self.folder(label)
				self.save(None, out)
				path = os.path.join(out, 'alpha_fx', 'alpha_fx_messages.json')
				full = [m['id'] for m in read_json(path)['messages']]
				self.assertLess(min(full), 5)
				with mock.patch('sys.stdin', stdin):
					code, stdout, err = self.run_cli(['--telegram-channel', 'alpha_fx', '--min-id', '5', '-o', out])
				self.assertEqual(code, 0, err)
				self.assertIn('replaces the saved messages of 1 channel(s)', err)
				self.assertNotIn(PROMPT, stdout)
				self.assertNotIn('Not replaced', err)
				self.assertEqual([m['id'] for m in read_json(path)['messages']], [i for i in full if i > 5])

	def test_the_legacy_form_does_not_warn_without_a_selection(self):
		out = self.folder('files')
		self.save(None, out)
		for extra in (['--limit-download-to-channel-metadata', '--min-id', '3'], ['--min-id', '0']):
			with self.subTest(extra=extra), mock.patch('sys.stdin', Unasked()):
				code, _, err = self.run_cli(['--telegram-channel', 'alpha_fx', '-o', out] + extra)
				self.assertEqual((code, err), (0, ''))

	def test_the_legacy_form_rejects_yes(self):
		code, _, err = self.run_cli(['--telegram-channel', 'alpha_fx', '--min-id', '5', '--yes'])
		self.assertEqual(code, 2)
		self.assertIn('unrecognized arguments: --yes', err)


class FreshProcessTests(ConfirmCase):
	'''
	A real interpreter with standard input that is not a terminal stops
	before reading credentials.
	'''

	def run_main(self, argv, stdin):
		code = ENTRY_PROBE.format(
			tests=TESTS, src=SRC, module=False, script=os.path.join(ROOT, 'main.py'),
			argv0=os.path.join(ROOT, 'main.py'), argv=list(argv)
		)
		with tempfile.TemporaryDirectory() as cwd:
			completed = subprocess.run(
				[sys.executable, '-I', '-c', textwrap.dedent(code)], cwd=cwd, input=stdin,
				stdin=None if stdin is not None else subprocess.DEVNULL,
				capture_output=True, text=True, encoding='utf-8', timeout=120
			)
			self.assertEqual(os.listdir(cwd), [])
		# a question without a newline would share the report's line
		reports = [l.partition('REPORT ')[2] for l in completed.stdout.splitlines() if 'REPORT ' in l]
		self.assertTrue(reports, completed.stderr[-2000:])
		return json.loads(reports[-1]), completed.stdout, completed.stderr

	def test_a_real_process_without_a_terminal_is_refused(self):
		for storage, stdin in (('files', None), ('sqlite', 'y\n'), ('both', '')):
			with self.subTest(storage=storage, stdin=stdin):
				out = self.folder(storage)
				self.save(storage, out)
				before, home = snapshot(out), home_listing()
				report, stdout, err = self.run_main(
					['collect', '--telegram-channel', 'alpha_fx', '--since', '2026-01-01', '-o', out], stdin
				)
				self.assertEqual((report['code'], report['violations']), (2, []), err)
				self.assertIn(REFUSED, err)
				self.assertNotIn('Missing app credentials', err)
				self.assertNotIn(PROMPT, stdout)
				self.assertEqual(snapshot(out), before)
				self.assertEqual(home_listing(), home)


class InteractiveTests(offline_guard.OfflineTestCase):

	def test_interactive_reads_the_terminal_state_at_call_time(self):
		for stdin, expected in ((Terminal(''), True), (io.StringIO(''), False), (None, False),
				(ClosedInput(), False)):
			with self.subTest(stdin=type(stdin).__name__), mock.patch('sys.stdin', stdin):
				self.assertIs(collect_cli.interactive(), expected)

	@unittest.skipUnless(os.name == 'nt', 'Windows consoles')
	def test_a_console_is_interactive_and_the_null_device_is_not(self):
		'''
		NUL reports isatty() on Windows; only a console answers. The first
		child gets a new console without a window as its standard input.
		'''
		with tempfile.TemporaryDirectory() as work:
			result = os.path.join(work, 'interactive.json')
			code = textwrap.dedent(f'''
				import json, sys
				sys.path[:0] = [{TESTS!r}, {SRC!r}]
				import offline_guard
				from telegram_tracker.cli import collect
				with open({result!r}, 'w') as fh:
					json.dump([sys.stdin.isatty(), collect.interactive(), offline_guard.VIOLATIONS], fh)
			''')
			cases = (
				('console', {'creationflags': subprocess.CREATE_NO_WINDOW}, [True, True, []]),
				('NUL', {'stdin': subprocess.DEVNULL}, [True, False, []]),
				('pipe', {'input': ''}, [False, False, []])
			)
			for label, kwargs, expected in cases:
				with self.subTest(stdin=label):
					if os.path.exists(result):
						os.remove(result)
					subprocess.run([sys.executable, '-I', '-c', code], timeout=120, **kwargs)
					self.assertEqual(read_json(result), expected)


if __name__ == '__main__':
	unittest.main()
