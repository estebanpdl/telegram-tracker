# -*- coding: utf-8 -*-
'''
--update against the stored histories, and reading a history back.

`SQLiteHistories` is the database's history source (see
storage.histories). It looks a history up by the resolved channel ID,
after resolution and before the first history request, through the
run's single connection; no transaction stays open across a request.
'''

# import modules
import json
import sqlite3

# import local submodules
from telegram_tracker.storage.histories import SavedHistory
from telegram_tracker.storage.provenance import HistoryRefused, RunProfile, refusal
from telegram_tracker.storage.records import HistoryUnreadable
from telegram_tracker.storage.sqlite.mapping import history_payload


class SQLiteHistories:
	'''
	Histories stored in the database. --update merges only into a history
	of the run's profile (provenance.refusal on the histories row's
	profile_name and profile_id); a run without a profile counts as an
	unknown one, which may merge only into a history of unknown profile.
	'''
	def __init__(self, con, profile=None):
		self.con = con
		self.profile = profile or RunProfile()

	def before_requests(self, target):
		return None

	def after_resolve(self, target, channel_id, saved):
		try:
			row = self.con.execute(
				'SELECT profile_name, profile_id, message_count, '
				'(SELECT max(message_id) FROM messages WHERE channel_id = h.channel_id) '
				'FROM histories h WHERE channel_id = ?',
				(channel_id,)
			).fetchone()
		except sqlite3.Error as exc:
			raise HistoryUnreadable(f'the stored history of {target} cannot be read: {exc}') from None
		if row is None:
			return None

		profile_name, profile_id, count, newest = row
		reason = refusal({'profile_name': profile_name, 'profile_id': profile_id}, self.profile)
		if reason is not None:
			raise HistoryRefused(
				f'--update refused: {reason}. Collect it without --update (this replaces '
				'the history and records the takeover) or into a fresh --output'
			)
		return SavedHistory(newest_id=newest or 0, messages=count, channel_id=channel_id)

	def stored_file(self, target):
		return False

	def stored(self, channel_id):
		try:
			row = self.con.execute('SELECT 1 FROM histories WHERE channel_id = ?', (channel_id,)).fetchone()
		except sqlite3.Error as exc:
			raise HistoryUnreadable(f'the stored history of channel {channel_id} cannot be read: {exc}') from None
		return row is not None

	def merge(self, target, saved, fetched):
		'''
		The writer upserts the fetched messages itself (ChannelResult.
		fetched), so no payload is returned. The count adds the fetched
		IDs not stored yet.
		'''
		ids = sorted({m['id'] for m in fetched['messages']})
		if not ids:
			return None, 'unchanged', saved.messages
		try:
			stored = self.con.execute(
				'SELECT count(*) FROM messages WHERE channel_id = ? '
				'AND message_id IN (SELECT value FROM json_each(?))',
				(saved.channel_id, json.dumps(ids))
			).fetchone()[0]
		except sqlite3.Error as exc:
			raise HistoryUnreadable(f'the stored history of {target} cannot be read: {exc}') from None
		return None, 'merged', saved.messages + len(ids) - stored


def load_history(con, channel_id):
	'''
	The stored history of `channel_id` as a payload (messages newest
	first; chats and users in stored order, each its latest stored
	object), or None when none is stored.
	'''
	row = con.execute('SELECT envelope FROM histories WHERE channel_id = ?', (channel_id,)).fetchone()
	if row is None:
		return None
	messages = [r[0] for r in con.execute(
		'SELECT raw FROM messages WHERE channel_id = ? ORDER BY message_id DESC', (channel_id,)
	)]
	entity_rows = con.execute(
		'SELECT e.list, p.raw FROM history_entities e '
		'JOIN peers p ON p.peer_kind = e.peer_kind AND p.peer_id = e.peer_id '
		'WHERE e.channel_id = ? ORDER BY e.list, e.ordinal',
		(channel_id,)
	).fetchall()
	return history_payload(row[0], messages, entity_rows)
