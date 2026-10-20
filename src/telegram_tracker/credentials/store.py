# -*- coding: utf-8 -*-
'''
credentials.json: the user's saved app credentials, in the application
home.

	{"schema_version": 1, "api_id": 12345, "api_hash": "...", "phone": "+..."}

Exactly these keys: a complete set, never a login code, cloud password,
or session key. It is plaintext protected by owner-only permissions (see
protection.py), not encrypted.

Saving stages the JSON in an exclusive sibling temporary file that is made
private before any secret byte is written, flushes it to disk, and
replaces the file atomically. Failures before replacement preserve the
previous file; a failed protection check after replacement reports an
error after the new file is already in place. Saves and removals hold a
short lease on the file so concurrent updates cannot interleave. Errors
never include stored values.
'''

# import modules
import contextlib
import json
import os

# import local submodules
from telegram_tracker.credentials.lease import Lease, LeaseError
from telegram_tracker.local_errors import file_reason
from telegram_tracker.credentials.protection import ProtectionError, check, \
	ensure_private_dir, protect, reject_link


STORE_NAME = 'credentials.json'
SCHEMA_VERSION = 1
FIELDS = ('api_id', 'api_hash', 'phone')
KEYS = ('schema_version',) + FIELDS


class StoreError(Exception):
	'''
	credentials.json cannot be read, is invalid or insecure, or cannot be
	written. Messages name the file and the problem, never its values.
	'''


def store_path(home):
	return os.path.join(home, STORE_NAME)


def check_field(field, value, label):
	'''
	One field with its checked type: api_id a positive integer (digits in
	text are accepted), api_hash and phone non-empty strings. Raises
	ValueError naming the source and field, never the value.
	'''
	if field == 'api_id':
		if isinstance(value, str) and value.strip().isdigit():
			value = int(value.strip())
		if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
			raise ValueError(f'{label}: api_id must be a positive integer')
		return value
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f'{label}: {field} must be a non-empty string')
	return value.strip()


def validate_set(values, label):
	'''
	A complete credential set with checked types (see check_field).
	'''
	return {field: check_field(field, values.get(field), label) for field in FIELDS}


def read_store(home):
	'''
	The saved credential set, or None when there is no credentials.json.
	Raises StoreError when it is unreadable, invalid, of another schema
	version, a link, or accessible to other accounts.
	'''
	path = store_path(home)
	if not os.path.lexists(path):
		return None
	try:
		reject_link(path)
		check(path)
	except ProtectionError as exc:
		raise StoreError(
			f'{exc}. Remove it with `tgtracker auth clear-credentials` and log in '
			'again, or restrict it to your account.'
		) from None

	try:
		with open(path, encoding='utf-8') as reader:
			data = json.load(reader)
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise StoreError(f'{path} cannot be read ({file_reason(exc)})') from None

	if not isinstance(data, dict):
		raise StoreError(f'{path} is not a credentials file')
	version = data.get('schema_version')
	if type(version) is not int or version != SCHEMA_VERSION:
		raise StoreError(
			f'{path} has an unsupported schema version; this version reads {SCHEMA_VERSION}'
		)
	if set(data) != set(KEYS):
		raise StoreError(f'{path} must hold exactly: {", ".join(KEYS)}')
	try:
		return validate_set(data, path)
	except ValueError as exc:
		raise StoreError(str(exc)) from None


def prepare_home(home):
	'''
	Create the application home if needed and make it private. Raises
	StoreError when that is not possible (nothing is written then).
	'''
	try:
		ensure_private_dir(home)
	except ProtectionError as exc:
		raise StoreError(str(exc)) from None


def save_store(home, values):
	'''
	Validate and save a complete credential set, replacing any earlier
	one atomically.
	'''
	try:
		checked = validate_set(values, 'credentials')
	except ValueError as exc:
		raise StoreError(str(exc)) from None

	prepare_home(home)
	path = store_path(home)
	text = json.dumps({'schema_version': SCHEMA_VERSION, **checked}, indent=1) + '\n'
	with _store_lease(path):
		try:
			reject_link(path)
		except ProtectionError as exc:
			raise StoreError(str(exc)) from None
		replace_privately(path, text.encode('utf-8'))


def clear_store(home):
	'''
	Remove credentials.json without reading it. Returns False when there
	was none. Nothing else in the home is touched.
	'''
	path = store_path(home)
	if not os.path.lexists(path):
		return False
	with _store_lease(path):
		try:
			os.remove(path)
		except FileNotFoundError:
			return False
		except OSError as exc:
			raise StoreError(f'cannot remove {path} ({exc.strerror or type(exc).__name__})') from None
	return True


@contextlib.contextmanager
def _store_lease(path):
	'''
	Lease on credentials.json; a held lease becomes a StoreError.
	'''
	lease = Lease(path, 'The credentials file')
	try:
		lease.__enter__()
	except LeaseError as exc:
		raise StoreError(str(exc)) from None
	try:
		yield lease
	finally:
		lease.__exit__(None, None, None)


def replace_privately(path, data):
	'''
	Stage `data` in an exclusive private sibling file, then replace `path`.
	The staged file is removed on exit. `path` keeps its old content on
	failures before replacement; the final protection check may fail
	after the new file is in place.
	'''
	tmp = f'{path}.{os.getpid()}.tmp'
	try:
		fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
	except OSError as exc:
		raise StoreError(f'cannot create {tmp} ({exc.strerror or type(exc).__name__})') from None
	try:
		try:
			with os.fdopen(fd, 'wb') as writer:
				protect(tmp)
				writer.write(data)
				writer.flush()
				os.fsync(writer.fileno())
			os.replace(tmp, path)
			check(path)
		except ProtectionError as exc:
			raise StoreError(str(exc)) from None
		except OSError as exc:
			raise StoreError(f'cannot save {path} ({exc.strerror or type(exc).__name__})') from None
	finally:
		try:
			os.remove(tmp)
		except FileNotFoundError:
			pass
