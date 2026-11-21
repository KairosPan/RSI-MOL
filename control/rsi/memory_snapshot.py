"""Place a bounded, task-filtered Scientific Memory snapshot into a private staged workspace."""

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from rsi import memory_store
from rsi.memory_receipts import original_problem
from rsi.paths import Layout

SNAPSHOT_BYTES = 16 * 1024 * 1024
STATUS_RESERVE = 1024


def _encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8') + b'\n'


def _reference_address(ref):
    invocation = ref['invocation_id'] if ref.get('delivery_source') == 'literature' else ref['historical_invocation']
    return ref['source_run'], ref['document_id'], ref['document_version'], invocation


def _delivery_address(d):
    return d['run'], d['document_id'], d['document_version'], d['invocation_id']


def _dependencies(obj, rows):
    """Resolve each authored reference to its exact earlier revision and, for quotes, its visible delivery."""
    refs = obj.get('object_refs', ())
    required = {(ref['id'], ref['revision']) for ref in refs}
    if not required.issubset(rows):
        return None
    questions = {key[0] for key in required if rows[key]['object']['kind'] == 'question'}
    if not set(obj.get('question_ids', ())).issubset(questions):
        return None
    for ref in obj.get('source_refs', ()):
        key = ref['id'], ref['revision']
        passage = rows.get(key)
        if passage is None:
            return None
        content = passage['object']['content']
        if (content['type'] != 'passage' or content['source_sha256'] != ref['source_sha256']
                or content['locator'] != ref['locator'] or not ref['quote'] or ref['quote'] not in content['text']):
            return None
        # Pin the cited delivery even when the same passage is available from another run or document alias.
        if ref.get('delivery_source') == 'literature' and ref['source_run'] != obj['origin']['run']:
            return None
        address = _reference_address(ref)
        if not any(_delivery_address(d) == address for d in passage['deliveries']):
            return None
        required.add(key)
    return required


def _select(ordered, dependencies, sizes, budget):
    """Prefer recent visible objects, admitting a revision only when all its dependencies also fit."""
    selected = set()
    for key in ordered:
        pending = [key]
        bundle = set()
        cost = 0
        while pending:
            node = pending.pop()
            if node in selected or node in bundle:
                continue
            cost += sizes[node]
            if cost > budget:
                break
            bundle.add(node)
            pending.extend(dependencies[node])
        else:
            selected.update(bundle)
            budget -= cost
    return selected


REVIEW_REASONS = {'newer_visible_revision', 'source_changed', 'dependency_needs_review'}
UNAVAILABLE_REASONS = {'original_unavailable', 'withdrawn', 'dependency_unavailable'}
# Reserve a bounded annotation before admission, so omitted originals never need to be checked or recorded.
FRESHNESS_RESERVE = len(_encode(dict(freshness='needs_review',
                                      freshness_reasons=sorted(REVIEW_REASONS | UNAVAILABLE_REASONS))))
LEVELS = ('current', 'needs_review', 'unavailable')


def _freshness(reasons):
    if not reasons:
        return {'freshness': 'current'}
    level = 'unavailable' if UNAVAILABLE_REASONS & reasons else 'needs_review'
    return {'freshness': level, 'freshness_reasons': sorted(reasons)}


def _annotate_freshness(rows, owner_events, unavailable, check_original):
    """Compute status after selection. Each citation inherits the health of its pinned delivery."""
    newest = {}
    for key in rows:
        newest[key[0]] = max(key[1], newest.get(key[0], 0))
    if check_original:
        originals = {r['object']['content'].get('source_sha256') for r in rows.values()}
        originals.update(d['derived_from']['source_sha256'] for r in rows.values()
                         for d in r['deliveries'] if d.get('derived_from'))
        for sha in originals - {None}:
            if check_original(sha):
                unavailable.add(sha)
    changed = {e['source_sha256'] for e in owner_events if e['type'] == 'source_changed'}
    withdrawn = {(e['id'], e['revision']) for e in owner_events if e['type'] == 'withdrawn'}

    def source_reasons(sha):
        reasons = set()
        if sha in changed:
            reasons.add('source_changed')
        if sha in unavailable:
            reasons.add('original_unavailable')
        return reasons

    delivered = {}
    for key, row in rows.items():  # dependencies precede their dependents in the ledger
        obj = row['object']
        reasons = source_reasons(obj['content'].get('source_sha256'))
        if newest[key[0]] > key[1]:
            reasons.add('newer_visible_revision')
        if key in withdrawn:
            reasons.add('withdrawn')
        inherited = {rows[ref['id'], ref['revision']]['freshness'] for ref in obj.get('object_refs', ())}
        inherited.update(delivered[(ref['id'], ref['revision']), _reference_address(ref)]
                         for ref in obj.get('source_refs', ()))
        if 'unavailable' in inherited:
            reasons.add('dependency_unavailable')
        if 'needs_review' in inherited:
            reasons.add('dependency_needs_review')
        row.update(_freshness(reasons))
        if obj['content']['type'] == 'passage':
            paths = []
            for index, d in enumerate(row['deliveries']):
                parent = (d.get('derived_from') or {}).get('source_sha256')
                status = _freshness(reasons | source_reasons(parent))
                delivered[key, _delivery_address(d)] = status['freshness']
                if parent:
                    row['deliveries'][index] = {**d, **status}
                paths.append(status)
            # An address-free lookup can use the best visible delivery. Pinned citations select their own path.
            row.update(min(paths, key=lambda s: LEVELS.index(s['freshness'])))


