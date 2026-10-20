# -*- coding: utf-8 -*-
'''
Message counts in the collect summary and the sidecar, in every storage
mode: a full or window run reports each saved history's size, an
--update run also the messages it added (`new_messages` in the sidecar
entry), and metadata only reports none. The block comes after every
earlier summary line, which stay as they were. Expected counts come from
the saved files or the database, never from the summary itself.
'''

# import modules
import os
import re
import unittest

from unittest import mock

from sqlite_support import ALPHA, add_messages
from storage_modes_support import ModesCase, read_json

from telegram_tracker import cli
from telegram_tracker.storage.files import history_path


MODES = ('files', 'sqlite', 'both')
TIMES = re.compile(r'(Init|End) program at .*')
HEADER = '> Messages saved:'


def message_block(out):
	'''
	The lines of the messages block (header and rows), or [].
	'''
	lines = out.splitlines()
	starts = [i for i, line in enumerate(lines) if line.startswith(HEADER)]
	if not starts:
		return []
	block = lines[starts[0]:]
	return block[:block.index('')] if '' in block else block


class MessageCountCase(ModesCase):

	def saved_count(self, target='alpha_fx', storage='files'):
		'''
		The history's size as stored: the messages file, or the database.
		'''
		if storage == 'sqlite':
			return self.rows(
				'SELECT count(*) FROM messages m JOIN peers p ON p.peer_kind = \'channel\' '
				'AND p.peer_id = m.channel_id WHERE p.username = ?', target)[0][0]
		return len(read_json(history_path(self.out, target))['messages'])

	def entry(self, index=0):
		return self.status()['targets'][index]

	def update_client(self, *new_ids):
		client = self.make_client()
		add_messages(client.world, ALPHA, *new_ids)
		return client

	def collect_mode(self, mode, *extra, **kwargs):
		storage = None if mode == 'files' else mode
		# a later run into the same folder inherits its mode
		if storage is not None and os.path.exists(self.out):
			storage = None
		code, out, err = self.collect(*extra, storage=storage, **kwargs)
		self.assertEqual(code, 0, err)
		return out


class FullAndWindowTests(MessageCountCase):

	def test_full_run_reports_each_history_size(self):
		for mode in MODES:
			with self.subTest(mode=mode):
				self.out = os.path.join(self.tmp, f'full-{mode}')
				out = self.collect_mode(mode)
				count = self.saved_count(storage=mode)
				self.assertGreater(count, 1)
				self.assertEqual(message_block(out), [
					f'> Messages saved: {count} in 1 channel(s)',
					f'>   alpha_fx: {count}'
				])
				self.assertEqual(self.entry()['messages'], count)
				self.assertNotIn('new_messages', self.entry())

	def test_window_run_reports_the_kept_messages(self):
		for mode in MODES:
			with self.subTest(mode=mode):
				self.out = os.path.join(self.tmp, f'window-{mode}')
				out = self.collect_mode(mode, '--max-messages', '2')
				self.assertEqual(self.saved_count(storage=mode), 2)
				self.assertEqual(message_block(out), [
					'> Messages saved: 2 in 1 channel(s)', '>   alpha_fx: 2'
				])

	def test_batch_lists_succeeded_channels_in_input_order(self):
		code, out, err = self.collect(targets=('beta_fx', 'missing_fx', 'alpha_fx'))
		self.assertEqual((code, err.splitlines()[-1]), (1, '> 1 of 3 targets failed: missing_fx'))
		alpha, beta = self.saved_count('alpha_fx'), self.saved_count('beta_fx')
		self.assertEqual(message_block(out), [
			f'> Messages saved: {alpha + beta} in 2 channel(s)',
			f'>   beta_fx: {beta}',
			f'>   alpha_fx: {alpha}'
		])
		self.assertNotIn('missing_fx', '\n'.join(message_block(out)))

	def test_metadata_only_reports_no_messages(self):
		code, out, err = self.collect('--limit-download-to-channel-metadata')
		self.assertEqual(code, 0, err)
		self.assertEqual(message_block(out), [])
		self.assertNotIn(HEADER, out)


