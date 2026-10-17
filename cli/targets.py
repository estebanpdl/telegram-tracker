# -*- coding: utf-8 -*-
'''
Collection targets from --telegram-channel or --batch-file: accepted
forms, normalization, and deduplication.
'''

# import modules
import re

# import local submodules
from utils import cmd_request_type


# accepted target forms
USERNAME_OR_ID = re.compile(r'^(?:[A-Za-z0-9_]+|-?[0-9]+)$')
PUBLIC_LINK = re.compile(
	r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?'
	r'([A-Za-z0-9_]+)/?$',
	re.IGNORECASE
)
TARGET_FORMS = 'use a username, @username, t.me/username, or a numeric id'


def normalize_target(text):
	'''
	One target in canonical form: a bare username or numeric id. `@name`
	and public t.me/name links become `name`. Returns None for a blank
	value; raises ValueError for anything else (paths, invite links,
	post links).
	'''
	value = text.strip()
	if not value:
		return None

	if value.startswith('@'):
		value = value[1:]
	else:
		link = PUBLIC_LINK.match(value)
		if link:
			value = link.group(1)

	if not USERNAME_OR_ID.match(value):
		raise ValueError(f'unsupported target {text.strip()!r}')
	return value


def read_targets(args, parser):
	'''
	Targets from --telegram-channel, or one per line from --batch-file,
	normalized (see normalize_target) and deduplicated case-insensitively,
	first occurrence kept. Blank lines are skipped. Returns (targets,
	number of targets given). An unreadable batch file or an unsupported
	target is an argument error (exit 2).
	'''
	req_type, req_input = cmd_request_type(args)
	if req_type != 'batch':
		try:
			target = normalize_target(req_input)
		except ValueError as exc:
			parser.error(f'--telegram-channel: {exc}; {TARGET_FORMS}')
		if target is None:
			parser.error('--telegram-channel is empty')
		return [target], 1

	try:
		with open(req_input, encoding='utf-8', mode='r') as reader:
			lines = reader.read().splitlines()
	except (OSError, UnicodeDecodeError) as exc:
		reason = getattr(exc, 'strerror', None) or str(exc)
		parser.error(f'cannot read --batch-file {req_input!r}: {reason}')

	targets, seen, given = [], set(), 0
	for number, line in enumerate(lines, start=1):
		try:
			target = normalize_target(line)
		except ValueError as exc:
			parser.error(f'--batch-file {req_input!r} line {number}: {exc}; {TARGET_FORMS}')
		if target is None:
			continue
		given += 1
		if target.lower() not in seen:
			seen.add(target.lower())
			targets.append(target)
	return targets, given
