# -*- coding: utf-8 -*-
'''
Messages for failures that happen on this computer rather than at
Telegram: a file that is missing, unreadable, or unwritable, and the
session file, which Telethon keeps as an SQLite database.

`file_reason` turns the error of a file operation into a short reason.
`client_failure` sorts an error that escaped the Telegram client into a
session, local file, or network message. The storage layer turns errors
of the output database into StorageError, so an sqlite3 error that
reaches the client's caller comes from the session. Messages name paths
and reasons, never credential values, and never suggest deleting the
session or logging in again.
'''

# import modules
import configparser
import errno
import sqlite3


REASONS = {
	errno.ENOENT: 'not found',
	errno.EACCES: 'permission denied',
	errno.EPERM: 'permission denied',
	errno.EISDIR: 'it is a folder',
	errno.ENOTDIR: 'a part of the path is not a folder',
	errno.EROFS: 'read-only file system',
	errno.ENOSPC: 'no space left on the device'
}


def file_reason(exc):
	'''
	A short reason for a failed file read or write: "not found",
	"permission denied", "not UTF-8 text", "not valid JSON", ... Unknown
	errors fall back to the system's text or the error type.
	'''
	if isinstance(exc, UnicodeDecodeError):
		return 'not UTF-8 text'
	if isinstance(exc, configparser.Error):
		return 'not a valid INI file'
	if isinstance(exc, ValueError):
		return 'not valid JSON'
	if isinstance(exc, OSError):
		if exc.errno in REASONS:
			return REASONS[exc.errno]
		if exc.strerror:
			return exc.strerror.rstrip('.').lower()
	return type(exc).__name__


def sqlite_code(exc):
	'''
	The SQLite result code name of an sqlite3 error (SQLITE_BUSY,
	SQLITE_READONLY, ...), or one derived from its message when the error
	carries none.
	'''
	name = getattr(exc, 'sqlite_errorname', None)
	if name:
		return name
	text = str(exc).lower()
	for fragment, code in (
			('database is locked', 'SQLITE_BUSY'),
			('readonly database', 'SQLITE_READONLY'),
			('unable to open database', 'SQLITE_CANTOPEN'),
			('not a database', 'SQLITE_NOTADB'),
			('malformed', 'SQLITE_CORRUPT')):
		if fragment in text:
			return code
	return ''


def session_failure(exc, path):
	'''
	The message for an sqlite3 error raised by the session file at `path`.
	'''
	code = sqlite_code(exc)
	detail = f'(SQLite: {exc})'
	if code.startswith('SQLITE_BUSY') or code.startswith('SQLITE_LOCKED'):
		return (
			f'The session file {path} is locked by another program {detail}. '
			'Close the program that has it open, then try again.'
		)
	if code.startswith('SQLITE_READONLY'):
		return (
			f'The session file {path} cannot be written {detail}. Make the file '
			'and its folder writable for your account.'
		)
	if code == 'SQLITE_CANTOPEN_ISDIR':
		return f'The session path {path} is a folder, not a session file {detail}.'
	if code.startswith('SQLITE_CANTOPEN'):
		return (
			f'The session file {path} cannot be opened {detail}. Check that its '
			'folder exists and that your account can read and write it.'
		)
	if code.startswith('SQLITE_NOTADB') or code.startswith('SQLITE_CORRUPT'):
		return f'The session file {path} is damaged or not a session file {detail}.'
	return f'The session file {path} cannot be used {detail}.'


def client_failure(exc, session_path):
	'''
	The message for an error that escaped the Telegram client or its
	session, or None for an error this module does not describe: an
	sqlite3 error is the session's, an OSError naming a file is local,
	and any other OSError (ConnectionError, TimeoutError, socket errors)
	is the network's.
	'''
	if isinstance(exc, sqlite3.Error):
		return session_failure(exc, session_path)
	if isinstance(exc, OSError):
		if exc.filename is not None and not isinstance(exc, ConnectionError):
			return f'A local file cannot be used: {exc.filename} ({file_reason(exc)}).'
		return f'Could not reach Telegram ({type(exc).__name__}).'
	return None
