"""The seal, and what the control program does with a sealed workspace.

Agents write work/ and trial/ by path, and a process an agent started may outlive its call, so the control program
never works in such a folder in place. It seals the entry first: one rename inside the RSI root, relative to the
root's descriptor, to `.sealed-<name>-<label>`, a name no agent profile grants (seal). A same-parent rename needs no
permission on the entry itself, works for a folder of any mode, a link or a file, and is atomic; from then on no agent
can create, rename or delete anything in the tree. Everything else happens on that static tree: making it storable
as a version (prepare_for_commit), removing it (remove) or moving it aside to keep it (keep).

Even so, everything here
- looks at an entry with lstat and opens it with O_NOFOLLOW relative to an opened folder, renames and removes only
  relative to such a folder, and changes lock flags, mode bits or an ACL only on the entry itself, never through a
  link and never on a file with more than one hard link (that inode is shared with a file elsewhere);
- holds at most about MAX_DEPTH folders open at a time (macOS starts a program with 256 open files);
- puts a hard cap on every search and every repeated pass: reaching it is an OSError, never a hang;
- reports in Chinese lines, at most MAX_LINES of them plus one line counting the rest.
"""
import ctypes
import errno
import functools
import os
import re
import resource
import secrets
import stat
import sys
from pathlib import Path

from rsi.settings import show
from rsi.versions import VersionStore

