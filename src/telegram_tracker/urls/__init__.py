# -*- coding: utf-8 -*-
'''
Offline URL occurrence builder (`build-urls`).

Modules:

	parse    one URL: classification, normalization, registered domain
	extract  the URL occurrences of one message
	outputs  urls_dataset.csv, urls.sqlite3 and urls-build.json
	build    reading a collection folder and publishing the outputs

	from telegram_tracker.urls.build import build_urls
	build_urls('./output/data')

Importing this package loads none of them; tldextract loads when a
build starts.
'''
