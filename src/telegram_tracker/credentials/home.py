# -*- coding: utf-8 -*-
'''
The application home and the session file a command uses.

The application home holds per-user state: credentials.json and, by
default, the Telegram session. It is `~/.telegram_tracker`, or the
absolute directory in TELEGRAM_TRACKER_HOME. Nothing here reads, creates,
or moves files: selection only checks which session files exist.

Session selection, first match wins:
	1. --session PATH
	2. TELEGRAM_TRACKER_SESSION
	3. <home>/session_file.session, when it exists
	4. ./session_file.session in the working directory, when it exists
	   (earlier versions created it there), with a notice
	5. <home>/session_file.session, created by the first login
Relative paths resolve against the working directory and links are
resolved, so one file always has one path (and one lease); a path without
the `.session` suffix gets it, as Telethon would add it.
'''

# import modules
import os

from dataclasses import dataclass


APP_DIR = '.telegram_tracker'
HOME_ENV = 'TELEGRAM_TRACKER_HOME'
SESSION_ENV = 'TELEGRAM_TRACKER_SESSION'
SESSION_NAME = 'session_file.session'
SESSION_SUFFIX = '.session'

# session sources
FLAG = '--session'
HOME = 'application home'
WORKING_DIRECTORY = 'working directory'


class HomeError(Exception):
	'''
	TELEGRAM_TRACKER_HOME or a session path is not usable.
	'''


def app_home(environ=None):
	'''
	(path, label): the application home and where it came from
	('default' or TELEGRAM_TRACKER_HOME). An override must be a non-empty
	absolute path; there is no fallback.
	'''
	environ = os.environ if environ is None else environ
	value = environ.get(HOME_ENV)
	if value is None:
		return os.path.join(os.path.expanduser('~'), APP_DIR), 'default'
	value = value.strip()
	if not value or not os.path.isabs(value):
		raise HomeError(f'{HOME_ENV} must be an absolute directory path')
	return os.path.normpath(value), HOME_ENV


def session_file(path, cwd=None):
	'''
	The session's canonical path: absolute, links and short names
	resolved (so every alias of one file gets the same lease), with the
	`.session` suffix.
	'''
	path = os.fspath(path).strip()
	if not path:
		raise HomeError('the session path is empty')
	path = os.path.join(cwd or os.getcwd(), path)
	if not path.lower().endswith(SESSION_SUFFIX):
		path += SESSION_SUFFIX
	return os.path.realpath(path)


@dataclass(frozen=True)
class SessionChoice:
	'''
	path -> absolute session file path
	source -> FLAG, SESSION_ENV, HOME, or WORKING_DIRECTORY
	in_home -> the path is the application home's default session
	notice -> one line to print when the choice needs explaining
	'''
	path: str
	source: str
	in_home: bool = False
	notice: str = None


def select_session(flag=None, environ=None, cwd=None, isfile=None):
	'''
	The session a command uses (see the module docstring). `isfile`
	replaces the existence check (tests).
	'''
	environ = os.environ if environ is None else environ
	isfile = isfile or os.path.isfile
	if flag is not None:
		return SessionChoice(session_file(flag, cwd), FLAG)
	value = environ.get(SESSION_ENV)
	if value is not None and value.strip():
		return SessionChoice(session_file(value, cwd), SESSION_ENV)

	home, _ = app_home(environ)
	default = os.path.realpath(os.path.join(home, SESSION_NAME))
	if isfile(default):
		return SessionChoice(default, HOME, in_home=True)

	legacy = os.path.realpath(os.path.join(cwd or os.getcwd(), SESSION_NAME))
	if isfile(legacy):
		return SessionChoice(
			legacy,
			WORKING_DIRECTORY,
			notice=(
				f'> Using {SESSION_NAME} from the working directory (there is none '
				f'in {home}); pass --session to choose another.'
			)
		)
	return SessionChoice(default, HOME, in_home=True)


def is_within(path, folder):
	'''
	True when `path` is `folder` or inside it, comparing canonical paths
	(links resolved; case-insensitive where the file system is).
	'''
	path = os.path.normcase(os.path.realpath(path))
	folder = os.path.normcase(os.path.realpath(folder))
	return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)
