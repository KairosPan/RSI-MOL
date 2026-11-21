"""M6 needs the same delivered passage, provenance and start tree, including after a later ledger append."""

import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path

from rsi import memory_store as store
from rsi.memory_snapshot import place_snapshot
from rsi.paths import Layout

AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tasks/paired-validation-followup/audit.py'))
DOC = {'document_id': 'doc_a', 'document_version': 'v1', 'source_sha256': 'a' * 64}
ADDR = dict(DOC, locator='block:000001')
TEXT = 'The complete passage includes a test quote.'
SOURCE = 'R0010'


def returned(server, tool, number, result):
    return {'type': 'McpToolCall', 'id': 'native-' + str(number), 'server': server, 'tool': tool,
            'status': 'completed', 'result': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(result)}]}}


class MemoryAudit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.layout = Layout(self.root)
        self.layout.records.mkdir()
        store.init_store(self.layout)
        self.state = store.load(self.layout)
        self.data = {'type': 'passage', 'locator': ADDR['locator'], 'source_sha256': DOC['source_sha256'], 'text': TEXT}
        origin = {'run': SOURCE, 'task': 'A', 'version': 'V0001'}
        self.obj = {'id': 'P1', 'revision': 1, 'kind': 'evidence', 'content': self.data, 'origin': origin, 'supersedes': None}
        self.obj['record_sha256'] = store.digest(self.obj)
        self.delivery = dict(origin, id='P1', revision=1, at='2026-11-20T00:00:00Z', invocation_id='historical', **DOC)
        store.publish(self.layout, self.state, SOURCE, [('object', self.obj), ('delivery', self.delivery)], {})
        self.memory_row = {'id': 'P1', 'revision': 1, 'content': self.data, 'deliveries': [self.delivery]}
        self.citation = dict(ADDR, quote='test quote', evidence_id='P1', revision=1, source_run=SOURCE)
        self.literature = returned('literature', 'read_document', 2,
                                   {'status': 'ok', 'invocation_id': 'read', 'data': {'passages': [dict(ADDR, text=TEXT)]}})

    def record(self, condition, run, tree, items, citations):
        work = self.root / condition
        work.mkdir()
        memory = place_snapshot(self.layout, work, condition)
        folder = self.layout.records / 'runs' / run
        (folder / 'sessions').mkdir(parents=True)
        (folder / 'out').mkdir()
        events = [{'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 'lead', 'item': item}}
                  for item in items]
        raw = '\n'.join(map(json.dumps, events)) + '\n'
        files = {'sessions/lead.jsonl': raw.encode(), 'sessions/child.jsonl': raw.encode(),
                 'out/evidence.json': json.dumps(citations).encode()}
        for path, data in files.items():
            (folder / path).write_bytes(data)
        call = {'id': run, 'purpose': 'task', 'task': AUDIT['TASK'], 'status': 'finished', 'memory': memory,
                'version_before': 'V0001', 'tree': tree, 'thread_id': 'lead', 'subagent_threads': ['child'],
                'files': {p: hashlib.sha256(v).hexdigest() for p, v in files.items()},
                'started': '2026-11-20T00:00:00Z', 'ended': '2026-11-20T00:02:00Z', 'usage': {'input_tokens': 10},
                'check': {'passed': True}}
        (folder / 'call.json').write_text(json.dumps(call))
        return folder

    def test_paired_audit_preserves_original_snapshot_and_provenance(self):
        baseline = self.record('none', 'R0020', 'tree', [self.literature], [dict(self.citation, delivery_source='literature')])
        memory = returned('memory', 'get_evidence', 1, {'results': [{'object': self.obj, 'deliveries': [self.delivery]}]})
        live = self.record('live', 'R0021', 'tree', [self.literature, memory], [dict(self.citation, delivery_source='both')])
        store.publish(self.layout, self.state, 'R0022', [('delivery', dict(self.delivery, run='R0022'))], {})
        result = AUDIT['audit'](baseline, live)
        self.assertTrue(result['passed'])
        self.assertEqual(result['none']['literature_calls'], 1)  # inherited child event was not counted twice
        self.assertEqual(result['live']['elapsed_seconds'], 120)
        self.assertEqual(result['live']['citations_by_delivery']['both'], 1)
        row = result['live']['citations'][0]
        self.assertTrue(row['memory_provenance_matches'])
        self.assertEqual(row['memory_receipts'][0]['call_id'], 'native-1')

    def test_addresses_other_blocks_wrong_aliases_or_revised_text_do_not_count_as_delivery(self):
        baseline = self.record('none', 'R0020', 'tree', [self.literature], [dict(self.citation, delivery_source='literature')])
        wrong = dict(self.memory_row, deliveries=[dict(self.delivery, document_id='other')])
        forged = dict(self.memory_row, content=dict(self.data, text='Rewritten: test quote.'))
        other = dict(self.memory_row, content=dict(self.data, locator='block:000099'))
        address_only = returned('memory', 'lookup_sources', 1, {'results': [self.memory_row]})
        memory = returned('memory', 'recall_passages', 3, {'results': [wrong, forged, other]})
        live = self.record('live', 'R0021', 'tree', [self.literature, address_only, memory],
                           [dict(self.citation, delivery_source='memory')])
        result = AUDIT['audit'](baseline, live)
        self.assertFalse(result['passed'])
        self.assertEqual(result['live']['citations'][0]['observed'], 'literature')

    def test_provenance_is_separate_from_receipt_and_tree_must_match(self):
        baseline = self.record('none', 'R0020', 'oldtree', [self.literature], [dict(self.citation, delivery_source='literature')])
        item = returned('memory', 'recall_passages', 3, {'results': [self.memory_row]})
        live = self.record('live', 'R0021', 'newtree', [item], [dict(self.citation, delivery_source='memory', source_run='R0013'),
                                                                dict(self.citation, delivery_source='memory')])
        result = AUDIT['audit'](baseline, live)
        self.assertFalse(result['passed'])
        self.assertTrue(result['confirmed_backfilled_memory_citation'])
        wrong_source, good = result['live']['citations']
        self.assertEqual(wrong_source['observed'], 'memory')
        self.assertFalse(wrong_source['memory_provenance_matches'])
        self.assertTrue(good['memory_provenance_matches'])

    def test_failed_control_check_or_bad_row_does_not_erase_costs_or_provenance(self):
        baseline = self.record('none', 'R0020', 'tree', [self.literature], [])
        call_file = baseline / 'call.json'
        call = json.loads(call_file.read_bytes())
        del call['files']['out/evidence.json']
        call['check'] = {'passed': False, 'message': 'Evidence is missing.'}
        call_file.write_text(json.dumps(call))
        live = self.record('live', 'R0021', 'tree', [returned('memory', 'recall_passages', 1,
                           {'results': [self.memory_row]}), self.literature], [
                               dict(self.citation, delivery_source='memory'), {'quote': 'Missing address'}])
        result = AUDIT['audit'](baseline, live)
        self.assertTrue(result['passed'])
        self.assertFalse(result['none']['check']['passed'])
        self.assertTrue(result['live']['check']['passed'])
        self.assertEqual(result['none']['literature_calls'], 1)
        self.assertEqual(result['none']['citation_coverage']['status'], 'unavailable')
        self.assertIsNone(result['none']['citation_coverage']['rows'])
        self.assertEqual(result['live']['citation_coverage'], {'status': 'partial', 'rows': 2, 'auditable_rows': 1})
        self.assertEqual(result['live']['citations_by_delivery']['both'], 1)
        self.assertFalse(result['live']['citations'][0]['label_matches'])
        self.assertIn('document_id', result['live']['citations'][1]['audit_error'])

    def test_contiguous_utf8_pages_count_both_and_gaps_or_another_address_do_not(self):
        text = 'A café has two pages.'
        quote = 'fé has two'
        row = dict(self.citation, quote=quote, delivery_source='memory')
        source = {'id': 'P1', 'revision': 1, 'source_run': SOURCE, 'text': text}
        key = tuple(row[k] for k in AUDIT['ADDRESS'])
        def part(lo, hi, address=ADDR):
            return dict(address, text=text[lo:hi], char_range=[lo, hi],
                        byte_range=[len(text[:lo].encode()), len(text[:hi].encode())], parser_version='p')
        def classify(right):
            events = []
            for n, segment in enumerate([part(0, 8), right]):
                payload = {'status': 'ok', 'data': {'passages': [segment]}, 'invocation_id': str(n)}
                events.append(('child', returned('literature', 'read_document', n, payload)))
            _, _, lit = AUDIT['receipts'](events, {})
            return AUDIT['compare_citations']([row], {key: [source]}, lit)[0]
        for start in (7, 8):
            with self.subTest(start=start):
                match = classify(part(start, len(text)))
                self.assertEqual(match['observed'], 'both')
                self.assertTrue(match['backfilled_memory_citation'])  # The inaccurate label is reported separately.
                self.assertEqual({m['call_id'] for m in match['literature_receipts']}, {'native-0', 'native-1'})
        self.assertEqual(classify(part(9, len(text)))['observed'], 'memory')
        self.assertEqual(classify(part(8, len(text), dict(ADDR, document_id='other')))['observed'], 'memory')
        altered = part(7, len(text))
        altered['text'] = 'X' + altered['text'][1:]
        self.assertEqual(classify(altered)['observed'], 'memory')


if __name__ == '__main__':
    unittest.main()
