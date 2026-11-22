"""Owner-side M6 comparison: match final quotes to the two runs' actual tool returns."""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'control'))
from rsi.memory_history import (ADDRESS, ObjectPartReads, attachment, passage_index, read, require,
                                returned_memory, timeline, trusted_rows)
from rsi.memory_store import load
from rsi.paths import Layout
from rsi.records import CALL_ID

TASK = 'paired-validation-followup'
BACKFILL_RUNS = {'R0009', 'R0010', 'R0012', 'R0013'}
CATEGORIES = ('memory', 'literature', 'both', 'unmatched')


def completions(layout, call):
    for _, _, thread, item, _ in timeline(layout, call):
        yield thread, item


def receipts(events, rows):
    counts = Counter()
    memory, literature = defaultdict(list), defaultdict(list)
    trusted = passage_index(rows)
    parts_read = ObjectPartReads(rows)
    for thread, item in events:
        name, tool = item['server'], item['tool']
        counts[name, tool] += 1
        reply = item.get('result') or {}
        if item.get('status') != 'completed' or reply.get('isError'):
            continue
        for part in reply.get('content', []):
            if part.get('type') != 'text':
                continue
            try:
                result = json.loads(part['text'])
            except (ValueError, TypeError):
                continue
            if not isinstance(result, dict):
                continue
            call = {'thread': thread, 'call_id': item['id'], 'tool': tool}
            if name == 'memory':
                if len(reply['content']) == 1:
                    result, _ = parts_read.verify(item, result, call)
                for address, match in returned_memory(tool, result, trusted):
                    memory[address].append({**call, **match})
            elif tool in ('read_document', 'find_passages') and result.get('status') in ('ok', 'partial'):
                for passage in result.get('data', {}).get('passages', []):
                    if isinstance(passage.get('text'), str):
                        address = tuple(passage.get(k) for k in ADDRESS)
                        literature[address].append({**call, 'text': passage['text'],
                                                     'invocation_id': result.get('invocation_id'),
                                                     'char_range': passage.get('char_range'),
                                                     'byte_range': passage.get('byte_range'),
                                                     'parser_version': passage.get('parser_version')})
    return counts, memory, literature


def literature_matches(quote, passages):
    """Match one return or contiguous, mutually consistent slices of this exact block in one thread."""
    singles = [p for p in passages if quote in p['text']]
    if singles:
        return singles
    groups = defaultdict(list)
    needle = quote.encode()
    for p in passages:
        chars, offsets = p.get('char_range'), p.get('byte_range')
        if any(not isinstance(r, list) or len(r) != 2 or not all(type(i) is int for i in r) or
               not 0 <= r[0] < r[1] for r in (chars, offsets)):
            continue
        raw = p['text'].encode()
        if chars[1] - chars[0] == len(p['text']) and offsets[1] - offsets[0] == len(raw):
            groups[p['thread'], p.get('parser_version')].append((chars, offsets, raw, p))
    for spans in groups.values():
        data = bytearray()
        used = []
        end_char = base = 0
        for (lo, hi), (start, end), raw, p in sorted(spans, key=lambda s: (s[0][0], -s[0][1])):
            end_byte = base + len(data)
            if data and hi <= end_char and base <= start <= end <= end_byte:
                continue  # A covered slice cannot add text to this chain.
            overlap = end_byte - start
            joins = (data and lo <= end_char < hi and base <= start <= end_byte and
                     len(p['text'][:end_char-lo].encode()) == overlap and data[start-base:] == raw[:overlap])
            if not joins:
                data.clear()
                used.clear()
                base, overlap = start, 0
            old_size = len(data)
            data.extend(raw[overlap:])
            used.append(p)
            end_char = hi
            position = data.find(needle, max(0, old_size - len(needle) + 1))
            if position >= 0:
                begin, stop = base + position, base + position + len(needle)
                return [m for m in used if m['byte_range'][0] < stop and m['byte_range'][1] > begin]
    return []


