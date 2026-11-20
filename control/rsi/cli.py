"""Command line of the control program: init, check, task, meta, status, rollback, recover.

Every command but `status` holds the lock (rsi.init.locked), so two never run at once. Exit codes: 0 done, 2 usage,
3 refused (a Refused, or a ValueError raised while the request is validated: an unknown task, a bad version name),
1 any other failure. Text for the owner is Chinese and short; `status --json` is the machine-readable form.
"""
import json
import os
import re
import sys
from pathlib import Path

from rsi.init import init, locked, recover
from rsi.labrun import run_task
from rsi.limits import usage_by_class
from rsi.meta import RELEASE_FILE, rollback, run_round
from rsi.paths import Layout, Refused, find_layout
from rsi.records import ENDED, Records
from rsi.versions import VersionStore
from rsi.workspace import raise_open_files_limit

SEED = Path(__file__).resolve().parents[1] / 'seed'
OWNER = 'owner'                                 # the author of a rollback made from the command line
ROUND_ID = re.compile(r'M[0-9]{4,}')
APPLICATION_FILE = re.compile(r'A([0-9]{4,})\.json')
CALL_KEYS = ('id', 'role', 'purpose', 'task', 'status', 'check')
RELEASE_KEYS = ('version', 'kind', 'parent', 'target', 'round', 'author', 'time', 'evidence', 'benefit')
APPLICATION_KEYS = ('id', 'round', 'call', 'what', 'why', 'records')
STATUS_WORDS = {'started': '未结束', 'finished': '完成', 'failed': '失败', 'timed_out': '超时',
                'interrupted': '中断', 'unreadable': '记录读不出来'}
OUTCOME_WORDS = {'no_change': '不改', 'released': '发布了新版本', 'limit_reached': '到了上限',
                 'invalid_decision': '没有交回合规的决定'}
SHOWN_PROBLEMS = 5

USAGE = """用法：rsi <子命令>

  init                 建目录和版本库，生成两份 Codex 配置，检查两个 Codex 目录是否已登录
  check                逐项测隔离要求，不调用模型
  task <编号>          跑一次任务，然后跑一轮 Meta；加 --no-meta 只跑任务
  meta                 单独跑一轮 Meta，看还没有被任何一轮看过的任务运行
  status [--json]      当前版本、最近的运行和发布、待处理的申请、各类成本合计
  rollback <版本>      退回到某个版本
  recover              控制程序被打断后，把没有结束的调用标成“中断”，把中断留下的东西移到 sealed/"""


