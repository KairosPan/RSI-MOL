"""Require a sealed submission and prior, verified deliveries for every judgment reference."""
import hashlib
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import memory_ingest as ingest, memory_store as ledger
from rsi.memory_snapshot import place_snapshot
from rsi.paths import Layout
from rsi.records import Records
from tests.test_memory_audit import ADDR, DOC, TEXT, returned


class Proposals(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.records = Records(self.layout.records)
        origin = {'task': 'history', 'run': 'R0000', 'version': 'V0001'}
        content = {'type': 'passage', 'text': TEXT, 'source_sha256': DOC['source_sha256'], 'locator': ADDR['locator']}
        obj = dict(kind='evidence', id='passage_1', revision=1, origin=origin, supersedes=None, content=content)
        obj['record_sha256'] = ledger.digest(obj)
        hist = dict(origin, id=obj['id'], revision=1, at='2026-01-01T00:00:00Z', invocation_id='original-read', **DOC)
        ledger.publish(self.layout, ledger.load(self.layout), 'R0000', [('object', obj), ('delivery', hist)], {})
        self.read = returned('memory', 'get_evidence', 'read', {'results': [{'object': obj, 'deliveries': [hist]}]})
        self.ref = dict(ADDR, relation='supports', evidence_id=obj['id'], revision=1, source_run='R0000', quote='test quote')
        self.args = dict(kind='judgment', id='judgment_paired', expected_revision=0, question_id='experiment',
                         statement='Use within-pool differences.', scope='Paired pools', source_refs=[self.ref])

    def write(self, num, args=None):
        pid = f'{num:032x}'
        item = returned('memory', 'propose_memory', num, {'status': 'pending', 'proposal_id': pid})
        item['arguments'] = args if args is not None else self.args
        return item

    def record(self, items, files, *, goal='Compare pairs', task='experiment', corrupt_snapshot=False, child=(),
               clocks=None, lead_prefix=()):
        run = self.records.new_id('R')
        directory = self.records.call_dir(run)
        staging = self.layout.root / run
        staging.mkdir()
        memory = place_snapshot(self.layout, staging)
        if corrupt_snapshot:
            memory['fingerprint'] = '0' * 64
        self.records.start_call(run, {'purpose': 'task', 'task': task, 'task_goal': goal, 'memory': memory,
                                      'thread_id': 'lead', 'subagent_threads': ['child'] if child else [],
                                      'version_before': 'V0001'})
        (directory / 'sessions').mkdir()
        sessions = {'lead': items, 'child': child} if child else {'lead': items}
        for thread, events in sessions.items():
            with (directory / 'sessions' / (thread + '.jsonl')).open('wb') as out:
                if thread == 'lead':
                    out.writelines(lead_prefix)
                for index, item in enumerate(events):
                    payload = dict(type='item_completed', thread_id=thread, item=item)
                    if item.get('type') == 'item_started':
                        payload = dict(item, thread_id=thread)
                    elif clocks is None:
                        payload.update(started_at_ms=index * 1000, completed_at_ms=index * 1000 + 1)
                    else:
                        payload.update(clocks.get(item['id'], {}))
                    event = {'type': 'event_msg', 'timestamp': f'2026-11-20T12:01:{index:02d}Z', 'payload': payload}
                    out.write(ledger.encode(event) + b'\n')
        (directory / 'proposals').mkdir()
        for num, value in files.items():
            (directory / 'proposals' / f'{num:032x}.json').write_bytes(ledger.encode(value) + b'\n')
        self.records.finish_call(run, {'status': 'finished'})
        report, = ingest.backfill(self.layout)
        return report

    def passage(self, original=b'<article>Fresh passage.</article>', **changes):
        sha = hashlib.sha256(original).hexdigest()
        path = self.layout.library / 'objects' / 'sha256' / sha
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
        text = 'Fresh passage.'
        row = dict(document_id='doc_' + 'a' * 64, document_version='b' * 64, source_sha256=sha,
                   locator='block:2', parser_version='1', kind='paragraph', text=text,
                   section='Methods', section_path=['Methods'], content_scope='full_text', block_complete=True,
                   char_range=[0, len(text)], byte_range=[0, len(text)], source_metadata={})
        return row | changes

    def literature(self, row, invocation='a' * 32, tool='find_passages', native_id='search'):
        native = returned('literature', tool, native_id, dict(status='ok', invocation_id=invocation,
                                                               data={'passages': [row]}))
        native['arguments'] = {'document_id': row['document_id']}
        ref = {k: row[k] for k in ('document_id', 'document_version', 'source_sha256', 'locator')}
        ref.update(delivery_source='literature', invocation_id=invocation, relation='supports', quote=row['text'])
        return native, ref

    def test_truncated_native_receipts_have_a_specific_diagnostic_and_give_no_credit(self):
        wrapper = {'isError': False, 'content': [{'type': 'text', 'text': '{"results":[…1234 chars truncated…]}'}]}
        memory = returned('memory', 'get_evidence', 'cut-read', wrapper)
        lit = returned('literature', 'find_passages', 'cut-literature', wrapper)
        failed = returned('memory', 'get_evidence', 'failed-read', {})
        failed['result'] = {'isError': True, 'content': [{'type': 'text', 'text': 'Tool error: …22 chars truncated…'}]}
        report = self.record([lit, memory, failed, self.write(1)], {1: self.args})
        self.assertEqual(report['reasons']['native tool receipt truncated by Codex'], 2)
        self.assertEqual(report['reasons'], {'native tool receipt truncated by Codex': 2,
                                             'evidence revision was not returned by memory in this run': 1})
        self.assertNotIn('judgment', report['objects'])

    def test_current_and_historical_references_preserve_both_native_calls(self):
        found, new = self.literature(self.passage())
        args = dict(self.args, source_refs=[self.ref, new])
        report = self.record([self.read, found, self.write(1, args)], {1: args})
        self.assertEqual(report['objects'], {'passage': 1, 'question': 1, 'judgment': 1})
        state = ledger.load(self.layout)
        passage, question, judgment = state.objects[-3:]
        history, current = judgment['source_refs']
        self.assertEqual(history['historical_invocation'], 'original-read')
        self.assertEqual(current, dict(new, id=passage['id'], revision=1, source_run=report['run'],
                                       thread='lead', call_id='native-search'))
        self.assertEqual(question['id'], judgment['object_refs'][0]['id'])
        # Redelivery of the same passage under a different invocation reuses its revision.
        again, another = self.literature(self.passage(), 'b' * 32, 'read_document', 'again')
        revised = dict(args, expected_revision=1, source_refs=[another])
        second = self.record([again, self.write(2, revised)], {2: revised})
        self.assertEqual(second['objects'], {'judgment': 1})
        proof, = ledger.load(self.layout).objects[-1]['source_refs']
        self.assertEqual((proof['source_run'], proof['id'], proof['revision']), (second['run'], passage['id'], 1))

    @staticmethod
    def object_parts(obj, deliveries, name):
        canonical = ledger.encode(obj).decode()
        bounds = (0, len(canonical) // 3, 2 * len(canonical) // 3, len(canonical))
        pages = []
        for i, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
            part = dict(id=obj['id'], revision=obj['revision'], offset=lo, total_chars=len(canonical),
                        text=canonical[lo:hi])
            result = {'results': [{'object_part': part, 'deliveries': deliveries}], 'omitted': 0}
            if hi < len(canonical):
                result['next_object_offset'] = hi
            item = returned('memory', 'get_evidence', f'{name}-{i}', result)
            item['arguments'] = dict(id=obj['id'], revision=obj['revision'], object_offset=lo)
            pages.append(item)
        return pages

    def test_complete_object_and_passage_parts_credit_only_the_final_native_calls(self):
        sub = self.submit_subquestion()
        entry = json.loads(self.read['result']['content'][0]['text'])['results'][0]
        state = ledger.load(self.layout)
        for _ in range(4):
            run = self.records.new_id('R')
            delivery = dict(entry['deliveries'][0], run=run, invocation_id=run + '-later')
            ledger.publish(self.layout, state, run, [('delivery', delivery)], {})
        work = self.layout.root / 'paged-reader'
        work.mkdir()
        place_snapshot(self.layout, work)
        from tests.installed_seed import WORK
        package = WORK / 'tools/memory'
        with mock.patch.object(sys, 'path', [str(package), *sys.path]):
            reader = runpy.run_path(str(package / 'server.py'))['Server'](work / 'memory')
        key = dict(id=entry['object']['id'], revision=1)
        recent = reader.call('get_evidence', key)['results'][0]
        self.assertEqual(recent['delivery_count'], 5)
        self.assertNotIn('R0000', [d['run'] for d in recent['deliveries']])
        source = []
        query = dict(key, source_run='R0000', max_bytes=650, object_offset=0)
        while True:
            page = reader.call('get_evidence', query)
            self.assertEqual(page['results'][0]['deliveries'][0]['run'], 'R0000')
            item = returned('memory', 'get_evidence', f'passage-{len(source)}', page)
            item['arguments'] = query
            source.append(item)
            if 'next_object_offset' not in page:
                break
            query = dict(query, object_offset=page['next_object_offset'])
        self.assertGreater(len(source), 1)
        question = self.object_parts(sub, [], 'question')
        args = dict(self.args, question_id=sub['id'],
                    object_refs=[dict(id=sub['id'], revision=1, relation='background')])
        result = self.record([*source, *question, self.write(2, args)], {2: args})
        self.assertEqual(result['objects'], {'judgment': 1})
        registered = ledger.load(self.layout).objects[-1]
        self.assertEqual(registered['source_refs'][0]['call_id'], source[-1]['id'])
        self.assertEqual(registered['object_refs'][0]['call_id'], question[-1]['id'])
        # Audit the registered citation through the same sealed participant sessions and starting snapshot.
        from rsi.memory_history import trusted_rows, passage_index
        audit = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tasks/pool-analysis-readout/audit.py'))
        call = self.records.load_call(result['run'])
        state = ledger.load(self.layout)
        rows = trusted_rows(state, call['memory'])
        with mock.patch.dict(audit['native_reads'].__globals__, LAYOUT=self.layout):
            items, reads, starts = audit['native_reads'](call, rows)
        revisions = {(o['id'], o['revision']): o for o in state.objects}
        proof = audit['verify_source'](registered['source_refs'][0], items, passage_index(rows), revisions,
                                       reads, starts['lead', 'native-2'])
        self.assertEqual((proof['source_run'], proof['call_id']), ('R0000', source[-1]['id']))

    def test_partial_tampered_late_and_cross_thread_pages_cannot_support_a_proposal(self):
        entry = json.loads(self.read['result']['content'][0]['text'])['results'][0]
        first, middle, last = self.object_parts(entry['object'], entry['deliveries'], 'passage')
        damaged = json.loads(json.dumps(middle))
        body = json.loads(damaged['result']['content'][0]['text'])
        body['results'][0]['object_part']['text'] = 'X' + body['results'][0]['object_part']['text'][1:]
        damaged['result']['content'][0]['text'] = json.dumps(body)
        write = self.write(1)
        for scenario, events, opts in (
            ('missing initial segment', [middle, last, write], {}),
            ('missing middle segment', [first, last, write], {}),
            ('changed segment', [first, damaged, last, write], {}),
            ('late final segment', [first, middle, write, last], {}),
            ('different participants', [last, write], {'child': [first, middle]}),
            ('earlier segment has later completion clock', [first, middle, last, write],
             {'clocks': {first['id']: {'completed_at_ms': 100}, middle['id']: {'completed_at_ms': 2},
                         last['id']: {'completed_at_ms': 3}, write['id']: {'started_at_ms': 4, 'completed_at_ms': 5}}}),
        ):
            with self.subTest(scenario=scenario):
                result = self.record(events, {1: self.args}, **opts)
                self.assertNotIn('judgment', result['objects'])
                self.assertEqual(len(result['rejected']), 1)

    def test_current_reference_rejects_wrong_quote_address_invocation_and_ledger_fields(self):
        found, ref = self.literature(self.passage())
        variants = [dict(ref, quote='invented'), dict(ref, document_id='doc_' + '0' * 64),
                    dict(ref, invocation_id='b' * 32), dict(ref, source_run='R0000'), dict(ref, revision=1)]
        files = {i: dict(self.args, source_refs=[v]) for i, v in enumerate(variants, 1)}
        report = self.record([found, *(self.write(i, a) for i, a in files.items())], files)
        self.assertEqual(report['objects'], {'passage': 1})
        self.assertEqual(len(report['rejected']), len(variants))

    def test_an_invocation_with_two_blocks_binds_each_quote_to_its_own_locator(self):
        first = self.passage()
        second = self.passage(text='Other passage.', locator='block:3')
        found, ref = self.literature(first)
        payload = json.loads(found['result']['content'][0]['text'])
        payload['data']['passages'].append(second)
        found['result']['content'][0]['text'] = json.dumps(payload)
        right = dict(ref, locator=second['locator'], quote=second['text'])
        files = {1: dict(self.args, source_refs=[ref, right]),
                 2: dict(self.args, id='judgment_mismatch', source_refs=[dict(ref, quote=second['text'])])}
        report = self.record([found, self.write(1, files[1]), self.write(2, files[2])], files)
        self.assertEqual(report['objects'], {'passage': 2, 'question': 1, 'judgment': 1})
        self.assertIn('quotation does not occur verbatim in the returned evidence', report['reasons'])
        citations = ledger.load(self.layout).objects[-1]['source_refs']
        self.assertEqual([p['locator'] for p in citations], [first['locator'], second['locator']])
        self.assertNotEqual(citations[0]['id'], citations[1]['id'])

    def test_current_reference_requires_a_verified_full_block_even_if_memory_contains_it(self):
        row = self.passage()
        complete, ref = self.literature(row)
        self.record([complete], {})
        damaged = [dict(row, block_complete=False), dict(row, byte_range=[0, 999]),
                   dict(row, source_sha256='e' * 64)]
        for bad in damaged:
            with self.subTest(bad=bad):
                invalid, current = self.literature(bad)
                args = dict(self.args, source_refs=[current])
                report = self.record([invalid, self.write(1, args)], {1: args})
                self.assertEqual(report['objects'], {})
                self.assertIn('invocation has no verified complete passage delivery in this run', report['reasons'])
        bad, _ = self.literature(dict(row, text='conflict'), native_id='other')
        args = dict(self.args, source_refs=[ref])
        conflict = self.record([complete, bad, self.write(1, args)], {1: args})
        self.assertIn('conflicting invocation_id', conflict['reasons'])
        self.assertEqual(conflict['deliveries'], {})

    def test_cross_thread_current_read_must_finish_before_proposal_begins(self):
        found, ref = self.literature(self.passage())
        args = dict(self.args, source_refs=[ref])
        for start, expected in ((90, False), (100, False), (None, False), (101, True)):
            with self.subTest(start=start):
                write = self.write(1, args)
                clocks = {found['id']: {'started_at_ms': 50, 'completed_at_ms': 100},
                          write['id']: {'completed_at_ms': 110}}
                if start is not None:
                    clocks[write['id']]['started_at_ms'] = start
                report = self.record([write], {1: args}, child=[found], clocks=clocks)
                self.assertEqual('judgment' in report['objects'], expected)
                if expected:
                    proof, = ledger.load(self.layout).objects[-1]['source_refs']
                    self.assertEqual(proof['thread'], 'child')

    def test_current_read_in_the_same_session_still_needs_a_verifiable_proposal_start(self):
        found, ref = self.literature(self.passage())
        args = dict(self.args, source_refs=[ref])
        write = self.write(1, args)
        unproven = self.record([found, write], {1: args}, clocks={})
        self.assertEqual(unproven['objects'], {'passage': 1})
        self.assertIn('without verifiable ordering', unproven['rejected'][0]['reason'])
        write['duration'] = {'secs': 0, 'nanos': 1000000}
        timed = self.record([found, write], {1: args}, clocks={})
        self.assertEqual(timed['objects'], {'question': 1, 'judgment': 1})

    def test_registration_and_optimistic_revisions(self):
        good = self.record([self.read, self.write(1)], {1: self.args})
        self.assertEqual(good['objects'], {'question': 1, 'judgment': 1})
        self.assertEqual(ingest.backfill(self.layout), [])
        next_args = dict(self.args, expected_revision=1, statement='Revised within-pool comparison.')
        second = self.record([self.read, self.write(1, next_args), self.write(2, next_args)], {1: next_args, 2: next_args},
                             goal='Compare fresh pairs')
        self.assertEqual(second['reasons'], {'expected judgment revision differs from the ledger': 1})
        obj = ledger.show(self.layout, self.args['id'])['versions'][-1]
        self.assertEqual((obj['revision'], obj['supersedes'], obj['object_refs'][0]['revision']), (2, 1, 2))
        self.assertEqual(obj['authority'], 'model_interpretation')
        self.assertEqual(obj['content']['support_status'], 'pending_review')
        proof, = obj['source_refs']
        self.assertEqual((proof['historical_invocation'], proof['call_id']), ('original-read', 'native-read'))

    def test_withdrawn_latest_judgment_uses_active_revision_for_optimistic_updates(self):
        self.record([self.read, self.write(1)], {1: self.args})
        edit = dict(self.args, expected_revision=1, statement='Revised interpretation.')
        self.record([self.read, self.write(2, edit)], {2: edit})
        ident = self.args['id']
        ledger.record_owner_action(self.layout, 'withdrawn', {'id': ident, 'revision': 2})
        stale = dict(edit, expected_revision=2)
        report = self.record([self.read, self.write(3, stale), self.write(4, edit)], {3: stale, 4: edit})
        self.assertEqual(report['reasons'], {'expected judgment revision differs from the ledger': 1})
        obj = ledger.show(self.layout, ident)['versions'][-1]
        self.assertEqual((obj['revision'], obj['supersedes']), (3, 1))
        for revision in (1, 3):
            ledger.record_owner_action(self.layout, 'withdrawn', {'id': ident, 'revision': revision})
        other = dict(self.args, question_id='another')
        rejected = self.record([self.read, self.write(5, other)], {5: other}, task='another')
        self.assertIn('judgment belongs to a different task or authority', rejected['reasons'])
        restored = self.record([self.read, self.write(6)], {6: self.args})
        self.assertEqual(restored['objects'], {'judgment': 1})
        self.assertEqual(ledger.show(self.layout, ident)['versions'][-1]['revision'], 4)

    def test_withdrawn_passage_can_be_corrected_and_both_native_reads_keep_their_text(self):
        from seed.work.tools.memory.query import Snapshot

        wrong, bad_ref = self.literature(self.passage(text='False passage.'))
        correct, good_ref = self.literature(self.passage(), 'b' * 32, native_id='correct')
        initial = dict(self.args, source_refs=[bad_ref])
        self.record([wrong, self.write(1, initial)], {1: initial})
        refused = self.record([correct], {})
        conflict = 'conflicting text or coordinates at the same passage address'
        self.assertEqual(refused['reasons'], {conflict: 1})
        pin, = ledger.show(self.layout, initial['id'])['versions'][0]['source_refs']
        ledger.record_owner_action(self.layout, 'withdrawn', {'id': pin['id'], 'revision': 1})
        # The first receipt repeats the withdrawn bytes. The next receipt corrects them in this same run.
        accepted = dict(self.args, id='judgment_corrected', source_refs=[good_ref])
        mixed = dict(self.args, id='judgment_mixed', source_refs=[dict(bad_ref, quote=good_ref['quote'])])
        report = self.record([wrong, correct, self.write(2, mixed), self.write(3, accepted)], {2: mixed, 3: accepted})
        self.assertEqual(report['objects'], {'passage': 1, 'judgment': 1})
        self.assertIn('quotation does not occur verbatim in the returned evidence', report['reasons'])
        self.assertEqual(ledger.show(self.layout, accepted['id'])['versions'][0]['source_refs'][0]['revision'], 2)
        history = ledger.show(self.layout, pin['id'])
        self.assertEqual([(o['revision'], o['supersedes'], o['content']['text']) for o in history['versions']],
                         [(1, None, bad_ref['quote']), (2, 1, good_ref['quote'])])
        self.assertEqual([d['revision'] for d in history['deliveries']], [1, 1, 2])
        again = self.record([wrong, correct], {})
        self.assertEqual(again['deliveries'], {'passage': 1})
        self.assertEqual(again['reasons'], {conflict: 1})
        work = self.layout.root / 'corrected'
        work.mkdir()
        place_snapshot(self.layout, work)
        snapshot = Snapshot(work / 'memory')
        for key, revision, level in [(pin['id'], None, 'current'), (pin['id'], 1, 'unavailable'),
                                     (initial['id'], None, 'unavailable'), (accepted['id'], None, 'current')]:
            row, = snapshot.get_evidence(key, revision)
            self.assertEqual(row['freshness'], level)
        self.assertEqual(snapshot.get_evidence(pin['id'], None)[0]['object']['revision'], 2)

    def test_single_submission_failures_leave_other_judgments_intact(self):
        bad_args = [dict(self.args, id='judgment_bad', source_refs=[dict(self.ref, **changes)]) for changes in
                    ({'quote': 'altered quote'}, {'document_id': 'other-doc'}, {'revision': 2}, {'source_run': 'R0009'})]
        files = {i: a for i, a in enumerate([self.args, *bad_args], 1)}
        writes = [self.write(i, args) for i, args in files.items()]
        files[6] = self.args  # workspace file only
        writes.append(self.write(7))  # native call only
        writes.append(self.write(8))
        files[8] = dict(self.args, statement='Changed after submission.')
        report = self.record([self.read, *writes], files)
        self.assertEqual(report['objects'], {'question': 1, 'judgment': 1})
        self.assertEqual({v['proposal_id'] for v in report['rejected']}, {f'{i:032x}' for i in range(2, 9)})

    def test_a_late_read_and_overlapping_read_cannot_support_the_submission(self):
        write = self.write(2)
        write['duration'] = {'secs': 2, 'nanos': 0}
        report = self.record([self.write(1), self.read, write], {1: self.args, 2: self.args}, clocks={})
        self.assertEqual(report['objects'], {})
        self.assertEqual(len(report['rejected']), 2)
        self.assertTrue(all('after the proposal' in r['reason'] for r in report['rejected']))

    def test_historical_read_needs_a_proven_start_even_in_one_session(self):
        write = self.write(1)
        marker = dict(type='item_started', item={k: write[k] for k in ('id', 'server', 'tool', 'type')})
        for items in ([self.read, write], [marker, self.read, write]):
            with self.subTest(items=len(items)):
                report = self.record(items, {1: self.args}, clocks={})
                self.assertEqual(report['objects'], {})
                self.assertIn('without verifiable ordering', report['rejected'][0]['reason'])
        report = self.record([self.read, marker, write], {1: self.args}, clocks={})
        self.assertEqual(report['objects'], {'question': 1, 'judgment': 1})

    def test_archived_start_clock_can_prove_historical_read_in_another_session(self):
        write = self.write(1)
        marker = dict(type='item_started', item={k: write[k] for k in ('id', 'server', 'tool', 'type')})
        clocks = {self.read['id']: {'completed_at_ms': 100}, write['id']: {'completed_at_ms': 200}}
        for start in (99, 100, 101):
            with self.subTest(start=start):
                result = self.record([dict(marker, started_at_ms=start), write], {1: self.args},
                                     clocks=clocks, child=[self.read])
                self.assertEqual('judgment' in result['objects'], start > 100)

    def test_one_large_session_can_register_both_evidence_and_judgment(self):
        found, ref = self.literature(self.passage())
        args = dict(self.args, source_refs=[ref])
        # 129 MiB of complete lines crosses the old per-file limit within the 256 MiB run budget.
        line = b'{"padding":"' + b' ' * ((1 << 20) - 15) + b'"}\n'
        result = self.record([found, self.write(1, args)], {1: args}, lead_prefix=(line for _ in range(129)))
        self.assertEqual(result['objects'], {'passage': 1, 'question': 1, 'judgment': 1})
        self.assertEqual(result['rejected'], [])

    def test_read_without_starting_snapshot_fingerprint_cannot_support_a_judgment(self):
        report = self.record([self.read, self.write(1)], {1: self.args}, corrupt_snapshot=True)
        self.assertEqual(report['objects'], {})
        self.assertIn('fingerprint', report['rejected'][0]['reason'])

    def test_current_paper_and_proposal_share_one_recoverable_batch(self):
        paper = {'paper_id': 'source:one', 'provider': 'source', 'title': 'Another study'}
        native = returned('literature', 'get_paper', 'paper', {
            'status': 'ok', 'data': {'paper': paper}, 'invocation_id': '1' * 32})
        native['arguments'] = {'paper_ref': 'source:one'}
        with mock.patch.object(ledger, 'finish_report', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            self.record([native, self.read, self.write(1)], {1: self.args})
        partial = ledger.load(self.layout)
        run, = partial.missing_reports
        report, = ingest.backfill(self.layout)
        self.assertEqual(report['run'], run)
        self.assertEqual(report['objects'], {'paper': 1, 'question': 1, 'judgment': 1})
        self.assertEqual((partial.tail, partial.version), (report['tail_sha256'], report['version_after']))
        self.assertEqual(ingest.backfill(self.layout), [])

    def test_no_files_uses_the_existing_session_scan_and_missing_proof_preserves_literature(self):
        native = returned('literature', 'get_paper', 'paper', {'status': 'ok', 'data': {
            'paper': {'paper_id': 'a:1', 'provider': 'a'}}, 'invocation_id': 'a' * 32})
        native['arguments'] = {}
        with mock.patch('rsi.memory_proposals.timeline', side_effect=AssertionError('unnecessary replay')):
            ordinary = self.record([native], {})
            missing = self.record([self.write(1)], {})
        self.assertEqual(ordinary['reasons'], {})
        self.assertEqual(missing['reasons'], {'native submission lacks a sealed proposal attachment': 1})
        with mock.patch('rsi.memory_proposals.timeline', side_effect=FileNotFoundError('session missing')):
            failed = self.record([native, self.read, self.write(1)], {1: self.args})
        self.assertEqual(failed['deliveries'], {'paper': 1})
        self.assertIn('session missing', failed['rejected'][0]['reason'])

    def submit_subquestion(self):
        args = dict(kind='question', id='subquestion_pairing', question_id='experiment', expected_revision=0,
                    text='Do both arms use the same pool?')
        report = self.record([self.write(1, args)], {1: args})
        self.assertEqual(report['objects'], {'question': 2})
        root, sub = ledger.load(self.layout).objects[-2:]
        self.assertEqual((root['authority'], sub['authority']), ('task_contract', 'model_interpretation'))
        self.assertEqual(sub['question_ids'], [root['id']])
        return sub

    def test_all_interpretations_attach_to_an_exact_previously_read_subquestion(self):
        sub = self.submit_subquestion()
        read = returned('memory', 'recall_objects', 'subquestion', {'results': [{'object': sub}]})
        content = dict(decision={'decision': 'Compare arms within pool.', 'scope': ''},
                       investigation={'description': 'Checked how arms were assigned.', 'scope': 'Shared pools'},
                       method_experience={'change': 'Paired comparisons', 'rationale': 'Pools vary.',
                                          'outcome': 'unchanged', 'scope': 'Shared pools'},
                       judgment={'statement': 'Pairing is appropriate.', 'scope': 'Shared pools'})
        files = {}
        for i, ((kind, fields), relation) in enumerate(zip(content.items(), ('derived_from', 'challenges', 'background', 'supports')), 1):
            ref = dict(id=sub['id'], revision=sub['revision'], relation=relation)
            files[i] = dict(kind=kind, id=kind + '_a', expected_revision=0, question_id=sub['id'], object_refs=[ref], **fields)
        result = self.record([read, *(self.write(i, args) for i, args in files.items())], files)
        self.assertEqual(result['objects'], dict.fromkeys(content, 1))
        for obj, args in zip(ledger.load(self.layout).objects[-4:], files.values()):
            self.assertEqual(obj['question_ids'], [sub['id']])
            self.assertEqual(obj['object_refs'], [dict(args['object_refs'][0], thread='lead', call_id='native-subquestion')])
            self.assertEqual(obj['content'], dict(type=args['kind'], support_status='pending_review', **content[args['kind']]))

    def test_model_cannot_claim_contract_ownership_or_revise_another_tasks_question(self):
        sub = self.submit_subquestion()
        root = sub['question_ids'][0]
        base = dict(kind='question', id=sub['id'], expected_revision=1, question_id='other', text='New proposal')
        files = {1: base, 2: dict(base, id=root), 3: dict(base, id='subquestion_other', expected_revision=0,
                                                         authority='task_contract')}
        denied = self.record([self.write(i, args) for i, args in files.items()], files, task='other')
        self.assertEqual(denied['objects'], {})
        self.assertEqual(len(denied['reasons']), 3)
        valid = dict(base, question_id='experiment')
        result = self.record([self.write(1, valid)], {1: valid})
        self.assertEqual(result['objects'], {'question': 1})
        self.assertEqual(ledger.load(self.layout).objects[-1]['supersedes'], 1)

    def test_canonical_author_can_update_legacy_judgment_and_its_contract_goal(self):
        folder = self.layout.tasks / 'experiment'
        folder.mkdir(parents=True)
        legacy = self.layout.tasks / 'Experiment'
        if not legacy.exists():  # Model the historical spelling on a case-sensitive test volume.
            legacy.symlink_to(folder, target_is_directory=True)
        old = dict(self.args, question_id='Experiment')
        self.record([self.read, self.write(1, old)], {1: old}, task='Experiment')
        original_root, original = ledger.load(self.layout).objects[-2:]
        self.assertEqual(original_root['id'], 'question_' + ledger.digest('Experiment'))
        edit = dict(self.args, expected_revision=1)
        revised = self.record([self.read, self.write(2, edit)], {2: edit}, goal='Updated goal')
        self.assertEqual(revised['objects'], {'question': 1, 'judgment': 1})
        earlier_root, earlier, new_root, new = ledger.load(self.layout).objects[-4:]
        self.assertEqual((earlier_root, earlier), (original_root, original))
        self.assertEqual((new_root['id'], new_root['revision'], new_root['content']['text']),
                         (original_root['id'], 2, 'Updated goal'))
        self.assertEqual(new['origin']['task'], 'experiment')
        self.assertEqual(new['supersedes'], original['revision'])
        self.assertEqual(new['question_ids'], [original_root['id']])
        self.assertEqual(new['object_refs'][0]['revision'], 2)

    def test_distinct_case_sensitive_task_cannot_revise_legacy_author(self):
        old = dict(self.args, question_id='Experiment')
        self.record([self.read, self.write(1, old)], {1: old}, task='Experiment')
        edit = dict(self.args, expected_revision=1)
        # Different parent paths let the test use genuinely distinct directories on every volume.
        first = self.layout.tasks / 'experiment'
        other = self.layout.root / 'other' / 'Experiment'
        first.mkdir(parents=True)
        other.mkdir(parents=True)
        from rsi.tasks import same_task_directory
        with mock.patch('rsi.memory_proposals.same_task_directory', return_value=same_task_directory(first, other)):
            denied = self.record([self.read, self.write(2, edit)], {2: edit})
        self.assertEqual(denied['objects'], {})
        self.assertIn('judgment belongs to a different task or authority', denied['reasons'])

    def test_object_reads_must_match_the_snapshot_and_precede_the_proposal(self):
        sub = self.submit_subquestion()
        ref = dict(id=sub['id'], revision=sub['revision'], relation='background')
        decision = dict(kind='decision', id='decision_match', expected_revision=0, decision='Pair by pool.', scope='',
                        question_id='experiment', object_refs=[ref])
        read = returned('memory', 'get_evidence', 'object', {'results': [{'object': sub}]})
        forged = returned('memory', 'get_evidence', 'forged', {'results': [{'object': dict(sub, authority='task_contract')}]})
        nested = returned('memory', 'get_evidence', 'nested', {'results': [{'questions': [sub]}]})
        write = self.write(1, decision)
        for events, opts in (([forged, nested, write], {}), ([write, read], {}), ([read, write], {'corrupt_snapshot': True}),
                             ([write], {'child': [read], 'clocks': {'native-object': {'completed_at_ms': 10},
                                                                  'native-1': {'started_at_ms': 10, 'completed_at_ms': 20}}})):
            with self.subTest(options=opts):
                rejected = self.record(events, {1: decision}, **opts)
                self.assertEqual(rejected['objects'], {})
                self.assertEqual(len(rejected['rejected']), 1)
        accepted = self.record([write], {1: decision}, child=[read],
                               clocks={'native-object': {'completed_at_ms': 10},
                                       'native-1': {'started_at_ms': 11, 'completed_at_ms': 20}})
        self.assertEqual(accepted['objects'], {'decision': 1})
        self.assertEqual(ledger.load(self.layout).objects[-1]['object_refs'][-1]['thread'], 'child')

    def test_rejects_unread_or_self_references_and_unattributed_outcomes(self):
        sub = self.submit_subquestion()
        read = returned('memory', 'get_evidence', 'sub', {'results': [{'object': sub}]})
        ref = dict(id=sub['id'], revision=1, relation='challenges')
        method = dict(kind='method_experience', id='method_experience_m', change='Compare pairs.', rationale='Balance.',
                      outcome='failed', scope='', question_id='experiment', expected_revision=0)
        files = {1: method, 2: dict(method, object_refs=[dict(ref, revision=2)]),
                 3: dict(method, object_refs=[dict(ref, relation='verified')]), 4: dict(method, outcome='verified', object_refs=[ref]),
                 5: dict(method, question_id=sub['id'], source_refs=[self.ref]),
                 6: dict(kind='question', id=sub['id'], expected_revision=1, question_id=sub['id'], object_refs=[ref], text='Self'),
                 7: dict(method, object_refs=[ref], freshness='verified'),
                 8: dict(method, source_refs=[self.ref])}
        report = self.record([self.read, read, *(self.write(i, a) for i, a in files.items())], files)
        self.assertEqual(report['objects'], {'method_experience': 1})
        self.assertEqual(len(report['rejected']), 7)

    def test_exact_subquestion_pin_survives_later_revision_and_same_named_task(self):
        old = self.submit_subquestion()
        edit = dict(kind='question', id=old['id'], expected_revision=1, question_id='experiment', text='Which pool?')
        self.record([self.write(1, edit)], {1: edit})
        new = ledger.load(self.layout).objects[-1]
        read = returned('memory', 'get_evidence', 'q1', {'results': [{'object': old}]})
        reread = returned('memory', 'get_evidence', 'q2', {'results': [{'object': new}]})
        pin = dict(id=old['id'], revision=1, relation='background')
        args = dict(kind='decision', id='decision_old_q', expected_revision=0, question_id=old['id'],
                    decision='Evaluate the earlier question.', scope='', object_refs=[pin])
        ambiguous = dict(args, object_refs=[pin, dict(pin, revision=2)])
        accepted = self.record([read, reread, self.write(1, ambiguous), self.write(2, args)],
                               {1: ambiguous, 2: args}, task=old['id'])
        self.assertEqual(accepted['objects'], {'decision': 1})
        self.assertIn('subquestion requires a single, exact question revision', accepted['reasons'])
        decision = ledger.load(self.layout).objects[-1]
        self.assertEqual(decision['question_ids'], [old['id']])
        self.assertEqual(decision['object_refs'], [dict(pin, thread='lead', call_id='native-q1')])

    def test_quotation_disambiguates_two_parser_versions_in_the_same_literature_delivery(self):
        row = self.passage()
        text = row['text'] + ' Different reading.'
        newer = dict(row, parser_version='2', text=text, char_range=[0, len(text)], byte_range=[0, len(text)])
        found, ref = self.literature(row)
        native_result = json.loads(found['result']['content'][0]['text'])
        native_result['data']['passages'].append(newer)
        found['result']['content'][0]['text'] = json.dumps(native_result)
        ambiguous = dict(self.args, source_refs=[ref])
        exact = dict(self.args, source_refs=[dict(ref, quote='Different reading.')])
        report = self.record([found, self.write(1, ambiguous), self.write(2, exact)], {1: ambiguous, 2: exact})
        self.assertEqual(report['objects'], {'passage': 2, 'judgment': 1, 'question': 1})
        self.assertEqual(report['reasons'], {'ambiguous current passage revision': 1})
        objects = ledger.load(self.layout).objects
        pin = objects[-1]['source_refs'][0]
        matched, = [o for o in objects if o['id'] == pin['id'] and o['revision'] == pin['revision']]
        self.assertEqual(matched['content']['parser_version'], '2')