def _project(state, condition: str, task: str | None, budget: int, *, unavailable=None, check_original=None):
    """Derive each index, origin and capacity choice from this run's visible revisions and deliveries."""
    task_filter = task if condition == 'trial' else None
    excluded = Counter()
    omitted = Counter()
    included = Counter()
    by_revision = defaultdict(list)
    if condition != 'none':
        for delivery in state.deliveries:
            if task_filter is not None and delivery['task'] == task_filter:
                excluded['deliveries'] += 1
            else:
                by_revision[delivery['id'], delivery['revision']].append(delivery)

    rows, dependencies, ranks = {}, {}, {}
    # Registration appends referenced revisions before their dependents. A missing dependency
    # propagates through later revisions, including an authored object's pinned question.
    for obj in state.objects if condition != 'none' else ():
        key = obj['id'], obj['revision']
        deliveries = by_revision.get(key, [])
        authored = obj['kind'] != 'evidence'
        required = _dependencies(obj, rows) if authored else ()
        if not deliveries or required is None or (authored and task_filter is not None
                                                  and obj['origin']['task'] == task_filter):
            excluded[obj['content']['type']] += 1
            if deliveries:
                excluded['deliveries'] += len(deliveries)
            continue
        # A requested historical backfill may have been registered out of run order.
        deliveries.sort(key=lambda d: (d['at'], d['seq']))
        origin = {k: deliveries[0][k] for k in ('run', 'task', 'version')}
        view = dict(obj)
        if not authored and view['origin'] != origin:
            view['origin'] = origin
            view.pop('record_sha256', None)
        row = dict(object=view, deliveries=deliveries)
        rows[key] = row
        dependencies[key] = required
        ranks[key] = deliveries[-1]['at'], deliveries[-1]['seq'], obj['id']

    sizes = {key: len(_encode(row)) for key, row in rows.items()}
    if unavailable is not None:
        for key, row in rows.items():
            parents = sum(bool(d.get('derived_from')) for d in row['deliveries'])
            sizes[key] += FRESHNESS_RESERVE * (1 + parents)
    ordered = sorted(rows, key=ranks.__getitem__, reverse=True)
    selected_keys = _select(ordered, dependencies, sizes, budget)
    if unavailable is not None:
        visible = {key: row for key, row in rows.items() if key in selected_keys}
        _annotate_freshness(visible, getattr(state, 'owner_events', ()), unavailable, check_original)
    selected = []
    for key in ordered:
        row = rows[key]
        obj = row['object']
        counts = included if key in selected_keys else omitted
        counts[obj['content']['type']] += 1
        counts['deliveries'] += len(row['deliveries'])
        if key in selected_keys:
            previous = obj.get('supersedes')
            if previous is not None and (obj['id'], previous) not in selected_keys:
                del obj['supersedes']
                obj.pop('record_sha256', None)
            selected.append(row)
    status = {'condition': condition, 'task_filter': task_filter, 'version': state.version,
              'counts': dict(included), 'excluded': dict(excluded), 'omitted': dict(omitted)}
    return status, selected


def place_snapshot(layout: Layout, staged_work: Path, condition: str = 'live', task: str | None = None,
                   *, max_bytes: int = SNAPSHOT_BYTES) -> dict:
    """Return the memory field for the run; only call on a private checkout before moving it into place."""
    if condition not in {'live', 'trial', 'none'} or max_bytes < STATUS_RESERVE:
        raise ValueError('Invalid memory snapshot condition or capacity')
    state = memory_store.load(layout)
    unavailable = set()
    status, rows = _project(state, condition, task, max_bytes - STATUS_RESERVE,
                            unavailable=unavailable, check_original=lambda sha: original_problem(layout, sha))
    header = _encode(status)
    if len(header) > STATUS_RESERVE:
        raise ValueError('Memory snapshot status exceeds its limit')
    folder = staged_work / 'memory'
    folder.mkdir()
    digest = hashlib.sha256()
    (folder / 'status.json').write_bytes(header)
    digest.update(b'status.json\0' + header)
    if condition != 'none':
        digest.update(b'evidence.jsonl\0')
        with (folder / 'evidence.jsonl').open('xb') as stream:
            for row in rows:
                data = _encode(row)
                stream.write(data)
                digest.update(data)
    # The owner replays the original observation even if a file disappears or is restored later.
    return {**status, 'fingerprint': digest.hexdigest(), 'projection_version': 2, 'max_bytes': max_bytes,
            'unavailable_sources': sorted(unavailable)}
