# -*- coding: utf-8 -*-
'''
The SQLite data database (docs/sqlite-schema.md): the same collected
data as the file outputs, plus a record of every collection run, in one
schema version 1 file.

Modules:

	schema      identity, the version 1 DDL, and classifying a file from
	            its header before anything connects to it
	connection  opening and creating databases, and transactions
	migrations  explicit offline upgrades, each after a checked backup
	mapping     payloads to rows and stored rows back to payloads
	histories   SQLiteHistories: --update against the stored histories
	writer      SQLiteWriter: one run's channel and aggregate writes
	lease       the database lease held by collections and upgrades

The collector reaches the database only through a writer factory:
storage.destination opens it for `collect --storage sqlite|both`, under
the database lease. Standard library only; importing any module opens
nothing.
'''
