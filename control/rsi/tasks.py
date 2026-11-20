"""The owner's tasks (`tasks/<id>/`), their check scripts, and what the control program does in a workspace.

A task is `tasks/<id>/task.md` with four `## ` sections (design §5.4), optionally `input/` (copied to the workspace's
`in/<id>/` before a run) and `check.py` (run on the collected results afterwards). Only the owner writes `tasks/`.

The Lab writes the workspace and the control program runs outside every sandbox, so the workspace's `in/` and
`out/` are handled through rsi.workspace, without ever following a link. Collected results keep only regular files
(copied by content) and folders, within a budget.
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
from rsi.workspace import MAX_DEPTH, Lines, clear, close, grant, kind, open_folder, remove, top_folder

SECTIONS = ('目标', '输入', '要求', '完成判据')
TASK_FILE, CHECK_FILE, INPUT_FOLDER = 'task.md', 'check.py', 'input'
HEADING = re.compile(r'##[ \t]+(.*?)[ \t]*')
FENCE = re.compile(r'(```|~~~)')
CHECK_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
CHECK_MESSAGE_LIMIT = 500                           # characters of the check's first output line that are kept
CHECK_TIMED_OUT = '检查脚本超时'
_READ_LIMIT = 4 * CHECK_MESSAGE_LIMIT + 4           # bytes of that line read at most (UTF-8 is up to 4 bytes a char)


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

    It runs with this program's own interpreter (never one the Lab can write), `-E -s` and a fixed PATH, in the task
    folder, in its own process group. Only its first stdout line is kept, cut to 500 characters; stderr is dropped,
    since a traceback could show the expected answer to Meta. After `timeout` seconds the group is killed and the
    check counts as failed with the message 检查脚本超时. Returns {'passed', 'message', 'exit_code'}.
    """
    if task.check is None:
        return None
    command = [sys.executable, '-E', '-s', '-B', '-X', 'utf8', str(task.check), os.path.abspath(out)]
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


# The task's inputs.

def _copy_into(source: Path, folder_fd: int, name: str, depth: int = 1) -> None:
    """Copy the owner's folder `source` as the new folder `name` of the open folder: folders made with
    mkdir(dir_fd), files written with O_EXCL|O_NOFOLLOW, links made again as links. Nothing is opened by a path the
    Lab could redirect, so even an `in` swapped for a link meanwhile is never written through."""
    os.mkdir(name, dir_fd=folder_fd)
    fd = open_folder(name, folder_fd)
    try:
        for entry in sorted(os.scandir(source), key=lambda entry: entry.name):
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), entry.name, dir_fd=fd)
            elif entry.is_dir(follow_symlinks=False):
                if depth < MAX_DEPTH:
                    _copy_into(Path(entry.path), fd, entry.name, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                with open(entry.path, 'rb') as stream:
                    target = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode) | 0o600, dir_fd=fd)
                    with os.fdopen(target, 'wb') as copy:
                        shutil.copyfileobj(stream, copy, 1 << 20)
    finally:
        os.close(fd)


def place_inputs(task: Task, workspace: Path) -> list[str]:
    """Copy tasks/<id>/input/ to <workspace>/in/<id>/ (links copied as links, the owner's), through fds only; a
    leftover in/<id> is removed first. Returns the problems (an `in` that was a link or not a folder, replaced)."""
    problems = clear(workspace, 'in', task.id)
    if task.inputs is None:
        return problems
    workspace_fd, in_fd = top_folder(workspace, 'in', problems, create=True)
    try:
        _copy_into(task.inputs, in_fd, task.id)
    finally:
        close(in_fd, workspace_fd)
    return problems


# Collected results: regular files and folders only, within a budget.

KIND_NAMES = {stat.S_IFLNK: '符号链接', stat.S_IFIFO: '命名管道', stat.S_IFSOCK: '套接字', stat.S_IFCHR: '字符设备',
              stat.S_IFBLK: '块设备'}
MAX_RESULT_BYTES = 1 << 30      # bytes collected from one out/<id>/, by st_size, checked before each file
MAX_RESULT_FILES = 5000         # files collected from one out/<id>/


def _kind_name(mode: int) -> str:
    return KIND_NAMES.get(stat.S_IFMT(mode), '特殊文件')


class _Budget:
    def __init__(self) -> None:
        self.bytes, self.files = 0, 0


