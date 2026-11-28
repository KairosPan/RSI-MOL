"""The append-only evidence ledger. Each run or owner action is one atomically published JSONL segment.

The last event of every segment holds enough information to recreate its separate registration report.
The report seals the end of the segment, so a valid but shorter hash-chain prefix fails verification.
All writers enter under the rsi command lock. Reading an absent store does not initialize it.
"""
import hashlib
import json
from rsi import fs as os
import stat
from dataclasses import dataclass, field
from pathlib import Path
import re

from rsi.paths import Layout
from rsi.records import CALL_ID, Records

EMPTY_HASH = '0' * 64
DIGEST = re.compile(r'[0-9a-f]{64}')
OWNER_ID = re.compile(r'O[0-9a-f]{64}')
SEGMENT = re.compile(rf'([0-9]{{12,}})-({CALL_ID.pattern}|{OWNER_ID.pattern})\.jsonl')
MAX_EVENT_BYTES = 16 << 20
MAX_SEGMENT_BYTES = 64 << 20
TYPES = ('passage', 'acquisition', 'paper')


def encode(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data) -> str:
    return hashlib.sha256(encode(data)).hexdigest()


@dataclass
class State:
    version: int = 0
    tail: str = EMPTY_HASH
    objects: list[dict] = field(default_factory=list)
    deliveries: list[dict] = field(default_factory=list)
    reports: dict[str, dict] = field(default_factory=dict)
    missing_reports: set[str] = field(default_factory=set)
    owner_events: list[dict] = field(default_factory=list)
    owner_reports: dict[str, dict] = field(default_factory=dict)
    missing_owner_reports: set[str] = field(default_factory=set)


def _folder(path, create=False):
    """The owner controls the parent. A reserved directory name must never alias another location."""
    if create:
        path.mkdir(exist_ok=True)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise ValueError(f'Memory path is not a plain directory: {path}')
    return True


def _file(path, limit):
    # An lstat before open avoids touching devices. O_NOFOLLOW and fstat check the opened entry.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(f'Memory file is not a bounded regular file: {path}')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    opened = os.fstat(fd)
    if not os.path.samestat(info, opened) or opened.st_size > limit:
        os.close(fd)
        raise ValueError(f'Memory file changed while opening: {path}')
    return os.fdopen(fd, 'rb')


def init_store(layout: Layout) -> None:
    _folder(layout.memory, create=True)
    _folder(layout.memory / 'batches', create=True)


def source_hashes(state):
    originals = {o['content']['source_sha256'] for o in state.objects if o['content'].get('source_sha256')}
    originals.update(d['derived_from']['source_sha256'] for d in state.deliveries if d.get('derived_from'))
    return originals


def _report(batch, before, seq, tail, summary):
    actor = 'owner' if batch.startswith('O') else 'run'
    return {**summary, actor: batch, 'version_before': before, 'version_after': seq,
            'event_count': seq - before, 'tail_sha256': tail}


def _report_sets(state, batch):
    if batch.startswith('O'):
        return state.owner_reports, state.missing_owner_reports
    return state.reports, state.missing_reports


def _owner_summary(state, action, target):
    if not isinstance(target, dict):
        raise ValueError('Invalid owner action target')
    if action == 'source_changed' and target.keys() == {'source_sha256'}:
        sha = target['source_sha256']
        if not isinstance(sha, str) or not DIGEST.fullmatch(sha) or sha not in source_hashes(state):
            raise ValueError(f'Unknown registered source SHA-256 {sha!r}')
    elif action == 'withdrawn' and target.keys() == {'id', 'revision'}:
        ident, revision = target['id'], target['revision']
        if type(revision) is not int or revision < 1 or not any(
                o['id'] == ident and o['revision'] == revision for o in state.objects):
            raise ValueError(f'Unknown memory object revision {ident!r} @ {revision!r}')
    else:
        raise ValueError('Invalid owner action or target')
    return {'action': action, **target}


def _apply(state, batch, event_type, value, seq, before):
    if batch.startswith('O'):
        if event_type == 'commit':
            valid = seq == before + 2 and batch == 'O' + digest(value)
        else:
            summary = _owner_summary(state, event_type, value)
            valid = seq == before + 1 and batch == 'O' + digest(summary)
            if valid:
                state.owner_events.append({'type': event_type, **value, 'seq': seq})
        if not valid:
            raise ValueError(f'Owner action disagrees with its batch at memory event {seq}')
        return
    if event_type == 'object':
        expected = value['record_sha256']
        if expected != digest({k: v for k, v in value.items() if k != 'record_sha256'}):
            raise ValueError(f'Bad object checksum at memory event {seq}')
        state.objects.append(value)
    elif event_type == 'delivery':
        state.deliveries.append({**value, 'seq': seq})
    elif event_type != 'commit':
        raise ValueError(f'Unknown memory event type at {seq}')


