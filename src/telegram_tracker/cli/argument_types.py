# -*- coding: utf-8 -*-
"""Dependency-free argument validators shared by the command parsers."""

import argparse
import math
from datetime import datetime, timezone


def bounded_int(low, high=None):
	'''
	argparse type: an integer in [low, high] (no upper bound when None).
	'''
	def parse(text):
		try:
			value = int(text)
		except ValueError:
			raise argparse.ArgumentTypeError(f'invalid integer: {text!r}') from None
		if value < low or (high is not None and value > high):
			span = f'between {low} and {high}' if high is not None else f'at least {low}'
			raise argparse.ArgumentTypeError(f'must be {span}, got {value}')
		return value

	return parse


def bounded_float(low, high):
	'''
	argparse type: a finite number in [low, high].
	'''
	def parse(text):
		try:
			value = float(text)
		except ValueError:
			raise argparse.ArgumentTypeError(f'invalid number: {text!r}') from None
		if not math.isfinite(value) or not low <= value <= high:
			raise argparse.ArgumentTypeError(
				f'must be a finite number between {low:g} and {high:g}, got {text}'
			)
		return value

	return parse


def utc_datetime(text):
	'''
	argparse type: an ISO 8601 date or datetime as an aware UTC datetime.
	A date alone means midnight UTC; a value without an offset is UTC; a
	`Z` or +HH:MM offset is converted to UTC.
	'''
	try:
		value = datetime.fromisoformat(text.strip())
	except ValueError:
		raise argparse.ArgumentTypeError(
			f'invalid date {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS], '
			'optionally with Z or +HH:MM'
		) from None

	if value.tzinfo is None:
		return value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)
