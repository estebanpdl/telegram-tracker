# -*- coding: utf-8 -*-
'''
Where the app credentials (api_id, api_hash, phone) come from.

	1. TELEGRAM_TRACKER_API_ID / _API_HASH / _PHONE, field by field
	2. --config PATH (an INI file)
	3. credentials.json of the selected profile (in the application home
	   for the default profile)
	4. ./config/config.ini in the working directory (default profile only)
	5. prompts (`auth login` only, outside this module)

Sources 2-4 are files and only one of them is used, as a whole: the first
that is present. A field the environment does not set comes from that
file or stays missing; files never fill each other's gaps. A file whose
values are all template placeholders (`api_id = api_id`, `<your api_id>`,
or empty) counts as absent when it was found automatically; placeholders
in a file given with --config count as missing values.

Files are read only when the environment leaves a field open. Results
carry a source label per field; values never appear in messages.
`auth status` alone also looks at a ./config/config.ini that saved
credentials shadow (shadowed_config), without changing the selection.
'''

# import modules
import configparser
import os

from dataclasses import dataclass, field as dataclass_field

# import local submodules
from telegram_tracker.credentials.store import FIELDS, StoreError, check_field, read_store
from telegram_tracker.local_errors import file_reason


ENV_VARS = {
	'api_id': 'TELEGRAM_TRACKER_API_ID',
	'api_hash': 'TELEGRAM_TRACKER_API_HASH',
	'phone': 'TELEGRAM_TRACKER_PHONE'
}
INI_SECTION = 'Telegram API credentials'
LEGACY_INI = os.path.join('config', 'config.ini')

# source labels
ENVIRONMENT = 'environment'
CONFIG = '--config'
STORE = 'credentials.json'
LEGACY_CONFIG = './config/config.ini'
PROMPT = 'prompt'

# sources that the user did not enter in `auth login`
EXTERNAL = (ENVIRONMENT, CONFIG, LEGACY_CONFIG)


class CredentialError(Exception):
	'''
	A credential source is missing, unreadable, or invalid. Messages name
	the source and field, never values.
	'''


@dataclass
class Resolution:
	'''
	values -> field: checked value (api_id an int)
	sources -> field: source label
	file_source -> the file source used (CONFIG, STORE, LEGACY_CONFIG) or None
	notes -> remarks about skipped sources, for status output
	'''
	values: dict = dataclass_field(default_factory=dict)
	sources: dict = dataclass_field(default_factory=dict)
	file_source: str = None
	notes: list = dataclass_field(default_factory=list)

	@property
	def missing(self):
		return [f for f in FIELDS if f not in self.values]

	@property
	def complete(self):
		return not self.missing

	def add(self, field, value, label, source=None):
		'''
		Check and set one field; `label` names the source in errors and
		`source` (default: label) is recorded.
		'''
		try:
			self.values[field] = check_field(field, value, label)
		except ValueError as exc:
			raise CredentialError(str(exc)) from None
		self.sources[field] = source or label

	def labels(self):
		'''
		"field: source" for every field, "missing" when unset.
		'''
		return [f'{f}: {self.sources.get(f, "missing")}' for f in FIELDS]


def is_placeholder(field, value):
	'''
	True for an unset template value: empty, the field's own name, or
	`<...>`.
	'''
	if value is None:
		return True
	text = str(value).strip()
	return (
		not text
		or text.lower() == field
		or (text.startswith('<') and text.endswith('>'))
	)


def read_ini(path, label):
	'''
	Raw field values from the INI file's credentials section. Raises
	CredentialError when the file cannot be read or has no such section.
	'''
	parser = configparser.ConfigParser(interpolation=None)
	try:
		with open(path, encoding='utf-8') as reader:
			parser.read_file(reader)
	except (OSError, UnicodeDecodeError, configparser.Error) as exc:
		raise CredentialError(f'{label} cannot be read ({file_reason(exc)})') from None
	if not parser.has_section(INI_SECTION):
		raise CredentialError(f'{label} has no [{INI_SECTION}] section')
	section = parser[INI_SECTION]
	return {f: section.get(f) for f in FIELDS}


