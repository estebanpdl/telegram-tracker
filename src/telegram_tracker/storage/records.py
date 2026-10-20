# -*- coding: utf-8 -*-
'''
Collection output records: file and column names, sidecar states, storage
errors, and the results workers hand to the writer. Importing this module
loads nothing beyond the standard library.
'''

# import modules
from dataclasses import dataclass, field


STATUS_FILE = 'collection-status.json'
LOCK_FILE = '.collection.lock'
STATUS_FORMAT = 1

CHANNELS_NOT_FOUND = '_exceptions-channels-not-found.txt'
USERS_NOT_FOUND = '_exceptions-users-not-found.txt'

COUNTER_COLUMNS = [
	'id',
	'username',
	'counter',
	'from_messages',
	'channel_request',
	'channel_req_targeted_by',
	'source'
]

# columns build-datasets.py adds; not part of collection output
DATASET_STAT_COLUMNS = (
	'collected_actions',
	'collected_posts',
	'replies',
	'other_actions',
	'number_views',
	'forwards',
	'replies_received'
)

# sidecar states
RUNNING = 'running'
COMPLETE = 'complete'
PARTIAL = 'partial'
INTERRUPTED = 'interrupted'




class StorageError(Exception):
	'''
	An output file could not be written. Ends the run.
	'''


class OutputLocked(Exception):
	'''
	Another collection holds the output folder, or an interrupted one left
	its lock behind.
	'''


class HistoryUnreadable(Exception):
	'''
	An existing <channel>_messages.json cannot be read or parsed.
	'''


'''

Records

'''

@dataclass
class ChannelResult:
	'''
	Everything one channel's collection produced, before anything is
	written.

	index -> position of the target in the run's input
	observations -> [(req_type, [(chat_id, username), ...]), ...] in the
		order the chats were seen (channel response, then history pages)
	history -> JSON-safe messages payload to write; None writes nothing
		(for 'merged', the history source decides: the files get the
		whole merged history, the database none)
	history_mode -> 'replaced' (full or window), 'merged' / 'unchanged' /
		'created' (--update), or 'not_collected' (metadata only)
	fetched -> for 'merged', the fetched payload merged into the saved
		history (the database upserts it); None otherwise
	'''
	index: int
	target: str
	channel_id: int | None = None
	channel_data: dict | None = None
	history: dict | None = None
	history_mode: str = 'not_collected'
	fetched: dict | None = None
	observations: list = field(default_factory=list)
	messages: int | None = None
	min_id: int | None = None


@dataclass
class EnrichOutcome:
	'''
	Result of enriching one discovered chat (its full-channel response).

	linked -> [(chat_id, username)] public chats listed in the response,
		in order, up to the point of any failure
	rows -> collected_chats rows (public chats with participants_count);
		None when enrichment failed
	error -> None, 'not_found' (logged to the users-not-found file), or a
		'missing field ...' description (reported, not logged)
	'''
	linked: list = field(default_factory=list)
	rows: list | None = None
	error: str | None = None
