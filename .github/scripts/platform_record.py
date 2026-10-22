# -*- coding: utf-8 -*-
'''
Print, as JSON, the platform a CI job tests: operating system and
architecture, the interpreter and its build, the SQLite library the
sqlite3 module loaded (version, JSON1, generated columns), the pinned
dependency versions, and, for each folder given, its file system type
and whether it ignores case or Unicode normalization and supports
symlinks and hard links. Only the standard library is needed; nothing
outside the given folders is written.

	python platform_record.py FOLDER...
'''

# import modules
import ctypes
import importlib.metadata
import json
import locale
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata


PACKAGES = (
	'telethon', 'pandas', 'openpyxl', 'tqdm', 'networkx', 'matplotlib', 'python-louvain', 'tldextract'
)


def sqlite_record():
	con = sqlite3.connect(':memory:')
	try:
		def works(sql):
			try:
				con.execute(sql)
				return True
			except sqlite3.Error:
				return False

		return {
			'version': sqlite3.sqlite_version,
			'json1': works("SELECT json_extract('{\"a\": 1}', '$.a')"),
			'generated_columns': works(
				"CREATE TABLE t (raw TEXT, a INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.a')) VIRTUAL)"
			),
			'compile_options': [row[0] for row in con.execute('PRAGMA compile_options')],
		}
	finally:
		con.close()


def file_system_type(folder):
	folder = os.path.realpath(folder)
	if os.name == 'nt':
		name = ctypes.create_unicode_buffer(64)
		root = os.path.splitdrive(folder)[0] + '\\'
		ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, name, len(name))
		return name.value if ok else 'unknown'
	if sys.platform == 'darwin':
		lines = subprocess.run(['mount'], capture_output=True, text=True).stdout.splitlines()
		# "<device> on <mount point> (<type>, <options>)"
		parts = (line.split(' on ', 1)[1].rsplit(' (', 1) for line in lines if ' on ' in line)
		mounts = [part for part in parts if len(part) == 2]
	else:
		with open('/proc/mounts', encoding='utf-8') as reader:
			mounts = [(fields[1], fields[2]) for fields in (line.split() for line in reader)]
	best, kind = '', 'unknown'
	for point, rest in mounts:
		inside = folder == point or folder.startswith(point.rstrip('/') + '/')
		if inside and len(point) >= len(best):
			best, kind = point, rest.split(',')[0].strip('()')
	return kind


def folder_record(folder):
	os.makedirs(folder, exist_ok=True)
	with tempfile.TemporaryDirectory(dir=folder) as probe:
		def exists_as(created, other):
			path = os.path.join(probe, created)
			os.mkdir(path)
			try:
				return os.path.isdir(os.path.join(probe, other))
			finally:
				os.rmdir(path)

		def supported(make):
			try:
				make()
				return True
			except (OSError, NotImplementedError):
				return False

		name = 'café'
		target = os.path.join(probe, 'target')
		open(target, 'w').close()
		return {
			'path': os.path.realpath(folder),
			'file_system': file_system_type(folder),
			'ignores_case': exists_as('Probe', 'pROBE'),
			'ignores_unicode_normalization': exists_as(
				unicodedata.normalize('NFC', name), unicodedata.normalize('NFD', name)
			),
			'symlinks': supported(lambda: os.symlink(target, os.path.join(probe, 'symlink'))),
			'hard_links': supported(lambda: os.link(target, os.path.join(probe, 'hardlink'))),
		}


def user_record():
	if os.name == 'nt':
		return {'administrator': bool(ctypes.windll.shell32.IsUserAnAdmin())}
	mask = os.umask(0o022)
	os.umask(mask)
	return {'uid': os.getuid(), 'euid': os.geteuid(), 'umask': oct(mask)}


def versions():
	found = {}
	for name in PACKAGES:
		try:
			found[name] = importlib.metadata.version(name)
		except importlib.metadata.PackageNotFoundError:
			found[name] = None
	return found


def main(folders):
	record = {
		'os': {
			'system': platform.system(), 'release': platform.release(), 'version': platform.version(),
			'machine': platform.machine(), 'platform': platform.platform(),
		},
		'python': {
			'version': sys.version, 'implementation': platform.python_implementation(),
			'build': platform.python_build(), 'compiler': platform.python_compiler(),
			'executable': sys.executable, 'base_executable': getattr(sys, '_base_executable', None),
			'gil_enabled': getattr(sys, '_is_gil_enabled', lambda: True)(),
			'filesystem_encoding': sys.getfilesystemencoding(),
			'preferred_encoding': locale.getpreferredencoding(False),
		},
		'sqlite': sqlite_record(),
		'user': user_record(),
		'packages': versions(),
		'folders': [folder_record(folder) for folder in folders],
	}
	print(json.dumps(record, indent=1))


if __name__ == '__main__':
	main(sys.argv[1:])
