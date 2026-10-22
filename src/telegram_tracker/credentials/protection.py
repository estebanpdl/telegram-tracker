# -*- coding: utf-8 -*-
'''
Owner-only protection for the application home and the files in it.

POSIX: directory mode 0700, file mode 0600, owned by the current user.
Windows: a protected DACL (inheritance from the parent disabled) whose only
entry grants full control to the current user's SID; on the directory the
entry is inherited by new files, so a session file Telethon creates there
gets the same protection. Administrators and the OS itself are outside
this boundary.

`protect` applies the protection and checks it; `check` only checks.
Both raise ProtectionError when the protection is missing or cannot be
applied, so callers fail closed instead of writing secrets to a readable
path.
'''

# import modules
import os
import stat


class ProtectionError(Exception):
	'''
	A path is not (or cannot be made) private to the current user.
	'''


def reject_link(path):
	'''
	Raise ProtectionError when `path` exists as a symlink or junction, or
	when it cannot be inspected.
	'''
	try:
		info = os.lstat(path)
	except (FileNotFoundError, NotADirectoryError):
		# POSIX reports a file in place of a parent folder as ENOTDIR,
		# Windows as not found: either way nothing exists at `path`
		return
	except OSError as exc:
		raise ProtectionError(f'cannot check {path} ({exc.strerror or type(exc).__name__})') from None
	tag = getattr(info, 'st_reparse_tag', 0)
	if stat.S_ISLNK(info.st_mode) or tag:
		raise ProtectionError(f'{path} is a link; refusing to use it')


def ensure_private_dir(path):
	'''
	Create `path` (and missing parents) if needed and make it private to
	the current user. Refuses links.
	'''
	reject_link(path)
	if not os.path.isdir(path):
		try:
			os.makedirs(path, mode=0o700, exist_ok=True)
		except OSError as exc:
			raise ProtectionError(f'cannot create {path} ({exc.strerror or type(exc).__name__})') from None
	reject_link(path)
	protect(path, directory=True)


def protect(path, *, directory=False):
	'''
	Make `path` private to the current user, then check it.
	'''
	try:
		if os.name == 'nt':
			_windows_protect(path, directory)
		else:
			os.chmod(path, 0o700 if directory else 0o600)
	except OSError as exc:
		raise ProtectionError(
			f'cannot restrict access to {path} ({exc.strerror or type(exc).__name__})'
		) from None
	check(path)


def check(path):
	'''
	Raise ProtectionError unless only the current user can access `path`.
	'''
	try:
		if os.name == 'nt':
			private = _windows_is_private(path)
		else:
			info = os.stat(path)
			private = info.st_uid == os.getuid() and not info.st_mode & 0o077
	except OSError as exc:
		raise ProtectionError(
			f'cannot check access to {path} ({exc.strerror or type(exc).__name__})'
		) from None
	if not private:
		raise ProtectionError(f'{path} is accessible to other accounts')


'''

Windows (advapi32 through ctypes; no extra dependency)

'''

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_USER = 1


def _api():
	import ctypes
	from ctypes import wintypes

	advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
	kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

	kernel32.GetCurrentProcess.restype = wintypes.HANDLE
	kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
	kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
	advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
	advapi32.GetTokenInformation.argtypes = [
		wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
	]
	advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
	advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
		wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)
	]
	advapi32.GetSecurityDescriptorDacl.argtypes = [
		ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
		ctypes.POINTER(wintypes.BOOL)
	]
	advapi32.SetNamedSecurityInfoW.argtypes = [
		wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
		ctypes.c_void_p, ctypes.c_void_p
	]
	advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
	advapi32.GetNamedSecurityInfoW.argtypes = [
		wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
		ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
	]
	advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
	advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
		ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
		ctypes.POINTER(wintypes.ULONG)
	]
	return ctypes, wintypes, advapi32, kernel32


