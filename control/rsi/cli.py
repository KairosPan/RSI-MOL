"""Command line of the control program: init, check, task, meta, status, rollback, recover.

Every command but `status` holds the lock (rsi.init.locked), so two never run at once. Exit codes: 0 done, 2 usage,
3 refused (a Refused, or a request that fails the checks made before anything runs: an unknown task, a bad or
missing version), 1 any other failure, shown as one short line without a traceback. Text for the owner is English
and short, with everything from records shown through safe(); `status --json` is the machine-readable form. Statuses
and outcomes are shown as their record values (finished, timed_out, no_change, ...).
"""
import json
import os
import re
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

from rsi.check import CHECK_FILE, check_state, run_check
from rsi.init import init, locked, recover
from rsi.labrun import noting_refusals, run_task, unrecovered
from rsi.limits import usage_by_class
from rsi.meta import RELEASE_FILE, rollback, run_round
from rsi.paths import Layout, Refused, find_layout
from rsi.programs import clean_path, inside
from rsi.records import ENDED, UNREADABLE, Records
from rsi.tasks import load_task
from rsi.versions import VERSION, VersionStore
from rsi.workspace import raise_open_files_limit

SEED = Path(__file__).resolve().parents[1] / 'seed'
OWNER = 'owner'                                 # the author of a rollback made from the command line
ROUND_ID = re.compile(r'M[0-9]{4,}')
APPLICATION_FILE = re.compile(r'A([0-9]{4,})\.json')
CALL_KEYS = ('id', 'role', 'purpose', 'task', 'status', 'check')
RELEASE_KEYS = ('version', 'kind', 'parent', 'target', 'round', 'author', 'time', 'evidence', 'benefit')
APPLICATION_KEYS = ('id', 'round', 'call', 'what', 'why', 'records')
YES_NO = {True: 'yes', False: 'no'}
# The messages of a check script that only repeat its verdict, so they are not shown again: the English ones, and the
# Chinese ones (written as escapes) that the check scripts printed before the program spoke English, which the
# records of a root made then still hold.
CHECK_VERDICTS = ('passed', 'not passed', '\u901a\u8fc7', '\u6ca1\u901a\u8fc7')
SHOWN_PROBLEMS = 5
SHOWN_CHARS = 300                               # characters of one value from records or an agent
ID_CHARS = 80                                   # characters of an id, a version or a status
ERROR_CHARS = 2000                              # characters of a refusal or error message
INTERRUPTED = ('Interrupted. Run rsi recover first: it stops the calls that have not ended, moves what the '
               'interruption left to sealed/, and puts the newest version back into work/. Until then rsi task, meta '
               'and rollback do not run.')
# Shown escaped in text mode: C0 and C1 controls (ESC, CR, LF), DEL, bidirectional controls, line and paragraph
# separators, lone surrogates.
UNSAFE = re.compile('[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069\ud800-\udfff]')

USAGE = """Usage: rsi <command>

  init                 make the folders and the version store, write the two Codex configurations, and check
                       whether the two Codex homes are signed in
  check                test the isolation requirements one by one; no model is called
  task <id>            run a task, then a Meta round; with --no-meta, only the task
  meta                 run a Meta round on the task runs that no round has looked at (none: no round)
  status [--json]      the current version, recent runs and releases, pending applications, costs by class
  rollback <version>   go back to an earlier version
  recover              after the control program was interrupted: stop the calls that have not ended and mark
                       them interrupted, move what the interruption left to sealed/, and put the newest version
                       back into work/; until then task, meta and rollback do not run"""


class Usage(Exception):
    """A command line that is not one of USAGE's."""


class Invalid(Exception):
    """A request that failed the checks made before anything runs (exit 3)."""


@contextmanager
def _validating():
    """Inside: a ValueError is a bad request (Invalid). Only the checks of a request run inside, never a call."""
    try:
        yield
    except ValueError as error:
        raise Invalid(str(error)) from None


def _parse(args: list[str]) -> tuple[str, dict]:
    if not args:
        raise Usage('')
    command, rest = args[0], args[1:]
    if command in ('init', 'check', 'meta', 'recover') and not rest:
        return command, {}
    if command == 'status' and rest in ([], ['--json']):
        return command, {'json': bool(rest)}
    if command == 'rollback' and len(rest) == 1:
        return command, {'version': rest[0]}
    if command == 'task':
        flags = [arg for arg in rest if arg == '--no-meta']
        names = [arg for arg in rest if arg != '--no-meta']
        if len(names) == 1 and len(flags) <= 1 and not names[0].startswith('-'):
            return command, {'task': names[0], 'meta': not flags}
    if command in ('help', '-h', '--help') and not rest:
        return 'help', {}
    raise Usage(f'Unknown command: rsi {" ".join(args)}')


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


