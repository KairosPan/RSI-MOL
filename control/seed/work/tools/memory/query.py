"""Deterministic lookups over the revisions and deliveries in this run's snapshot."""

import json
import re
from collections import defaultdict

NOTICE = ('Historical text returned by the literature server. The original file hash was checked. '
          'This passage has not been independently checked against the original.')
DEFAULT_BYTES = 24 * 1024
# Native Codex sessions have truncated MCP results around 50 KB. Size the escaped
# MCP envelope with headroom, including when a caller supplies an older, larger budget.
NATIVE_RECEIPT_BYTES = 32 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
RECENT_DELIVERIES = 3
ID_FIELDS = ('paper_id', 'doi', 'pmid', 'pmcid')


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def normalized(value):
    key = str(value or '').strip().casefold()
    # These provider-relative IDs have unambiguous namespaces, even before a paper is indexed.
    if re.fullmatch(r'(?:med|pmc|ppr|agr|pat|cba):[a-z0-9._-]+', key):
        return 'europe_pmc:' + key
    openalex = re.fullmatch(r'(?:https?://openalex\.org/)?(w\d+)', key)
    if openalex:
        return 'openalex:' + openalex[1]
    if re.fullmatch(r'[0-9a-f]{40}', key):
        return 'semantic_scholar:' + key
    if key.startswith('pmcid:'):
        identifier = key[6:].strip()
        return 'pmc' + identifier if identifier.isdigit() else identifier
    for prefix in ('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'http://dx.doi.org/',
                   'doi:', 'pmid:', 'pubmed:'):
        if key.startswith(prefix):
            return key[len(prefix):].strip()
    return key


def source_identifier(query):
    key = normalized(query)
    return bool(re.fullmatch(r'[a-z][\w.-]*:\S+|10\.\d+/\S+|doc_[\w-]+|pmc\d+|\d{1,12}', key)
                or query.strip().casefold().startswith(('doi:', 'pmid:', 'pmcid:', 'pubmed:')))


def doc_key(doc):
    return tuple(doc.get(k) for k in ('document_id', 'document_version', 'source_sha256'))


def result_views(item):
    """Expand interpretations when there is room. Their own exact revision carries all outbound pins."""
    yield item
    if 'related_objects' not in item:
        return
    details = ('related_objects', 'questions', 'evidence', 'counterevidence')
    summary = {k: v for k, v in item.items() if k not in details}
    counts = {k: len(item[k]) for k in details if item.get(k)}
    if not counts:
        return
    summary['omitted_details'] = counts
    challengers = item.get('counterevidence', ())
    if challengers:
        summary['counterevidence'] = [{k: v for k, v in row.items() if k != 'content'} for row in challengers]
    yield summary
    # Incoming challenges are unbounded. If even their addresses do not all fit,
    # keep the highest-ranked ones and disclose exactly how many addresses are absent.
    while len(summary.get('counterevidence', ())) > 0:
        keep = len(summary['counterevidence']) // 2
        summary = {**summary, 'counterevidence': summary['counterevidence'][:keep],
                   'omitted_details': {**counts, 'counterevidence_addresses': len(challengers) - keep}}
        yield summary


def bounded(items, limit):
    """Keep complete rows within both the requested JSON budget and the escaped MCP receipt budget."""
    empty = encode({'results': [], 'omitted': 0})
    size = len(empty)
    wire = len(encode({'content': [{'type': 'text', 'text': empty.decode()}], 'isError': False}))
    result, sizes = [], []
    omitted = 0
    for item in items:
        for view in result_views(item):
            raw = encode(view)
            extra = len(raw) + int(bool(result))
            # The item is already JSON. Quotes and backslashes are escaped again
            # when it becomes the text of an MCP content block.
            escaped = extra + raw.count(b'"') + raw.count(b'\\')
            if size + extra <= limit and wire + escaped <= NATIVE_RECEIPT_BYTES:
                result.append(view)
                sizes.append((extra, escaped))
                size += extra
                wire += escaped
                break
        else:
            omitted += 1
    while result and len(str(omitted)) - 1 > min(limit - size, NATIVE_RECEIPT_BYTES - wire):
        result.pop()
        extra, escaped = sizes.pop()
        size -= extra
        wire -= escaped
        omitted += 1
    return {'results': result, 'omitted': omitted}


def recent_deliveries(deliveries):
    return {'deliveries': list(reversed(deliveries[-RECENT_DELIVERIES:])), 'delivery_count': len(deliveries)}