def _fail(ctypes, what, code=None):
	code = ctypes.get_last_error() if code is None else code
	raise OSError(code, f'{what} failed (Windows error {code})')


def _current_user_sid():
	'''
	The current process user's SID as a string (S-1-5-...).
	'''
	ctypes, wintypes, advapi32, kernel32 = _api()
	token = wintypes.HANDLE()
	if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
		_fail(ctypes, 'OpenProcessToken')
	try:
		size = wintypes.DWORD()
		advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
		buffer = ctypes.create_string_buffer(size.value)
		if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
			_fail(ctypes, 'GetTokenInformation')
		sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
		text = wintypes.LPWSTR()
		if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
			_fail(ctypes, 'ConvertSidToStringSidW')
		try:
			return text.value
		finally:
			kernel32.LocalFree(text)
	finally:
		kernel32.CloseHandle(token)


def _owner_only_sddl(directory):
	inherit = 'OICI' if directory else ''
	return f'D:P(A;{inherit};FA;;;{_current_user_sid()})'


def _sddl_of(ctypes, wintypes, advapi32, kernel32, descriptor):
	text = wintypes.LPWSTR()
	if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
			descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, ctypes.byref(text), None):
		_fail(ctypes, 'ConvertSecurityDescriptorToStringSecurityDescriptorW')
	try:
		return text.value
	finally:
		kernel32.LocalFree(text)


def _descriptor(ctypes, wintypes, advapi32, sddl):
	descriptor = ctypes.c_void_p()
	if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
			sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None):
		_fail(ctypes, 'ConvertStringSecurityDescriptorToSecurityDescriptorW')
	return descriptor


def _windows_protect(path, directory):
	ctypes, wintypes, advapi32, kernel32 = _api()
	descriptor = _descriptor(ctypes, wintypes, advapi32, _owner_only_sddl(directory))
	try:
		present, defaulted = wintypes.BOOL(), wintypes.BOOL()
		dacl = ctypes.c_void_p()
		if not advapi32.GetSecurityDescriptorDacl(
				descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
			_fail(ctypes, 'GetSecurityDescriptorDacl')
		code = advapi32.SetNamedSecurityInfoW(
			os.fspath(path), SE_FILE_OBJECT,
			DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
			None, None, dacl, None
		)
		if code:
			_fail(ctypes, 'SetNamedSecurityInfoW', code)
	finally:
		kernel32.LocalFree(descriptor)


def _aces(sddl):
	'''
	(flags, [ace fields]) of the DACL part of an SDDL string.
	'''
	dacl = sddl.split('D:', 1)[1] if 'D:' in sddl else ''
	flags = dacl.split('(', 1)[0]
	aces = [ace.split(';') for ace in dacl[len(flags):].strip('()').split(')(') if ace]
	return flags, aces


def _windows_is_private(path):
	'''
	True when every DACL entry, explicit or inherited from the private
	home, allows only the current user (compared in the form Windows
	prints the SID back).
	'''
	ctypes, wintypes, advapi32, kernel32 = _api()
	expected = _owner_only_sddl(False)
	reference = _descriptor(ctypes, wintypes, advapi32, expected)
	try:
		user = _aces(_sddl_of(ctypes, wintypes, advapi32, kernel32, reference))[1][0][5]
	finally:
		kernel32.LocalFree(reference)

	descriptor = ctypes.c_void_p()
	code = advapi32.GetNamedSecurityInfoW(
		os.fspath(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
		None, None, None, None, ctypes.byref(descriptor)
	)
	if code:
		_fail(ctypes, 'GetNamedSecurityInfoW', code)
	try:
		_, aces = _aces(_sddl_of(ctypes, wintypes, advapi32, kernel32, descriptor))
	finally:
		kernel32.LocalFree(descriptor)

	return bool(aces) and all(
		len(ace) == 6 and ace[0] == 'A' and ace[5] == user for ace in aces
	)
