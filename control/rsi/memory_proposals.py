"""Validate sealed memory proposals against the starting snapshot and native tool receipts."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import re

from rsi import memory_store as ledger
from rsi.memory_history import (ADDRESS, attachment, passage_index, require, returned_memory,
                                returned_objects, timeline, trusted_rows)
from rsi.memory_receipts import CALLS_LIMIT, INVOCATION, deadline_check
from rsi.records import CALL_ID
from rsi.tasks import same_task_directory

PROPOSAL_FILE = re.compile(r'proposals/([0-9a-f]{32})\.json')
CONTENT_FIELDS = {'question': ('text',), 'judgment': ('statement', 'scope'), 'decision': ('decision', 'scope'),
                  'investigation': ('description', 'scope'), 'method_experience': ('change', 'rationale', 'outcome', 'scope')}
COMMON_FIELDS = {'kind', 'id', 'expected_revision', 'question_id'}
REF_KEYS = {'source_refs', 'object_refs'}
IDENT_SUFFIX = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}')
OUTCOMES = {'improved', 'degraded', 'unchanged', 'failed'}
REF_FIELDS = {'relation', 'evidence_id', 'revision', 'source_run', 'quote', *ADDRESS}
CURRENT_FIELDS = {'relation', 'delivery_source', 'invocation_id', 'quote', *ADDRESS}
RELATIONS = {'supports', 'challenges', 'background', 'derived_from'}
MAX_FILE_BYTES = 64 << 10
MAX_MATCHES = 65536


def _text(value, limit=4096):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _before(read, proposal):
    marker = proposal['start_position']
    boundary = marker if marker is not None else proposal['position']
    same_session = read['position'][0] == boundary[0]
    if same_session and read['position'] >= boundary:
        return False
    if read['time'] is not None and proposal['start'] is not None:
        # Tied millisecond clocks in different sessions do not establish an order.
        return read['time'] <= proposal['start'] if same_session else read['time'] < proposal['start']
    return same_session and marker is not None


def _clock(value):
    if type(value) is not int or not 0 <= value < 253402300799000:
        raise ValueError('invalid native event clock')
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _native_bounds(stamp, item, payload):
    """Prefer occurrence times recorded by Codex over the later session-log write timestamp."""
    end = _clock(payload['completed_at_ms']) if 'completed_at_ms' in payload else _stamp(stamp)
    if 'started_at_ms' in payload:
        start = _clock(payload['started_at_ms'])
    elif end is not None and item.get('duration'):
        duration = item['duration']
        if not isinstance(duration, dict):
            raise ValueError('invalid native event duration')
        seconds, nanos = duration.get('secs'), duration.get('nanos')
        if type(seconds) is not int or type(nanos) is not int or not 0 <= seconds <= 86400 or not 0 <= nanos < 10**9:
            raise ValueError('invalid native event duration')
        start = end - timedelta(seconds=seconds, microseconds=(nanos + 999) // 1000)
    else:
        start = None
    if start is not None and end is not None and start > end:
        raise ValueError('reversed native event clocks')
    return start, end


def _proofs(reg, rows, current):
    """Bind prior memory reads and successfully staged literature deliveries to their native completions."""
    reads, writes = defaultdict(list), defaultdict(list)
    starts = {}
    passages = passage_index(rows)
    objects = {(o['id'], o['revision']): ledger.encode(o) for row in rows if (o := row['object'])['kind'] in CONTENT_FIELDS}
    count = 0
    for stamp, pos, thread, item, payload in timeline(reg.layout, reg.call, reg.deadline, include_proposal_starts=True):
        tool = item['tool']
        if payload['type'] == 'item_started':
            require(len(starts) < CALLS_LIMIT, 'proposal start receipt limit')
            starts[thread, item['id']] = (pos, payload.get('started_at_ms'))
            continue
        if item.get('status') != 'completed':
            continue
        try:
            start, end = _native_bounds(stamp, item, payload)
            start_position, marker_clock = starts.get((thread, item['id']), (None, None))
            if marker_clock is not None:
                marker_time = _clock(marker_clock)
                if end is not None and marker_time > end:
                    continue
                start = min(start, marker_time) if start is not None else marker_time
        except (OverflowError, ValueError):
            continue
        native = {'at': stamp or '', 'time': end, 'position': pos, 'thread': thread, 'call_id': item['id']}
        if item['server'] == 'literature':
            for d in current.get((thread, item['id']), ()):
                content = reg.latest[d['id']]['content']
                address = (d['document_id'], d['document_version'], content['source_sha256'], content['locator'])
                reads['literature', d['invocation_id']].append({**native, 'address': address, 'text': content['text'],
                        'source_run': d['run'], 'id': d['id'], 'revision': d['revision']})
            continue
        if item['server'] != 'memory' or tool not in ('recall_passages', 'get_evidence', 'recall_objects',
                                                       'recall_judgments', 'propose_memory'):
            continue
        reply = item.get('result')
        if not isinstance(reply, dict) or reply.get('isError'):
            continue
        parts = reply.get('content')
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict) or parts[0].get('type') != 'text':
            continue
        try:
            value = json.loads(parts[0]['text'])
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
        if not isinstance(value, dict):
            continue
        if tool == 'propose_memory':
            pid = value.get('proposal_id')
            if not (isinstance(pid, str) and INVOCATION.fullmatch(pid) and value.get('status') == 'pending'):
                continue
            require(len(writes) < CALLS_LIMIT or pid in writes, 'proposal receipt limit')
            writes[pid].append({**native, 'start': start, 'start_position': start_position,
                                'args': item.get('arguments')})
        else:
            for address, match in returned_memory(tool, value, passages):
                count += 1
                require(count <= MAX_MATCHES, 'proposal evidence receipt limit')
                reads['memory', match['id'], match['revision']].append({**match, **native, 'address': address})
            for ident, revision in returned_objects(tool, value, objects):
                count += 1
                require(count <= MAX_MATCHES, 'proposal object receipt limit')
                reads['object', ident, revision].append(native)
    return reads, writes


def _reference(ref, reads, proposal, history_error):
    require(isinstance(ref, dict) and ref.keys() in (REF_FIELDS, CURRENT_FIELDS), 'invalid evidence reference fields')
    require(all(_text(v) for k, v in ref.items() if k != 'revision'), 'missing or oversized evidence reference field')
    require(ref['relation'] in RELATIONS, 'invalid evidence relation')
    current = 'invocation_id' in ref
    if current:
        require(ref['delivery_source'] == 'literature' and INVOCATION.fullmatch(ref['invocation_id']),
                'invalid current literature invocation')
        candidates = reads.get(('literature', ref['invocation_id']), ())
        require(candidates, 'invocation has no verified complete passage delivery in this run')
    else:
        require(not history_error, history_error)
        require(type(ref['revision']) is int and ref['revision'] > 0 and CALL_ID.fullmatch(ref['source_run']),
                'invalid evidence revision or source run')
        candidates = reads.get(('memory', ref['evidence_id'], ref['revision']), ())
        require(candidates, 'evidence revision was not returned by memory in this run')
    address = tuple(ref[k] for k in ADDRESS)
    located = [r for r in candidates if r['address'] == address and (current or r['source_run'] == ref['source_run'])]
    require(located, 'evidence address or historical delivery does not match')
    preceding = [r for r in located if _before(r, proposal)]
    require(preceding, 'tool returned the evidence after the proposal or without verifiable ordering')
    matches = [r for r in preceding if ref['quote'] in r['text']]
    require(matches, 'quotation does not occur verbatim in the returned evidence')
    match = matches[0]
    if current:
        require(len({(r['id'], r['revision']) for r in matches}) == 1, 'ambiguous current passage revision')
        return {**ref, **{k: match[k] for k in ('id', 'revision', 'source_run', 'thread', 'call_id')}}
    return {**{k: v for k, v in ref.items() if k != 'evidence_id'}, 'id': ref['evidence_id'],
            **{k: match[k] for k in ('thread', 'call_id', 'historical_invocation')}}


def _object_reference(ref, ident, reads, write, history_error):
    require(isinstance(ref, dict) and ref.keys() == {'id', 'revision', 'relation'}, 'invalid object reference fields')
    require(_text(ref['id']) and type(ref['revision']) is int and ref['revision'] > 0 and
            isinstance(ref['relation'], str) and ref['relation'] in RELATIONS, 'invalid object ID, revision or relation')
    require(ref['id'] != ident, 'an object cannot reference itself')
    require(not history_error, history_error)
    candidates = reads.get(('object', ref['id'], ref['revision']), ())
    require(candidates, 'object revision was not returned by memory in this run')
    match = next((r for r in candidates if _before(r, write)), None)
    require(match, 'tool returned the object after the proposal or without verifiable ordering')
    return {**ref, 'thread': match['thread'], 'call_id': match['call_id']}


def _object(ident, kind, prior, origin, content, *, sources=(), parents=(), questions=(), authority='model_interpretation'):
    obj = {'schema_version': 'rsi_mol_memory_v1', 'id': ident, 'kind': kind,
           'revision': prior['revision'] + 1 if prior else 1, 'supersedes': prior['revision'] if prior else None,
           'content': {'type': kind, **content}, 'origin': origin, 'question_ids': list(questions),
           'object_refs': list(parents), 'source_refs': list(sources),
           'authority': authority}
    return {**obj, 'record_sha256': ledger.digest(obj)}


def _task_contract(reg):
    task = reg.call['task']
    authors = {task}
    # Legacy calls could record another spelling on a case-insensitive volume. Do not
    # authorize a similarly named task that has a separate directory or is now absent.
    names = {obj['origin']['task'] for obj in reg.latest.values() if obj['kind'] != 'evidence'}
    authors.update(name for name in names if name != task and name.casefold() == task.casefold()
                   and same_task_directory(reg.layout.tasks / task, reg.layout.tasks / name))
    canonical = 'question_' + ledger.digest(task)
    roots = [obj['id'] for obj in reg.latest.values() if obj.get('authority') == 'task_contract'
             and obj['origin']['task'] in authors]
    return authors, roots, canonical if canonical in roots or not roots else roots[0]


def _question_parent(reg, requested, parents, origin, question_id):
    """An explicit subquestion pin distinguishes it from a task that happens to have the same name."""
    require(_text(requested), 'missing task or subquestion ID')
    pins = {r['revision'] for r in parents if r['id'] == requested}
    if requested.startswith('subquestion_') and pins:
        require(reg.latest.get(requested, {}).get('kind') == 'question' and len(pins) == 1,
                'subquestion requires a single, exact question revision')
        return requested, []
    require(requested == reg.call['task'], 'subquestion requires an exact object reference read in this run')
    require(_text(reg.call.get('task_goal'), 65536), 'missing task question')
    question = reg.latest.get(question_id)
    created = []
    if question is None or question['content']['text'] != reg.call['task_goal']:
        question = _object(question_id, 'question', question, origin, {'text': reg.call['task_goal']},
                           authority='task_contract')
        created.append(question)
    if not any(r['id'] == question_id and r['revision'] == question['revision'] for r in parents):
        parents.insert(0, {'id': question_id, 'revision': question['revision'], 'relation': 'background'})
    return question_id, created


def _objects(reg, args, reads, write, history_error, contract):
    require(isinstance(args, dict) and isinstance(args.get('kind'), str) and args['kind'] in CONTENT_FIELDS,
            'invalid proposal kind')
    kind = args['kind']
    fields = set(CONTENT_FIELDS[kind]) | COMMON_FIELDS
    require(fields <= args.keys() <= fields | REF_KEYS, f'invalid {kind} fields')
    ident, expected = args['id'], args['expected_revision']
    prefix = 'subquestion_' if kind == 'question' else kind + '_'
    require(_text(ident) and ident.startswith(prefix) and IDENT_SUFFIX.fullmatch(ident[len(prefix):])
            and type(expected) is int and expected >= 0, f'invalid {kind} ID or expected revision')
    content = {key: args[key] for key in CONTENT_FIELDS[kind]}
    for key, value in content.items():
        require(_text(value) or (key == 'scope' and value == ''), f'missing or oversized {key}')
    if kind == 'method_experience':
        require(content['outcome'] in OUTCOMES, 'unknown method experience outcome')
    prior = reg.latest.get(ident)
    authors, roots, default_root = contract
    require((prior['revision'] if prior else 0) == expected, f'expected {kind} revision differs from the ledger')
    require(not prior or (prior['kind'] == kind and prior.get('authority') == 'model_interpretation' and
                          prior['origin']['task'] in authors), f'{kind} belongs to a different task or authority')
    source_refs, object_refs = args.get('source_refs', []), args.get('object_refs', [])
    require(all(isinstance(refs, list) and len(refs) <= 16 for refs in (source_refs, object_refs)),
            'expected at most sixteen references per reference list')
    require(kind == 'question' or source_refs or object_refs, 'missing evidence or object reference')
    refs = [_reference(ref, reads, write, history_error) for ref in source_refs]
    parents = [_object_reference(ref, ident, reads, write, history_error) for ref in object_refs]
    origin = {'run': reg.call['id'], 'task': reg.call['task'], 'version': reg.call.get('version_before')}
    # If different spellings had already produced two roots, preserve a revised object's
    # own contract lineage. Explicit subquestion pins still take precedence.
    root = next((key for key in prior['question_ids'] if key in roots), default_root) if prior else default_root
    question_id, objects = _question_parent(reg, args['question_id'], parents, origin, root)
    content['support_status'] = 'pending_review'
    objects.append(_object(ident, kind, prior, origin, content, sources=refs, parents=parents, questions=[question_id]))
    return objects


def _append(reg, pid, write, objects, max_bytes, max_items):
    staged = []
    for obj in objects:
        delivery = {**obj['origin'], 'id': obj['id'], 'revision': obj['revision'],
                    'tool': 'propose_memory', 'thread': write['thread'], 'at': write['at'],
                    'role': 'lead' if write['thread'] == reg.call['thread_id'] else 'subagent',
                    'call_id': write['call_id'], 'invocation_id': pid}
        staged.extend([('object', obj), ('delivery', delivery)])
    size = sum(len(ledger.encode(value)) for _, value in staged)
    require(reg.bytes + size <= max_bytes and sum(reg.accepted.values()) + len(objects) <= max_items,
            'registration capacity exceeded')
    reg.rows.extend(staged)
    reg.bytes += size
    for obj in objects:
        reg.latest[obj['id']] = obj
        reg.created[obj['kind']] += 1
        reg.accepted[obj['kind']] += 1


def register(reg, pending, current, max_bytes, max_items):
    """Stage accepted interpretations in the same transaction as the run's automatic literature evidence."""
    files = {}
    for name in reg.call.get('files', {}):
        if name.startswith('proposals/'):
            m = PROPOSAL_FILE.fullmatch(name)
            if m:
                files[m[1]] = name
            else:
                reg.reject('invalid proposal filename')
    if not files:
        # The literature pass has already inspected the sealed native events. An ordinary
        # task without proposals needs neither a second session scan nor a snapshot replay.
        for pid in sorted(pending):
            reg.reject('native submission lacks a sealed proposal attachment', proposal_id=pid)
        return
    history_error = None
    try:
        trusted = trusted_rows(reg.state, reg.call['memory'])
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        trusted = []
        history_error = f'starting snapshot unavailable: {str(exc)[:120]}'
    try:
        reads, writes = _proofs(reg, trusted, current)
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        for pid in files:
            reg.reject(f'proposal receipts unavailable: {str(exc)[:120]}', proposal_id=pid)
        return
    for pid in sorted(files.keys() - writes.keys()):
        reg.reject('no completed native submission for the sealed proposal', proposal_id=pid)
    # Process concurrent submissions deterministically; competing writes to the same revision admit one.
    contract = _task_contract(reg)
    for pid in sorted(writes, key=lambda key: (writes[key][0]['at'], writes[key][0]['position'], key)):
        deadline_check(reg.deadline)
        try:
            require(pid in files, 'native submission lacks a sealed proposal attachment')
            require(len(writes[pid]) == 1, 'ambiguous proposal ID returned by multiple native calls')
            write, = writes[pid]
            raw = attachment(reg.layout, reg.call, files[pid], MAX_FILE_BYTES)
            args = json.loads(raw)
            require(ledger.encode(args) == ledger.encode(write['args']), 'sealed proposal differs from native arguments')
            _append(reg, pid, write, _objects(reg, args, reads, write, history_error, contract), max_bytes, max_items)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            reg.reject(str(exc)[:160], proposal_id=pid)
