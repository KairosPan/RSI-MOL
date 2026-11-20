"""Command line of the control program: init, check, task, meta, status, rollback, recover.

Every command but `status` holds the lock (rsi.init.locked), so two never run at once. Exit codes: 0 done, 2 usage,
3 refused (a Refused, or a request that fails the checks made before anything runs: an unknown task, a bad or
missing version), 1 any other failure, shown as one short line without a traceback. Text for the owner is Chinese
and short, with everything from records shown through safe(); `status --json` is the machine-readable form.
"""
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from rsi.check import CHECK_FILE, WORDS, run_check
from rsi.init import init, locked, recover
from rsi.labrun import run_task
from rsi.limits import usage_by_class
from rsi.meta import RELEASE_FILE, rollback, run_round
from rsi.paths import Layout, Refused, find_layout
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
CHECK_KEYS = ('codex_version', 'passed', 'at')
YES_NO = {True: '是', False: '否'}
STATUS_WORDS = {'started': '未结束', 'finished': '完成', 'failed': '失败', 'timed_out': '超时',
                'interrupted': '中断', 'unreadable': '记录读不出来'}
OUTCOME_WORDS = {'no_change': '不改', 'released': '发布了新版本', 'limit_reached': '到了上限',
                 'invalid_decision': '没有交回合规的决定'}
SHOWN_PROBLEMS = 5
SHOWN_CHARS = 300                               # characters of one value from records or an agent
ID_CHARS = 80                                   # characters of an id, a version or a status
ERROR_CHARS = 2000                              # characters of a refusal or error message
# Shown escaped in text mode: C0 and C1 controls (ESC, CR, LF), DEL, bidirectional controls, line and paragraph
# separators, lone surrogates.
UNSAFE = re.compile('[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069\ud800-\udfff]')

USAGE = """用法：rsi <子命令>

  init                 建目录和版本库，生成两份 Codex 配置，检查两个 Codex 目录是否已登录
  check                逐项测隔离要求，不调用模型
  task <编号>          跑一次任务，然后跑一轮 Meta；加 --no-meta 只跑任务
  meta                 跑一轮 Meta，看还没有被任何一轮看过的任务运行（没有就不跑）
  status [--json]      当前版本、最近的运行和发布、待处理的申请、各类成本合计
  rollback <版本>      退回到某个版本
  recover              控制程序被打断后，把没有结束的调用标成“中断”，把中断留下的东西移到 sealed/"""


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
    raise Usage(f'看不懂这条命令：rsi {" ".join(args)}')


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
    try:
        if command == 'status':
            return _status(layout, options['json'])
        with locked(layout):
            return COMMANDS[command](layout, **options)
    except (Refused, Invalid) as error:
        _error(error)
        return 3
    except KeyboardInterrupt:
        _error('被打断了。如果 rsi status 里还有“未结束”的调用，请运行 rsi recover。')
        return 1
    except Exception as error:
        _error(f'出错了：{type(error).__name__}: {error}')
        return 1


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
    return '、'.join(safe(item, ID_CHARS) for item in items) if isinstance(items, list) and items else '无'


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
    in_round = f'，轮 {safe(call["round"], ID_CHARS)}' if call.get('round') else ''   # status's entries carry none
    if role == 'meta':
        return f'Meta{in_round}'
    if purpose == 'task':
        return f'Lab 任务 {safe(call.get("task"), ID_CHARS)}'
    if purpose == 'trial':
        task = call.get('task')
        return f'Lab 试跑（{"任务 " + safe(task, ID_CHARS) if task else "测试文字"}）{in_round}'
    return f'{safe(role, ID_CHARS)} {safe(purpose, ID_CHARS)}'


def _check_words(check) -> str:
    if not isinstance(check, dict):
        return ''
    word = '检查通过' if check.get('passed') is True else '检查没通过'
    message = check.get('message')
    if isinstance(message, str) and message and message not in ('通过', '没通过'):
        return f'{word}：{safe(message)}'
    return word


