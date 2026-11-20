"""Meta's decision (design §5.5): the JSON object every Meta call ends with, and its checks.

Meta is untrusted, so its answer is parsed strictly before anything is done with it: exactly the keys of SCHEMA, of
the types it names, and the rules beyond it. A trial decision lists at least one trial, and each trial names exactly
one of a task (a plain id, rsi.paths.safe_id) and a probe text; a release decision cites at least one record (each id
is kept once); every record id (observations, evidence, applications) is a call id; every text can be encoded as
UTF-8, so it can be recorded and sent on. Anything else is a ValueError whose message is one English line, which goes
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
        raise ValueError(f'an object in the decision has {len(pairs)} keys; at most {MAX_PAIRS} are allowed')
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f'a key is repeated in the decision: {show(key)}')
        found[key] = value
    return found


def _utf8(value: str, where: str) -> str:
    """value, once it is known that UTF-8 can hold it: a lone surrogate (JSON lets one in as an escape) could be
    neither recorded nor sent on."""
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'{where} has characters that cannot be stored as UTF-8 (a lone surrogate, for example); '
                         'remove them') from None
    return value


def _keys(value, keys: tuple, where: str) -> dict:
    """value as an object with exactly these keys."""
    if not isinstance(value, dict):
        raise ValueError(f'{where} must be a JSON object')
    missing = [key for key in keys if key not in value]
    extra = sorted(set(value) - set(keys), key=str)
    if missing:
        raise ValueError(f'{where} lacks keys: {", ".join(missing)}')
    if extra:
        raise ValueError(f'{where} has unknown keys: {show(", ".join(map(str, extra)))} (only {", ".join(keys)} are '
                         'allowed)')
    return value


def _text(value, where: str, *, filled: bool = False) -> str:
    if not isinstance(value, str) or (filled and not value.strip()):
        raise ValueError(f'{where} must be {"non-empty " if filled else ""}text')
    return _utf8(value, where)


def _list(value, where: str, limit: int) -> list:
    if not isinstance(value, list):
        raise ValueError(f'{where} must be a list')
    if len(value) > limit:
        raise ValueError(f'{where} may have at most {limit} items; it has {len(value)}')
    return value


def _call_id(value, where: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_ID or not CALL_ID.fullmatch(value):
        raise ValueError(f'{show(value)} in {where} is not a call id (a call id is R and at least four digits, for '
                         'example R0002)')
    return value


def _ids(value, where: str) -> list[str]:
    return [_call_id(item, where) for item in _list(value, where, MAX_ITEMS)]


def _trial(value, number: int) -> dict:
    where = f'trials item {number}'
    if not isinstance(value, dict):
        raise ValueError(f'every item of trials must be an object; {where} is not')
    trial = _keys(value, ('task', 'probe', 'version'), where)
    task, probe, version = trial['task'], trial['probe'], trial['version']
    if version not in VERSIONS:
        raise ValueError(f'the field version of {where} must be candidate or current; it is {show(version)}')
    if (task is None) == (probe is None):
        raise ValueError(f'{where} must give exactly one of task (the id of an existing task) and probe (a test '
                         'text)')
    if task is not None and not (isinstance(task, str) and SAFE_ID.fullmatch(task)):
        raise ValueError(f'the task {show(task)} of {where} is not a valid task id (letters, digits, _ and -, '
                         'starting with a letter or a digit)')
    if probe is not None and (not isinstance(probe, str) or not probe.strip() or len(probe) > PROBE_LIMIT):
        raise ValueError(f'the field probe of {where} must be non-empty text of at most {PROBE_LIMIT} characters')
    if probe is not None:
        _utf8(probe, f'the field probe of {where}')
    return {'task': task, 'probe': probe, 'version': version}


def _observation(value, number: int) -> dict:
    where = f'observations item {number}'
    item = _keys(value, ('record', 'note'), where)
    return {'record': _call_id(item['record'], 'observations'),
            'note': _text(item['note'], f'the field note of {where}')}


def _application(value, number: int) -> dict:
    where = f'applications item {number}'
    item = _keys(value, ('what', 'why', 'records'), where)
    return {'what': _text(item['what'], f'the field what of {where}', filled=True),
            'why': _text(item['why'], f'the field why of {where}'), 'records': _ids(item['records'], 'applications')}


def parse_decision(text: str) -> Decision:
    """The decision in Meta's answer; ValueError with a one-line English reason when it is not a valid one."""
    if not isinstance(text, str):
        raise ValueError('no decision was returned as text')
    if len(text) > MAX_TEXT:
        raise ValueError(f'the decision is too long: {len(text)} characters; at most {MAX_TEXT} are allowed')
    try:
        data = json.loads(text.strip(), object_pairs_hook=_no_duplicates)
    except RecursionError:
        raise ValueError('it is not valid JSON: it is nested too deeply') from None
    except json.JSONDecodeError as error:
        raise ValueError(f'it is not valid JSON: {error.msg} (line {error.lineno}, column {error.colno})') from None
    if not isinstance(data, dict):
        raise ValueError('the decision must be a JSON object')
    _keys(data, KEYS, 'the decision')
    kind = data['decision']
    if kind not in KINDS:
        raise ValueError(f'the field decision must be one of {", ".join(KINDS)}; it is {show(kind)}')
    notes = {'observations': [_observation(item, number) for number, item in
                              enumerate(_list(data['observations'], 'observations', MAX_OBSERVATIONS), start=1)]}
    for key in NOTE_KEYS:
        notes[key] = _text(data[key], key)
    trials = [_trial(item, number) for number, item in enumerate(_list(data['trials'], 'trials', MAX_ITEMS), start=1)]
    evidence = list(dict.fromkeys(_ids(data['evidence'], 'evidence')))           # each id once, in order
    applications = [_application(item, number)
                    for number, item in enumerate(_list(data['applications'], 'applications', MAX_ITEMS), start=1)]
    if kind == 'trial' and not trials:
        raise ValueError('a trial decision must list at least one trial in trials')
    if kind == 'release' and not evidence:
        raise ValueError('a release decision must cite at least one run record in evidence')
    decision = Decision(kind, notes, trials, evidence, applications)
    _utf8(json.dumps(decision.as_dict(), ensure_ascii=False), 'the decision')      # every part can be recorded
    return decision
