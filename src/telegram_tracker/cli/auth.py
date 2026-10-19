# -*- coding: utf-8 -*-
'''
`auth login|status|logout|clear-credentials|profiles`.

Except for the inventory action `profiles`, each action works on one
profile (--profile, TELEGRAM_TRACKER_PROFILE, or the default; see
credentials/profiles.py). Only login creates a named profile; other
single-profile actions refuse a profile that does not exist.

login: resolve the session and app credentials, ask for missing ones,
log in with the shared code/two-step lifecycle, check the logged-in
account (get_me) against the phone number and the profile's binding,
record the profile in profile.json, then save credentials (see
save_plan). status: offline report of the home, profile, session, and
credential sources by label; --check connects to verify the login.
logout: revoke the session on Telegram. clear-credentials: delete the
profile's credentials.json only. profiles: offline list of profiles.

Telethon is loaded only by the actions that connect (login, status
--check, logout). Each holds the session lease while its one client
exists and disconnects it once. Credential values and account IDs are
never printed.
'''

# import modules
import contextlib
import functools
import getpass
import os
import sys
import warnings

# import local submodules
from telegram_tracker.cli import profile_checks
from telegram_tracker.cli.builders import missing_dependency
from telegram_tracker.cli.parsers import AUTH, CLEAR_CREDENTIALS, LOGIN, LOGOUT, PROFILES, STATUS
from telegram_tracker.credentials import accounts, profiles
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources
from telegram_tracker.credentials.identity import IdentityError, read_identity
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.credentials.protection import ProtectionError
from telegram_tracker.credentials.store import FIELDS, StoreError, clear_store, \
	read_store, save_store, store_path


MAX_ATTEMPTS = 3

FIELD_PROMPTS = {
	'api_id': 'API ID: ',
	'api_hash': 'API hash (not shown): ',
	'phone': 'Phone number, international format: '
}

# what login does with the credentials it used
SAVE, ASK, SKIP, UNCHANGED = 'save', 'ask', 'skip', 'unchanged'


class InputUnavailable(Exception):
	'''
	A required answer could not be read or was rejected too often.
	'''


def fail(message, code):
	print(f'> {message}', file=sys.stderr)
	return code


'''

Prompts (replaced in tests)

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


'''

Dispatch

