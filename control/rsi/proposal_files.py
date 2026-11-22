"""Stage the managed task's proposal directory and collect its sealed contents before call finalization."""
import os
import re
import stat
import tempfile
from contextlib import ExitStack
from pathlib import Path

from rsi.workspace import Lines, open_folder

DIRECTORY = 'memory-proposals'
ENABLE_ENV = 'RSI_MEMORY_PROPOSALS'
ATTACHMENTS = 'proposals'
FILE_NAME = re.compile(r'[0-9a-f]{32}\.json')
MAX_FILES = 64
MAX_BYTES = 64 << 10


def _identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _open(stack, entry, parent=None):
    fd = open_folder(entry, parent)
    stack.callback(os.close, fd)
    return fd


def prepare(workspace: Path) -> list[int]:
    """Called in private staging. Both identities survive renaming the workspace into place and sealing it."""
    with ExitStack() as stack:
        root = _open(stack, workspace)
        temp = _open(stack, '.tmp', root)
        os.mkdir(DIRECTORY, 0o700, dir_fd=temp)
        return _identity(root) + _identity(_open(stack, DIRECTORY, temp))


def _read(folder, name):
    before = os.stat(name, dir_fd=folder, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_BYTES:
        raise ValueError('expected a bounded single-link regular file')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not os.path.samestat(before, info) or info.st_nlink != 1 or info.st_size != before.st_size:
            raise ValueError('file changed while opening')
        raw = stream.read(info.st_size + 1)
        final = os.fstat(fd)
        if len(raw) != info.st_size or final.st_nlink != 1 or final.st_mtime_ns != info.st_mtime_ns:
            raise ValueError('file changed while copying')
        return raw


def collect(workspace: Path, expected: list[int], call_dir: Path) -> list[str]:
    """Copy whole bounded files. Only registration can bind their contents to successful native tool calls."""
    problems = Lines()
    target = call_dir / ATTACHMENTS
    if target.is_dir():
        return []   # Previously published by this controller, including on a retry after an interruption.
    with ExitStack() as stack:
        root = _open(stack, workspace)
        temp = _open(stack, '.tmp', root)
        folder = _open(stack, DIRECTORY, temp)
        if _identity(root) + _identity(folder) != expected:
            raise ValueError('sealed proposal directory does not belong to this call')
        names = []
        with os.scandir(folder) as entries:
            for number, entry in enumerate(entries):
                if number == MAX_FILES:
                    problems.add(f'Proposal collection exceeded its {MAX_FILES}-file limit')
                    # Direct filesystem writes can bypass the tool's limit. Do not select files by directory order.
                    return problems.done()
                if FILE_NAME.fullmatch(entry.name):
                    names.append(entry.name)
                else:
                    problems.add(f'Unexpected entry in the proposal directory: {ascii(entry.name)}')
        if not names:
            return problems.done()
        # Stage outside the run directory so an interrupted copy can never enter the run's attachment hashes.
        with tempfile.TemporaryDirectory(prefix='.proposals-', dir=call_dir.parent) as private:
            staged = Path(private) / ATTACHMENTS
            staged.mkdir(mode=0o700)
            for name in sorted(names):
                try:
                    raw = _read(folder, name)
                except (OSError, ValueError) as error:
                    problems.add(f'Proposal {name} could not be collected: {error}')
                    continue
                with (staged / name).open('xb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
            source = _open(stack, staged)
            os.fsync(source)
            os.rename(staged, target)
            os.fsync(_open(stack, call_dir))
    return problems.done()
