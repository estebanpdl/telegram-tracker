# -*- coding: utf-8 -*-
'''
Profile steps shared by the commands: the account check after `auth
login`, the account check before `collect` sends channel requests, and
the `auth profiles` listing. Account IDs and phone numbers are compared,
never printed.
'''

# import modules
import contextlib
import os
import sys

# import local submodules
from telegram_tracker.credentials import accounts, profiles
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials.identity import AccountMismatch, IdentityError, ensure_identity


def _digits(value):
	return ''.join(c for c in str(value or '') if c.isdigit())


def record_login(profile, session, resolution, me, root_ready):
	'''
	After a successful login with `resolution`'s credentials, `me` being
	get_me's answer: refuse a session logged in to another account than
	the phone number in use or the profile's binding, then record the
	profile in profile.json (binding the account only through the
	profile's own session). Raises accounts.AccountError to refuse; nothing
	is written then. The default profile's record is optional: without a
	usable home (`root_ready` False) or when it cannot be written, a note
	is printed instead.
	'''
	option = '' if profile.is_default else f' --profile {profile.name}'
	account_id = getattr(me, 'id', None)
	if type(account_id) is not int or account_id <= 0:
		raise accounts.AccountError(
			'Logged in, but Telegram did not return the account; nothing was saved.'
		)
	own_phone = _digits(getattr(me, 'phone', None))
	if own_phone and own_phone != _digits(resolution.values['phone']):
		raise accounts.AccountError(
			'The session is logged in to a different account than the phone number '
			f'in use ({resolution.sources["phone"]}). Run `auth logout{option}` first to '
			'log in with that number; nothing was saved.'
		)
	if not root_ready:
		print('> No profile record: the application home cannot be used.')
		return
	managed = profiles.is_managed(session)
	try:
		ensure_identity(profile.root, profile.name, account_id if managed else None)
	except AccountMismatch:
		raise accounts.AccountError(accounts.mismatch_message(profile) + ' Nothing was saved.') from None
	except IdentityError as exc:
		if not profile.is_default:
			raise accounts.AccountError(
				f'Logged in, but profile {profile.name} was not recorded: {exc}'
			) from None
		print(f'> No profile record: {exc}')
		return
	print(
		f'> Profile record: {profile.identity_path}'
		+ ('' if managed else ' (account not bound: the session was chosen with '
			'--session or TELEGRAM_TRACKER_SESSION)')
	)


def account_check(profile, session, home, run_profile):
	'''
	The collection's `identify` step (see collector.run_collection):
	verify the account with get_me, check it against the profile's
	binding, and return the account lease, whose value is the run's
	provenance. `run_profile` is storage.provenance.RunProfile, passed in
	so this module loads no storage code.
	'''
	async def identify(client):
		account_id = await accounts.verified_account_id(client)
		profile_id, notice = accounts.decide_profile_id(profile, session, account_id)
		if notice:
			print(notice, file=sys.stderr)
		return _holding(
			accounts.lease_for(home, account_id, profile.name),
			run_profile(profile.name, profile_id)
		)

	return identify


@contextlib.contextmanager
def _holding(lease, value):
	with lease:
		yield value


def print_profiles():
	'''
	`auth profiles`: the application home, then one line per profile with
	the presence of its record, credentials, and session. Exit code 0.
	'''
	home, home_label = homes.app_home()
	print(f'Application home: {home} ({home_label})')
	entries, skipped = profiles.list_profiles(home)
	for entry in entries:
		if entry.name == profiles.DEFAULT:
			name = f'{entry.name} (the application home itself)'
		elif not entry.identity:
			name = f'{entry.name} (not set up: run `auth login --profile {entry.name}`)'
		else:
			name = entry.name
		print(
			f'{name}: profile record {"present" if entry.identity else "absent"}; '
			f'credentials {"present" if entry.credentials else "absent"}; '
			f'session {"present" if entry.session else "missing"}'
		)
	if skipped:
		print(
			f'Note: {skipped} folder(s) in {os.path.join(home, profiles.PROFILES_DIR)} '
			'are not profiles (invalid names or links) and were ignored.'
		)
	return 0
