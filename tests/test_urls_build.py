# -*- coding: utf-8 -*-
'''
build-urls on whole collection folders: the occurrences and counts
expected from url_data, the same outputs whether a folder is read from
its files or its database (files, sqlite and both folders; both read
either way), repeated builds, the CSV and the database holding the same
rows, inputs left unchanged, provenance, and a renamed channel (the
documented difference). Synthetic fixtures only.
'''

# import modules
import contextlib
import io
import os
import unittest

from builder_parity_support import ALICE, BATCH, BOB, BUILDS, ParityCase, add_post, collect, read_bytes, \
	rename
from url_build_support import comparable, csv_rows, inputs, leftovers, record, run_build, tables
from url_data import ALPHA, CHANNELS, DOMAINS, OCCURRENCES, OCCURRENCE_FIELDS, add_url_messages

from telegram_tracker import cli
from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.urls.outputs import COLUMN_NAMES, URLS_DATABASE, URLS_DATASET


def with_urls(*changes):
	'''
	A world change adding the URL messages, then each of `changes`.
	'''
	def change(world):
		add_url_messages(world)
		for extra in changes:
			extra(world)
	return change


class UrlCase(ParityCase):

	def builds(self, *, repeat=1):
		'''
		{build label: [comparable outputs per build]} over BUILDS, with
		the inputs checked unchanged by every build.
		'''
		results = {}
		for label, mode, source in BUILDS:
			out = self.folders[mode]
			before = inputs(out)
			results[label] = []
			for _ in range(repeat):
				run_build(out, source)
				results[label].append(comparable(out))
				self.assertEqual(inputs(out), before, label)
				self.assertEqual(leftovers(out), [], label)
		return results

	def assertParity(self, results, labels=None):
		expected = results['files'][0]
		for label in labels or [b[0] for b in BUILDS]:
			for index, outputs in enumerate(results[label]):
				for name, value in outputs.items():
					with self.subTest(build=label, repeat=index, output=name):
						self.assertEqual(value, expected[name])