def _word(words: dict, value) -> str:
    """The Chinese word for a status or outcome; the value itself (shown safely) when there is none."""
    return (words.get(value) if isinstance(value, str) else None) or safe(value, ID_CHARS)


def _call_head(call: dict) -> str:
    state = call.get('status')
    if state == UNREADABLE:                     # calls()' stand-in for a call.json that cannot be read
        return f'{safe(call.get("id"), ID_CHARS)}：记录读不出来（{safe(call.get("problem"))}）'
    text = f'{safe(call.get("id"), ID_CHARS)}（{_what(call)}）：{_word(STATUS_WORDS, state)}'
    check = _check_words(call.get('check'))
    return f'{text}，{check}' if check else text


def _problem_lines(problems, where: str) -> list[str]:
    if not isinstance(problems, list) or not problems:
        return []
    lines = [f'  问题：{safe(line)}' for line in problems[:SHOWN_PROBLEMS]]
    if len(problems) > SHOWN_PROBLEMS:
        lines.append(f'  另有 {len(problems) - SHOWN_PROBLEMS} 条问题，见 {where}')
    return lines


def _call_lines(layout: Layout, call: dict) -> list[str]:
    folder = _run_folder(layout, call.get('id'))
    lines = [_call_head(call), f'  结果和记录：{folder}/（结果在 out/）']
    if call.get('purpose') == 'task':
        before, after = safe(call.get('version_before'), ID_CHARS), call.get('version_after')
        if after:
            lines.append(f'  版本：{before} → {safe(after, ID_CHARS)}')
        elif call.get('commit_failed'):
            lines.append(f'  版本：仍是 {before}；这次的改动没能存成版本（见问题）')
        else:
            lines.append(f'  版本：仍是 {before}（工作区没有改动）')
    return lines + _problem_lines(call.get('problems'), f'{folder}/call.json')


def _round_lines(layout: Layout, record: dict) -> list[str]:
    outcome = record.get('outcome')
    head = f'Meta 轮 {safe(record.get("id"), ID_CHARS)}：{_word(OUTCOME_WORDS, outcome)}'
    if record.get('version_after'):
        head += f' {safe(record["version_after"], ID_CHARS)}'
    if record.get('reason'):
        head += f'（{safe(record["reason"])}）'
    lines = [head, f'  Meta 调用：{_ids(record.get("calls"))}']
    trials = record.get('trials') if isinstance(record.get('trials'), list) else []
    trials = [trial.get('id') for trial in trials if isinstance(trial, dict)]
    if trials:
        lines.append(f'  试跑：{_ids(trials)}')
    if record.get('applications'):
        lines.append(f'  新的申请：{_ids(record["applications"])}')
    where = f'{_shown(layout, layout.records / "rounds")}/{safe(record.get("id"), ID_CHARS)}/round.json'
    lines.append(f'  记录：{where}')
    return lines + _problem_lines(record.get('problems'), where)


def _applications(items: list[dict]) -> str:
    return '；'.join(f'{safe(item.get("id"), ID_CHARS)} {safe(item.get("what"))}' for item in items) or '无'


def _footer(layout: Layout) -> list[str]:
    state = status(layout)
    lines = [f'当前版本：{safe(state["version"], ID_CHARS)}']
    if state['applications']:
        lines.append(f'待处理的申请：{_applications(state["applications"])}')
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
                   else {'version': name.removesuffix('.json'), 'problem': '记录读不出来'})
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


def _check_state(records: Path) -> dict | None:
    """The last `rsi check` as records/check.json tells it; None when there is none, not passed when unreadable."""
    if not os.path.lexists(records / CHECK_FILE):
        return None
    data = _json_file(records / CHECK_FILE)
    if not isinstance(data, dict):
        data = {'passed': False}
    return {key: data.get(key) for key in CHECK_KEYS}


