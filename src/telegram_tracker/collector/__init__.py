# -*- coding: utf-8 -*-
'''
Asynchronous channel collection.

`run_collection` owns one client for the whole run. It creates the client
through an injected factory inside the running event loop, connects,
authorizes once, collects the targets with at most `concurrency` channel
workers (pages of one channel stay sequential), finalizes the aggregate
files, and disconnects exactly once after success, failure, or
cancellation.

Boundaries:
	- network: `Requests` (application RPCs by method class, all admitted
	  through one `request_policy.RequestPolicy`) and `MetadataCache`
	  (shares requests in flight and caches successful responses and
	  permanent access errors by chat for the run);
	- collection: `collect_channel` returns a `storage.records.ChannelResult`
	  and writes nothing;
	- files: `storage.writer.OutputWriter` writes channel JSON and aggregates;
	  `storage.writer.OutputLock` guards the output folder, and
	  `storage.status.StatusFile` writes collection-status.json.

Modules:

	run       run_collection and the worker/writer orchestration
	channel   collect_channel: one target's collection
	history   HistoryWindow and history paging
	rpc       Requests, MetadataCache, and chat enrichment
	outcomes  errors, CollectionSummary, and stop reasons

Importing any of them is inert: no event loop, argv parsing, config reads,
connections, or writes. The package exports only what the CLI uses.
'''

# import local submodules
from telegram_tracker.collector.history import HistoryWindow
from telegram_tracker.collector.run import run_collection
