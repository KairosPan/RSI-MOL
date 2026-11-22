"""The owner's tasks (`tasks/<id>/`), their check scripts, their inputs and their collected results.

A task is `tasks/<id>/task.md` with four `## ` sections (design §5.4), optionally `input/` (copied to the staged
workspace's `in/<id>/` before a run) and `check.py` (run on the collected results afterwards). Only the owner writes
`tasks/`.

Results are collected from a sealed workspace (rsi.workspace), never from one an agent can still write, and without
ever following a link: they keep only regular files (copied by content) and folders, within a budget.
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
from rsi.workspace import MAX_DEPTH, Lines, close, grant, kind, open_folder, open_unlocked

SECTIONS = ('Goal', 'Input', 'Requirements', 'Completion criteria')
HEADINGS = ', '.join(f'## {name}' for name in SECTIONS)     # how messages name the four headings
TASK_FILE, CHECK_FILE, INPUT_FOLDER = 'task.md', 'check.py', 'input'
HEADING = re.compile(r'##[ \t]+(.*?)[ \t]*')
FENCE = re.compile(r'(```|~~~)')
CHECK_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
CHECK_MESSAGE_LIMIT = 500                           # characters of the check's first output line that are kept
CHECK_TIMED_OUT = 'the check script timed out'
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
            raise ValueError(f'{path} has an unknown section "{current}". A task has only these four sections, '
                             f'in this spelling: {HEADINGS}.')
        if current in sections:
            raise ValueError(f'{path} has the section "{current}" more than once.')
        sections[current] = []
    return {name: '\n'.join(lines).strip() for name, lines in sections.items()}


def load_task(tasks: Path, task_id: str) -> Task:
    """tasks/<task_id>/ as a Task; ValueError (in English) for a bad id, a missing task, an unknown or repeated
    heading, and naming the first of the four sections that is missing or empty."""
    safe_id(task_id, 'task id')
    folder = Path(tasks) / task_id
    path = folder / TASK_FILE
    try:
        # Directory lookup may ignore case. The stored task identity must use the owner's spelling.
        names = os.listdir(tasks)
        if task_id not in names:
            match = next((name for name in sorted(names) if name.casefold() == task_id.casefold()), None)
            if match is not None:
                raise ValueError(f'Task IDs are case-sensitive. Use {match} for this task.')
            raise FileNotFoundError
        text = path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        raise ValueError(f'There is no task {task_id}: {path} does not exist.') from None
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f'{path} cannot be read: {error}') from None
    sections = _sections(text, path)
    for name in SECTIONS:
        if not sections.get(name):
            raise ValueError(f'{path} has no "{name}" section, or it is empty. A task has four sections, each '
                             f'starting with its heading line: {HEADINGS}.')
    check, inputs = folder / CHECK_FILE, folder / INPUT_FOLDER
    return Task(task_id, {name: sections[name] for name in SECTIONS}, folder,
                check if check.is_file() else None, inputs if inputs.is_dir() else None)


def same_task_directory(folder: Path, other: Path) -> bool:
    """Confirm a historical case variant actually resolves to the same owner task directory."""
    if folder.name.casefold() != other.name.casefold():
        return False
    try:
        return folder.is_dir() and folder.samefile(other)
    except FileNotFoundError:
        return False


def results_sentence(name: str) -> str:
    """The prompt's sentence saying where the results go."""
    return f'Write the results to out/{name}/.'


def prompt_for(task: Task) -> str:
    """The four sections, then where the inputs are (only when the task has inputs) and where the results go."""
    parts = [f'## {name}\n\n{task.sections[name]}' for name in SECTIONS]
    if task.inputs is not None:
        parts.append(f'Input files are in in/{task.id}/.')
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
    check counts as failed with the message CHECK_TIMED_OUT. Returns {'passed', 'message', 'exit_code'}.
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

def place_inputs(task: Task, workspace: Path) -> None:
    """Copy tasks/<id>/input/ (when the task has one) to <workspace>/in/<id>/. The workspace is a staging folder,
    which only the control program writes; links are copied as links (they are the owner's)."""
    if task.inputs is not None:
        shutil.copytree(task.inputs, Path(workspace) / 'in' / task.id, symlinks=True)


# Collected results: regular files and folders only, within a budget.

KIND_NAMES = {stat.S_IFLNK: 'a symbolic link', stat.S_IFIFO: 'a named pipe', stat.S_IFSOCK: 'a socket',
              stat.S_IFCHR: 'a character device', stat.S_IFBLK: 'a block device'}