def _state(layout: Layout) -> tuple[dict, list[dict]]:
    """What `status --json` prints, and the newest five call records whole (for the text form)."""
    calls = Records(layout.records).calls() if layout.records.is_dir() else []
    recent = list(reversed(calls[-5:]))
    store = VersionStore(layout.versions)
    log = store.log() if (store.git_dir / 'HEAD').is_file() else []
    return {'version': log[0]['version'] if log else None, 'versions': log[:5],
            'calls': [{key: call.get(key) for key in CALL_KEYS} for call in recent],
            'releases': _releases(layout.records, 3), 'applications': _pending(layout.records),
            'usage': usage_by_class(calls), 'check': _check_state(layout.records)}, recent


def status(layout: Layout) -> dict:
    """What `status --json` prints. Reads records/ and the version store only; makes nothing."""
    return _state(layout)[0]


def _status(layout: Layout, as_json: bool) -> int:
    state, recent = _state(layout)
    if as_json:
        print(json.dumps(state, ensure_ascii=True, indent=2))       # every control character comes out escaped
        return 0
    lines = [f'当前版本：{safe(state["version"], ID_CHARS) if state["version"] else "还没有（请先运行 rsi init）"}']
    if state['versions']:
        lines.append('最近的版本：' + '；'.join(f'{safe(entry["version"], ID_CHARS)}（{safe(entry["author"], ID_CHARS)}）'
                                          for entry in state['versions']))
    lines.append('最近的调用：' if state['calls'] else '最近的调用：无')
    for call in recent:
        lines.append(f'  {_call_head(call)}；记录在 {_run_folder(layout, call.get("id"))}/')
    if state['releases']:
        lines.append('最近的发布和回退：' + '；'.join(
            f'{safe(item.get("version"), ID_CHARS)}（'
            f'{"回退到 " + safe(item.get("target"), ID_CHARS) if item.get("kind") == "rollback" else "发布"}，'
            f'{safe(item.get("author"), ID_CHARS)}）' for item in state['releases']))
    lines.append(f'待处理的申请：{_applications(state["applications"])}')
    lines.append('用量：' + '；'.join(
        f'{safe(cls, ID_CHARS)} {totals["calls"]} 次，输入 {totals["input_tokens"]}、输出 {totals["output_tokens"]} token'
        + (f'（{totals["unknown_usage_calls"]} 次用量不明）' if totals['unknown_usage_calls'] else '')
        for cls, totals in state['usage'].items()))
    check = state['check']
    lines.append('隔离检查：' + ('还没有运行 rsi check' if check is None else
                                f'Codex {safe(check.get("codex_version"), ID_CHARS)}，'
                                f'{"通过" if check.get("passed") is True else "没通过"}'
                                + (f'（{safe(check["at"], ID_CHARS)}）' if check.get('at') else '')))
    _print(lines)
    return 0


# The commands that hold the lock.

def _init(layout: Layout) -> int:
    report = init(layout, SEED)
    lines = [f'RSI 文件夹：{layout.root}']
    if report['folders']:
        lines.append(f'新建的文件夹：{"、".join(report["folders"])}')
    if report['files']:
        lines.append(f'新写的文件：{"、".join(report["files"])}')
    envelope = {'imported': f'从负责人原来的 Codex 配置导入（备份在 {safe(report["envelope_source"])}）',
                'default': '按默认值新写（没有可用的原 Codex 配置备份）', 'kept': '已有，没有改'}
    lines.append(f'Lab 的对外范围 owner/lab-envelope.toml：{envelope[report["envelope"]]}')
    first = f'，其中 {report["first_version"]} 是这次用种子文件建的' if report['first_version'] else ''
    lines.append(f'当前版本：{report["version"]}{first}')
    lines.append({'staged': 'work/：换成了当前版本', 'kept': 'work/：已有，没有动'}[report['work']])
    lines.append('env/：' + {'created': '新建了 Python 环境', 'kept': '已有，没有动', 'skipped': '没有建（见问题）',
                            'failed': '没建好（见问题）'}[report['env']])
    lines.append('Codex 配置 codex-home/config.toml：已重新生成')
    original = report['original']
    if original is not None and original['dropped']:
        lines.append(f'  原来的配置（{safe(original["path"])}）里这些顶层键没有带进新的 config.toml：'
                     f'{_ids(original["dropped"])}（approval_policy、features、apps、plugins 以 owner/lab-envelope.toml 为准）')
    for home, state in report['signed_in'].items():
        lines.append(f'{home}：{"已登录" if state else "没有登录" if state is False else "查不了登录状态"}'
                     + ('' if state else f'（登录：CODEX_HOME={layout.root / home} codex login）'))
    lines += [f'问题：{safe(line)}' for line in report['problems']]
    _print(lines)
    return 0


