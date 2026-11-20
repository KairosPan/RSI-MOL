"""One Codex call: `codex exec` with the whole configuration as `-c` flags, recorded in its own folder.

The record folder gets prompt.txt, events.jsonl (Codex's JSON event stream), stderr.txt and last-message.txt;
none of them may exist beforehand.
"""
import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rsi.mockmodel import provider_config
from rsi.paths import safe_id
from rsi.tomlout import render_flags

KILL_GRACE = 5  # seconds between SIGTERM and SIGKILL of a call's process group
RECORD_FILES = ('prompt.txt', 'events.jsonl', 'stderr.txt', 'last-message.txt')
LOCAL_HOSTS = '127.0.0.1,localhost'


@dataclass
class CallResult:
    status: str                 # finished, failed or timed_out
    exit_code: int | None       # None when the program could not be started
    thread_id: str | None
    usage: dict | None          # the `usage` of the last turn.completed event; None when there was none
    last_message: str
    command: list[str]


def codex_program() -> str:
    """The Codex program to start: $RSI_CODEX, default `codex`."""
    return os.environ.get('RSI_CODEX', 'codex')


def codex_version() -> str:
    """The version Codex reports, e.g. '0.170.0'.

    Codex writes helper files into its home even for --version, so this runs with a throwaway home.
    """
    with tempfile.TemporaryDirectory() as home:
        done = subprocess.run([codex_program(), '--version'], capture_output=True, text=True, timeout=60,
                              check=True, env=dict(os.environ, CODEX_HOME=home))
    return done.stdout.split()[-1]


def _merge(base: dict, extra: dict) -> dict:
    """base with extra laid over it; tables present in both are merged key by key."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            value = _merge(merged[key], value)
        merged[key] = value
    return merged


def _stop(process: subprocess.Popen) -> None:
    """SIGTERM the call's process group, SIGKILL it after KILL_GRACE seconds, and reap the leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    _kill_group(process)
    process.wait()


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_events(path: Path) -> tuple[str | None, dict | None, bool]:
    """(thread id, usage of the last turn.completed, whether a turn.completed was seen)."""
    thread_id, usage, completed = None, None, False
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
            thread_id = event['thread_id']
        elif event.get('type') == 'turn.completed':
            completed, usage = True, event.get('usage')
    return thread_id, usage, completed


def run_codex(*, home: Path, cwd: Path, prompt: str, config: dict, record_dir: Path, timeout: float,
              resume: str | None = None, schema: Path | None = None, on_start=None) -> CallResult:
    """Run one `codex exec` (or `codex exec resume <thread>`) with the prompt on stdin and CODEX_HOME=home.

    When $RSI_TEST_MODEL_ENDPOINT is set the call goes to that scripted endpoint instead of a model.
    on_start(pid) is called once the process exists. Status is `finished` (exit code 0 and a turn.completed
    event), `timed_out` (the process group got SIGTERM, then SIGKILL) or `failed`.
    """
    env = dict(os.environ, CODEX_HOME=str(home))
    endpoint = os.environ.get('RSI_TEST_MODEL_ENDPOINT')
    if endpoint:
        config = _merge(config, provider_config(endpoint))
        env['NO_PROXY'] = LOCAL_HOSTS
    if resume is not None:
        safe_id(resume, 'thread id')
    last_message = record_dir / 'last-message.txt'
    command = [codex_program(), '--no-daemon', 'exec', *(['resume'] if resume is not None else []),
               '--skip-git-repo-check', '--json', '--ignore-user-config', *render_flags(config),
               *(['--output-schema', str(schema)] if schema is not None else []),
               '-o', str(last_message), *([resume] if resume is not None else []), '-']

    record_dir.mkdir(parents=True, exist_ok=True)
    taken = [name for name in RECORD_FILES if os.path.lexists(record_dir / name)]
    if taken:
        raise FileExistsError(f'record files already exist in {record_dir}: {", ".join(taken)}')
    with open(record_dir / 'prompt.txt', 'x', encoding='utf-8') as stream:
        stream.write(prompt)
    home.mkdir(parents=True, exist_ok=True)

    timed_out = False
    with open(record_dir / 'prompt.txt', 'rb') as stdin, open(record_dir / 'events.jsonl', 'xb') as stdout, \
            open(record_dir / 'stderr.txt', 'xb') as stderr:
        try:
            process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=env,
                                       start_new_session=True)
        except OSError as error:
            stderr.write(f'could not start {command[0]}: {error}\n'.encode())
            process = None
        if process is not None:
            deadline = time.monotonic() + timeout
            try:
                if on_start is not None:
                    on_start(process.pid)
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop(process)
            except BaseException:
                _stop(process)
                raise
            # After every call, finished or not, SIGKILL the whole process group on purpose: nothing the call
            # started (a tool's background process, a stray child) may outlive it.
            _kill_group(process)
    if not os.path.lexists(last_message):
        last_message.touch()

    thread_id, usage, completed = _read_events(record_dir / 'events.jsonl')
    exit_code = None if process is None else process.returncode
    if timed_out:
        status = 'timed_out'
    elif exit_code == 0 and completed:
        status = 'finished'
    else:
        status = 'failed'
    text = last_message.read_text(encoding='utf-8', errors='replace') if last_message.is_file() else ''
    return CallResult(status, exit_code, thread_id, usage, text, command)
