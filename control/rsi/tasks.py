"""The owner's tasks (`tasks/<id>/`), their check scripts, and what the control program does in a workspace.

A task is `tasks/<id>/task.md` with four `## ` sections (design §5.4), optionally `input/` (copied to the workspace's
`in/<id>/` before a run) and `check.py` (run on the collected results afterwards). Only the owner writes `tasks/`.

The Lab writes the workspace and the control program runs outside every sandbox, so the workspace's `in/`, `out/`
and `.tmp/` are handled without ever following a link: entries are looked at with lstat, opened with O_NOFOLLOW
relative to an already opened folder, and removed relative to that folder. Collected results keep only regular
files (copied by content) and folders. set_aside_git renames what git would refuse to store, so that versioning a
workspace never fails on it.
"""
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rsi.paths import safe_id
from rsi.settings import show
from rsi.versions import VersionStore

SECTIONS = ('目标', '输入', '要求', '完成判据')
TASK_FILE, CHECK_FILE, INPUT_FOLDER = 'task.md', 'check.py', 'input'
HEADING = re.compile(r'##[ \t]+(.*?)[ \t]*')
FENCE = re.compile(r'(```|~~~)')
CHECK_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
CHECK_MESSAGE_LIMIT = 500                           # characters of the check's first output line that are kept
CHECK_TIMED_OUT = '检查脚本超时'
_READ_LIMIT = 4 * CHECK_MESSAGE_LIMIT + 4           # bytes of that line read at most (UTF-8 is up to 4 bytes a char)
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class Task:
    id: str
    sections: dict[str, str]
    folder: Path
    check: Path | None
    inputs: Path | None


def _sections(text: str, path: Path) -> dict[str, str]:
    """Section name -> its text. Lines before the first `## ` heading (a title) are not part of any section; a
    heading inside a fenced code block is text. An unknown or repeated heading is a ValueError."""
    sections, current, fenced = {}, None, False
    for line in text.splitlines():
        if FENCE.match(line.lstrip()):
            fenced = not fenced
        heading = None if fenced else HEADING.fullmatch(line)
        if heading is None:
            if current is not None:
                sections[current].append(line)
            continue
        current = heading.group(1)
        if current not in SECTIONS:
            raise ValueError(f'{path} 里有不认识的段落“{current}”。任务只能有这四段：{"、".join(SECTIONS)}。')
        if current in sections:
            raise ValueError(f'{path} 里“{current}”一段出现了不止一次。')
        sections[current] = []
    return {name: '\n'.join(lines).strip() for name, lines in sections.items()}


def load_task(tasks: Path, task_id: str) -> Task:
    """tasks/<task_id>/ as a Task; ValueError (in Chinese) for a bad id, a missing task, an unknown or repeated
    heading, and naming the first of the four sections that is missing or empty."""
    safe_id(task_id, '任务编号')
    folder = Path(tasks) / task_id
    path = folder / TASK_FILE
    try:
        text = path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        raise ValueError(f'找不到任务 {task_id}：没有 {path}') from None
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f'{path} 读不了：{error}') from None
    sections = _sections(text, path)
    for name in SECTIONS:
        if not sections.get(name):
            raise ValueError(f'{path} 缺少“{name}”一段，或这一段是空的。任务要有四段：'
                             f'{"、".join(SECTIONS)}，每段以“## 段名”一行开头。')
    check, inputs = folder / CHECK_FILE, folder / INPUT_FOLDER
    return Task(task_id, {name: sections[name] for name in SECTIONS}, folder,
                check if check.is_file() else None, inputs if inputs.is_dir() else None)


def results_sentence(name: str) -> str:
    """The prompt's sentence saying where the results go."""
    return f'结果写到 out/{name}/。'


def prompt_for(task: Task) -> str:
    """The four sections, then where the inputs are (only when the task has inputs) and where the results go."""
    parts = [f'## {name}\n\n{task.sections[name]}' for name in SECTIONS]
    if task.inputs is not None:
        parts.append(f'输入文件在 in/{task.id}/。')
    parts.append(results_sentence(task.id))
    return '\n\n'.join(parts) + '\n'


# The check script.

def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _keep(head: bytearray, chunk: bytes, done: bool) -> bool:
    """Add chunk to the first line collected so far; True once that line is complete (or long enough)."""
    if done:
        return True
    head += chunk
    if b'\n' in head:
        del head[head.index(b'\n'):]
        return True
    if len(head) >= _READ_LIMIT:
        del head[_READ_LIMIT:]
        return True
    return False


