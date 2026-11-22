"""Deterministic lookups over the revisions and deliveries in this run's snapshot."""

import json
import re
from collections import defaultdict
from itertools import chain

NOTICE = ('Historical text returned by the literature server. The original file hash was checked. '
          'This passage has not been independently checked against the original.')
DEFAULT_BYTES = 24 * 1024
# Native Codex sessions have truncated MCP results around 50 KB. Size the escaped
# MCP envelope with headroom, including when a caller supplies an older, larger budget.
NATIVE_RECEIPT_BYTES = 32 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
RECENT_DELIVERIES = 3
ID_FIELDS = ('paper_id', 'doi', 'pmid', 'pmcid')
PROVIDER_SHORTHAND = re.compile(r'w\d+|(?:med|pmc|ppr|agr|pat|cba):[a-z0-9._-]+')


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def normalized(value):
    key = str(value or '').strip().casefold()
    if PROVIDER_SHORTHAND.fullmatch(key):
        return ('europe_pmc:' if ':' in key else 'openalex:') + key
    openalex = re.fullmatch(r'https?://openalex\.org/(w\d+)', key)
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


# In these scripts whitespace is not a reliable word boundary. Keep adjacent pairs
# within each run, including Thai combining marks, without forming pairs across punctuation.
UNSPACED = re.compile(r'[\u3041-\u3096\u3099-\u309f\u30a1-\u30fa\u30fc-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
                      r'\U00020000-\U0002fa1f\U00030000-\U000323af\u0e01-\u0e3a\u0e40-\u0e4e]+')
WORDS = re.compile(r'[^\W_]+')
ROOT_HEADINGS = {'body', 'abstract', 'title', 'document', 'references', 'back matter', 'untitled section'}


def words(text, *, query):
    text = text.casefold()
    tokens = set(WORDS.findall(UNSPACED.sub(' ', text)))
    for match in UNSPACED.finditer(text):
        run = match[0]
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
        if not query or len(run) == 1:
            tokens.update(run)
    return tokens


def word_forms(word):
    """Share conservative English inflections, without matching arbitrary substrings."""
    forms = {word}
    if not (word.isascii() and word.isalpha() and len(word) >= 4):
        return forms
    singular = word
    if len(word) >= 6 and word.endswith('ies'):
        singular = word[:-3] + 'y'
    elif len(word) >= 5 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        singular = word[:-1]
    forms.add(singular)
    for suffix in ('ment', 'ing', 'ed'):
        base = singular.removesuffix(suffix)
        if base != singular and len(base) >= 4:
            forms.add(base)
            if suffix in ('ing', 'ed'):
                forms.add(base + 'e')
            break
    return forms


def lexical_score(query_forms, text):
    terms = {form for word in words(text, query=False) for form in word_forms(word)}
    return sum(not group.isdisjoint(terms) for group in query_forms)


def text_matcher(query, *, literal=False):
    """Return the number of terms and a scorer. Treat ambiguous short IDs as one literal token."""
    raw = query.strip().casefold()
    if literal or PROVIDER_SHORTHAND.fullmatch(raw):
        pattern = re.compile(r'(?<!\w)' + re.escape(raw) + r'(?!\w)')
        return 1, lambda text: int(bool(pattern.search(text.casefold())))
    forms = [word_forms(w) for w in words(query, query=True)]
    return len(forms), lambda text: lexical_score(forms, text)


def passage_text(content):
    headings = content.get('section_path', ())
    if headings and (headings[0].casefold() in ROOT_HEADINGS or re.fullmatch(r'Page \d+', headings[0])):
        headings = headings[1:]
    return ' '.join([*(h for h in headings if h != 'Untitled section'), content['text']])


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


def fits(payload, limit):
    raw = encode(payload)
    return len(raw) <= limit and len(encode({'content': [{'type': 'text', 'text': raw.decode()}],
                                             'isError': False})) <= NATIVE_RECEIPT_BYTES


def exact_pointer(item):
    obj = item.get('object', item)
    if 'id' not in obj or 'revision' not in obj:
        return None
    args = {'id': obj['id'], 'revision': obj['revision']}
    if 'object' not in item and item.get('deliveries'):
        # Preserve the source match when a filtered passage search points at a shared block.
        d = item['deliveries'][0]
        args.update({k: d[k] for k in ('document_id', 'document_version', 'invocation_id') if k in d})
        args['source_run'] = d['run']
    return {'get_evidence': args}


def bounded(items, limit, offset=None):
    """A search page is a contiguous slice of the frozen ranking, including exact-read pointers."""
    results = []
    omitted = 0
    first_pointer = None
    empty = encode({'results': [], 'omitted': 0})
    payload_bytes = len(empty)
    receipt_bytes = len(encode({'content': [{'type': 'text', 'text': empty.decode()}], 'isError': False}))
    for index, item in enumerate(items):
        if offset is not None:
            if index < offset:
                continue
            if omitted:
                omitted += 1
                continue
            if index == offset:
                first_pointer = exact_pointer(item)
        views = result_views(item)
        if index == offset and first_pointer:
            views = chain(views, (first_pointer,))
        for view in views:
            raw = encode(view)
            added = len(raw) + bool(results)
            escaped = added + raw.count(b'"') + raw.count(b'\\')
            if payload_bytes + added <= limit and receipt_bytes + escaped <= NATIVE_RECEIPT_BYTES:
                results.append(view)
                payload_bytes += added
                receipt_bytes += escaped
                break
        else:
            omitted += 1
    while True:
        page = {'results': results, 'omitted': omitted}
        if offset is not None and omitted and results:
            page['next_offset'] = offset + len(results)
        if not results or fits(page, limit):
            return page
        if len(results) == 1 and first_pointer and results[0] != first_pointer:
            results[0] = first_pointer
            continue
        results.pop()
        omitted += 1


