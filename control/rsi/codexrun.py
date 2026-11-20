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
from rsi.paths import Refused, find_layout, safe_id
from rsi.programs import clean_path, native_codex
from rsi.tomlout import render_flags

KILL_GRACE = 5  # seconds between SIGTERM and SIGKILL of a call's process group
RECORD_FILES = ('prompt.txt', 'events.jsonl', 'stderr.txt', 'last-message.txt')
LOCAL_HOSTS = '127.0.0.1,localhost'
# The only variables of the control program's own environment that Codex gets, besides LC_* and the owner's tool
# secrets RSI_TOOL_* (bearer tokens of URL tool servers, see settings.BEARER). The proxy variables are kept so that
# model calls work behind the owner's proxy.
PROXY_VARIABLES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
                   'no_proxy')
KEPT_VARIABLES = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TERM', 'TMPDIR', *PROXY_VARIABLES)


@dataclass
class CallResult:
    status: str                 # finished, failed or timed_out
    exit_code: int | None       # None when the program could not be started
    thread_id: str | None
    usage: dict | None          # the `usage` of the last turn.completed event; None when there was none
    last_message: str
    command: list[str]


def _root(root: Path | None) -> Path:
    """The RSI root whose folders no program may come from: the given one, else find_layout()'s."""
    return find_layout().root if root is None else Path(root)


def codex_program(root: Path | None = None) -> str:
    """The Codex program to start: the validated native program (rsi.programs.native_codex: $RSI_CODEX, else
    `codex` from the PATH entries outside the RSI root and the shared temp folders). Refused when there is none."""
    return native_codex(_root(root))


def codex_environment(home: Path, *, secrets: bool = True, root: Path | None = None) -> dict:
    """The environment a Codex process starts with: the few basics of KEPT_VARIABLES and LC_*, the owner's
    RSI_TOOL_* secrets (unless secrets is false) and CODEX_HOME. Nothing else of os.environ reaches Codex, and its
    PATH keeps no entry that is relative or lies inside the RSI root or a shared temp folder (Codex itself starts
    programs, git among them, by PATH, outside the sandbox)."""
    env = {key: value for key, value in os.environ.items()
           if key in KEPT_VARIABLES or key.startswith('LC_') or (secrets and key.startswith('RSI_TOOL_'))}
    if 'PATH' in env:
        env['PATH'] = clean_path(_root(root), env['PATH'])
    env['CODEX_HOME'] = str(home)
    return env


def _with_local_hosts(value: str | None) -> str:
    """A NO_PROXY value that also names LOCAL_HOSTS, keeping what it named before."""
    hosts = [host.strip() for host in (value or '').split(',') if host.strip()]
    hosts += [host for host in LOCAL_HOSTS.split(',') if host not in hosts]
    return ','.join(hosts)


def codex_version(root: Path | None = None) -> str:
    """The version Codex reports, e.g. '0.170.0'; RuntimeError (in Chinese) when it reports none or there is no
    usable Codex program (codex_program).

    Codex writes helper files into its home even for --version, so this runs with a throwaway home, in its own
    process group, which is killed afterwards.
    """
    try:
        program = codex_program(root)
    except Refused as error:
        raise RuntimeError(str(error)) from None
    with tempfile.TemporaryDirectory() as home:
        process = subprocess.Popen([program, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   env=codex_environment(Path(home), secrets=False, root=root))
        try:
            out, err = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.wait()
            raise RuntimeError(f'`{program} --version` 60 秒内没有结束') from None
        finally:
            _kill_group(process)
    words = out.split()
    if process.returncode != 0 or not words:
        detail = ' '.join(err.split())[:300]
        raise RuntimeError(f'`{program} --version` 没有报出版本号（退出码 {process.returncode}）：{detail}')
    return words[-1]


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
    except (ProcessLookupError, PermissionError):  # macOS: PermissionError for a group of one exited leader
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
              resume: str | None = None, schema: Path | None = None, on_start=None,
              root: Path | None = None) -> CallResult:
    """Run one `codex exec` (or `codex exec resume <thread>`) with the prompt on stdin and CODEX_HOME=home.

    When $RSI_TEST_MODEL_ENDPOINT is set the call goes to that scripted endpoint instead of a model.
    on_start(pid) is called once the process exists. Status is `finished` (exit code 0 and a turn.completed
    event), `timed_out` (the process group got SIGTERM, then SIGKILL) or `failed` (also when there is no usable
    Codex program for the RSI root `root`, default find_layout()'s: nothing is started then). The paths are made
    absolute first. Codex gets codex_environment(home), not the control program's whole environment.
    """
    home, cwd, record_dir = Path(home).resolve(), Path(cwd).resolve(), Path(record_dir).resolve()
    schema = Path(schema).resolve() if schema is not None else None
    env = codex_environment(home, root=root)
    try:
        program, unusable = codex_program(root), None
    except Refused as error:
        program, unusable = os.environ.get('RSI_CODEX', 'codex'), str(error)
    endpoint = os.environ.get('RSI_TEST_MODEL_ENDPOINT')
    if endpoint:
        config = _merge(config, provider_config(endpoint))
        for key in ('NO_PROXY', 'no_proxy'):              # the endpoint is local: never through a proxy
            env[key] = _with_local_hosts(env.get(key))
    if resume is not None:
        safe_id(resume, 'thread id')
    last_message = record_dir / 'last-message.txt'
    # --ignore-user-config skips only $CODEX_HOME/config.toml; --ignore-rules also skips the saved command rules
    # ($CODEX_HOME/rules/*.rules), where an `allow` rule the owner approved by hand could run a command unsandboxed.
    command = [program, '--no-daemon', 'exec', *(['resume'] if resume is not None else []),
               '--skip-git-repo-check', '--json', '--ignore-user-config', '--ignore-rules', *render_flags(config),
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
        process = None
        if unusable is not None:
            stderr.write(f'could not start Codex: {unusable}\n'.encode())
        else:
            try:
                process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=env,
                                           start_new_session=True)
            except OSError as error:
                stderr.write(f'could not start {command[0]}: {error}\n'.encode())
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
            # After every call, finished or not, SIGKILL the whole process group on purpose, so that what the call
            # left in it (a tool's background process, a stray child) does not outlive it. A process that started
            # its own session or group is out of this reach.
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
