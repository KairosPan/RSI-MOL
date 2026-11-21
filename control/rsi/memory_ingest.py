"""Register literature evidence and checked, explicitly attributed interpretations in a single run batch."""
from collections import Counter, defaultdict
import hashlib
import re
import time

from rsi import memory_proposals, memory_store as ledger
from rsi.memory_receipts import deadline_check, native_calls, original_problem, service_source, session_paths
from rsi.paths import Layout
from rsi.records import Records

TIME_LIMIT = 120
MAX_ITEMS = 4096
MAX_TOTAL = 16 << 20
MAX_PASSAGE = 128 << 10
DOC_ID = re.compile(r'doc_[0-9a-f]{64}')
LOCAL_ERRORS = {'invalid_arguments': 'invalid arguments', 'runtime_unavailable': 'local runtime unavailable',
                'source_storage_error': 'local archive failure', 'response_too_large': 'response too large',
                'log_unavailable': 'local log failure', 'internal_error': 'local service error',
                'trial_cache_miss': 'local cache miss', 'unknown_source': 'unknown local source',
                'configuration_required': 'local configuration required'}


def _string(row, field, limit=2048, optional=False):
    value = row.get(field)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f'invalid {field}')
    return value


def _address(row):
    doc = _string(row, 'document_id')
    version = _string(row, 'document_version')
    source = _string(row, 'source_sha256')
    if not (DOC_ID.fullmatch(doc) and ledger.DIGEST.fullmatch(version) and ledger.DIGEST.fullmatch(source)):
        raise ValueError('invalid document address')
    return {'document_id': doc, 'document_version': version, 'source_sha256': source}


def _strings(value, field, *, maximum=128):
    if not isinstance(value, list) or len(value) > maximum or not all(isinstance(s, str) and len(s) <= 4096 for s in value):
        raise ValueError(f'invalid {field}')
    return value


def _range(row, key, size):
    pair = row.get(key)
    if (not isinstance(pair, list) or len(pair) != 2 or not all(type(x) is int for x in pair)
            or pair[0] < 0 or pair[1] - pair[0] != size):
        raise ValueError(f'invalid {key}')
    return pair


def _passage(row, original):
    if row.get('block_complete') is False:
        raise ValueError('partial block')
    if row.get('block_complete') is not True:
        raise ValueError('invalid block_complete')
    text = _string(row, 'text', MAX_PASSAGE)
    data = text.encode()
    address = _address(row)
    meta = row.get('source_metadata')
    if not isinstance(meta, dict):
        raise ValueError('invalid source_metadata')
    parent = meta.get('derived_from')
    if parent is None and ('parser_identity' in meta or 'extraction_coverage' in meta):
        raise ValueError('missing derived document parent')
    if 'derived_from' in meta:
        if not isinstance(parent, dict):
            raise ValueError('invalid derived document parent')
        parent = _address(parent)
        original(parent['source_sha256'])
    original(address['source_sha256'])
    body = {'type': 'passage', 'text': text, 'text_sha256': hashlib.sha256(data).hexdigest(),
            'source_sha256': address['source_sha256'], 'parser_version': _string(row, 'parser_version'),
            'locator': _string(row, 'locator'), 'kind': _string(row, 'kind'),
            'section': _string(row, 'section'), 'section_path': _strings(row.get('section_path'), 'section_path'),
            'char_range': _range(row, 'char_range', len(text)), 'byte_range': _range(row, 'byte_range', len(data))}
    extra = {key: _string(meta, key, 4096, optional=True) for key in ('paper_ref', 'source_id', 'provider', 'source_url')}
    extra.update(address, content_scope=_string(row, 'content_scope'), derived_from=parent)
    key = [body[name] for name in ('source_sha256', 'parser_version', 'locator')]
    return key, body, extra


def _paper(row):
    paper_id, provider = _string(row, 'paper_id'), _string(row, 'provider')
    if not paper_id.startswith(provider + ':') or not paper_id[len(provider) + 1:]:
        raise ValueError('invalid source paper ID')
    body = {k: _string(row, k, 8192, optional=True) for k in ('doi', 'pmid', 'pmcid', 'title')}
    year = row.get('year')
    if year is not None and type(year) not in (str, int):
        raise ValueError('invalid year')
    body.update(type='paper', provider=provider, paper_id=paper_id, year=year)
    return paper_id, body, {}


def _attempts(prov):
    attempts = prov.get('download_attempts', [])
    if not isinstance(attempts, list) or len(attempts) > 256 or not all(isinstance(v, dict) for v in attempts):
        raise ValueError('invalid download_attempts')
    cleaned = []
    for row in attempts:
        attempt = {k: _string(row, k, optional=True) for k in ('format', 'host', 'outcome') if k in row}
        if 'http_status' in row:
            if type(row['http_status']) is not int:
                raise ValueError('invalid download HTTP status')
            attempt['http_status'] = row['http_status']
        cleaned.append(attempt)
    return cleaned