MAX_RESULT_BYTES = 1 << 30      # bytes collected from one out/<id>/, by st_size, checked before each file
MAX_RESULT_ENTRIES = 5000       # files and folders collected from one out/<id>/
NOT_COLLECTED = '; it was not collected into the record'      # the end of a line about what is left out


def _kind_name(mode: int) -> str:
    return KIND_NAMES.get(stat.S_IFMT(mode), 'a special file')


class _Budget:
    def __init__(self) -> None:
        self.bytes, self.entries = 0, 0

    def full(self, where: str, lines: Lines) -> bool:
        """Whether no further file or folder may be collected (then a line says so); else it is counted."""
        if self.entries >= MAX_RESULT_ENTRIES:
            lines.add(f'{where} is over the collection limit (at most {MAX_RESULT_ENTRIES} files and folders)'
                      f'{NOT_COLLECTED}')
            return True
        self.entries += 1
        return False


def _copy_file(name: str, dir_fd: int, target: Path, size: int) -> str | None:
    """Copy at most `size` bytes of the regular file `name` of an open folder, by content, to the new file target
    (so the record never shares an inode with a file the Lab wrote). None, or the kind of thing it turned out to be
    when it is not a regular file."""
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
    if budget.bytes + info.st_size > MAX_RESULT_BYTES:
        lines.add(f'{where} has {info.st_size} bytes, over the collection limit ({MAX_RESULT_BYTES} bytes in all)'
                  f'{NOT_COLLECTED}')
    elif budget.full(where, lines):
        return
    elif not grant(name, folder_fd, info, stat.S_IRUSR):
        lines.add(f'{where} cannot be read and has other hard links (its permissions are not changed)'
                  f'{NOT_COLLECTED}')
    else:
        other = _copy_file(name, folder_fd, target, info.st_size)
        if other is not None:
            lines.add(f'{where} is {other}{NOT_COLLECTED}')
        else:
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
                    lines.add(f'{where} is below level {MAX_DEPTH}{NOT_COLLECTED}')
                    continue
                if budget.full(where, lines):
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
                lines.add(f'{where} is {_kind_name(info.st_mode)}{NOT_COLLECTED}')
        except OSError as error:
            lines.add(f'{where} cannot be read ({error.strerror or error}){NOT_COLLECTED}')


def _results_folder(parent_fd: int, name: str, label: str, problems: list[str]) -> int | None:
    """An fd of the folder `name` of an open folder, given owner rwx; None (with a problem line if something else
    is there) when it is missing, a link or not a folder."""
    found = kind(name, parent_fd)
    if found is None:
        return None
    if found != 'folder':
        problems.append(f'{label} {"is a symbolic link" if found == "link" else "is not a folder"}{NOT_COLLECTED}')
        return None
    grant(name, parent_fd, os.stat(name, dir_fd=parent_fd, follow_symlinks=False), stat.S_IRWXU)
    return open_folder(name, parent_fd)


def collect_out(workspace: Path, task_id: str, dest: Path) -> list[str]:
    """Copy out/<task_id>/ of a sealed workspace into dest, and return the problems (English lines).

    The record holds only regular files (copied by content) and folders: the owner's check script runs outside
    every sandbox and would follow a link. Every link, FIFO, socket or device under out/<task_id>/ is left out, and
    so is whatever goes past MAX_DEPTH levels, MAX_RESULT_ENTRIES files and folders or MAX_RESULT_BYTES bytes (by
    st_size): one problem line each, capped. An `out` or `out/<task_id>` that is itself a link, or not a folder, is
    not collected. Nothing is followed through a link, and nothing in the workspace is removed (a sealed workspace
    is removed or kept as a whole). dest must not exist; it is always created, empty when there was nothing to
    collect, so a check script always gets a folder.
    """
    safe_id(task_id, 'task id')
    dest = Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(f'{dest} already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    problems = []
    workspace_fd = open_unlocked(str(workspace), None, str(workspace))
    out_fd = results_fd = None
    try:
        out_fd = _results_folder(workspace_fd, 'out', 'out', problems)
        if out_fd is not None:
            results_fd = _results_folder(out_fd, task_id, f'out/{task_id}', problems)
        if results_fd is not None:
            lines = Lines()
            _copy_folder(results_fd, dest, f'out/{task_id}', lines, _Budget(), 1)
            problems += lines.done()
    finally:
        close(results_fd, out_fd, workspace_fd)
    return problems