MAX_DEPTH = 48                  # folder levels walked; a deeper folder is left out of the version (removal: moved up)
MAX_PATH_BYTES = 700            # a folder whose relative path is longer is left out: git's paths stay < 1024 bytes
BIG_FILE = 256 << 20            # a regular file bigger than this (by st_size) is left out of the version
MAX_LINES = 100                 # problem lines named one by one; the rest are counted
LINEAR_TRIES = 64               # name, name-2, ... tried in order, then RANDOM_TRIES random suffixes, then an error
RANDOM_TRIES = 16
MAX_PASSES = 10_000             # passes of one removal; each takes MAX_DEPTH levels off the deepest folder
SEALED = '.sealed-'             # <root>/.sealed-<name>-<label>: a sealed workspace
EXCLUDED = tuple(entry.rstrip('/').lower() for entry in VersionStore.EXCLUDED)     # never versioned: .tmp, out, …
KEPT = '.rsi-kept'
LOCKS = stat.UF_IMMUTABLE | stat.UF_APPEND      # lock flags (uchg, uappnd): they stop renaming or removing an entry
ACL_TYPE_EXTENDED = 0x00000100
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Code points HFS+ ignores in names, as git with core.protectHFS does (git's next_hfs_char).
_HFS_IGNORABLE = re.compile(r'[‌-‏‪-‮⁪-⁯﻿]')
_GITMODULES_SHORT = re.compile(r'(?:gi7eba|gi7eb|gi7e|gi7|gi|g|)~[1-9][0-9]*')


class Lines:
    """Chinese problem lines, at most MAX_LINES of them plus one line counting the rest."""

    def __init__(self) -> None:
        self.lines, self.more, self.limit = [], 0, MAX_LINES

    def add(self, line: str) -> None:
        if len(self.lines) < self.limit:
            self.lines.append(line)
        else:
            self.more += 1

    def done(self) -> list[str]:
        return self.lines + ([f'另有 {self.more} 条同类情况，不再逐条列出'] if self.more else [])


def raise_open_files_limit() -> None:
    """Raise this process's soft limit of open files as far as the hard limit allows (macOS starts at 256)."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for target in (hard, 1 << 20, 10240):                 # macOS refuses RLIM_INFINITY; its OPEN_MAX is 10240
        if target == resource.RLIM_INFINITY or target <= soft or (hard != resource.RLIM_INFINITY and target > hard):
            continue
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            return
        except (ValueError, OSError):
            continue


# Primitives on entries.

def open_folder(name, dir_fd: int | None = None) -> int:
    """An fd of a real folder; OSError if it is a link or not a folder."""
    return os.open(name, _FOLDER, dir_fd=dir_fd)


def close(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            os.close(fd)


def kind(name: str, dir_fd: int) -> str | None:
    """'folder', 'link' or 'other' for an entry of an open folder, without following it; None if missing."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    return 'folder' if stat.S_ISDIR(mode) else 'link' if stat.S_ISLNK(mode) else 'other'


def grant(name: str, dir_fd: int | None, info: os.stat_result, needed: int) -> bool:
    """Whether the owner has the permission bits `needed` on the entry, after giving them through the entry itself
    if missing. A file with more than one hard link is never changed (False if it lacks the bits)."""
    mode = stat.S_IMODE(info.st_mode)
    if mode & needed == needed:
        return True
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
        return False
    os.chmod(name, mode | needed, dir_fd=dir_fd, follow_symlinks=False)
    return True


@functools.cache
def _acl_functions():
    """libc's acl_init, acl_set_link_np and acl_free (macOS only), or None."""
    if sys.platform != 'darwin':
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        init, set_link, free = libc.acl_init, libc.acl_set_link_np, libc.acl_free
    except (OSError, AttributeError):
        return None
    init.restype, init.argtypes = ctypes.c_void_p, [ctypes.c_int]
    set_link.restype, set_link.argtypes = ctypes.c_int, [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    free.restype, free.argtypes = ctypes.c_int, [ctypes.c_void_p]
    return init, set_link, free


def clear_acl(path: str) -> bool:
    """Give the entry at path an empty extended ACL, on the entry itself and never through a link (macOS
    acl_set_link_np; `chmod -h -N` cannot even open an entry whose ACL denies listing it). Whether that worked."""
    functions = _acl_functions()
    if functions is None:
        return False
    init, set_link, free = functions
    acl = init(0)
    if not acl:
        return False
    try:
        return set_link(os.fsencode(path), ACL_TYPE_EXTENDED, acl) == 0
    finally:
        free(acl)


def _unlock(name: str, dir_fd: int | None, path: str, info: os.stat_result, needed: int) -> None:
    """Clear the entry's lock flags and give the owner the mode bits `needed`, on the entry itself. path is where
    the entry is (chflags has no fd form). A file with other hard links is left as it is."""
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
        return
    flags = getattr(info, 'st_flags', 0)
    if flags & LOCKS:
        os.chflags(path, flags & ~LOCKS, follow_symlinks=False)
    if needed and not stat.S_ISLNK(info.st_mode):
        grant(name, dir_fd, info, needed)


def _opened(name: str, dir_fd: int | None, path: str) -> int:
    """An fd of the folder; if it refuses to open, its ACL is cleared and it is opened once more."""
    try:
        return open_folder(name, dir_fd)
    except PermissionError:
        if not clear_acl(path):
            raise
    return open_folder(name, dir_fd)


def _retrying(action, path: str, info: os.stat_result) -> None:
    """action() (an unlink, rmdir or rename of the entry at path); if an ACL on the entry refuses it, the ACL is
    cleared on the entry itself and action() runs once more. A file with other hard links is never changed."""
    try:
        action()
    except PermissionError:
        if (not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1) or not clear_acl(path):
            raise
        action()


def _listed(name: str, dir_fd: int, path: str) -> tuple[int, list[str]]:
    """An fd of the folder (see _opened) and its entry names, sorted."""
    fd = _opened(name, dir_fd, path)
    try:
        return fd, sorted(os.listdir(fd))
    except BaseException:
        os.close(fd)
        raise


def open_unlocked(name: str, dir_fd: int | None, path: str) -> int:
    """An fd of the folder `name` of an open folder (or of path itself when dir_fd is None), unlocked on the entry
    itself: lock flags cleared, owner rwx, an ACL that refuses opening it cleared. NotADirectoryError if the entry is
    a link or not a folder; OSError if it still cannot be opened."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, '不是文件夹（可能是符号链接或文件）', path)
    _unlock(name, dir_fd, path, info, stat.S_IRWXU)
    return _opened(name, dir_fd, path)


def _taken(name: str, dir_fd: int) -> bool:
    """Whether the open folder has an entry by that name (one that cannot even be stat'ed counts as there)."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    return True


def free_name(dir_fd: int, base: str, counters: dict | None = None) -> str:
    """base, or base-2, base-3, ... (LINEAR_TRIES of them), then base-<random> (RANDOM_TRIES of them): a name no
    entry of the open folder has; FileExistsError when all are taken. counters (base -> next number to try) makes a
    run of calls in one folder linear instead of quadratic."""
    counters = {} if counters is None else counters
    first = counters.get(base, 1)
    for number in range(first, first + LINEAR_TRIES):
        name = base if number == 1 else f'{base}-{number}'
        if not _taken(name, dir_fd):
            counters[base] = number + 1
            return name
    for _ in range(RANDOM_TRIES):
        name = f'{base}-{secrets.token_hex(8)}'
        if not _taken(name, dir_fd):
            return name
    raise FileExistsError(errno.EEXIST, f'找不到空闲的名字（试了 {LINEAR_TRIES + RANDOM_TRIES} 个）', base)


# The seal, and what becomes of a sealed entry.

def seal(root_fd: int, name: str, label: str) -> str | None:
    """Seal the entry `name` (work or trial) of the RSI root, whatever it is: rename it, relative to root_fd, to
    .sealed-<name>-<label> in the same folder. Returns the new name, or None when there is no such entry; OSError
    when the new name is taken or the rename fails (a lock flag or a deny-delete ACL on the entry itself)."""
    sealed = f'{SEALED}{name}-{label}'
    if kind(sealed, root_fd) is not None:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), sealed)
    try:
        os.rename(name, sealed, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except FileNotFoundError:
        return None
    return sealed


def keep(root_fd: int, entry: str, path: str, target: Path) -> None:
    """Move the sealed entry of the root (at path) to target, whose folder is made if missing (only the control
    program writes there). A folder is unlocked first: macOS moves a folder to another parent only when it may
    write the folder itself."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = open_folder(target.parent)
    try:
        if kind(target.name, fd) is not None:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        info = os.stat(entry, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _unlock(entry, root_fd, path, info, stat.S_IRWXU)
        os.rename(entry, target.name, src_dir_fd=root_fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def remove(name: str, dir_fd: int, path: str) -> None:
    """Remove an entry of an open folder (path: where it is) with everything under it, never following a link."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        _unlock(name, dir_fd, path, info, 0)
        _retrying(lambda: os.unlink(name, dir_fd=dir_fd), path, info)
        return
    top = open_unlocked(name, dir_fd, path)
    try:
        empty(top, path)
    finally:
        os.close(top)
    _retrying(lambda: os.rmdir(name, dir_fd=dir_fd), path, info)


def empty(top: int, path: str) -> None:
    """Remove everything inside the open folder at path, never following a link. Entries are unlocked as needed; at
    most MAX_DEPTH folders are held open, and a folder deeper than that is first moved up into top and removed in a
    later pass (at most MAX_PASSES passes). An ACL that refuses removing an entry is cleared (see _retrying)."""
    counters = {}
    for _ in range(MAX_PASSES):
        names = sorted(os.listdir(top))
        if not names:
            return
        stack = [[top, iter(names), None, path, None]]
        try:
            while stack:
                fd, pending, own_name, here, own_info = stack[-1]
                name = next(pending, None)
                if name is None:
                    stack.pop()
                    if stack:
                        os.close(fd)
                        parent = stack[-1][0]
                        _retrying(lambda: os.rmdir(own_name, dir_fd=parent), here, own_info)
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                where = f'{here}/{name}'
                if not stat.S_ISDIR(info.st_mode):
                    _unlock(name, fd, where, info, 0)
                    _retrying(lambda: os.unlink(name, dir_fd=fd), where, info)
                    continue
                _unlock(name, fd, where, info, stat.S_IRWXU)    # macOS moves a folder elsewhere only if writable
                if len(stack) >= MAX_DEPTH:
                    deep = free_name(top, '.rsi-deep', counters)
                    _retrying(lambda: os.rename(name, deep, src_dir_fd=fd, dst_dir_fd=top), where, info)
                else:
                    child, names = _listed(name, fd, where)
                    stack.append([child, iter(names), name, where, info])
        finally:
            for frame in stack[1:]:
                os.close(frame[0])
    raise OSError(errno.EBUSY, f'删了 {MAX_PASSES} 遍还没删完', path)


# Names git refuses to store.

def _ntfs_tail(rest: str, ends: str) -> bool:
    """Only spaces and periods, up to the end or one of the characters `ends` (as git's NTFS checks read it)."""
    for char in rest:
        if char in ends:
            return True
        if char not in ' .':
            return False
    return True


def is_dotgit(name: str) -> bool:
    """Whether git takes the name for `.git` (core.protectHFS and core.protectNTFS, both on by default here): in
    any letter case, with HFS-ignorable code points, with trailing spaces or periods, `git~1`, or followed by `:`."""
    lower = name.lower()
    if _HFS_IGNORABLE.sub('', lower) == '.git':
        return True
    return any(lower.startswith(stem) and _ntfs_tail(lower[len(stem):], ':\\') for stem in ('.git', 'git~1'))


def is_dotgitmodules(name: str) -> bool:
    """Whether git takes the name for `.gitmodules`; git refuses to store a link by such a name."""
    lower = name.lower()
    if _HFS_IGNORABLE.sub('', lower) == '.gitmodules':
        return True
    if lower.startswith('.gitmodules') and _ntfs_tail(lower[len('.gitmodules'):], ':'):
        return True
    if len(lower) >= 8 and lower[:6] == 'gitmod' and lower[6] == '~' and lower[7] in '1234':
        return _ntfs_tail(lower[8:], ':')
    return bool(len(lower) >= 8 and _GITMODULES_SHORT.fullmatch(lower[:8]) and _ntfs_tail(lower[8:], ':'))


# Before versioning a sealed tree.

class _Aside:
    """Entries that must be left out of the version: renamed (never copied) to <folder>/<n>-<name>. The folder is
    made when first needed; only the control program writes there. shown is how lines name it. moved counts what
    was moved; blocked counts what had to be left out but could not be moved out of the tree."""

    def __init__(self, folder: Path, shown: str) -> None:
        self.folder, self.shown, self.moved, self.blocked, self.fd = folder, shown, 0, 0, None

    def take(self, name: str, dir_fd: int, path: str, info: os.stat_result, relative: str, why: str,
             lines: Lines) -> None:
        # A cut on a character boundary: APFS refuses a name that is not valid UTF-8 (EILSEQ).
        target = f'{self.moved + 1}-{os.fsencode(name)[:200].decode("utf-8", "ignore")}'
        try:
            if self.fd is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                self.fd = open_folder(self.folder)
            if stat.S_ISDIR(info.st_mode):
                _unlock(name, dir_fd, path, info, stat.S_IRWXU)  # macOS moves a folder elsewhere only if writable
            _retrying(lambda: os.rename(name, target, src_dir_fd=dir_fd, dst_dir_fd=self.fd), path, info)
        except OSError as error:
            self.blocked += 1
            lines.add(f'{show(relative)} {why}，应排除在版本之外，但移不出去（{error.strerror or error}），'
                      '所以这次不存版本')
            return
        self.moved += 1
        lines.add(f'{show(relative)} {why}，没有存进版本，已移到 {show(self.shown + "/" + target)}')


def _readable(name: str, dir_fd: int, path: str, info: os.stat_result) -> bool:
    """Whether the owner can open the regular file; an ACL that refuses it is cleared, unless the file has other
    hard links."""
    for attempt in range(2):
        try:
            os.close(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd))
            return True
        except PermissionError:
            if attempt or info.st_nlink > 1 or not clear_acl(path):
                return False
    return False


def _visit(name: str, fd: int, prefix: str, depth: int, counters: dict, tree: str, aside: _Aside, lines: Lines,
           stack: list) -> None:
    """One entry of the walk of prepare_for_commit."""
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    path = f'{tree}/{prefix}{name}'
    folder, regular = stat.S_ISDIR(info.st_mode), stat.S_ISREG(info.st_mode)
    _unlock(name, fd, path, info, stat.S_IRWXU if folder else stat.S_IRUSR if regular else 0)
    if is_dotgit(name) or (stat.S_ISLNK(info.st_mode) and is_dotgitmodules(name)):
        new = free_name(fd, ('.git' if is_dotgit(name) else '.gitmodules') + KEPT, counters)
        os.rename(name, new, src_dir_fd=fd, dst_dir_fd=fd)
        lines.add(f'{show(prefix + name)} 是 git 不收的名字（嵌套的 .git 之类），已改名为 {show(prefix + new)}，'
                  '以便存成版本')
        name, path = new, f'{tree}/{prefix}{new}'
    relative = prefix + name
    if folder:
        if depth + 1 > MAX_DEPTH or len(os.fsencode(relative)) > MAX_PATH_BYTES:
            aside.take(name, fd, path, info, relative, f'嵌套超过 {MAX_DEPTH} 层或路径超过 {MAX_PATH_BYTES} 字节', lines)
            return
        try:
            child, names = _listed(name, fd, path)
        except PermissionError:
            aside.take(name, fd, path, info, relative, '打不开', lines)
            return
        stack.append([child, iter(names), relative + '/', depth + 1, {}])
    elif regular:
        if info.st_size > BIG_FILE:
            aside.take(name, fd, path, info, relative, f'有 {info.st_size} 字节，超过 {BIG_FILE} 字节', lines)
        elif not _readable(name, fd, path, info):
            aside.take(name, fd, path, info, relative, '读不了', lines)


def prepare_for_commit(tree: Path, left_out: Path, shown: str) -> tuple[list[str], int, int]:
    """Make a sealed tree storable as a version. Returns the Chinese lines (capped), how many entries were moved
    out of the tree (left out of the version), and how many are blocking: entries that had to be left out but could
    not be moved out, or that could not be handled at all. When any entry is blocking, the tree must not be saved as
    a version: it may hold what the rules leave out. OSError if the tree itself is not a folder that can be opened.

    The tree is walked without following links, at most MAX_DEPTH folders deep; its top-level .tmp, out, in and
    .agents (never versioned) are not entered. Every entry is unlocked on the entry itself (lock flags cleared, a folder
    owner rwx, a regular file owner r, an ACL that refuses opening it cleared), except a file with other hard links.
    An entry git takes for `.git`, or a link git takes for `.gitmodules`, is renamed in place to `.git.rsi-kept`
    (`.gitmodules.rsi-kept`), -2, -3, ... when taken. Left out, i.e. renamed (never copied) to left_out/<n>-<name>
    (shown names left_out in the lines): a folder deeper than MAX_DEPTH or whose relative path is longer than
    MAX_PATH_BYTES, a regular file bigger than BIG_FILE, and a folder or file that cannot be opened; an ACL that
    refuses the move is cleared first. Links, FIFOs and sockets stay: git stores a link as a link and skips the
    other two.
    """
    tree = str(tree)
    lines, aside, failed = Lines(), _Aside(left_out, shown), 0
    root = open_unlocked(tree, None, tree)
    stack = [[root, iter(()), '', 0, {}]]
    try:
        stack[0][1] = iter(sorted(os.listdir(root)))
        while stack:
            fd, pending, prefix, depth, counters = stack[-1]
            name = next(pending, None)
            if name is None:
                os.close(stack.pop()[0])
                continue
            if depth == 0 and name.lower() in EXCLUDED:
                continue
            try:
                _visit(name, fd, prefix, depth, counters, tree, aside, lines, stack)
            except OSError as error:
                failed += 1
                lines.add(f'{show(prefix + name)} 处理不了（{error.strerror or error}），所以这次不存版本')
    finally:
        close(*(frame[0] for frame in stack), aside.fd)
    return lines.done(), aside.moved, aside.blocked + failed
