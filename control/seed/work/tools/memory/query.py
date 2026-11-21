"""Deterministic lookups over the revisions and deliveries in this run's snapshot."""

import json
import re
from collections import defaultdict

NOTICE = ('Historical text returned by the literature server. The original file hash was checked. '
          'This passage has not been independently checked against the original.')
DEFAULT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
ID_FIELDS = ('paper_id', 'doi', 'pmid', 'pmcid')


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def normalized(value):
    key = str(value or '').strip().casefold()
    if key.startswith('pmcid:'):
        identifier = key[6:].strip()
        return 'pmc' + identifier if identifier.isdigit() else identifier
    for prefix in ('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'http://dx.doi.org/', 'doi:', 'pmid:'):
        if key.startswith(prefix):
            return key[len(prefix):].strip()
    return key


def doc_key(doc):
    return tuple(doc.get(k) for k in ('document_id', 'document_version', 'source_sha256'))


def bounded(items, limit):
    """Preserve whole records, count every omitted item and bound the UTF-8 tool result including its envelope."""
    size = len(encode({'results': [], 'omitted': 0}))
    result, sizes = [], []
    omitted = 0
    for item in items:
        extra = len(encode(item)) + int(bool(result))
        if size + extra <= limit:
            result.append(item)
            sizes.append(extra)
            size += extra
        else:
            omitted += 1
    while result and size + len(str(omitted)) - 1 > limit:
        result.pop()
        size -= sizes.pop()
        omitted += 1
    return {'results': result, 'omitted': omitted}


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
        self.challengers = defaultdict(list)
        if self.status['condition'] != 'none':
            with (folder / 'evidence.jsonl').open('rb') as stream:
                data = stream.read(MAX_SNAPSHOT_BYTES + 1)
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise ValueError('Oversized memory snapshot')
            for line in data.splitlines():
                row = json.loads(line)
                obj = row['object']
                self.versions[obj['id'], obj['revision']] = row
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
                for key in {normalized(content.get(k)) for k in ID_FIELDS} - {''}:
                    self.by_identifier[key].append(row)
            elif kind == 'acquisition':
                self.acquisitions.append(row)
                if all(doc_key(content)):
                    self.by_doc[doc_key(content)].append(row)
                    self.by_hash[content['source_sha256']].append(row)
            elif kind == 'passage':
                self.passages.append(row)

    @staticmethod
    def request(content):
        return normalized(content.get('paper_ref') or content.get('source_id'))

    def papers_for(self, row):
        content = row['object']['content']
        return self.by_identifier.get(self.request(content), []) if content.get('paper_ref') else []

    def dois_for(self, row):
        content = row['object']['content']
        requested = normalized(content.get('paper_ref'))
        dois = {normalized(p['object']['content'].get('doi')) for p in self.papers_for(row)}
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
                'source_records': [dict(id=p['object']['id'], revision=p['object']['revision'],
                                        **{k: p['object']['content'].get(k) for k in ID_FIELDS})
                                   for p in self.papers_for(row)],
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

    def candidates(self, query):
        key = normalized(query)
        papers = list(self.by_identifier.get(key, []))
        if not papers:
            papers = [row for row in self.latest.values() if row['object']['content']['type'] == 'paper'
                      and key in normalized(row['object']['content'].get('title'))]
        refs = {normalized(p['object']['content'].get(field)) for p in papers for field in ID_FIELDS} - {''}
        refs.add(key)
        acquisitions = {}
        for row in self.acquisitions:
            obj = row['object']
            content = obj['content']
            if self.request(content) in refs or normalized(content.get('document_id')) == key:
                acquisitions[obj['id']] = row
        return papers, acquisitions

    def passage(self, row, deliveries):
        obj = row['object']
        content = obj['content']
        sources = {}
        for d in deliveries:
            sources.update(self.acquisitions_for(d, content))
        # Filtered requests can expose a narrower set of deliveries than the stored object view.
        return {'id': obj['id'], 'revision': obj['revision'], 'content': content,
                'deliveries': list(reversed(deliveries)), 'sources': [self.attribution(r) for r in sources.values()],
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
        item = dict(row)
        kind = row['object']['content']['type']
        if kind == 'passage':
            payload = self.passage(row, row['deliveries'])
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
        words = set(re.findall(r'\w+', key))
        matches = []
        for row in self.latest.values():
            obj = row['object']
            if kind and obj['kind'] != kind:
                continue
            text = ' '.join(v for v in obj['content'].values() if isinstance(v, str)).casefold()
            exact = key == normalized(obj['id'])
            lexical = sum(word in text for word in words)
            if exact or lexical:
                challenge = any(ref['relation'] == 'challenges' for field in ('source_refs', 'object_refs')
                                for ref in obj.get(field, ()))
                rank = (exact, row.get('freshness') == 'needs_review', challenge, lexical,
                        row['deliveries'][-1]['at'], row['deliveries'][-1]['seq'], obj['id'])
                matches.append((rank, obj['id']))
        for _, identity in sorted(matches, reverse=True):
            yield from self.get_evidence(identity, None)

    def lookup(self, query):
        papers, acquisitions = self.candidates(query)
        for paper in papers:
            yield dict(type='paper', **paper)
        for acq in acquisitions.values():
            yield dict(type='acquisition', **acq, attribution=self.attribution(acq))
        key = normalized(query)
        for row in self.passages:
            obj = row['object']
            kept = []
            for d in reversed(row['deliveries']):
                links = self.acquisitions_for(d, obj['content'])
                if (set(links) & acquisitions.keys() or key == normalized(d.get('document_id'))
                        or key == normalized(d.get('paper_ref'))):
                    kept.append(d)
            if kept:
                yield {'type': 'passage', 'id': obj['id'], 'revision': obj['revision'],
                       'locator': obj['content']['locator'], 'source_sha256': obj['content']['source_sha256'],
                       'deliveries': [{k: d[k] for k in ('run', 'task', 'document_id', 'document_version')} for d in kept],
                       'passage_verification': 'not_independently_checked', **self.delivery_freshness(row, kept)}

    def recall(self, query, paper, source_sha256, task):
        key = normalized(query)
        words = set(re.findall(r'\w+', query.casefold()))
        exact_refs = {normalized(row['object']['content'].get(field)) for row in self.by_identifier.get(key, [])
                      for field in ID_FIELDS} - {''}
        exact_refs.add(key)
        paper_acqs = set(self.candidates(paper)[1]) if paper else set()
        matches = []
        for row in self.passages:
            obj = row['object']
            content = obj['content']
            if source_sha256 and content['source_sha256'] != source_sha256:
                continue
            kept = []
            exact = key in (normalized(obj['id']), normalized(content['source_sha256']))
            for d in row['deliveries']:
                if task and d['task'] != task:
                    continue
                links = self.acquisitions_for(d, content)
                terms = {normalized(d.get(k)) for k in ('document_id', 'paper_ref')}
                if paper and normalized(paper) not in terms and not (paper_acqs & links.keys()):
                    continue
                kept.append(d)
                if exact_refs.intersection(terms) or any(self.request(a['object']['content']) in exact_refs
                                                          for a in links.values()):
                    exact = True
            if not kept:
                continue
            text = (str(content.get('section') or '') + ' ' + content['text']).casefold()
            lexical = sum(word in text for word in words)
            if lexical or exact:
                rank = (exact, lexical, len({d['task'] for d in kept}), kept[-1]['at'], kept[-1]['seq'], obj['id'])
                matches.append((rank, row, kept))
        matches.sort(key=lambda v: v[0], reverse=True)
        for _, row, kept in matches:
            yield self.passage(row, kept)
