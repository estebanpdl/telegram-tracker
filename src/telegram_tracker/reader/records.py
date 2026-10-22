# -*- coding: utf-8 -*-
'''
What a collection reader hands to the builders: the selected source,
one record per saved message history, and the collected-chats input.
Standard library only.
'''

# import modules
from dataclasses import dataclass, field


# --source values
AUTO = 'auto'
FILES = 'files'
SQLITE = 'sqlite'
SOURCES = (AUTO, FILES, SQLITE)


@dataclass(frozen=True)
class HistoryProvenance:
	'''
	Who collected a saved history, as recorded locally; every field may
	be None (unknown). Profile names and IDs are local labels, not
	verified account identities.

	run -> the run that last changed the history: its run_uuid in the
		database; None for files, which do not identify runs
	mode -> 'replaced', 'created' or 'merged' (database), or the
		provenance record's `history` (files)
	takeover -> the last replace took over another profile's history
	'''
	profile_name: str | None = None
	profile_id: str | None = None
	run: str | None = None
	mode: str | None = None
	takeover: bool | None = None

	def record(self):
		return {
			'profile_name': self.profile_name,
			'profile_id': self.profile_id,
			'run': self.run,
			'mode': self.mode,
			'takeover': self.takeover
		}


@dataclass
class ChannelHistory:
	'''
	One saved message history. `name` is the collection target it was
	saved under (the <name>/<name>_messages.json of the files) and
	`label` names it in messages. `load()` returns the payload: the
	response's fields (`count`, ...), `messages`, newest first, and the
	`chats` and `users` it carries.
	'''
	name: str
	label: str
	channel_id: int | None
	provenance: HistoryProvenance
	load: object = field(repr=False)

	def record(self, messages=None):
		'''
		The history as listed in build metadata.
		'''
		return {
			'channel': self.name,
			'channel_id': self.channel_id,
			'messages': messages,
			**self.provenance.record()
		}


BUILD_RECORD_FORMAT = 1


def build_record(builder, collection, built_at, *, histories=None, outputs=None):
	'''
	The build metadata a builder writes beside its outputs (<builder
	file>-build.json): what it read and from which source, without
	message text or credentials.

	histories -> ChannelHistory.record() of every history read, or None
		for a builder that reads none
	outputs -> {file name: details}
	'''
	return {
		'format': BUILD_RECORD_FORMAT,
		'builder': builder,
		'built_at': built_at,
		'source': collection.source,
		'storage': collection.storage,
		**collection.describe(),
		'histories': histories,
		'outputs': outputs or {}
	}


@dataclass(frozen=True)
class ChatsInput:
	'''
	The collected_chats.csv input: `source` is what pandas reads (a path
	or a text buffer), `label` names it in messages.
	'''
	source: object
	label: str
