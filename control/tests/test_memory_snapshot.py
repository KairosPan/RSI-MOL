"""Trial projections, bounded results and source attribution through the seeded offline MCP server."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.memory_history import trusted_passages, trusted_rows
from rsi.memory_snapshot import _encode, _project, place_snapshot
from rsi.memory_store import State
from rsi.paths import Layout

SEED = Path(__file__).resolve().parents[1] / 'seed' / 'work'
SHA = '1' * 64


def object_row(identity, revision, run, task, kind, **content):
    return {'id': identity, 'revision': revision, 'origin': {'run': run, 'task': task, 'version': 'V1'},
            'kind': 'evidence', 'content': {'type': kind, **content}, 'supersedes': revision - 1 or None,
            'record_sha256': 'original-record-digest'}


def receipt(identity, revision, seq, task, run, **fields):
    return {'id': identity, 'revision': revision, 'at': str(seq).zfill(4), 'seq': seq, 'task': task,
            'run': run, 'thread': 'thread', 'version': 'V1', 'service': {'entry': 'seed'},
            'invocation_id': 'call-' + str(seq), **fields}


def samples():
    paper = {'paper_id': 'provider:42', 'pmid': '123', 'pmcid': 'PMC123', 'title': 'Memory paper'}
    doc_a = {'document_id': 'doc_a', 'document_version': 'va', 'source_sha256': SHA}
    doc_b = {'document_id': 'doc_b', 'document_version': 'vb', 'source_sha256': SHA}
    return State(version=20, objects=[
        object_row('paper', 1, 'R1', 'A', 'paper', doi='10.1/alpha', **paper),
        object_row('paper', 2, 'R2', 'T', 'paper', doi='10.1/secret', **paper),
        object_row('get_a', 1, 'R1', 'A', 'acquisition', paper_ref='provider:42', **doc_a),
        object_row('get_b', 1, 'R3', 'B', 'acquisition', paper_ref='10.1/beta', **doc_b),
        object_row('paragraph', 1, 'R2', 'T', 'passage', text='A quoted finding.', section='Findings',
                   source_sha256=SHA, locator='block:004', parser_version='v1'),
    ], deliveries=[
        receipt('paper', 1, 1, 'A', 'R1'), receipt('paper', 2, 2, 'T', 'R2'),
        receipt('get_a', 1, 3, 'A', 'R1'), receipt('get_b', 1, 4, 'B', 'R3'),
        receipt('paragraph', 1, 5, 'T', 'R2', document_id='secret_alias', document_version='vs'),
        receipt('paragraph', 1, 6, 'A', 'R1', **doc_a), receipt('paragraph', 1, 7, 'B', 'R3', **doc_b),
    ])


class MemorySnapshot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.layout = Layout(self.parent / 'root')
        self.state = samples()
        p = patch('rsi.memory_snapshot.memory_store.load', return_value=self.state)
        p.start()
        self.addCleanup(p.stop)

    def place(self, condition='trial', task='T', max_bytes=16 << 20, name='work'):
        work = self.parent / name
        shutil.copytree(SEED / 'tools' / 'memory', work / 'tools' / 'memory')
        info = place_snapshot(self.layout, work, condition, task, max_bytes=max_bytes)
        return work, info

    def mcp(self, work, calls):
        requests = [{'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                    {'method': 'notifications/initialized'}, {'id': 2, 'method': 'tools/list'}]
        requests.extend({'id': n, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
                        for n, (name, args) in enumerate(calls, 3))
        lines = [json.dumps({'jsonrpc': '2.0', **r}) for r in requests]
        done = subprocess.run([sys.executable, '-B', str(work / 'tools/memory/server.py')],
                              input='\n'.join(lines) + '\n', capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(done.stderr, '')
        replies = [json.loads(line)['result'] for line in done.stdout.splitlines()]
        self.assertEqual(replies[0]['protocolVersion'], '2025-06-18')
        self.assertEqual({t['name'] for t in replies[1]['tools']},
                         {'memory_status', 'get_evidence', 'recall_passages', 'lookup_sources', 'recall_judgments',
                          'recall_objects'})
        return replies[2:]

    def values(self, work, calls):
        replies = self.mcp(work, calls)
        self.assertFalse(any(r['isError'] for r in replies), replies)
        return [json.loads(r['content'][0]['text']) for r in replies]

    def test_unavailable_manual_snapshot_reports_the_problem_to_readers(self):
        work = self.parent / 'manual'
        shutil.copytree(SEED / 'tools' / 'memory', work / 'tools' / 'memory')
        notices = []
        with patch('rsi.memory_snapshot.memory_store.load', side_effect=KeyError('missing ledger field')):
            meta = place_snapshot(self.layout, work, manual_problems=notices)
        self.assertIn('missing ledger field', notices[0])
        # Released readers open this file for every condition except "none".
        # An empty file would let them report a successful search with no matches.
        self.assertFalse((work / 'memory/evidence.jsonl').exists())
        header = (work / 'memory/status.json').read_bytes()
        self.assertEqual(meta['fingerprint'], hashlib.sha256(b'status.json\0' + header).hexdigest())
        status, lookup = self.mcp(work, [('memory_status', {}), ('lookup_sources', {'query': 'paper'})])
        self.assertFalse(status['isError'])
        self.assertEqual(json.loads(status['content'][0]['text'])['condition'], 'unavailable')
        self.assertTrue(lookup['isError'])
        self.assertIn('could not be read or verified', lookup['content'][0]['text'])

    def test_trial_projects_every_field_and_hashes_what_it_wrote(self):
        work, meta = self.place()
        folder = work / 'memory'
        blob = (folder / 'evidence.jsonl').read_bytes()
        for private in (b'secret_alias', b'R2', b'10.1/secret'):
            self.assertNotIn(private, blob)
        expected_hash = hashlib.sha256(b'status.json\0' + (folder / 'status.json').read_bytes() +
                                       b'evidence.jsonl\0' + blob).hexdigest()
        self.assertEqual(meta['fingerprint'], expected_hash)
        status, paper, paragraph, hidden = self.values(work, [
            ('memory_status', {}), ('get_evidence', {'id': 'paper'}), ('get_evidence', {'id': 'paragraph'}),
            ('get_evidence', {'id': 'paper', 'revision': 2})])
        self.assertEqual(status['excluded'], {'paper': 1, 'deliveries': 2})
        self.assertEqual(paper['results'][0]['object']['revision'], 1)
        self.assertEqual(paragraph['results'][0]['object']['origin']['task'], 'A')
        self.assertNotIn('record_sha256', paragraph['results'][0]['object'])
        self.assertEqual(hidden, {'results': [], 'omitted': 0})
        _, same = self.place(name='other')
        self.assertEqual(same, meta)

    def test_paper_filter_conflicting_dois_and_retrieval_budget(self):
        work, _ = self.place()
        found, recall, small, absent = self.values(work, [
            ('lookup_sources', {'query': 'HTTPS://doi.org/10.1/ALPHA'}),
            ('recall_passages', {'query': 'quoted', 'paper': 'PMID:123'}),
            ('recall_passages', {'query': 'quoted', 'max_bytes': 64}),
            ('recall_passages', {'query': 'quoted', 'task': 'T'})])
        acq = [r for r in found['results'] if r['type'] == 'acquisition']
        self.assertEqual([r['object']['id'] for r in acq], ['get_a'])
        source = acq[0]['attribution']
        self.assertEqual(source['verification'], 'unverified')
        self.assertEqual(source['conflicting_requests_sharing_file'][0]['dois'], ['10.1/beta'])
        self.assertEqual([d['document_id'] for d in recall['results'][0]['deliveries']], ['doc_a'])
        self.assertEqual(recall['results'][0]['passage_verification'], 'not_independently_checked')
        self.assertEqual(small, {'results': [], 'omitted': 1})
        self.assertEqual(absent['results'], [])

    def test_exact_source_queries_return_only_matching_deliveries_and_attributions(self):
        # The text also matches words in the DOI, even when its actual delivery is filtered out.
        self.state.objects[-1]['content']['text'] = 'A quoted finding about alpha (PMID 333).'
        work, _ = self.place()
        calls = [('recall_passages', {'query': query}) for query in (
            'https://doi.org/10.1/alpha', 'PMID:123', 'doc_a', '10.1/beta', 'quoted', SHA, 'paragraph')]
        calls.extend([('recall_passages', {'query': '10.1/alpha', 'task': 'B'}),
                      ('recall_passages', {'query': 'doc_a', 'paper': '10.1/beta'}),
                      ('recall_passages', {'query': '10.1/alpha-absent'}),
                      ('recall_passages', {'query': 'PMID:333'})])
        alpha, pmid, document, beta, words, original, passage, *absent = self.values(work, calls)
        for response, doc, acquisition, conflict in (
                (alpha, 'doc_a', 'get_a', '10.1/beta'), (pmid, 'doc_a', 'get_a', '10.1/beta'),
                (document, 'doc_a', 'get_a', '10.1/beta'), (beta, 'doc_b', 'get_b', '10.1/alpha')):
            result, = response['results']
            self.assertEqual([d['document_id'] for d in result['deliveries']], [doc])
            source, = result['sources']
            self.assertEqual(source['acquisition_id'], acquisition)
            self.assertEqual(source['conflicting_requests_sharing_file'][0]['dois'], [conflict])
        for response in (words, original, passage):
            self.assertEqual([d['document_id'] for d in response['results'][0]['deliveries']], ['doc_b', 'doc_a'])
        for response in absent:
            self.assertEqual(response['results'], [])

    def test_doi_scopes_sources_when_acquisitions_share_a_document_address(self):
        # The native passage return identifies the document but does not say which request produced it.
        self.state.objects[-2]['content'].update(document_id='doc_a', document_version='va')
        self.state.deliveries[-1].update(document_id='doc_a', document_version='va')
        work, _ = self.place()
        exact, filtered, whole = self.values(work, [
            ('recall_passages', {'query': '10.1/beta'}),
            ('recall_passages', {'query': 'quoted', 'paper': '10.1/beta'}),
            ('recall_passages', {'query': 'doc_a'})])
        for response in (exact, filtered):
            sources = response['results'][0]['sources']
            self.assertEqual([s['acquisition_id'] for s in sources], ['get_b'])
            self.assertEqual(sources[0]['conflicting_requests_sharing_file'][0]['dois'], ['10.1/alpha'])
        self.assertEqual({s['acquisition_id'] for s in whole['results'][0]['sources']}, {'get_a', 'get_b'})

    def test_sparse_paper_uses_visible_identifier_observations_without_changing_pins(self):
        # The latest citation list only reports the provider ID and PMID. A prior run
        # corrected the DOI; a trial excluding that run must use its own visible history.
        paper = self.state.objects[1]
        sparse = dict(paper, revision=3, content=dict(paper['content'], doi=None, pmcid=None))
        self.state.objects.append(sparse)
        self.state.deliveries.append(receipt('paper', 3, 8, 'B', 'R4'))
        self.state.objects[2]['content']['paper_ref'] = 'PMC123'
        for condition, available, absent in (
                ('live', '10.1/secret', '10.1/alpha'), ('trial', '10.1/alpha', '10.1/secret')):
            work, _ = self.place(condition, 'T', name=condition)
            found, other, pinned = self.values(work, [
                ('lookup_sources', {'query': available}), ('lookup_sources', {'query': absent}),
                ('get_evidence', {'id': 'paper', 'revision': 3})])
            self.assertEqual([r['type'] for r in found['results']], ['paper', 'acquisition', 'passage'])
            self.assertEqual(other['results'], [])
            self.assertIsNone(pinned['results'][0]['object']['content']['doi'])
            self.assertEqual(found['results'][0]['object'], pinned['results'][0]['object'])
            sources = found['results'][1]['attribution']['source_records']
            reported = [(s['revision'], s['doi']) for s in sources if s.get('doi')]
            self.assertEqual(reported, [(2 if condition == 'live' else 1, available)])

    def test_partial_identifier_correction_stops_fallback_to_superseded_record(self):
        self.state.objects[1]['content'].update(doi=None, pmcid='PMC999')
        work, _ = self.place('live')
        old_doi, old_pmcid, new_pmcid = self.values(work, [
            ('lookup_sources', {'query': q}) for q in ('10.1/alpha', 'PMC123', 'PMC999')])
        self.assertEqual(old_doi['results'], [])
        self.assertEqual(old_pmcid['results'], [])
        self.assertEqual(new_pmcid['results'][0]['object']['revision'], 2)

    def test_provider_references_scope_recall_and_passage_search_uses_heading_words(self):
        paper = self.state.objects[0]['content']
        paper.update(paper_id='europe_pmc:MED:123', provider='europe_pmc')
        self.state.objects[2]['content'].update(provider='europe_pmc', paper_ref='MED:123')
        self.state.objects[-1]['content'].update(section='section:000007', section_path=['Methods'],
                                                  text='Remediation with medication for pubmed W999. A passage.')
        oa = object_row('oa', 1, 'R4', 'B', 'paper', provider='openalex', paper_id='openalex:W456',
                        doi='10.1/oa', title='OpenAlex record')
        self.state.objects.append(oa)
        self.state.deliveries.append(receipt('oa', 1, 8, 'B', 'R4'))
        self.state.objects[3]['content'].update(provider='openalex', paper_ref='W456')
        work, _ = self.place()
        queries = ['MED:123', 'europe_pmc:MED:123', 'pubmed:123', 'W456', 'https://openalex.org/W456',
                   '10.1/oa', 'methods section', 'W999', 'MED:999', 'pubmed:999', 'section', '000007', 'med',
                   'passage_' + 'f' * 64]
        results = self.values(work, [('recall_passages', {'query': query}) for query in queries])
        for result, expected in zip(results[:8], [['doc_a']] * 3 + [['doc_b']] * 3 + [['doc_b', 'doc_a']] * 2):
            self.assertEqual([d['document_id'] for d in result['results'][0]['deliveries']], expected)
        self.assertTrue(all(r['results'] == [] for r in results[8:]))
        for result in self.values(work, [('lookup_sources', {'query': key}) for key in ('MED:123', 'W456')]):
            self.assertEqual([r['type'] for r in result['results']], ['paper', 'acquisition', 'passage'])

    def test_unknown_provider_shorthand_matches_literal_text_and_titles(self):
        self.state.objects[0]['content']['title'] = 'W12 paper on PAT:marker'
        self.state.objects[-1]['content']['text'] = 'W303 and w1118. MED:label, not MED separated from 303.'
        work, _ = self.place()
        queries = ['W303', 'W1118', 'med:LABEL', 'W30', 'MED:303', 'PAT:label',
                   'openalex:W303', 'https://openalex.org/w1118', 'europe_pmc:MED:label']
        for tool in ('recall_passages', 'recall_objects'):
            responses = self.values(work, [(tool, {'query': q}) for q in queries])
            for query, response in zip(queries, responses):
                ids = [row.get('object', row)['id'] for row in response['results']]
                self.assertEqual(ids, ['paragraph'] if query in queries[:3] else [], (tool, query))
        queries = ['w12', 'PAT:MARKER', 'W1', 'med:marker', 'https://openalex.org/W12', 'europe_pmc:pat:marker']
        titles = self.values(work, [('lookup_sources', {'query': q}) for q in queries])
        for query, response in zip(queries, titles):
            types = [row['type'] for row in response['results']]
            self.assertEqual(types, ['paper', 'acquisition', 'passage'] if query in queries[:2] else [], query)
        title, missing = self.values(work, [('recall_passages', {'query': 'W303', 'paper': p}) for p in ('W12', 'W303')])
        self.assertEqual([d['document_id'] for d in title['results'][0]['deliveries']], ['doc_a'])
        self.assertEqual(missing['results'], [])

    def test_acquisition_or_delivery_shorthand_precedes_literal_title_and_text(self):
        self.state.objects[0]['content']['title'] = 'W303 and PAT:marker appear in this unrelated title'
        self.state.objects[3]['content']['paper_ref'] = 'openalex:W303'
        self.state.deliveries[-2]['paper_ref'] = 'europe_pmc:PAT:marker'
        self.state.objects[-1]['content']['text'] = 'Unrelated mentions of W303 and PAT:marker.'
        work, _ = self.place()
        for query, doc, types in [('W303', 'doc_b', ['acquisition', 'passage']), ('PAT:marker', 'doc_a', ['passage'])]:
            sources, passages = self.values(work, [('lookup_sources', {'query': query}), ('recall_passages', {'query': query})])
            self.assertEqual([r['type'] for r in sources['results']], types)
            for row in (sources['results'][-1], passages['results'][0]):
                self.assertEqual([d['document_id'] for d in row['deliveries']], [doc])

    def test_none_has_only_status_and_caps_apply_to_whole_projected_revisions(self):
        work, meta = self.place('none')
        self.assertEqual([p.name for p in (work / 'memory').iterdir()], ['status.json'])
        self.assertEqual(meta['condition'], 'none')
        self.assertEqual(self.values(work, [('lookup_sources', {'query': 'provider:42'})])[0]['results'], [])
        limited, info = self.place('live', max_bytes=1024, name='capped')
        self.assertEqual(info['omitted']['deliveries'], len(self.state.deliveries))
        self.assertEqual((limited / 'memory' / 'evidence.jsonl').read_bytes(), b'')

    def test_text_queries_preserve_numbers_scientific_terms_and_inflections(self):
        paper = self.state.objects[0]['content']
        paper.update(title='CONSORT 2010 statement on GO:0006915')
        self.state.objects[-1]['content'].update(section_path=['Body', 'Sampling methods'],
            text='SAMtools performed paired alignments in 2009. Measurements: 95. GO:0006915.')
        work, _ = self.place()
        positive = ('2009', '95', 'GO:0006915', 'samtools:view', 'pair', 'align', 'measure', 'methods')
        negative = ('200', '950', 'goals', 'med', 'body', 'PMID:95', 'MED:2009', 'W2009')
        found = self.values(work, [('recall_passages', {'query': q}) for q in (*positive, *negative)])
        for query, response in zip((*positive, *negative), found):
            self.assertEqual([r['id'] for r in response['results']], ['paragraph'] if query in positive else [], query)
        titles = self.values(work, [('lookup_sources', {'query': q}) for q in ('2010', 'GO:0006915', '201')])
        self.assertEqual([r['type'] for r in titles[0]['results']], ['paper', 'acquisition', 'passage'])
        self.assertEqual(titles[1]['results'], titles[0]['results'])
        self.assertEqual(titles[2]['results'], [])

    def test_generated_headings_and_provider_names_do_not_add_unrelated_matches(self):
        para = self.state.objects[-1]
        para['content'].update(section_path=['Body', 'Untitled section'], text='Our goal was to compare repairs.')
        other = object_row('real', 1, 'R4', 'B', 'passage', section_path=['Body', 'Body weight'], text='Observed mass.',
                           source_sha256=SHA, locator='block:8', parser_version='v1')
        self.state.objects.append(other)
        self.state.deliveries.append(receipt('real', 1, 8, 'B', 'R4', document_id='doc_b', document_version='vb'))
        for i in (1, 2):
            self.state.objects.append(object_row('oa' + str(i), 1, 'R5', 'B', 'paper', provider='openalex',
                                                 paper_id=f'openalex:W{i}', title=f'OpenAlex paper {i}'))
            self.state.deliveries.append(receipt('oa' + str(i), 1, i + 8, 'B', 'R5'))
        work, _ = self.place()
        body, no_section, go, pair, oa, absent = self.values(work, [
            ('recall_passages', {'query': q}) for q in ('body weight', 'section', 'GO:0006915', 'pair')] + [
            ('recall_objects', {'query': q}) for q in ('W1', 'W10')])
        self.assertEqual([r['id'] for r in body['results']], ['real'])
        self.assertTrue(all(r['results'] == [] for r in (no_section, go, pair, absent)))
        self.assertEqual([r['object']['id'] for r in oa['results']], ['oa1'])

    def test_unspaced_passages_and_judgments_match_local_phrases(self):
        self.add_judgments()
        para = next(o for o in self.state.objects if o['id'] == 'paragraph')
        para['content']['text'] += ' 先计算池内差值，再等权汇总。解析した結果です。การทดลองทางวิทยาศาสตร์'
        judgment = next(o for o in self.state.objects if o['id'] == 'J')
        judgment['content']['statement'] = '先计算池内差值，再等权汇总。'
        work, _ = self.place()
        queries = ('池内差值', '如何汇总配对差值', '解析した', 'ทดลอง', '秋風夜雨')
        matches = self.values(work, [('recall_passages', {'query': q}) for q in queries] + [
            ('recall_judgments', {'query': '如何汇总配对差值'})])
        for response in matches[:4]:
            self.assertEqual([r['id'] for r in response['results']], ['paragraph'])
        self.assertEqual(matches[4]['results'], [])
        self.assertEqual([r['object']['id'] for r in matches[5]['results']], ['J'])

    def test_native_budget_counts_escaping_and_caps_repeated_deliveries(self):
        from seed.work.tools.memory.query import NATIVE_RECEIPT_BYTES, bounded, encode
        # A single large row and a dense mix of quotes, escapes and UTF-8 text.
        rows = [{'text': '中' * 20000}] + [{'text': '"\\中\n' * 50, 'i': i} for i in range(150)]
        page = bounded(rows, 16 << 20)
        self.assertLessEqual(len(encode({'content': [{'type': 'text', 'text': encode(page).decode()}],
                                         'isError': False})), NATIVE_RECEIPT_BYTES)
        self.assertGreater(page['omitted'], 1)
        self.assertEqual(page['results'], rows[1:1 + len(page['results'])])
        self.assertEqual(len(page['results']) + page['omitted'], len(rows))

        original = self.state.deliveries[-1]
        for seq in range(10, 22):
            self.state.deliveries.append(dict(original, seq=seq, at=f'{seq:04}', invocation_id=str(seq)))
        work, _ = self.place()
        for page in self.values(work, [('get_evidence', {'id': 'paragraph', 'max_bytes': 16 << 20}),
                                       ('recall_passages', {'query': 'quoted'})]):
            row, = page['results']
            self.assertEqual(row['delivery_count'], 14)
            self.assertEqual([d['invocation_id'] for d in row['deliveries']], ['21', '20', '19'])

    def test_exact_delivery_filters_and_pages_preserve_historical_citations(self):
        from rsi.memory_history import passage_index, returned_memory

        original = self.state.deliveries[-1]
        self.state.deliveries.extend(dict(original, seq=n, at=str(n), invocation_id=str(n)) for n in range(10, 19))
        work, _ = self.place()
        key = {'id': 'paragraph', 'revision': 1}
        addresses = {'source_run': 'R1', 'document_id': 'doc_a', 'document_version': 'va', 'invocation_id': 'call-6'}
        early, wrong, hidden = self.values(work, [('get_evidence', {**key, **addresses}),
            ('get_evidence', {**key, **addresses, 'invocation_id': '17'}),
            ('get_evidence', {**key, 'source_run': 'R2'})])
        row, = early['results']
        self.assertEqual((row['delivery_count'], row['deliveries'][0]['run']), (1, 'R1'))
        self.assertEqual([s['acquisition_id'] for s in row['sources']], ['get_a'])
        self.assertEqual(wrong['results'], hidden['results'])
        self.assertFalse(hidden['results'])
        trusted = passage_index([json.loads(line) for line in (work / 'memory/evidence.jsonl').read_bytes().splitlines()])
        [(address, proof)] = returned_memory('get_evidence', early, trusted)
        self.assertEqual((address[:2], proof['source_run'], proof['historical_invocation']), (('doc_a', 'va'), 'R1', 'call-6'))
        deliveries, offset = [], 0
        while offset is not None:
            page, = self.values(work, [('get_evidence', {**key, 'delivery_offset': offset})])
            row, = page['results']
            deliveries.extend(d['invocation_id'] for d in row['deliveries'])
            offset = row.get('next_delivery_offset')
        self.assertEqual(deliveries, [str(n) for n in reversed(range(10, 19))] + ['call-7', 'call-6'])

    def test_search_continuations_visit_every_ranked_result_once(self):
        from seed.work.tools.memory.query import Snapshot

        self.add_judgments()
        original = next(o for o in self.state.objects if o['id'] == 'paragraph')
        for seq in range(21, 28):
            ident = f'passage_{seq}'
            self.state.objects.append({**original, 'id': ident})
            self.state.deliveries.append(receipt(ident, 1, seq, 'A', 'R1', document_id='doc_a', document_version='va'))
        work, _ = self.place('live')
        snapshot = Snapshot(work / 'memory')
        cases = [('lookup_sources', 'PMID:123', snapshot.lookup('PMID:123')),
                 ('recall_objects', 'paired', snapshot.recall_objects('paired')),
                 ('recall_judgments', 'paired', snapshot.recall_judgments('paired')),
                 ('recall_passages', 'quoted', snapshot.recall('quoted', None, None, None))]

        def identity(row):
            obj = row.get('get_evidence', row.get('object', row))
            return obj['id'], obj['revision']

        for name, query, ranked in cases:
            with self.subTest(tool=name):
                expected = [identity(row) for row in ranked]
                self.assertGreater(len(expected), 1)
                offset, found = 0, []
                while offset is not None:
                    page, = self.values(work, [(name, {'query': query, 'offset': offset, 'max_bytes': 1050})])
                    self.assertTrue(page['results'])
                    found.extend(identity(row) for row in page['results'])
                    self.assertEqual(page['omitted'], len(expected) - len(found))
                    offset = page.get('next_offset')
                    self.assertIn(offset, (None, len(found)))
                self.assertEqual(found, expected)

    def test_oversize_revision_has_a_search_pointer_and_lossless_unicode_pages(self):
        from rsi.memory_store import encode
        from seed.work.tools.memory.query import NATIVE_RECEIPT_BYTES

        self.add_judgments()
        obj = next(o for o in self.state.objects if o['id'] == 'J')
        obj['content']['statement'] = '存🧬"\\\n' * 8000
        work, _ = self.place('live')
        expected = next(row['object'] for row in map(json.loads, (work / 'memory/evidence.jsonl').read_bytes().splitlines())
                        if row['object']['id'] == 'J')
        found, = self.values(work, [('recall_judgments', {'query': 'J'})])
        key = found['results'][0]['get_evidence']
        self.assertEqual(key, {'id': 'J', 'revision': 1})
        chunks, args = [], key
        while True:
            reply, = self.mcp(work, [('get_evidence', args)])
            self.assertFalse(reply['isError'])
            self.assertLessEqual(len(encode(reply)), NATIVE_RECEIPT_BYTES)
            page = json.loads(reply['content'][0]['text'])
            part = page['results'][0]['object_part']
            self.assertEqual(part['offset'], args.get('object_offset', 0))
            self.assertTrue(part['text'])
            chunks.append(part['text'])
            if 'next_object_offset' not in page:
                break
            self.assertEqual(page['next_object_offset'], part['offset'] + len(part['text']))
            args = {**key, 'object_offset': page['next_object_offset']}
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks).encode(), encode(expected))

    def test_latest_visible_revision_can_have_a_hidden_predecessor(self):
        work, _ = self.place('trial', 'A')
        current = self.values(work, [('get_evidence', {'id': 'paper'})])[0]['results'][0]['object']
        self.assertEqual(current['revision'], 2)
        self.assertNotIn('supersedes', current)
        self.assertNotIn('record_sha256', current)

    def test_existing_out_of_order_ledger_drops_wrong_supersedes_from_snapshot(self):
        # Older controllers recorded the late backfill of alpha as revision 2 superseding revision 1.
        self.state.deliveries[0]['run'] = 'R9'
        self.state.objects[0]['content']['doi'] = '10.1/corrected'
        self.state.objects[1]['content']['doi'] = '10.1/alpha'
        work, _ = self.place('live')
        latest, backfill = self.values(work, [('get_evidence', {'id': 'paper'}),
                                               ('get_evidence', {'id': 'paper', 'revision': 2})])
        self.assertEqual(latest['results'][0]['object']['content']['doi'], '10.1/corrected')
        self.assertNotIn('supersedes', backfill['results'][0]['object'])
        self.assertNotIn('record_sha256', backfill['results'][0]['object'])
        self.assertEqual(self.state.objects[1]['supersedes'], 1)

    def test_legacy_snapshot_bytes_remain_replayable(self):
        expected = {'live': '94898f4680e3d112af271763abb2d68b19a25799f6135eb4bf8bc51ae1fe277b',
                    'trial': 'cdc9a12ecb1c2770cbef34f95fa1feea8366272864c9e888b5c5165de951b87b',
                    'none': '24c428218cdac1b1d2fc6a587565155e4d241e5a5fc041c947d66264580f8642'}
        for condition, fingerprint in expected.items():
            with self.subTest(condition=condition):
                self.state.reports['R3'] = {'version_after': self.state.version}
                trusted_passages(self.state, {'condition': condition, 'task_filter': 'T', 'version': self.state.version,
                                              'fingerprint': fingerprint})
        # Version 2 annotates freshness by registration order, before late paper backfills were handled.
        trusted_passages(self.state, {'condition': 'live', 'version': self.state.version, 'projection_version': 2,
                                      'unavailable_sources': [],
                                      'fingerprint': 'c8c97276250bf110724310d6ced8071afe2930900c4f8d6e8898b2b8b012df4e'})

    def add_judgments(self):
        citation = dict(id='paragraph', revision=1, source_run='R1', document_id='doc_a', document_version='va',
                        source_sha256=SHA, locator='block:004', quote='quoted finding', relation='supports',
                        historical_invocation='call-6')

        def authored(identity, kind, task, *, objects=(), sources=(), **content):
            seq = len(self.state.deliveries) + 1
            run = 'R' + str(seq)
            obj = object_row(identity, 1, run, task, kind, **content)
            obj.update(kind=kind, object_refs=list(objects), source_refs=list(sources),
                       question_ids=['Q'] if kind == 'judgment' else [])
            self.state.objects.append(obj)
            self.state.deliveries.append(receipt(identity, 1, seq, task, run))

        authored('Q', 'question', 'B', text='Compare paired scores.')
        question = dict(id='Q', revision=1, relation='background')
        content = dict(statement='Use paired differences.', scope='Two observations per pool.',
                       support_status='pending_review')
        authored('J', 'judgment', 'B', objects=[question], sources=[citation], **content)
        hidden = dict(citation, source_run='R2', document_id='secret_alias',
                      document_version='vs', historical_invocation='call-5')
        authored('alias-owner', 'judgment', 'B', objects=[question], sources=[hidden], **content)
        authored('derived', 'judgment', 'C', sources=[citation], **content,
                 objects=[question, dict(id='alias-owner', revision=1, relation='background')])
        authored('from-T', 'judgment', 'T', objects=[question], sources=[citation], **content)
        # An independent delivery cannot give a judgment a different author.
        self.state.deliveries.append(receipt('from-T', 1, 16, 'A', 'R16'))

    def test_get_and_recall_return_pinned_passage_question_and_pending_judgment(self):
        self.add_judgments()
        work, _ = self.place('live')
        lookup, recall = self.values(work, [('get_evidence', {'id': 'J', 'revision': 1}),
                                             ('recall_judgments', {'query': 'paired differences'})])
        item, = lookup['results']
        self.assertIn(item, recall['results'])
        self.assertEqual(item['object']['content']['scope'], 'Two observations per pool.')
        self.assertEqual(item['object']['content']['support_status'], 'pending_review')
        self.assertEqual(item['questions'][0]['content']['text'], 'Compare paired scores.')
        self.assertEqual(item['evidence'][0]['reference']['document_id'], 'doc_a')
        self.assertIn(item['evidence'][0]['reference']['quote'], item['evidence'][0]['passage']['text'])

    def test_judgment_recall_matches_words_from_id_and_origin_task(self):
        identity = 'judgment_pool-analysis-protocol_paired-contrasts'
        task = 'pool-analysis-readout'
        judgment = object_row(identity, 1, 'R4', task, 'judgment', statement='Use a difference.',
                              scope='Within each group.', support_status='pending_review')
        judgment['kind'] = 'judgment'
        self.state.objects.append(judgment)
        self.state.deliveries.append(receipt(identity, 1, 8, task, 'R4'))
        work, _ = self.place()
        calls = [('recall_judgments', {'query': query}) for query in ('paired', 'pool', 'readout', identity)]
        calls.append(('recall_objects', {'query': 'paired_contrasts', 'kind': 'judgment'}))
        for response in self.values(work, calls):
            self.assertEqual([r['object']['id'] for r in response['results']], [identity])

    def test_expanded_evidence_can_be_omitted_without_losing_the_exact_judgment(self):
        from rsi.memory_history import returned_objects
        from rsi.memory_store import encode

        self.add_judgments()
        passage = next(o for o in self.state.objects if o['id'] == 'paragraph')
        passage['content']['text'] += ' Large supporting passage.' * 2500
        challenger = next(o for o in self.state.objects if o['id'] == 'derived')
        challenger['object_refs'].append({'id': 'J', 'revision': 1, 'relation': 'challenges'})
        work, _ = self.place('live')
        obj = next(row['object'] for row in map(json.loads, (work / 'memory/evidence.jsonl').read_bytes().splitlines())
                   if row['object']['id'] == 'J')
        calls = [('get_evidence', {'id': 'J'}), ('recall_judgments', {'query': 'J', 'max_bytes': 16 << 20})]
        for (name, _), reply in zip(calls, self.values(work, calls)):
            row = next(r for r in reply['results'] if r['object']['id'] == 'J')
            self.assertEqual(row['object'], obj)
            self.assertEqual(row['omitted_details'], {'questions': 1, 'related_objects': 1, 'evidence': 1,
                                                     'counterevidence': 1})
            self.assertEqual([(r['id'], r['revision']) for r in row['counterevidence']], [('derived', 1)])
            self.assertEqual(list(returned_objects(name, reply, {('J', 1): encode(obj)})), [('J', 1)])
        tiny, = self.values(work, [('get_evidence', {'id': 'J', 'max_bytes': 64})])
        self.assertEqual(tiny, {'results': [], 'omitted': 1, 'next_counterevidence_offset': 0})

    def test_trial_drops_authored_revisions_hidden_aliases_and_transitive_dependents(self):
        self.add_judgments()
        work, _ = self.place('trial', 'T')
        raw = (work / 'memory' / 'evidence.jsonl').read_bytes()
        for private in (b'alias-owner', b'from-T', b'derived', b'secret_alias', b'call-5'):
            self.assertNotIn(private, raw)
        found, = self.values(work, [('recall_judgments', {'query': 'paired'})])
        self.assertEqual([item['object']['id'] for item in found['results']], ['J'])
        other, _ = self.place('trial', 'B', name='trial-B')
        self.assertEqual(self.values(other, [('recall_judgments', {'query': 'paired'})])[0]['results'], [])

    def test_trial_groups_historical_task_spellings_and_replays_prior_rules(self):
        self.add_judgments()
        self.state.reports['R3'] = {'version_after': self.state.version}
        work, meta = self.place('trial', 't')
        rows = [json.loads(line) for line in (work / 'memory/evidence.jsonl').read_bytes().splitlines()]
        self.assertEqual(trusted_rows(self.state, meta), rows)
        self.assertEqual({r['object']['id'] for r in rows}, {'paper', 'get_a', 'get_b', 'paragraph', 'Q', 'J'})
        self.assertTrue(all(d['task'] != 'T' for row in rows for d in row['deliveries']))

        # Version 3 used exact spelling. Its recorded fingerprint must still reproduce that view.
        historical = {**meta, 'projection_version': 3,
                      'fingerprint': 'c71be9c6c41206984536926dab7e443254a3589a64ac982b945ed30b635a4e07'}
        self.assertIn('from-T', [r['object']['id'] for r in trusted_rows(self.state, historical)])

    def test_case_distinct_task_directories_remain_independent_during_replay(self):
        self.add_judgments()
        self.state.reports['R3'] = {'version_after': self.state.version}
        # Model directory listings from a case-sensitive volume, including on macOS CI.
        first, other = self.parent / 't', self.parent / 'other' / 'T'
        first.mkdir()
        other.mkdir(parents=True)
        self.layout.tasks.mkdir(parents=True)
        (self.layout.tasks / 't').symlink_to(first, target_is_directory=True)
        with patch.object(Path, 'iterdir', return_value=iter([first, other])):
            _, meta = self.place('trial', 't')
        self.assertEqual(meta['case_distinct_tasks'], ['T'])
        self.assertIn('from-T', [r['object']['id'] for r in trusted_rows(self.state, meta)])

    def test_budget_includes_judgment_together_with_all_referenced_revisions(self):
        self.add_judgments()
        _, all_rows = _project(self.state, 'trial', 'T', 1 << 20)
        needed = {'J', 'Q', 'paragraph'}
        size = sum(len(_encode(row)) for row in all_rows if row['object']['id'] in needed)
        for budget, expect_judgment in ((size, True), (size - 1, False)):
            status, rows = _project(self.state, 'trial', 'T', budget)
            keys = {r['object']['id'] for r in rows}
            self.assertEqual('J' in keys, expect_judgment)
            if expect_judgment:
                self.assertEqual(keys, needed)
            else:
                self.assertEqual(status['omitted']['judgment'], 1)
            self.assertLessEqual(sum(len(_encode(row)) for row in rows), budget)

    def test_current_read_stays_bound_to_the_author_run_and_its_invocation(self):
        self.add_judgments()
        judgment = next(obj for obj in self.state.objects if obj['id'] == 'J')
        ref, = judgment['source_refs']
        ref.pop('historical_invocation')
        run = judgment['origin']['run']
        ref.update(delivery_source='literature', source_run=run, invocation_id='current-read')
        # An older delivery already exposes the same text, revision and document address.
        def visible():
            _, rows = _project(self.state, 'live', None, 1 << 20)
            return any(r['object']['id'] == 'J' for r in rows)
        self.assertFalse(visible())
        self.state.deliveries.append(receipt('paragraph', 1, 20, 'B', run, document_id='doc_a',
                                            document_version='va', invocation_id='current-read'))
        self.assertTrue(visible())
        ref.update(source_run='R1', invocation_id='call-6')
        self.assertFalse(visible())

    def test_hidden_revisions_and_owners_cannot_affect_visible_dependencies(self):
        self.add_judgments()
        question = next(obj for obj in self.state.objects if obj['id'] == 'Q')
        self.state.objects.append(dict(question, revision=2, supersedes=1, origin=dict(question['origin'], task='T')))
        self.state.deliveries.append(receipt('Q', 2, 18, 'T', 'R18'))
        self.state.owner_events = [{'type': 'withdrawn', 'id': 'Q', 'revision': 2, 'seq': 19}]
        # A withdrawn later question no longer makes the valid earlier pin stale.
        def views(condition):
            _, rows = _project(self.state, condition, 'T', 1 << 20, unavailable=set())
            return {(r['object']['id'], r['object']['revision']): r for r in rows}
        live = views('live')
        self.assertEqual(live['Q', 2]['freshness'], 'unavailable')
        self.assertEqual(live['J', 1]['freshness'], 'current')
        trial = views('trial')
        for key in [('Q', 1), ('paragraph', 1), ('J', 1)]:
            self.assertEqual(trial[key]['freshness'], 'current')
        self.assertNotIn(('Q', 2), trial)
        self.state.owner_events.append({'type': 'withdrawn', 'id': 'Q', 'revision': 1, 'seq': 20})
        self.assertEqual(views('trial')['J', 1]['freshness'], 'unavailable')

    def test_withdrawal_restores_paper_identifiers_and_preserves_the_pinned_revision(self):
        self.state.owner_events = [{'type': 'withdrawn', 'id': 'paper', 'revision': 2, 'seq': 20}]
        self.state.reports['R3'] = {'version_after': self.state.version}
        # Actual projection-4 fingerprint of this withdrawal before the fallback rule changed.
        old = trusted_rows(self.state, dict(condition='live', version=20, projection_version=4,
                           unavailable_sources=[], fingerprint='7760de5f6fc18ad79c62579221e3bfe8016023caaad25205b1ff5996b6b6769a'))
        previous = next(r for r in old if r['object']['id'] == 'paper' and r['object']['revision'] == 1)
        self.assertEqual(previous['freshness_reasons'], ['newer_visible_revision'])
        work, memory = self.place('live')
        trusted_rows(self.state, memory)
        current, pinned, right, wrong = self.values(work, [('get_evidence', {'id': 'paper'}),
            ('get_evidence', {'id': 'paper', 'revision': 2}), ('lookup_sources', {'query': '10.1/alpha'}),
            ('lookup_sources', {'query': '10.1/secret'})])
        current, = current['results']
        self.assertEqual((current['object']['revision'], current['freshness']), (1, 'current'))
        self.assertEqual([r['type'] for r in right['results']], ['paper', 'acquisition', 'passage'])
        self.assertFalse(wrong['results'])
        self.assertEqual(pinned['results'][0]['freshness_reasons'], ['withdrawn'])
        self.state.owner_events.append(dict(type='withdrawn', id='paper', revision=1, seq=21))
        work, _ = self.place('live', name='all-withdrawn')
        current, search, exact = self.values(work, [('get_evidence', {'id': 'paper'}),
            ('recall_objects', {'query': 'Memory paper'}), ('get_evidence', {'id': 'paper', 'revision': 1})])
        self.assertFalse(current['results'])
        self.assertNotIn('paper', [r['object']['id'] for r in search['results']])
        self.assertEqual(exact['results'][0]['freshness_reasons'], ['withdrawn'])

    def test_source_changes_and_unavailable_parent_propagate_and_replay_after_restoration(self):
        self.add_judgments()
        parent = hashlib.sha256(b'original PDF').hexdigest()
        self.state.deliveries[5]['derived_from'] = {'source_sha256': parent, 'document_id': 'parent',
                                                   'document_version': 'v1'}
        self.state.owner_events = [{'type': 'source_changed', 'source_sha256': SHA, 'seq': 19}]
        _, rows = _project(self.state, 'live', None, 1 << 20, unavailable=set())
        freshness = {r['object']['id']: r['freshness'] for r in rows}
        self.assertEqual({freshness[k] for k in ('paragraph', 'J', 'derived')}, {'needs_review'})
        self.assertEqual(freshness['Q'], 'current')

        target = next(obj for obj in self.state.objects if obj['id'] == 'J')
        alternate = dict(target, id='alternate', source_refs=[
            dict(target['source_refs'][0], document_id='doc_b', document_version='vb', source_run='R3',
                 historical_invocation='call-7')])
        self.state.objects.append(alternate)
        self.state.deliveries.append(receipt('alternate', 1, 18, 'B', 'R18'))
        with patch('rsi.memory_snapshot.original_problem', side_effect=lambda _, sha: sha == parent):
            work, memory = self.place('trial', 'T')
        item, other, subset = self.values(work, [('get_evidence', {'id': 'J'}),
                                                 ('get_evidence', {'id': 'alternate'}),
                                                 ('recall_passages', {'query': 'quoted', 'task': 'A'})])
        self.assertEqual(item['results'][0]['evidence'][0]['freshness'], 'unavailable')
        self.assertEqual(item['results'][0]['freshness'], 'unavailable')
        self.assertEqual(other['results'][0]['freshness'], 'needs_review')
        self.assertEqual(other['results'][0]['evidence'][0]['freshness'], 'needs_review')
        self.assertEqual(subset['results'][0]['freshness'], 'unavailable')
        self.assertEqual(memory['unavailable_sources'], [parent])
        self.state.reports['R3'] = {'version_after': self.state.version}
        with patch('rsi.memory_snapshot.original_problem', side_effect=AssertionError('replay checked live file')):
            trusted_passages(self.state, memory)

    def test_general_retrieval_prioritizes_challenges_and_excludes_hidden_backlinks(self):
        self.add_judgments()
        target = next(obj for obj in self.state.objects if obj['id'] == 'J')
        decision = dict(target, id='D', kind='decision', object_refs=[{'id': 'J', 'revision': 1,
                                                                        'relation': 'challenges'}], question_ids=[],
                        content={'type': 'decision', 'decision': 'Use paired contrasts', 'scope': ''})
        self.state.objects.append(decision)
        self.state.deliveries.append(receipt('D', 1, 17, 'B', 'R17'))
        hidden = dict(decision, id='secret-challenge', origin=dict(decision['origin'], task='T'))
        self.state.objects.append(hidden)
        self.state.deliveries.append(receipt('secret-challenge', 1, 18, 'T', 'R18'))
        work, _ = self.place('trial', 'T')
        found, sliced, linked = self.values(work, [('recall_objects', {'query': 'paired'}),
                                                   ('recall_objects', {'query': 'paired', 'kind': 'decision'}),
                                                   ('get_evidence', {'id': 'J', 'revision': 1})])
        self.assertEqual(found['results'][0]['object']['id'], 'D')
        item, = sliced['results']
        self.assertEqual(item['object']['content']['scope'], '')
        pin, = item['related_objects']
        self.assertEqual(pin['reference'], {'id': 'J', 'revision': 1, 'relation': 'challenges'})
        self.assertEqual(pin['object'], linked['results'][0]['object'])
        self.assertEqual([o['id'] for o in linked['results'][0]['counterevidence']], ['D'])

    def test_new_judgment_discloses_challenges_and_their_original_target_revisions(self):
        self.add_judgments()
        target = next(o for o in self.state.objects if o['id'] == 'J')
        pin = {'id': 'J', 'revision': 1, 'relation': 'challenges'}
        self.state.objects.append(dict(target, revision=2, supersedes=1))
        self.state.deliveries.append(receipt('J', 2, 18, 'B', 'R18'))
        for n, (name, task, refs) in enumerate([('old', 'A', [pin]), ('both', 'A', [pin, dict(pin, revision=2)]),
                                                ('hidden', 'T', [pin])], 19):
            self.state.objects.append(dict(target, id=name, object_refs=refs, question_ids=[], source_refs=[],
                                           origin=dict(target['origin'], task=task)))
            self.state.deliveries.append(receipt(name, 1, n, task, 'R' + str(n)))
        work, _ = self.place('trial', 'T')
        current, exact, recall = self.values(work, [('get_evidence', {'id': 'J'}),
            ('get_evidence', {'id': 'J', 'revision': 1}), ('recall_judgments', {'query': 'J'})])
        active, = current['results']
        self.assertEqual(active['object']['revision'], 2)
        self.assertEqual([(r['id'], r['challenged_revisions']) for r in active['counterevidence']],
                         [('both', [1, 2]), ('old', [1])])
        self.assertIn(active, recall['results'])
        former, = exact['results']
        self.assertEqual({r['id']: r['challenged_revisions'] for r in former['counterevidence']}, {'old': [1], 'both': [1]})
        # Canonical object pages expose a compact first challenge and a path to every remaining pin.
        from rsi.memory_history import returned_objects
        from rsi.memory_store import encode
        quote = 'q' * 3900
        next(o for o in self.state.objects if o['id'] == 'paragraph')['content']['text'] += quote
        large = next(o for o in self.state.objects if o['id'] == 'J' and o['revision'] == 2)
        large['source_refs'] = [dict(target['source_refs'][0], quote=quote) for _ in range(14)]
        work, _ = self.place('trial', 'T', name='oversized-judgment')
        trusted = {(r['object']['id'], r['object']['revision']): encode(r['object']) for r in
                   map(json.loads, (work / 'memory/evidence.jsonl').read_bytes().splitlines())}
        target = {'id': 'J', 'revision': 2}
        part, = self.values(work, [('get_evidence', target)])
        fragment, = part['results']
        self.assertGreater(part['next_object_offset'], 0)
        self.assertEqual(fragment['object_part']['offset'], 0)
        self.assertEqual(fragment['counterevidence'][0]['challenged_revisions'], [1, 2])
        self.assertEqual(fragment['omitted_details']['counterevidence_addresses'], 1)
        more, = self.values(work, [('get_evidence', {**target, 'counterevidence_offset': fragment['next_counterevidence_offset']})])
        self.assertEqual(more['results'][0]['challenge']['id'], 'old')
        self.assertEqual(more['results'][0]['challenge']['challenged_revisions'], [1])
        self.assertEqual(list(returned_objects('get_evidence', more, trusted)), [])
        # A small list page can resume without repeating a challenge or consuming object-part credit.
        first, = self.values(work, [('get_evidence', {**target, 'counterevidence_offset': 0, 'max_bytes': 233})])
        self.assertEqual(first['next_offset'], 1)
        self.assertEqual(first['results'][0]['challenge']['id'], 'both')

    def test_review_precedes_more_recent_healthy_matches(self):
        self.add_judgments()
        question = next(obj for obj in self.state.objects if obj['id'] == 'Q')
        self.state.objects.append(dict(question, revision=2, supersedes=1, content={'type': 'question',
                                                                                   'text': 'New paired question'}))
        self.state.deliveries.append(receipt('Q', 2, 18, 'B', 'R18'))
        with patch('rsi.memory_snapshot.original_problem', return_value=None):
            work, _ = self.place('trial', 'T')
        result, pinned, by_kind = self.values(work, [('recall_objects', {'query': 'paired'}),
                                                      ('get_evidence', {'id': 'J'}),
                                                      ('recall_objects', {'query': 'paired', 'kind': 'judgment'})])
        self.assertEqual([(r['object']['id'], r['freshness']) for r in result['results']],
                         [('J', 'needs_review'), ('Q', 'current')])
        self.assertEqual(pinned['results'], [result['results'][0]])
        self.assertEqual(by_kind['results'], pinned['results'])

    def test_capacity_omissions_have_no_original_observations_or_backlinks(self):
        self.add_judgments()
        hidden = 'f' * 64
        hidden_doc = {'document_id': 'hidden', 'document_version': 'v', 'source_sha256': hidden}
        self.state.objects.append(object_row('large', 1, 'R4', 'B', 'passage', text='x' * 7000,
                                             locator='block:1', parser_version='1', source_sha256=hidden))
        self.state.deliveries.append(receipt('large', 1, 17, 'B', 'R4', **hidden_doc))
        target = next(obj for obj in self.state.objects if obj['id'] == 'J')
        self.state.objects.append(dict(target, id='oversized-backlink', object_refs=[
            {'id': 'J', 'revision': 1, 'relation': 'challenges'}], question_ids=[],
            content=dict(target['content'], statement='z' * 7000)))
        self.state.deliveries.append(receipt('oversized-backlink', 1, 18, 'B', 'R5'))
        with patch('rsi.memory_snapshot.original_problem', return_value='missing') as check:
            work, memory = self.place('trial', 'T', max_bytes=7000)
        self.assertNotIn(hidden, str(check.call_args_list))
        self.assertNotIn(hidden, str(memory))
        blob = (work / 'memory' / 'evidence.jsonl').read_bytes()
        self.assertNotIn(hidden.encode(), blob)
        self.assertNotIn(b'oversized-backlink', blob)
        result, = self.values(work, [('get_evidence', {'id': 'J'})])
        self.assertNotIn('counterevidence', result['results'][0])
        self.state.reports['R3'] = {'version_after': self.state.version}
        trusted_passages(self.state, memory)


if __name__ == '__main__':
    unittest.main()
