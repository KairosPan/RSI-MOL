"""One Lab run (design §5.4): a task in work/, or a trial of a commit in trial/.

run_task: the task must have its four sections, the gate and the owner's daily limits must allow a call, and work/
must hold no `.codex`. Changes found in work/ are first committed as a manual version. The configuration is
composed from the owner's envelope and work/'s own settings, the task's inputs are copied to work/in/<id>/, and one
Codex call runs in work/. Then out/<id>/ is collected into the call's record, in/<id>/ is removed, the owner's check
script runs on the collected results, and work/ is committed as a new version by the Lab.

run_trial does the same in trial/ (emptied, then checked out from a given commit), with a task or with a probe text
instead, and never commits: it is Meta's evidence, cost class `meta`.

Whatever the Lab leaves behind must never block, crash or starve a later run, and a finished call keeps its status
and usage:
- before every commit the workspace is made storable (rsi.workspace.prepare_for_commit): what git refuses is renamed
  or moved to library/moved/, and reported;
- before a call, if preparing or committing the workspace still fails, the workspace is quarantined (set apart whole
  under quarantine/), the last version (task: main's head; trial: the trial commit) is checked out into a fresh
  folder, a `quarantine` note is written, and the run goes on with a problem line; only if that fails too is the run
  Refused. Other refusals are noted in records/index.jsonl; a bad request is a ValueError;
- once a call has started it is always finished; after Codex has ended, a failing step becomes a line of the call's
  `problems` and never changes its status or usage. If the Lab's commit fails, work/ is quarantined and main's head
  restored at once, so nothing of that state is later committed as the owner's.
The workspace is the Lab's and the control program runs outside every sandbox, so nothing in it is followed through
a link.
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
from rsi.tasks import Task, collect_out, load_task, place_inputs, prompt_for, results_sentence, run_check
from rsi.versions import VersionStore
from rsi.workspace import clear, empty_folder, kind, open_folder, prepare_for_commit, quarantine, real_folder

MANUAL = 'owner (manual)'
PROBE = 'probe'                 # the results folder of a probe: out/probe/
PROBE_LIMIT = 20_000            # characters of a probe text
# What can go wrong with a folder the Lab wrote, or with git on it (a time-out included).
WORKSPACE_ERRORS = (OSError, subprocess.SubprocessError, RuntimeError, ValueError)


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


@contextmanager
def _refusing(what: str):
    """Before a call: turn a workspace or git failure into a Refused (which _noting_refusals notes)."""
    try:
        yield
    except WORKSPACE_ERRORS as error:
        raise Refused(f'{what}：{type(error).__name__}: {error}') from None


@contextmanager
def _problem(problems: list[str], what: str):
    """After a call: turn any failure of one step into a problem line of the call, and go on."""
    try:
        yield
    except Exception as error:
        problems.append(f'{what}：{type(error).__name__}: {error}')


def _admit(layout: Layout, records: Records) -> tuple[Limits, list[dict]]:
    """The owner's limits and the calls so far, once the gate and the daily caps allow another call."""
    reason = gate(layout)
    if reason:
        raise Refused(reason)
    limits = load_limits(layout.owner)
    calls = records.calls()
    reason = refusal(limits, calls, datetime.now().astimezone())
    if reason:
        raise Refused(reason)
    return limits, calls


def _store(layout: Layout) -> VersionStore:
    store = VersionStore(layout.versions)
    if not (store.git_dir / 'HEAD').is_file():
        raise Refused(f'还没有版本库（{layout.versions}），请先运行 rsi init。')
    return store


def _no_codex_entry(workspace: Path) -> None:
    """Refused if the workspace holds a `.codex` entry. A workspace that is not a usable folder is not judged here:
    preparing it fails, and it is quarantined."""
    try:
        fd = open_folder(workspace)
    except OSError:
        return
    try:
        if kind('.codex', fd) is not None:
            raise Refused(f'{workspace} 里有 .codex，不能在这里启动 Lab Codex：请先移走它。')
    finally:
        os.close(fd)


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


def _uncommitted(calls: list[dict]) -> str | None:
    """The id of the last task run if its changes may still be in work/ without having become a version (its commit
    failed and work/ was not restored, or it was interrupted), else None."""
    for call in reversed(calls):
        if call.get('purpose') == 'task':
            failed = (call.get('commit_failed') is True and call.get('workspace_restored') is not True
                      or call.get('status') == 'interrupted')
            return call['id'] if failed and isinstance(call.get('id'), str) else None
    return None


def _commit_manual(store: VersionStore, layout: Layout, calls: list[dict]) -> list[str]:
    """Commit what changed in work/ since the last version, once it is made storable; return the notes. Changes left
    by a Lab run whose own commit failed (or that was interrupted) are named after that run. Raises what preparing or
    committing raises (WORKSPACE_ERRORS)."""
    notes = prepare_for_commit(layout.work, layout.library, 'manual')
    previous = _uncommitted(calls)
    if previous is None:
        author, message = MANUAL, '运行前发现的人工改动'
    else:
        author, message = f'owner (manual) with lab {previous}', f'运行前发现的改动，含 Lab 运行 {previous} 没能存成版本的改动'
    store.commit(layout.work, author, message)
    return notes


