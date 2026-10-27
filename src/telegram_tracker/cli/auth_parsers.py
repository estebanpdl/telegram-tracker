# -*- coding: utf-8 -*-
'''
Authentication actions and the profile/session options shared by commands.
Building these parsers reads no credentials and imports no services.
'''


AUTH = 'auth'

# auth actions
LOGIN = 'login'
STATUS = 'status'
LOGOUT = 'logout'
CLEAR_CREDENTIALS = 'clear-credentials'
PROFILES = 'profiles'


def add_profile_argument(group):
	'''
	--profile, shared by `collect` and the `auth` actions.
	'''
	group.add_argument(
		'--profile',
		metavar='NAME',
		help='Account profile: its own saved credentials and session '
			'(created by `auth login --profile NAME`). Default: '
			'TELEGRAM_TRACKER_PROFILE if set, else the default profile, which '
			'uses the application home itself.'
	)


def add_session_arguments(group):
	'''
	--profile, --session, and --config, shared by `collect` and `auth`.
	'''
	add_profile_argument(group)
	group.add_argument(
		'--session',
		metavar='PATH',
		help='Telegram session file (default profile only). Default: '
			'TELEGRAM_TRACKER_SESSION if set, else session_file.session in the '
			'application home (~/.telegram_tracker, or TELEGRAM_TRACKER_HOME); an '
			'existing ./session_file.session is used while the home has none. A '
			'named profile always uses its own session.'
	)
	group.add_argument(
		'--config',
		metavar='PATH',
		help='INI file with a [Telegram API credentials] section, used instead of '
			"the profile's saved credentials (and, for the default profile, "
			'./config/config.ini). TELEGRAM_TRACKER_API_ID, '
			'TELEGRAM_TRACKER_API_HASH, and TELEGRAM_TRACKER_PHONE override '
			'single values.'
	)


def add_auth_parsers(commands):
	'''Return the `auth` parser and its action parsers by name.'''
	auth = commands.add_parser(
		AUTH,
		help='Log in to Telegram, check or revoke the session, and manage saved credentials.',
		description='Log in to Telegram, check or revoke the session, and manage '
			'the app credentials saved in the application home and its profiles.'
	)
	actions = auth.add_subparsers(dest='auth_command', metavar='<action>', required=True)

	login = actions.add_parser(
		LOGIN,
		help='Log in and save the app credentials.',
		description='Log in to Telegram with the selected session. Missing app '
			'credentials (api_id, api_hash, phone) are asked for; after a '
			'successful login, a complete set you typed is saved to the '
			"profile's credentials.json when nothing was saved before "
			'(plaintext, readable only by your account). '
			'Values from the environment or an INI file are saved only if you '
			'agree, as are replacements. An authorized session skips the login '
			'code and two-step verification password prompts. The logged-in '
			'account is checked and recorded in profile.json; --profile NAME '
			'creates a named profile.'
	)
	add_session_arguments(login)
	save = login.add_mutually_exclusive_group()
	save.add_argument(
		'--save',
		action='store_true',
		help='Save the credentials used without asking, replacing saved ones.'
	)
	save.add_argument(
		'--no-save',
		action='store_true',
		help='Do not save credentials.'
	)

	status = actions.add_parser(
		STATUS,
		help='Show which session and credential sources are used.',
		description='Show the application home, the profile, the selected session, '
			'and where each app credential comes from. Values are never shown. Notes a '
			'./config/config.ini that saved credentials hide. Offline: a session file does '
			'not prove that it is logged in.'
	)
	add_session_arguments(status)
	status.add_argument(
		'--check',
		action='store_true',
		help='Connect to Telegram to check that the session is logged in. Never '
			'asks for a code; Telethon may update the session file.'
	)

	logout = actions.add_parser(
		LOGOUT,
		help='Revoke the session on Telegram.',
		description='Log the selected session out on Telegram, which revokes it '
			'and deletes the session file. Saved credentials stay.'
	)
	add_session_arguments(logout)
	logout.add_argument('--yes', action='store_true', help='Do not ask for confirmation.')

	clear = actions.add_parser(
		CLEAR_CREDENTIALS,
		help='Delete the saved credentials.',
		description="Delete the profile's credentials.json. The session, the "
			'profile record (profile.json), environment variables, and INI files '
			'are not touched.'
	)
	add_profile_argument(clear)
	clear.add_argument('--yes', action='store_true', help='Do not ask for confirmation.')

	profiles = actions.add_parser(
		PROFILES,
		help='List the account profiles.',
		description='List the default profile and every named profile in the '
			'application home, with whether each has a profile record, saved '
			'credentials, and a session file. Offline; reads no values and '
			'creates nothing.'
	)

	return auth, {
		LOGIN: login, STATUS: status, LOGOUT: logout, CLEAR_CREDENTIALS: clear,
		PROFILES: profiles
	}
