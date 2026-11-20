"""One Lab run (design §5.4): a task in work/, or a trial of a commit in trial/.

Agents write work/ and trial/ by path, and a process an agent started may outlive its call, so the control program
never reads, walks, changes or runs git on either folder in place. It builds the next workspace in staging/<label>/
(no agent profile grants it), seals whatever sits at work/ (trial/) by renaming that entry inside the RSI root
(rsi.workspace.seal), renames the staged folder in, and starts the call. A folder staged for work/ or trial/ first gets
its skills/ copied to .agents/skills/, where Codex loads them (rsi.skills). After the call it seals the workspace again
and does everything else on the sealed, static tree: collecting out/<id>/, the check, making it storable and the
commit. A sealed tree is then removed; when something could not be versioned (or it cannot be removed) it is kept
under sealed/<label>/ with a `sealed` note and a problem line of the call.

run_task: the task, that no earlier interrupted rsi is left to recover from (unrecovered), the gate, the owner's
daily limits, a version store that can take a new version, the envelope and Codex itself are checked first. Then
what sat in work/ is sealed and committed as `manual or leftover` (when it differs from head), main's head is staged
with the task's inputs in in/<id>/ and renamed into work/, the configuration is composed from the owner's envelope
and that version's settings, and one Codex call runs in work/. Afterwards the sealed tree is committed by the Lab,
and main's head is staged and renamed into work/ again, so the owner can keep opening the Lab Codex there by hand.

run_trial does the same in trial/, staged from a given commit, with a task or with a probe text instead, and never
commits: it is Meta's evidence, cost class `meta`. trial/ is sealed and removed afterwards.

Refusals before a call are noted in records/index.jsonl and start no call; a bad request is a ValueError. Once a call
has started it is always finished, and after Codex has ended a failing step becomes a line of the call's `problems`
and never changes its status or usage.
"""
import os
import secrets
import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rsi import check
from rsi.codexrun import codex_version, run_codex
from rsi.compose import lab_config, load_envelope, tool_server_problems, write_home_config, write_role_files
from rsi.limits import Limits, load_limits, refusal
from rsi.paths import Layout, Refused, safe_id
from rsi.records import STARTED, Records
from rsi.sessions import collect_sessions
from rsi.settings import Settings, load_settings
from rsi.skills import materialise
from rsi.tasks import Task, collect_out, load_task, place_inputs, prompt_for, results_sentence, run_check
from rsi.versions import VersionStore
from rsi.workspace import SEALED, has_entry, keep, open_folder, prepare_for_commit, remove, seal

MANUAL = 'manual or leftover'   # author of what is found in work/ before a run: the owner's or a leftover process's
RESTAGED = 'In the newest version put back into work/ after the run: '    # before a skill line of that work/
PROBE = 'probe'                 # the results folder of a probe: out/probe/
PROBE_LIMIT = 20_000            # characters of a probe text
PROCESS_FILE = 'process.json'   # in a call's record folder: the pid and start time of its Codex process
LAB_WORKSPACES = ('work', 'trial')  # where a Lab Codex runs: moved in with the skills in .agents/skills/
# What can go wrong with a sealed or staged folder, or with git on it (a time-out included).
WORKSPACE_ERRORS = (OSError, subprocess.SubprocessError, RuntimeError, ValueError)


def gate(layout: Layout) -> str | None:
    """Why no agent may start now, or None: unless the last `rsi check` passed for the Codex version installed now
    (rsi.check.gate). Every Lab run and every Meta call is admitted through it (admit)."""
    return check.gate(layout)


@dataclass
class _Run:
    name: str                                               # the workspace: 'work' or 'trial'
    task: Task | None
    out_name: str
    prompt: str
    config: dict
    timeout: int
    fields: dict                                            # call.json from the start
    context: dict                                           # purpose, task, round: for notes
    notes: list[str] = field(default_factory=list)         # problems found before the call
    version_before: str | None = None
    envelope: dict | None = None                            # a task run: for the owner's Lab config afterwards
    resolve: object = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _stamp() -> str:
    """A label for a staged or sealed folder that is not named after a call."""
    return datetime.now().strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3)