class GoldenTests(UrlCase):

	def setUp(self):
		super().setUp()
		self.out = self.folders['files']
		collect(self.out, 'files', BATCH, world_change=add_url_messages)
		self.summary, self.stdout, self.stderr = run_build(self.out)
		self.rows = csv_rows(os.path.join(self.out, URLS_DATASET))

	def test_occurrences(self):
		got = [tuple(row[f] for f in OCCURRENCE_FIELDS) for row in self.rows]
		want = [tuple('' if v is None else str(v) for v in row) for row in OCCURRENCES]
		self.assertEqual(got, want)
		self.assertEqual([row['occurrence_id'] for row in self.rows],
			[str(i) for i in range(1, len(OCCURRENCES) + 1)])
		self.assertEqual((self.summary.occurrences, self.summary.histories), (len(OCCURRENCES), 3))
		self.assertEqual(list(self.rows[0]), list(COLUMN_NAMES))

	def by_message(self, message_id):
		return [row for row in self.rows if row['message_id'] == str(message_id)]

	def test_message_context_and_forward_origin(self):
		forwarded = self.by_message(14)
		self.assertEqual(len(forwarded), 4)
		for row in forwarded:
			self.assertEqual(
				{k: row[k] for k in ('channel_id', 'date', 'msg_link', 'is_forward', 'fwd_from_type',
					'fwd_from_id', 'fwd_from_post', 'fwd_from_date', 'fwd_from_name')},
				{'channel_id': str(ALPHA), 'date': '2026-01-14 14:00:00+00:00',
					'msg_link': 'https://t.me/alpha_fx/14', 'is_forward': '1', 'fwd_from_type': 'channel',
					'fwd_from_id': '201', 'fwd_from_post': '77', 'fwd_from_date': '2026-01-05 05:00:00+00:00',
					'fwd_from_name': ''}
			)
		self.assertEqual({row['is_forward'] for row in self.by_message(12)}, {'0'})
		self.assertEqual({row['fwd_from_type'] for row in self.by_message(12)}, {''})

	def test_parsed_parts(self):
		idn, www = self.by_message(11)
		self.assertEqual(
			{k: idn[k] for k in ('scheme', 'host', 'host_unicode', 'path', 'query', 'fragment', 'suffix',
				'is_private_suffix', 'subdomain', 'port')},
			{'scheme': 'https', 'host': 'xn--e1afmkfd.xn--p1ai', 'host_unicode': 'пример.рф',
				'path': '/путь', 'query': 'q=1', 'fragment': 'frag', 'suffix': 'xn--p1ai',
				'is_private_suffix': '0', 'subdomain': '', 'port': ''}
		)
		self.assertEqual((www['scheme'], www['subdomain'], www['suffix']), ('', 'www', 'co.uk'))
		blog = self.by_message(14)[0]
		self.assertEqual((blog['suffix'], blog['is_private_suffix'], blog['registry_domain']),
			('blogspot.com', '1', 'blogspot.com'))
		ipv6 = self.by_message(14)[1]
		self.assertEqual((ipv6['netloc'], ipv6['host'], ipv6['port']), ('[::1]:80', '::1', '80'))
		self.assertEqual((www['netloc'], idn['netloc']), ('www.Example.CO.UK', 'пример.рф'))

	def test_previews_and_hidden_targets(self):
		preview = self.by_message(13)[-1]
		self.assertEqual(
			{k: preview[k] for k in ('preview_type', 'preview_matches_text', 'preview_display_url',
				'preview_title', 'text_offset', 'label')},
			{'preview_type': 'WebPagePending', 'preview_matches_text': '0', 'preview_display_url': '',
				'preview_title': '', 'text_offset': '', 'label': ''}
		)
		text, preview = self.by_message(8)
		self.assertEqual((text['preview_matches_text'], preview['preview_matches_text']), ('', '1'))
		self.assertEqual((preview['preview_site_name'], preview['preview_title'], preview['preview_description']),
			('Example', 'Example A', 'Synthetic page'))
		hidden = self.by_message(12)[0]
		self.assertEqual((hidden['label'], hidden['raw_url']), ('https://example.com/a', 'https://evil.example.net/x'))

	def test_counts(self):
		db = tables(os.path.join(self.out, URLS_DATABASE))
		domains = {
			row['domain_key']: (row['domain_kind'], row['occurrences'], row['text_occurrences'],
				row['preview_occurrences'], row['messages'], row['urls'], row['channels'])
			for row in db['domains']
		}
		self.assertEqual(domains, DOMAINS)
		channels = {
			row['channel']: (row['occurrences'], row['text_occurrences'], row['preview_occurrences'],
				row['messages_with_urls'], row['urls'], row['domains'])
			for row in db['channels']
		}
		self.assertEqual(channels, CHANNELS)
		# messages read: every item of each history, service messages too
		self.assertEqual({row['channel']: row['histories'] for row in db['channels']},
			{'alpha_fx': 1, 'beta_fx': 1, 'empty_fx': 1})
		alpha = next(row for row in db['channels'] if row['channel'] == 'alpha_fx')
		self.assertEqual(alpha['messages'], record(self.out)['histories'][0]['messages'])

		pairs = {(row['channel'], row['domain_key']): (row['occurrences'], row['messages'], row['urls'])
			for row in db['channel_domains']}
		self.assertEqual(pairs[('beta_fx', 'example.co.uk')], (1, 1, 1))
		self.assertEqual(pairs[('alpha_fx', 'example.co.uk')], (1, 1, 1))
		self.assertEqual(pairs[('alpha_fx', 't.me')], (2, 1, 1))
		self.assertEqual(len(pairs), sum(row['domains'] for row in db['channels']))

		registered = next(row for row in db['domains'] if row['domain_key'] == 'foo.blogspot.com')
		self.assertEqual((registered['registry_domain'], registered['suffix'], registered['is_private_suffix']),
			('blogspot.com', 'blogspot.com', 1))
		bucket = next(row for row in db['domains'] if row['domain_key'] == '[public-suffix]')
		self.assertEqual((bucket['registered_domain'], bucket['suffix']), (None, None))
		uk = next(row for row in db['domains'] if row['domain_key'] == 'example.co.uk')
		self.assertEqual((uk['first_date'], uk['last_date']),
			('2026-01-11 11:00:00+00:00', '2026-01-21 21:00:00+00:00'))

	def test_csv_and_database_hold_the_same_rows(self):
		db = tables(os.path.join(self.out, URLS_DATABASE))['occurrences']
		as_text = [{k: '' if v is None else str(v) for k, v in row.items()} for row in db]
		self.assertEqual(as_text, self.rows)

	def test_build_record_and_table(self):
		built = record(self.out)
		db = tables(os.path.join(self.out, URLS_DATABASE))
		build, = db['build']
		self.assertEqual(built['build_uuid'], build['build_uuid'])
		self.assertEqual(built['build_uuid'], self.summary.build_uuid)
		self.assertEqual((built['builder'], built['source'], built['storage']), ('build-urls', 'files', 'files'))
		self.assertEqual(built['outputs'][URLS_DATASET]['rows'], len(OCCURRENCES))
		self.assertEqual((build['format'], build['normalization_version'], build['built_at']),
			(1, built['normalization_version'], built['built_at']))
		self.assertEqual(built['suffix_list']['version'], '5.4.0')
		self.assertEqual([h['channel'] for h in built['histories']], ['alpha_fx', 'beta_fx', 'empty_fx'])


