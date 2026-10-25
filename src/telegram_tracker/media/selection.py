# -*- coding: utf-8 -*-
'''
What a run downloads: the selected media types and the user's byte
limits (--media-types, --max-media-size, --max-media-total). Nothing is
selected unless the run asks for media.
'''

# import modules
from dataclasses import dataclass, field

# import local submodules
from telegram_tracker.media.classify import TYPES
from telegram_tracker.media.layout import MEDIA_FOLDER
from telegram_tracker.media.limits import MediaLimits


UNITS = (('GiB', 1024 ** 3), ('MiB', 1024 ** 2), ('KiB', 1024))


def describe_size(nbytes):
	'''
	`nbytes` in the largest unit that divides it exactly.
	'''
	for name, unit in UNITS:
		if nbytes % unit == 0:
			return f'{nbytes // unit} {name}'
	return f'{nbytes} bytes'


@dataclass(frozen=True)
class MediaSelection:
	'''
	types -> the selected classify.TYPES, in that order
	limits -> the run's MediaLimits
	'''
	types: tuple = TYPES
	limits: MediaLimits = field(default_factory=MediaLimits)

	def __post_init__(self):
		unknown = set(self.types) - set(TYPES)
		if unknown or not self.types:
			raise ValueError(f'media types must be some of {", ".join(TYPES)}, not {self.types!r}')
		object.__setattr__(self, 'types', tuple(t for t in TYPES if t in self.types))

	def selects(self, ref):
		'''
		Whether a classify.MediaRef (or None) is downloaded.
		'''
		return ref is not None and ref.type in self.types

	def record(self):
		'''
		The selection as the ledger and collection-status.json record it.
		'''
		return {
			'types': list(self.types),
			'max_media_size': self.limits.max_file,
			'max_media_total': self.limits.max_total
		}

	def describe(self):
		'''
		One line for the run header.
		'''
		limits = []
		if self.limits.max_file is not None:
			limits.append(f'{describe_size(self.limits.max_file)} per file')
		if self.limits.max_total is not None:
			limits.append(f'{describe_size(self.limits.max_total)} per run')
		return (
			f'{", ".join(self.types)} into {MEDIA_FOLDER}/; '
			+ (f'at most {" and ".join(limits)}' if limits else 'no size limits')
		)