def _rescue(layout: Layout, records: Records, store: VersionStore, workspace: Path, version: str, label: str,
            error: BaseException, context: dict) -> str:
    """Before a call: quarantine a workspace that could not be prepared or committed, check `version` (a version
    name or commit) out into the fresh folder, note it, and return the problem line for the call. Refused if the
    quarantine or the checkout fails too."""
    reason = f'{type(error).__name__}: {error}'
    try:
        moved = quarantine(layout, workspace, label)
        store.checkout(version, workspace)
    except WORKSPACE_ERRORS as second:
        raise Refused(f'{workspace.name}/ 准备不了（{reason}），隔离它也失败了：{type(second).__name__}: {second}') from None
    where = moved.relative_to(layout.root).as_posix() if moved is not None else None
    records.note('quarantine', {'workspace': workspace.name, 'moved_to': where, 'reason': reason,
                                'restored': version, **context})
    return f'{workspace.name}/ 准备不了（{reason}），已整个隔离到 {where}，并从 {version} 重新检出'


def _prepare(run: _Run) -> None:
    """Make the workspace ready: a real .tmp folder (TMPDIR and TMPPREFIX point into it), no stale results, the
    task's inputs."""
    run.notes += real_folder(run.workspace, '.tmp')
    run.notes += clear(run.workspace, 'out', run.out_name, report=True)
    if run.task is not None:
        run.notes += place_inputs(run.task, run.workspace)


def _prepare_or_rescue(layout: Layout, records: Records, store: VersionStore, run: _Run, version: str, label: str,
                       context: dict) -> None:
    try:
        _prepare(run)
    except WORKSPACE_ERRORS as error:
        run.notes.append(_rescue(layout, records, store, run.workspace, version, label, error, context))
        with _refusing('工作区准备不了'):
            _prepare(run)


def run_task(layout: Layout, task_id: str, *, resolve=socket.getaddrinfo) -> dict:
    """Run a task in work/ and return its finished call record. Refused or ValueError before any call.

    resolve looks up the hosts of URL tool servers (socket.getaddrinfo; tests pass a stand-in)."""
    task = load_task(layout.tasks, task_id)
    records = Records(layout.records)
    context = {'purpose': 'task', 'task': task.id}
    with _noting_refusals(records, context):
        limits, calls = _admit(layout, records)
        store = _store(layout)
        _no_codex_entry(layout.work)
        try:
            notes = _commit_manual(store, layout, calls)
        except WORKSPACE_ERRORS as error:
            notes = [_rescue(layout, records, store, layout.work, store.head(), 'work', error, context)]
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
        run = _Run(layout.work, False, task, task.id, prompt_for(task), config, limits.lab_seconds, fields, notes,
                   version_before=version_before)
        _prepare_or_rescue(layout, records, store, run, version_before, 'work', context)
    return _execute(layout, records, store, run)


def run_trial(layout: Layout, *, commit: str, round_id: str, task_id: str | None = None, probe: str | None = None,
              resolve=socket.getaddrinfo) -> dict:
    """Run a task, or a probe text, on a commit of the version store in trial/; return the finished call record.

    trial/ is emptied first (its .tmp, out and in included), then checked out. main and work/ are left alone.
    Refused or ValueError before any call."""
    if (task_id is None) == (probe is None):
        raise ValueError('试跑要么给一个任务编号，要么给一段测试文字，只能给其中一个。')
    safe_id(round_id, '轮次编号')
    task = load_task(layout.tasks, task_id) if task_id is not None else None
    if probe is not None and (not isinstance(probe, str) or not probe.strip() or len(probe) > PROBE_LIMIT):
        raise ValueError(f'测试文字必须是非空文本，最多 {PROBE_LIMIT} 个字符。')
    records = Records(layout.records)
    context = {'purpose': 'trial', 'round': round_id, 'task': task_id}
    with _noting_refusals(records, context):
        store = _store(layout)
        commit_id = store.resolve(commit)                   # ValueError: a bad request, whatever the limits
        limits, _ = _admit(layout, records)
        notes = []
        try:
            layout.trial.mkdir(exist_ok=True)
            empty_folder(layout.trial)                      # whatever an earlier trial left, never followed
            store.checkout(commit_id, layout.trial)
        except WORKSPACE_ERRORS as error:
            notes.append(_rescue(layout, records, store, layout.trial, commit_id, 'trial', error, context))
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
        run = _Run(layout.trial, True, task, out_name, prompt, config, limits.lab_seconds, fields, notes)
        _prepare_or_rescue(layout, records, store, run, commit_id, 'trial', context)
    return _execute(layout, records, store, run)


def _end_interrupted(records: Records, call_id: str) -> None:
    """Best effort while an exception is on its way out; `rsi recover` marks the call if this fails too."""
    try:
        records.finish_call(call_id, {'status': 'interrupted', 'ended': _now(), 'usage': None})
    except Exception:
        pass


