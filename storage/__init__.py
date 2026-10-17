# -*- coding: utf-8 -*-
'''
Collection output: the single owner of counters, aggregate files, channel
JSON, the output-folder lock, and the collection-status.json sidecar.

Workers hand finished `ChannelResult`s to an `OutputWriter`, which writes
each channel's JSON at once and keeps its chat observations. Aggregates
are computed at finalization by replaying the committed results in input
order, so they do not depend on the order in which channels finished:

	chats.txt            earlier ids + this run's, deduplicated (first occurrence)
	counter.csv          this run's counts
	collected_chats.csv  earlier rows unchanged + this run's rows with this
	                     run's counts (no _x/_y columns on a rerun)
	_exceptions-*.txt    appended

JSON, CSV, and chats.txt files are replaced through sibling temporary
files and os.replace, so a reader sees the old or the new file. Exception
logs are appended directly. Atomicity is per replaced file: a hard crash
can leave files of different generations, and collection-status.json then
still says "running".

Modules:

	records     file and column names, sidecar states, errors, and the
	            results workers hand to the writer
	aggregates  counters and aggregate files
	files       atomic file writes and saved message histories
	writer      OutputWriter and the output-folder lock
	status      collection-status.json and the builders' gate

Importing any of them has no side effects and loads neither Telethon nor
pandas (pandas loads when aggregates are written). The package exports
only what the CLI and the builders use.
'''

# import local submodules
from storage.records import OutputLocked, StorageError
from storage.status import check_collection_status
