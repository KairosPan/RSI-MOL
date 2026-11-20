"""The owner's tasks (`tasks/<id>/`), their check scripts, and the two workspace folders the control program owns.

A task is `tasks/<id>/task.md` with four `## ` sections (design §5.4), optionally `input/` (copied to the workspace's
`in/<id>/` before a run) and `check.py` (run on the collected results afterwards). Only the owner writes `tasks/`.

The Lab writes the workspace and the control program runs outside every sandbox, so `in/` and `out/` of a workspace
are handled without ever following a link: entries are looked at with lstat, opened with O_NOFOLLOW relative to an
already opened folder, moved and removed relative to that folder, and copied with links kept as links.
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
            problems.append(f'工作区里的 {name} {_describe(kind)}，已删掉（它由控制程序管理，不跟随链接）')
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


def _make_readable(folder: Path) -> None:
    """Give the owner read access to every file and folder under folder (and search access to the folders), links
    untouched, so the record can always be read and hashed whatever modes the Lab left."""
    stack = [folder]
    while stack:
        path = stack.pop()
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(path, stat.S_IMODE(mode) | stat.S_IRWXU, follow_symlinks=False)
                stack.extend(Path(entry.path) for entry in os.scandir(path))
            elif stat.S_ISREG(mode) and not mode & stat.S_IRUSR:
                os.chmod(path, stat.S_IMODE(mode) | stat.S_IRUSR, follow_symlinks=False)
        except OSError:
            continue


def collect_out(workspace: Path, task_id: str, dest: Path) -> list[str]:
    """Move <workspace>/out/<task_id>/ to dest without following links, and return the problems (Chinese lines).

    Links inside the results stay links, and everything else is made readable by the owner. An `out` or
    `out/<task_id>` that is itself a link, or not a folder, is not collected: it is removed from the workspace and
    reported. dest must not exist; it is always created, empty when there was nothing to collect, so a check script
    always gets a folder. An `out` left empty is removed.
    """
    safe_id(task_id, '任务编号')
    dest = Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(f'{dest} already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    problems = []
    workspace_fd, out_fd = _top_folder(workspace, 'out', problems, create=False)
    try:
        kind = None if out_fd is None else _kind(task_id, out_fd)
        if kind == 'folder':
            parent_fd = _open_folder(dest.parent)
            try:
                os.rename(task_id, dest.name, src_dir_fd=out_fd, dst_dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            if dest.is_symlink() or not dest.is_dir():            # swapped for something else just before the move
                problems.append(f'out/{task_id} 在收取时变成了别的东西，没有收进记录')
                os.unlink(dest)
            else:
                _make_readable(dest)
        elif kind is not None:
            problems.append(f'out/{task_id} {_describe(kind)}，没有收进记录，已从工作区删掉')
            os.unlink(task_id, dir_fd=out_fd)
        if out_fd is not None and not os.listdir(out_fd):
            os.rmdir('out', dir_fd=workspace_fd)
    finally:
        _close(out_fd, workspace_fd)
    if not os.path.lexists(dest):
        dest.mkdir()
    return problems
