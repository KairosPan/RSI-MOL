"""Read new evidence on an empty ledger, then recover its evidence and judgment from the same sealed run."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from rsi import memory_ingest, memory_store
from rsi.labrun import run_task, run_trial
from rsi.mockmodel import run_js, say
from rsi.paths import Layout
from tests.support import Root, needs_codex
from tests.test_judgment_lifecycle import RECALL, SEED, SOURCE


READ_AND_PROPOSE = """
const acquired = await tools.mcp__literature__acquire_fulltext({source_id:'paired'});
const doc = JSON.parse(acquired.content[0].text).data.document_id;
const read = await tools.mcp__literature__read_document({document_id:doc});
const receipt = JSON.parse(read.content[0].text);
const p = receipt.data.passages.find(p => p.block_complete && p.text.includes('difference within each pair'));
const r = await tools.mcp__memory__propose_memory({kind:'judgment',id:'judgment_first_read',
    expected_revision:0,question_id:'author',statement:'Estimate the paired contrast.',scope:'One pair per pool.',
    source_refs:[{delivery_source:'literature',invocation_id:receipt.invocation_id,document_id:p.document_id,
        document_version:p.document_version,source_sha256:p.source_sha256,locator:p.locator,
        relation:'supports',quote:'Score the difference within each pair.'}]});