def compare_citations(rows, memory, literature):
    classified = []
    for n, row in enumerate(rows, 1):
        fields = (*ADDRESS, 'quote')
        missing = [k for k in fields if not isinstance(row, dict) or not isinstance(row.get(k), str)
                   or not row[k].strip()]
        if missing:
            classified.append({'row': n, 'observed': None, 'audit_error': 'Missing or invalid ' + ', '.join(missing),
                               'backfilled_memory_citation': False})
            continue
        quote = row['quote']
        address = tuple(row[k] for k in ADDRESS)
        mem = [m for m in memory.get(address, []) if quote in m['text']]
        lit = literature_matches(quote, literature.get(address, []))
        declared = row.get('delivery_source')
        observed = ('both' if lit else 'memory') if mem else ('literature' if lit else 'unmatched')
        confirmed = [m for m in mem if (m['id'], m['revision'], m['source_run']) ==
                     (row.get('evidence_id'), row.get('revision'), row.get('source_run'))
                     and type(row.get('revision')) is int]
        def details(matches):
            return [{k: v for k, v in m.items() if k != 'text'} for m in matches]
        classified.append({'row': n, **{k: row[k] for k in ADDRESS}, 'quote': quote, 'declared': declared,
                           'observed': observed, 'label_matches': observed == declared,
                           'memory_receipts': details(mem), 'literature_receipts': details(lit),
                           'memory_provenance_matches': bool(confirmed),
                           'backfilled_memory_citation': any(m['source_run'] in BACKFILL_RUNS for m in confirmed)})
    return classified


def final_citations(layout, call, memory, literature):
    """Missing or malformed deliverables are observations. Digest failures on sealed files remain audit failures."""
    name = 'out/evidence.json'
    error = None
    rows = None
    if name not in call['files']:
        error = 'No final evidence.json recorded'
    else:
        raw = attachment(layout, call, name, 256 << 10)
        try:
            rows = json.loads(raw)
            require(isinstance(rows, list), 'Expected a final evidence array')
        except (ValueError, RecursionError) as exc:
            error = str(exc)
            rows = None
    classified = compare_citations(rows or [], memory, literature)
    auditable = sum(r['observed'] is not None for r in classified)
    coverage = {'rows': len(rows) if rows is not None else None, 'auditable_rows': auditable,
                'status': 'unavailable' if error else 'partial' if auditable != len(classified) else 'complete'}
    if error:
        coverage['error'] = error
    return classified, coverage


def arm(layout, run, condition, state):
    call = json.loads(read(layout.root, f'records/runs/{run}/call.json', 2 << 20))
    require(call['id'] == run and call['purpose'] == 'task' and call['task'] == TASK and
            call['status'] == 'finished' and call['memory']['condition'] == condition,
            f'Expected a completed {condition} run of {TASK}: {run}')
    rows = trusted_rows(state, call['memory'])
    counts, memory, literature = receipts(completions(layout, call), rows)
    classified, coverage = final_citations(layout, call, memory, literature)
    observed = Counter(r['observed'] for r in classified)
    elapsed = (datetime.fromisoformat(call['ended']) - datetime.fromisoformat(call['started'])).total_seconds()
    return {'run': run, 'task': call['task'], 'tree': call['tree'], 'version_before': call['version_before'],
            'memory': call['memory'], 'memory_calls': sum(n for (server, _), n in counts.items() if server == 'memory'),
            'literature_calls': sum(n for (server, _), n in counts.items() if server == 'literature'),
            'acquire_fulltext': counts['literature', 'acquire_fulltext'], 'read_document': counts['literature', 'read_document'],
            'find_passages': counts['literature', 'find_passages'], 'lead_usage': call.get('usage'),
            'elapsed_seconds': elapsed, 'check': call.get('check'), 'citations_by_delivery': {k: observed[k] for k in CATEGORIES},
            'citation_coverage': coverage, 'citations': classified}


def audit(none_dir, live_dir):
    none_dir, live_dir = Path(none_dir).absolute(), Path(live_dir).absolute()
    require(CALL_ID.fullmatch(none_dir.name) and CALL_ID.fullmatch(live_dir.name) and
            none_dir.parent == live_dir.parent and none_dir.parent.parts[-2:] == ('records', 'runs'),
            'Supply two recorded run directories under the same records/runs folder')
    require(int(none_dir.name[1:]) < int(live_dir.name[1:]), 'The no-memory run must precede the live run')
    layout = Layout(none_dir.parents[2])
    state = load(layout)
    none = arm(layout, none_dir.name, 'none', state)
    live = arm(layout, live_dir.name, 'live', state)
    same_tree = bool(none['tree']) and none['tree'] == live['tree']
    matched = any(row['backfilled_memory_citation'] for row in live['citations'])
    return {'passed': same_tree and matched, 'same_starting_tree': same_tree,
            'confirmed_backfilled_memory_citation': matched, 'none': none, 'live': live,
            'measurement_scope': 'Token usage covers the lead only. Both runs share library/.',
            'receipt_scope': 'Classification checks delivery of the quoted text and its address; '
                             'scientific support is reviewed separately.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('none_run')
    parser.add_argument('live_run')
    args = parser.parse_args()
    try:
        report = audit(args.none_run, args.live_run)
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        report = {'passed': False, 'error': str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report['passed'] else 1)