@contextmanager
def _interruptible():
    """While inside, SIGHUP (the terminal closed) and SIGTERM end rsi the way Ctrl-C does: a KeyboardInterrupt in the
    main thread, so a running Codex is stopped (SIGTERM, then SIGKILL) and its call finished as interrupted. The
    handlers from before are put back afterwards. Outside the main thread nothing is changed."""
    previous = {}
    try:
        for number in (signal.SIGHUP, signal.SIGTERM):
            previous[number] = signal.signal(number, _interrupt)
    except ValueError:
        pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str] | None = None) -> int:
    raise_open_files_limit()        # macOS starts programs with 256 open files
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        command, options = _parse(args)
    except Usage as error:
        if str(error):
            _error(error)
        print(f'\n{USAGE}' if str(error) else USAGE, file=sys.stderr)
        return 2
    if command == 'help':
        print(USAGE)
        return 0
    layout = find_layout()
    if 'PATH' in os.environ:                        # what every program rsi starts inherits: nothing agents write
        os.environ['PATH'] = clean_path(layout.root)
    with _interruptible():
        try:
            _own_python(layout)
            if command == 'status':
                return _status(layout, options['json'])
            with locked(layout):
                return COMMANDS[command](layout, **options)
        except (Refused, Invalid) as error:
            _error(error)
            return 3
        except KeyboardInterrupt:
            try:
                _error(INTERRUPTED)
            except (OSError, KeyboardInterrupt):        # the terminal may be gone (SIGHUP)
                pass
            return 1
        except Exception as error:
            _error(f'Error: {type(error).__name__}: {error}')
            return 1


def _own_python(layout: Layout) -> None:
    """Refused when the Python running the control program lies inside the RSI root, which agents partly write (the
    launcher never picks such a one; this also covers starting the program some other way)."""
    for path in (sys.executable, sys.prefix, sys.base_prefix):
        if not path or inside(path, str(layout.root)):
            raise Refused(f'The Python that runs rsi ({path!r}) is inside the RSI folder (or unknown), where agents '
                          'can change it. Use a Python outside the RSI folder (./rsi picks one itself, or name one '
                          'with $RSI_PYTHON).')


# What the commands print. Everything that comes from records or from an agent (ids, task names, check messages,
# problem lines, application text, version authors, reasons) is shown through safe(), and every printed line passes
# through it once more, so no control character reaches the owner's terminal.

def safe(value, limit: int | None = SHOWN_CHARS) -> str:
    """value as text that is safe to print on a terminal: every C0 and C1 control character (ESC, CR and newline
    included), DEL, every bidirectional-control or line-separator character and every lone surrogate is shown
    escaped (ESC as \\x1b, U+202E as \\u202e), and the text is cut to `limit` characters (None: not cut)."""
    def escape(match: re.Match) -> str:
        code = ord(match.group())
        return f'\\x{code:02x}' if code < 0x100 else f'\\u{code:04x}'
    text = UNSAFE.sub(escape, value if isinstance(value, str) else str(value))
    return text if limit is None or len(text) <= limit else text[:limit - 1] + '…'


def _print(lines: list[str], stream=None) -> None:
    print('\n'.join(safe(line, None) for line in lines), file=stream or sys.stdout)


def _error(text) -> None:
    _print([safe(text, ERROR_CHARS)], sys.stderr)


def _ids(items) -> str:
    return ', '.join(safe(item, ID_CHARS) for item in items) if isinstance(items, list) and items else 'none'


def _shown(layout: Layout, path: Path) -> str:
    """A path inside the root as the owner types it there, e.g. records/runs/R0001."""
    try:
        return path.relative_to(layout.root).as_posix()
    except ValueError:
        return str(path)


def _run_folder(layout: Layout, call_id) -> str:
    return f'{_shown(layout, layout.records / "runs")}/{safe(call_id, ID_CHARS)}'


def _what(call: dict) -> str:
    role, purpose = call.get('role'), call.get('purpose')
    in_round = f', round {safe(call["round"], ID_CHARS)}' if call.get('round') else ''  # status's entries carry none
    if role == 'meta':
        return f'Meta{in_round}'
    if purpose == 'task':
        return f'Lab task {safe(call.get("task"), ID_CHARS)}'
    if purpose == 'trial':
        task = call.get('task')
        return f'Lab trial ({"task " + safe(task, ID_CHARS) if task else "a test text"}){in_round}'
    return f'{safe(role, ID_CHARS)} {safe(purpose, ID_CHARS)}'


