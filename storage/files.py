# -*- coding: utf-8 -*-
'''
Output file writes. JSON and text files are replaced through a sibling
temporary file and os.replace, so a reader sees the old or the new file;
exception logs are appended directly. OSError becomes StorageError.
'''

# import modules
import json
import os

# import local submodules
from storage.records import StorageError
from utils import discard, temp_path_for


def to_json_text(data):
	'''
	Compact UTF-8 JSON.
	'''
	return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def replace_file(path, write):
	'''
	Call write(temp_path), then move the temporary file over `path`.
	OSError becomes StorageError; the temporary file never survives.
	'''
	tmp = temp_path_for(path)
	try:
		write(tmp)
		os.replace(tmp, path)
	except OSError as exc:
		raise StorageError(f'cannot write {path}: {exc}') from exc
	finally:
		discard(tmp)


def write_text(path, text):
	def write(tmp):
		with open(tmp, mode='w', encoding='utf-8') as writer:
			writer.write(text)

	replace_file(path, write)


def write_json(path, data):
	write_text(path, to_json_text(data))


def write_json_pretty(path, data):
	write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + '\n')


def append_lines(path, lines):
	if not lines:
		return
	try:
		with open(path, mode='a', encoding='utf-8') as writer:
			for line in lines:
				writer.write(f'{line}\n')
	except OSError as exc:
		raise StorageError(f'cannot write {path}: {exc}') from exc
