# -*- coding: utf-8 -*-
'''
Offline reading of a collection folder for the builders: the same
records from the file outputs or from the database.

Modules:

	records   the records a reader yields and the --source values
	select    choosing the source and the gates before reading
	files     the file outputs
	database  the database, read only, under its lease

	with open_collection(data_path, source) as collection:
		for history in collection.histories():
			payload = history.load()
		chats = collection.collected_chats()

Nothing here authenticates, opens a session or imports a Telegram
client. The database modules load only when the database is read.
'''

# import local submodules
from telegram_tracker.reader.records import AUTO, FILES, SOURCES, SQLITE
from telegram_tracker.reader.select import open_collection
