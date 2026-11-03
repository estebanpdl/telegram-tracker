# -*- coding: utf-8 -*-
'''
Offline maintenance of named profiles: rename, and deletion from this
computer.

Both work on <home>/profiles/<name>/ only, never on the default profile.
They read no credential values, make no Telegram request, and expect
every other tgtracker process to be stopped. While they change a profile
they hold its session, credentials-file and profile-file leases (in that
order), so a process holding one of those leases refuses them, and a
process asking for one meanwhile is refused. A lease left by a crashed
process refuses them too; it is never removed here.

Rename moves the profile's folder in one directory rename, then rewrites
the name in its profile.json; the profile ID, account binding, saved
credentials and session stay. The command's leases move with the folder
and are released at the new name. Between the two steps the folder's
profile.json still names the old profile, and every command refuses it
(identity.read_identity). Running the rename again finishes it, unless
the old name holds a profile with the same profile ID: the new folder is
then a copy, and is refused. Collected outputs keep the profile names
they recorded.

Deletion removes only the files a profile keeps (profile_files), then its own
lock files and the folder, once that is empty. Any other entry in the
folder refuses it before anything is removed. profile.json goes last, so
an interrupted deletion leaves a profile, or a folder of its remaining
files, that running the deletion again finishes. The session is not
logged out on Telegram.
'''

# import modules
import contextlib
import errno
import os
import stat

from dataclasses import dataclass

# import local submodules
from telegram_tracker.credentials import home as homes
from telegram_tracker.credentials import profiles
from telegram_tracker.credentials.identity import IDENTITY_NAME, Identity, IdentityError, \
	read_identity, read_record, relabel
from telegram_tracker.credentials.lease import Lease, LeaseError, holder, lease_path
from telegram_tracker.credentials.protection import ProtectionError, reject_link
from telegram_tracker.credentials.store import STORE_NAME
from telegram_tracker.local_errors import file_reason


# a named profile's session file, and the SQLite files beside it
# (rollback journal, write-ahead log)
SESSION_FILE = homes.SESSION_NAME
COMPANION_SUFFIXES = ('-journal', '-wal', '-shm')


def profile_files():
	'''
	The files of a named profile, in deletion order: the session first,
	profile.json last.
	'''
	companions = tuple(SESSION_FILE + suffix for suffix in COMPANION_SUFFIXES)
	return companions + (SESSION_FILE, STORE_NAME, IDENTITY_NAME)


def leased_files():
	'''
	(file, how its lease names it) for each lease the commands hold, in
	the order taken.
	'''
	return (
		(SESSION_FILE, 'The session'),
		(STORE_NAME, 'The credentials file'),
		(IDENTITY_NAME, 'The profile file')
	)


def lock_files():
	return tuple(os.path.basename(lease_path(name)) for name, _ in leased_files())


class MaintenanceError(Exception):
	'''
	The change was refused or failed; `code` is the exit code.
	'''
	def __init__(self, message, code=1):
		super().__init__(message)
		self.code = code


def named_profile(home, name, label):
	'''
	The named profile `name` in `home`. Raises profiles.ProfileError for
	an invalid name, `default`, or a folder that is a link or differs
	from the name only in case.
	'''
	profiles.check_name(name, label)
	if name == profiles.DEFAULT:
		raise profiles.ProfileError(f'{label}: the default profile cannot be renamed or deleted')
	profile = profiles.Profile(name, label, home)
	profiles.check_folder(profile)
	return profile


def held_locks(root):
	'''
	One line for each lease lock file of the profile in `root` that
	exists now. Takes no lease, so it never refuses another process.
	'''
	found = []
	for name, what in leased_files():
		guarded = os.path.join(root, name)
		if os.path.lexists(lease_path(guarded)):
			detail = holder(guarded)
			found.append(
				f'{what} is in use by another process' + (f' ({detail})' if detail else '')
				+ f'; if none is running, delete {lease_path(guarded)}'
			)
	return found


