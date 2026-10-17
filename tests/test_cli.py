# -*- coding: utf-8 -*-
'''
Unified CLI (cli package): canonical subcommands, legacy forms, validation
before credentials, history-window options, and offline help.

In-process runs inject the fake client; argument errors run with no
client factory, so the offline guard fails any test that reads
config/config.ini or creates a client.
'''

# import modules
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from datetime import datetime, timezone
from unittest import mock

import offline_guard
import virtual_time

from builder_data import ROOT, TESTS, collected_data, run_script
from fakes import FakeTelegramClient, load_expected

import cli
import cli.collect
import cli.builders
import cli.parsers
import collector
import collector.outcomes
import datasets
import network


EXPECTED = load_expected('collection.json')


class CliTestCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.out = os.path.join(self.tmp, 'data')
		self.factory_calls = 0
		virtual = mock.patch.object(cli.collect, 'asyncio_run', virtual_time.run)
		virtual.start()
		self.addCleanup(virtual.stop)
		self.client = FakeTelegramClient(page_size=EXPECTED['page_size'])

	def factory(self):
		self.factory_calls += 1
		return self.client

	def run_cli(self, argv, entry=None, **kwargs):
		stdout, stderr = io.StringIO(), io.StringIO()
		if entry is None:
			kwargs.setdefault('client_factory', self.factory)
			entry = lambda a: cli.main(a, **kwargs)
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			try:
				code = entry(argv)
			except SystemExit as exc:
				code = exc.code
		return code, stdout.getvalue(), stderr.getvalue()

	def assert_rejected(self, argv, message=None):
		code, _, err = self.run_cli(argv, client_factory=None)
		self.assertEqual(code, 2, err)
		if message:
			self.assertIn(message, err)
		self.assertEqual(self.factory_calls, 0)
		return err

	def write_batch(self, *lines):
		path = os.path.join(self.tmp, 'channels.txt')
		with open(path, 'w', encoding='utf-8') as fh:
			fh.write(''.join(f'{line}\n' for line in lines))
		return path

	def history_requests(self):
		return [r for r in self.client.requests if r['type'] == 'GetHistoryRequest']


class HelpTests(CliTestCase):

	def test_top_level_help_lists_commands_and_legacy_forms(self):
		for argv in (['--help'], ['-h']):
			code, out, _ = self.run_cli(argv, client_factory=None)
			self.assertEqual(code, 0)
			for text in ('collect', 'build-dataset', 'build-network', 'legacy forms',
					'--telegram-channel', '--batch-file', '--min-id', '--output',
					'build-datasets.py', 'channels-to-network.py'):
				self.assertIn(text, out)

	def test_no_arguments_prints_overview_and_exits_2(self):
		code, out, err = self.run_cli([], client_factory=None)
		self.assertEqual((code, out), (2, ''))
		self.assertIn('<command>', err)

	def test_subcommand_help_is_offline(self):
		expected = {
			'collect': ('--telegram-channel', '--since', '--until', '--max-id', '--max-messages',
				'--concurrency', '--request-interval', '--max-flood-wait', '--max-retries',
				'excludes 1 October', '--update', 'not a safe rate guaranteed'),
			'build-dataset': ('--data-path', '-d'),
			'build-network': ('--data-path', '-d'),
		}
		for command, texts in expected.items():
			with self.subTest(command=command):
				code, out, _ = self.run_cli([command, '--help'], client_factory=None)
				self.assertEqual(code, 0)
				for text in texts:
					self.assertIn(text, out)
		self.assertEqual(self.factory_calls, 0)

	def test_legacy_collection_help_shows_only_legacy_options(self):
		code, out, _ = self.run_cli(['--telegram-channel', 'x', '--help'], client_factory=None)
		self.assertEqual(code, 0)
		self.assertIn('--min-id', out)
		self.assertNotIn('--since', out)

	def help_sections(self, argv):
		'''
		Option names listed under each help section heading. A section with
		a description lists its options after the first blank line.
		'''
		code, out, _ = self.run_cli(argv, client_factory=None)
		self.assertEqual(code, 0)
		blocks, heading = {}, None
		for line in out.splitlines():
			if line and not line[0].isspace() and line.endswith(':'):
				heading = line[:-1]
				blocks[heading] = [[]]
			elif heading and not line.strip():
				blocks[heading].append([])
			elif heading:
				blocks[heading][-1].append(line)

		sections = {}
		for heading, parts in blocks.items():
			options = [part for part in parts if part][-1]
			sections[heading] = [
				line.split()[0].rstrip(',') for line in options if line.startswith('  -')
			]
		return sections

	def test_min_id_is_grouped_with_the_history_window(self):
		sections = self.help_sections(['collect', '--help'])
		self.assertEqual(
			sections['history window'],
			['--min-id', '--since', '--until', '--max-id', '--max-messages']
		)
		self.assertNotIn('--min-id', sections['options'])

		sections = self.help_sections(['--telegram-channel', 'x', '--help'])
		self.assertEqual(sections['history window'], ['--min-id'])
		self.assertNotIn('--min-id', sections['options'])
		self.assertIn('--output', sections['options'])


