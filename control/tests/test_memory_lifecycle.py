"""Exercise registration, manual restaging and task filtering through actual managed Codex sessions."""
import hashlib
import json
import shutil
import unittest
from pathlib import Path

from rsi.labrun import run_task, run_trial
from rsi.mockmodel import run_js, say
from tests.support import Root, needs_codex


@needs_codex
class Lifecycle(unittest.TestCase):
    def test_a_live_delivery_then_an_excluded_trial_and_a_memory_free_task(self):
        from tests.installed_seed import WORK as seed
        with Root() as root:
            root.add_task('source')
            root.add_task('reader')
            work = root.layout.work
            for name in ('tools', 'codex'):
                shutil.copytree(seed / name, work / name, ignore=shutil.ignore_patterns('__pycache__'))
            raw = b'<article><body><sec><p>Controlled historical witness.</p></sec></body></article>'
            (work / 'example.xml').write_bytes(raw)
            (work / 'tools/literature/sources.json').write_text(json.dumps({
                'example': {'path': 'example.xml', 'media_type': 'application/xml',
                            'sha256': hashlib.sha256(raw).hexdigest()}}))
            root.model.push(run_js(
                "const r=await tools.mcp__literature__acquire_fulltext({source_id:'example'});"
                "const doc=JSON.parse(r.content[0].text).data.document_id;"
                "text((await tools.mcp__literature__read_document({document_id:doc})).content[0].text);"),
                say('done'))
            call = run_task(root.layout, 'source')
            self.assertEqual(call['memory']['version'], 0)
            self.assertEqual(call['status'], 'finished', call)
            report = json.loads((root.layout.records / 'memory' / f'{call["id"]}.json').read_bytes())
            live = json.loads((work / 'memory/status.json').read_bytes())
            self.assertEqual(live['version'], report['version_after'])
            self.assertIn('Controlled historical witness.', (work / 'memory/evidence.jsonl').read_text())
            self.assertGreater(live['counts']['passage'], 0)
            self.assertEqual(root.records.load_call(call['id']), call)

            root.model.push(say('done'))
            trial = run_trial(root.layout, commit=root.store.resolve(root.store.head()), round_id='M0001',
                              task_id='source')
            self.assertEqual(trial['memory']['condition'], 'trial')
            self.assertFalse(trial['memory']['counts'].get('passage'))
            self.assertGreater(trial['memory']['excluded']['passage'], 0)
            root.model.push(say('done'))
            control = run_task(root.layout, 'reader', memory='none')
            self.assertEqual(control['memory']['condition'], 'none')
            self.assertEqual(json.loads((work / 'memory/status.json').read_bytes()), live)
            self.assertEqual([p.stem for p in (root.layout.records / 'memory').glob('*.json')], [call['id']])