def _read_segment(state, path, batch):
    before = state.version
    actor = 'owner' if batch.startswith('O') else 'run'
    report = None
    with _file(path, MAX_SEGMENT_BYTES) as stream:
        while raw := stream.readline(MAX_EVENT_BYTES + 1):
            if report is not None or len(raw) > MAX_EVENT_BYTES or not raw.endswith(b'\n'):
                raise ValueError(f'Invalid or incomplete memory segment {path.name}')
            event = json.loads(raw)
            seq = state.version + 1
            if (event['seq'] != seq or event['previous'] != state.tail or event[actor] != batch
                    or ('owner' in event and 'run' in event)):
                raise ValueError(f'Memory chain disagrees at event {seq} in {path.name}')
            state.tail = hashlib.sha256(raw).hexdigest()
            state.version = seq
            _apply(state, batch, event['type'], event['value'], seq, before)
            if event['type'] == 'commit':
                report = _report(batch, before, seq, state.tail, event['value'])
    if report is None:
        raise ValueError(f'Missing commit at end of memory segment {path.name}')
    reports, _ = _report_sets(state, batch)
    reports[batch] = report


def load(layout: Layout) -> State:
    """Read all committed events and verify their chain and available external report anchors."""
    state = State()
    folder = layout.memory / 'batches'
    has_batches = _folder(layout.memory) and _folder(folder)
    reports = layout.records / 'memory'
    has_reports = _folder(layout.records) and _folder(reports)
    # Global 'rsi status' may run without the writer lock. Reports follow their batches, so
    # taking their names first cannot introduce a report for a batch we already missed.
    report_files = list(reports.glob('*.json')) if has_reports else []
    for path in sorted(folder.iterdir()) if has_batches else []:
        if path.name.startswith('.'):
            continue                 # an interrupted writer's private temporary file was never committed
        m = SEGMENT.fullmatch(path.name)
        if not m or int(m[1]) != state.version + 1 or m[2] in state.reports or m[2] in state.owner_reports:
            raise ValueError(f'Unexpected memory segment {path.name}')
        try:
            _read_segment(state, path, m[2])
        except (KeyError, TypeError, UnicodeError, ValueError, RecursionError) as exc:
            raise ValueError(f'Invalid memory segment {path.name}: {exc}') from exc
    anchored = set()
    for path in report_files:
        batches, _ = _report_sets(state, path.stem)
        if path.stem not in batches:
            raise ValueError(f'Registration report has no matching memory batch: {path.name}')
        with _file(path, MAX_EVENT_BYTES) as stream:
            if json.load(stream) != batches[path.stem]:
                raise ValueError(f'Registration report disagrees with memory batch {path.stem}')
        anchored.add(path.stem)
    state.missing_reports = state.reports.keys() - anchored
    state.missing_owner_reports = state.owner_reports.keys() - anchored
    return state


def _sync_dir(folder: Path):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def finish_report(layout, state, batch):
    reports, missing = _report_sets(state, batch)
    report = reports[batch]
    if batch in missing:
        Records(layout.records).write_json(f'memory/{batch}.json', report)
        _sync_dir(layout.records / 'memory')
        missing.remove(batch)
    return report


def publish(layout, state, run, rows, summary):
    """Publish a validated run under the outer command lock. Nothing partial ever becomes visible."""
    if not CALL_ID.fullmatch(run):
        raise ValueError('Invalid registration run ID')
    return _publish(layout, state, run, rows, summary)


def record_owner_action(layout, action, target):
    """Append an owner action once, or finish its independent report after an interrupted write."""
    state = load(layout)
    summary = _owner_summary(state, action, target)
    return _publish(layout, state, 'O' + digest(summary), [(action, target)], summary)


def _publish(layout, state, batch, rows, summary):
    reports, missing = _report_sets(state, batch)
    if batch in reports:
        return finish_report(layout, state, batch)
    init_store(layout)
    folder = layout.memory / 'batches'
    before, tail = state.version, state.tail
    rows = [*rows, ('commit', summary)]
    chunks = []
    actor = 'owner' if batch.startswith('O') else 'run'
    for seq, (kind, value) in enumerate(rows, before + 1):
        raw = encode({'seq': seq, 'previous': tail, actor: batch, 'type': kind, 'value': value}) + b'\n'
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError(f'Memory event exceeds {MAX_EVENT_BYTES} bytes')
        tail = hashlib.sha256(raw).hexdigest()
        chunks.append(raw)
    target = folder / f'{before + 1:012d}-{batch}.jsonl'
    if sum(map(len, chunks)) > MAX_SEGMENT_BYTES:
        raise ValueError('Registration batch exceeds the segment byte limit')
    Records(layout.records)._create(target, b''.join(chunks))
    _sync_dir(folder)
    for seq, (kind, value) in enumerate(rows, before + 1):
        _apply(state, batch, kind, value, seq, before)
    state.version, state.tail = before + len(rows), tail
    # Materialize the report from the committed bytes, as recovery does, including nested field order.
    reports[batch] = _report(batch, before, state.version, tail, json.loads(chunks[-1])['value'])
    missing.add(batch)
    return finish_report(layout, state, batch)


def show(layout, evidence_id):
    state = load(layout)
    versions = [v for v in state.objects if v['id'] == evidence_id]
    if not versions:
        raise ValueError(f'Unknown evidence ID {evidence_id!r}')
    return {'versions': versions, 'deliveries': [d for d in state.deliveries if d['id'] == evidence_id]}
