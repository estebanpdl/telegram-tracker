# -*- coding: utf-8 -*-
'''
Helpers for the build-urls tests: running the builder quietly and its
outputs in comparable form. Synthetic data only.
'''

# import modules
import contextlib
import csv
import io
import json
import os
import sqlite3

import source_path  # noqa: F401  (imports telegram_tracker from src)

from builder_parity_support import read_bytes

from telegram_tracker.urls.build import build_urls
from telegram_tracker.urls.outputs import BUILD_FILE, URLS_DATABASE, URLS_DATASET


URL_OUTPUTS = (URLS_DATASET, URLS_DATABASE, BUILD_FILE)
TABLES = ('build', 'channels', 'domains', 'channel_domains', 'occurrences')

# source-specific fields: history provenance differs between the files
# (no run) and the database (the run that wrote the history)
SOURCE_COLUMNS = ('history_run',)


def run_build(out, source='auto'):
	'''
	build_urls on `out`. Returns (summary, stdout, stderr).
	'''
	stdout, stderr = io.StringIO(), io.StringIO()
	with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
		summary = build_urls(out, source=source)
	return summary, stdout.getvalue(), stderr.getvalue()


def csv_rows(path):
	'''
	urls_dataset.csv as a list of dicts (every value text).
	'''
	with open(path, encoding='utf-8', newline='') as fh:
		return list(csv.DictReader(fh))


def tables(path):
	'''
	{table: [row dicts]} of a urls.sqlite3, each table in key order.
	'''
	con = sqlite3.connect(path)
	con.row_factory = sqlite3.Row
	try:
		return {
			table: [dict(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY 1, 2')]
			for table in TABLES
		}
	finally:
		con.close()


def record(out):
	with open(os.path.join(out, BUILD_FILE), encoding='utf-8') as fh:
		return json.load(fh)


def comparable(out):
	'''
	The outputs of a build without what differs by build (UUID, time) or
	by source (history runs, the source fields, the CSV hash, which
	covers the history runs).
	'''
	rows = csv_rows(os.path.join(out, URLS_DATASET))
	for row in rows:
		for column in SOURCE_COLUMNS:
			row.pop(column)
	db = tables(os.path.join(out, URLS_DATABASE))
	for row in db['occurrences']:
		for column in SOURCE_COLUMNS:
			row.pop(column)
	build, = db.pop('build')
	build = {k: v for k, v in build.items() if k not in ('build_uuid', 'built_at', 'source', 'storage',
		'collection')}
	built = record(out)
	for key in ('built_at', 'build_uuid', 'source', 'storage', 'database', 'last_run'):
		built.pop(key)
	built['outputs'][URLS_DATASET].pop('sha256')
	for history in built['histories']:
		for key in ('run', 'mode', 'takeover'):
			history.pop(key)
	return {'csv': rows, 'db': db, 'build': build, 'record': built}


def inputs(out):
	'''
	{relative path: bytes} of every file in `out` but the build-urls
	outputs.
	'''
	contents = {}
	for root, _, files in os.walk(out):
		for name in files:
			rel = os.path.relpath(os.path.join(root, name), out).replace(os.sep, '/')
			if rel not in URL_OUTPUTS:
				contents[rel] = read_bytes(os.path.join(root, name))
	return contents


def output_bytes(out):
	return {name: read_bytes(os.path.join(out, name)) for name in URL_OUTPUTS
		if os.path.exists(os.path.join(out, name))}


def leftovers(out):
	'''
	Temporary and lock files left in `out`.
	'''
	return sorted(n for n in os.listdir(out) if '.tmp' in n or n.endswith('.lock'))
