# -*- coding: utf-8 -*-
'''
Downloaded media: which files a collection's messages refer to
(classify), where they are stored in the output folder (layout), the
ledger that records them (ledger_schema, ledger), the dataset identity
that binds the ledger to the collected data (identity), leftover
partial files (partials), and user byte limits (limits).

Downloading them: where a file lives on Telegram (location), its chunks
(stream), staging and publication (staging), and the adapter that takes
a requested ledger item to a stored file (download). Only location,
stream and download load Telethon; the other modules stay offline.
'''
