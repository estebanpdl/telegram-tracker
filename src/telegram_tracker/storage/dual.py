# -*- coding: utf-8 -*-
'''
`both` storage: the file outputs and the database written by one writer,
with every point where they can disagree recorded in the database.

For each channel:

	1. stage: the channel's files (as files mode writes them) go to
	   sibling temporary files;
	2. the database transaction stores the channel and sets the target's
	   files_state to 'pending';
	3. publish: each temporary file replaces its file;
	4. files_state becomes 'written'.

The aggregates follow the same order: chats.txt, counter.csv and
collected_chats.csv are staged; the database transaction that stores the
run's aggregates and end state sets aggregate_files to 'pending'; the
files are replaced and the exception logs appended; aggregate_files
becomes 'written'.

A channel staging or transaction failure preserves its previous payloads
in both sinks; run bookkeeping may still record failure. A failure while
publishing stops the run with StorageError: the target is recorded as
failed if the database still accepts it, and 'pending' stays. Any
'pending' left behind (also by a crash between steps 2 and 4) blocks
further collection into the folder (storage.destination). There is no
transaction across the two sinks and no repair: the folder is kept for
inspection and the collection is run again into a fresh --output.

On Windows, replacing a file that another program (an antivirus or
indexing service, an editor) briefly holds open fails with a permission
error. Publishing therefore retries a refused replace, a bounded number of
times (REPLACE_ATTEMPTS, with REPLACE_DELAYS between them), in both mode
only; files mode keeps its single attempt.

--update: the history must be the same in both sinks before the first
history request (same newest ID, same message IDs). A channel whose
histories disagree, or that has one in a single sink, fails with
SinksDisagree; with neither, it is collected in full. The files get the
merged history files mode writes; the database upserts the fetched
messages.
'''

# import modules
import os
import time

# import local submodules
from telegram_tracker.storage.aggregates import aggregate_files, append_logs, reduce_results
from telegram_tracker.storage.files import discard_staged, history_path, publish_staged, read_history, \
	stage_file, text_writer
from telegram_tracker.storage.histories import FileHistories
from telegram_tracker.storage.records import HistoryUnreadable, StorageError
from telegram_tracker.storage.writer import OutputWriter


# Windows replace retry: up to 5 attempts, waiting 0.05, 0.1, 0.2, 0.4 s
# between them (0.75 s total configured delay per file, excluding filesystem calls)
REPLACE_ATTEMPTS = 5
REPLACE_DELAYS = (0.05, 0.1, 0.2, 0.4)


class SinksDisagree(HistoryUnreadable):
	'''
	--update in both mode: the saved files and the database hold
	different histories of a channel. Fails only that channel.
	'''


def retrying_replace(*, windows=None, sleep=None, replace=None):
	'''
	os.replace that, on Windows, retries a PermissionError up to
	REPLACE_ATTEMPTS attempts in all. `windows`, `sleep` and `replace`
	replace the platform check, time.sleep and os.replace (tests).
	'''
	windows = (os.name == 'nt') if windows is None else windows

	def run(src, dst):
		attempt = 0
		while True:
			try:
				return (replace or os.replace)(src, dst)
			except PermissionError:
				if not windows or attempt >= REPLACE_ATTEMPTS - 1:
					raise
				(sleep or time.sleep)(REPLACE_DELAYS[attempt])
				attempt += 1

	return run


def stage_all(files):
	'''
	Stage every (path, write) of `files` (an iterable, read lazily);
	returns [(temp_path, path)]. A failure removes what was staged.
	'''
	staged = []
	try:
		for path, write in files:
			staged.append((stage_file(path, write), path))
	except BaseException:
		discard_staged(staged)
		raise
	return staged


