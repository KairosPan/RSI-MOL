"""Submit all interpretation kinds through native MCP and read their linked, filtered revisions."""
import hashlib
import json
import shutil
import unittest

from rsi.labrun import run_task, run_trial
from rsi.memory_store import load, record_owner_action
from rsi.mockmodel import run_js, say
from tests.support import Root, needs_codex
from tests.test_current_judgment_recovery import READ_AND_PROPOSE
from tests.test_judgment_lifecycle import SEED, SOURCE


ASK = """
const sub = await tools.mcp__memory__propose_memory({kind:'question',id:'subquestion_pairs',
    question_id:'author',expected_revision:0,text:'Which pairs share comparable measurements?'});
text(sub.content[0].text);
"""

LINK = """
const results = async (query, kind) => JSON.parse((await tools.mcp__memory__recall_objects({
    query, kind, max_bytes:65536})).content[0].text).results;
const q = (await results('subquestion_pairs','question'))[0].object;
const j = (await results('judgment_first_read','judgment'))[0].object;
const proposals = [
    {kind:'decision',id:'decision_pairs',decision:'Use paired measurements.'},
    {kind:'investigation',id:'investigation_pairs',description:'Check pairs with incomplete measurements.'},
    {kind:'method_experience',id:'method_experience_pairs',change:'Group by pair before scoring.',
        rationale:'Compare measurements within the same pool.',outcome:'unchanged'}
];
for (const p of proposals) {
    const relation = p.kind === 'investigation' ? 'challenges' : 'derived_from';
    const r = await tools.mcp__memory__propose_memory({...p,expected_revision:0,question_id:q.id,scope:'',
        object_refs:[{id:q.id,revision:q.revision,relation:'background'},{id:j.id,revision:j.revision,relation}]});
    text(r.content[0].text);
}
"""

INSPECT = """
const get = async (id) => JSON.parse((await tools.mcp__memory__get_evidence({id,revision:1})).content[0].text).results;
const j = (await get('judgment_first_read'))[0];
const m = (await get('method_experience_pairs'))[0];
text('judgment=' + (j?.freshness ?? 'absent'));
text('challenge=' + (j?.counterevidence?.[0]?.id ?? 'absent'));
text('experience=' + (m?.freshness ?? 'absent'));
text('outcome=' + (m?.object.content.outcome ?? 'absent'));
"""


@needs_codex
class ObjectLifecycle(unittest.TestCase):
    def test_pinned_subquestion_challenge_and_owner_events_in_new_snapshots(self):
        with Root() as root:
            layout = root.layout
            for task in ('author', 'analysis', 'reader'):
                root.add_task(task)
            for folder in ('tools', 'codex'):
                shutil.copytree(SEED / folder, layout.work / folder, ignore=shutil.ignore_patterns('__pycache__'))
            sha = hashlib.sha256(SOURCE).hexdigest()
            (layout.work / 'paired.xml').write_bytes(SOURCE)
            (layout.work / 'tools/literature/sources.json').write_text(json.dumps({'paired': {
                'path': 'paired.xml', 'media_type': 'application/xml', 'sha256': sha}}))

            def run(task, script):
                root.model.push(run_js(script), say('done'))
                call = run_task(layout, task)
                self.assertEqual(call['status'], 'finished', call['problems'])
                report = load(layout).reports[call['id']]
                self.assertEqual(report['rejected'], [], report)
                return report

            first = run('author', READ_AND_PROPOSE + ASK)
            self.assertEqual((first['objects']['question'], first['objects']['judgment']), (2, 1))
            linked = run('analysis', LINK)
            self.assertEqual(linked['objects'], {'decision': 1, 'investigation': 1, 'method_experience': 1},
                             root.model.tool_outputs())

            record_owner_action(layout, 'source_changed', {'source_sha256': sha})
            run('reader', INSPECT)
            output = ' '.join(root.model.tool_outputs())
            for needle in ('judgment=needs_review', 'challenge=investigation_pairs',
                           'experience=needs_review', 'outcome=unchanged'):
                self.assertIn(needle, output)

            record_owner_action(layout, 'withdrawn', {'id': 'judgment_first_read', 'revision': 1})
            run('reader', INSPECT)
            self.assertIn('experience=unavailable', ' '.join(root.model.tool_outputs()))

            # Same-task filtering also removes other tasks' interpretations of the hidden judgment.
            root.model.push(run_js(INSPECT), say('done'))
            trial = run_trial(layout, commit=root.store.resolve(root.store.head()), round_id='M0001', task_id='author')
            self.assertEqual(trial['memory']['excluded']['method_experience'], 1)
            output = ' '.join(root.model.tool_outputs())
            for needle in ('judgment=absent', 'challenge=absent', 'experience=absent'):
                self.assertIn(needle, output)
