# -*- coding: utf-8 -*-
'''
Terminal input for app credentials and confirmations. Secrets require
a terminal that supports no-echo input; confirmations default to no.
'''

import getpass
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