def _remove_inputs_quietly(run: _Run) -> None:
    if run.task is not None:
        try:
            clear(run.workspace, 'in', run.task.id)
        except OSError:
            pass


def _execute(layout: Layout, records: Records, store: VersionStore, run: _Run) -> dict:
    """Start the call, run Codex, collect, finish.

    If Codex is stopped by an exception (Ctrl-C, say), the call is finished as `interrupted` and the exception goes
    on. After Codex has ended, the call keeps Codex's status and usage whatever happens: a failing step is a problem
    line, and an interruption while collecting is finished with what was collected and then goes on."""
    call_id = None
    try:
        call_id = records.new_id('R')
        call_dir = records.call_dir(call_id)
        records.start_call(call_id, {**run.fields, 'started': _now()})
        result = run_codex(home=layout.lab_home, cwd=run.workspace, prompt=run.prompt, config=run.config,
                           record_dir=call_dir, timeout=run.timeout)
    except BaseException:
        _remove_inputs_quietly(run)
        if call_id is not None:
            _end_interrupted(records, call_id)
        raise
    end = {'status': result.status, 'exit_code': result.exit_code, 'thread_id': result.thread_id,
           'usage': result.usage, 'command': result.command, 'ended': _now(), 'check': None,
           'subagent_threads': [], 'version_after': None}
    if not run.trial:
        end['commit_failed'] = True                 # until the Lab's commit succeeds, whatever stops collecting
    problems = list(run.notes)
    try:
        _collect(layout, records, store, run, call_id, call_dir, result, end, problems)
    except BaseException as error:                  # only an interruption gets here: each step catches Exception
        problems.append(f'收取结果时被打断（{type(error).__name__}），其余步骤没有做')
        _remove_inputs_quietly(run)
        try:
            records.finish_call(call_id, {**end, 'problems': problems})
        except Exception:
            pass
        raise
    return records.finish_call(call_id, {**end, 'problems': problems})


def _collect(layout: Layout, records: Records, store: VersionStore, run: _Run, call_id: str, call_dir: Path, result,
             end: dict, problems: list[str]) -> None:
    """Everything after the Codex call, into end and problems; each failing step becomes a problem line."""
    if result.thread_id is not None:
        with _problem(problems, '没能取出会话记录'):
            end['subagent_threads'] = collect_sessions(layout.lab_home, result.thread_id, call_dir / 'sessions')
    out = call_dir / 'out'
    with _problem(problems, f'out/{run.out_name} 没能收进记录'):
        problems += collect_out(run.workspace, run.out_name, out)
    if run.task is not None:
        with _problem(problems, f'in/{run.task.id} 没能删掉'):
            problems += clear(run.workspace, 'in', run.task.id)
        end['check'] = {'passed': False, 'message': '检查脚本没能运行', 'exit_code': None}
        with _problem(problems, '检查脚本没能运行'):
            end['check'] = run_check(run.task, out)
    with _problem(problems, '工作区没能整理成可存的样子'):
        problems += prepare_for_commit(run.workspace, layout.library, call_id)
    if run.trial:
        end['tree_after'] = None
        with _problem(problems, '试跑后的工作区算不出版本树'):
            end['tree_after'] = store.tree_of_dir(run.workspace)
        return
    with _problem(problems, '工作区没能存成新版本'):
        end['version_after'] = store.commit(run.workspace, f'lab {call_id}', f'Lab 运行 {call_id}（任务 {run.task.id}）')
        end['commit_failed'] = False
    if end['commit_failed']:
        with _problem(problems, 'work/ 没能隔离并恢复'):
            _restore_work(layout, records, store, call_id, end, problems)
    diff = ''
    if end['version_after'] is not None:
        with _problem(problems, f'没能算出 {run.version_before} 到 {end["version_after"]} 的差异'):
            diff = store.diff(run.version_before, end['version_after'])
    with _problem(problems, '没能写 diff.patch'):
        with open(call_dir / 'diff.patch', 'x', encoding='utf-8') as stream:
            stream.write(diff)


def _restore_work(layout: Layout, records: Records, store: VersionStore, call_id: str, end: dict,
                  problems: list[str]) -> None:
    """After a Lab commit that failed: quarantine work/ and check main's head out into a fresh folder, so the next run
    starts clean and nothing of this state is ever committed as the owner's."""
    head = store.head()
    moved = quarantine(layout, layout.work, f'work-{call_id}')
    store.checkout(head, layout.work)
    where = moved.relative_to(layout.root).as_posix() if moved is not None else None
    records.note('quarantine', {'workspace': 'work', 'moved_to': where, 'reason': 'Lab 的改动存不成版本',
                                'restored': head, 'call': call_id})
    end['workspace_restored'] = True
    problems.append(f'work/ 存不成版本，已整个隔离到 {where}，并从 {head} 重新检出；这次运行的改动没有进版本')
