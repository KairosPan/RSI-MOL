"""Drive the managed judgment lifecycle through real Codex and a local scripted model."""
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path

from rsi.labrun import run_task, run_trial
from rsi.memory_history import timeline
from rsi.memory_ingest import verify
from rsi.mockmodel import run_js, say
from tests.support import Root, needs_codex


SEED = Path(__file__).resolve().parents[1] / 'seed/work'
SOURCE = b'<article><body><sec><p>Score the difference within each pair.</p></sec></body></article>'
# Enough matching blocks and JSON escapes to exceed Codex's old native receipt budget.
PARAGRAPHS = ''.join(f'<p>Score the difference within each pair. Pool {n}: ' + '"\\中' * 35 + '</p>'
                     for n in range(48))
LARGE_SOURCE = ('<article><body><sec>' + PARAGRAPHS + '</sec></body></article>').encode()

ACQUIRE = """
const result = await tools.mcp__literature__acquire_fulltext({source_id:'paired'});
const doc = JSON.parse(result.content[0].text).data.document_id;
let cursor;
do {
    const read = await tools.mcp__literature__read_document({document_id:doc, max_bytes:65536, ...(cursor && {cursor})});
    const page = JSON.parse(read.content[0].text);
    if (read.isError) throw Error(JSON.stringify(page.errors));
    cursor = page.continuation.next_cursor;
} while (cursor);
"""
SUBMIT = """
const r = await tools.mcp__memory__recall_passages({query:'within each pair'});
const page = JSON.parse(r.content[0].text);
if (!page.omitted) throw Error('Expected the native receipt budget to omit whole matching blocks');
const row = page.results[0];
const d = row.deliveries[0];
const ref = {evidence_id:row.id,revision:row.revision,source_run:d.run,document_id:d.document_id,
    document_version:d.document_version,source_sha256:row.content.source_sha256,locator:row.content.locator,
    relation:'supports',quote:'Score the difference within each pair.'};
const proposal = {kind:'judgment',id:'judgment_paired',expected_revision:0,question_id:'author',
    statement:'Estimate the paired contrast.',scope:'Two measurements from each pool.',source_refs:[ref]};
const response = await tools.mcp__memory__propose_memory(proposal);
text(response.content[0].text);
const current = await tools.mcp__memory__recall_judgments({query:'paired contrast'});
text('before_seal=' + JSON.parse(current.content[0].text).results.length);
"""
RECALL = """
const r = await tools.mcp__memory__recall_judgments({query:'paired contrast'});
const rows = JSON.parse(r.content[0].text).results;
text('matches=' + rows.length + ', submit=' + typeof tools.mcp__memory__propose_memory);
if (rows.length) {
    text(rows[0].object.content);
    text(rows[0].evidence[0].reference);
}
"""


def manual_tools(root):
    """Discover the tools from the exact wrapper configuration written for the manual session."""
    server = tomllib.loads((root.layout.lab_home / 'config.toml').read_text())['mcp_servers']['memory']
    requests = [{'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'id': 2, 'method': 'tools/list'}]
    lines = ''.join(json.dumps(dict(jsonrpc='2.0', **r)) + '\n' for r in requests)
    done = subprocess.run([server['command'], *server['args']], cwd=root.layout.work,
                          env={**os.environ, **server['env']}, input=lines, text=True, capture_output=True,
                          check=True, timeout=15)
    response = json.loads(done.stdout.splitlines()[-1])
    return {t['name'] for t in response['result']['tools']}


@needs_codex
class JudgmentLifecycle(unittest.TestCase):
    def test_propose_then_read_live_and_filter_same_task(self):
        with Root() as root:
            for task in ('archive', 'author', 'reader'):
                root.add_task(task)
            work = root.layout.work
            for folder in ('tools', 'codex'):
                shutil.copytree(SEED / folder, work / folder, ignore=shutil.ignore_patterns('__pycache__'))
            (work / 'paired.xml').write_bytes(LARGE_SOURCE)
            (work / 'tools/literature/sources.json').write_text(json.dumps({'paired': {
                'path': 'paired.xml', 'media_type': 'application/xml', 'sha256': hashlib.sha256(LARGE_SOURCE).hexdigest()}}))
            root.model.push(run_js(ACQUIRE), say('done'))
            archive = run_task(root.layout, 'archive')
            self.assertEqual(archive['status'], 'finished', archive['problems'])
            self.assertIn('Score the difference within each pair.', (work / 'memory/evidence.jsonl').read_text())

            root.model.push(run_js(SUBMIT), say('done'))
            author = run_task(root.layout, 'author')
            self.assertEqual(author['status'], 'finished', author['problems'])
            self.assertIn('before_seal=0', ' '.join(root.model.tool_outputs()))
            report = json.loads((root.layout.records / 'memory' / (author['id'] + '.json')).read_bytes())
            self.assertEqual(report['objects'].get('judgment'), 1, report)
            self.assertEqual(report['objects'].get('question'), 1)
            # Check sealed native completions from the installed Codex, including the
            # historical read used for the proposed judgment, above the previous cap.
            for call, tool in ((archive, 'read_document'), (author, 'recall_passages')):
                sizes = []
                for _, _, _, item, _ in timeline(root.layout, call):
                    if item['tool'] == tool:
                        reply = item['result']
                        self.assertFalse(reply.get('isError'))
                        self.assertIn('data' if tool == 'read_document' else 'results',
                                      json.loads(reply['content'][0]['text']))
                        sizes.append(len(json.dumps(reply, ensure_ascii=False, separators=(',', ':')).encode()))
                self.assertGreater(max(sizes), 32 * 1024, tool)
            sealed, = [name for name in author['files'] if name.startswith('proposals/')]
            proposal_bytes = (root.records.call_dir(author['id']) / sealed).read_bytes()
            self.assertEqual(hashlib.sha256(proposal_bytes).hexdigest(), author['files'][sealed])
            self.assertEqual(json.loads(proposal_bytes)['id'], 'judgment_paired')
            self.assertNotIn('propose_memory', manual_tools(root))

            root.model.push(run_js(RECALL), say('done'))
            reader = run_task(root.layout, 'reader')
            self.assertEqual(reader['memory']['version'], report['version_after'])
            output = ' '.join(root.model.tool_outputs())
            for needle in ('matches=1, submit=function', 'pending_review', 'Two measurements from each pool.',
                           'Score the difference within each pair.', archive['id']):
                self.assertIn(needle, output)

            root.model.push(run_js(RECALL), say('done'))
            trial = run_trial(root.layout, commit=root.store.resolve(root.store.head()), round_id='M0001',
                              task_id='author')
            self.assertEqual(trial['memory']['excluded']['judgment'], 1)
            self.assertIn('matches=0, submit=undefined', ' '.join(root.model.tool_outputs()))

            root.model.push(run_js(RECALL), say('done'))
            empty = run_task(root.layout, 'reader', memory='none')
            self.assertEqual(empty['memory']['counts'], {})
            self.assertIn('matches=0, submit=undefined', ' '.join(root.model.tool_outputs()))
            self.assertEqual(verify(root.layout)['runs'], 3)