class Snapshot:
    def __init__(self, folder):
        with (folder / 'status.json').open('rb') as stream:
            header = stream.read(1025)
        if len(header) > 1024:
            raise ValueError('Oversized memory status')
        self.status = json.loads(header)
        self.versions = {}
        self.latest = {}
        self.acquisitions = []
        self.passages = []
        self.by_doc = defaultdict(list)
        self.by_hash = defaultdict(list)
        self.by_identifier = defaultdict(list)
        self.paper_refs = {}
        self.paper_records = {}
        self.delivery_identifiers = set()
        self.challengers = defaultdict(list)
        paper_history = defaultdict(list)
        if self.status['condition'] != 'none':
            with (folder / 'evidence.jsonl').open('rb') as stream:
                data = stream.read(MAX_SNAPSHOT_BYTES + 1)
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise ValueError('Oversized memory snapshot')
            for line in data.splitlines():
                row = json.loads(line)
                obj = row['object']
                self.versions[obj['id'], obj['revision']] = row
                if obj['content']['type'] == 'paper':
                    paper_history[obj['id']].append(row)
                # A late backfill can give older paper metadata a larger revision number.
                if obj['content']['type'] == 'paper' and 'newer_visible_revision' in row.get('freshness_reasons', ()):
                    continue
                latest = self.latest.get(obj['id'])
                if latest is None or latest['object']['revision'] < obj['revision']:
                    self.latest[obj['id']] = row
        for row in self.latest.values():
            obj = row['object']
            content = obj['content']
            kind = content['type']
            challenged = {(ref['id'], ref['revision']) for ref in obj.get('object_refs', ())
                          if ref['relation'] == 'challenges'}
            for target in challenged:
                self.challengers[target].append(row)
            if kind == 'paper':
                self.index_paper(row, paper_history[obj['id']])
            elif kind == 'acquisition':
                self.acquisitions.append(row)
                if all(doc_key(content)):
                    self.by_doc[doc_key(content)].append(row)
                    self.by_hash[content['source_sha256']].append(row)
            elif kind == 'passage':
                self.passages.append(row)
                self.delivery_identifiers.update(value for d in row['deliveries']
                                                 for value in (normalized(d.get('document_id')), self.request(d)) if value)

    def index_paper(self, head, history):
        """Fill unreported identifiers using compatible observations from this snapshot."""
        def order(row):
            d = row['deliveries'][-1]
            return int(d['run'][1:]), d['at'], d['seq']

        ident = head['object']['id']
        head_order = order(head)
        known = {}
        records = []
        for row in sorted(history, key=order, reverse=True):
            if order(row) > head_order or (row is not head and 'withdrawn' in row.get('freshness_reasons', ())):
                continue
            obj = row['object']
            reported = {k: normalized(obj['content'][k]) for k in ID_FIELDS if obj['content'].get(k)}
            if any(known[k] != reported[k] for k in known.keys() & reported.keys()):
                # A correction is a boundary; do not revive a superseded identifier.
                break
            if row is head or reported.keys() - known.keys():
                records.append(dict(id=ident, revision=obj['revision'],
                                    **{k: obj['content'].get(k) for k in ID_FIELDS}))
                known.update(reported)
            if len(known) == len(ID_FIELDS):
                break
        refs = set(known.values())
        self.paper_refs[ident] = refs
        self.paper_records[ident] = records
        for key in refs:
            self.by_identifier[key].append(head)

    @staticmethod
    def request(content):
        return normalized(content.get('paper_ref') or content.get('source_id'))

    def papers_for(self, row):
        content = row['object']['content']
        return self.by_identifier.get(self.request(content), []) if content.get('paper_ref') else []

    def dois_for(self, row):
        content = row['object']['content']
        requested = normalized(content.get('paper_ref'))
        dois = {normalized(s['doi']) for p in self.papers_for(row)
                for s in self.paper_records[p['object']['id']] if s.get('doi')}
        if requested.startswith('10.') and '/' in requested:
            dois.add(requested)
        return dois - {''}

    def attribution(self, row):
        obj = row['object']
        content = obj['content']
        dois = self.dois_for(row)
        conflicts = []
        for other in self.by_hash.get(content.get('source_sha256'), []):
            if other is row:
                continue
            other_dois = self.dois_for(other)
            if dois and other_dois and dois != other_dois:
                conflicts.append({'id': other['object']['id'], 'paper_ref': other['object']['content'].get('paper_ref'),
                                  'dois': sorted(other_dois)})
        return {'acquisition_id': obj['id'], 'paper_ref': content.get('paper_ref'),
                'source_id': content.get('source_id'), 'provider': content.get('provider'),
                'identifiers_reported_verified': content.get('verified_paper_identifiers') or [],
                'verification': 'reported' if content.get('verified_paper_identifiers') else 'unverified',
                'source_records': [s for p in self.papers_for(row) for s in self.paper_records[p['object']['id']]],
                'conflicting_requests_sharing_file': conflicts, **self.freshness(row)}

    def acquisitions_for(self, delivery, content):
        # A checksum alone cannot establish the requested paper or identify a document alias.
        keys = [doc_key({**delivery, 'source_sha256': content['source_sha256']})]
        requested = self.request(delivery)
        if delivery.get('derived_from'):
            keys.append(doc_key(delivery['derived_from']))
        found = {}
        for key in keys:
            for row in self.by_doc.get(key, []):
                if requested and requested != self.request(row['object']['content']):
                    continue
                found[row['object']['id']] = row
        return found

    def candidates(self, query, *, allow_title=True):
        key = normalized(query)
        papers = list(self.by_identifier.get(key, []))
        if allow_title and not papers and key and not source_identifier(query):
            papers = [row for row in self.latest.values() if row['object']['content']['type'] == 'paper'
                      and key in normalized(row['object']['content'].get('title'))]
        refs = {ref for p in papers for ref in self.paper_refs[p['object']['id']]}
        if key:
            refs.add(key)
        acquisitions = {}
        for row in self.acquisitions:
            obj = row['object']
            content = obj['content']
            if self.request(content) in refs or (key and normalized(content.get('document_id')) == key):
                acquisitions[obj['id']] = row
        return papers, refs, acquisitions

    def selected_deliveries(self, row, task=None, filters=()):
        deliveries, sources = [], {}
        for d in row['deliveries']:
            if task and d['task'] != task:
                continue
            links = self.acquisitions_for(d, row['object']['content'])
            for refs, acquisitions in filters:
                if normalized(d.get('document_id')) in refs or self.request(d) in refs:
                    continue
                links = {key: link for key, link in links.items() if key in acquisitions}
                if not links:
                    break
            else:
                deliveries.append(d)
                sources.update(links)
        return deliveries, sources

    def passage(self, row, deliveries, sources):
        obj = row['object']
        content = obj['content']
        # Filtered requests can expose a narrower set of deliveries than the stored object view.
        return {'id': obj['id'], 'revision': obj['revision'], 'content': content,
                **recent_deliveries(deliveries), 'sources': [self.attribution(r) for r in sources.values()],
                'passage_verification': 'not_independently_checked', 'notice': NOTICE,
                **self.delivery_freshness(row, deliveries)}

    @staticmethod
    def freshness(row):
        return {key: row[key] for key in ('freshness', 'freshness_reasons') if key in row}

    def delivery_freshness(self, row, deliveries):
        paths = (d if 'freshness' in d else row for d in deliveries)
        best = min(paths, key=lambda d: ('current', 'needs_review', 'unavailable').index(d.get('freshness', 'current')))
        return self.freshness(best)

    def reference_rank(self, ref):
        target = self.versions[ref['id'], ref['revision']]
        status = self.citation_freshness(target, ref) if 'source_sha256' in ref else self.freshness(target)
        return (ref['relation'] != 'challenges', status.get('freshness') != 'needs_review')

    def citation_freshness(self, passage, ref):
        invocation = ref['invocation_id'] if ref.get('delivery_source') == 'literature' else ref['historical_invocation']
        address = ref['source_run'], ref['document_id'], ref['document_version'], invocation
        for d in passage['deliveries']:
            if (d['run'], d['document_id'], d['document_version'], d['invocation_id']) == address:
                return self.freshness(d if 'freshness' in d else passage)
        raise ValueError('Citation delivery absent from snapshot')

    def get_evidence(self, evidence_id, revision):
        row = self.latest.get(evidence_id) if revision is None else self.versions.get((evidence_id, revision))
        if row is None:
            return []
        item = {**row, **recent_deliveries(row['deliveries'])}
        kind = row['object']['content']['type']
        if kind == 'passage':
            payload = self.passage(row, *self.selected_deliveries(row))
            item.update({k: payload[k] for k in ('sources', 'notice', 'passage_verification')})
        elif kind == 'acquisition':
            item['attribution'] = self.attribution(row)
        elif row['object']['kind'] != 'evidence':
            obj = row['object']
            item['related_objects'] = []
            item['questions'] = []
            for ref in sorted(obj.get('object_refs', ()), key=self.reference_rank):
                target = self.versions[ref['id'], ref['revision']]
                item['related_objects'].append({'reference': ref, 'object': target['object'], **self.freshness(target)})
                if ref['id'] in obj.get('question_ids', ()):
                    item['questions'].append(target['object'])
            item['evidence'] = []
            for ref in sorted(obj.get('source_refs', ()), key=self.reference_rank):
                target = self.versions[ref['id'], ref['revision']]
                item['evidence'].append({'reference': ref, 'passage': target['object']['content'],
                                         'passage_verification': 'not_independently_checked', 'notice': NOTICE,
                                         **self.citation_freshness(target, ref)})
        challengers = self.challengers.get((evidence_id, row['object']['revision']), ())
        if challengers:
            item['counterevidence'] = [
                {**{k: other['object'][k] for k in ('id', 'revision', 'kind', 'content')}, **self.freshness(other)}
                for other in sorted(challengers, key=lambda r: r.get('freshness') != 'needs_review')]
        return [item]

    def recall_judgments(self, query):
        yield from self.recall_objects(query, 'judgment')

    def recall_objects(self, query, kind=None):
        key = normalized(query)
        words = set(re.findall(r'[^\W_]+', key))
        matches = []
        for row in self.latest.values():
            obj = row['object']
            if kind and obj['kind'] != kind:
                continue
            text = ' '.join(v for v in obj['content'].values() if isinstance(v, str)).casefold()
            identifiers = set(re.findall(r'[^\W_]+', (obj['id'] + ' ' + obj['origin']['task']).casefold()))
            exact = key == normalized(obj['id'])
            lexical = sum(word in text or word in identifiers for word in words)
            if exact or lexical:
                challenge = any(ref['relation'] == 'challenges' for field in ('source_refs', 'object_refs')
                                for ref in obj.get(field, ()))
                rank = (exact, row.get('freshness') == 'needs_review', challenge, lexical,
                        row['deliveries'][-1]['at'], row['deliveries'][-1]['seq'], obj['id'])
                matches.append((rank, obj['id']))
        for _, identity in sorted(matches, reverse=True):
            yield from self.get_evidence(identity, None)

    def lookup(self, query):
        papers, refs, acquisitions = self.candidates(query)
        for paper in papers:
            result = {'type': 'paper', **paper, **recent_deliveries(paper['deliveries'])}
            records = self.paper_records[paper['object']['id']]
            if len(records) > 1:
                result['identifier_records'] = records
            yield result
        for acq in acquisitions.values():
            yield {'type': 'acquisition', **acq, **recent_deliveries(acq['deliveries']), 'attribution': self.attribution(acq)}
        for row in self.passages:
            obj = row['object']
            kept, _ = self.selected_deliveries(row, filters=[(refs, acquisitions)])
            if kept:
                yield {'type': 'passage', 'id': obj['id'], 'revision': obj['revision'],
                       'locator': obj['content']['locator'], 'source_sha256': obj['content']['source_sha256'],
                       'deliveries': [{k: d[k] for k in ('run', 'task', 'document_id', 'document_version')}
                                      for d in reversed(kept[-RECENT_DELIVERIES:])], 'delivery_count': len(kept),
                       'passage_verification': 'not_independently_checked', **self.delivery_freshness(row, kept)}

    def recall(self, query, paper, source_sha256, task):
        key = normalized(query)
        words = set(re.findall(r'[^\W_]+', query.casefold()))
        papers, refs, acquisitions = self.candidates(query, allow_title=False)
        source_query = bool(papers or acquisitions or key in self.delivery_identifiers or source_identifier(query))
        passage_address = re.fullmatch(r'(?:passage_)?[0-9a-f]{64}', key)
        filters = [(refs, acquisitions)] if source_query else []
        if paper:
            _, paper_refs, paper_acquisitions = self.candidates(paper)
            filters.append((paper_refs, paper_acquisitions))
        matches = []
        for row in self.passages:
            obj = row['object']
            content = obj['content']
            if source_sha256 and content['source_sha256'] != source_sha256:
                continue
            kept, sources = self.selected_deliveries(row, task, filters)
            if not kept:
                continue
            exact = source_query or key in (normalized(obj['id']), normalized(content['source_sha256']))
            if passage_address and not exact:
                continue
            text = ' '.join([*content.get('section_path', ()), content['text']]).casefold()
            lexical = len(words.intersection(re.findall(r'[^\W_]+', text)))
            if lexical or exact:
                rank = (exact, lexical, len({d['task'] for d in kept}), kept[-1]['at'], kept[-1]['seq'], obj['id'])
                matches.append((rank, row, kept, sources))
        matches.sort(key=lambda v: v[0], reverse=True)
        for _, row, kept, sources in matches:
            yield self.passage(row, kept, sources)
