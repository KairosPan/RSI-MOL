"""Meta's decision (design §5.5): the JSON object every Meta call ends with, and its checks.

Meta is untrusted, so its answer is parsed strictly before anything is done with it: exactly the keys of SCHEMA, of
the types it names, and the rules beyond it. A trial decision lists at least one trial, and each trial names exactly
one of a task (a plain id, rsi.paths.safe_id) and a probe text; a release decision cites at least one record (each id
is kept once); every record id (observations, evidence, applications) is a call id; every text can be encoded as
UTF-8, so it can be recorded and sent on. Anything else is a ValueError whose message is one Chinese line, which goes
back to Meta as the reason.
"""
import json
from dataclasses import dataclass, field

from rsi.labrun import PROBE_LIMIT
from rsi.paths import SAFE_ID
from rsi.records import CALL_ID
from rsi.settings import show

KINDS = ('no_change', 'trial', 'release')
VERSIONS = ('candidate', 'current')                 # a trial runs on the candidate or on main's head
NOTE_KEYS = ('problem', 'likely_cause', 'other_explanations', 'change', 'expected_effect', 'risks', 'uncertain')
MAX_TEXT = 1 << 20          # characters of a whole decision
MAX_ITEMS = 20              # entries of trials, evidence and applications
MAX_OBSERVATIONS = 100
MAX_ID = 16                 # characters of a record id
MAX_PAIRS = 32              # keys of any one object (the decision itself has 12)


def _object(properties: dict) -> dict:
    """A strict object schema: no other keys, and every key required (as Codex's structured output wants)."""
    return {'type': 'object', 'additionalProperties': False, 'required': list(properties), 'properties': properties}


TEXT = {'type': 'string'}
IDS = {'type': 'array', 'items': TEXT}
SCHEMA = _object({
    'decision': {'type': 'string', 'enum': list(KINDS)},
    'observations': {'type': 'array', 'items': _object({'record': TEXT, 'note': TEXT})},
    **{key: TEXT for key in NOTE_KEYS},
    'trials': {'type': 'array', 'items': _object({'task': {'type': ['string', 'null']},
                                                  'probe': {'type': ['string', 'null']},
                                                  'version': {'type': 'string', 'enum': list(VERSIONS)}})},
    'evidence': IDS,
    'applications': {'type': 'array', 'items': _object({'what': TEXT, 'why': TEXT, 'records': IDS})},
})
KEYS = tuple(SCHEMA['properties'])


@dataclass
class Decision:
    kind: str                                               # no_change, trial or release
    notes: dict                                             # observations and the NOTE_KEYS texts
    trials: list[dict] = field(default_factory=list)        # {'task': id or None, 'probe': text or None, 'version'}
    evidence: list[str] = field(default_factory=list)
    applications: list[dict] = field(default_factory=list)  # {'what', 'why', 'records'}

    def as_dict(self) -> dict:
        """The decision as Meta wrote it (after the checks), in SCHEMA's key order."""
        return {'decision': self.kind, 'observations': self.notes['observations'],
                **{key: self.notes[key] for key in NOTE_KEYS}, 'trials': self.trials, 'evidence': self.evidence,
                'applications': self.applications}


def _no_duplicates(pairs: list) -> dict:
    """One object of the JSON text, refused when it has more than MAX_PAIRS keys or a key twice; linear in size."""
    if len(pairs) > MAX_PAIRS:
        raise ValueError(f'决定里有一个对象有 {len(pairs)} 个键，最多只能有 {MAX_PAIRS} 个')
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f'决定里的键重复了：{show(key)}')
        found[key] = value
    return found


def _utf8(value: str, where: str) -> str:
    """value, once it is known that UTF-8 can hold it: a lone surrogate (JSON lets one in as an escape) could be
    neither recorded nor sent on."""
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'{where} 里有不能存成 UTF-8 的字符（例如单独的代理码位），请去掉') from None
    return value


def _keys(value, keys: tuple, where: str) -> dict:
    """value as an object with exactly these keys."""
    if not isinstance(value, dict):
        raise ValueError(f'{where} 必须是一个 JSON 对象')
    missing = [key for key in keys if key not in value]
    extra = sorted(set(value) - set(keys), key=str)
    if missing:
        raise ValueError(f'{where} 缺少键：{"、".join(missing)}')
    if extra:
        raise ValueError(f'{where} 多了不认识的键：{show(", ".join(map(str, extra)))}（只能有 {"、".join(keys)}）')
    return value


def _text(value, where: str, *, filled: bool = False) -> str:
    if not isinstance(value, str) or (filled and not value.strip()):
        raise ValueError(f'{where} 必须是{"非空" if filled else ""}文本')
    return _utf8(value, where)