def process_start(pid: int) -> str | None:
    """When the process pid started, as local ISO time to the second, from ps; None when that cannot be told."""
    try:
        done = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=10, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = ' '.join(done.stdout.split())
    if done.returncode != 0 or not text:
        return None
    try:
        return datetime.strptime(text, '%a %b %d %H:%M:%S %Y').astimezone().isoformat(timespec='seconds')
    except ValueError:
        return None


def process_recorder(records: Records, call_id: str):
    """run_codex's on_start for a call: records runs/<call>/process.json, {pid, started} (the start time as
    process_start tells it), created once like every record file, so `rsi recover` can tell that the process group
    is still this call's. Never raises an Exception: run_codex would stop the call."""
    def record(pid: int) -> None:
        try:
            records.write_json(f'runs/{call_id}/{PROCESS_FILE}', {'pid': pid, 'started': process_start(pid) or _now()})
        except Exception:
            pass
    return record


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
    """Before a call: turn a workspace, store or git failure into a Refused (which _noting_refusals notes)."""
    try:
        yield
    except WORKSPACE_ERRORS as error:
        raise Refused(f'{what}: {type(error).__name__}: {error}') from None


@contextmanager
def _problem(problems: list[str], what: str):
    """After a call: turn any failure of one step into a problem line of the call, and go on."""
    try:
        yield
    except Exception as error:
        problems.append(f'{what}: {type(error).__name__}: {error}')


@contextmanager
def _root(layout: Layout):
    """A descriptor of the RSI root, which no agent can write: seals and renames are made relative to it."""
    fd = open_folder(layout.root)
    try:
        yield fd
    finally:
        os.close(fd)


def unrecovered(layout: Layout, records: Records) -> None:
    """Refused while an earlier rsi was ended before it could finish and `rsi recover` has not run since: a call
    still `started` (its Codex may still be running, writing by path into what a next run would use), or a sealed
    entry still in the root (`.sealed-*`: a tree that was being handled, whose content is in no version). rsi recover
    stops the first and keeps the second under sealed/. rsi task, rsi meta and rsi rollback check this first."""
    started = [str(call.get('id')) for call in records.calls() if call.get('status') == STARTED]
    sealed = sorted(name for name in os.listdir(layout.root) if name.startswith(SEALED))
    what = ([f'the call {call_id} has not ended' for call_id in started[:3]]
            + [f'{name} is still in the RSI folder' for name in sealed[:3]])
    if len(started) + len(sealed) > len(what):
        what.append(f'and {len(started) + len(sealed) - len(what)} more')
    if what:
        raise Refused(f'An earlier rsi was interrupted and nothing has cleaned up after it: {"; ".join(what)}. '
                      'Run rsi recover first, then try again.')


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
    """The version store, once it is known to take a new version now: no call is spent on a run whose result could
    not be stored."""
    store = VersionStore(layout.versions)
    if not (store.git_dir / 'HEAD').is_file():
        raise Refused(f'There is no version store yet ({layout.versions}). Run rsi init first.')
    with _refusing(f'The version store ({layout.versions}) cannot take a new version now; repair it first'):
        store.check_writable()
    return store


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _own_interpreter(layout: Layout, task: Task | None) -> None:
    """A check script runs with this program's own Python, so that Python must not lie where an agent can write."""
    if task is None or task.check is None:
        return
    forms = {os.path.abspath(sys.executable), os.path.realpath(sys.executable)}
    roots = {os.path.abspath(layout.root), os.path.realpath(layout.root)}
    if not sys.executable or any(_within(form, root) for form in forms for root in roots):
        raise Refused(f'The Python of the control program ({sys.executable!r}) is inside the RSI folder, where '
                      'agents can change it, so it cannot run a check script. Run rsi with a python3 outside the RSI '
                      'folder.')