def exact_page(items, limit, offset):
    if offset is None or not items:
        page = bounded(items, limit)
        if page['results'] or not items:
            return page
        offset = 0
    item, = items
    obj = item['object']
    canonical = json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if offset >= len(canonical):
        raise ValueError('object_offset is outside this revision')
    # Keep both the exact revision and its selected passage delivery available for receipt replay.
    part = {'id': obj['id'], 'revision': obj['revision'], 'offset': offset, 'total_chars': len(canonical)}
    fields = ('run', 'document_id', 'document_version', 'invocation_id')
    row = {'object_part': part, 'deliveries': [{k: d[k] for k in fields if k in d} for d in item['deliveries']],
           'delivery_count': item['delivery_count']}
    for key in ('next_delivery_offset', 'freshness', 'freshness_reasons', 'notice', 'passage_verification'):
        if key in item:
            row[key] = item[key]
    length = min(len(canonical) - offset, limit, NATIVE_RECEIPT_BYTES)
    while length:
        end = offset + length
        part['text'] = canonical[offset:end]
        page = {'results': [row], 'omitted': 0}
        if end < len(canonical):
            page['next_object_offset'] = end
        if fits(page, limit):
            return page
        length //= 2
    return {'results': [], 'omitted': 1}


def recent_deliveries(deliveries, offset=0):
    end = max(0, len(deliveries) - offset)
    page = {'deliveries': list(reversed(deliveries[max(0, end - RECENT_DELIVERIES):end])),
            'delivery_count': len(deliveries)}
    if end > RECENT_DELIVERIES:
        page['next_delivery_offset'] = offset + RECENT_DELIVERIES
    return page


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
        self.source_identifiers = set()
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
                self.source_identifiers.update((self.request(content), normalized(content.get('document_id'))))
                if all(doc_key(content)):
                    self.by_doc[doc_key(content)].append(row)
                    self.by_hash[content['source_sha256']].append(row)
            elif kind == 'passage':
                self.passages.append(row)
                for d in row['deliveries']:
                    self.source_identifiers.update((self.request(d), normalized(d.get('document_id'))))
        self.source_identifiers.discard('')

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

    def source_query(self, query, key):
        if key in self.by_identifier or key in self.source_identifiers:
            return True
        raw = query.strip().casefold()
        if PROVIDER_SHORTHAND.fullmatch(raw):
            return False
        return bool(re.fullmatch(r'(?:europe_pmc|openalex|semantic_scholar|unpaywall):\S+|'
                                 r'10\.\d+/\S+|doc_[\w-]+|pmc\d+', key)
                    or re.fullmatch(r'(?:pmid|pubmed):\s*\d+', raw))

    def candidates(self, query, *, allow_title=True):
        key = normalized(query)
        papers = list(self.by_identifier.get(key, []))
        if allow_title and key and not self.source_query(query, key):
            count, score = text_matcher(query)
            papers = [row for row in self.latest.values() if row['object']['content']['type'] == 'paper'
                      and count and score(row['object']['content'].get('title') or '') == count]
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

    def selected_deliveries(self, row, task=None, filters=(), address=None):
        deliveries, sources = [], {}
        for d in row['deliveries']:
            if address and any(d.get(k) != v for k, v in address.items()):
                continue
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

    def get_evidence(self, evidence_id, revision, *, address=None, delivery_offset=0):
        row = self.latest.get(evidence_id) if revision is None else self.versions.get((evidence_id, revision))
        if row is None:
            return []
        kind = row['object']['content']['type']
        deliveries = row['deliveries']
        if kind == 'passage':
            deliveries, sources = self.selected_deliveries(row, address=address)
        elif address:
            deliveries = [d for d in deliveries if all(d.get(k) == v for k, v in address.items())]
        if (address and not deliveries) or (delivery_offset and delivery_offset >= len(deliveries)):
            return []
        item = {**row, **recent_deliveries(deliveries, delivery_offset)}
        if kind == 'passage':
            item.update(sources=[self.attribution(r) for r in sources.values()], notice=NOTICE,
                        passage_verification='not_independently_checked', **self.delivery_freshness(row, deliveries))
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
        source_query = self.source_query(query, key)
        _, score = text_matcher(query, literal=source_query)
        matches = []
        for row in self.latest.values():
            obj = row['object']
            if kind and obj['kind'] != kind:
                continue
            text = ' '.join(v for v in obj['content'].values() if isinstance(v, str)).casefold()
            paper_match = key in self.paper_refs.get(obj['id'], ())
            exact = key == normalized(obj['id']) or paper_match
            if source_query:
                # Namespace expansion is for identity comparisons, never for full-text keywords.
                exact = exact or key in {self.request(obj['content']), normalized(obj['content'].get('document_id'))}
            else:
                text = ' '.join([obj['id'], obj['origin']['task'], text])
            lexical = score(text)
            if exact or lexical:
                challenge = any(ref['relation'] == 'challenges' for field in ('source_refs', 'object_refs')
                                for ref in obj.get(field, ()))
                rank = (exact, paper_match, row.get('freshness') == 'needs_review', challenge, lexical,
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
        _, score = text_matcher(query)
        _, refs, acquisitions = self.candidates(query, allow_title=False)
        source_query = self.source_query(query, key)
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
            lexical = 0 if source_query else score(passage_text(content))
            if lexical or exact:
                rank = (exact, lexical, len({d['task'] for d in kept}), kept[-1]['at'], kept[-1]['seq'], obj['id'])
                matches.append((rank, row, kept, sources))
        matches.sort(key=lambda v: v[0], reverse=True)
        for _, row, kept, sources in matches:
            yield self.passage(row, kept, sources)