class HelpWithoutDependenciesTests(offline_guard.OfflineTestCase):
	'''
	Help never imports service dependencies: these runs block Telethon,
	pandas, NetworkX, matplotlib, python-louvain, tqdm, and openpyxl.
	'''

	PROBE = '''
	import importlib.abc, runpy, sys
	sys.path[:0] = [{tests!r}, {root!r}]
	BLOCKED = ('telethon', 'pandas', 'networkx', 'matplotlib', 'community', 'tqdm', 'openpyxl')
	class Block(importlib.abc.MetaPathFinder):
		def find_spec(self, name, path=None, target=None):
			if name.split('.')[0] in BLOCKED:
				raise ModuleNotFoundError(f'No module named {{name!r}}', name=name)
	sys.meta_path.insert(0, Block())
	sys.argv = [{script!r}] + {argv!r}
	try:
		runpy.run_path({script!r}, run_name='__main__')
	except SystemExit as exc:
		print('EXIT', exc.code)
	'''

	def run_blocked(self, script, argv, cwd):
		code = textwrap.dedent(self.PROBE.format(
			tests=TESTS, root=ROOT, script=os.path.join(ROOT, script), argv=argv
		))
		completed = subprocess.run(
			[sys.executable, '-I', '-c', code],
			cwd=cwd, capture_output=True, text=True, encoding='utf-8', timeout=120
		)
		return completed.stdout, completed.stderr

	def test_help_works_without_service_dependencies(self):
		with tempfile.TemporaryDirectory() as cwd:
			for script, argv in (
					('main.py', ['--help']),
					('main.py', ['collect', '--help']),
					('main.py', ['build-dataset', '--help']),
					('main.py', ['build-network', '--help']),
					('main.py', ['--telegram-channel', 'x', '--help']),
					('build-datasets.py', ['--help']),
					('channels-to-network.py', ['--help'])):
				with self.subTest(script=script, argv=argv):
					out, err = self.run_blocked(script, argv, cwd)
					self.assertIn('EXIT 0', out, err)
					self.assertIn('usage:', out)

	def test_service_reports_a_missing_dependency(self):
		with tempfile.TemporaryDirectory() as cwd:
			out, err = self.run_blocked('main.py', ['build-network', '-d', cwd], cwd)
		self.assertIn('EXIT 1', out, err)
		self.assertIn("> build-network: missing dependency 'networkx'", err)
		self.assertNotIn('Traceback', err)


