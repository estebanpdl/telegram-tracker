# -*- coding: utf-8 -*-
'''
Interactive login, shared by `collect` and `auth login`.

An authorized session skips every prompt. Otherwise the client requests a
login code, prompts for it, and signs in. When the account has two-step
verification, Telegram answers the code with SessionPasswordNeededError;
the cloud password is then prompted without echo and passed to `sign_in`
on the same client, which completes the session.

The password is never stored, logged, or accepted as an argument: it only
exists as a local value handed to `sign_in`. Prompts are injectable so
tests can replace them. Error messages name the failing step, never the
phone number, code, or password.
'''

# import modules
import contextlib
import getpass
import signal
import threading
import warnings

# import Telegram API submodules
from telethon import errors


MAX_ATTEMPTS = 3

CODE_PROMPT = 'Enter the code: '
PASSWORD_PROMPT = 'Enter your two-step verification password: '


class AuthenticationError(Exception):
	'''
	Login could not complete. Messages never include secrets.
	'''


def prompt_code(prompt):
	'''
	Read the login code from the terminal.
	'''
	return input(prompt)


def prompt_password(prompt):
	'''
	Read the cloud password without echo. Refuse getpass's fallback, which
	would echo the password when no terminal is available.
	'''
	with warnings.catch_warnings():
		warnings.simplefilter('error', getpass.GetPassWarning)
		try:
			return getpass.getpass(prompt)
		except getpass.GetPassWarning:
			raise AuthenticationError(
				'Cannot read the two-step verification password without echo; '
				'run the command from an interactive terminal.'
			) from None


@contextlib.contextmanager
def _interruptible():
	'''
	Let Ctrl+C interrupt a blocking prompt. asyncio.run replaces the SIGINT
	handler with one that only cancels the main task, which a blocking
	input() call never notices.
	'''
	if threading.current_thread() is not threading.main_thread():
		yield
		return

	previous = signal.getsignal(signal.SIGINT)
	signal.signal(signal.SIGINT, signal.default_int_handler)
	try:
		yield
	finally:
		if previous is not None:
			signal.signal(signal.SIGINT, previous)


def _ask(prompt_func, prompt, what):
	'''
	Call a prompt; turn missing input (EOF) into a clean error.
	'''
	with _interruptible():
		try:
			return prompt_func(prompt)
		except EOFError:
			raise AuthenticationError(
				f'No {what} was entered because input is unavailable; '
				'run the command from an interactive terminal.'
			) from None


async def _sign_in_with_code(client, phone, code_prompt):
	'''
	Sign in with a login code. Returns True when Telegram then requires the
	cloud password.
	'''
	for attempt in range(1, MAX_ATTEMPTS + 1):
		code = _ask(code_prompt, CODE_PROMPT, 'login code').strip()
		if not code:
			print(f'> The code cannot be empty ({attempt}/{MAX_ATTEMPTS}).')
			continue

		try:
			await client.sign_in(phone, code)
			return False
		except errors.SessionPasswordNeededError:
			return True
		except errors.PhoneCodeInvalidError:
			print(f'> Invalid code ({attempt}/{MAX_ATTEMPTS}).')
		except errors.PhoneCodeExpiredError:
			raise AuthenticationError(
				'The login code expired; run the command again to request a new code.'
			) from None

	raise AuthenticationError(f'The login code was not accepted after {MAX_ATTEMPTS} attempts.')


async def _sign_in_with_password(client, password_prompt):
	'''
	Complete a two-step verification sign-in on the same client.
	'''
	print('> Two-step verification is enabled.')
	for attempt in range(1, MAX_ATTEMPTS + 1):
		password = _ask(password_prompt, PASSWORD_PROMPT, 'password')
		if not password:
			print(f'> The password cannot be empty ({attempt}/{MAX_ATTEMPTS}).')
			continue

		try:
			await client.sign_in(password=password)
			return
		except errors.PasswordHashInvalidError:
			print(f'> Invalid password ({attempt}/{MAX_ATTEMPTS}).')

	raise AuthenticationError(
		f'The two-step verification password was not accepted after {MAX_ATTEMPTS} attempts.'
	)


async def ensure_authorized(
		client,
		phone,
		*,
		code_prompt=prompt_code,
		password_prompt=prompt_password
	):
	'''
	Make sure `client` (connected) is authorized, logging in interactively
	when it is not.

	client -> connected Telethon client (or a test double)
	phone -> account phone number from config, used only for the code request
	code_prompt -> callable(prompt) returning the login code
	password_prompt -> callable(prompt) returning the cloud password; must
		not echo

	Raises AuthenticationError when login cannot complete: rejected input
	after MAX_ATTEMPTS, an expired code, unavailable input, a Telegram rate
	limit, or another Telegram refusal. Login rate limits are never retried
	automatically. KeyboardInterrupt and task cancellation propagate.
	'''
	if await client.is_user_authorized():
		print('> Authorized!')
		return

	print('> Not Authorized! Sending code request...')

	# Login flood waits are run-fatal: stop Telethon from sleeping and
	# retrying them silently, then restore the threshold for collection.
	threshold = client.flood_sleep_threshold
	client.flood_sleep_threshold = 0
	try:
		await client.send_code_request(phone)
		if await _sign_in_with_code(client, phone, code_prompt):
			await _sign_in_with_password(client, password_prompt)
	except errors.FloodWaitError as exc:
		raise AuthenticationError(
			f'Telegram rate-limited the login; wait {exc.seconds} seconds before trying again.'
		) from None
	except errors.RPCError as exc:
		raise AuthenticationError(
			f'Telegram rejected the login ({type(exc).__name__}).'
		) from None
	finally:
		client.flood_sleep_threshold = threshold

	print('> Authorized!')