@contextlib.contextmanager
def _leases(root, name):
	'''
	Hold the leases of the profile in `root` (see leased_files); the Lease
	objects are yielded so they can follow a moved folder.
	'''
	with contextlib.ExitStack() as stack:
		held = []
		for file, what in leased_files():
			lease = Lease(os.path.join(root, file), what, name)
			try:
				stack.enter_context(lease)
			except LeaseError as exc:
				raise MaintenanceError(str(exc)) from None
			held.append(lease)
		yield held


def _identity(root, name):
	try:
		return read_identity(root, name)
	except IdentityError as exc:
		raise MaintenanceError(str(exc)) from None


'''

Rename

'''

@dataclass(frozen=True)
class RenamePlan:
	'''
	old, new -> the profiles; identity -> the record being renamed;
	finishing -> the folder is already at the new name (an interrupted
	rename), so only profile.json changes.
	'''
	old: profiles.Profile
	new: profiles.Profile
	identity: Identity
	finishing: bool = False

	@property
	def folder(self):
		'''
		Where the profile's folder is now.
		'''
		return self.new.root if self.finishing else self.old.root


def plan_rename(home, old_name, new_name):
	'''
	The RenamePlan for renaming profile `old_name` to `new_name`, after
	every check that needs no lease. Raises profiles.ProfileError for
	unusable operands (exit 2) and MaintenanceError otherwise. Writes
	nothing.
	'''
	old = named_profile(home, old_name, 'OLD')
	new = named_profile(home, new_name, 'NEW')
	if old.name == new.name:
		raise profiles.ProfileError('OLD and NEW are the same profile name')
	if os.path.lexists(new.root):
		return RenamePlan(old, new, _interrupted(old, new), finishing=True)
	if not profiles.exists(old):
		raise profiles.ProfileError(f'there is no profile {old.name}')
	return RenamePlan(old, new, _identity(old.root, old.name))


def _interrupted(old, new):
	'''
	The record of `old` that a rename left in `new`'s existing folder.
	Raises MaintenanceError when the folder is another profile, or a copy
	of `old` (the same profile ID under both names).
	'''
	taken = MaintenanceError(
		f'profile {new.name} already exists ({new.root}); choose another name, or '
		f'delete it first with `auth delete-profile {new.name}`'
	)
	if not os.path.isdir(new.root):
		raise taken
	try:
		record = read_record(new.root)
	except IdentityError:
		raise taken from None
	if record is None or record.name != old.name:
		raise taken
	if os.path.lexists(old.root):
		try:
			original = read_record(old.root)
		except IdentityError as exc:
			raise MaintenanceError(
				f'{new.root} holds the record of profile {old.name}, but whether it is a '
				f'copy of profile {old.name} cannot be checked: {exc}'
			) from None
		if original is not None and original.profile_id == record.profile_id:
			raise MaintenanceError(
				f'the folder of profile {new.name} is a copy of profile {old.name} (the same '
				f'profile ID), not an interrupted rename; remove the copy ({new.root}) or '
				'move it out of the profiles folder',
				code=2
			)
	return record


def rename(plan):
	'''
	Carry out `plan` (see plan_rename) under the profile's leases and
	return the renamed Identity. Raises MaintenanceError when refused, when
	the folder cannot be moved (nothing moved), or when profile.json cannot
	be rewritten after the move (a rerun finishes).
	'''
	with _leases(plan.folder, plan.old.name) as held:
		if plan_rename(plan.old.home, plan.old.name, plan.new.name) != plan:
			raise MaintenanceError(
				f'profile {plan.old.name} or {plan.new.name} changed while the rename was '
				'checked; nothing was changed. Run the command again.'
			)
		if not plan.finishing:
			_move(plan)
			for lease in held:
				lease.moved(plan.new.root)
		try:
			return relabel(plan.new.root, plan.old.name, plan.new.name)
		except IdentityError as exc:
			raise MaintenanceError(
				f'profile {plan.old.name} was moved to {plan.new.root}, but its profile.json '
				f'still names {plan.old.name} ({exc}); run the command again to finish'
			) from None