def _codex_version(layout: Layout) -> str:
    try:
        return codex_version(layout.root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise Refused(f'The Codex program cannot be used: {error}') from None


def _compose(layout: Layout, envelope: dict, settings: Settings, *, trial: bool, resolve) -> tuple[dict, list[str]]:
    """The run's configuration, and its settings problems (load_settings', lab_config's and the URL servers')."""
    write_role_files(layout, settings, trial=trial)
    config = lab_config(layout, envelope, settings, trial=trial, resolve=resolve)
    return config, list(settings.problems) + tool_server_problems(envelope, settings, resolve=resolve)


def _common_fields(codex: str, settings: Settings, config: dict, limits: Limits) -> dict:
    return {'role': 'lab', 'codex_version': codex, 'timeout_seconds': limits.lab_seconds,
            'network': settings.network, 'tool_servers': sorted(config['mcp_servers'])}


# Staging, and renaming into place.

def _discard(folder: Path) -> None:
    """Remove a folder only the control program writes (staging/<label>), best effort."""
    try:
        fd = open_folder(folder.parent)
        try:
            remove(folder.name, fd, str(folder))
        finally:
            os.close(fd)
    except OSError:
        pass


def _stage(layout: Layout, store: VersionStore, version: str, task: Task | None = None) -> tuple[Path, Settings]:
    """The next workspace, in staging/<label>/: the version (a version name or commit id), a real .tmp/ and out/,
    and the task's inputs in in/<id>/; and its settings. Refused when the version holds a `.codex` entry. Removed
    again if anything fails."""
    layout.staging.mkdir(exist_ok=True)
    folder = layout.staging / _stamp()
    folder.mkdir()
    try:
        store.checkout(version, folder)
        if has_entry(folder, '.codex'):
            raise Refused(f'The version {version} holds .codex, so the Lab Codex cannot be started on it. Remove '
                          '.codex from the version first.')
        (folder / '.tmp').mkdir()
        (folder / 'out').mkdir()
        if task is not None:
            place_inputs(task, folder)
        return folder, load_settings(folder)
    except BaseException:
        _discard(folder)
        raise


def _rename_in(staged: Path, root_fd: int, name: str) -> None:
    """The staged folder becomes <root>/<name>: one rename, relative to the root's descriptor. It fails, and
    replaces nothing, when a link, a file or a non-empty folder is there."""
    fd = open_folder(staged.parent)
    try:
        os.rename(staged.name, name, src_dir_fd=fd, dst_dir_fd=root_fd)
    finally:
        os.close(fd)


def _move_in(layout: Layout, records: Records, root_fd: int, staged: Path, name: str, context: dict,
             problems: list[str], *, skill_lines: list[str] | None = None) -> None:
    """Seal whatever sits at <root>/<name> now, then rename the staged folder there. A found trial/ (an earlier
    trial's leftover) is removed; a found work/ is kept under sealed/, never versioned: it appeared after work/ was
    sealed and committed. The staged folder is removed if this fails.

    Before that, a folder staged for work/ or trial/ (where a Lab Codex runs; candidate/ is moved in elsewhere) gets
    its skills/ copied to .agents/skills/ (rsi.skills.materialise); the lines of what was left out go to skill_lines,
    or to problems when that is None."""
    try:
        if name in LAB_WORKSPACES:
            (problems if skill_lines is None else skill_lines).extend(materialise(staged))
        label = _stamp()
        entry = seal(root_fd, name, label)
        if entry is not None:
            reasons = [] if name == 'trial' else ['it appeared while the run was being prepared; it was not saved '
                                                  'as a version']
            _dispose(layout, records, root_fd, entry, name, label, reasons, context, problems)
        _rename_in(staged, root_fd, name)
    except BaseException:
        _discard(staged)
        raise


# Sealed trees.

def _dispose(layout: Layout, records: Records, root_fd: int, entry: str, name: str, label: str, reasons: list[str],
             context: dict, problems: list[str]) -> None:
    """Remove the sealed entry <root>/<entry> (which was <root>/<name>), or keep it as sealed/<label>/<name>, with a
    `sealed` note and a problem line, when there are reasons to or it cannot be removed. Moving it is best effort:
    if that fails it stays where it is, and the note says so."""
    path = str(layout.root / entry)
    if not reasons:
        try:
            remove(entry, root_fd, path)
            return
        except OSError as error:
            reasons = [f'it cannot be removed ({error.strerror or error})']
    where = entry
    try:
        keep(root_fd, entry, path, layout.sealed / label / name)
        where = f'{layout.sealed.name}/{label}/{name}'
    except OSError:
        pass
    records.note('sealed', {'workspace': name, 'path': where, 'reasons': reasons, **context})
    problems.append(f'The sealed {name}/: {"; ".join(reasons)}; it was kept whole at {where}')


def _settle(layout: Layout, records: Records, root_fd: int, entry: str, name: str, label: str, save, context: dict,
            problems: list[str]) -> None:
    """Handle the sealed tree <root>/<entry>: make it storable (its lines go to problems), and save(tree), a commit
    or a tree id, unless it holds `.codex` or something that must be left out could not be moved out of it: a version
    never holds what the rules leave out. Then _dispose: the tree is kept when save failed or was not done, or when
    anything was left out of the version. A failure is a reason to keep it, never an exception."""
    tree, reasons = layout.root / entry, []
    try:
        lines, moved, blocked = prepare_for_commit(tree, layout.sealed / label / 'left-out',
                                                   f'{layout.sealed.name}/{label}/left-out')
        problems += lines
        if moved:
            reasons.append(f'{moved} entries were not saved in the version; they were moved to '
                           f'{layout.sealed.name}/{label}/left-out/')
        if blocked:
            reasons.append(f'{blocked} entries that must be left out of the version could not be moved out; the '
                           'version was not saved')
        if has_entry(tree, '.codex'):
            reasons.append('it holds .codex; the version was not saved')
        elif not blocked:
            save(tree)
    except WORKSPACE_ERRORS as error:
        reasons.append(f'the version was not saved ({type(error).__name__}: {error})')
    _dispose(layout, records, root_fd, entry, name, label, reasons, context, problems)


def _uncommitted(calls: list[dict]) -> str | None:
    """The id of the last task run when it did not become a version (its commit failed or it was interrupted)."""
    for call in reversed(calls):
        if call.get('purpose') == 'task':
            failed = call.get('commit_failed') is True or call.get('status') == 'interrupted'
            return call['id'] if failed and isinstance(call.get('id'), str) else None
    return None


def _take_work(layout: Layout, records: Records, store: VersionStore, root_fd: int, calls: list[dict],
               context: dict, notes: list[str]) -> None:
    """Before a task run: seal what sits in work/ and commit it as `manual or leftover` when it differs from head
    (the program cannot tell the owner's edits from what a leftover process wrote). Refused if work/ cannot be
    sealed; anything else becomes a problem line (and a `sealed` note when the tree is kept)."""
    label = _stamp()
    with _refusing('work/ cannot be sealed, so this run cannot start (check the flags and the ACL of work/ itself)'):
        entry = seal(root_fd, 'work', label)
    if entry is None:
        return
    previous = _uncommitted(calls)
    message = 'Changes found in work/ before a run (made by hand, or left by an earlier process)'
    if previous is not None:
        message += f'; the last task run {previous} could not be saved as a version'
    _settle(layout, records, root_fd, entry, 'work', label, lambda tree: store.commit(tree, MANUAL, message), context,
            notes)


def _restore_work(layout: Layout, records: Records, store: VersionStore, root_fd: int, context: dict) -> str:
    """After a refusal that came once work/ was sealed: stage main's head and move it into work/ again (best
    effort), so the owner can keep opening the Lab Codex there. '' when that worked, else the sentence for the
    refusal saying that there is no usable work/ now, and why."""
    try:
        _move_in(layout, records, root_fd, _stage(layout, store, store.head())[0], 'work', context, [])
        return ''
    except Exception as error:
        return (f'; work/ could not be put back to the newest version of main either ({type(error).__name__}: '
                f'{error}), so there is no usable work/ now')


# The two runs.

def run_task(layout: Layout, task_id: str, *, resolve=socket.getaddrinfo) -> dict:
    """Run a task in work/ and return its finished call record. Refused or ValueError before any call.

    resolve looks up the hosts of URL tool servers (socket.getaddrinfo; tests pass a stand-in)."""
    task = load_task(layout.tasks, task_id)
    records = Records(layout.records)
    context = {'purpose': 'task', 'task': task.id}
    with _noting_refusals(records, context), _root(layout) as root_fd:
        unrecovered(layout, records)
        limits, calls = _admit(layout, records)
        store = _store(layout)
        envelope = load_envelope(layout.owner)
        _own_interpreter(layout, task)
        codex = _codex_version(layout)
        notes, skill_lines = [], []
        _take_work(layout, records, store, root_fd, calls, context, notes)
        try:
            with _refusing('The version store cannot be used'):
                version_before = store.head()
                fields = {'version_before': version_before, 'commit': store.resolve(version_before),
                          'tree': store.tree_of(version_before)}
                staged, settings = _stage(layout, store, version_before, task)
            with _refusing('The staged workspace could not be moved into work/'):
                _move_in(layout, records, root_fd, staged, 'work', context, notes, skill_lines=skill_lines)
        except Refused as refused:              # work/ is sealed by now: put main's head back for the owner
            raise Refused(f'{refused}{_restore_work(layout, records, store, root_fd, context)}') from None
        write_home_config(layout, envelope, settings, resolve=resolve)
        config, settings_problems = _compose(layout, envelope, settings, trial=False, resolve=resolve)
        fields = {**_common_fields(codex, settings, config, limits), 'purpose': 'task', 'cost_class': 'online',
                  'task': task.id, 'probe': None, 'round': None, 'workspace': 'work', **fields,
                  'settings_problems': settings_problems + skill_lines}
        run = _Run('work', task, task.id, prompt_for(task), config, limits.lab_seconds, fields, context, notes,
                   version_before, envelope, resolve)
    return _execute(layout, records, store, run)


def run_trial(layout: Layout, *, commit: str, round_id: str, task_id: str | None = None, probe: str | None = None,
              resolve=socket.getaddrinfo) -> dict:
    """Run a task, or a probe text, on a commit of the version store in trial/; return the finished call record.

    trial/ is staged from that commit, never from main; main and work/ are left alone. Refused or ValueError before
    any call."""
    if (task_id is None) == (probe is None):
        raise ValueError('A trial needs exactly one of a task id and a test text.')
    safe_id(round_id, 'round id')
    task = load_task(layout.tasks, task_id) if task_id is not None else None
    if probe is not None and (not isinstance(probe, str) or not probe.strip() or len(probe) > PROBE_LIMIT):
        raise ValueError(f'A test text must be non-empty text of at most {PROBE_LIMIT} characters.')
    records = Records(layout.records)
    context = {'purpose': 'trial', 'round': round_id, 'task': task_id}
    with _noting_refusals(records, context), _root(layout) as root_fd:
        store = _store(layout)
        try:
            commit_id = store.resolve(commit)               # ValueError: a bad request, whatever the limits
        except (OSError, subprocess.SubprocessError) as error:
            raise Refused(f'The version store cannot be used: {type(error).__name__}: {error}') from None
        limits, _ = _admit(layout, records)
        envelope = load_envelope(layout.owner)
        _own_interpreter(layout, task)
        codex = _codex_version(layout)
        with _refusing('The version store cannot be used'):
            tree = store.tree_of(commit_id)
            head, work_settings = _stage(layout, store, store.head())      # for the owner's own Lab config:
            _discard(head)                                                  # read from main's head, not work/
            staged, settings = _stage(layout, store, commit_id, task)
        notes, skill_lines = [], []
        with _refusing('The staged workspace could not be moved into trial/'):
            _move_in(layout, records, root_fd, staged, 'trial', context, notes, skill_lines=skill_lines)
        write_home_config(layout, envelope, work_settings, resolve=resolve)
        config, settings_problems = _compose(layout, envelope, settings, trial=True, resolve=resolve)
        fields = {**_common_fields(codex, settings, config, limits), 'purpose': 'trial', 'cost_class': 'meta',
                  'task': task_id, 'probe': probe, 'round': round_id, 'workspace': 'trial',
                  'version_before': None, 'commit': commit_id, 'tree': tree,
                  'settings_problems': settings_problems + skill_lines}
        if task is not None:
            prompt, out_name = prompt_for(task), task.id
        else:
            prompt, out_name = probe.rstrip() + '\n\n' + results_sentence(PROBE) + '\n', PROBE
        run = _Run('trial', task, out_name, prompt, config, limits.lab_seconds, fields, context, notes)
    return _execute(layout, records, store, run)


def _end_interrupted(records: Records, call_id: str) -> None:
    """Best effort while an exception is on its way out; `rsi recover` marks the call if this fails too."""
    try:
        records.finish_call(call_id, {'status': 'interrupted', 'ended': _now(), 'usage': None})
    except Exception:
        pass


def _execute(layout: Layout, records: Records, store: VersionStore, run: _Run) -> dict:
    """Start the call, run Codex, collect, finish.

    If Codex is stopped by an exception (Ctrl-C, say), the call is finished as `interrupted` and the exception goes
    on; the workspace is left as it is, and the next task run seals and commits it. After Codex has ended, the call
    keeps Codex's status and usage whatever happens: a failing step is a problem line, and an interruption while
    collecting is finished with what was collected and then goes on."""
    call_id = None
    try:
        call_id = records.new_id('R')
        call_dir = records.call_dir(call_id)
        records.start_call(call_id, {**run.fields, 'started': _now()})
        result = run_codex(home=layout.lab_home, cwd=layout.root / run.name, prompt=run.prompt, config=run.config,
                           record_dir=call_dir, timeout=run.timeout, on_start=process_recorder(records, call_id),
                           root=layout.root)
    except BaseException:
        if call_id is not None:
            _end_interrupted(records, call_id)
        raise
    end = {'status': result.status, 'exit_code': result.exit_code, 'thread_id': result.thread_id,
           'usage': result.usage, 'command': result.command, 'ended': _now(), 'check': None,
           'subagent_threads': [], 'version_after': None}
    if run.name == 'trial':
        end['tree_after'] = None
    else:
        end['commit_failed'] = True                 # until the Lab's commit succeeds, whatever stops collecting
    problems = list(run.notes)
    try:
        _collect(layout, records, store, run, call_id, call_dir, result, end, problems)
    except BaseException as error:                  # only an interruption gets here: each step catches Exception
        problems.append(f'Interrupted while collecting the results ({type(error).__name__}); the remaining steps were '
                        'not done')
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
        with _problem(problems, 'Could not copy the session records'):
            end['subagent_threads'] = collect_sessions(layout.lab_home, result.thread_id, call_dir / 'sessions')
    context = {**run.context, 'call': call_id}
    with _root(layout) as root_fd:
        entry = None
        with _problem(problems, f'{run.name}/ cannot be sealed; the results of this run were not collected'):
            entry = seal(root_fd, run.name, call_id)
            if entry is None:
                problems.append(f'{run.name}/ was gone when the run ended')
        out = call_dir / 'out'
        with _problem(problems, f'out/{run.out_name} could not be collected into the record'):
            if entry is None:
                out.mkdir()
            else:
                problems += collect_out(layout.root / entry, run.out_name, out)
        if run.task is not None:
            end['check'] = {'passed': False, 'message': 'the check script could not run', 'exit_code': None}
            with _problem(problems, 'The check script could not run'):
                end['check'] = run_check(run.task, out)
        if entry is not None:
            with _problem(problems, f'The sealed {run.name}/ could not be handled completely'):
                _settle(layout, records, root_fd, entry, run.name, call_id, _saver(store, run, call_id, end),
                        context, problems)
        if run.name == 'trial':
            return
        diff = ''
        if end['version_after'] is not None:
            with _problem(problems, f'Could not compute the diff from {run.version_before} to {end["version_after"]}'):
                diff = store.diff(run.version_before, end['version_after'])
        with _problem(problems, 'Could not write diff.patch'):
            with open(call_dir / 'diff.patch', 'x', encoding='utf-8') as stream:
                stream.write(diff)
        restaged, settings = [], None       # what of the new head's skills/ the owner's work/ lacks: told at once
        with _problem(problems, 'work/ could not be replaced by the newest version'):
            staged, head_settings = _stage(layout, store, store.head())
            _move_in(layout, records, root_fd, staged, 'work', context, problems, skill_lines=restaged)
            settings = head_settings
        problems += [RESTAGED + line for line in restaged]
        if settings is not None:            # the owner's manual Lab Codex uses the version now in work/
            with _problem(problems, "The Lab's Codex configuration (codex-home/config.toml) could not be updated"):
                write_home_config(layout, run.envelope, settings, resolve=run.resolve)


def _saver(store: VersionStore, run: _Run, call_id: str, end: dict):
    """What becomes of the sealed tree after a call: the Lab's commit (a task run), or its tree id (a trial)."""
    def save(tree: Path) -> None:
        if run.name == 'trial':
            end['tree_after'] = store.tree_of_dir(tree)
        else:
            end['version_after'] = store.commit(tree, f'lab {call_id}', f'Lab run {call_id} (task {run.task.id})')
            end['commit_failed'] = False
    return save


# The same steps for the Meta round (rsi.meta) and the commands (rsi.init), which refuse, stage, seal and rename in
# exactly as a Lab run does. Thin wrappers that look the private helpers up when called, so a test that patches one
# reaches both.

def now() -> str:
    return _now()


def stamp() -> str:
    return _stamp()


def noting_refusals(records: Records, context: dict):
    return _noting_refusals(records, context)


def refusing(what: str):
    return _refusing(what)


def problem(problems: list[str], what: str):
    return _problem(problems, what)


def root_descriptor(layout: Layout):
    return _root(layout)


def admit(layout: Layout, records: Records) -> tuple[Limits, list[dict]]:
    return _admit(layout, records)


def open_store(layout: Layout) -> VersionStore:
    return _store(layout)


def usable_codex_version(layout: Layout) -> str:
    return _codex_version(layout)


def stage(layout: Layout, store: VersionStore, version: str, task: Task | None = None) -> tuple[Path, Settings]:
    return _stage(layout, store, version, task)


def discard(folder: Path) -> None:
    _discard(folder)


def rename_in(staged: Path, root_fd: int, name: str) -> None:
    _rename_in(staged, root_fd, name)


def move_in(layout: Layout, records: Records, root_fd: int, staged: Path, name: str, context: dict,
            problems: list[str]) -> None:
    _move_in(layout, records, root_fd, staged, name, context, problems)


def dispose(layout: Layout, records: Records, root_fd: int, entry: str, name: str, label: str, reasons: list[str],
            context: dict, problems: list[str]) -> None:
    _dispose(layout, records, root_fd, entry, name, label, reasons, context, problems)


def settle(layout: Layout, records: Records, root_fd: int, entry: str, name: str, label: str, save, context: dict,
           problems: list[str]) -> None:
    _settle(layout, records, root_fd, entry, name, label, save, context, problems)


def take_work(layout: Layout, records: Records, store: VersionStore, root_fd: int, calls: list[dict], context: dict,
              notes: list[str]) -> None:
    _take_work(layout, records, store, root_fd, calls, context, notes)


def restore_work(layout: Layout, records: Records, store: VersionStore, root_fd: int, context: dict) -> str:
    return _restore_work(layout, records, store, root_fd, context)


def end_interrupted(records: Records, call_id: str) -> None:
    _end_interrupted(records, call_id)
