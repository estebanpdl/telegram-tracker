# -*- coding: utf-8 -*-
'''
Account profiles: which credentials, session, and profile record a
command uses.

Selection, first match wins: --profile NAME, TELEGRAM_TRACKER_PROFILE,
then `default`. Nothing remembers a selection between commands.

	default        the application home itself: <home>/credentials.json,
	               the session chosen as in home.py, <home>/profile.json
	<name>         <home>/profiles/<name>/: credentials.json,
	               session_file.session, profile.json

A name is a lowercase ASCII letter followed by up to 31 lowercase
letters, digits, `_` or `-`; `default` names the default profile, and
Windows device names (con, prn, aux, nul, com1-9, lpt1-9) are refused.
Names are checked before any path is built. A named profile's folder must
not be a link or junction, and must not differ from the name only in
case. A name is a local label, not proof of a different Telegram account.

A named profile uses only its own session (no --session, no
TELEGRAM_TRACKER_SESSION) and only its own credentials.json, the
environment, or an INI file given with --config: never
./config/config.ini, ./session_file.session, or another profile's files.
It exists once `auth login --profile NAME` has created its profile.json.

Nothing here creates files except prepare_root.
'''

# import modules
import os
import re

from dataclasses import dataclass

# import local submodules
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials.identity import IDENTITY_NAME
from telegram_tracker.credentials.protection import ProtectionError, ensure_private_dir, \
	reject_link
from telegram_tracker.credentials.store import STORE_NAME


PROFILE_ENV = 'TELEGRAM_TRACKER_PROFILE'
DEFAULT = 'default'
PROFILES_DIR = 'profiles'

NAME_PATTERN = re.compile(r'[a-z][a-z0-9_-]{0,31}')
DEVICE_NAMES = frozenset(
	['con', 'prn', 'aux', 'nul']
	+ [f'com{i}' for i in range(1, 10)]
	+ [f'lpt{i}' for i in range(1, 10)]
)

# where the selection came from
FLAG = '--profile'
DEFAULT_SOURCE = 'default'

# session source of a named profile
PROFILE_SESSION = 'profile'


class ProfileError(homes.HomeError):
	'''
	A profile name, selection, or folder is not usable (exit 2).
	'''


def check_name(name, label=FLAG):
	'''
	`name` when it is a valid profile name; ProfileError otherwise. The
	message names the rule, not the rejected text.
	'''
	if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
		raise ProfileError(
			f'{label}: a profile name is a lowercase letter followed by up to 31 '
			'lowercase letters, digits, _ or -'
		)
	if name in DEVICE_NAMES:
		raise ProfileError(f'{label}: {name} is a reserved Windows device name')
	return name


@dataclass(frozen=True)
class Profile:
	'''
	name -> DEFAULT or a checked name
	source -> FLAG, PROFILE_ENV, or DEFAULT_SOURCE
	home -> the application home
	'''
	name: str
	source: str
	home: str

	@property
	def is_default(self):
		return self.name == DEFAULT

	@property
	def root(self):
		'''
		Folder holding the profile's credentials.json and profile.json.
		'''
		if self.is_default:
			return self.home
		return os.path.join(self.home, PROFILES_DIR, self.name)

	@property
	def identity_path(self):
		return os.path.join(self.root, IDENTITY_NAME)

	@property
	def store_path(self):
		return os.path.join(self.root, STORE_NAME)

	def label(self):
		return f'{self.name} ({self.source})'


def select_profile(flag=None, environ=None, home=None):
	'''
	The profile a command uses. `home` defaults to the application home.
	Raises ProfileError for an invalid name and HomeError for an invalid
	TELEGRAM_TRACKER_HOME. Touches no path.
	'''
	environ = os.environ if environ is None else environ
	if home is None:
		home, _ = homes.app_home(environ)
	if flag is not None:
		return Profile(check_name(flag), FLAG, home)
	value = environ.get(PROFILE_ENV)
	if value is not None and value.strip():
		return Profile(check_name(value.strip(), PROFILE_ENV), PROFILE_ENV, home)
	return Profile(DEFAULT, DEFAULT_SOURCE, home)