def _check_words(check) -> str:
    if not isinstance(check, dict):
        return ''
    word = 'check passed' if check.get('passed') is True else 'check not passed'
    message = check.get('message')
    if isinstance(message, str) and message and message not in CHECK_VERDICTS:
        return f'{word}: {safe(message)}'
    return word


def _call_head(call: dict) -> str:
    state = call.get('status')
    if state == UNREADABLE:                     # calls()' stand-in for a call.json that cannot be read
        return f'{safe(call.get("id"), ID_CHARS)}: the record cannot be read ({safe(call.get("problem"))})'
    text = f'{safe(call.get("id"), ID_CHARS)} ({_what(call)}): {safe(state, ID_CHARS)}'
    check = _check_words(call.get('check'))
    return f'{text}, {check}' if check else text


def _problem_lines(problems, where: str) -> list[str]:
    if not isinstance(problems, list) or not problems:
        return []
    lines = [f'  problem: {safe(line)}' for line in problems[:SHOWN_PROBLEMS]]
    if len(problems) > SHOWN_PROBLEMS:
        lines.append(f'  {len(problems) - SHOWN_PROBLEMS} more problems, see {where}')
    return lines


def _call_lines(layout: Layout, call: dict) -> list[str]:
    folder = _run_folder(layout, call.get('id'))
    lines = [_call_head(call), f'  results and record: {folder}/ (the results are in out/)']
    if call.get('purpose') == 'task':
        before, after = safe(call.get('version_before'), ID_CHARS), call.get('version_after')
        if after:
            lines.append(f'  version: {before} -> {safe(after, ID_CHARS)}')
        elif call.get('commit_failed'):
            lines.append(f'  version: still {before}; the changes of this run could not be saved as a version (see the '
                         'problems)')
        else:
            lines.append(f'  version: still {before} (the workspace did not change)')
    return lines + _problem_lines(call.get('problems'), f'{folder}/call.json')


def _round_lines(layout: Layout, record: dict) -> list[str]:
    outcome = record.get('outcome')
    head = f'Meta round {safe(record.get("id"), ID_CHARS)}: {safe(outcome, ID_CHARS)}'
    if record.get('version_after'):
        head += f' {safe(record["version_after"], ID_CHARS)}'
    if record.get('reason'):
        head += f' ({safe(record["reason"])})'
    lines = [head, f'  Meta calls: {_ids(record.get("calls"))}']
    trials = record.get('trials') if isinstance(record.get('trials'), list) else []
    trials = [trial.get('id') for trial in trials if isinstance(trial, dict)]
    if trials:
        lines.append(f'  trials: {_ids(trials)}')
    if record.get('applications'):
        lines.append(f'  new applications: {_ids(record["applications"])}')
    where = f'{_shown(layout, layout.records / "rounds")}/{safe(record.get("id"), ID_CHARS)}/round.json'
    lines.append(f'  record: {where}')
    return lines + _problem_lines(record.get('problems'), where)


def _applications(items: list[dict]) -> str:
    return '; '.join(f'{safe(item.get("id"), ID_CHARS)} {safe(item.get("what"))}' for item in items) or 'none'


def _footer(layout: Layout) -> list[str]:
    state = status(layout)
    lines = [f'Current version: {safe(state["version"], ID_CHARS)}']
    if state['applications']:
        lines.append(f'Pending applications: {_applications(state["applications"])}')
    return lines


# status