text(r.content[0].text);
"""


def stop_publishing_in(folder):
    """Interrupt the final publication link after the complete private file has been written."""
    link = os.link

    def publish(source, target, **kwargs):
        if Path(target).parent == folder:
            raise InterruptedError('injected publication interruption')
        return link(source, target, **kwargs)

    return mock.patch('rsi.records.os.link', side_effect=publish)


def clone_inputs(layout, run, path):
    replica = Layout(path.resolve())
    shutil.copytree(layout.records / 'runs' / run, replica.records / 'runs' / run)
    shutil.copytree(layout.library, replica.library)
    shutil.copytree(layout.versions, replica.versions)
    return replica


@needs_codex
class CurrentJudgmentRecovery(unittest.TestCase):
    def test_first_read_and_judgment_publish_and_recover_together(self):
        with Root() as root, tempfile.TemporaryDirectory() as replicas:
            for task in ('author', 'reader'):
                root.add_task(task)
            layout = root.layout
            for folder in ('tools', 'codex'):
                shutil.copytree(SEED / folder, layout.work / folder, ignore=shutil.ignore_patterns('__pycache__'))
            (layout.work / 'paired.xml').write_bytes(SOURCE)
            (layout.work / 'tools/literature/sources.json').write_text(json.dumps({'paired': {
                'path': 'paired.xml', 'media_type': 'application/xml', 'sha256': hashlib.sha256(SOURCE).hexdigest()}}))

            root.model.push(run_js(READ_AND_PROPOSE), say('done'))
            with stop_publishing_in(layout.memory / 'batches'):
                author = run_task(layout, 'author')
            run = author['id']
            self.assertEqual((author['status'], author['memory']['version']), ('finished', 0))
            self.assertIn('injected publication interruption', ' '.join(author['post_call_problems']))
            self.assertIn('"pending"', ' '.join(root.model.tool_outputs()))
            call_folder = root.records.call_dir(run)
            call_files = {p: p.read_bytes() for p in call_folder.rglob('*') if p.is_file()}
            self.assertEqual(memory_store.load(layout).version, 0)
            self.assertEqual(list((layout.memory / 'batches').glob('*.jsonl')), [])
            self.assertFalse((layout.records / 'memory' / (run + '.json')).exists())
            self.assertEqual(json.loads((layout.work / 'memory/status.json').read_text())['counts'], {})

            # Both comparison installations replay exactly this call, including its native timestamps and IDs.
            normal = clone_inputs(layout, run, Path(replicas) / 'normal')
            report, = memory_ingest.backfill(normal)
            self.assertEqual(report['rejected'], [])
            self.assertEqual(report['objects'].get('judgment'), 1)
            self.assertEqual(report['objects'].get('question'), 1)
            self.assertGreaterEqual(report['objects']['passage'], 1)
            segment, = (normal.memory / 'batches').glob('*.jsonl')
            batch_bytes = segment.read_bytes()
            report_rel = Path('memory') / (run + '.json')
            report_bytes = (normal.records / report_rel).read_bytes()
            state = memory_store.load(normal)
            judgment, = (o for o in state.objects if o['kind'] == 'judgment')
            ref, = judgment['source_refs']
            passage, = (o for o in state.objects if (o['id'], o['revision']) == (ref['id'], ref['revision']))
            self.assertEqual(passage['origin']['run'], run)
            self.assertIn(ref['quote'], passage['content']['text'])
            self.assertEqual(judgment['content']['support_status'], 'pending_review')
            session, = (call_folder / 'sessions').glob('*.jsonl')
            payloads = (json.loads(line)['payload'] for line in session.read_bytes().splitlines())
            read, = (p['item'] for p in payloads if p.get('type') == 'item_completed' and
                     p.get('item', {}).get('tool') == 'read_document' and p['item']['server'] == 'literature')
            receipt = json.loads(read['result']['content'][0]['text'])
            self.assertEqual((ref['delivery_source'], ref['source_run'], ref['thread'], ref['call_id'], ref['invocation_id']),
                             ('literature', run, author['thread_id'], read['id'], receipt['invocation_id']))

            no_report = clone_inputs(layout, run, Path(replicas) / 'no_report')
            with stop_publishing_in(no_report.records / 'memory'), self.assertRaises(InterruptedError):
                memory_ingest.backfill(no_report)
            pending = memory_store.load(no_report)
            self.assertEqual(pending.missing_reports, {run})
            self.assertEqual(pending.objects, state.objects)
            self.assertEqual(pending.deliveries, state.deliveries)
            pending_segment = no_report.memory / 'batches' / segment.name
            self.assertEqual(pending_segment.read_bytes(), batch_bytes)
            inode = pending_segment.stat().st_ino
            with self.assertRaisesRegex(ValueError, 'Missing registration report'):
                memory_ingest.verify(no_report)
            with mock.patch('rsi.memory_ingest.native_calls', side_effect=AssertionError('already committed')):
                self.assertEqual(memory_ingest.backfill(no_report), [report])
            self.assertEqual(pending_segment.stat().st_ino, inode)
            self.assertEqual(pending_segment.read_bytes(), batch_bytes)
            self.assertEqual((no_report.records / report_rel).read_bytes(), report_bytes)
            self.assertEqual(memory_ingest.backfill(no_report), [])
            self.assertEqual(memory_ingest.verify(no_report), memory_ingest.verify(normal))

            # After the pre-publication interruption, starting the next ordinary task retries before presenting its snapshot.
            root.model.push(run_js(RECALL), say('done'))
            reader = run_task(layout, 'reader')
            self.assertEqual(reader['memory']['version'], state.version)
            self.assertIn('matches=1', ' '.join(root.model.tool_outputs()))
            self.assertEqual((layout.memory / 'batches' / segment.name).read_bytes(), batch_bytes)
            self.assertEqual((layout.records / report_rel).read_bytes(), report_bytes)
            self.assertEqual(len(list((layout.memory / 'batches').glob(f'*-{run}.jsonl'))), 1)
            self.assertEqual({p: p.read_bytes() for p in call_folder.rglob('*') if p.is_file()}, call_files)
            self.assertEqual(memory_ingest.backfill(layout), [])

            root.model.push(run_js(RECALL), say('done'))
            trial = run_trial(layout, commit=root.store.resolve(root.store.head()), round_id='M0001', task_id='author')
            self.assertEqual(trial['memory']['excluded']['judgment'], 1)
            self.assertGreaterEqual(trial['memory']['excluded']['passage'], 1)
            self.assertIn('matches=0', ' '.join(root.model.tool_outputs()))