class ParityTests(UrlCase):

	def check(self, *steps, repeat=2):
		self.sequence(*steps)
		results = self.builds(repeat=repeat)
		self.assertParity(results)
		return results

	def test_partial_batch(self):
		results = self.check((BATCH, {'world_change': add_url_messages}))
		self.assertEqual(len(results['files'][0]['csv']), len(OCCURRENCES))

	def test_update_adds_new_occurrences(self):
		results = self.check(
			(BATCH, {'profile': ALICE, 'world_change': add_url_messages}),
			(BATCH, {'profile': ALICE, 'update': True, 'world_change': with_urls(add_post)})
		)
		# the copied newest message (15) adds its occurrences as message 16
		ids = [row['message_id'] for row in results['files'][0]['csv']]
		self.assertEqual(ids[:3], ['16', '16', '16'])
		self.assertEqual(len(ids), len(OCCURRENCES) + 3)

	def test_window_after_full(self):
		results = self.check(
			(BATCH, {'world_change': add_url_messages}),
			(['alpha_fx'], {'window': HistoryWindow(max_messages=2), 'world_change': add_url_messages})
		)
		alpha = {row['message_id'] for row in results['files'][0]['csv'] if row['channel'] == 'alpha_fx'}
		self.assertEqual(alpha, {'15', '14'})

	def test_profiles_and_takeover(self):
		results = self.check(
			(['alpha_fx'], {'profile': ALICE, 'world_change': add_url_messages}),
			(['beta_fx'], {'profile': BOB, 'world_change': add_url_messages}),
			(['alpha_fx'], {'profile': BOB, 'world_change': add_url_messages})
		)
		profiles = {(row['channel'], row['profile_name'], row['profile_id'])
			for row in results['files'][0]['csv']}
		self.assertEqual(profiles, {('alpha_fx', 'bob', BOB.id), ('beta_fx', 'bob', BOB.id)})

	def test_unknown_profile_stays_unknown(self):
		results = self.check((['alpha_fx'], {'world_change': add_url_messages}), repeat=1)
		self.assertEqual({(row['profile_name'], row['profile_id']) for row in results['files'][0]['csv']},
			{('', '')})

	def test_no_histories(self):
		results = self.check((['missing_fx'], {}))
		self.assertEqual(results['files'][0]['csv'], [])
		self.assertEqual(results['files'][0]['db']['domains'], [])

	def test_history_runs_are_recorded_by_the_database(self):
		self.sequence((['alpha_fx'], {'world_change': add_url_messages}))
		for label, mode, source in BUILDS:
			with self.subTest(build=label):
				out = self.folders[mode]
				run_build(out, source)
				runs = {row['history_run'] for row in csv_rows(os.path.join(out, URLS_DATASET))}
				built = record(out)
				if built['source'] == 'sqlite':
					self.assertEqual(runs, {built['last_run']['run_uuid']})
					self.assertEqual(built['histories'][0]['run'], built['last_run']['run_uuid'])
				else:
					self.assertEqual(runs, {''})


