# -*- coding: utf-8 -*-
'''
Where downloaded files live in an output folder, and how their paths are
made safe.

	<output>/downloaded-media/
		media.sqlite3                          the ledger
		<channel_id>/<type folder>/<message_id>-<sha256[:16]>.<ext>
		.partial/<run_uuid>/<channel_id>-<message_id>-<media_id>.part

Every name is generated from integers, hex digits and an allowed
extension; nothing from the remote side (file names, MIME extensions)
reaches a path. A stored relative path is accepted back only when it has
exactly that generated shape. Files of the reader's history pattern
(`*_messages.json`) can never be produced here.

Each folder from `downloaded-media` down is checked with lstat before
use: a symlink, junction or other reparse point, or anything that is not
a directory, is refused (MediaPathError), and the folder must resolve
inside `downloaded-media`. The output folder itself may be reached
through a link, as elsewhere. A final name is inspected with lstat too:
a link or a non-regular file there is a conflict, never followed or
adopted.
'''

# import modules
import errno
import hashlib
import os
import re
import stat
import uuid

# import local submodules
from telegram_tracker.credentials.home import is_within
from telegram_tracker.media.classify import ALLOWED_EXTENSIONS, TYPES
from telegram_tracker.storage.records import StorageError


MEDIA_FOLDER = 'downloaded-media'
LEDGER_FILE = 'media.sqlite3'
PARTIAL_FOLDER = '.partial'
PARTIAL_SUFFIX = '.part'
TYPE_FOLDERS = {'image': 'images', 'video': 'videos', 'audio': 'audios', 'file': 'files'}
HASH_PREFIX = 16
READ_BLOCK = 1024 * 1024

# what inspect_final finds at a final name
ABSENT = 'absent'
SAME = 'same'
DIFFERENT = 'different'
NOT_REGULAR = 'not_regular'

RELATIVE_PATH = re.compile(
	r'^([1-9][0-9]*)/(' + '|'.join(TYPE_FOLDERS.values()) + r')/([1-9][0-9]*)-([0-9a-f]{16})\.([a-z0-9]+)$'
)
PARTIAL_NAME = re.compile(r'^[1-9][0-9]*-[1-9][0-9]*--?[0-9]+\.part$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')

# Windows ERROR_HANDLE_DISK_FULL and ERROR_DISK_FULL
DISK_FULL_WINERRORS = (39, 112)


def is_disk_full(exc):
	return isinstance(exc, OSError) and (
		exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) in DISK_FULL_WINERRORS
	)


class MediaPathError(StorageError):
	'''
	A path under downloaded-media cannot be used safely: a link or
	reparse point, not a directory, outside the folder, or not a
	generated name. Nothing was written through it.
	'''


class PathConflict(MediaPathError):
	'''
	A final name holds other content, a link or a non-regular file; it
	was left as it is. `reason` is the ledger's failure reason.
	'''
	reason = 'path_conflict'


def positive_int(value, label):
	if type(value) is not int or value <= 0:
		raise ValueError(f'{label} must be a positive integer, not {value!r}')
	return value


def check_sha256(value):
	if not isinstance(value, str) or not SHA256.match(value):
		raise ValueError(f'not a lower-case SHA-256 hex digest: {value!r}')
	return value


def check_run_uuid(value):
	'''
	The canonical text of a UUID (the only form used as a folder name).
	'''
	try:
		if isinstance(value, str) and str(uuid.UUID(value)) == value:
			return value
	except ValueError:
		pass
	raise ValueError(f'not a canonical run UUID: {value!r}')


def file_name(message_id, sha256, ext):
	positive_int(message_id, 'message_id')
	check_sha256(sha256)
	if ext not in ALLOWED_EXTENSIONS:
		raise ValueError(f'extension {ext!r} is not allowed')
	return f'{message_id}-{sha256[:HASH_PREFIX]}.{ext}'


def relative_path(channel_id, media_type, message_id, sha256, ext):
	'''
	The blob's path relative to downloaded-media, with '/' separators.
	'''
	positive_int(channel_id, 'channel_id')
	if media_type not in TYPES:
		raise ValueError(f'unknown media type {media_type!r}')
	return f'{channel_id}/{TYPE_FOLDERS[media_type]}/{file_name(message_id, sha256, ext)}'


def check_relative(path):
	'''
	`path` when it is a generated relative path (as stored in the
	ledger), else MediaPathError.
	'''
	match = RELATIVE_PATH.match(path) if isinstance(path, str) else None
	if match is None or match.group(5) not in ALLOWED_EXTENSIONS:
		raise MediaPathError(f'{path!r} is not a generated media path')
	return path


def partial_name(channel_id, message_id, media_id):
	positive_int(channel_id, 'channel_id')
	positive_int(message_id, 'message_id')
	if type(media_id) is not int:
		raise ValueError(f'media_id must be an integer, not {media_id!r}')
	return f'{channel_id}-{message_id}-{media_id}{PARTIAL_SUFFIX}'


def is_link(info):
	return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_reparse_tag', 0))


def sync_folder(path):
	'''
	Make a new name in folder `path` durable. POSIX only: Windows cannot
	open a folder for fsync, and NTFS journals the name itself.
	'''
	if os.name == 'nt':
		return
	fd = os.open(path, os.O_RDONLY)
	try:
		os.fsync(fd)
	finally:
		os.close(fd)