class DispatchEquivalenceTests(CliTestCase):
	'''
	Canonical and legacy forms reach the same handler with the same inputs.
	'''

	def record_collection(self):
		calls = []

		async def fake_run_collection(targets, output_folder, **kwargs):
			calls.append({'targets': targets, 'output': output_folder, **{
				k: kwargs[k] for k in ('metadata_only', 'window')
			}})
			return collector.outcomes.CollectionSummary(requested=targets, output_folder=output_folder, succeeded=list(targets))

		patcher = mock.patch.object(collector, 'run_collection', fake_run_collection)
		patcher.start()
		self.addCleanup(patcher.stop)
		return calls

	def test_collection_forms(self):
		calls = self.record_collection()
		batch = self.write_batch('alpha_fx', 'beta_fx')
		pairs = [
			(['--telegram-channel', 'channel_name'], ['collect', '--telegram-channel', 'channel_name']),
			(['--batch-file', batch], ['collect', '--batch-file', batch]),
			(['--telegram-channel', 'channelname', '--limit-download-to-channel-metadata'],
				['collect', '--telegram-channel', 'channelname', '--limit-download-to-channel-metadata']),
			(['--batch-file', batch, '--limit-download-to-channel-metadata'],
				['collect', '--batch-file', batch, '--limit-download-to-channel-metadata']),
			(['--telegram-channel', 'channelname', '--min-id', '12345'],
				['collect', '--telegram-channel', 'channelname', '--min-id', '12345']),
			(['--telegram-channel', 'channelname', '--output', './path/to/chosen/directory'],
				['collect', '--telegram-channel', 'channelname', '-o', './path/to/chosen/directory/']),
			([f'--batch-file={batch}', '--output=out_dir', '--min-id=7'],
				['collect', f'--batch-file={batch}', '--output=out_dir', '--min-id=7']),
		]
		for legacy, canonical in pairs:
			with self.subTest(legacy=legacy):
				del calls[:]
				for argv in (legacy, canonical):
					code, _, err = self.run_cli(argv)
					self.assertEqual((code, err), (0, ''))
				self.assertEqual(len(calls), 2)
				self.assertEqual(calls[0], calls[1])

		self.assertEqual(calls[0]['targets'], ['alpha_fx', 'beta_fx'])
		self.assertEqual(calls[0]['output'], 'out_dir')
		self.assertEqual(calls[0]['window'], collector.HistoryWindow(min_id=7))

	def test_default_output_folder(self):
		calls = self.record_collection()
		self.run_cli(['collect', '--telegram-channel', 'a_fx'])
		self.assertEqual(calls[0]['output'], './output/data')

	def test_builder_forms(self):
		seen = []
		for module, name, command, prog in (
				(datasets, 'build_dataset', 'build-dataset', 'build-datasets.py'),
				(network, 'build_network', 'build-network', 'channels-to-network.py')):
			def record(data_path, name=name):
				seen.append((name, data_path))
				raise cli.builders.DataInputError('recorded')

			with mock.patch.object(module, name, record):
				for argv in ([], ['--data-path', 'some/dir/'], ['-d', 'some/dir'], ['--data-path=some/dir']):
					with self.subTest(command=command, argv=argv):
						del seen[:]
						self.run_cli([command, *argv])
						self.run_cli(argv, entry=lambda a: cli.builder_main(command, a, prog=prog))
						self.assertEqual(len(seen), 2)
						self.assertEqual(seen[0], seen[1])
						self.assertEqual(seen[0][1], 'some/dir' if argv else './output/data')


class CanonicalRunTests(CliTestCase):

	def test_collect_end_to_end_then_build(self):
		batch = self.write_batch('alpha_fx', 'beta_fx')
		code, out, err = self.run_cli(['collect', '--batch-file', batch, '-o', self.out])
		self.assertEqual((code, err), (0, ''))
		self.assertIn('> Collected 2 of 2 targets into', out)
		self.assertEqual((self.client.connect_count, self.client.disconnect_count), (1, 1))

		code, out, _ = self.run_cli(['build-dataset', '-d', self.out])
		self.assertEqual(code, 0)
		self.assertIn('> Wrote 11 rows to', out)
		code, out, _ = self.run_cli(['build-network', '--data-path', self.out])
		self.assertEqual(code, 0)
		self.assertIn('> Wrote 5 nodes and 4 edges to', out)

	def test_legacy_and_canonical_min_id_requests_match(self):
		spec = EXPECTED['scenarios']['min_id']
		logs = []
		for argv in (['--telegram-channel', 'alpha_fx', '--min-id', str(spec['min_id'])],
				['collect', '--telegram-channel', 'alpha_fx', '--min-id', str(spec['min_id'])]):
			self.client = FakeTelegramClient(page_size=EXPECTED['page_size'])
			code, _, _ = self.run_cli(argv + ['-o', self.out])
			self.assertEqual(code, 0)
			logs.append(self.history_requests())
		self.assertEqual(logs[0], logs[1])
		for request in logs[0]:
			self.assertEqual(
				(request['limit'], request['max_id'], request['min_id'], request['offset_date']),
				(100, 0, spec['min_id'], 0)
			)

	def test_window_reaches_the_collector_and_the_header(self):
		code, out, _ = self.run_cli([
			'collect', '--telegram-channel', 'alpha_fx', '-o', self.out,
			'--until', '2026-01-08', '--max-messages', '3'
		])
		self.assertEqual(code, 0)
		self.assertIn(
			'History window: date < 2026-01-08 00:00:00 UTC, newest 3 messages', out
		)
		requests = self.history_requests()
		self.assertEqual(requests[0]['offset_date'], datetime(2026, 1, 8, tzinfo=timezone.utc))
		self.assertEqual(requests[0]['limit'], 3)
		with open(os.path.join(self.out, 'alpha_fx', 'alpha_fx_messages.json'), encoding='utf-8') as fh:
			self.assertEqual([m['id'] for m in json.load(fh)['messages']], [7, 6, 5])

	def test_no_window_header_when_unfiltered(self):
		code, out, _ = self.run_cli(['collect', '--telegram-channel', 'alpha_fx', '-o', self.out])
		self.assertEqual(code, 0)
		self.assertNotIn('History window', out)

	def test_unknown_target_exits_1(self):
		code, _, err = self.run_cli(['collect', '--telegram-channel', 'missing_fx', '-o', self.out])
		self.assertEqual(code, 1)
		self.assertIn('1 of 1 targets failed: missing_fx', err)