class CommandTests(UrlCase):

	def test_the_command_passes_its_source(self):
		out = self.folders['both']
		collect(out, 'both', BATCH, world_change=add_url_messages)
		for source in ('auto', 'files', 'sqlite'):
			with self.subTest(source=source):
				stdout, stderr = io.StringIO(), io.StringIO()
				with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
					code = cli.main(['build-urls', '-d', out, '--source', source])
				self.assertEqual(code, 0, stderr.getvalue())
				self.assertIn(f'> Wrote {len(OCCURRENCES)} URL occurrences from 3 histories to {out}',
					stdout.getvalue())
				self.assertEqual(record(out)['source'], 'sqlite' if source == 'sqlite' else 'files')

	def test_one_history(self):
		out = self.folders['files']
		collect(out, 'files', ['beta_fx'], world_change=add_url_messages)
		stdout = io.StringIO()
		with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(cli.main(['build-urls', '-d', out]), 0)
		self.assertIn(f'> Wrote 1 URL occurrence from 1 history to {out}', stdout.getvalue())


class RepeatTests(UrlCase):

	def test_a_rebuild_replaces_and_reproduces(self):
		out = self.folders['sqlite']
		collect(out, 'sqlite', BATCH, world_change=add_url_messages)
		run_build(out)
		first_csv = read_bytes(os.path.join(out, URLS_DATASET))
		first = tables(os.path.join(out, URLS_DATABASE))
		first_uuid = record(out)['build_uuid']
		summary, _, err = run_build(out)
		self.assertEqual(read_bytes(os.path.join(out, URLS_DATASET)), first_csv)
		second = tables(os.path.join(out, URLS_DATABASE))
		self.assertNotEqual(summary.build_uuid, first_uuid)
		for table in second:
			if table != 'build':
				self.assertEqual(second[table], first[table], table)
		self.assertNotIn('Note: urls', err)


class RenameTests(UrlCase):
	'''
	A channel collected under its old and then its new username: the
	files keep a messages file per name, the database one history under
	the latest name, so the files count the old name's occurrences too.
	'''

	def test_rename(self):
		self.sequence(
			(['alpha_fx'], {'world_change': add_url_messages}),
			(['alpha_new_fx'], {'world_change': with_urls(lambda w: rename(w, ALPHA, 'alpha_new_fx'))})
		)
		results = self.builds()
		self.assertParity(results, labels=['both'])
		channels = lambda label: sorted({row['channel'] for row in results[label][0]['csv']})
		self.assertEqual(channels('files'), ['alpha_fx', 'alpha_new_fx'])
		# the same rows under the new name, numbered from 1 without the old name's
		without_id = lambda rows: [{k: v for k, v in row.items() if k != 'occurrence_id'} for row in rows]
		new_name = [row for row in results['files'][0]['csv'] if row['channel'] == 'alpha_new_fx']
		self.assertEqual(len(new_name), 20)
		for label in ('sqlite', 'both-sqlite'):
			self.assertEqual(channels(label), ['alpha_new_fx'])
			self.assertEqual(without_id(results[label][0]['csv']), without_id(new_name))


if __name__ == '__main__':
	unittest.main()