def run_check(task: Task, out: Path, timeout: float = 60) -> dict | None:
    """Run the task's check.py on the results folder; None when the task has no check script.

    It runs with this program's own interpreter (never one the Lab can write), `-E` and a fixed PATH, in the task
    folder, in its own process group. Only its first stdout line is kept, cut to 500 characters; stderr is dropped,
    since a traceback could show the expected answer to Meta. After `timeout` seconds the group is killed and the
    check counts as failed with the message 检查脚本超时. Returns {'passed', 'message', 'exit_code'}.
    """
    if task.check is None:
        return None
    command = [sys.executable, '-E', '-B', '-X', 'utf8', str(task.check), os.path.abspath(out)]
    process = subprocess.Popen(command, cwd=task.folder, env={'PATH': CHECK_PATH}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    head, line_done, timed_out = bytearray(), False, False
    deadline = time.monotonic() + timeout
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            stream_open = True
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if stream_open and selector.select(min(remaining, 0.1)):
                    try:
                        chunk = os.read(fd, 1 << 16)
                    except BlockingIOError:
                        continue
                    if chunk:
                        line_done = _keep(head, chunk, line_done)
                        continue
                    stream_open = False
                    selector.unregister(fd)
                if process.poll() is not None:
                    break
                if not stream_open:
                    time.sleep(min(remaining, 0.05))
        while not timed_out and not line_done:        # what the script wrote just before it ended
            try:
                chunk = os.read(fd, 1 << 16)
            except BlockingIOError:
                break
            if not chunk:
                break
            line_done = _keep(head, chunk, line_done)
    finally:
        _kill_group(process)          # on time-out, and whatever the script left running in its group
        process.wait()
        process.stdout.close()
    if timed_out:
        return {'passed': False, 'message': CHECK_TIMED_OUT, 'exit_code': None}
    message = head.decode('utf-8', errors='replace').strip()[:CHECK_MESSAGE_LIMIT]
    return {'passed': process.returncode == 0, 'message': message, 'exit_code': process.returncode}


# The workspace folders `in/` and `out/`.

def _open_folder(path, dir_fd: int | None = None) -> int:
    """An fd of a real folder; OSError if it is a link or not a folder."""
    return os.open(path, _FOLDER, dir_fd=dir_fd)


def _kind(name: str, dir_fd: int) -> str | None:
    """'folder', 'link' or 'other' for the entry of an open folder, without following it; None if missing."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    return 'folder' if stat.S_ISDIR(mode) else 'link' if stat.S_ISLNK(mode) else 'other'


def _remove(name: str, dir_fd: int) -> None:
    """Remove an entry of an open folder: a folder with its content, anything else by unlinking it (a link is
    removed, never followed)."""
    if _kind(name, dir_fd) == 'folder':
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _describe(kind: str) -> str:
    return '是符号链接' if kind == 'link' else '不是文件夹'


def _top_folder(workspace: Path, name: str, problems: list[str], *, create: bool) -> tuple[int, int | None]:
    """(fd of the workspace, fd of its folder `name` or None). A `name` that is a link or not a folder is removed
    and reported; with create, a missing (or removed) one is made."""
    workspace_fd = _open_folder(workspace)
    try:
        kind = _kind(name, workspace_fd)
        if kind not in (None, 'folder'):
            done = '已删掉并重建为文件夹' if create else '已删掉'
            problems.append(f'工作区里的 {name} {_describe(kind)}，{done}（它由控制程序管理，不跟随链接）')
            os.unlink(name, dir_fd=workspace_fd)
            kind = None
        if kind is None:
            if not create:
                return workspace_fd, None
            os.mkdir(name, dir_fd=workspace_fd)
        return workspace_fd, _open_folder(name, workspace_fd)
    except BaseException:
        os.close(workspace_fd)
        raise


def _close(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            os.close(fd)


def clear(workspace: Path, folder: str, name: str, *, report: bool = False) -> list[str]:
    """Remove <workspace>/<folder>/<name> (`folder` is `in` or `out`) and then <folder> itself if it is left empty,
    never following a link. Returns the problems: a <folder> that was a link or not a folder (removed), and with
    report, a <name> that was there (a leftover of an earlier run)."""
    safe_id(name, '任务编号')
    problems = []
    workspace_fd, folder_fd = _top_folder(workspace, folder, problems, create=False)
    try:
        if folder_fd is not None:
            if _kind(name, folder_fd) is not None:
                _remove(name, folder_fd)
                if report:
                    problems.append(f'{folder}/{name} 在运行前就有（以前的运行留下的），已删掉，不算这次的结果')
            if not os.listdir(folder_fd):
                os.rmdir(folder, dir_fd=workspace_fd)
    finally:
        _close(folder_fd, workspace_fd)
    return problems


def real_folder(workspace: Path, name: str) -> list[str]:
    """Make <workspace>/<name> (a single name such as `.tmp`) a real folder: a link or anything else by that name
    is removed, never followed, and a folder made in its place; a real folder is left as it is. Returns the
    problems (Chinese lines)."""
    problems = []
    _close(*_top_folder(workspace, name, problems, create=True))
    return problems


def place_inputs(task: Task, workspace: Path) -> list[str]:
    """Copy tasks/<id>/input/ to <workspace>/in/<id>/, links copied as links; a leftover in/<id> is removed first.
    Returns the problems (an `in` that was a link or not a folder, removed and made afresh)."""
    problems = clear(workspace, 'in', task.id)
    if task.inputs is None:
        return problems
    workspace_fd, in_fd = _top_folder(workspace, 'in', problems, create=True)
    _close(in_fd, workspace_fd)
    # No agent runs while the inputs are placed, so the path checked just now still names the same folder.
    shutil.copytree(task.inputs, Path(workspace) / 'in' / task.id, symlinks=True)
    return problems


# Collected results: regular files and folders only.

KIND_NAMES = {stat.S_IFLNK: '符号链接', stat.S_IFIFO: '命名管道', stat.S_IFSOCK: '套接字', stat.S_IFCHR: '字符设备',
              stat.S_IFBLK: '块设备'}
MAX_DEPTH = 64              # folder levels collected below out/<id>/
MAX_DROPPED_LINES = 100     # problem lines naming dropped entries; the rest are counted in one more line


def _kind_name(mode: int) -> str:
    return KIND_NAMES.get(stat.S_IFMT(mode), '特殊文件')


class _Dropped:
    """Problem lines about entries left out of the record, at most MAX_DROPPED_LINES plus one counting the rest."""

    def __init__(self) -> None:
        self.lines, self.more = [], 0

    def add(self, line: str) -> None:
        if len(self.lines) < MAX_DROPPED_LINES:
            self.lines.append(line)
        else:
            self.more += 1

    def done(self) -> list[str]:
        return self.lines + ([f'另有 {self.more} 项没有收进记录，原因同上，不再逐条列出'] if self.more else [])


def _grant(name: str, dir_fd: int, mode: int, needed: int) -> None:
    """Give the owner the permission bits `needed` on an entry (never through a link) if it lacks them."""
    if stat.S_IMODE(mode) & needed != needed:
        os.chmod(name, stat.S_IMODE(mode) | needed, dir_fd=dir_fd, follow_symlinks=False)


def _copy_file(name: str, dir_fd: int, target: Path) -> str | None:
    """Copy the regular file `name` of an open folder, by content, to the new file target (so the record never
    shares an inode with a file the Lab can still write). None, or the kind of thing it was if not a regular file."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            return _kind_name(info.st_mode)
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, 'wb') as copy:
                shutil.copyfileobj(source, copy, 1 << 20)
                os.fchmod(copy.fileno(), stat.S_IMODE(info.st_mode) & 0o755 | 0o600)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return None


