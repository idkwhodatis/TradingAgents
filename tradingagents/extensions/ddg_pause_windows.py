"""Fail-closed local Windows store using pywin32, never POSIX permission emulation.

All ancestors stay open without FILE_SHARE_DELETE. Objects are checked through
those handles; new objects receive a protected owner-only DACL at creation.
Only local fixed drives with persistent ACLs are supported. Administrators and
SYSTEM are trusted for ancestor integrity, as root is on POSIX.
"""
from __future__ import annotations

import errno
import ntpath
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32security


@dataclass(eq=False)
class DirectoryHandle:
    path: str
    handles: list


@dataclass(eq=False)
class FileHandle:
    handle: object


def _native_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except pywintypes.error as exc:
            code = exc.winerror
            if code in (2, 3):
                raise FileNotFoundError(errno.ENOENT, "pause file missing") from None
            raise OSError(
                errno.EIO, f"Windows pause store unavailable ({function.__name__}, winerror={code})"
            ) from exc
    return wrapped


def _sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _attributes():
    sid = _sid()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED,
                                            win32security.SE_DACL_PROTECTED)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def _security(handle, private):
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    current = win32security.ConvertSidToStringSid(_sid())
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    acl = descriptor.GetSecurityDescriptorDacl()
    control, _ = descriptor.GetSecurityDescriptorControl()
    trusted = {current, "S-1-5-18", "S-1-5-32-544",
               "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
    if acl is None or owner not in ({current} if private else trusted):
        raise OSError("unsafe pause store security")
    if private and not control & win32security.SE_DACL_PROTECTED:
        raise OSError("unprotected pause store DACL")
    if private and acl.GetAceCount() != 1:
        raise OSError("non-private pause store DACL")
    dangerous = (win32con.GENERIC_ALL | win32con.GENERIC_WRITE | win32con.WRITE_DAC
                 | win32con.WRITE_OWNER | win32con.DELETE | ntsecuritycon.FILE_DELETE_CHILD
                 | ntsecuritycon.FILE_WRITE_DATA | ntsecuritycon.FILE_WRITE_ATTRIBUTES
                 | ntsecuritycon.FILE_WRITE_EA)
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        kind, flags = ace[0]
        if kind not in (win32security.ACCESS_ALLOWED_ACE_TYPE, win32security.ACCESS_DENIED_ACE_TYPE):
            raise OSError("unsupported pause store ACE")
        principal = win32security.ConvertSidToStringSid(ace[2])
        # OWNER RIGHTS is the object owner, not an additional account. The owner
        # was already checked above; do not extend this to CREATOR OWNER/group.
        # https://learn.microsoft.com/windows-server/identity/ad-ds/manage/understand-security-identifiers
        effective_principal = owner if principal == "S-1-3-4" else principal
        if private:
            if (kind != win32security.ACCESS_ALLOWED_ACE_TYPE or flags != 0
                    or principal != current or ace[1] != ntsecuritycon.FILE_ALL_ACCESS):
                raise OSError("non-private pause store ACE")
        elif (kind == win32security.ACCESS_ALLOWED_ACE_TYPE
              and not flags & win32con.INHERIT_ONLY_ACE
              and effective_principal not in trusted and ace[1] & dangerous):
            raise OSError(
                f"writable pause store ancestor (mask={ace[1]:#x}, flags={flags:#x}, sid={principal})"
            )


def _name(name):
    if (not name or name in (".", "..") or any(c in name for c in '\\/:\x00')
            or name.endswith((".", " "))):
        raise OSError("invalid pause filename")
    return name


@_native_errors
def directory(path, create=False):
    path = str(path)
    drive, tail = ntpath.splitdrive(path)
    if (len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha()
            or drive[1] != ':' or not tail.startswith('\\') or '/' in path):
        raise OSError("pause store requires a local absolute drive path")
    parts = tail[1:].split('\\')
    if not parts or any(p in ('', '.', '..') or p.endswith(('.', ' '))
                        or ':' in p or '\x00' in p for p in parts):
        raise OSError("invalid pause store path")
    root = drive + '\\'
    if win32file.GetDriveType(root) != win32con.DRIVE_FIXED:
        raise OSError("pause store requires a local fixed drive")
    # FILE_PERSISTENT_ACLS is 0x8 in the documented filesystem flags.
    if not win32api.GetVolumeInformation(root)[3] & 0x8:
        raise OSError("pause store requires persistent ACLs")
    result = DirectoryHandle(path, [])
    try:
        current = root
        for index, part in enumerate([None] + parts):
            if part is not None:
                current = ntpath.join(current, part)
            try:
                handle = _open_directory(current, writable=create and index == len(parts))
            except pywintypes.error as exc:
                if exc.winerror not in (2, 3) or part is None:
                    raise
                if not create:
                    close_directory(result)
                    return None
                try:
                    win32file.CreateDirectory(current, _attributes())
                except pywintypes.error as exists:
                    if exists.winerror != 183:
                        raise
                handle = _open_directory(current, writable=create and index == len(parts))
            result.handles.append(handle)
            info = win32file.GetFileInformationByHandle(handle)
            if (not info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY
                    or info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT):
                raise OSError("unsafe pause store directory")
            try:
                _security(handle, private=index == len(parts))
            except OSError as exc:
                raise OSError(f"pause directory security check failed at depth {index}") from exc
        return result
    except BaseException:
        close_directory(result)
        raise


def _open_directory(path, writable=False):
    access = win32con.READ_CONTROL | ntsecuritycon.FILE_READ_ATTRIBUTES
    if writable:
        access |= ntsecuritycon.FILE_ADD_FILE
    return win32file.CreateFile(
        path, access,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)


@_native_errors
def close_directory(directory):
    while directory.handles:
        directory.handles.pop().Close()


@_native_errors
def open_file(directory, name, mode='read'):
    if mode not in ('read', 'lock'):
        raise OSError("invalid pause file mode")
    access = win32con.GENERIC_READ
    if mode == 'lock':
        access |= win32con.GENERIC_WRITE
    sharing = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
    if mode == 'read' and name == 'ddg-block.json':
        sharing |= win32con.FILE_SHARE_DELETE
    handle = win32file.CreateFile(
        ntpath.join(directory.path, _name(name)), access, sharing,
        _attributes() if mode == 'lock' else None,
        win32con.OPEN_ALWAYS if mode == 'lock' else win32con.OPEN_EXISTING,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    result = FileHandle(handle)
    try:
        check_file(result)
        return result
    except BaseException:
        handle.Close()
        raise


@_native_errors
def check_file(file):
    info = win32file.GetFileInformationByHandle(file.handle)
    if (win32file.GetFileType(file.handle) != win32con.FILE_TYPE_DISK
            or info[0] & (win32con.FILE_ATTRIBUTE_DIRECTORY | win32con.FILE_ATTRIBUTE_REPARSE_POINT)
            or info[7] != 1):
        raise OSError("unsafe pause file")
    _security(file.handle, private=True)
    return (info[5] << 32) | info[6]


@_native_errors
def close_file(file):
    file.handle.Close()


@_native_errors
def read_at(file, size, offset):
    win32file.SetFilePointer(file.handle, offset, win32con.FILE_BEGIN)
    try:
        return win32file.ReadFile(file.handle, size)[1]
    except pywintypes.error as exc:
        if exc.winerror == 38:  # ERROR_HANDLE_EOF
            return b''
        raise


@_native_errors
def write_at(file, data, offset):
    win32file.SetFilePointer(file.handle, offset, win32con.FILE_BEGIN)
    return win32file.WriteFile(file.handle, data)[1]


@_native_errors
def truncate(file, size):
    win32file.SetFilePointer(file.handle, size, win32con.FILE_BEGIN)
    win32file.SetEndOfFile(file.handle)


@_native_errors
def flush(file):
    win32file.FlushFileBuffers(file.handle)


def _lock(file, exclusive, offset=1):
    flags = win32con.LOCKFILE_FAIL_IMMEDIATELY
    if exclusive:
        flags |= win32con.LOCKFILE_EXCLUSIVE_LOCK
    try:
        # Lock a byte beyond marker contents so unlocked status can inspect it.
        overlap = pywintypes.OVERLAPPED()
        overlap.Offset = offset
        win32file.LockFileEx(file.handle, flags, 1, 0, overlap)
    except pywintypes.error as exc:
        if exc.winerror == 33:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError(errno.EWOULDBLOCK, "pause lock busy") from None
        raise


@_native_errors
def lock_shared(file):
    _lock(file, False)


@_native_errors
def lock_exclusive(file):
    _lock(file, True)


@contextmanager
def _metadata(directory, exclusive):
    # This independent lock is held only around filesystem metadata operations,
    # never across HTTP. Read-only handles support LockFileEx as documented.
    file = open_file(directory, 'ddg-block.lock')
    acquired = False
    try:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                _lock(file, exclusive, offset=2)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("pause metadata lock timed out") from None
                time.sleep(0.005)
        yield
    finally:
        try:
            if acquired:
                overlap = pywintypes.OVERLAPPED()
                overlap.Offset = 2
                win32file.UnlockFileEx(file.handle, 1, 0, overlap)
        finally:
            close_file(file)


@_native_errors
def read_state(directory, limit):
    if not isinstance(limit, int) or limit < 0:
        raise OSError("invalid pause state limit")
    try:
        with _metadata(directory, False):
            file = open_file(directory, 'ddg-block.json')
            try:
                if check_file(file) > limit:
                    raise OSError("oversized pause state")
                raw = read_at(file, limit + 1, 0)
                if len(raw) > limit:
                    raise OSError("oversized pause state")
                return raw
            finally:
                close_file(file)
    except FileNotFoundError:
        # No coordination lock is safe only when no state exists either. Do not
        # return bytes from an uncoordinated read or silently ignore state.
        file = open_file(directory, 'ddg-block.json')
        close_file(file)
        raise OSError("pause state has no usable metadata lock") from None


@_native_errors
def write_state(directory, raw):
    try:
        with _metadata(directory, True):
            _write_state_locked(directory, raw)
    except FileNotFoundError:
        raise OSError("pause metadata lock missing") from None


def _write_state_locked(directory, raw):
    try:
        existing = open_file(directory, 'ddg-block.json')
    except FileNotFoundError:
        pass
    else:
        close_file(existing)
    name = '.ddg-' + uuid.uuid4().hex + '.tmp'
    handle = win32file.CreateFile(
        ntpath.join(directory.path, name),
        win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.DELETE,
        win32con.FILE_SHARE_READ, _attributes(), win32con.CREATE_NEW,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_WRITE_THROUGH, None)
    temporary = FileHandle(handle)
    renamed = False
    try:
        check_file(temporary)
        if write_at(temporary, raw, 0) != len(raw):
            raise OSError("incomplete pause state write")
        flush(temporary)
        # Rename the verified open object, never reopen its temporary pathname.
        win32file.SetFileInformationByHandle(handle, win32file.FileRenameInfo, {
            'ReplaceIfExists': True, 'RootDirectory': directory.handles[-1],
            'FileName': 'ddg-block.json'})
        renamed = True
        flush(temporary)
    finally:
        try:
            if not renamed:
                win32file.SetFileInformationByHandle(handle, win32file.FileDispositionInfo, True)
        finally:
            handle.Close()
