"""Scientific Memory's ingestion contract, report recovery, and native Codex completion path."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import memory_ingest as ingest, memory_store as ledger
from rsi.labrun import run_task
from rsi.memory_receipts import original_problem, service_source
from rsi.memory_snapshot import place_snapshot
from rsi.mockmodel import run_js, say
from rsi.paths import Layout
from rsi.records import Records
from rsi.tomlout import toml_value
from rsi.versions import VersionStore
from seed.work.tools.literature.rsi_literature.providers import ProviderClient
from seed.work.tools.memory.query import Snapshot
from tests.support import Root, needs_codex
from tests.test_literature_server import ARTICLE, DRIVER, SEED


class Ledger(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(Path(self.enterContext(tempfile.TemporaryDirectory())).resolve())
        self.records = Records(self.layout.records)
        self.original = hashlib.sha256(ARTICLE).hexdigest()
        self.file = self.layout.library / 'objects' / 'sha256' / self.original
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(ARTICLE)
        self.address = {'source_sha256': self.original, 'document_id': 'doc_' + 'a' * 64, 'document_version': 'b' * 64}
        self.row = {**self.address, 'locator': 'block:000001', 'content_scope': 'full_text', 'text': 'café',
                    'char_range': [2, 6], 'byte_range': [2, 7], 'section': 'section:000001', 'section_path': ['Results'],
                    'block_complete': True, 'kind': 'paragraph', 'parser_version': 'parser-1', 'source_metadata': {}}

    @staticmethod
    def event(index, tool, data, args=None, status='ok', **fields):
        failure = status in ('failed', 'unavailable')
        result = dict(data=data, status=status, invocation_id=f'{index:032x}', provenance={'provider': 'archive'},
                      coverage={}, errors=[]) | fields
        item = {'type': 'McpToolCall', 'server': 'literature', 'status': 'failed' if failure else 'completed',
                'tool': tool, 'arguments': args or {}, 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}],
                                                                 'isError': failure}}
        return {'type': 'event_msg', 'timestamp': f'2026-01-01T01:00:{index:02d}Z',
                'payload': {'type': 'item_completed', 'thread_id': 'lead', 'item': item}}

    def call(self, events, *, status='finished', purpose='task', task='T1', memory=None, child=None, **end):
        run = self.records.new_id('R')
        fields = {'purpose': purpose, 'task': task, 'version_before': 'V0001', 'thread_id': 'lead',
                  'subagent_threads': ['child'] if child is not None else []}
        if memory is not None:
            fields['memory'] = memory
        self.records.start_call(run, fields)
        folder = self.records.call_dir(run) / 'sessions'
        folder.mkdir()
        for tid, rows in [('lead', events), ('child', child)]:
            if rows is not None:
                (folder / f'rollout-{tid}.jsonl').write_bytes(b''.join(ledger.encode(v) + b'\n' for v in rows))
        self.records.finish_call(run, {'status': status, **end})
        return run

    def snapshot(self, condition='live', task=None):
        work = self.layout.root / condition
        work.mkdir()
        place_snapshot(self.layout, work, condition, task)
        return Snapshot(work / 'memory')

    def test_out_of_order_backfill_preserves_pins_and_uses_latest_visible_paper_delivery(self):
        old = {'paper_id': 'archive:42', 'provider': 'archive', 'doi': '10.1/old', 'title': 'Old title'}
        corrected = {**old, 'doi': '10.1/corrected', 'title': 'Corrected title'}
        # The source returned both observations at the same timestamp in separate, serial runs.
        first = self.call([self.event(1, 'get_paper', {'paper': old})], task='original')
        second = self.call([self.event(1, 'get_paper', {'paper': corrected})], task='correction')
        self.assertEqual([r['run'] for r in ingest.backfill(self.layout, [second])], [second])
        self.assertEqual([r['run'] for r in ingest.backfill(self.layout)], [first])

        ident = 'paper_' + ledger.digest('archive:42')
        snapshot = self.snapshot()
        latest, = snapshot.get_evidence(ident, None)
        self.assertEqual((latest['object']['revision'], latest['object']['content']['title'], latest['freshness']),
                         (1, corrected['title'], 'current'))
        self.assertEqual(list(snapshot.lookup(corrected['doi']))[0]['object'], latest['object'])
        self.assertEqual(list(snapshot.lookup(old['doi'])), [])
        historical, = snapshot.get_evidence(ident, 2)
        self.assertEqual(historical['object']['content']['doi'], old['doi'])
        self.assertEqual(historical['freshness_reasons'], ['newer_visible_revision'])
        self.assertIsNone(historical['object']['supersedes'])

        trial = self.snapshot('trial', 'correction')
        visible, = trial.get_evidence(ident, None)
        self.assertEqual((visible['object']['revision'], visible['freshness']), (2, 'current'))
        self.assertEqual(list(trial.lookup(old['doi']))[0]['object'], visible['object'])
        self.assertEqual(trial.get_evidence(ident, 1), [])
        self.assertEqual(list(trial.lookup(corrected['doi'])), [])

        third = self.call([self.event(1, 'get_paper', {'paper': corrected})])
        report, = ingest.backfill(self.layout, [third])
        self.assertEqual(report['objects'], {})
        self.assertEqual(ledger.load(self.layout).deliveries[-1]['revision'], 1)

    def test_source_with_no_fulltext_locations_is_retrievable_as_an_acquisition_attempt(self):
        ref = '10.1234/example'
        paper = {'source': 'MED', 'id': '123', 'doi': ref, 'title': 'A paper'}
        raw = json.dumps({'hitCount': 1, 'resultList': {'result': [paper]}}).encode()
        client = ProviderClient(mock.Mock(return_value={'status': 200, 'body': raw, 'headers': {}}), env={})
        reply = client.fulltext_candidates('europe_pmc', ref)
        self.call([self.event(1, 'acquire_fulltext', args={'paper_ref': ref}, **reply)])

        report, = ingest.backfill(self.layout)
        self.assertEqual(report['reasons'], {})
        attempt, = self.snapshot().lookup(ref)
        content = attempt['object']['content']
        self.assertEqual((attempt['type'], content['provider'], content['status'], content['stage']),
                         ('acquisition', 'europe_pmc', 'unavailable', 'candidate_resolution'))
        self.assertEqual(content['errors'][0]['code'], 'fulltext_location_unavailable')
        self.assertEqual(content['download_attempts'], [])

    def test_revisions_dedup_and_rejections(self):
        paper = {'paper_id': 'a:x', 'provider': 'a', 'doi': '10.1/a', 'title': 'A'}
        first = self.event(1, 'read_document', {'passages': [self.row, {**self.row, 'block_complete': False}]})
        r1 = self.call([self.event(0, 'get_paper', {'paper': paper}), first], child=[first])
        r2 = self.call([self.event(0, 'get_paper', {'paper': {**paper, 'doi': '10.1/b'}}),
                        self.event(1, 'read_document', {'passages': [{**self.row, 'document_id': 'doc_' + 'c' * 64}]}),
                        self.event(2, 'read_document', {'passages': [{**self.row, 'text': 'cafè'}]}),
                        self.event(3, 'read_document', {'passages': [{**self.row, 'source_sha256': 'd' * 64}]}),
                        self.event(4, 'read_document', {'passages': [{**self.row, 'char_range': 'bad'}]})], status='timed_out')
        reports = ingest.backfill(self.layout)
        self.assertEqual([r['run'] for r in reports], [r1, r2])
        self.assertEqual(reports[0]['reasons'], {'partial block': 1})
        self.assertEqual(set(reports[1]['reasons']), {'original missing', 'invalid char_range',
                                                     'conflicting text or coordinates at the same passage address'})
        state = ledger.load(self.layout)
        self.assertEqual([o['revision'] for o in state.objects if o['content']['type'] == 'paper'], [1, 2])
        self.assertEqual({d['revision'] for d in state.deliveries if d['tool'] == 'get_paper'}, {1, 2})
        ids = {d['id'] for d in state.deliveries if d['tool'] == 'read_document'}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(ledger.show(self.layout, ids.pop())['deliveries']), 2)
        self.assertEqual(ingest.verify(self.layout)['originals'], 1)
        self.assertEqual(ingest.backfill(self.layout), [])

    def test_bad_archive_is_isolated_until_the_owner_retries_it(self):
        bad = self.call([])
        healthy = self.call([self.event(1, 'read_document', {'passages': [self.row]})])
        archived = self.records.call_dir(bad) / 'sessions/rollout-lead.jsonl'
        original = archived.read_bytes()
        archived.write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, bad + '.*checksum mismatch'):
            ingest.backfill(self.layout)
        self.assertEqual(ingest.status(self.layout)['blocked_runs'], [bad])
        self.assertEqual(ledger.load(self.layout).reports.keys(), {healthy})
        archived.write_bytes(original)
        self.assertEqual(ingest.backfill(self.layout), [])
        report, = ingest.backfill(self.layout, [bad])
        self.assertEqual(report['run'], bad)
        self.assertEqual(ingest.status(self.layout)['blocked_runs'], [])
        self.assertEqual(ingest.verify(self.layout)['runs'], 2)

    def test_only_string_invocation_ids_are_registered(self):
        self.call([self.event(1, 'read_document', {'passages': [self.row]}, invocation_id=10**31),
                   self.event(1, 'read_document', {'passages': [self.row]})])
        report, = ingest.backfill(self.layout)
        self.assertEqual(report['reasons'], {'invalid invocation_id': 1})
        self.assertEqual(report['delivery_count'], 1)

    def test_legacy_collection_uses_provable_stages_and_keeps_the_committed_batch(self):
        evidence = self.event(1, 'read_document', {'passages': [self.row]})
        interrupted = ['Interrupted while collecting the results (KeyboardInterrupt); the remaining steps were not done']
        ambiguous = self.call([evidence], memory_collection_complete=False, problems=interrupted)
        usable = self.call([evidence], memory_collection_complete=False,
                           problems=['Memory proposals could not be collected: missing spool'])
        late = self.call([evidence], memory_collection_complete=False, problems=interrupted, check={'passed': False})
        report, = ingest.backfill(self.layout, [usable])
        self.assertEqual((report['deliveries'], report['proposal_collection']), ({'passage': 1}, 'incomplete'))
        self.assertEqual([r['run'] for r in ingest.backfill(self.layout)], [late])
        info = ingest.status(self.layout)
        self.assertEqual(info['blocked_runs'], [ambiguous])
        self.assertIn('legacy record', info['blocked_reasons'][ambiguous])
        with self.assertRaisesRegex(ValueError, ambiguous + '.*session archive'):
            ingest.backfill(self.layout, [ambiguous])
        self.assertEqual(ledger.load(self.layout).reports.keys(), {usable, late})
        # A missing independent report is recreated from committed bytes without reinterpreting the old call.
        (self.layout.records / 'memory' / f'{usable}.json').unlink()
        with mock.patch.object(ingest, 'eligible', side_effect=AssertionError('already committed')):
            self.assertEqual(ingest.backfill(self.layout, [usable]), [report])

    def test_malformed_session_manifest_does_not_hide_healthy_pending_records(self):
        bad, good = self.call([]), self.call([])
        first, second = self.records.calls()
        with mock.patch.object(ingest, '_calls', return_value=[{**first, 'files': 'bad manifest'}, second]):
            with self.assertRaisesRegex(ValueError, 'Invalid session manifest'):
                ingest.backfill(self.layout)
            self.assertEqual(ingest.status(self.layout)['blocked_runs'], [bad])
        self.assertEqual(ledger.load(self.layout).reports.keys(), {good})
        self.assertFalse(ingest.eligible({**second, 'subagent_threads': ['missing-child']}))
        self.assertTrue(ingest.eligible({**second, 'problems': [
            'Proposal collection exceeded its 64-file limit',
            'Proposal sample.json could not be collected: expected a bounded single-link regular file']}))
        self.assertTrue(ingest.eligible({**second, 'problems': [
            'Proposal sample.json could not be collected: [Errno 5] Input/output error']}))

    def test_distinguish_source_failures_from_local_failures_and_unknown_stage(self):
        def failed(i, code, attempts):
            return self.event(i, 'acquire_fulltext', {}, {'paper_ref': '10.1/a'}, status='unavailable',
                              errors=[{'code': code, 'detail': 'test'}], provenance={'provider': 'archive', 'download_attempts': attempts})
        reports = self.call([failed(1, 'fulltext_unavailable', [{'host': 'a.example', 'http_status': 404}]),
                             failed(2, 'source_storage_error', [{'host': 'a.example', 'http_status': 200}]),
                             failed(3, 'runtime_unavailable', []), failed(4, 'fulltext_unavailable', []),
                             failed(5, 'invalid_arguments', []), failed(6, 'fulltext_format_unavailable', [])], status='failed')
        rep, = ingest.backfill(self.layout, [reports])
        self.assertEqual(rep['reasons'], {'local archive failure': 1, 'local runtime unavailable': 1,
                                          'unknown failure stage': 1, 'invalid arguments': 1})
        objects = ledger.load(self.layout).objects
        self.assertEqual([(o['content']['status'], o['content']['stage']) for o in objects],
                         [('unavailable', 'source_http'), ('unavailable', 'candidate_resolution')])

    def test_object_and_middle_event_corruption_and_truncated_commit(self):
        self.call([self.event(1, 'read_document', {'passages': [self.row]})])
        ingest.backfill(self.layout)
        batch, = (self.layout.memory / 'batches').glob('*.jsonl')
        original = batch.read_bytes()
        lines = original.splitlines(keepends=True)
        for index, key, value, diagnostic in ((0, 'authority', 'changed', 'object checksum'),
                                               (1, 'task', 'different-task', 'chain disagrees')):
            with self.subTest(event=index):
                changed = json.loads(lines[index])
                changed['value'][key] = value
                batch.write_bytes(b''.join(lines[:index] + [ledger.encode(changed) + b'\n'] + lines[index + 1:]))
                with self.assertRaisesRegex(ValueError, diagnostic):
                    ingest.verify(self.layout)
        batch.write_bytes(original[:-7])
        with self.assertRaisesRegex(ValueError, 'incomplete memory segment'):
            ingest.verify(self.layout)

    def test_retry_before_publication_matches_uninterrupted_batch_and_report(self):
        self.call([self.event(1, 'read_document', {'passages': [self.row]})])
        reference = Layout(self.layout.root / 'reference')
        shutil.copytree(self.layout.records, reference.records)
        shutil.copytree(self.layout.library, reference.library)
        wanted, = ingest.backfill(reference)
        # The segment has been written to a private staging file when os.link publishes it.
        with mock.patch('rsi.records.os.link', side_effect=InterruptedError('before publication')), self.assertRaises(InterruptedError):
            ingest.backfill(self.layout)
        self.assertEqual(ledger.load(self.layout).version, 0)
        self.assertFalse((self.layout.records / 'memory').exists())
        report, = ingest.backfill(self.layout)
        self.assertEqual(report, wanted)
        actual_file, = (self.layout.memory / 'batches').glob('*.jsonl')
        self.assertEqual(actual_file.read_bytes(), (reference.memory / 'batches' / actual_file.name).read_bytes())

    def test_registration_is_recoverable_and_call_files_are_immutable(self):
        run = self.call([self.event(1, 'read_document', {'passages': [self.row]})])
        folder = self.records.call_dir(run)
        before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        with mock.patch.object(Records, 'write_json', side_effect=OSError('no space')), self.assertRaises(OSError):
            ingest.backfill(self.layout)
        with self.assertRaisesRegex(ValueError, 'Missing registration report'):
            ingest.verify(self.layout)
        batch, = (self.layout.memory / 'batches').glob('*.jsonl')
        committed = batch.read_bytes()
        report, = ingest.backfill(self.layout)
        self.assertEqual(batch.read_bytes(), committed)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(ingest.backfill(self.layout, [run]), [report])
        rows = committed.splitlines(keepends=True)
        edited = json.loads(rows[-1]); edited['value']['delivery_count'] += 1
        batch.write_bytes(b''.join(rows[:-1]) + ledger.encode(edited) + b'\n')
        with self.assertRaisesRegex(ValueError, 'report disagrees'):
            ingest.verify(self.layout)
        batch.write_bytes(b''.join(rows[:-1]))
        with self.assertRaisesRegex(ValueError, 'Missing commit'):
            ingest.verify(self.layout)
        batch.unlink()
        with self.assertRaisesRegex(ValueError, 'no matching memory batch'):
            ingest.backfill(self.layout)

    def test_ineligible_runs_and_conflicting_or_malformed_receipts(self):
        event = self.event(1, 'read_document', {'passages': [self.row]})
        self.call([event], purpose='trial')
        self.call([event], memory={'condition': 'none'})
        self.call([event], status='interrupted')
        malformed = self.event(2, 'get_paper', {})
        malformed['payload']['item']['result']['content'][0]['text'] = 'broken'
        self.call([event, self.event(1, 'get_paper', {}), malformed])
        report, = ingest.backfill(self.layout)
        self.assertEqual(report['delivery_count'], 0)
        self.assertEqual(report['reasons']['conflicting invocation_id'], 1)
        self.assertEqual(len(report['rejected']), 2)

    def test_legacy_inherited_copy_cannot_claim_the_parent_call(self):
        event = self.event(1, 'read_document', {'passages': [self.row]})
        del event['payload']['thread_id']
        self.call([event], child=[event])  # the child's filename sorts before the lead's
        ingest.backfill(self.layout)
        delivery, = ledger.load(self.layout).deliveries
        self.assertEqual((delivery['thread'], delivery['role']), ('lead', 'lead'))

    def test_original_rejects_hardlinks_mismatches_and_changed_directory_entries(self):
        link = self.file.parent / 'other'
        os.link(self.file, link)
        self.assertIn('single-link', original_problem(self.layout, self.original))
        link.unlink()
        self.file.write_bytes(b'different')
        self.assertIn('mismatch', original_problem(self.layout, self.original))
        base = self.file.parent
        moved = base.with_name('old'); base.rename(moved); base.symlink_to(moved)
        self.assertIn('opened', original_problem(self.layout, self.original))

    def test_service_provenance_does_not_echo_custom_command_or_token(self):
        custom = {'command': ['-c', 'mcp_servers={literature={url="https://x.example?q=test-secret"}}']}
        source = service_source(self.layout, custom)
        self.assertEqual(source['origin'], 'custom')
        self.assertNotIn('test-secret', json.dumps(source))

    def test_provenance_uses_actual_entry_and_records_code_version_transitions(self):
        store = VersionStore(self.layout.versions)
        store.init()
        entry = self.layout.work / 'tools' / 'literature' / 'server.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('installed\n')
        installed = store.commit(self.layout.work, 'owner', 'Install the Literature tool server')
        entry.write_text('lab version\n')
        lab = store.commit(self.layout.work, 'lab R0001', 'Edited the server')
        entry.write_text('meta version\n')
        meta = store.commit(self.layout.work, 'meta M0001', 'Edited again')

        def launch(before, after=None, script=entry):
            server = {'command': '/usr/bin/codex', 'args': ['sandbox', '--', '/usr/bin/env', 'python3', '-B', str(script)]}
            call = {'version_before': before, 'version_after': after, 'command': ['-c', 'mcp_servers=' + toml_value({'literature': server})]}
            return service_source(self.layout, call)

        fixed = launch(installed)
        self.assertEqual((fixed['origin'], fixed['version'], fixed['changed_during_run']), ('installed', installed, False))
        changed = launch(installed, after=lab)
        self.assertEqual((changed['origin'], changed['version'], changed['changed_during_run']), ('installed', installed, True))
        for version, origin in [(lab, 'lab'), (meta, 'meta')]:
            with self.subTest(version=version):
                src = launch(version)
                self.assertEqual((src['origin'], src['version'], src['changed_during_run']), (origin, version, False))
        self.assertEqual(launch(installed, script=entry.with_name('alternative.py'))['origin'], 'custom')
        self.assertEqual(service_source(self.layout, {})['origin'], 'unknown')

    def test_invalid_receipt_paths_and_required_fields_reject_only_the_bad_rows(self):
        missing_locator = dict(self.row)
        del missing_locator['locator']
        impostor = {**self.row, 'source_sha256': '../outside'}
        self.call([self.event(1, 'read_document', {'passages': [missing_locator, impostor, self.row]})])
        rep, = ingest.backfill(self.layout)
        self.assertEqual(rep['reasons'], {'invalid locator': 1, 'invalid document address': 1})
        self.assertEqual(rep['delivery_count'], 1)

    def test_reserved_store_and_report_names_must_be_real_directories_and_files(self):
        outside = self.layout.root / 'outside'
        outside.mkdir()
        self.layout.memory.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'plain directory'):
            ledger.init_store(self.layout)
        self.assertEqual(list(outside.iterdir()), [])
        self.layout.memory.unlink()
        self.layout.memory.mkdir()
        batches = self.layout.memory / 'batches'
        batches.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'plain directory'):
            ingest.backfill(self.layout)
        batches.unlink()
        r = self.call([])
        ingest.backfill(self.layout)
        batch, = batches.glob('*.jsonl')
        moved = outside / 'batch'
        batch.rename(moved)
        batch.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            ingest.verify(self.layout)
        batch.unlink()
        moved.rename(batch)
        report = self.layout.records / 'memory' / f'{r}.json'
        report.rename(outside / 'report')
        report.symlink_to(outside / 'report')
        with self.assertRaisesRegex(ValueError, 'regular file'):
            ingest.backfill(self.layout)


@needs_codex
class NativeLedger(unittest.TestCase):
    def test_codex_returns_injected_paper_original_and_passages(self):
        with Root() as root:
            root.add_task('T1')
            tools = root.layout.work / 'tools/literature'
            shutil.copytree(SEED, tools, ignore=shutil.ignore_patterns('__pycache__'))
            (tools / 'fixture_server.py').write_text(DRIVER)
            (root.layout.work / 'article.xml').write_bytes(ARTICLE)
            config = root.layout.work / 'codex/settings.toml'
            config.parent.mkdir()
            config.write_text('[tool_servers.literature]\ncommand="python3"\nargs=["-B", "tools/literature/fixture_server.py"]\n')
            root.model.push(run_js('const p=await tools.mcp__literature__get_paper({paper_ref:"PMC456",provider:"europe_pmc"}); '
                                   'const a=await tools.mcp__literature__acquire_fulltext({paper_ref:"PMC456",provider:"europe_pmc"}); '
                                   'const d=JSON.parse(a.content[0].text).data.document_id; '
                                   'text(await tools.mcp__literature__read_document({document_id:d}));'), say('done'))
            with mock.patch.object(ledger, 'publish', side_effect=OSError('injected registration failure')):
                call = run_task(root.layout, 'T1')
            self.assertEqual(call['status'], 'finished', call['problems'])
            saved = root.records.call_dir(call['id'])
            original_files = {p: p.read_bytes() for p in saved.rglob('*') if p.is_file()}
            report_file = root.layout.records / 'memory' / f'{call["id"]}.json'
            self.assertFalse(report_file.exists())
            root.model.push(say('done'))
            following = run_task(root.layout, 'T1')
            report = json.loads(report_file.read_bytes())
            self.assertEqual(following['memory']['version'], report['version_after'])
            self.assertGreater(following['memory']['counts']['passage'], 0)
            self.assertEqual({p: p.read_bytes() for p in original_files}, original_files)
            self.assertEqual(report['deliveries']['paper'], 1, report)
            self.assertEqual(report['deliveries']['acquisition'], 1, report)
            self.assertGreaterEqual(report['deliveries']['passage'], 1, report)
            self.assertEqual(report['service']['origin'], 'custom')
            self.assertEqual(ingest.verify(root.layout)['originals'], 1)


if __name__ == '__main__':
    unittest.main()
