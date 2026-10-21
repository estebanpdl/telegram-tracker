# -*- coding: utf-8 -*-
'''
profile.json: a profile's identity, beside its credentials.json.

	{"schema_version": 1, "name": "alice",
	 "profile_id": "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "account_id": null}

Exactly these keys. `profile_id` is a random UUID, created once and never
changed; collection outputs record it as the profile's opaque ID.
`account_id` is the Telegram user ID verified through the profile's own
session (null until then). This private metadata is not added to collection
provenance or diagnostics; collected payloads can independently contain
the same user ID. Once set it is never replaced, so a profile cannot silently
move to another account. Removing credentials.json leaves profile.json,
so the profile keeps its ID and binding across `clear-credentials` and a
later login.

The file holds no secret, but gets the same owner-only protection and
atomic replacement as credentials.json (see store.py), under a short
lease on the file.
'''

# import modules
import contextlib
import json
import os
import uuid

from dataclasses import dataclass

# import local submodules
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.credentials.protection import ProtectionError, check, reject_link
from telegram_tracker.credentials.store import StoreError, replace_privately
from telegram_tracker.local_errors import file_reason


IDENTITY_NAME = 'profile.json'
SCHEMA_VERSION = 1
KEYS = ('schema_version', 'name', 'profile_id', 'account_id')


class IdentityError(Exception):
	'''
	profile.json cannot be read, is invalid or insecure, or cannot be
	written. Messages never include the account ID.
	'''


class AccountMismatch(IdentityError):
	'''
	The profile is bound to a different Telegram account.
	'''


@dataclass(frozen=True)
class Identity:
	name: str
	profile_id: str
	account_id: int | None = None


def identity_path(root):
	return os.path.join(root, IDENTITY_NAME)


def _is_uuid(value):
	try:
		return isinstance(value, str) and str(uuid.UUID(value)) == value
	except ValueError:
		return False


def _is_account_id(value):
	return value is None or (type(value) is int and value > 0)


def read_identity(root, name):
	'''
	The Identity in <root>/profile.json, or None when there is none.
	Raises IdentityError when it is unreadable, invalid, a link,
	accessible to other accounts, or names another profile.
	'''
	path = identity_path(root)
	if not os.path.lexists(path):
		return None
	try:
		reject_link(path)
		check(path)
	except ProtectionError as exc:
		raise IdentityError(f'{exc}. Restrict it to your account.') from None
	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise IdentityError(f'{path} cannot be read ({file_reason(exc)})') from None

	if not isinstance(data, dict) or set(data) != set(KEYS):
		raise IdentityError(f'{path} must hold exactly: {", ".join(KEYS)}')
	version = data['schema_version']
	if type(version) is not int or version != SCHEMA_VERSION:
		raise IdentityError(
			f'{path} has an unsupported schema version; this version reads {SCHEMA_VERSION}'
		)
	if data['name'] != name:
		raise IdentityError(f'{path} belongs to another profile')
	if not _is_uuid(data['profile_id']) or not _is_account_id(data['account_id']):
		raise IdentityError(f'{path} has an invalid profile_id or account_id')
	return Identity(name, data['profile_id'], data['account_id'])


def ensure_identity(root, name, account_id=None):
	'''
	The profile's Identity, creating profile.json (new UUID) when there is
	none and recording `account_id` when the profile has no account yet.
	Raises AccountMismatch when the profile is bound to another account
	(nothing is written then) and IdentityError when the file cannot be
	read or written. `root` must exist and be private.
	'''
	if not _is_account_id(account_id):
		raise IdentityError('the verified account ID is not a positive integer')
	path = identity_path(root)
	with _identity_lease(path):
		current = read_identity(root, name)
		if current is not None:
			if account_id is None or current.account_id == account_id:
				return current
			if current.account_id is not None:
				raise AccountMismatch(
					f'profile {name} is bound to a different Telegram account than '
					'the one this session is logged in to'
				)
			updated = Identity(name, current.profile_id, account_id)
		else:
			updated = Identity(name, str(uuid.uuid4()), account_id)
		_write(path, updated)
		return updated


def _write(path, identity):
	text = json.dumps({
		'schema_version': SCHEMA_VERSION,
		'name': identity.name,
		'profile_id': identity.profile_id,
		'account_id': identity.account_id
	}, indent=1) + '\n'
	try:
		reject_link(path)
		replace_privately(path, text.encode('utf-8'))
	except (ProtectionError, StoreError) as exc:
		raise IdentityError(str(exc)) from None


@contextlib.contextmanager
def _identity_lease(path):
	lease = Lease(path, 'The profile file')
	try:
		lease.__enter__()
	except LeaseError as exc:
		raise IdentityError(str(exc)) from None
	try:
		yield lease
	finally:
		lease.__exit__(None, None, None)