def _acquisition(run, args, rec, original):
    data, prov = rec['data'], rec.get('provenance')
    if not isinstance(prov, dict):
        raise ValueError('invalid acquisition provenance')
    errors = rec.get('errors', [])
    if not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors):
        raise ValueError('invalid acquisition errors')
    code = _string(errors[0], 'code') if errors else None
    if code in LOCAL_ERRORS:
        raise ValueError(LOCAL_ERRORS[code])
    attempts = _attempts(prov)
    success = rec['status'] in ('ok', 'partial') or (data.get('document_id') and data.get('source_artifact') and not code)
    if not success:
        if code in ('fulltext_format_unavailable', 'fulltext_location_unavailable'):
            stage = 'candidate_resolution'
        elif code == 'fulltext_unavailable' and any((attempt.get('http_status') or 0) >= 400 for attempt in attempts):
            stage = 'source_http'
        else:
            raise ValueError('unknown failure stage')
    ref = _string(args, 'paper_ref', optional=True)
    source_id = _string(args, 'source_id', optional=True)
    if bool(ref) == bool(source_id):
        raise ValueError('invalid acquisition request identity')
    body = {'type': 'acquisition', 'paper_ref': ref, 'source_id': source_id,
            'provider': _string(args, 'provider', optional=True) or _string(prov, 'provider'),
            'download_attempts': attempts, 'download_hosts': _strings(prov.get('download_hosts', []), 'download_hosts')}
    if success:
        address = _address(data)
        original(address['source_sha256'])
        coverage = rec.get('coverage')
        if not isinstance(coverage, dict):
            raise ValueError('invalid acquisition coverage')
        verified = coverage.get('verified_paper_identifiers')
        if verified is not None:
            _strings(verified, 'verified_paper_identifiers')
        meta = data.get('source_metadata') or {}
        body.update(address, media_type=_string(data, 'media_type'), content_scope=_string(data, 'content_scope'),
                    source_url=_string(meta, 'source_url', 4096, optional=True), status='acquired',
                    verified_paper_identifiers=verified)
    else:
        body.update(status='unavailable', stage=stage, errors=[{'code': code, 'detail': _string(errors[0], 'detail', 4096, optional=True)}])
    return [run, rec['invocation_id']], body, {}


def _earlier_papers(state, run):
    """Find the metadata in effect before this run, even if earlier batches were backfilled out of order."""
    before = int(run[1:])
    latest = {}
    for d in state.deliveries:
        ident = d['id']
        if not ident.startswith('paper_'):
            continue
        delivered = int(d['run'][1:]), d['seq'], d['revision']
        if delivered[0] < before and (ident not in latest or delivered > latest[ident]):
            latest[ident] = delivered
    return {o['id']: o for o in state.objects if o['id'] in latest and o['revision'] == latest[o['id']][2]}