def _move(plan):
	source, target = plan.old.root, plan.new.root
	try:
		os.rename(source, target)
	except OSError as exc:
		if os.path.lexists(target) or exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
			reason = f'{target} was created meanwhile'
		elif isinstance(exc, PermissionError) and os.name == 'nt':
			reason = (
				f'a file in {source} is open in another program; close that program (for '
				'example a running collection) and try again'
			)
		else:
			reason = file_reason(exc)
		raise MaintenanceError(
			f'cannot rename profile {plan.old.name} to {plan.new.name}: {reason}. Nothing was moved.'
		) from None


'''

Deletion

'''

@dataclass(frozen=True)
class DeletePlan:
	'''
	profile -> the profile; files -> its profile_files() that exist, in deletion
	order; identity -> its record, None when profile.json is gone.
	'''
	profile: profiles.Profile
	files: tuple
	identity: Identity | None


@dataclass(frozen=True)
class Deletion:
	'''
	removed -> the files removed; remaining -> what is left in the folder
	after a failure; problem -> the failure, None when everything went.
	'''
	removed: tuple
	remaining: tuple = ()
	problem: str | None = None


def plan_delete(home, name):
	'''
	The DeletePlan for deleting profile `name`, after every check that
	needs no lease. A folder without profile.json (left by an interrupted
	deletion) can be deleted too. Raises profiles.ProfileError for an
	unusable or unknown name (exit 2) and MaintenanceError otherwise.
	Writes nothing.
	'''
	profile = named_profile(home, name, 'NAME')
	root = profile.root
	if not os.path.lexists(root):
		raise profiles.ProfileError(f'there is no profile {name}')
	if not os.path.isdir(root):
		raise MaintenanceError(f'{root} is not a folder')
	entries = set(os.listdir(root))
	unexpected = sorted(entries - set(profile_files()) - set(lock_files()))
	if unexpected:
		raise MaintenanceError(
			f'{root} holds entries that are not files of a profile: {", ".join(unexpected)}. '
			'Nothing was removed; move or delete them first.'
		)
	present = tuple(f for f in profile_files() if f in entries)
	for file in present:
		_check_file(os.path.join(root, file))
	identity = _identity(root, name) if IDENTITY_NAME in entries else None
	return DeletePlan(profile, present, identity)


def _check_file(path):
	'''
	Refuse a link, anything but a regular file, and a file with other
	hard links (deleting this name would not delete its content).
	'''
	try:
		reject_link(path)
		info = os.lstat(path)
	except ProtectionError as exc:
		raise MaintenanceError(str(exc)) from None
	except OSError as exc:
		raise MaintenanceError(f'cannot check {path} ({file_reason(exc)})') from None
	if not stat.S_ISREG(info.st_mode):
		raise MaintenanceError(f'{path} is not a regular file; nothing was removed')
	if info.st_nlink > 1:
		raise MaintenanceError(
			f'{path} has other hard links, so deleting it would not delete its content; '
			'nothing was removed'
		)


def delete(plan):
	'''
	Carry out `plan` (see plan_delete) under the profile's leases. Raises
	MaintenanceError when refused (nothing removed); returns a Deletion,
	whose `problem` reports a failure part way.
	'''
	root = plan.profile.root
	removed = []
	try:
		with _leases(root, plan.profile.name):
			if plan_delete(plan.profile.home, plan.profile.name) != plan:
				raise MaintenanceError(
					f'profile {plan.profile.name} changed after it was checked; nothing was '
					'removed. Run the command again.'
				)
			for file in plan.files:
				os.remove(os.path.join(root, file))
				removed.append(file)
		# the leases are released, the session's last: the folder is empty now
		os.rmdir(root)
	except OSError as exc:
		where = exc.filename or root
		return Deletion(tuple(removed), _remaining(root), f'{where}: {file_reason(exc)}')
	return Deletion(tuple(removed))


def _remaining(root):
	try:
		return tuple(sorted(os.listdir(root)))
	except OSError:
		return ()
