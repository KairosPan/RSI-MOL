"""Copies of a Lab call's Codex session files: the lead thread's and those of its sub-agents (design §5.7).

The Lab's Codex home also holds the owner's own conversations, so a sessions folder is never listed for its own
sake: a file is fetched only through the pattern <home>/sessions/*/*/*/*<thread id>.jsonl, and only when its name is
`<thread id>.jsonl` or ends in `-<thread id>.jsonl`. Thread ids must be plain ids (rsi.paths.safe_id), so they carry
no glob characters. Files are opened without following links and copied only when they are regular files.
"""
import glob
import json
import os
import shutil
import stat
from pathlib import Path

from rsi.paths import SAFE_ID, safe_id

ACTIVITY = 'SubAgentActivity'


def _session_files(home: Path, thread_id: str) -> list[Path]:
    pattern = os.path.join(glob.escape(str(home)), 'sessions', '*', '*', '*', f'*{thread_id}.jsonl')
    names = (f'{thread_id}.jsonl', f'-{thread_id}.jsonl')
    return sorted(Path(path) for path in glob.glob(pattern)
                  if Path(path).name == names[0] or Path(path).name.endswith(names[1]))


def _copy(source: Path, dest: Path) -> Path | None:
    """Copy one regular file into the folder dest without following a link; the copy, or None if not copied."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / source.name
        try:
            copy = open(target, 'xb')
        except FileExistsError:
            return None
        try:
            with copy:
                shutil.copyfileobj(stream, copy)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return target


def _sub_agents(session: Path) -> list[str]:
    """The thread ids of the sub-agents a session file names: `agent_thread_id` of every line whose
    payload.item.type is SubAgentActivity, each once, in order; lines that are not JSON objects are skipped."""
    found = []
    with open(session, 'rb') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            payload = event.get('payload') if isinstance(event, dict) else None
            item = payload.get('item') if isinstance(payload, dict) else None
            if isinstance(item, dict) and item.get('type') == ACTIVITY:
                child = item.get('agent_thread_id')
                if isinstance(child, str) and SAFE_ID.fullmatch(child) and child not in found:
                    found.append(child)
    return found


def collect_sessions(home: Path, thread_id: str, dest: Path, *, problems: list[str] | None = None) -> list[str]:
    """Copy the lead thread's session file and those of its sub-agents (and theirs, at any depth) into dest.

    Returns the sub-agent thread ids, in the order they were found. dest is created only when something is copied.
    ValueError if thread_id is not a plain id.
    """
    safe_id(thread_id, 'thread id')
    seen, queue, children = {thread_id}, [thread_id], []
    while queue:
        current = queue.pop(0)
        sources = _session_files(Path(home), current)
        if not sources and problems is not None:
            problems.append(f'Missing session archive for thread {current}')
        for source in sources:
            copy = _copy(source, Path(dest))
            if copy is None:
                if problems is not None:
                    problems.append(f'Could not copy the session record {source.name}')
                continue
            for child in _sub_agents(copy):
                if child not in seen:
                    seen.add(child)
                    children.append(child)
                    queue.append(child)
    return children