class _Registration:
    def __init__(self, layout, state, call):
        self.layout = layout
        self.call = call
        self.state = state
        self.deadline = time.monotonic() + TIME_LIMIT
        self.source = service_source(layout, call)
        self.rows = []
        self.latest = {v['id']: v for v in state.objects}
        self.earlier_papers = _earlier_papers(state, call['id'])
        self.rejections = []
        self.reasons = Counter()
        self.accepted = Counter()
        self.created = Counter()
        self.checked = {}
        self.bytes = 0

    def reject(self, reason, **where):
        self.reasons[reason] += 1
        # One diagnostic per rejected return up to the run's item bound; all counts remain exact.
        if len(self.rejections) < MAX_ITEMS:
            self.rejections.append({'reason': reason, **where})

    def original(self, address):
        if address not in self.checked:
            self.checked[address] = original_problem(self.layout, address)
        if self.checked[address]:
            raise ValueError(self.checked[address])

    def add(self, key, content, extra, delivery):
        subtype = content['type']
        ident = subtype + '_' + ledger.digest(key)
        last_registered = self.latest.get(ident)
        prior = self.earlier_papers.get(ident) if subtype == 'paper' else last_registered
        if prior is None and last_registered and last_registered['content'] == content:
            # A later run already delivered these same bytes. Preserve its immutable revision pin.
            prior = last_registered
        if prior and subtype == 'passage' and prior['content'] != content:
            raise ValueError('conflicting text or coordinates at the same passage address')
        new = not prior or prior['content'] != content
        if new:
            rev = last_registered['revision'] + 1 if last_registered else 1
        else:
            rev = prior['revision']
        body = {**delivery, **extra, 'id': ident, 'revision': rev}
        size = len(ledger.encode(body)) + (len(ledger.encode(content)) if new else 0)
        if self.bytes + size > MAX_TOTAL or sum(self.accepted.values()) >= MAX_ITEMS:
            raise ValueError('registration capacity exceeded')
        self.bytes += size
        if new:
            origin = {k: delivery[k] for k in ('run', 'task', 'version')}
            obj = {'schema_version': 'rsi_mol_memory_v1', 'id': ident, 'kind': 'evidence', 'revision': rev,
                   'supersedes': prior['revision'] if prior else None, 'content': content, 'origin': origin,
                   'question_ids': [], 'source_refs': [], 'object_refs': [], 'authority': 'receipt'}
            obj['record_sha256'] = ledger.digest(obj)
            self.latest[ident] = obj
            self.rows.append(('object', obj))
            self.created[subtype] += 1
        if subtype == 'paper':
            self.earlier_papers[ident] = obj if new else prior
        self.rows.append(('delivery', body))
        self.accepted[subtype] += 1
        return body

    def run(self):
        pending = set() if 'task_goal' in self.call else None
        current = defaultdict(list)
        calls = native_calls(self.layout, self.call, self.reject, self.deadline, pending)
        for at, thread, role, tool, args, rec, native_id in calls:
            base = {'run': self.call['id'], 'task': self.call['task'], 'version': self.call.get('version_before'),
                    'thread': thread, 'role': role, 'at': at, 'tool': tool, 'invocation_id': rec['invocation_id'], 'service': self.source}
            data = rec['data']
            if tool in ('find_passages', 'read_document'):
                rows = data.get('passages')
            elif tool == 'acquire_fulltext':
                rows = [data]
            elif tool == 'get_paper':
                rows = [data.get('paper')]
            else:
                rows = data.get('papers')
            if not isinstance(rows, list):
                self.reject('missing result rows', invocation_id=rec['invocation_id'])
                continue
            within_call = set()
            for index, row in enumerate(rows):
                deadline_check(self.deadline)
                try:
                    if not isinstance(row, dict):
                        raise ValueError('invalid result row')
                    if tool in ('find_passages', 'read_document'):
                        result = _passage(row, self.original)
                    elif tool == 'acquire_fulltext':
                        result = _acquisition(base['run'], args, rec, self.original)
                    else:
                        result = _paper(row)
                    signature = ledger.digest(result)
                    if signature not in within_call:
                        delivery = self.add(*result, base)
                        if pending and result[1]['type'] == 'passage' and isinstance(native_id, str):
                            # Only a successfully staged delivery can support a current-run citation.
                            current[thread, native_id].append(delivery)
                        within_call.add(signature)
                except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                    self.reject(str(exc)[:180], invocation_id=rec['invocation_id'], row=index)
        memory_proposals.register(self, pending or (), current, MAX_TOTAL, MAX_ITEMS)
        summary = {'service': self.source, 'objects': dict(self.created), 'deliveries': dict(self.accepted),
                   'delivery_count': sum(self.accepted.values()), 'reasons': dict(self.reasons), 'rejected': self.rejections}
        deadline_check(self.deadline)
        return ledger.publish(self.layout, self.state, self.call['id'], self.rows, summary)


def eligible(call):
    memory = call.get('memory') or {}
    return (call.get('purpose') == 'task' and call.get('status') in ('finished', 'failed', 'timed_out')
            and memory.get('condition', 'live') != 'none' and bool(session_paths(call)))


def _calls(layout):
    return Records(layout.records).calls() if layout.records.is_dir() else []


def backfill(layout: Layout, run_ids=None):
    """Register eligible calls in run order. Explicitly requested, already registered calls are idempotent."""
    state = ledger.load(layout)
    calls = _calls(layout)
    requested = set(run_ids or [])
    unknown = requested - {call['id'] for call in calls}
    if unknown:
        raise ValueError(f'Unknown registration run: {", ".join(sorted(unknown))}')
    reports = []
    if not requested:
        for owner in sorted(state.missing_owner_reports, key=lambda key: state.owner_reports[key]['version_after']):
            reports.append(ledger.finish_report(layout, state, owner))
    for call in calls:
        run = call['id']
        if requested and run not in requested:
            continue
        if run in state.reports:
            if run in state.missing_reports or requested:
                reports.append(ledger.finish_report(layout, state, run))
        elif eligible(call):
            reports.append(_Registration(layout, state, call).run())
    return reports


def status(layout: Layout):
    state = ledger.load(layout)
    counts = {kind: len({o['id'] for o in state.objects if o['content']['type'] == kind})
              for kind in (*ledger.TYPES, *memory_proposals.CONTENT_FIELDS)}
    pending = sum(eligible(c) and (c['id'] not in state.reports or c['id'] in state.missing_reports) for c in _calls(layout))
    return {'version': state.version, 'counts': counts, 'deliveries': len(state.deliveries), 'pending': pending,
            'owner_actions': len(state.owner_events), 'pending_owner_reports': len(state.missing_owner_reports),
            'recent': [{k: r[k] for k in ('run', 'version_after', 'delivery_count')} for r in list(state.reports.values())[-5:]]}


def verify(layout: Layout):
    state = ledger.load(layout)
    missing = state.missing_reports | state.missing_owner_reports
    if missing:
        raise ValueError('Missing registration report for ' + ', '.join(sorted(missing)))
    originals = ledger.source_hashes(state)
    for sha in sorted(originals):
        if problem := original_problem(layout, sha):
            raise ValueError(f'{sha}: {problem}')
    return {'version': state.version, 'objects': len(state.objects), 'deliveries': len(state.deliveries),
            'originals': len(originals), 'runs': len(state.reports), 'owner_actions': len(state.owner_events)}
