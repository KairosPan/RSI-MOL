"""One Lab run (design §5.4): a task in work/, or a trial of a commit in trial/.

run_task: the task must have its four sections, the gate and the owner's daily limits must allow a call, and work/
must hold no `.codex`. Changes found in work/ are first committed as a manual version. The configuration is
composed from the owner's envelope and work/'s own settings, the task's inputs are copied to work/in/<id>/, and one
Codex call runs in work/. Then out/<id>/ is moved into the call's record, in/<id>/ is removed, the owner's check
script runs on the collected results, and work/ is committed as a new version by the Lab.

run_trial does the same in trial/ on a given commit, with a task or with a probe text instead, and never commits:
it is Meta's evidence, cost class `meta`.

A refusal before a call is noted in records/index.jsonl and raised as Refused; a bad request is a ValueError. Once a
call has started it is always finished: what goes wrong while collecting its results becomes a line in its
`problems`. The workspace is the Lab's and the control program runs outside every sandbox, so nothing in it is
followed through a link (see rsi.tasks).
"""
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rsi.codexrun import codex_program, codex_version, run_codex
from rsi.compose import lab_config, load_envelope, tool_server_problems, write_home_config, write_role_files
from rsi.limits import Limits, load_limits, refusal
from rsi.paths import Layout, Refused, safe_id
from rsi.records import Records
from rsi.sessions import collect_sessions
from rsi.settings import Settings, load_settings
from rsi.tasks import Task, clear, collect_out, load_task, place_inputs, prompt_for, results_sentence, run_check
from rsi.versions import VersionStore

MANUAL = 'owner (manual)'
PROBE = 'probe'                 # the results folder of a probe: out/probe/
PROBE_LIMIT = 20_000            # characters of a probe text


def gate(layout: Layout) -> str | None:
    """Why no agent may start now, or None. A placeholder that always allows; a later task fills it in."""
    return None


@dataclass
class _Run:
    workspace: Path
    trial: bool
    task: Task | None
    out_name: str
    prompt: str
    config: dict
    timeout: int
    fields: dict                                            # call.json from the start
    notes: list[str] = field(default_factory=list)         # problems found before the call
    version_before: str | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


@contextmanager
def _noting_refusals(records: Records, context: dict):
    """Note every Refused raised inside as a `refusal` index line (reason plus context), then let it go on."""
    try:
        yield
    except Refused as refused:
        records.note('refusal', {'reason': str(refused), **context})
        raise


def _admit(layout: Layout, records: Records) -> Limits:
    """The owner's limits, once the gate and the daily caps allow another call; Refused otherwise."""
    reason = gate(layout)
    if reason:
        raise Refused(reason)
    limits = load_limits(layout.owner)
    reason = refusal(limits, records.calls(), datetime.now().astimezone())
    if reason:
        raise Refused(reason)
    return limits


def _store(layout: Layout) -> VersionStore:
    store = VersionStore(layout.versions)
    if not (store.git_dir / 'HEAD').is_file():
        raise Refused(f'还没有版本库（{layout.versions}），请先运行 rsi init。')
    return store


def _no_codex_entry(workspace: Path) -> None:
    if os.path.lexists(workspace / '.codex'):
        raise Refused(f'{workspace} 里有 .codex，不能在这里启动 Lab Codex：请先移走它。')


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _own_interpreter(layout: Layout, task: Task | None) -> None:
    """A check script runs with this program's own Python, so that Python must not lie where an agent can write."""
    if task is None or task.check is None:
        return
    forms = {os.path.abspath(sys.executable), os.path.realpath(sys.executable)}
    roots = {os.path.abspath(layout.root), os.path.realpath(layout.root)}
    if not sys.executable or any(_within(form, root) for form in forms for root in roots):
        raise Refused(f'控制程序用的 Python（{sys.executable!r}）在 RSI 文件夹里，agent 改得到，不能用它运行检查脚本。'
                      '请用 RSI 文件夹以外的 python3 运行 rsi。')


def _codex_version() -> str:
    try:
        return codex_version()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise Refused(f'Codex 程序（{codex_program()}）用不了：{error}') from None