class UpdateTests(MessageCountCase):

	def test_update_reports_new_and_total_messages(self):
		for mode in MODES:
			with self.subTest(mode=mode):
				self.out = os.path.join(self.tmp, f'update-{mode}')
				self.collect_mode(mode)
				before = self.saved_count(storage=mode)
				out = self.collect_mode(mode, '--update', client=self.update_client(11, 12))
				after = self.saved_count(storage=mode)
				self.assertEqual(after - before, 2)
				self.assertEqual(message_block(out), [
					f'> Messages saved: 2 new; the saved histories of 1 channel(s) now hold {after}',
					f'>   alpha_fx: 2 new, {after} in total'
				])
				entry = self.entry()
				self.assertEqual((entry['history'], entry['messages'], entry['new_messages']), ('merged', after, 2))

	def test_update_without_new_posts_reports_zero(self):
		for mode in MODES:
			with self.subTest(mode=mode):
				self.out = os.path.join(self.tmp, f'unchanged-{mode}')
				self.collect_mode(mode)
				count = self.saved_count(storage=mode)
				out = self.collect_mode(mode, '--update')
				self.assertEqual(message_block(out), [
					f'> Messages saved: 0 new; the saved histories of 1 channel(s) now hold {count}',
					f'>   alpha_fx: 0 new, {count} in total'
				])
				self.assertEqual((self.entry()['history'], self.entry()['new_messages']), ('unchanged', 0))

	def test_update_of_a_channel_without_history_counts_all_as_new(self):
		self.collect_mode('files', targets=('beta_fx',))
		beta = self.saved_count('beta_fx')
		out = self.collect_mode(
			'files', '--update', targets=('alpha_fx', 'beta_fx'), client=self.update_client(11))
		alpha = self.saved_count('alpha_fx')
		self.assertEqual(message_block(out), [
			f'> Messages saved: {alpha} new; the saved histories of 2 channel(s) now hold {alpha + beta}',
			f'>   alpha_fx: {alpha} new, {alpha} in total',
			f'>   beta_fx: 0 new, {beta} in total'
		])
		self.assertEqual([e['new_messages'] for e in self.status()['targets']], [alpha, 0])
		self.assertEqual(self.entry()['history'], 'created')


class StableLinesTests(MessageCountCase):
	'''
	The block is appended: without it, the output is what the summary
	printed before.
	'''

	def run_case(self, folder, extra, targets, first, client_factory, *, block):
		'''
		stdout of `collect *extra` into a fresh folder, after a first run
		of the `first` targets, with or without the messages block.
		'''
		self.out = os.path.join(self.tmp, folder)
		if first:
			self.collect_mode('files', targets=first)
		client = client_factory and client_factory()
		if block:
			out = self.collect_mode('files', *extra, targets=targets, client=client)
		else:
			with mock.patch.object(cli.collect, 'print_messages', lambda saved: None):
				out = self.collect_mode('files', *extra, targets=targets, client=client)
		return TIMES.sub('', out).replace(folder, '<out>')

	def test_earlier_lines_keep_their_text_and_order(self):
		update = lambda: self.update_client(11, 12)  # noqa: E731
		cases = (
			('full', (), ('alpha_fx',), (), None),
			('update', ('--update',), ('alpha_fx',), ('alpha_fx',), update),
			# alpha has no saved history: the "collected in full" paragraph follows the summary
			('update-created', ('--update',), ('alpha_fx', 'beta_fx'), ('beta_fx',), update),
		)
		for name, extra, targets, first, client_factory in cases:
			with self.subTest(case=name):
				old = self.run_case(f'old-{name}', extra, targets, first, client_factory, block=False)
				new = self.run_case(f'new-{name}', extra, targets, first, client_factory, block=True)
				block = message_block(new)
				self.assertTrue(block)
				if name == 'update-created':
					self.assertIn('were collected in full', old)
				self.assertEqual(new, old + '\n'.join(block) + '\n\n')


if __name__ == '__main__':
	unittest.main()