def _json_file(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


def _numbered(folder: Path, pattern: re.Pattern) -> list[str]:
    """The names in folder that match pattern (its group 1 is a number, or the name is V<number>.json), lowest
    number first."""
    try:
        names = os.listdir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = [name for name in names if pattern.fullmatch(name)]
    return sorted(found, key=lambda name: int(re.search(r'[0-9]+', name).group()))


def _releases(records: Path, count: int) -> list[dict]:
    out = []
    for name in reversed(_numbered(records / 'releases', RELEASE_FILE)):
        data = _json_file(records / 'releases' / name)
        out.append({key: data.get(key) for key in RELEASE_KEYS} if isinstance(data, dict)
                   else {'version': name.removesuffix('.json'), 'problem': 'the record cannot be read'})
        if len(out) == count:
            break
    return out


def _pending(records: Path) -> list[dict]:
    out = []
    for name in _numbered(records / 'applications', APPLICATION_FILE):
        data = _json_file(records / 'applications' / name)
        if isinstance(data, dict) and data.get('status') == 'pending':
            out.append({key: data.get(key) for key in APPLICATION_KEYS})
    return out


def _state(layout: Layout) -> tuple[dict, list[dict]]:
    """What `status --json` prints, and the newest five call records whole (for the text form)."""
    calls = Records(layout.records).calls() if layout.records.is_dir() else []
    recent = list(reversed(calls[-5:]))
    store = VersionStore(layout.versions)
    log = store.log() if (store.git_dir / 'HEAD').is_file() else []
    return {'version': log[0]['version'] if log else None, 'versions': log[:5],
            'calls': [{key: call.get(key) for key in CALL_KEYS} for call in recent],
            'releases': _releases(layout.records, 3), 'applications': _pending(layout.records),
            'usage': usage_by_class(calls), 'check': check_state(layout)}, recent


def status(layout: Layout) -> dict:
    """What `status --json` prints. Reads records/ and the version store only, plus what the gate looks at (the owner
    files, the program's code, `codex --version` in a throwaway home); makes nothing in the root."""
    return _state(layout)[0]


def _status(layout: Layout, as_json: bool) -> int:
    state, recent = _state(layout)
    if as_json:
        print(json.dumps(state, ensure_ascii=True, indent=2))       # every control character comes out escaped
        return 0
    version = safe(state['version'], ID_CHARS) if state['version'] else 'none yet (run rsi init first)'
    lines = [f'Current version: {version}']
    if state['versions']:
        lines.append('Recent versions: ' + '; '.join(
            f'{safe(entry["version"], ID_CHARS)} ({safe(entry["author"], ID_CHARS)})' for entry in state['versions']))
    lines.append('Recent calls:' if state['calls'] else 'Recent calls: none')
    for call in recent:
        lines.append(f'  {_call_head(call)}; record in {_run_folder(layout, call.get("id"))}/')
    if state['releases']:
        lines.append('Recent releases and rollbacks: ' + '; '.join(
            f'{safe(item.get("version"), ID_CHARS)} ('
            f'{"rollback to " + safe(item.get("target"), ID_CHARS) if item.get("kind") == "rollback" else "release"}, '
            f'{safe(item.get("author"), ID_CHARS)})' for item in state['releases']))
    lines.append(f'Pending applications: {_applications(state["applications"])}')
    lines.append('Usage: ' + '; '.join(
        f'{safe(cls, ID_CHARS)} {totals["calls"]} calls, {totals["input_tokens"]} input and '
        f'{totals["output_tokens"]} output tokens'
        + (f' ({totals["unknown_usage_calls"]} calls with unknown usage)' if totals['unknown_usage_calls'] else '')
        for cls, totals in state['usage'].items()))
    check = state['check']                       # the gate's verdict, never check.json's word alone
    if check is None:
        lines.append('Isolation check: rsi check has not been run yet')
    elif check['passed']:
        lines.append(f'Isolation check: passed (Codex {safe(check["codex_version"], ID_CHARS)}, '
                     f'{safe(check["at"], ID_CHARS)})')
    else:
        lines.append(f'Isolation check: not passed: {safe(check["reason"])}')
    _print(lines)
    return 0


# The commands that hold the lock.

def _init(layout: Layout) -> int:
    report = init(layout, SEED)
    lines = [f'RSI folder: {layout.root}']
    if report['folders']:
        lines.append(f'New folders: {", ".join(report["folders"])}')
    if report['files']:
        lines.append(f'New files: {", ".join(report["files"])}')
    envelope = {'imported': f"imported from the owner's original Codex configuration (backed up at "
                            f'{safe(report["envelope_source"])})',
                'default': 'written with the defaults (there is no usable backup of the original Codex configuration)',
                'kept': 'already there, not changed'}
    lines.append(f'What the Lab may reach outside, owner/lab-envelope.toml: {envelope[report["envelope"]]}')
    first = f'; {report["first_version"]} was made now from the seed files' if report['first_version'] else ''
    lines.append(f'Current version: {report["version"]}{first}')
    lines.append({'staged': 'work/: replaced by the current version', 'kept': 'work/: already there, left as it is'}
                 [report['work']])
    lines.append('env/: ' + {'created': 'a new Python environment was made', 'kept': 'already there, left as it is',
                             'skipped': 'not made (see the problems)', 'failed': 'could not be made (see the problems)'}
                 [report['env']])
    lines.append('Codex configuration codex-home/config.toml: written anew')
    original = report['original']
    if original is not None and original['dropped']:
        lines.append(f'  these top-level keys of the original configuration ({safe(original["path"])}) were not '
                     f'carried into the new config.toml: {_ids(original["dropped"])} (approval_policy, features, apps '
                     'and plugins follow owner/lab-envelope.toml)')
    for home, state in report['signed_in'].items():
        said = 'signed in' if state else 'not signed in' if state is False else 'the sign-in state cannot be told'
        lines.append(f'{home}: {said}'
                     + ('' if state else f' (to sign in: CODEX_HOME={layout.root / home} codex login)'))
    lines += [f'Problem: {safe(line)}' for line in report['problems']]
    _print(lines)
    return 0


def _check(layout: Layout) -> int:
    report = run_check(layout)
    if report['passed']:
        lines = [f'All passed (Codex {safe(report["codex_version"], ID_CHARS)}, {len(report["results"])} cases)']
    else:
        lines = [f'{safe(r["profile"], ID_CHARS)} {safe(r["name"], ID_CHARS)}: expected '
                 f'{safe(r["expected"], ID_CHARS)}, observed {safe(r["observed"], ID_CHARS)} ({safe(r["detail"])})'
                 for r in report['results'] if not r['ok']]
        lines += [f'Problem: {safe(line)}' for line in report['problems']]
    lines += [f'Note: {safe(line)}' for line in report['notes']]
    lines += [f'Information (not a case): {safe(item["profile"], ID_CHARS)} {safe(item["name"], ID_CHARS)} = '
              f'{YES_NO.get(item["value"], "unknown")} ({safe(item["detail"])})' for item in report['info']]
    lines.append(f'Record: {_shown(layout, layout.records / CHECK_FILE)}')
    _print(lines)
    return 0 if report['passed'] else 1


def _task(layout: Layout, task: str, meta: bool) -> int:
    with _validating():
        load_task(layout.tasks, task)           # an unknown task or a bad id: refused before anything runs
    call = run_task(layout, task)
    _print(_call_lines(layout, call))
    if meta:                                    # the task run is done: its exit code is 0 whatever the round does
        try:
            record = run_round(layout, [call['id']])
        except Refused as error:
            _print([f'No Meta round was run after this task run: {safe(error, ERROR_CHARS)}'])
        except Exception as error:
            _print([f'The Meta round after this task run could not be completed: {type(error).__name__}: '
                    f'{safe(error, ERROR_CHARS)}'])
        else:
            _print(_round_lines(layout, record))
    _print(_footer(layout))
    return 0


def untriggered(records: Records) -> list[str]:
    """The ids of the Lab task calls that ended and that no round has had as a trigger (a round records its
    trigger in rounds/<id>/round.json; a round that left no readable record has had none)."""
    triggered = set()
    for name in _numbered(records.root / 'rounds', ROUND_ID):
        data = _json_file(records.root / 'rounds' / name / 'round.json')
        if isinstance(data, dict) and isinstance(data.get('trigger'), list):
            triggered.update(item for item in data['trigger'] if isinstance(item, str))
    return [call['id'] for call in records.calls()
            if call.get('role') == 'lab' and call.get('purpose') == 'task' and call.get('status') in ENDED
            and isinstance(call.get('id'), str) and call['id'] not in triggered]


def _meta(layout: Layout) -> int:
    records = Records(layout.records)
    with noting_refusals(records, {'purpose': 'meta'}):
        unrecovered(layout, records)                    # before deciding whether there is anything to look at
    trigger = untriggered(records)
    if not trigger:
        _print(['There are no new task runs; Meta was not started.'])
        return 0
    _print([f'Task runs looked at in this round: {_ids(trigger)}'])
    _print(_round_lines(layout, run_round(layout, trigger)))
    _print(_footer(layout))
    return 0


def _rollback(layout: Layout, version: str) -> int:
    with _validating():
        if not VERSION.fullmatch(version):
            raise ValueError(f'{version} is not a version name. A version name looks like V0001.')
        VersionStore(layout.versions).resolve(version)          # a version the store lacks
        # rollback's ValueErrors are all about the request (its documented contract), the last one being a
        # version whose content is already the current one.
        new = rollback(layout, version, OWNER)
    new, version = safe(new, ID_CHARS), safe(version, ID_CHARS)
    _print([f'Rolled back: the new version {new} has the content of {version}, and work/ now holds it.',
            f'Record: {_shown(layout, layout.records / "releases")}/{new}.json'])
    return 0


def _recover(layout: Layout) -> int:
    moved = []
    ids = recover(layout, moved)
    lines = [f'Calls marked interrupted: {_ids(ids)}' if ids else 'There are no calls that have not ended.']
    lines += [f'  {safe(line)}' for line in moved]
    _print(lines)
    return 0


COMMANDS = {'init': _init, 'check': _check, 'task': _task, 'meta': _meta, 'rollback': _rollback, 'recover': _recover}