def _compose(layout: Layout, envelope: dict, settings: Settings, *, trial: bool, resolve) -> tuple[dict, list[str]]:
    """The run's configuration, and its settings problems (load_settings', lab_config's and the URL servers')."""
    write_role_files(layout, settings, trial=trial)
    config = lab_config(layout, envelope, settings, trial=trial, resolve=resolve)
    return config, list(settings.problems) + tool_server_problems(envelope, settings, resolve=resolve)


def _common_fields(settings: Settings, config: dict, limits: Limits) -> dict:
    return {'role': 'lab', 'codex_version': _codex_version(), 'timeout_seconds': limits.lab_seconds,
            'network': settings.network, 'tool_servers': sorted(config['mcp_servers'])}


def run_task(layout: Layout, task_id: str, *, resolve=socket.getaddrinfo) -> dict:
    """Run a task in work/ and return its finished call record. Refused or ValueError before any call.

    resolve looks up the hosts of URL tool servers (socket.getaddrinfo; tests pass a stand-in)."""
    task = load_task(layout.tasks, task_id)
    records = Records(layout.records)
    with _noting_refusals(records, {'purpose': 'task', 'task': task.id}):
        limits = _admit(layout, records)
        _no_codex_entry(layout.work)
        store = _store(layout)
        try:
            store.commit(layout.work, MANUAL, '运行前发现的人工改动')
        except (ValueError, RuntimeError) as error:       # e.g. a nested .git, or a file git cannot read
            raise Refused(f'work/ 里有改动，但存不成版本：{error}。请处理后再运行。') from None
        version_before = store.head()
        envelope = load_envelope(layout.owner)
        settings = load_settings(layout.work)
        write_home_config(layout, envelope, settings, resolve=resolve)
        config, settings_problems = _compose(layout, envelope, settings, trial=False, resolve=resolve)
        _own_interpreter(layout, task)
        fields = {**_common_fields(settings, config, limits), 'purpose': 'task', 'cost_class': 'online',
                  'task': task.id, 'probe': None, 'round': None, 'workspace': 'work',
                  'version_before': version_before, 'commit': store.resolve(version_before),
                  'tree': store.tree_of(version_before), 'settings_problems': settings_problems}
    run = _Run(layout.work, False, task, task.id, prompt_for(task), config, limits.lab_seconds, fields,
               version_before=version_before)
    return _execute(layout, records, store, run)


def run_trial(layout: Layout, *, commit: str, round_id: str, task_id: str | None = None, probe: str | None = None,
              resolve=socket.getaddrinfo) -> dict:
    """Run a task, or a probe text, on a commit of the version store in trial/; return the finished call record.

    main and work/ are left alone. Refused or ValueError before any call."""
    if (task_id is None) == (probe is None):
        raise ValueError('试跑要么给一个任务编号，要么给一段测试文字，只能给其中一个。')
    safe_id(round_id, '轮次编号')
    task = load_task(layout.tasks, task_id) if task_id is not None else None
    if probe is not None and (not isinstance(probe, str) or not probe.strip() or len(probe) > PROBE_LIMIT):
        raise ValueError(f'测试文字必须是非空文本，最多 {PROBE_LIMIT} 个字符。')
    records = Records(layout.records)
    with _noting_refusals(records, {'purpose': 'trial', 'round': round_id, 'task': task_id}):
        store = _store(layout)
        commit_id = store.resolve(commit)                   # ValueError: a bad request, whatever the limits
        limits = _admit(layout, records)
        layout.trial.mkdir(exist_ok=True)
        store.checkout(commit_id, layout.trial)
        _no_codex_entry(layout.trial)
        envelope = load_envelope(layout.owner)
        write_home_config(layout, envelope, load_settings(layout.work), resolve=resolve)
        settings = load_settings(layout.trial)
        config, settings_problems = _compose(layout, envelope, settings, trial=True, resolve=resolve)
        _own_interpreter(layout, task)
        fields = {**_common_fields(settings, config, limits), 'purpose': 'trial', 'cost_class': 'meta',
                  'task': task_id, 'probe': probe, 'round': round_id, 'workspace': 'trial',
                  'version_before': None, 'commit': commit_id, 'tree': store.tree_of(commit_id),
                  'settings_problems': settings_problems}
    if task is not None:
        prompt, out_name = prompt_for(task), task.id
    else:
        prompt, out_name = probe.rstrip() + '\n\n' + results_sentence(PROBE) + '\n', PROBE
    run = _Run(layout.trial, True, task, out_name, prompt, config, limits.lab_seconds, fields)
    return _execute(layout, records, store, run)