class DualHistories:
	'''
	The history source of both mode (see storage.histories): the files'
	checks before any request, then, after resolution, the database's
	checks and the agreement of the two histories.
	'''
	def __init__(self, output_folder, files, database, con):
		self.output_folder = output_folder
		self.files = files
		self.database = database
		self.con = con

	def before_requests(self, target):
		return self.files.before_requests(target)

	def stored(self, channel_id):
		return self.database.stored(channel_id)

	def stored_file(self, target):
		return self.files.stored_file(target)

	def after_resolve(self, target, channel_id, saved):
		stored = self.database.after_resolve(target, channel_id, None)
		if saved is None and stored is None:
			return None
		advice = (
			'messages not saved. Keep the folder for inspection and collect into a '
			'fresh --output'
		)
		if saved is None or stored is None:
			where = 'the database' if saved is None else 'the files'
			raise SinksDisagree(
				f'its saved history exists only in {where} (the two copies differ); {advice}'
			)
		file_ids = {m['id'] for m in read_history(history_path(self.output_folder, target))['messages']}
		try:
			stored_ids = {row[0] for row in self.con.execute(
				'SELECT message_id FROM messages WHERE channel_id = ?', (channel_id,))}
		except Exception as exc:
			raise HistoryUnreadable(f'the stored history of {target} cannot be read: {exc}') from None
		if saved.newest_id != stored.newest_id or file_ids != stored_ids:
			raise SinksDisagree(
				f'its saved files and the database hold different histories (newest message '
				f'{saved.newest_id} in the files, {stored.newest_id} in the database; '
				f'{len(file_ids ^ stored_ids)} message ID(s) in only one); {advice}'
			)
		return stored

	def merge(self, target, saved, fetched):
		return self.files.merge(target, saved, fetched)


class DualWriter:
	'''
	Writer of both mode (the methods of storage.writer.OutputWriter):
	`files` is the run's OutputWriter, `database` its SQLiteWriter
	(storage 'both'). See the module docstring for the order of writes.
	'''
	def __init__(self, files, database, replace=None):
		self.files = files
		self.database = database
		self.replace = replace or retrying_replace()
		self.histories = DualHistories(
			files.output_folder, FileHistories(files.output_folder, files.profile),
			database.histories, database.con
		)
		self.committed = []
		self.not_found = []
		self.staged_aggregates = None
		self.aggregates = None
		self.finalized = False
		self.pending_targets = []
		self.aggregate_files = None

	def commit(self, result):
		'''
		Stage, store, publish and mark one channel. Returns the files'
		takeover entry (the database records its own in run_targets).
		'''
		files, takeover = self.files.channel_files(result)
		staged = stage_all((path, text_writer(text)) for path, text in files)
		try:
			self.database.commit(result, files_state='pending')
		except BaseException:
			discard_staged(staged)
			raise
		self.pending_targets.append(result.target)
		try:
			publish_staged(staged, self.replace)
			self.database.mark_target_written(result.index)
		except StorageError as exc:
			self.database.fail_target(result.index, f'storage error: {exc}')
			raise
		self.pending_targets.remove(result.target)
		self.files.committed.append(result)
		self.committed.append(result)
		return takeover

	def has_history(self, target, channel_id):
		return self.files.has_history(target, channel_id)

	def record_not_found(self, index, target):
		self.not_found.append((index, target))

	def finalize(self, outcomes):
		'''
		Replay the committed channels once, stage the aggregate files and
		store the observations. Returns the Aggregates.
		'''
		agg = reduce_results(self.committed, outcomes, self.not_found)
		self.finalized = True
		try:
			staged = stage_all(aggregate_files(self.files.output_folder, agg))
		except OSError as exc:
			raise StorageError(
				f'cannot update aggregate files in {self.files.output_folder}: {exc}') from exc
		try:
			self.database.record_observations(agg)
		except BaseException:
			discard_staged(staged)
			raise
		self.staged_aggregates = staged
		self.aggregates = agg
		self.files.report_skipped(agg)
		return agg

	def finish(self, state, *, summary, stop_reason, requests, targets):
		'''
		Store the end of the run (with aggregate_files 'pending' when the
		aggregates were staged), publish the aggregate files and logs, then
		mark them written.
		'''
		staged, self.staged_aggregates = self.staged_aggregates, None
		try:
			self.database.finish(
				state, summary=summary, stop_reason=stop_reason, requests=requests, targets=targets
			)
		except BaseException:
			discard_staged(staged or [])
			raise
		if staged is None:
			return
		self.aggregate_files = 'pending'
		try:
			publish_staged(staged, self.replace)
			append_logs(self.files.output_folder, self.aggregates)
		except StorageError as exc:
			error = f'storage error: {exc}'
			self.database.interrupt_finished(f'{stop_reason}; {error}' if stop_reason else error)
			raise
		self.database.mark_aggregates_written()
		self.aggregate_files = 'written'

	def status_fields(self):
		fields = self.database.status_fields()
		fields['publication'] = {
			'pending_targets': list(self.pending_targets),
			'aggregate_files': self.aggregate_files
		}
		return fields

	def close(self):
		self.discard()
		self.database.close()

	def discard(self):
		if self.staged_aggregates:
			discard_staged(self.staged_aggregates)
			self.staged_aggregates = None
