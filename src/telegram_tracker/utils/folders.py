# -*- coding: utf-8 -*-
'''
Output and data folder paths given on the command line: trailing
separators are dropped without ever turning a filesystem root into an
empty or drive-relative path, and a root is refused as a folder.

`flavor` is a path module (os.path, ntpath or posixpath), so Windows and
POSIX paths can be checked on any system.
'''

# import modules
import os


def separators(flavor):
	'''
	The flavor's path separators: '/' on POSIX, '\\' and '/' on Windows.
	'''
	return flavor.sep + (flavor.altsep or '')


def data_folder_from(path, default='./output/data', *, flavor=os.path):
	'''
	Data folder without trailing separators; `default` when not given. A
	root keeps its separator: '/' stays '/', 'D:/' stays 'D:/'.
	'''
	if not path:
		return default

	drive, root, tail = flavor.splitroot(path)
	return drive + root + tail.rstrip(separators(flavor))


def is_filesystem_root(path, *, flavor=os.path):
	'''
	Whether `path` names a filesystem root: '/', '//', or on Windows 'D:\\',
	'D:/', '\\' and a UNC share ('\\\\server\\share'), with or without
	trailing separators. A bare drive ('D:') is that drive's current
	directory, not a root.
	'''
	drive, root, tail = flavor.splitroot(path)
	if tail.rstrip(separators(flavor)):
		return False
	# a UNC or device drive ('\\server\share', '\\?\D:') is a root on its own
	return bool(root) or drive.startswith(tuple(separators(flavor)))


def root_folder_refusal(folder, role, *, flavor=os.path):
	'''
	Why `folder` can't be used as the `role` ('output folder', 'data
	folder'), or None when it can. A filesystem root is refused: writing a
	collection's files into it is almost certainly a mistake.
	'''
	if not is_filesystem_root(folder, flavor=flavor):
		return None
	return f'refusing to use a filesystem root as the {role}; choose a subfolder, e.g. /data/telegram'