def resolve(
		*,
		config=None,
		environ=None,
		home=None,
		cwd=None,
		ini_reader=None,
		store_reader=None,
		isfile=None,
		discover_legacy=True
	):
	'''
	Resolve the credentials for one command.

	config -> --config path or None
	home -> application home (for credentials.json)
	cwd -> working directory (for ./config/config.ini)
	discover_legacy -> False skips ./config/config.ini (named profiles)
	ini_reader / store_reader / isfile -> replace the file access (tests)

	Returns a Resolution, possibly incomplete. Raises CredentialError when
	a selected source is unreadable or holds an invalid value.
	'''
	environ = os.environ if environ is None else environ
	cwd = cwd or os.getcwd()
	isfile = isfile or os.path.isfile
	ini_reader = ini_reader or read_ini
	store_reader = store_reader or read_store
	result = Resolution()

	for f, variable in ENV_VARS.items():
		value = environ.get(variable)
		if value is not None and value.strip():
			result.add(f, value, variable, ENVIRONMENT)

	if config is not None:
		config = os.path.join(cwd, config)
		if not isfile(config):
			raise CredentialError(f'--config {config} does not exist')
	if result.complete:
		return result

	if config is not None:
		_take_file(result, ini_reader(config, CONFIG), CONFIG)
		return result

	try:
		stored = store_reader(home) if home is not None else None
	except StoreError as exc:
		raise CredentialError(str(exc)) from None
	if stored is not None:
		_take_file(result, stored, STORE)
		return result

	legacy = os.path.join(cwd, LEGACY_INI)
	if discover_legacy and isfile(legacy):
		values = ini_reader(legacy, LEGACY_CONFIG)
		if all(is_placeholder(f, values.get(f)) for f in FIELDS):
			result.notes.append(f'{LEGACY_CONFIG} holds only placeholder values; ignored')
		else:
			_take_file(result, values, LEGACY_CONFIG)
	return result


def shadowed_config(resolution, *, cwd=None, ini_reader=None, isfile=None):
	'''
	For `auth status` of the default profile: when the saved credentials
	were the file source and ./config/config.ini is present too, a note
	saying that the INI file is not used, with the fields it sets (and
	which of them are invalid) or why it cannot be read. None when the
	saved credentials were not used, or the INI file is absent or holds
	only placeholders. Reads nothing in the first case and changes no
	selection; names fields and sources, never values.
	'''
	if resolution.file_source != STORE:
		return None
	cwd = cwd or os.getcwd()
	isfile = isfile or os.path.isfile
	ini_reader = ini_reader or read_ini
	legacy = os.path.join(cwd, LEGACY_INI)
	if not isfile(legacy):
		return None

	unused = f'not used, because {STORE} comes first'
	try:
		values = ini_reader(legacy, LEGACY_CONFIG)
	except CredentialError as exc:
		return f'{exc}; {unused}'
	fields = [f for f in FIELDS if not is_placeholder(f, values.get(f))]
	if not fields:
		return None
	invalid = []
	for f in fields:
		try:
			check_field(f, values[f], LEGACY_CONFIG)
		except ValueError:
			invalid.append(f)
	return (
		f'{LEGACY_CONFIG} also sets {", ".join(fields)}'
		+ (f' (invalid: {", ".join(invalid)})' if invalid else '')
		+ f' but is {unused}. Pass --config {LEGACY_CONFIG} to use it instead.'
	)


def _take_file(result, values, label):
	'''
	Fill the fields the environment left open from one file source.
	'''
	result.file_source = label
	for f in FIELDS:
		if f in result.values or is_placeholder(f, values.get(f)):
			continue
		result.add(f, values.get(f), label)
