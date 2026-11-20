"""The owner's caps, from `owner/limits.toml`, and the accounting they are checked against.

Only the owner edits limits.toml; a missing file means the defaults, and anything the file holds besides the six
keys below is an error rather than ignored. refusal() checks only the daily caps, over the call records; the caps
per Lab run and per Meta round are for the code that starts those. Usage comes from each call's record after the
call; a call whose usage is not known is counted as unknown, never as zero.
"""
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FILE = 'limits.toml'
COST_CLASSES = ('online', 'meta', 'eval')
# (table, key) in limits.toml -> (field of Limits, smallest allowed value)
KEYS = {
    ('lab', 'seconds_per_run'): ('lab_seconds', 1),
    ('meta', 'calls_per_round'): ('meta_calls', 0),
    ('meta', 'trials_per_round'): ('meta_trials', 0),
    ('meta', 'seconds_per_call'): ('meta_seconds', 1),
    ('day', 'calls'): ('day_calls', 0),
    ('day', 'tokens'): ('day_tokens', 0),        # 0 means no cap
}
TABLES = {table for table, _ in KEYS}


@dataclass
class Limits:
    lab_seconds: int = 1800
    meta_calls: int = 8
    meta_trials: int = 4
    meta_seconds: int = 900
    day_calls: int = 60
    day_tokens: int = 0


def load_limits(owner: Path) -> Limits:
    """owner/limits.toml over the defaults; ValueError (in Chinese, listing every problem) for anything else."""
    path = Path(owner) / FILE
    try:
        text = path.read_bytes().decode('utf-8')
        table = tomllib.loads(text)
    except FileNotFoundError:
        return Limits()
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f'{path} 不是合法的 UTF-8 TOML 文件：{error}') from error
    values, problems = {}, []
    for name, body in table.items():
        if name not in TABLES:
            problems.append(f'不认识的项 {name}')
            continue
        if not isinstance(body, dict):
            problems.append(f'{name} 应写成 [{name}] 表')
            continue
        for key, value in body.items():
            if (name, key) not in KEYS:
                problems.append(f'不认识的键 {name}.{key}')
                continue
            field, smallest = KEYS[(name, key)]
            if type(value) is not int or value < smallest:
                problems.append(f'{name}.{key} 应为不小于 {smallest} 的整数，现在是 {value!r}')
            else:
                values[field] = value
    if problems:
        allowed = '、'.join(f'{name}.{key}' for name, key in KEYS)
        raise ValueError(f'{path} 有问题：{"；".join(problems)}。能写的键只有：{allowed}。')
    return Limits(**values)


def _local_day(moment: datetime):
    """The calendar day of moment in this machine's time zone (a naive moment is taken as local time)."""
    return moment.astimezone().date()


def _tokens(usage) -> tuple[int, int] | None:
    """(input, output) tokens of a call's usage, or None when they are not both known."""
    if not isinstance(usage, dict):
        return None
    counts = usage.get('input_tokens'), usage.get('output_tokens')
    if not all(type(count) is int and count >= 0 for count in counts):
        return None
    return counts


def refusal(limits: Limits, calls: list[dict], now: datetime) -> str | None:
    """Why no new call may start today, in Chinese, or None.

    Today is the local calendar day of now; a call belongs to the day it started, whatever its status. A call
    without a readable `started` time is not counted. The token cap counts only calls whose usage is known.
    """
    today = _local_day(now)
    todays = []
    for call in calls:
        try:
            started = datetime.fromisoformat(call['started'])
        except (KeyError, TypeError, ValueError):
            continue
        if _local_day(started) == today:
            todays.append(call)
    if len(todays) >= limits.day_calls:
        return (f'今天（{today}）已经开始了 {len(todays)} 次调用，到了每天 {limits.day_calls} 次的上限'
                f'（owner/limits.toml 的 day.calls）。')
    if limits.day_tokens:
        known = [counts for call in todays if (counts := _tokens(call.get('usage'))) is not None]
        used = sum(i + o for i, o in known)
        if used >= limits.day_tokens:
            unknown = len(todays) - len(known)
            note = f'另有 {unknown} 次调用的用量不明，没有算进去。' if unknown else ''
            return (f'今天（{today}）的调用已用 {used} 个 token，到了每天 {limits.day_tokens} 个的上限'
                    f'（owner/limits.toml 的 day.tokens）。{note}')
    return None


def _no_usage() -> dict:
    return {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'unknown_usage_calls': 0}


def usage_by_class(calls: list[dict]) -> dict:
    """cost class -> {'calls', 'input_tokens', 'output_tokens', 'unknown_usage_calls'}; the three classes always."""
    summary = {cls: _no_usage() for cls in COST_CLASSES}
    for call in calls:
        totals = summary.setdefault(call.get('cost_class'), _no_usage())
        totals['calls'] += 1
        counts = _tokens(call.get('usage'))
        if counts is None:
            totals['unknown_usage_calls'] += 1
        else:
            totals['input_tokens'] += counts[0]
            totals['output_tokens'] += counts[1]
    return summary