'''

def run_auth(args, *, client_factory=None, authorize=None):
	'''
	Run one auth action. client_factory (zero-argument, returns an
	unconnected client) and authorize (awaited with the connected client)
	replace Telethon and the interactive login (tests).
	'''
	action = args.pop('auth_command')
	handler = {
		LOGIN: login,
		STATUS: status,
		LOGOUT: logout,
		CLEAR_CREDENTIALS: clear_credentials,
		PROFILES: list_profiles
	}[action]
	try:
		return handler(args, client_factory=client_factory, authorize=authorize)
	except ModuleNotFoundError as exc:
		return missing_dependency(AUTH, exc)
	except (homes.HomeError, sources.CredentialError) as exc:
		return fail(exc, 2)
	except InputUnavailable as exc:
		return fail(exc, 1)
	except KeyboardInterrupt:
		print('> Interrupted.', file=sys.stderr)
		return 130


def _connect(session, factory, work):
	'''
	Under the session lease, create one client, connect it, await
	work(client, closed), and disconnect the client once, unless work
	marked it closed (closed['done'] = True) because Telethon already
	disconnected it. Returns (exit code or None, work's result).
	'''
	try:
		from telegram_tracker.api.auth import AuthenticationError
		from telegram_tracker.cli import collect
	except ModuleNotFoundError as exc:
		return missing_dependency(AUTH, exc), None

	async def run():
		client = factory()
		closed = {'done': False}
		try:
			await client.connect()
			return await work(client, closed)
		finally:
			if not closed['done']:
				await client.disconnect()

	try:
		with Lease(session.path, 'The session'):
			return None, collect.asyncio_run(run())
	except (LeaseError, accounts.AccountError) as exc:
		return fail(exc, 1), None
	except AuthenticationError as exc:
		return fail(f'Login failed: {exc}', 1), None
	except (OSError, ConnectionError) as exc:
		return fail(f'Could not reach Telegram ({type(exc).__name__}).', 1), None


def _client_factory(session, resolution, client_factory):
	if client_factory is not None:
		return client_factory
	from telegram_tracker.api import create_client

	return functools.partial(
		create_client, session.path, resolution.values['api_id'], resolution.values['api_hash']
	)


def _require_app_id(resolution):
	missing = [f for f in ('api_id', 'api_hash') if f not in resolution.values]
	if missing:
		raise sources.CredentialError(
			f'missing {", ".join(missing)}; run `auth login`, set the '
			'TELEGRAM_TRACKER_API_* variables, or pass --config'
		)


'''

login

'''

def login(args, *, client_factory=None, authorize=None):
	home, _ = homes.app_home()
	profile = profiles.select_profile(args.get('profile'), home=home)
	profiles.check_folder(profile)
	session = profiles.select_session(profile, args.get('session'))
	if session.notice:
		print(session.notice)
	resolution = _resolve(profile, args)

	existing = None
	if not args['no_save']:
		try:
			existing = read_store(profile.root)
		except StoreError as exc:
			return fail(f'{exc}', 2)

	if resolution.missing:
		if not args['no_save']:
			print(
				f'> Values you enter are saved to {profile.store_path} after a '
				'successful login (plaintext, readable only by your account). '
				'Use --no-save to skip.'
			)
		ask_missing(resolution)
	print(f'> Profile: {profile.label()}')
	print('> Credentials: ' + '; '.join(resolution.labels()))

	# The default profile's record is optional: a home that cannot be used
	# only stops a login that has to write there.
	plan = save_plan(args, resolution, existing)
	creating_session = session.in_home and not os.path.isfile(session.path)
	needs_root = plan in (SAVE, ASK) or creating_session or not profile.is_default
	try:
		profiles.prepare_root(profile)
		root_ready = True
	except ProtectionError as exc:
		if needs_root:
			hint = ' or pass --session' if profile.is_default else ''
			return fail(
				f'Cannot use {profile.root}: {exc}. Set TELEGRAM_TRACKER_HOME{hint}'
				+ (', or use --no-save' if not creating_session and profile.is_default else '') + '.',
				1
			)
		root_ready = False

	if authorize is None:
		from telegram_tracker.api.auth import ensure_authorized

		authorize = functools.partial(ensure_authorized, phone=resolution.values['phone'])
	factory = _client_factory(session, resolution, client_factory)

	async def work(client, closed):
		await authorize(client)
		try:
			return await client.get_me()
		except Exception as exc:
			raise accounts.AccountError(
				f'Logged in, but the account could not be checked ({type(exc).__name__}); '
				'nothing was saved.'
			) from None

	code, me = _connect(session, factory, work)
	if code is not None:
		return code
	print(f'> Session: {session.path}')

	try:
		profile_checks.record_login(profile, session, resolution, me, root_ready)
	except accounts.AccountError as exc:
		return fail(exc, 1)
	return finish_save(plan, profile.root, resolution, existing)


def _resolve(profile, args):
	return sources.resolve(
		config=args.get('config'),
		home=profile.root,
		discover_legacy=profile.is_default
	)


def ask_missing(resolution):
	'''
	Ask for each missing field; at most MAX_ATTEMPTS answers each.
	'''
	for field in list(resolution.missing):
		ask = prompt_secret if field == 'api_hash' else prompt_text
		for attempt in range(1, MAX_ATTEMPTS + 1):
			try:
				value = ask(FIELD_PROMPTS[field])
			except EOFError:
				raise InputUnavailable(
					f'No {field} was entered because input is unavailable; run the '
					'command from an interactive terminal.'
				) from None
			try:
				resolution.add(field, value, 'Input', sources.PROMPT)
				break
			except sources.CredentialError as exc:
				print(f'> {exc} ({attempt}/{MAX_ATTEMPTS}).')
		else:
			raise InputUnavailable(f'The {field} was not accepted after {MAX_ATTEMPTS} attempts.')


def save_plan(args, resolution, existing):
	'''
	SKIP with --no-save; UNCHANGED when the saved set is the one used;
	SAVE with --save, or when every value was typed now and nothing is
	saved yet; otherwise ASK (values from the environment or an INI file,
	or replacing a different saved set).
	'''
	if args['no_save']:
		return SKIP
	if existing == resolution.values:
		return UNCHANGED
	if args['save']:
		return SAVE
	typed = all(resolution.sources.get(f) == sources.PROMPT for f in FIELDS)
	return SAVE if typed and existing is None else ASK


def finish_save(plan, home, resolution, existing):
	path = store_path(home)
	if plan == SKIP:
		print('> Credentials not saved (--no-save).')
		return 0
	if plan == UNCHANGED:
		print(f'> Saved credentials unchanged ({path}).')
		return 0
	values = resolution.values
	if plan == ASK:
		if existing is not None:
			changed = [
				f'{f} (from {resolution.sources[f]})' for f in FIELDS if existing.get(f) != values[f]
			]
			question = (
				f'Replace the credentials saved in {path}? Changed: {", ".join(changed)}.'
			)
		else:
			question = (
				f'Save the credentials used now to {path} (plaintext, readable only '
				'by your account)?'
			)
		answer = confirm(question)
		if not answer:
			hint = ' Pass --save to save them without asking.' if answer is None else ''
			print(f'> Credentials not saved.{hint}')
			return 0
	try:
		save_store(home, values)
	except StoreError as exc:
		return fail(f'Logged in, but the credentials were not saved: {exc}', 1)
	print(f'> Credentials saved to {path}.')
	return 0


'''

status, logout, clear-credentials

'''

def status(args, *, client_factory=None, authorize=None):
	home, home_label = homes.app_home()
	profile = profiles.select_profile(args.get('profile'), home=home)
	print(f'Application home: {home} ({home_label})')
	print(f'Profile: {profile.label()}')
	profiles.require(profile)
	session = profiles.select_session(profile, args.get('session'))
	present = os.path.isfile(session.path)
	state = 'present; login not verified (offline)' if present else 'missing'
	print(f'Session: {session.path} ({session.source}): {state}')
	if session.notice:
		print(session.notice)

	errors = 0
	try:
		record = 'present' if read_identity(profile.root, profile.name) else 'absent'
	except IdentityError as exc:
		record, errors = f'invalid: {exc}', 1
	print(f'Profile record: {profile.identity_path}: {record}')
	try:
		saved = 'present' if read_store(profile.root) is not None else 'absent'
	except StoreError as exc:
		saved, errors = f'invalid: {exc}', 1
	print(f'Saved credentials: {profile.store_path}: {saved}')

	try:
		resolution = _resolve(profile, args)
	except sources.CredentialError as exc:
		print(f'Credentials: error: {exc}')
		return 2
	print('Credentials in use: ' + '; '.join(resolution.labels()))
	for note in resolution.notes:
		print(f'Note: {note}')
	if errors:
		return 2
	if not args['check']:
		return 0

	if not present:
		return fail(f'No session at {session.path}; a check never creates one.', 1)
	_require_app_id(resolution)
	factory = _client_factory(session, resolution, client_factory)

	async def work(client, closed):
		return await client.is_user_authorized()

	code, authorized = _connect(session, factory, work)
	if code is not None:
		return code
	print('Login: ' + ('logged in' if authorized else 'not logged in'))
	return 0 if authorized else 1


def logout(args, *, client_factory=None, authorize=None):
	profile = profiles.select_profile(args.get('profile'))
	profiles.require(profile)
	session = profiles.select_session(profile, args.get('session'))
	if not os.path.isfile(session.path):
		print(f'> No session at {session.path}; nothing to log out.')
		return 0
	resolution = _resolve(profile, args)
	_require_app_id(resolution)

	if not args['yes']:
		answer = confirm(
			f'Log out {session.path} on Telegram? This revokes the session and '
			'deletes its file; saved credentials stay.'
		)
		if not answer:
			print('> Not logged out.')
			return 1
	factory = _client_factory(session, resolution, client_factory)

	async def work(client, closed):
		if not await client.is_user_authorized():
			return None
		revoked = await client.log_out()
		# a successful log_out has disconnected the client and deleted the
		# session file
		closed['done'] = bool(revoked)
		return revoked

	code, revoked = _connect(session, factory, work)
	if code is not None:
		return code
	if revoked is None:
		print('> The session is not logged in; nothing to revoke.')
		return 0
	if not revoked:
		return fail('Telegram did not confirm the logout; the session may still be valid.', 1)
	print('> Logged out; the session is revoked.')
	return 0


def clear_credentials(args, *, client_factory=None, authorize=None):
	profile = profiles.select_profile(args.get('profile'))
	profiles.require(profile)
	option = '' if profile.is_default else f' --profile {profile.name}'
	path = store_path(profile.root)
	if not os.path.lexists(path):
		print(f'> No saved credentials at {path}; nothing to remove.')
		return 0
	if not args['yes']:
		answer = confirm(f'Delete the saved credentials in {path}? The session stays logged in.')
		if not answer:
			print('> Nothing removed.')
			return 1

	# A named profile's own session lease keeps this from running while the
	# profile collects or logs in.
	session = None if profile.is_default else profiles.own_session(profile)
	try:
		with Lease(session, 'The session') if session else contextlib.nullcontext():
			clear_store(profile.root)
	except (StoreError, LeaseError) as exc:
		return fail(exc, 1)
	print(f'> Removed {path}.')
	if os.path.lexists(profile.identity_path):
		print(f'> The profile record {profile.identity_path} stays.')

	variables = [v for v in sources.ENV_VARS.values() if os.environ.get(v, '').strip()]
	if variables:
		print(f'> Still set in the environment: {", ".join(variables)}.')
	if profile.is_default and os.path.isfile(sources.LEGACY_INI):
		print(f'> {sources.LEGACY_CONFIG} in the working directory is not touched.')
	session = session or homes.select_session(None).path
	if os.path.isfile(session):
		print(f'> The session {session} stays logged in; `auth logout{option}` revokes it.')
	return 0


def list_profiles(args, *, client_factory=None, authorize=None):
	return profile_checks.print_profiles()
