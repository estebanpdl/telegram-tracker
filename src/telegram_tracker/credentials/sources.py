# -*- coding: utf-8 -*-
'''
Where the app credentials (api_id, api_hash, phone) come from.

	1. TELEGRAM_TRACKER_API_ID / _API_HASH / _PHONE, field by field
	2. --config PATH (an INI file)
	3. credentials.json in the application home
	4. ./config/config.ini in the working directory
	5. prompts (`auth login` only, outside this module)

Sources 2-4 are files and only one of them is used, as a whole: the first
that is present. A field the environment does not set comes from that
file or stays missing; files never fill each other's gaps. A file whose
values are all template placeholders (`api_id = api_id`, `<your api_id>`,
or empty) counts as absent when it was found automatically; placeholders
in a file given with --config count as missing values.

Files are read only when the environment leaves a field open. Results
carry a source label per field; values never appear in messages.
'''

# import modules
import configparser
import os

from dataclasses import dataclass, field as dataclass_field

# import local submodules
from telegram_tracker.credentials.store import FIELDS, StoreError, check_field, read_store


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
		raise CredentialError(f'{label} cannot be read ({type(exc).__name__})') from None
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
		ini_reader=read_ini,
		store_reader=read_store,
		isfile=os.path.isfile
	):
	'''
	Resolve the credentials for one command.

	config -> --config path or None
	home -> application home (for credentials.json)
	cwd -> working directory (for ./config/config.ini)
	ini_reader / store_reader / isfile -> replace the file access (tests)

	Returns a Resolution, possibly incomplete. Raises CredentialError when
	a selected source is unreadable or holds an invalid value.
	'''
	environ = os.environ if environ is None else environ
	cwd = cwd or os.getcwd()
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
	if isfile(legacy):
		values = ini_reader(legacy, LEGACY_CONFIG)
		if all(is_placeholder(f, values.get(f)) for f in FIELDS):
			result.notes.append(f'{LEGACY_CONFIG} holds only placeholder values; ignored')
		else:
			_take_file(result, values, LEGACY_CONFIG)
	return result


def _take_file(result, values, label):
	'''
	Fill the fields the environment left open from one file source.
	'''
	result.file_source = label
	for f in FIELDS:
		if f in result.values or is_placeholder(f, values.get(f)):
			continue
		result.add(f, values.get(f), label)