def _copy_folder(folder_fd: int, dest: Path, label: str, dropped: _Dropped, depth: int) -> None:
    """Copy the content of the open folder into the existing folder dest: regular files by content, folders level
    by level up to MAX_DEPTH. Every other entry (a link, a FIFO, a socket, a device) is left out and reported."""
    for name in sorted(os.listdir(folder_fd)):
        where = show(f'{label}/{name}')
        try:
            mode = os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                if depth >= MAX_DEPTH:
                    dropped.add(f'{where} 在第 {MAX_DEPTH} 层以下，没有收进记录')
                    continue
                _grant(name, folder_fd, mode, stat.S_IRWXU)
                child = _open_folder(name, folder_fd)
                try:
                    (dest / name).mkdir()
                    _copy_folder(child, dest / name, f'{label}/{name}', dropped, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                _grant(name, folder_fd, mode, stat.S_IRUSR)
                kind = _copy_file(name, folder_fd, dest / name)
                if kind is not None:
                    dropped.add(f'{where} 是{kind}，没有收进记录，已从工作区删掉')
            else:
                dropped.add(f'{where} 是{_kind_name(mode)}，没有收进记录，已从工作区删掉')
        except OSError as error:
            dropped.add(f'{where} 读不了（{error.strerror or error}），没有收进记录')


def collect_out(workspace: Path, task_id: str, dest: Path) -> list[str]:
    """Take <workspace>/out/<task_id>/ out of the workspace into dest, and return the problems (Chinese lines).

    The record holds only regular files (copied by content) and folders: the owner's check script runs outside
    every sandbox and would follow a link. Every link, FIFO, socket or device under out/<task_id>/ is left out, one
    problem line each naming its path and kind. An `out` or `out/<task_id>` that is itself a link, or not a folder,
    is not collected. Nothing is ever followed through a link; afterwards out/<task_id> is removed from the workspace
    (and `out` too if it is left empty). dest must not exist; it is always created, empty when there was nothing to
    collect, so a check script always gets a folder.
    """
    safe_id(task_id, '任务编号')
    dest = Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(f'{dest} already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    problems = []
    workspace_fd, out_fd = _top_folder(workspace, 'out', problems, create=False)
    try:
        dest.mkdir()
        kind = None if out_fd is None else _kind(task_id, out_fd)
        if kind == 'folder':
            _grant(task_id, out_fd, os.stat(task_id, dir_fd=out_fd, follow_symlinks=False).st_mode, stat.S_IRWXU)
            dropped = _Dropped()
            results_fd = _open_folder(task_id, out_fd)
            try:
                _copy_folder(results_fd, dest, f'out/{task_id}', dropped, 1)
            finally:
                os.close(results_fd)
            problems += dropped.done()
            try:
                shutil.rmtree(task_id, dir_fd=out_fd)
            except OSError as error:
                problems.append(f'out/{task_id} 已收进记录，但没能从工作区删掉：{error}')
        elif kind is not None:
            problems.append(f'out/{task_id} {_describe(kind)}，没有收进记录，已从工作区删掉')
            os.unlink(task_id, dir_fd=out_fd)
        if out_fd is not None and not os.listdir(out_fd):
            os.rmdir('out', dir_fd=workspace_fd)
    finally:
        _close(out_fd, workspace_fd)
    return problems


# Names git refuses to store.

# Code points that HFS+ ignores in names, and git with core.protectHFS too (git's next_hfs_char).
_HFS_IGNORABLE = re.compile('[‌-‏‪-‮⁪-⁯﻿]')
_GITMODULES_SHORT = re.compile(r'(?:gi7eba|gi7eb|gi7e|gi7|gi|g|)~[1-9][0-9]*')
KEPT = '.rsi-kept'


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


def _free_name(base: str, dir_fd: int) -> str:
    """base, or base-2, base-3, ...: the first name no entry of the open folder has."""
    number = 1
    while True:
        name = base if number == 1 else f'{base}-{number}'
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return name
        number += 1


def set_aside_git(workspace: Path) -> list[str]:
    """Rename, in place, every entry of the workspace that git would refuse to store in a version, so that versioning
    never fails on it: a nested `.git` (folder, file or link, at any depth, in any form git takes for `.git`) becomes
    `.git.rsi-kept`, a link named like `.gitmodules` becomes `.gitmodules.rsi-kept`; `-2`, `-3`, ... is added when
    the name is taken. Links are never followed, and the top-level entries that are never versioned (.tmp, out, in)
    are not entered. Returns one Chinese problem line per renamed entry, naming both paths.
    """
    skipped = {entry.rstrip('/').lower() for entry in VersionStore.EXCLUDED}
    problems = []
    root_fd = _open_folder(workspace)
    try:
        for top, folders, files, top_fd in os.fwalk('.', dir_fd=root_fd, follow_symlinks=False):
            prefix = '' if top == '.' else top.removeprefix('./') + '/'
            if not prefix:
                folders[:] = [name for name in folders if name.lower() not in skipped]
            for name in sorted(folders + files):
                if is_dotgit(name):
                    base = '.git' + KEPT
                elif is_dotgitmodules(name) and _kind(name, top_fd) == 'link':
                    base = '.gitmodules' + KEPT
                else:
                    continue
                new = _free_name(base, top_fd)
                os.rename(name, new, src_dir_fd=top_fd, dst_dir_fd=top_fd)
                if name in folders:
                    folders[folders.index(name)] = new              # still entered, under its new name
                problems.append(f'{show(prefix + name)} 是 git 不收的名字（嵌套的 .git 之类），'
                                f'已改名为 {show(prefix + new)}，以便存成版本')
    finally:
        os.close(root_fd)
    return problems