class ValidationTests(CliTestCase):
	'''
	Every rejected command exits 2 before a client or credentials.
	'''

	def test_selectors(self):
		for argv in (
				['collect'],
				['collect', '--telegram-channel', 'a_fx', '--batch-file', 'b.txt'],
				['collect', '--min-id', '5'],
				['--min-id', '5'],
				['--telegram-channel', 'a_fx', '--batch-file', 'b.txt']):
			with self.subTest(argv=argv):
				self.assert_rejected(argv, '--telegram-channel')

	def test_unreadable_batch_file(self):
		missing = os.path.join(self.tmp, 'missing.txt')
		self.assert_rejected(['collect', '--batch-file', missing], 'cannot read --batch-file')

	def test_unknown_command(self):
		self.assert_rejected(['colect', '--telegram-channel', 'a_fx'], "invalid choice: 'colect'")

	def test_control_bounds(self):
		rejected = [
			('--concurrency', '0'), ('--concurrency', '11'), ('--concurrency', 'x'),
			('--request-interval', '0.05'), ('--request-interval', '61'),
			('--request-interval', 'nan'), ('--request-interval', 'inf'),
			('--max-flood-wait', '-1'), ('--max-flood-wait', '3601'), ('--max-flood-wait', '1.5'),
			('--max-retries', '-1'), ('--max-retries', '11'),
		]
		for flag, value in rejected:
			with self.subTest(flag=flag, value=value):
				self.assert_rejected(['collect', '--telegram-channel', 'a_fx', flag, value], flag)

	def test_control_bounds_are_inclusive(self):
		for flag, value in (('--concurrency', '1'), ('--concurrency', '10'),
				('--request-interval', '0.1'), ('--request-interval', '60'),
				('--max-flood-wait', '0'), ('--max-flood-wait', '3600'),
				('--max-retries', '0'), ('--max-retries', '10')):
			with self.subTest(flag=flag, value=value):
				args = cli.parsers.build_parser().parse_args(
					['collect', '--telegram-channel', 'a_fx', f'{flag}={value}']
				)
				self.assertEqual(vars(args)[flag[2:].replace('-', '_')], float(value))

	def test_control_defaults(self):
		args = cli.parsers.build_parser().parse_args(['collect', '--telegram-channel', 'a_fx'])
		self.assertEqual(
			(args.concurrency, args.request_interval, args.max_flood_wait, args.max_retries),
			(None, 0.25, 60, 3)
		)

	def test_window_values(self):
		for argv, message in (
				(['--since', '2026-13-01'], 'invalid date'),
				(['--until', 'yesterday'], 'invalid date'),
				(['--since', '2026-10-01', '--until', '2026-10-01'], '--since must be earlier than --until'),
				(['--min-id', '-1'], 'must be at least 0'),
				(['--max-id', '0'], 'must be at least 1'),
				(['--min-id', '5', '--max-id', '6'], 'leaves no ids above --min-id 5'),
				(['--max-messages', '0'], 'must be at least 1'),
				(['--max-messages', '2.5'], 'invalid integer')):
			with self.subTest(argv=argv):
				self.assert_rejected(['collect', '--telegram-channel', 'a_fx', *argv], message)

	def test_window_needs_message_collection(self):
		for flag, value in (('--since', '2026-01-01'), ('--until', '2026-01-01'),
				('--min-id', '3'), ('--max-id', '9'), ('--max-messages', '5')):
			with self.subTest(flag=flag):
				self.assert_rejected(
					['collect', '--telegram-channel', 'a_fx', '--limit-download-to-channel-metadata', flag, value],
					'need message collection'
				)

	def test_legacy_form_accepts_only_min_id(self):
		for flag, value in (('--since', '2026-01-01'), ('--until', '2026-01-01'),
				('--max-id', '9'), ('--max-messages', '5'), ('--concurrency', '2'),
				('--request-interval', '1'), ('--max-flood-wait', '5'), ('--max-retries', '1')):
			with self.subTest(flag=flag):
				self.assert_rejected(['--telegram-channel', 'a_fx', flag, value], 'unrecognized arguments')

	def test_legacy_metadata_with_min_id_is_unchanged(self):
		code, _, err = self.run_cli([
			'--telegram-channel', 'alpha_fx', '--limit-download-to-channel-metadata',
			'--min-id', '5', '-o', self.out
		])
		self.assertEqual((code, err), (0, ''))
		self.assertEqual(self.history_requests(), [])

	def test_cross_command_flags(self):
		for argv in (
				['build-dataset', '--concurrency', '3'],
				['build-dataset', '--telegram-channel', 'a_fx'],
				['build-network', '--output', 'x'],
				['build-network', '--min-id', '3'],
				['collect', '--telegram-channel', 'a_fx', '--data-path', 'x']):
			with self.subTest(argv=argv):
				self.assert_rejected(argv, 'unrecognized arguments')

	def test_legacy_builders_reject_collection_flags(self):
		for command, prog in (('build-dataset', 'build-datasets.py'), ('build-network', 'channels-to-network.py')):
			with self.subTest(prog=prog):
				code, _, err = self.run_cli(
					['--telegram-channel', 'a_fx'],
					entry=lambda a: cli.builder_main(command, a, prog=prog)
				)
				self.assertEqual(code, 2)
				self.assertIn(f'usage: {prog}', err)


