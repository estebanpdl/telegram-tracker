# -*- coding: utf-8 -*-
'''
Steps shared by the commands that log in to work on an output folder
(`collect`, `download-media`): the profile and session, checked against
the folder, the client factory from the app credentials (see
telegram_tracker.credentials), and the session's home before the session
lease is taken. Credential sources are named, never their values.
'''

# import modules
import functools
import os
import sys

# import local submodules
from telegram_tracker.credentials import accounts, profiles
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import sources
from telegram_tracker.credentials.protection import ProtectionError


def select_run_session(args, parser, output_folder):
	'''
	(home, profile, session) for --profile and --session. A session file
	inside the output folder, or an output folder inside the application
	home (or the reverse), is an argument error. Raises homes.HomeError
	and sources.CredentialError as the selection does.
	'''
	home, _ = homes.app_home()
	profile = profiles.select_profile(args.get('profile'), home=home)
	profiles.require(profile)
	session = profiles.select_session(profile, args.get('session'))
	if homes.is_within(session.path, output_folder):
		parser.error('the session file must not be inside the --output folder')
	if homes.is_within(output_folder, home) or homes.is_within(home, output_folder):
		parser.error(f'the --output folder must be outside the application home ({home})')
	return home, profile, session


def app_client(args, profile, session):
	'''
	(client factory, phone) from the profile's app credentials (--config
	and the environment included), or (None, None) after explaining on
	stderr which are missing (exit code 2 for the caller).
	'''
	from telegram_tracker.api import create_client

	resolution = sources.resolve(
		config=args.get('config'),
		home=profile.root,
		discover_legacy=profile.is_default
	)
	if resolution.missing:
		missing_credentials(resolution, profile)
		return None, None
	factory = functools.partial(
		create_client,
		session.path,
		resolution.values['api_id'],
		resolution.values['api_hash']
	)
	return factory, resolution.values['phone']


def prepare_session(session, profile, home):
	'''
	Print the session's notice, create the profile's folder for a new
	session in the home, and prepare the accounts folder. Returns None, or
	exit code 1 after explaining (stderr).
	'''
	if session.notice:
		print(session.notice, file=sys.stderr)
	if session.in_home and not os.path.isfile(session.path):
		try:
			profiles.prepare_root(profile)
		except ProtectionError as exc:
			print(
				f'> Cannot create the session in {profile.root}: {exc}. Set '
				'TELEGRAM_TRACKER_HOME' + (' or pass --session.' if profile.is_default else '.'),
				file=sys.stderr
			)
			return 1
	try:
		accounts.prepare(home)
	except accounts.AccountError as exc:
		print(f'> {exc}', file=sys.stderr)
		return 1
	return None


def missing_credentials(resolution, profile):
	'''
	Explain which app credentials are missing and how to provide them
	(stderr, exit 2). Names sources only, never values.
	'''
	found = [label for label in resolution.labels() if not label.endswith(': missing')]
	login = 'auth login' + ('' if profile.is_default else f' --profile {profile.name}')
	print(
		f'> Missing app credentials: {", ".join(resolution.missing)}'
		+ (f' (found {"; ".join(found)})' if found else '')
		+ f'. Run `{login}` to enter and save them, set '
		+ ', '.join(sources.ENV_VARS.values())
		+ (', or fill in config/config.ini.' if profile.is_default else ', or pass --config.'),
		file=sys.stderr
	)
	for note in resolution.notes:
		print(f'> Note: {note}', file=sys.stderr)
	return 2
