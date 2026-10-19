# -*- coding: utf-8 -*-
'''
One collection per Telegram account, and the account check that comes
first.

Before a collection sends any channel request, it asks Telegram which
account the session is logged in to (one `get_me` request, outside the
request controls) and holds a lease for that account in
<home>/account-leases/ until the run ends. A second collection with the
same account, through another profile or another session file, is
refused there, so two processes never combine their request pacing on one
account. Different accounts may collect at the same time into different
output folders. The coordination covers one application home; a second
home or another client using the same account is not detected.

The lease file name is derived from the account ID by hashing, and its
content names only the process and profile. A lease left by a crashed run
is reported, never removed automatically.

The check also compares the account with the profile's binding in
profile.json (see identity.py and decide_profile_id).
'''

# import modules
import hashlib
import os

# import local submodules
from telegram_tracker.credentials import profiles
from telegram_tracker.credentials.identity import IdentityError, ensure_identity, read_identity
from telegram_tracker.credentials.lease import Lease
from telegram_tracker.credentials.protection import ProtectionError, ensure_private_dir


LEASES_DIR = 'account-leases'


class AccountError(Exception):
	'''
	The account check failed or the account cannot be leased. Ends the run
	before any channel request. Messages never include the account ID.
	'''


def leases_dir(home):
	return os.path.join(home, LEASES_DIR)


def prepare(home):
	'''
	Create the home and its account-leases folder if needed, private to the
	current user. Called before connecting, so a home that cannot hold the
	lease stops the run before login.
	'''
	try:
		ensure_private_dir(home)
		ensure_private_dir(leases_dir(home))
	except ProtectionError as exc:
		raise AccountError(
			f'cannot prepare {leases_dir(home)} for the account lease ({exc}); every '
			'collection needs a writable application home (set TELEGRAM_TRACKER_HOME)'
		) from None


def lease_for(home, account_id, profile_name):
	'''
	The (not yet entered) lease of `account_id`.
	'''
	key = hashlib.sha256(f'telegram-account:{account_id}'.encode('ascii')).hexdigest()[:32]
	return Lease(os.path.join(leases_dir(home), key), 'The Telegram account', profile_name)


async def verified_account_id(client):
	'''
	The Telegram user ID of the logged-in session (one get_me request).
	Raises AccountError when Telegram does not answer with a user.
	'''
	try:
		me = await client.get_me()
	except Exception as exc:
		raise AccountError(
			f'Could not verify the Telegram account ({type(exc).__name__}); no channel '
			'was requested.'
		) from None
	account_id = getattr(me, 'id', None)
	if type(account_id) is not int or account_id <= 0:
		raise AccountError('Telegram did not return the logged-in account; no channel was requested.')
	return account_id


def decide_profile_id(profile, session, account_id):
	'''
	(profile_id or None, notice or None) for a run of `profile` whose
	session is logged in to `account_id`:

	- the profile is bound to this account: its profile ID;
	- it is not bound yet and `session` is its own: the account is bound
	  now (profile.json is created only by `auth login`) and its ID used;
	- it is bound to another account through its own session:
	  AccountError, nothing is written;
	- the default profile with --session or TELEGRAM_TRACKER_SESSION and
	  another or no binding: no profile ID for this run, and the binding is
	  not changed (notice for another binding).
	'''
	try:
		identity = read_identity(profile.root, profile.name)
		if identity is None:
			if not profile.is_default:
				raise AccountError(f'profile {profile.name} has no profile.json; run `auth login`')
			return None, None
		if identity.account_id == account_id:
			return identity.profile_id, None
		if not profiles.is_managed(session):
			if identity.account_id is None:
				return None, None
			return None, (
				'> The session is logged in to a different account than the one bound '
				'to the default profile; this run is recorded without its profile ID.'
			)
		if identity.account_id is not None:
			raise AccountError(mismatch_message(profile))
		return ensure_identity(profile.root, profile.name, account_id).profile_id, None
	except IdentityError as exc:
		raise AccountError(str(exc)) from None


def mismatch_message(profile):
	option = '' if profile.is_default else f' --profile {profile.name}'
	return (
		f'The session of profile {profile.name} is logged in to a different Telegram '
		f'account than the one bound to the profile. Run `auth logout{option}` and log '
		'in with the bound account, or use a new profile for this account.'
	)
