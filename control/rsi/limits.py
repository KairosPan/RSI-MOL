"""The owner's caps, from `owner/limits.toml`, and the accounting they are checked against.

Only the owner edits limits.toml; a missing file means the defaults, and anything the file holds besides the six
keys below is an error rather than ignored. refusal() checks only the daily caps, over the call records; the caps
per Lab run and per Meta round are for the code that starts those. Usage comes from each call's record after the
call; a call whose usage is not known is counted as unknown, never as zero. A call record that cannot be read
(status `unreadable`) counts as a call of today with unknown usage and cost class `unknown`.
"""
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rsi.records import UNREADABLE

FILE = 'limits.toml'
COST_CLASSES = ('online', 'meta', 'eval')
UNKNOWN_CLASS = 'unknown'      # for a call whose cost class is missing, not a string, or unreadable
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
    """owner/limits.toml over the defaults; ValueError (in English, listing every problem) for anything else."""
    path = Path(owner) / FILE
    try:
        text = path.read_bytes().decode('utf-8')
        table = tomllib.loads(text)
    except FileNotFoundError:
        return Limits()
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f'{path} is not a valid UTF-8 TOML file: {error}') from error
    values, problems = {}, []
    for name, body in table.items():
        if name not in TABLES:
            problems.append(f'unknown item {name}')
            continue
        if not isinstance(body, dict):
            problems.append(f'{name} must be written as the table [{name}]')
            continue
        for key, value in body.items():
            if (name, key) not in KEYS:
                problems.append(f'unknown key {name}.{key}')
                continue
            field, smallest = KEYS[(name, key)]
            if type(value) is not int or value < smallest:
                problems.append(f'{name}.{key} must be an integer of at least {smallest}, not {value!r}')
            else:
                values[field] = value
    if problems:
        allowed = ', '.join(f'{name}.{key}' for name, key in KEYS)
        raise ValueError(f'{path} has problems: {"; ".join(problems)}. The only keys allowed are {allowed}.')
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


def _unreadable(call) -> bool:
    return not isinstance(call, dict) or call.get('status') == UNREADABLE


def _usage(call):
    return None if _unreadable(call) else call.get('usage')


def refusal(limits: Limits, calls: list[dict], now: datetime) -> str | None:
    """Why no new call may start today, in English, or None.

    Today is the local calendar day of now; a call belongs to the day it started, whatever its status. An
    unreadable record counts as today's, whatever its date, since its date is not known; a readable call without a
    readable `started` time is not counted. The token cap counts only calls whose usage is known.
    """
    today = _local_day(now)
    todays = []
    for call in calls:
        if _unreadable(call):
            todays.append(call)
            continue
        try:
            started = datetime.fromisoformat(call['started'])
        except (KeyError, TypeError, ValueError):
            continue
        if _local_day(started) == today:
            todays.append(call)
    if len(todays) >= limits.day_calls:
        unreadable = sum(map(_unreadable, todays))
        note = f" Unreadable call records among them, counted as today's: {unreadable}." if unreadable else ''
        return (f'Calls started today ({today}): {len(todays)}. That reaches the limit of {limits.day_calls} a day '
                f'(day.calls in owner/limits.toml).{note}')
    if limits.day_tokens:
        known = [counts for call in todays if (counts := _tokens(_usage(call))) is not None]
        used = sum(i + o for i, o in known)
        if used >= limits.day_tokens:
            unknown = len(todays) - len(known)
            note = f' Calls of today with unknown usage, not counted: {unknown}.' if unknown else ''
            return (f"Tokens used by today's calls ({today}): {used}. That reaches the limit of {limits.day_tokens} "
                    f'a day (day.tokens in owner/limits.toml).{note}')
    return None


def _no_usage() -> dict:
    return {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'unknown_usage_calls': 0}


def usage_by_class(calls: list[dict]) -> dict:
    """cost class -> {'calls', 'input_tokens', 'output_tokens', 'unknown_usage_calls'}.

    The three classes are always there. A call whose class is missing, empty, not a string or unreadable goes under
    `unknown`.
    """
    summary = {cls: _no_usage() for cls in COST_CLASSES}
    for call in calls:
        cls = None if _unreadable(call) else call.get('cost_class')
        totals = summary.setdefault(cls if isinstance(cls, str) and cls else UNKNOWN_CLASS, _no_usage())
        totals['calls'] += 1
        counts = _tokens(_usage(call))
        if counts is None:
            totals['unknown_usage_calls'] += 1
        else:
            totals['input_tokens'] += counts[0]
            totals['output_tokens'] += counts[1]
    return summary
