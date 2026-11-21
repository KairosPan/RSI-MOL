"""Replay the recorded starting snapshot and typed tool completions against sealed owner attachments."""
import hashlib
import json

from rsi.memory_receipts import open_evidence_file, session_lines, session_paths
from rsi.memory_snapshot import SNAPSHOT_BYTES, STATUS_RESERVE, _encode, _project
from rsi.memory_store import State, encode

ADDRESS = ('document_id', 'document_version', 'source_sha256', 'locator')
HISTORICAL_DELIVERY = ('run', 'document_id', 'document_version', 'invocation_id')


def read(root, relative, limit):
    """All components below the supplied RSI root are opened relative to verified directory descriptors."""
    with open_evidence_file(root, relative) as (stream, size):
        require(size <= limit, 'Oversized evidence file: ' + relative)
        data = stream.read(size + 1)
        require(len(data) == size, 'Evidence changed while reading: ' + relative)
        return data


def require(value, detail):
    if not value:
        raise ValueError(detail)


def trusted_rows(state, memory):
    """Reconstruct exactly the revisions and owner annotations visible when this call started."""
    version, condition = memory['version'], memory['condition']
    require(type(version) is int and version >= 0, 'Invalid starting memory version')
    reports = {**state.reports, **state.owner_reports}
    require(version == 0 or any(r['version_after'] == version for r in reports.values()),
            'Starting memory version is missing from the ledger')
    require(all(reports[key]['version_after'] > version for key in state.missing_reports | state.missing_owner_reports),
            'The starting ledger prefix is missing an independent registration report')
    deliveries = [d for d in state.deliveries if d['seq'] <= version]
    keys = {(d['id'], d['revision']) for d in deliveries}
    objects = [o for o in state.objects if (o['id'], o['revision']) in keys]
    before = State(version=version, objects=objects, deliveries=deliveries,
                   owner_events=[e for e in state.owner_events if e['seq'] <= version])
    projection = memory.get('projection_version', 1)
    require(type(projection) is int and projection in (1, 2, 3), 'Unknown starting snapshot projection')
    options = {'unavailable': frozenset(memory['unavailable_sources'])} if projection >= 2 else {}
    budget = memory.get('max_bytes', SNAPSHOT_BYTES) - STATUS_RESERVE
    status, rows = _project(before, condition, memory.get('task_filter'), budget, projection_version=projection, **options)
    digest = hashlib.sha256(b'status.json\0' + _encode(status))
    if condition != 'none':
        digest.update(b'evidence.jsonl\0')
    for row in rows:
        digest.update(_encode(row))
    require(digest.hexdigest() == memory['fingerprint'], 'Starting snapshot fingerprint differs from the ledger replay')
    return rows


def passage_index(rows):
    return {(o['id'], o['revision']): (o['content'], {
                tuple(d.get(k) for k in HISTORICAL_DELIVERY) for d in row['deliveries']})
            for row in rows if (o := row['object'])['content']['type'] == 'passage'}


def trusted_passages(state, memory):
    """Keep the original audit API for archived evidence citations."""
    return passage_index(trusted_rows(state, memory))


def attachment(layout, call, name, bound):
    require(name in call['files'] and name not in (call.get('links') or {}), f'Unsealed attachment: {name}')
    raw = read(layout.root, f'records/runs/{call["id"]}/{name}', bound)
    require(call['files'].get(name) == hashlib.sha256(raw).hexdigest(), f'{call["id"]}/{name} checksum mismatch')
    return raw


def timeline(layout, call, deadline=None, *, include_proposal_starts=False):
    """Typed MCP completions and optionally proposal starts, counting inherited events once."""
    seen = {}
    paths = session_paths(call)
    expected = {call['thread_id'], *call['subagent_threads']}
    require({thread for _, thread, _ in paths} == expected, 'Missing a recorded participant session')
    for rel, thread, position, line in session_lines(layout, call, deadline):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
            continue
        payload = event.get('payload') or {}
        item = payload.get('item') or {}
        if (not isinstance(item, dict) or event.get('type') != 'event_msg' or
                payload.get('thread_id') not in (None, thread) or item.get('type') != 'McpToolCall' or
                item.get('server') not in ('literature', 'memory')):
            continue
        phase = payload.get('type')
        if phase != 'item_completed' and not (include_proposal_starts and phase == 'item_started' and
                                               item['server'] == 'memory' and item.get('tool') == 'propose_memory'):
            continue
        call_id = item.get('id')
        require(isinstance(call_id, str) and call_id, 'Missing native tool-call ID')
        key = (phase, call_id)
        fingerprint = hashlib.sha256(encode(item)).digest()
        if key in seen:
            require(seen[key] == fingerprint, 'Inconsistent duplicate tool-call ID: ' + call_id)
            continue
        seen[key] = fingerprint
        yield event.get('timestamp'), (rel, position), thread, item, payload


def returned_memory(tool, result, trusted):
    if tool not in ('get_evidence', 'recall_objects', 'recall_passages'):
        return
    rows = result.get('results')
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        obj = row if tool == 'recall_passages' else row.get('object')
        if not isinstance(obj, dict) or not isinstance(obj.get('id'), str):
            continue
        key = (obj.get('id'), obj.get('revision'))
        entry = trusted.get(key) if type(key[1]) is int else None
        if not entry or not isinstance(obj.get('content'), dict):
            continue
        try:
            if encode(obj['content']) != encode(entry[0]):
                continue
        except (ValueError, RecursionError):
            continue
        content, source_deliveries = entry
        if not isinstance(row.get('deliveries'), list):
            continue
        for d in row['deliveries']:
            if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in HISTORICAL_DELIVERY):
                continue
            if tuple(d[k] for k in HISTORICAL_DELIVERY) in source_deliveries:
                yield (d['document_id'], d['document_version'], content['source_sha256'], content['locator']), {
                    'text': content['text'], 'id': key[0], 'revision': key[1], 'source_run': d['run'],
                    'historical_invocation': d['invocation_id']}


def returned_objects(tool, result, trusted):
    """An address, snippet, nested relationship or a modified object is not a completed read of a revision."""
    if tool not in ('get_evidence', 'recall_objects', 'recall_judgments') or not isinstance(result.get('results'), list):
        return
    for row in result['results']:
        obj = row.get('object') if isinstance(row, dict) else None
        if not isinstance(obj, dict) or not isinstance(obj.get('id'), str) or type(obj.get('revision')) is not int:
            continue
        key = obj['id'], obj['revision']
        if key not in trusted or (tool == 'recall_judgments' and obj.get('kind') != 'judgment'):
            continue
        try:
            exact = encode(obj) == trusted[key]
        except (ValueError, RecursionError):
            continue
        if exact:
            yield key
