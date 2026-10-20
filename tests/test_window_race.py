"""A history created while logging in still needs replacement consent."""

import io
import os
import unittest

from window_confirm_support import ConfirmCase


class LateHistoryTests(ConfirmCase):
	def appearing_file(self, out):
		path = os.path.join(out, 'alpha_fx', 'alpha_fx_messages.json')
		previous = b'{"count":1,"messages":[{"id":999}],"chats":[],"users":[]}'

		async def authorize(client):
			# The file appears after the scan, before the output lock is taken.
			os.makedirs(os.path.dirname(path))
			with open(path, 'wb') as fh:
				fh.write(previous)

		return path, previous, authorize

	def test_a_file_appearing_after_the_prompt_check_is_kept(self):
		for storage in ('files', 'both'):
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, targets=('beta_fx',))
				path, previous, authorize = self.appearing_file(out)
				code, _, err = self.window(io.StringIO(''), out, authorize=authorize,
					targets=('alpha_fx', 'empty_fx'))
				self.assertEqual(code, 1, err)
				self.assertIn('appeared after the replacement check', err)
				with open(path, 'rb') as fh:
					self.assertEqual(fh.read(), previous)
				self.assertEqual([t['status'] for t in self.status(out)['targets']], ['failed', 'succeeded'])
				self.assertNotIn(('get_entity', {'target': 'alpha_fx'}), self.last_client.calls)

	def test_yes_authorizes_replacing_a_late_file(self):
		for storage in ('files', 'both'):
			with self.subTest(storage=storage):
				out = self.folder(storage)
				self.save(storage, out, targets=('beta_fx',))
				path, previous, authorize = self.appearing_file(out)
				code, _, err = self.window(io.StringIO(''), out, '--yes', authorize=authorize)
				self.assertEqual(code, 0, err)
				with open(path, 'rb') as fh:
					self.assertNotEqual(fh.read(), previous)
				self.assertEqual(self.history_size(storage, out), 1)


if __name__ == '__main__':
	unittest.main()