def _list(value, where: str, limit: int) -> list:
    if not isinstance(value, list):
        raise ValueError(f'{where} 必须是列表')
    if len(value) > limit:
        raise ValueError(f'{where} 最多 {limit} 项，现在有 {len(value)} 项')
    return value


def _call_id(value, where: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_ID or not CALL_ID.fullmatch(value):
        raise ValueError(f'{where} 里的 {show(value)} 不是调用编号（应为 R 加至少四位数字，例如 R0002）')
    return value


def _ids(value, where: str) -> list[str]:
    return [_call_id(item, where) for item in _list(value, where, MAX_ITEMS)]


def _trial(value, number: int) -> dict:
    where = f'trials 第 {number} 项'
    if not isinstance(value, dict):
        raise ValueError(f'trials 的每一项必须是对象，{where} 不是')
    trial = _keys(value, ('task', 'probe', 'version'), where)
    task, probe, version = trial['task'], trial['probe'], trial['version']
    if version not in VERSIONS:
        raise ValueError(f'{where} 的 version 只能是 candidate 或 current，现在是 {show(version)}')
    if (task is None) == (probe is None):
        raise ValueError(f'{where} 要么给 task（已有任务的编号），要么给 probe（一段测试文字），只能给其中一个')
    if task is not None and not (isinstance(task, str) and SAFE_ID.fullmatch(task)):
        raise ValueError(f'{where} 的 task {show(task)} 不是合规的任务编号（字母、数字、_ 和 -，以字母或数字开头）')
    if probe is not None and (not isinstance(probe, str) or not probe.strip() or len(probe) > PROBE_LIMIT):
        raise ValueError(f'{where} 的 probe 必须是非空文本，最多 {PROBE_LIMIT} 个字符')
    if probe is not None:
        _utf8(probe, f'{where} 的 probe')
    return {'task': task, 'probe': probe, 'version': version}


def _observation(value, number: int) -> dict:
    where = f'observations 第 {number} 项'
    item = _keys(value, ('record', 'note'), where)
    return {'record': _call_id(item['record'], 'observations'), 'note': _text(item['note'], f'{where} 的 note')}


def _application(value, number: int) -> dict:
    where = f'applications 第 {number} 项'
    item = _keys(value, ('what', 'why', 'records'), where)
    return {'what': _text(item['what'], f'{where} 的 what', filled=True), 'why': _text(item['why'], f'{where} 的 why'),
            'records': _ids(item['records'], 'applications')}


def parse_decision(text: str) -> Decision:
    """The decision in Meta's answer; ValueError with a one-line Chinese reason when it is not a valid one."""
    if not isinstance(text, str):
        raise ValueError('没有交回文本形式的决定')
    if len(text) > MAX_TEXT:
        raise ValueError(f'决定太长：{len(text)} 个字符，最多 {MAX_TEXT} 个字符')
    try:
        data = json.loads(text.strip(), object_pairs_hook=_no_duplicates)
    except RecursionError:
        raise ValueError('不是合法的 JSON：嵌套太深') from None
    except json.JSONDecodeError as error:
        raise ValueError(f'不是合法的 JSON：{error.msg}（第 {error.lineno} 行第 {error.colno} 列）') from None
    if not isinstance(data, dict):
        raise ValueError('决定必须是一个 JSON 对象')
    _keys(data, KEYS, '决定')
    kind = data['decision']
    if kind not in KINDS:
        raise ValueError(f'decision 只能是 {"、".join(KINDS)} 之一，现在是 {show(kind)}')
    notes = {'observations': [_observation(item, number) for number, item in
                              enumerate(_list(data['observations'], 'observations', MAX_OBSERVATIONS), start=1)]}
    for key in NOTE_KEYS:
        notes[key] = _text(data[key], key)
    trials = [_trial(item, number) for number, item in enumerate(_list(data['trials'], 'trials', MAX_ITEMS), start=1)]
    evidence = list(dict.fromkeys(_ids(data['evidence'], 'evidence')))           # each id once, in order
    applications = [_application(item, number)
                    for number, item in enumerate(_list(data['applications'], 'applications', MAX_ITEMS), start=1)]
    if kind == 'trial' and not trials:
        raise ValueError('trial 决定至少要在 trials 里列一项试跑')
    if kind == 'release' and not evidence:
        raise ValueError('release 决定至少要在 evidence 里引用一条运行记录')
    decision = Decision(kind, notes, trials, evidence, applications)
    _utf8(json.dumps(decision.as_dict(), ensure_ascii=False), '决定')      # every part can be recorded
    return decision