def lstat_or_none(path):
	try:
		return os.lstat(path)
	except (FileNotFoundError, NotADirectoryError):
		return None
	except OSError as exc:
		raise MediaPathError(f'cannot check {path} ({exc.strerror or type(exc).__name__})') from None


class MediaLayout:
	'''
	The downloaded-media folder of one output folder.
	'''
	def __init__(self, output_folder):
		self.output_folder = output_folder
		self.root = os.path.join(output_folder, MEDIA_FOLDER)

	@property
	def ledger_path(self):
		return os.path.join(self.root, LEDGER_FILE)

	@property
	def partial_root(self):
		return os.path.join(self.root, PARTIAL_FOLDER)

	def path_of(self, relative):
		'''
		Absolute path of a generated relative path; nothing is checked on
		disk.
		'''
		return os.path.join(self.root, *check_relative(relative).split('/'))

	'''

	Folders

	'''

	def check_folder(self, path, *, create):
		'''
		`path` (under root) as a real directory: missing is created when
		`create` is set (returns False when it is missing and not created),
		and its new name is synced in its parent. A link, reparse point or
		non-directory raises MediaPathError, and so does a folder that
		cannot be created, except on a full disk: that OSError is raised
		as it is, so that it is reported as a full disk.
		'''
		info = lstat_or_none(path)
		if info is None:
			if not create:
				return False
			try:
				os.mkdir(path)
				# the files published in it are synced, but not this new name
				sync_folder(os.path.dirname(path))
			except FileExistsError:
				pass
			except OSError as exc:
				if is_disk_full(exc):
					raise
				raise MediaPathError(f'cannot create {path} ({exc.strerror or type(exc).__name__})') from None
			info = lstat_or_none(path)
			if info is None:
				raise MediaPathError(f'{path} disappeared while it was being created')
		if is_link(info):
			raise MediaPathError(f'{path} is a link; refusing to use it')
		if not stat.S_ISDIR(info.st_mode):
			raise MediaPathError(f'{path} is not a folder')
		return True

	def ensure(self, *parts, create=True):
		'''
		The folder root/parts..., each level checked (and created when
		missing, with `create`) and the result confined to root. Returns
		None when a level is missing and `create` is not set.
		'''
		path = self.root
		levels = [path]
		for part in parts:
			path = os.path.join(path, part)
			levels.append(path)
		for level in levels:
			if not self.check_folder(level, create=create):
				return None
		if not is_within(path, self.root):
			raise MediaPathError(f'{path} resolves outside {self.root}')
		return path

	def type_folder(self, channel_id, media_type, *, create=True):
		positive_int(channel_id, 'channel_id')
		return self.ensure(str(channel_id), TYPE_FOLDERS[media_type], create=create)

	def partial_folder(self, run_uuid, *, create=True):
		return self.ensure(PARTIAL_FOLDER, check_run_uuid(run_uuid), create=create)

	def final_path(self, relative, *, create=True):
		'''
		Absolute path for a generated relative path, its folders checked
		(and created with `create`).
		'''
		channel, folder, _ = check_relative(relative).split('/')
		parent = self.ensure(channel, folder, create=create)
		if parent is None:
			return None
		return os.path.join(parent, relative.rsplit('/', 1)[1])


'''

Files

'''


def file_digest(path):
	'''
	(bytes, sha256) of the regular file at `path`. Raises MediaPathError
	when it is a link or not a regular file, or when the file opened is
	not the one inspected (replaced meanwhile).
	'''
	info = lstat_or_none(path)
	if info is None:
		raise FileNotFoundError(path)
	if is_link(info) or not stat.S_ISREG(info.st_mode):
		raise MediaPathError(f'{path} is not a regular file')
	digest = hashlib.sha256()
	size = 0
	with open(path, 'rb') as reader:
		if not os.path.samestat(os.fstat(reader.fileno()), info):
			raise MediaPathError(f'{path} changed while it was being read')
		while block := reader.read(READ_BLOCK):
			digest.update(block)
			size += len(block)
	return size, digest.hexdigest()


def inspect_final(path, sha256, size):
	'''
	What is at a final name, compared with the content (size, sha256)
	about to be published there: ABSENT, SAME, DIFFERENT, or NOT_REGULAR
	(a link, reparse point, folder or other non-regular file; never
	followed).
	'''
	info = lstat_or_none(path)
	if info is None:
		return ABSENT
	if is_link(info) or not stat.S_ISREG(info.st_mode):
		return NOT_REGULAR
	if info.st_size != size:
		return DIFFERENT
	try:
		found = file_digest(path)
	except FileNotFoundError:
		return ABSENT
	except MediaPathError:
		return NOT_REGULAR
	return SAME if found == (size, sha256) else DIFFERENT


def check_final(path, sha256, size):
	'''
	ABSENT (the content can be published at `path`) or SAME (the file
	there is that content, to be adopted). Anything else at `path` raises
	PathConflict and is never overwritten, followed or adopted.
	'''
	found = inspect_final(path, sha256, size)
	if found in (ABSENT, SAME):
		return found
	what = 'other content' if found == DIFFERENT else 'a link or a non-regular file'
	raise PathConflict(f'{path} holds {what}; it was left as it is')
