"""What the control program does inside a folder the Lab writes (work/, trial/), never following a link.

The control program runs outside every sandbox, and the Lab may leave anything behind: links, FIFOs, folders and
files with mode 000, hard links to files elsewhere, trees hundreds of levels deep, huge sparse files, names git
refuses. None of it may block, starve or misdirect a later run. So everything here

- looks at an entry with lstat and opens it with O_NOFOLLOW relative to an already opened folder, and writes,
  renames or removes only relative to such a folder;
- holds at most about MAX_DEPTH folders open at a time (macOS starts a program with 256 open files);
- gives the owner a permission only through the entry itself, and never on a file with more than one hard link
  (that inode is shared with a file elsewhere);
- reports every step in one Chinese line, at most MAX_LINES lines plus one line counting the rest.

What versioning cannot take is moved aside (renamed, never copied) to library/moved/<label>/<relative path>: the
Lab can still reach it there, and the version store never sees it.
"""
import os
import re
import resource
import secrets
import stat
import subprocess
import sys
from pathlib import Path

from rsi.paths import safe_id
from rsi.settings import show
from rsi.versions import VersionStore

MAX_DEPTH = 48                  # folder levels walked; deeper folders are moved aside (or, when removing, up)
MAX_PATH_BYTES = 700            # a folder whose relative path is longer is moved aside: git's paths stay < 1024
BIG_FILE = 256 << 20            # a regular file bigger than this (by st_size) is moved aside before versioning
MAX_LINES = 100                 # problem lines named one by one; the rest are counted
LINEAR_TRIES = 64               # name, name-2, ... tried in order before a random suffix is used (never fails)
EXCLUDED = tuple(entry.rstrip('/').lower() for entry in VersionStore.EXCLUDED)     # never versioned: .tmp, out, in
KEPT = '.rsi-kept'
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Code points HFS+ ignores in names, as git with core.protectHFS does (git's next_hfs_char).
_HFS_IGNORABLE = re.compile(r'[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]')
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


# Primitives on entries of an open folder.

def open_folder(name, dir_fd: int | None = None) -> int:
    """An fd of a real folder; OSError if it is a link or not a folder."""
    return os.open(name, _FOLDER, dir_fd=dir_fd)


def kind(name: str, dir_fd: int) -> str | None:
    """'folder', 'link' or 'other' for an entry of an open folder, without following it; None if missing."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    return 'folder' if stat.S_ISDIR(mode) else 'link' if stat.S_ISLNK(mode) else 'other'


def grant(name: str, dir_fd: int, info: os.stat_result, needed: int) -> bool:
    """Whether the owner has the permission bits `needed` on the entry, after giving them through the entry itself
    if missing. A file with more than one hard link is never changed (False if it lacks the bits)."""
    mode = stat.S_IMODE(info.st_mode)
    if mode & needed == needed:
        return True
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
        return False
    os.chmod(name, mode | needed, dir_fd=dir_fd, follow_symlinks=False)
    return True


def _taken(name: str, dir_fd: int) -> bool:
    """Whether the open folder has an entry by that name (one that cannot even be stat'ed counts as there)."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    return True


def _names(base: str, first: int = 1, numbered: bool = False):
    """base (or base-1 when numbered), base-2, base-3, ... for LINEAR_TRIES names, then base-<random> without end:
    however many names the Lab has taken, a free one is always found."""
    for number in range(first, first + LINEAR_TRIES):
        yield base if number == 1 and not numbered else f'{base}-{number}'
    while True:
        yield f'{base}-{secrets.token_hex(8)}'


def free_name(dir_fd: int, base: str, counters: dict | None = None) -> str:
    """base, or base-2, base-3, ... (base-<random> once many are taken): a name no entry of the open folder has.
    counters (base -> next number to try) makes a run of calls in one folder linear instead of quadratic."""
    counters = {} if counters is None else counters
    first = counters.get(base, 1)
    for number, name in enumerate(_names(base, first), start=first):
        if not _taken(name, dir_fd):
            counters[base] = min(number, first + LINEAR_TRIES) + 1
            return name


def remove(name: str, dir_fd: int) -> None:
    """Remove an entry of an open folder with everything under it, never following a link."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    grant(name, dir_fd, info, stat.S_IRWXU)
    top = open_folder(name, dir_fd)
    try:
        empty(top)
    finally:
        os.close(top)
    os.rmdir(name, dir_fd=dir_fd)


def empty(top: int) -> None:
    """Remove everything inside the open folder, never following a link. Folders are given owner rwx as needed; at
    most MAX_DEPTH of them are held open, and a folder deeper than that is first moved up into top and removed in a
    later pass."""
    counters = {}
    while True:
        names = sorted(os.listdir(top))
        if not names:
            return
        stack = [[top, iter(names), None]]
        try:
            while stack:
                fd, pending, _ = stack[-1]
                name = next(pending, None)
                if name is None:
                    _, _, own_name = stack.pop()
                    if stack:
                        os.close(fd)
                        os.rmdir(own_name, dir_fd=stack[-1][0])
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    os.unlink(name, dir_fd=fd)
                elif len(stack) >= MAX_DEPTH:
                    grant(name, fd, info, stat.S_IRWXU)         # macOS moves a folder elsewhere only if writable
                    os.rename(name, free_name(top, '.rsi-deep', counters), src_dir_fd=fd, dst_dir_fd=top)
                else:
                    grant(name, fd, info, stat.S_IRWXU)
                    child = open_folder(name, fd)
                    stack.append([child, iter(sorted(os.listdir(child))), name])
        finally:
            for fd, _, _ in stack[1:]:
                os.close(fd)


def top_folder(workspace: Path, name: str, problems: list[str], *, create: bool) -> tuple[int, int | None]:
    """(fd of the workspace, fd of its top-level folder `name` or None). The folder is given owner rwx; a `name`
    that is a link or not a folder is removed and reported; with create, a missing (or removed) one is made."""
    workspace_fd = open_folder(workspace)
    try:
        try:
            info = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            what = '是符号链接' if stat.S_ISLNK(info.st_mode) else '不是文件夹'
            done = '已删掉并重建为文件夹' if create else '已删掉'
            problems.append(f'工作区里的 {name} {what}，{done}（它由控制程序管理，不跟随链接）')
            os.unlink(name, dir_fd=workspace_fd)
            info = None
        if info is None:
            if not create:
                return workspace_fd, None
            os.mkdir(name, dir_fd=workspace_fd)
        else:
            grant(name, workspace_fd, info, stat.S_IRWXU)
        return workspace_fd, open_folder(name, workspace_fd)
    except BaseException:
        os.close(workspace_fd)
        raise


def close(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            os.close(fd)


def real_folder(workspace: Path, name: str) -> list[str]:
    """Make the top-level <workspace>/<name> (`.tmp`, say) a real folder the owner can use: a link or anything else
    by that name is removed, never followed, and a folder made in its place; a real folder is kept, with owner rwx.
    Returns the problems (Chinese lines)."""
    problems = []
    close(*top_folder(workspace, name, problems, create=True))
    return problems


def clear(workspace: Path, folder: str, name: str, *, report: bool = False) -> list[str]:
    """Remove <workspace>/<folder>/<name> (`folder` is `in` or `out`), and <folder> itself if it is left empty, never
    following a link. Returns the problems: a <folder> that was a link or not a folder (removed), and with report, a
    <name> that was there (a leftover of an earlier run)."""
    safe_id(name, '任务编号')
    problems = []
    workspace_fd, folder_fd = top_folder(workspace, folder, problems, create=False)
    try:
        if folder_fd is not None:
            if kind(name, folder_fd) is not None:
                remove(name, folder_fd)
                if report:
                    problems.append(f'{folder}/{name} 在运行前就有（以前的运行留下的），已删掉，不算这次的结果')
            if not os.listdir(folder_fd):
                os.rmdir(folder, dir_fd=workspace_fd)
    finally:
        close(folder_fd, workspace_fd)
    return problems


def empty_folder(folder: Path) -> None:
    """Remove everything inside folder (one the control program owns, such as trial/), never following a link."""
    fd = open_folder(folder)
    try:
        empty(fd)
    finally:
        os.close(fd)


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


# Before versioning.

def _enter(parent_fd: int, name: str, *, numbered: bool = False) -> tuple[int, str]:
    """An fd of the real folder `name` inside the open folder (made if missing, given owner rwx), and the name used.
    A name taken by anything but a real folder is passed over for name-2, name-3, ... (name-<random> after many)."""
    for candidate in _names(name, numbered=numbered):
        try:
            os.mkdir(candidate, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            info = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                grant(candidate, parent_fd, info, stat.S_IRWXU)
                return open_folder(candidate, parent_fd), candidate
        except FileNotFoundError:
            continue
        except PermissionError:
            continue


def _library_fd(library: Path) -> int:
    """An fd of library/, given owner rwx; OSError if it is a link or not a folder (the Lab can replace it)."""
    try:
        os.mkdir(library)
    except FileExistsError:
        pass
    info = os.lstat(library)
    if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
        os.chmod(library, stat.S_IMODE(info.st_mode) | stat.S_IRWXU, follow_symlinks=False)
    return open_folder(library)


def _to_library(name: str, dir_fd: int, relative: str, library: Path, label: str) -> str:
    current = _library_fd(library)
    used = [library.name]
    try:
        for part in ['moved', label, *relative.split('/')[:-1]]:
            child, actual = _enter(current, part)
            os.close(current)
            current = child
            used.append(actual)
        final = free_name(current, relative.split('/')[-1])
        os.rename(name, final, src_dir_fd=dir_fd, dst_dir_fd=current)
        used.append(final)
    finally:
        os.close(current)
    return '/'.join(used)


def _to_tmp(name: str, dir_fd: int, relative: str, root_fd: int) -> str:
    """Rename the entry into the workspace's own `.tmp/` (never versioned), made a real folder first."""
    try:
        info = os.stat('.tmp', dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink('.tmp', dir_fd=root_fd)
            raise FileNotFoundError
        grant('.tmp', root_fd, info, stat.S_IRWXU)
    except FileNotFoundError:
        os.mkdir('.tmp', dir_fd=root_fd)
    tmp = open_folder('.tmp', root_fd)
    try:
        final = free_name(tmp, 'rsi-moved-' + relative.split('/')[-1][:200])
        os.rename(name, final, src_dir_fd=dir_fd, dst_dir_fd=tmp)
    finally:
        os.close(tmp)
    return f'.tmp/{final}'


def move_aside(name: str, dir_fd: int, relative: str, library: Path, label: str, root_fd: int) -> str:
    """Rename (never copy) an entry of an open folder out of the version's way; return where it went.

    First choice: library/moved/<label>/<relative>, through fds and without following links (the Lab writes
    library/ too): every folder entered gets owner rwx, a folder name taken by anything but a real folder is passed
    over for name-2, ..., and the last name gets -2, ... when taken. If that fails (the Lab made library/ unusable),
    the entry goes into the workspace's own `.tmp/` (root_fd is the workspace), which is never versioned."""
    try:
        return _to_library(name, dir_fd, relative, library, label)
    except OSError:
        return _to_tmp(name, dir_fd, relative, root_fd)


def _visit(name: str, fd: int, prefix: str, depth: int, counters: dict, library: Path, label: str,
           lines: Lines, stack: list) -> None:
    """One entry of the walk of prepare_for_commit; stack[0][0] is the workspace."""
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    linked = stat.S_ISLNK(info.st_mode)
    if is_dotgit(name) or (linked and is_dotgitmodules(name)):
        new = free_name(fd, ('.git' if is_dotgit(name) else '.gitmodules') + KEPT, counters)
        os.rename(name, new, src_dir_fd=fd, dst_dir_fd=fd)
        lines.add(f'{show(prefix + name)} 是 git 不收的名字（嵌套的 .git 之类），已改名为 {show(prefix + new)}，'
                  '以便存成版本')
        name = new
    path = prefix + name
    if stat.S_ISDIR(info.st_mode):
        if depth + 1 > MAX_DEPTH or len(path.encode()) > MAX_PATH_BYTES:
            grant(name, fd, info, stat.S_IRWXU)             # macOS moves a folder to another parent only if writable
            where = move_aside(name, fd, path, library, label, stack[0][0])
            lines.add(f'{show(path)} 嵌套超过 {MAX_DEPTH} 层或路径超过 {MAX_PATH_BYTES} 字节，版本库存不了，'
                      f'已整个移到 {show(where)}')
            return
        grant(name, fd, info, stat.S_IRWXU)
        child = open_folder(name, fd)
        stack.append([child, iter(sorted(os.listdir(child))), path + '/', depth + 1, {}])
    elif stat.S_ISREG(info.st_mode):
        if info.st_size > BIG_FILE:
            where = move_aside(name, fd, path, library, label, stack[0][0])
            lines.add(f'{show(path)} 有 {info.st_size} 字节，超过 {BIG_FILE} 字节，版本库不收，已移到 {show(where)}')
        elif not grant(name, fd, info, stat.S_IRUSR):
            where = move_aside(name, fd, path, library, label, stack[0][0])
            lines.add(f'{show(path)} 读不了，又有别的硬链接（不能改它的权限），版本库存不了，已移到 {show(where)}')


def prepare_for_commit(workspace: Path, library: Path, label: str) -> list[str]:
    """Make a workspace's content storable as a version; return what was done, one Chinese line each (capped).

    The workspace is walked without following links (the top-level .tmp, out and in are never versioned and not
    entered), at most MAX_DEPTH folders deep:
    - an entry git takes for `.git`, or a link git takes for `.gitmodules`, is renamed in place to `.git.rsi-kept`
      (`.gitmodules.rsi-kept`), with -2, -3, ... when that name is taken;
    - a folder gets owner rwx; one deeper than MAX_DEPTH levels, or whose relative path is longer than
      MAX_PATH_BYTES, is moved aside with everything in it;
    - a regular file bigger than BIG_FILE is moved aside; one the owner cannot read gets u+r, or is moved aside when
      it has other hard links.
    Moving aside renames into library/moved/<label>/<relative path>, or into the workspace's `.tmp/` when library/
    is unusable. Links, FIFOs and sockets stay: git stores a link as a link and skips the other two. An entry that
    cannot be handled is reported and passed over.
    """
    lines = Lines()
    root = open_folder(workspace)
    stack = [[root, iter(sorted(os.listdir(root))), '', 0, {}]]
    try:
        while stack:
            fd, pending, prefix, depth, counters = stack[-1]
            name = next(pending, None)
            if name is None:
                stack.pop()
                os.close(fd)
                continue
            if depth == 0 and name.lower() in EXCLUDED:
                continue
            try:
                _visit(name, fd, prefix, depth, counters, library, label, lines, stack)
            except FileNotFoundError:
                continue
            except OSError as error:
                lines.add(f'{show(prefix + name)} 处理不了（{error.strerror or error}），没有整理')
    finally:
        for frame in stack:
            os.close(frame[0])
    return lines.done()


# The backstop: a workspace that cannot be prepared or committed is set apart whole.

def _unlock(path: Path) -> None:
    """Best effort on the entry itself, never through a link: clear its flags (uchg, ...), its ACL and give the
    owner rwx, so it can be moved to another folder."""
    try:
        os.chflags(path, 0, follow_symlinks=False)
    except (AttributeError, NotImplementedError, OSError):
        pass
    if sys.platform == 'darwin' and os.path.exists('/bin/chmod'):
        for command in (['/bin/chmod', '-h', '-N', str(path)], ['/bin/chmod', '-N', str(path)]):
            if os.path.islink(path):
                break
            try:
                done = subprocess.run(command, capture_output=True, timeout=60, env={'PATH': '/usr/bin:/bin'})
                if done.returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        info = os.lstat(path)
        if not stat.S_ISLNK(info.st_mode):
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IRWXU, follow_symlinks=False)
    except OSError:
        pass


def quarantine(layout, folder: Path, label: str) -> Path | None:
    """Set a workspace entry (work/, trial/, ...) apart whole and put a fresh empty folder in its place.

    The entry may be anything the Lab made of it: a folder (mode 000, flagged, with a deny ACL), a link (the link
    itself is moved, never followed) or a file. Its flags, ACL and mode are cleared as far as possible, then it is
    renamed to <root>/quarantine/<label>-<n> (quarantine/ is a real folder the control program owns, under the root
    every agent profile denies). Returns where it went, or None when there was nothing at that path."""
    folder = Path(folder)
    parent = open_folder(folder.parent)
    try:
        moved = None
        if _taken(folder.name, parent):
            try:
                linked = stat.S_ISLNK(os.stat(folder.name, dir_fd=parent, follow_symlinks=False).st_mode)
            except PermissionError:                 # a deny ACL can refuse even stat; a link never does
                linked = False
            if not linked:
                _unlock(folder)
            root = open_folder(layout.root)
            try:
                holder, holder_name = _enter(root, 'quarantine')
            finally:
                os.close(root)
            try:
                target = next(name for name in _names(label, numbered=True) if not _taken(name, holder))
                os.rename(folder.name, target, src_dir_fd=parent, dst_dir_fd=holder)
            finally:
                os.close(holder)
            moved = Path(layout.root) / holder_name / target
        os.mkdir(folder.name, dir_fd=parent)
        return moved
    finally:
        os.close(parent)
