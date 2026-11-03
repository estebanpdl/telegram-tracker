# -*- coding: utf-8 -*-
'''
Terminal input for app credentials and confirmations, and whether there
is a terminal to ask. Secrets require a terminal that supports no-echo
input; confirmations default to no.
'''

import getpass
import io
import os
import sys
import warnings


class InputUnavailable(Exception):
	'''
	A required answer could not be read or was rejected too often.
	'''


def prompt_text(prompt):
	return input(prompt)


def prompt_secret(prompt):
	'''
	Read a value without echo; refuse getpass's echoing fallback.
	'''
	with warnings.catch_warnings():
		warnings.simplefilter('error', getpass.GetPassWarning)
		try:
			return getpass.getpass(prompt)
		except getpass.GetPassWarning:
			raise InputUnavailable(
				'Cannot read the API hash without echo; run the command from an '
				'interactive terminal.'
			) from None


def confirm(question):
	'''
	True for yes, False for anything else, None when input is unavailable.
	'''
	try:
		answer = input(f'{question} [y/N] ')
	except EOFError:
		return None
	return answer.strip().lower() in ('y', 'yes')


def interactive():
	'''
	True when standard input is a terminal that can answer a question.
	On Windows the NUL device also reports isatty(), so a standard input
	with a file descriptor must also be a console there.
	'''
	try:
		if sys.stdin is None or not sys.stdin.isatty():
			return False
		fd = sys.stdin.fileno()
	except io.UnsupportedOperation:
		# a stream without a descriptor: only its isatty() can tell
		return True
	except (OSError, ValueError):
		return False
	return os.name != 'nt' or windows_console(fd)


def windows_console(fd):
	import ctypes
	import msvcrt

	try:
		handle = msvcrt.get_osfhandle(fd)
	except OSError:
		return False
	mode = ctypes.c_ulong()
	return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