def _end_interrupted(records: Records, call_id: str) -> None:
    """Best effort while an exception is on its way out; `rsi recover` marks the call if this fails too."""
    try:
        records.finish_call(call_id, {'status': 'interrupted', 'ended': _now(), 'usage': None})
    except Exception:
        pass


def _prepare(run: _Run) -> None:
    """Make the workspace ready: its TMPDIR, no stale results, the task's inputs."""
    try:
        os.mkdir(run.workspace / '.tmp')
    except FileExistsError:
        pass
    run.notes += clear(run.workspace, 'out', run.out_name, report=True)
    if run.task is not None:
        run.notes += place_inputs(run.task, run.workspace)


def _execute(layout: Layout, records: Records, store: VersionStore, run: _Run) -> dict:
    """Start the call, run Codex, collect, finish. If anything raises once the call has started (Ctrl-C, say), the
    call is finished as `interrupted` before the exception goes on; the inputs are removed either way."""
    _prepare(run)
    call_id = None
    try:
        call_id = records.new_id('R')
        call_dir = records.call_dir(call_id)
        records.start_call(call_id, {**run.fields, 'started': _now()})
        result = run_codex(home=layout.lab_home, cwd=run.workspace, prompt=run.prompt, config=run.config,
                           record_dir=call_dir, timeout=run.timeout)
        return records.finish_call(call_id, _collect(layout, store, run, call_id, call_dir, result))
    except BaseException:
        if run.task is not None:
            try:
                clear(run.workspace, 'in', run.task.id)
            except OSError:
                pass
        if call_id is not None:
            _end_interrupted(records, call_id)
        raise


def _collect(layout: Layout, store: VersionStore, run: _Run, call_id: str, call_dir: Path, result) -> dict:
    """Everything after the Codex call, as the final fields of call.json; what goes wrong becomes a problem."""
    problems = list(run.notes)
    subagents = []
    if result.thread_id is not None:
        try:
            subagents = collect_sessions(layout.lab_home, result.thread_id, call_dir / 'sessions')
        except (OSError, ValueError) as error:
            problems.append(f'没能取出会话记录：{error}')
    out = call_dir / 'out'
    try:
        problems += collect_out(run.workspace, run.out_name, out)
    except OSError as error:
        problems.append(f'out/{run.out_name} 没能收进记录：{error}')
    check = None
    if run.task is not None:
        try:
            problems += clear(run.workspace, 'in', run.task.id)
        except OSError as error:
            problems.append(f'in/{run.task.id} 没能删掉：{error}')
        try:
            check = run_check(run.task, out)
        except OSError as error:
            check = {'passed': False, 'message': '检查脚本没能启动', 'exit_code': None}
            problems.append(f'检查脚本没能启动：{error}')

    end = {'status': result.status, 'exit_code': result.exit_code, 'thread_id': result.thread_id,
           'usage': result.usage, 'command': result.command, 'ended': _now(), 'check': check,
           'subagent_threads': subagents, 'version_after': None}
    if run.trial:
        try:
            end['tree_after'] = store.tree_of_dir(run.workspace)
        except (ValueError, RuntimeError) as error:
            end['tree_after'] = None
            problems.append(f'试跑后的工作区算不出版本树：{error}')
    else:
        end['version_after'] = _commit_lab(store, run, call_id, call_dir, problems)
    end['problems'] = problems
    return end


def _commit_lab(store: VersionStore, run: _Run, call_id: str, call_dir: Path, problems: list[str]) -> str | None:
    """Commit work/ as the Lab's new version and write diff.patch (empty without a new version)."""
    version, diff = None, ''
    try:
        version = store.commit(run.workspace, f'lab {call_id}', f'Lab 运行 {call_id}（任务 {run.task.id}）')
    except (ValueError, RuntimeError) as error:
        problems.append(f'工作区没能存成新版本：{error}')
    if version is not None:
        try:
            diff = store.diff(run.version_before, version)
        except (ValueError, RuntimeError) as error:
            problems.append(f'没能算出 {run.version_before} 到 {version} 的差异：{error}')
    with open(call_dir / 'diff.patch', 'x', encoding='utf-8') as stream:
        stream.write(diff)
    return version