def _copy_file(name: str, dir_fd: int, target: Path, size: int) -> str | None:
    """Copy at most `size` bytes of the regular file `name` of an open folder, by content, to the new file target
    (so the record never shares an inode with a file the Lab can still write). None, or the kind of thing it turned
    out to be when it is not a regular file."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            return _kind_name(info.st_mode)
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, 'wb') as copy:
                left = min(size, info.st_size)
                while left > 0 and (chunk := source.read(min(left, 1 << 20))):
                    copy.write(chunk)
                    left -= len(chunk)
                os.fchmod(copy.fileno(), stat.S_IMODE(info.st_mode) & 0o755 | 0o600)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return None


def _collect_file(name: str, folder_fd: int, info: os.stat_result, target: Path, where: str, lines: Lines,
                  budget: _Budget) -> None:
    if budget.files >= MAX_RESULT_FILES:
        lines.add(f'{where} 超出收取上限（最多 {MAX_RESULT_FILES} 个文件），没有收进记录，已从工作区删掉')
    elif budget.bytes + info.st_size > MAX_RESULT_BYTES:
        lines.add(f'{where} 有 {info.st_size} 字节，超出收取上限（共 {MAX_RESULT_BYTES} 字节），没有收进记录，'
                  '已从工作区删掉')
    elif not grant(name, folder_fd, info, stat.S_IRUSR):
        lines.add(f'{where} 读不了，又有别的硬链接（不改它的权限），没有收进记录，已从工作区删掉')
    else:
        other = _copy_file(name, folder_fd, target, info.st_size)
        if other is not None:
            lines.add(f'{where} 是{other}，没有收进记录，已从工作区删掉')
        else:
            budget.files += 1
            budget.bytes += info.st_size


def _copy_folder(folder_fd: int, dest: Path, label: str, lines: Lines, budget: _Budget, depth: int) -> None:
    """Copy the content of the open folder into the existing folder dest: regular files by content, folders level
    by level up to MAX_DEPTH, within the budget. Every other entry (a link, a FIFO, a socket, a device) and whatever
    is over the budget is left out and reported."""
    for name in sorted(os.listdir(folder_fd)):
        where = show(f'{label}/{name}')
        try:
            info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth >= MAX_DEPTH:
                    lines.add(f'{where} 在第 {MAX_DEPTH} 层以下，没有收进记录，已从工作区删掉')
                    continue
                grant(name, folder_fd, info, stat.S_IRWXU)
                child = open_folder(name, folder_fd)
                try:
                    (dest / name).mkdir()
                    _copy_folder(child, dest / name, f'{label}/{name}', lines, budget, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                _collect_file(name, folder_fd, info, dest / name, where, lines, budget)
            else:
                lines.add(f'{where} 是{_kind_name(info.st_mode)}，没有收进记录，已从工作区删掉')
        except OSError as error:
            lines.add(f'{where} 读不了（{error.strerror or error}），没有收进记录')


def collect_out(workspace: Path, task_id: str, dest: Path) -> list[str]:
    """Take <workspace>/out/<task_id>/ out of the workspace into dest, and return the problems (Chinese lines).

    The record holds only regular files (copied by content) and folders: the owner's check script runs outside
    every sandbox and would follow a link. Every link, FIFO, socket or device under out/<task_id>/ is left out, and
    so is whatever goes past MAX_DEPTH levels, MAX_RESULT_FILES files or MAX_RESULT_BYTES bytes (by st_size): one
    problem line each, capped. An `out` or `out/<task_id>` that is itself a link, or not a folder, is not collected.
    Nothing is ever followed through a link; afterwards out/<task_id> is removed from the workspace (and `out` too
    if it is left empty). dest must not exist; it is always created, empty when there was nothing to collect, so a
    check script always gets a folder.
    """
    safe_id(task_id, '任务编号')
    dest = Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(f'{dest} already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    problems = []
    workspace_fd, out_fd = top_folder(workspace, 'out', problems, create=False)
    try:
        dest.mkdir()
        found = None if out_fd is None else kind(task_id, out_fd)
        if found == 'folder':
            grant(task_id, out_fd, os.stat(task_id, dir_fd=out_fd, follow_symlinks=False), stat.S_IRWXU)
            lines = Lines()
            results_fd = open_folder(task_id, out_fd)
            try:
                _copy_folder(results_fd, dest, f'out/{task_id}', lines, _Budget(), 1)
            finally:
                os.close(results_fd)
            problems += lines.done()
            try:
                remove(task_id, out_fd)
            except OSError as error:
                problems.append(f'out/{task_id} 已收进记录，但没能从工作区删掉：{error}')
        elif found is not None:
            what = '是符号链接' if found == 'link' else '不是文件夹'
            problems.append(f'out/{task_id} {what}，没有收进记录，已从工作区删掉')
            os.unlink(task_id, dir_fd=out_fd)
        if out_fd is not None and not os.listdir(out_fd):
            os.rmdir('out', dir_fd=workspace_fd)
    finally:
        close(out_fd, workspace_fd)
    return problems