def check_folder(profile):
	'''
	Raise ProfileError when a named profile's folder, or the profiles
	folder above it, is a link or junction, or when the folder on disk
	differs from the name only in case. Reads directory entries only.
	'''
	if profile.is_default:
		return
	parent = os.path.join(profile.home, PROFILES_DIR)
	try:
		reject_link(parent)
		reject_link(profile.root)
	except ProtectionError as exc:
		raise ProfileError(str(exc)) from None
	if not os.path.isdir(parent):
		return
	entries = os.listdir(parent)
	if profile.name not in entries and any(e.lower() == profile.name for e in entries):
		raise ProfileError(
			f'{parent} has a folder whose name differs from profile {profile.name} '
			'only in case; profile names are lowercase'
		)


def exists(profile):
	'''
	True for the default profile, and for a named profile with a
	profile.json.
	'''
	if profile.is_default:
		return True
	check_folder(profile)
	return os.path.lexists(profile.identity_path)


def require(profile):
	'''
	Raise ProfileError unless the profile exists (see exists).
	'''
	if not exists(profile):
		raise ProfileError(
			f'there is no profile {profile.name}; create it with '
			f'`auth login --profile {profile.name}`'
		)


def select_session(profile, flag=None, environ=None, cwd=None):
	'''
	The session a profile uses: the default profile chooses as in home.py;
	a named profile always uses <root>/session_file.session and refuses
	--session and TELEGRAM_TRACKER_SESSION.
	'''
	environ = os.environ if environ is None else environ
	if profile.is_default:
		return homes.select_session(flag, environ=environ, cwd=cwd)
	if flag is not None:
		raise ProfileError(
			f'--session cannot be used with profile {profile.name}: a named '
			'profile always uses its own session'
		)
	value = environ.get(homes.SESSION_ENV)
	if value is not None and value.strip():
		raise ProfileError(
			f'{homes.SESSION_ENV} is set, but profile {profile.name} always uses its '
			f'own session: unset {homes.SESSION_ENV} or use the default profile'
		)
	return homes.SessionChoice(own_session(profile), PROFILE_SESSION, in_home=True)


def own_session(profile):
	'''
	The canonical path of a named profile's own session file.
	'''
	check_folder(profile)
	return homes.session_file(os.path.join(profile.root, homes.SESSION_NAME))


def is_managed(session):
	'''
	True when `session` is its profile's own session rather than one given
	with --session or TELEGRAM_TRACKER_SESSION (possible for the default
	profile only). Only a managed session binds a Telegram account to the
	profile.
	'''
	return session.source not in (homes.FLAG, homes.SESSION_ENV)


def prepare_root(profile):
	'''
	Create the profile's folder (and the home and profiles folders above
	it) if needed, each private to the current user. Raises
	ProtectionError when that is not possible.
	'''
	ensure_private_dir(profile.home)
	if profile.is_default:
		return
	check_folder(profile)
	ensure_private_dir(os.path.join(profile.home, PROFILES_DIR))
	ensure_private_dir(profile.root)


@dataclass(frozen=True)
class Listing:
	'''
	One `auth profiles` entry: presence of the profile's files only.
	'''
	name: str
	identity: bool
	credentials: bool
	session: bool


def list_profiles(home, isfile=None):
	'''
	(entries, skipped): the default profile, then every named profile
	folder in name order; `skipped` counts folders under profiles/ that
	are not usable profile names or are links. Reads directory entries
	and checks file presence; parses and creates nothing.
	'''
	isfile = isfile or os.path.isfile
	default = Profile(DEFAULT, DEFAULT_SOURCE, home)
	entries = [_listing(default, homes.session_file(os.path.join(home, homes.SESSION_NAME)), isfile)]
	parent = os.path.join(home, PROFILES_DIR)
	skipped = 0
	try:
		reject_link(parent)
	except ProtectionError:
		return entries, skipped
	if not os.path.isdir(parent):
		return entries, skipped
	for name in sorted(os.listdir(parent)):
		profile = Profile(name, DEFAULT_SOURCE, home)
		try:
			check_name(name)
			if name == DEFAULT:
				raise ProfileError(name)
			reject_link(profile.root)
		except (ProfileError, ProtectionError):
			skipped += 1
			continue
		if not os.path.isdir(profile.root):
			skipped += 1
			continue
		session = os.path.join(profile.root, homes.SESSION_NAME)
		entries.append(_listing(profile, session, isfile))
	return entries, skipped


def _listing(profile, session, isfile):
	return Listing(
		profile.name,
		os.path.lexists(profile.identity_path),
		os.path.lexists(profile.store_path),
		isfile(session)
	)