class Usage(Exception):
    """A command line that is not one of USAGE's."""


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
        print(f'{error}\n\n{USAGE}' if str(error) else USAGE, file=sys.stderr)
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
    except (Refused, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print('被打断了。如果 rsi status 里还有“未结束”的调用，请运行 rsi recover。', file=sys.stderr)
        return 1
    except Exception as error:
        print(f'出错了：{type(error).__name__}: {error}', file=sys.stderr)
        return 1


# What the commands print.

def _shown(layout: Layout, path: Path) -> str:
    """A path inside the root as the owner types it there, e.g. records/runs/R0001."""
    try:
        return path.relative_to(layout.root).as_posix()
    except ValueError:
        return str(path)


def _what(call: dict) -> str:
    role, purpose = call.get('role'), call.get('purpose')
    in_round = f'，轮 {call["round"]}' if call.get('round') else ''        # status's entries carry no round
    if role == 'meta':
        return f'Meta{in_round}'
    if purpose == 'task':
        return f'Lab 任务 {call.get("task")}'
    if purpose == 'trial':
        return f'Lab 试跑（{"任务 " + str(call["task"]) if call.get("task") else "测试文字"}）{in_round}'
    return f'{role} {purpose}'


def _check_words(check) -> str:
    if not isinstance(check, dict):
        return ''
    word = '检查通过' if check.get('passed') is True else '检查没通过'
    message = check.get('message')
    return f'{word}：{message}' if isinstance(message, str) and message and message not in ('通过', '没通过') else word


def _call_head(call: dict) -> str:
    text = f'{call.get("id")}（{_what(call)}）：{STATUS_WORDS.get(call.get("status"), call.get("status"))}'
    check = _check_words(call.get('check'))
    return f'{text}，{check}' if check else text


def _problem_lines(problems, where: str) -> list[str]:
    if not isinstance(problems, list) or not problems:
        return []
    lines = [f'  问题：{line}' for line in problems[:SHOWN_PROBLEMS]]
    if len(problems) > SHOWN_PROBLEMS:
        lines.append(f'  另有 {len(problems) - SHOWN_PROBLEMS} 条问题，见 {where}')
    return lines


def _call_lines(layout: Layout, call: dict) -> list[str]:
    folder = _shown(layout, layout.records / 'runs' / call['id'])
    lines = [_call_head(call), f'  结果和记录：{folder}/（结果在 out/）']
    if call.get('purpose') == 'task':
        before, after = call.get('version_before'), call.get('version_after')
        if after:
            lines.append(f'  版本：{before} → {after}')
        elif call.get('commit_failed'):
            lines.append(f'  版本：仍是 {before}；这次的改动没能存成版本（见问题）')
        else:
            lines.append(f'  版本：仍是 {before}（工作区没有改动）')
    return lines + _problem_lines(call.get('problems'), f'{folder}/call.json')


def _round_lines(layout: Layout, record: dict) -> list[str]:
    head = f'Meta 轮 {record["id"]}：{OUTCOME_WORDS.get(record.get("outcome"), record.get("outcome"))}'
    if record.get('version_after'):
        head += f' {record["version_after"]}'
    if record.get('reason'):
        head += f'（{record["reason"]}）'
    lines = [head, f'  Meta 调用：{"、".join(record.get("calls") or []) or "无"}']
    trials = [trial.get('id') for trial in record.get('trials') or []]
    if trials:
        lines.append(f'  试跑：{"、".join(trials)}')
    if record.get('applications'):
        lines.append(f'  新的申请：{"、".join(record["applications"])}')
    where = f'{_shown(layout, layout.records / "rounds" / record["id"])}/round.json'
    lines.append(f'  记录：{where}')
    return lines + _problem_lines(record.get('problems'), where)


def _footer(layout: Layout) -> list[str]:
    state = status(layout)
    lines = [f'当前版本：{state["version"]}']
    if state['applications']:
        lines.append('待处理的申请：' + '；'.join(f'{item["id"]} {item["what"]}' for item in state['applications']))
    return lines


def _print(lines: list[str]) -> None:
    print('\n'.join(lines))


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


def status(layout: Layout) -> dict:
    """What `status --json` prints. Reads records/ and the version store only; makes nothing."""
    calls = Records(layout.records).calls() if layout.records.is_dir() else []
    store = VersionStore(layout.versions)
    log = store.log() if (store.git_dir / 'HEAD').is_file() else []
    return {'version': log[0]['version'] if log else None, 'versions': log[:5],
            'calls': [{key: call.get(key) for key in CALL_KEYS} for call in reversed(calls[-5:])],
            'releases': _releases(layout.records, 3), 'applications': _pending(layout.records),
            'usage': usage_by_class(calls),
            'check': None}                      # rsi check (a later task) fills this in


def _status(layout: Layout, as_json: bool) -> int:
    state = status(layout)
    if as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    lines = [f'当前版本：{state["version"] or "还没有（请先运行 rsi init）"}']
    if state['versions']:
        lines.append('最近的版本：' + '；'.join(f'{entry["version"]}（{entry["author"]}）' for entry in state['versions']))
    lines.append('最近的调用：' if state['calls'] else '最近的调用：无')
    for call in state['calls']:
        lines.append(f'  {_call_head(call)}；记录在 {_shown(layout, layout.records / "runs" / str(call["id"]))}/')
    if state['releases']:
        lines.append('最近的发布和回退：' + '；'.join(
            f'{item.get("version")}（{"回退到 " + str(item.get("target")) if item.get("kind") == "rollback" else "发布"}，'
            f'{item.get("author")}）' for item in state['releases']))
    lines.append('待处理的申请：' + ('；'.join(f'{item["id"]} {item["what"]}' for item in state['applications'])
                                    or '无'))
    lines.append('用量：' + '；'.join(
        f'{cls} {totals["calls"]} 次，输入 {totals["input_tokens"]}、输出 {totals["output_tokens"]} token'
        + (f'（{totals["unknown_usage_calls"]} 次用量不明）' if totals['unknown_usage_calls'] else '')
        for cls, totals in state['usage'].items()))
    lines.append('隔离检查：' + ('还没有运行 rsi check' if state['check'] is None else
                                f'Codex {state["check"]["codex_version"]}，'
                                f'{"通过" if state["check"]["passed"] else "没通过"}'))
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
    envelope = {'imported': '从 codex-home/config.toml 导入（原文件另存为 config.toml.before-rsi）',
                'default': '按默认值新写（codex-home/config.toml 不存在）', 'kept': '已有，没有改'}
    lines.append(f'Lab 的对外范围 owner/lab-envelope.toml：{envelope[report["envelope"]]}')
    first = f'，其中 {report["first_version"]} 是这次用种子文件建的' if report['first_version'] else ''
    lines.append(f'当前版本：{report["version"]}{first}')
    lines.append({'staged': 'work/：换成了当前版本', 'kept': 'work/：已有，没有动'}[report['work']])
    lines.append('env/：' + {'created': '新建了 Python 环境', 'kept': '已有，没有动', 'skipped': '没有建（见问题）',
                            'failed': '没建好（见问题）'}[report['env']])
    lines.append('Codex 配置 codex-home/config.toml：已重新生成')
    for home, state in report['signed_in'].items():
        lines.append(f'{home}：{"已登录" if state else "没有登录" if state is False else "查不了登录状态"}'
                     + ('' if state else f'（登录：CODEX_HOME={layout.root / home} codex login）'))
    lines += [f'问题：{line}' for line in report['problems']]
    _print(lines)
    return 0


def _check(layout: Layout) -> int:
    print('rsi check 还没有实现（计划的第 9 步）。', file=sys.stderr)
    return 1


def _task(layout: Layout, task: str, meta: bool) -> int:
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
            and call['id'] not in triggered]


def _meta(layout: Layout) -> int:
    trigger = untriggered(Records(layout.records))
    _print([f'这一轮看的任务运行：{"、".join(trigger) or "无"}'])
    _print(_round_lines(layout, run_round(layout, trigger)))
    _print(_footer(layout))
    return 0


def _rollback(layout: Layout, version: str) -> int:
    new = rollback(layout, version, OWNER)
    _print([f'已回退：新版本 {new} 的内容和 {version} 相同，work/ 已换成它。',
            f'记录：{_shown(layout, layout.records / "releases" / f"{new}.json")}'])
    return 0


def _recover(layout: Layout) -> int:
    moved = []
    ids = recover(layout, moved)
    lines = [f'标成“中断”的调用：{"、".join(ids)}' if ids else '没有未结束的调用。']
    lines += [f'  {line}' for line in moved]
    _print(lines)
    return 0


COMMANDS = {'init': _init, 'check': _check, 'task': _task, 'meta': _meta, 'rollback': _rollback, 'recover': _recover}