def _check(layout: Layout) -> int:
    report = run_check(layout)
    if report['passed']:
        lines = [f'全部通过（Codex {safe(report["codex_version"], ID_CHARS)}，共 {len(report["results"])} 项）']
    else:
        lines = [f'{safe(r["profile"], ID_CHARS)} {safe(r["name"], ID_CHARS)}：应当{WORDS.get(r["expected"])}，'
                 f'实际{WORDS.get(r["observed"], safe(r["observed"], ID_CHARS))}（{safe(r["detail"])}）'
                 for r in report['results'] if not r['ok']]
        lines += [f'问题：{safe(line)}' for line in report['problems']]
    lines += [f'说明：{safe(line)}' for line in report['notes']]
    lines += [f'信息（不算检查项）：{safe(item["profile"], ID_CHARS)} {safe(item["name"], ID_CHARS)} = '
              f'{YES_NO.get(item["value"], "不明")}（{safe(item["detail"])}）' for item in report['info']]
    lines.append(f'记录：{_shown(layout, layout.records / CHECK_FILE)}')
    _print(lines)
    return 0 if report['passed'] else 1


def _task(layout: Layout, task: str, meta: bool) -> int:
    with _validating():
        load_task(layout.tasks, task)           # an unknown task or a bad id: refused before anything runs
    call = run_task(layout, task)
    _print(_call_lines(layout, call))
    if meta:
        _print(_round_lines(layout, run_round(layout, [call['id']])))
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
    trigger = untriggered(Records(layout.records))
    if not trigger:
        _print(['没有新的任务运行，没有启动 Meta。'])
        return 0
    _print([f'这一轮看的任务运行：{_ids(trigger)}'])
    _print(_round_lines(layout, run_round(layout, trigger)))
    _print(_footer(layout))
    return 0


def _rollback(layout: Layout, version: str) -> int:
    with _validating():
        if not VERSION.fullmatch(version):
            raise ValueError(f'不是版本名：{version}。版本名形如 V0001。')
        VersionStore(layout.versions).resolve(version)          # a version the store lacks
        # rollback's ValueErrors are all about the request (its documented contract), the last one being a
        # version whose content is already the current one.
        new = rollback(layout, version, OWNER)
    new, version = safe(new, ID_CHARS), safe(version, ID_CHARS)
    _print([f'已回退：新版本 {new} 的内容和 {version} 相同，work/ 已换成它。',
            f'记录：{_shown(layout, layout.records / "releases")}/{new}.json'])
    return 0


def _recover(layout: Layout) -> int:
    moved = []
    ids = recover(layout, moved)
    lines = [f'标成“中断”的调用：{_ids(ids)}' if ids else '没有未结束的调用。']
    lines += [f'  {safe(line)}' for line in moved]
    _print(lines)
    return 0


COMMANDS = {'init': _init, 'check': _check, 'task': _task, 'meta': _meta, 'rollback': _rollback, 'recover': _recover}