class DateParsingTests(offline_guard.OfflineTestCase):

	def test_dates_become_aware_utc(self):
		cases = {
			'2026-10-01': datetime(2026, 10, 1, tzinfo=timezone.utc),
			'2026-10-01T12:30': datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc),
			'2026-10-01 12:30:15': datetime(2026, 10, 1, 12, 30, 15, tzinfo=timezone.utc),
			'2026-10-01T12:30:00Z': datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc),
			'2026-10-01T12:30:00+02:00': datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc),
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				value = cli.parsers.utc_datetime(text)
				self.assertEqual(value, expected)
				self.assertEqual(value.utcoffset().total_seconds(), 0)


class ReplaceWarningTests(CliTestCase):
	'''
	A filtered run warns, before credentials, about saved histories it
	will replace; unfiltered and metadata-only runs never warn.
	'''

	def setUp(self):
		super().setUp()
		self.collected = []

		async def fake_run_collection(targets, output_folder, **kwargs):
			self.collected.append(targets)
			return collector.outcomes.CollectionSummary(requested=targets, output_folder=output_folder, succeeded=list(targets))

		patcher = mock.patch.object(collector, 'run_collection', fake_run_collection)
		patcher.start()
		self.addCleanup(patcher.stop)

	def save_history(self, *channels):
		for channel in channels:
			os.makedirs(os.path.join(self.out, channel), exist_ok=True)
			with open(os.path.join(self.out, channel, f'{channel}_messages.json'), 'w', encoding='utf-8') as fh:
				fh.write('{"count": 0, "messages": []}')

	def test_single_channel_with_saved_history(self):
		self.save_history('alpha_fx')
		for argv in (['collect', '--telegram-channel', 'alpha_fx', '--since', '2026-01-01'],
				['--telegram-channel', 'alpha_fx', '--min-id', '3']):
			with self.subTest(argv=argv):
				code, _, err = self.run_cli(argv + ['-o', self.out])
				self.assertEqual(code, 0)
				self.assertIn('> Warning: this filtered run replaces the saved messages of 1 channel(s)', err)
				self.assertIn(': alpha_fx. Use a fresh --output', err)

	def test_batch_lists_count_and_first_ten(self):
		names = [f'c{i:02d}_fx' for i in range(12)]
		self.save_history(*names)
		batch = self.write_batch(*names, 'new_fx')
		code, _, err = self.run_cli(['collect', '--batch-file', batch, '-o', self.out, '--max-messages', '5'])
		self.assertEqual(code, 0)
		self.assertIn('saved messages of 12 channel(s)', err)
		self.assertIn(', '.join(names[:10]) + ', and 2 more.', err)
		self.assertNotIn('new_fx', err)

	def test_no_warning_without_window_history_or_messages(self):
		self.save_history('alpha_fx')
		for argv in (['collect', '--telegram-channel', 'alpha_fx'],
				['collect', '--telegram-channel', 'beta_fx', '--since', '2026-01-01'],
				['--telegram-channel', 'alpha_fx', '--limit-download-to-channel-metadata', '--min-id', '3'],
				['--telegram-channel', 'alpha_fx', '--min-id', '0']):
			with self.subTest(argv=argv):
				code, _, err = self.run_cli(argv + ['-o', self.out])
				self.assertEqual((code, err), (0, ''))


if __name__ == '__main__':
	unittest.main()
