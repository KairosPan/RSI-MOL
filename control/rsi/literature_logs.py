"""Collect the literature server's own call log of a run from its sealed workspace into its content-hashed record.

The log is what the server reports about its calls. The server writes it by path into the workspace, where the Lab's
commands can write too, so a row is not proof that a call happened; the originals in library/ are what a row can be
checked against. The control program only copies complete, well-formed rows, within limits."""
import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

from rsi.workspace import Lines, open_folder

LOG = 'literature.jsonl'
PENDING = '.' + LOG + '.pending'    # in the call's record folder while the attachment is being written
ROW_BYTES = 256 << 10
TOTAL_BYTES = 64 << 20
MAX_FILES = 512
PROCESS_FILE = re.compile(r'[0-9a-f]{32}\.jsonl')
INVOCATION = re.compile(r'[0-9a-f]{32}')


def identity(workspace: Path) -> list[int]:
    """Identity of the directory entry placed by the controller, saved before Codex starts."""
    info = workspace.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('The literature workspace is not a directory')
    return [info.st_dev, info.st_ino]


def _report(line: bytes, ids: set) -> bytes:
    row = json.loads(line.decode('utf-8'))
    if not isinstance(row, dict):
        raise ValueError('expected an object')
    call = row.get('invocation_id')
    if not isinstance(call, str) or not INVOCATION.fullmatch(call):
        raise ValueError('invalid invocation_id')
    if call in ids:
        raise ValueError('duplicate invocation_id')
    if not isinstance(row.get('tool'), str) or not row['tool'] or not isinstance(row.get('status'), str):
        raise ValueError('missing tool or status')
    data = json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii') + b'\n'
    ids.add(call)
    return data


def _read_file(folder: int, name: str, output, budget: int, ids: set, problems: Lines) -> int:
    """Read at most the initial size and the remaining byte budget, even if an old writer kept an fd."""
    before = os.stat(name, dir_fd=folder, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('not a single-link regular file')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not os.path.samestat(info, before) or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError('the file changed while opening')
        remaining = size = min(info.st_size, budget)
        if remaining < info.st_size:
            problems.add(f'Literature log collection reached its {TOTAL_BYTES}-byte limit')
        number = 0
        while remaining:
            line = stream.readline(min(remaining, ROW_BYTES + 1))
            if not line:
                break
            number += 1
            remaining -= len(line)
            if len(line) > ROW_BYTES:
                problems.add(f'Literature log {name} line {number} exceeds {ROW_BYTES} bytes')
                while remaining and line and not line.endswith(b'\n'):
                    line = stream.readline(min(remaining, ROW_BYTES))
                    remaining -= len(line)
                continue
            if not line.endswith(b'\n'):
                problems.add(f'Literature log {name} line {number} is incomplete')
                break
            try:
                data = _report(line, ids)
            except (ValueError, RecursionError) as error:
                problems.add(f'Literature log {name} line {number} is invalid: {str(error)[:200]}')
            else:
                output.write(data)
        return size


def _read_calls(folder: int, output, problems: Lines) -> None:
    names = []
    with os.scandir(folder) as entries:
        for index, item in enumerate(entries):
            if index == MAX_FILES:
                problems.add(f'Literature log collection reached its {MAX_FILES}-file limit')
                break
            if PROCESS_FILE.fullmatch(item.name):
                names.append(item.name)
            else:
                problems.add(f'Unexpected file in literature calls: {ascii(item.name)}')
    budget, ids = TOTAL_BYTES, set()
    for name in sorted(names):
        if not budget:
            problems.add('Literature log collection reached its byte limit; remaining files were skipped')
            break
        try:
            budget -= _read_file(folder, name, output, budget, ids, problems)
        except (OSError, ValueError) as error:
            problems.add(f'Literature log {name} could not be collected: {error}')


def discard_pending(call_dir: Path) -> None:
    """Remove what an interrupted collection left in the call's record folder, so it is never hashed into it."""
    (call_dir / PENDING).unlink(missing_ok=True)


def _sync_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def collect(workspace: Path, expected: list[int], call_dir: Path) -> list[str]:
    """Publish complete rows only. A newly staged workspace with no service calls gets an empty attachment.

    Written like every record file: complete and synced under a temporary name, then linked to its name (refused if
    the name is taken), so the attachment is whole or absent. A temporary file an interrupted collection left is
    removed first."""
    final, pending = call_dir / LOG, call_dir / PENDING
    discard_pending(call_dir)
    if os.path.lexists(final):
        return []  # the controller already published this attachment before an interruption
    problems = Lines()
    try:
        with ExitStack() as stack:
            fd = open_folder(workspace)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino] != expected:
                raise ValueError('the sealed workspace does not belong to this call')
            out = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(out, 'wb') as stream:
                for name in ('.tmp', 'literature', 'calls'):
                    try:
                        fd = open_folder(name, fd)
                    except FileNotFoundError:
                        break
                    stack.callback(os.close, fd)
                else:
                    _read_calls(fd, stream, problems)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(pending, final, follow_symlinks=False)
    finally:
        discard_pending(call_dir)
    _sync_folder(call_dir)
    return problems.done()
