"""OS operations used on RSI workspaces.

POSIX calls are forwarded to os. On Windows, NT RootDirectory makes each
child lookup relative to an already open directory. Reparse points are
opened as entries when examining, renaming or removing them. A request for
a no-follow file descriptor refuses every kind of reparse point.
"""
import os as _os


def __getattr__(name):
    return getattr(_os, name)


def lock_exclusive(fd, *, wait=True):
    """Hold an exclusive lock until the descriptor is closed."""
    if _os.name == 'nt':
        overlapped = _Overlapped()
        overlapped.OffsetHigh = 0x40000000  # serialize writers without locking bytes read by status
        flags = 2 if wait else 3  # LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY
        if not _lock_file(_handle(fd), flags, 0, 1, 0, _byref(overlapped)):
            code = _ct.get_last_error()
            if not wait and code == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(_errno.EAGAIN, 'file is already locked')
            raise _ct.WinError(code)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


if _os.name == 'nt':
    import ctypes as _ct
    import errno as _errno
    import msvcrt as _crt
    import re as _re
    import secrets as _secrets
    import stat as _stat
    import struct as _struct
    from contextlib import contextmanager as _contextmanager
    from contextlib import ExitStack as _ExitStack
    from ctypes import byref as _byref, wintypes as _w

    # These flags have no CRT equivalent. They are consumed here before a
    # native handle is turned into a binary-mode CRT file descriptor.
    O_DIRECTORY = 1 << 26
    O_NOFOLLOW = 1 << 27
    O_NONBLOCK = 1 << 28

    _nt = _ct.WinDLL('ntdll')
    _kernel = _ct.WinDLL('kernel32', use_last_error=True)
    _HANDLE = _w.HANDLE
    _ULONG = _w.DWORD
    _PTR = _ct.c_void_p
    _SIZE = _ct.c_size_t

    class _Unicode(_ct.Structure):
        _fields_ = [('Length', _w.USHORT), ('MaximumLength', _w.USHORT), ('Buffer', _PTR)]

    class _Object(_ct.Structure):
        _fields_ = [('Length', _ULONG), ('RootDirectory', _HANDLE), ('ObjectName', _ct.POINTER(_Unicode)),
                    ('Attributes', _ULONG), ('SecurityDescriptor', _PTR), ('SecurityQualityOfService', _PTR)]

    class _IoStatus(_ct.Structure):
        _fields_ = [('Status', _PTR), ('Information', _SIZE)]

    class _Rename(_ct.Structure):
        _fields_ = [('Replace', _ct.c_ubyte), ('RootDirectory', _HANDLE), ('NameLength', _ULONG),
                    ('Name', _w.WCHAR * 1)]

    class _Basic(_ct.Structure):
        _fields_ = [('times', _ct.c_longlong * 4), ('attributes', _ULONG)]

    class _Overlapped(_ct.Structure):
        _fields_ = [('Internal', _SIZE), ('InternalHigh', _SIZE), ('Offset', _ULONG), ('OffsetHigh', _ULONG),
                    ('hEvent', _HANDLE)]

    def _bind(lib, name, result, *args):
        method = getattr(lib, name)
        method.restype, method.argtypes = result, args
        return method

    _create = _bind(_nt, 'NtCreateFile', _w.LONG, _ct.POINTER(_HANDLE), _ULONG, _ct.POINTER(_Object),
                    _ct.POINTER(_IoStatus), _PTR, _ULONG, _ULONG, _ULONG, _ULONG, _PTR, _ULONG)
    _set = _bind(_nt, 'NtSetInformationFile', _w.LONG, _HANDLE, _ct.POINTER(_IoStatus), _PTR, _ULONG, _ULONG)
    _enumerate = _bind(_nt, 'NtQueryDirectoryFile', _w.LONG, _HANDLE, _HANDLE, _PTR, _PTR,
                       _ct.POINTER(_IoStatus), _PTR, _ULONG, _ULONG, _ct.c_ubyte, _PTR, _ct.c_ubyte)
    _dos_error = _bind(_nt, 'RtlNtStatusToDosError', _ULONG, _w.LONG)
    _close_handle = _bind(_kernel, 'CloseHandle', _w.BOOL, _HANDLE)
    _file_info = _bind(_kernel, 'GetFileInformationByHandleEx', _w.BOOL, _HANDLE, _ct.c_int, _PTR, _ULONG)
    _set_info = _bind(_kernel, 'SetFileInformationByHandle', _w.BOOL, _HANDLE, _ct.c_int, _PTR, _ULONG)
    _final_name = _bind(_kernel, 'GetFinalPathNameByHandleW', _ULONG, _HANDLE, _w.LPWSTR, _ULONG, _ULONG)
    _volume_info = _bind(_kernel, 'GetVolumeInformationByHandleW', _w.BOOL, _HANDLE, _PTR, _ULONG, _PTR,
                         _PTR, _PTR, _w.LPWSTR, _ULONG)
    _flush = _bind(_kernel, 'FlushFileBuffers', _w.BOOL, _HANDLE)
    _lock_file = _bind(_kernel, 'LockFileEx', _w.BOOL, _HANDLE, _ULONG, _ULONG, _ULONG, _ULONG,
                       _ct.POINTER(_Overlapped))

    _READ_ATTR = 0x80
    _WRITE_ATTR = 0x100
    _SYNCHRONIZE = 0x00100000
    _DELETE = 0x10000
    _SHARE_ALL = 7
    _OPEN = 1
    _CREATE = 2
    _OPEN_IF = 3
    _DIRECTORY = 1
    _NON_DIRECTORY = 0x40
    _OPEN_REPARSE = 0x00200000
    _READ_ONLY = 1
    _REPARSE = 0x400

    def _handle(fd):
        return _crt.get_osfhandle(fd)

    def _checked(status, path):
        if status < 0:
            error = _ct.WinError(_dos_error(status))
            error.filename = _os.fspath(path)
            raise error

    def _child(path):
        name = _os.fsdecode(path)
        if not name or name in ('.', '..') or any(char in name for char in ('/', '\\', ':', '\0')):
            raise OSError(_errno.EINVAL, 'expected one directory entry', name)
        return name

    def _nt_name(path):
        path = _os.path.abspath(_os.fsdecode(path))
        if path.startswith('\\\\?\\UNC\\'):
            path = '\\\\' + path[8:]
        elif path.startswith('\\\\?\\'):
            path = path[4:]
        if path.startswith('\\\\.\\') or '\0' in path:
            raise OSError(_errno.EINVAL, 'expected a filesystem path', path)
        if path.startswith('\\\\'):
            return '\\??\\UNC\\' + path[2:]
        drive, tail = _os.path.splitdrive(path)
        if len(drive) != 2 or drive[1] != ':' or not tail.startswith('\\'):
            raise OSError(_errno.EINVAL, 'expected a drive or UNC path', path)
        return '\\??\\' + path

    def _native(path, parent, access, disposition=_OPEN, options=0, *, reopen=False, share=_SHARE_ALL,
                volume_root=False):
        if reopen:
            name = ''
        elif volume_root:
            name = path  # validated kernel volume name from _pinned_target
        else:
            name = _nt_name(path) if parent is None else _child(path)
        payload = name.encode('utf-16-le', 'surrogatepass')
        if len(payload) > 65532:
            raise OSError(_errno.ENAMETOOLONG, 'name is too long', name)
        storage = _ct.create_string_buffer(payload)
        string = _Unicode(len(payload), len(payload), _ct.addressof(storage))
        attrs = _Object(_ct.sizeof(_Object), None if parent is None else _handle(parent),
                        _ct.pointer(string), 0x40, None, None)  # OBJ_CASE_INSENSITIVE
        handle = _HANDLE()
        status = _create(_byref(handle), access | _SYNCHRONIZE, _byref(attrs), _byref(_IoStatus()), None,
                         0, share, disposition, options | 0x20, None, 0)  # synchronous non-alert
        _checked(status, path)
        return handle.value

    @_contextmanager
    def _entry(path, parent, access, options=_OPEN_REPARSE, *, share=_SHARE_ALL):
        handle = _native(path, parent, access, options=options, share=share)
        try:
            yield handle
        finally:
            _close_handle(handle)

    def _basic(handle):
        data = _Basic()
        if not _file_info(handle, 0, _byref(data), _ct.sizeof(data)):
            raise _ct.WinError(_ct.get_last_error())
        return data

    def _descriptor(handle, flags):
        try:
            return _crt.open_osfhandle(handle, flags | _os.O_BINARY | _os.O_NOINHERIT)
        except BaseException:
            _close_handle(handle)
            raise

    def open(path, flags, mode=0o777, *, dir_fd=None):
        special = O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK
        if dir_fd is None and not flags & special:
            return _os.open(path, flags | _os.O_BINARY, mode)
        create = _OPEN_IF if flags & _os.O_CREAT else _OPEN
        if flags & _os.O_EXCL and flags & _os.O_CREAT:
            create = _CREATE
        access_mode = flags & (_os.O_WRONLY | _os.O_RDWR)
        rights = (0x80000000, 0x40000000, 0xC0000000)[access_mode]  # GENERIC_READ/WRITE
        options = _DIRECTORY if flags & O_DIRECTORY else _NON_DIRECTORY
        if flags & O_NOFOLLOW:
            options |= _OPEN_REPARSE
        handle = _native(path, dir_fd, rights | _READ_ATTR, create, options)
        fd = _descriptor(handle, access_mode | (flags & _os.O_APPEND))
        try:
            if flags & O_NOFOLLOW and _basic(handle).attributes & _REPARSE:
                raise OSError(_errno.ELOOP, 'refusing to open a reparse point', _os.fspath(path))
            if flags & _os.O_TRUNC:
                _os.ftruncate(fd, 0)
            return fd
        except BaseException:
            _os.close(fd)
            raise

    def fstat(fd):
        info = _os.fstat(fd)
        if getattr(info, 'st_file_attributes', 0) & _REPARSE and not _stat.S_ISLNK(info.st_mode):
            # Treat junctions and every other reparse tag as a link. Retain
            # Windows' non-sequence fields (notably ns times and file attributes).
            _, (values, fields) = info.__reduce__()
            return _os.stat_result((_stat.S_IFLNK | _stat.S_IMODE(info.st_mode), *values[1:]), fields)
        return info

    def stat(path, *, dir_fd=None, follow_symlinks=True):
        if isinstance(path, int):
            return fstat(path)
        if dir_fd is None and follow_symlinks:
            return _os.stat(path)
        handle = _native(path, dir_fd, _READ_ATTR, options=0 if follow_symlinks else _OPEN_REPARSE)
        fd = _descriptor(handle, _os.O_RDONLY)
        try:
            return fstat(fd)
        finally:
            _os.close(fd)

    def lstat(path, *, dir_fd=None):
        return stat(path, dir_fd=dir_fd, follow_symlinks=False)

    def _names(path):
        buffer = _ct.create_string_buffer(64 * 1024)
        io = _IoStatus()
        restart = True
        while True:
            status = _enumerate(_handle(path), None, None, None, _byref(io), buffer, len(buffer),
                                12, False, None, restart)  # FileNamesInformation
            if status & 0xffffffff == 0x80000006:  # STATUS_NO_MORE_FILES
                return
            _checked(status, '<directory>')
            offset = 0
            while offset < io.Information:
                following, _, count = _struct.unpack_from('<III', buffer, offset)
                start = offset + 12
                name = buffer[start:start + count].decode('utf-16-le', 'surrogatepass')
                if name not in ('.', '..'):
                    yield name
                if not following:
                    break
                offset += following
            restart = False

    def listdir(path='.'):
        return list(_names(path)) if isinstance(path, int) else _os.listdir(path)

    @_contextmanager
    def scandir(path='.'):
        if isinstance(path, int):
            from types import SimpleNamespace
            # RSI callers consume only entry.name; enumeration remains lazy.
            yield (SimpleNamespace(name=name) for name in _names(path))
        else:
            with _os.scandir(path) as entries:
                yield entries

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return _os.mkdir(path, mode)
        # Access is granted by the parent Windows DACL, also for mode 0700.
        handle = _native(path, dir_fd, _READ_ATTR, _CREATE, _DIRECTORY | _OPEN_REPARSE)
        _close_handle(handle)

    @_contextmanager
    def _pinned_target(folder, child):
        """A local NTFS target address stable for the duration of a rename.

        Some Windows filesystem filters do not accept a user directory
        handle in FileRenameInformation. Pin every ancestor against rename
        or deletion and match the final handle to the caller's. The temporary
        child keeps the last directory nonempty. NTFS cannot make a nonempty
        directory a reparse point. Delete-on-close removes the child even
        if the controller terminates. If any pin fails the rename is refused.
        """
        raw = _handle(folder)
        filesystem = _ct.create_unicode_buffer(32)
        if not _volume_info(raw, None, 0, None, None, None, filesystem, len(filesystem)):
            raise _ct.WinError(_ct.get_last_error())
        if filesystem.value != 'NTFS':
            raise OSError(_errno.ENOTSUP, 'this volume does not support a pinned NTFS rename')
        path = _ct.create_unicode_buffer(32768)
        count = _final_name(raw, path, len(path), 1)  # VOLUME_NAME_GUID, normalized
        if not count:
            raise _ct.WinError(_ct.get_last_error())
        if count >= len(path):
            raise OSError(_errno.ENAMETOOLONG, 'destination directory name is too long')
        volume, separator, suffix = path.value[4:].partition('\\')
        if not separator or not path.value.startswith('\\\\?\\') or not _re.fullmatch(
                r'Volume\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}', volume):
            raise OSError(_errno.ENOTSUP, 'expected a local volume GUID path')
        base = '\\??\\' + volume + '\\'
        chain = [base, *filter(None, suffix.split('\\'))]
        with _ExitStack() as pins:
            parent = None
            directories = []
            for index, part in enumerate(chain):
                handle = _native(part, parent, _READ_ATTR | 1, options=_DIRECTORY | _OPEN_REPARSE,
                                 share=3, volume_root=index == 0)  # FILE_LIST_DIRECTORY, share read/write only
                parent = _descriptor(handle, _os.O_RDONLY)
                pins.callback(_os.close, parent)
                directories.append(handle)
                if _basic(handle).attributes & _REPARSE:
                    raise OSError(_errno.ELOOP, 'cannot pin a reparse point in the destination')
            if not _os.path.samestat(fstat(parent), fstat(folder)):
                raise OSError(_errno.EBUSY, 'destination directory changed')
            guard = _native('.rsi-rename-' + _secrets.token_hex(16), parent, _DELETE | _READ_ATTR,
                            _CREATE, _NON_DIRECTORY | _OPEN_REPARSE | 0x1000, share=1)  # DELETE_ON_CLOSE
            pins.callback(_close_handle, guard)
            if any(_basic(handle).attributes & _REPARSE for handle in directories):
                raise OSError(_errno.ELOOP, 'destination has a reparse point')
            yield base + _os.path.join(suffix, _child(child))

    def _rename_handle(source, name, parent, replace):
        encoded = name.encode('utf-16-le', 'surrogatepass')
        size = max(_ct.sizeof(_Rename), _Rename.Name.offset + len(encoded))
        buffer = _ct.create_string_buffer(size)
        request = _Rename.from_buffer(buffer)
        request.Replace, request.RootDirectory, request.NameLength = replace, parent, len(encoded)
        _ct.memmove(_ct.addressof(buffer) + _Rename.Name.offset, encoded, len(encoded))
        return _set(source, _byref(_IoStatus()), buffer, size, 10)  # FileRenameInformation

    def _rename(src, dst, src_fd, dst_fd, replace):
        if src_fd is None and dst_fd is None:
            return (_os.replace if replace else _os.rename)(src, dst)
        if dst_fd is None:
            raise OSError(_errno.EINVAL, 'expected a destination directory handle')
        # With a simple name and NULL RootDirectory, the NT rename keeps
        # the source in its current parent. No path lookup is involved.
        same_parent = src_fd is not None and _os.path.samestat(fstat(src_fd), fstat(dst_fd))
        with _entry(src, src_fd, _DELETE, share=3) as handle:
            result = _rename_handle(handle, _child(dst), None if same_parent else _handle(dst_fd), replace)
            if not same_parent and result & 0xffffffff == 0xc0000008:  # STATUS_INVALID_HANDLE
                with _pinned_target(dst_fd, dst) as target:
                    result = _rename_handle(handle, target, None, replace)
            _checked(result, src)

    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        return _rename(src, dst, src_dir_fd, dst_dir_fd, False)

    def replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        return _rename(src, dst, src_dir_fd, dst_dir_fd, True)

    def _remove(path, parent, directory):
        # IGNORE_READONLY_ATTRIBUTE also needs FILE_WRITE_ATTRIBUTES on
        # the handle. Request the additional right only for read-only entries.
        access = _DELETE | _READ_ATTR
        with _entry(path, parent, _READ_ATTR) as observed:
            if _basic(observed).attributes & _READ_ONLY:
                access |= _WRITE_ATTR
        with _entry(path, parent, access) as handle:
            attributes = _basic(handle).attributes
            if (attributes & 0x10 and not attributes & _REPARSE) and not directory:
                raise IsADirectoryError(_errno.EISDIR, 'entry is a directory', path)
            if not attributes & 0x10 and directory:
                raise NotADirectoryError(_errno.ENOTDIR, 'entry is not a directory', path)
            # Remove this name even when read-only, without changing the
            # attributes of another hard link to the same underlying file.
            flags = _ULONG(1 | 2 | 16)  # DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
            status = _set(handle, _byref(_IoStatus()), _byref(flags), _ct.sizeof(flags), 64)
            if status & 0xffffffff in (0xc0000002, 0xc0000003, 0xc000000d, 0xc00000bb):
                delete = _ct.c_ubyte(1)
                status = _set(handle, _byref(_IoStatus()), _byref(delete), 1, 13)
            _checked(status, path)

    def unlink(path, *, dir_fd=None):
        if dir_fd is None:
            return _os.unlink(path)
        _remove(path, dir_fd, False)

    def rmdir(path, *, dir_fd=None):
        if dir_fd is None:
            return _os.rmdir(path)
        _remove(path, dir_fd, True)

    def _set_mode(handle, mode):
        basic = _basic(handle)
        bits = basic.attributes
        updated = bits & ~_READ_ONLY if mode & _stat.S_IWRITE else bits | _READ_ONLY
        if updated != bits:
            change = _Basic()
            change.attributes = updated or 0x80
            if not _set_info(handle, 0, _byref(change), _ct.sizeof(change)):
                raise _ct.WinError(_ct.get_last_error())

    def chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is None and follow_symlinks:
            return _os.chmod(path, mode)
        with _entry(path, dir_fd, _READ_ATTR | _WRITE_ATTR, 0 if follow_symlinks else _OPEN_REPARSE) as handle:
            _set_mode(handle, mode)

    def fchmod(fd, mode):
        _set_mode(_handle(fd), mode)

    def fsync(fd):
        if not _stat.S_ISDIR(_os.fstat(fd).st_mode):
            return _os.fsync(fd)
        # FlushFileBuffers needs write access even for a directory. Reopen
        # the existing object by handle, preserving its identity.
        handle = _native('', fd, 0x40000000, options=_DIRECTORY | _OPEN_REPARSE, reopen=True)
        try:
            if not _flush(handle):
                raise _ct.WinError(_ct.get_last_error())
        finally:
            _close_handle(handle)
