"""Replay R0016 memory reads or R0010 literature reads with simulated submissions in a temporary ledger."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'control'))
from rsi import memory_ingest, memory_store, proposal_files
from rsi.memory_history import ADDRESS, attachment, read, trusted_passages
from rsi.memory_receipts import SESSION_LIMIT, SOURCE_LIMIT, session_paths
from rsi.memory_snapshot import place_snapshot
from rsi.paths import Layout
from rsi.records import Records
from rsi.tasks import _sections


def require(condition, message):
    if not condition:
        raise ValueError(message)


def copy_originals(source, target, hashes):
    folder = target.library / 'objects/sha256'
    folder.mkdir(parents=True, exist_ok=True)
    for sha in hashes:
        require(memory_store.DIGEST.fullmatch(sha), 'Invalid archived original address')
        (folder / sha).write_bytes(read(source.root, f'library/objects/sha256/{sha}', SOURCE_LIMIT))


def copy_prefix(source, target, state, version):
    memory_store.init_store(target)
    reports = target.records / 'memory'
    reports.mkdir(parents=True)
    for segment in (source.memory / 'batches').glob('*.jsonl'):
        batch_id = segment.stem.split('-', 1)[1]
        report = (state.owner_reports if batch_id.startswith('O') else state.reports)[batch_id]
        if report['version_after'] <= version:
            shutil.copyfile(segment, target.memory / 'batches' / segment.name)
            shutil.copyfile(source.records / 'memory' / (batch_id + '.json'), reports / (batch_id + '.json'))
    prefix = memory_store.load(target)
    require(prefix.version == version and not (prefix.missing_reports or prefix.missing_owner_reports),
            'Incomplete starting ledger prefix')
    copy_originals(source, target, memory_store.source_hashes(prefix))


def simulate(target, original, call, citation, variants):
    """Extend a private copy of the lead session. All pre-existing tool calls are the original recorded bytes."""
    records = Records(target.records)
    run = call['id']
    goal = _sections(attachment(original, call, 'prompt.txt', 1 << 20).decode(), Path('prompt.txt'))['Goal']
    workspace = target.work
    (workspace / '.tmp').mkdir(parents=True)
    identity = proposal_files.prepare(workspace)
    records.start_call(run, {k: call[k] for k in ('purpose', 'task', 'version_before', 'thread_id',
                                                   'subagent_threads', 'started', 'memory')} |
                       {'task_goal': goal, 'proposal_workspace': identity})
    folder = records.call_dir(run)
    (folder / 'sessions').mkdir()
    paths = session_paths(call)
    lead_rel, = (rel for rel, thread, _ in paths if thread == call['thread_id'])
    lead = folder / lead_rel
    remaining = SESSION_LIMIT
    for rel, _, _ in paths:
        raw = attachment(original, call, rel, remaining)
        (folder / rel).write_bytes(raw)
        remaining -= len(raw)
    ended = datetime.fromisoformat(call['ended'])
    for index, (label, reference) in enumerate(variants.items(), 1):
        args = {'kind': 'judgment', 'id': 'judgment_' + label, 'expected_revision': 0,
                'question_id': call['task'], 'statement': citation['claim'],
                'scope': 'Each candidate pool receives both arms and contributes one paired contrast.',
                'source_refs': [reference]}
        pid = f'{index:032x}'
        receipt = {'status': 'pending', 'proposal_id': pid}
        item = {'type': 'McpToolCall', 'status': 'completed', 'id': 'simulated-' + label,
                'server': 'memory', 'tool': 'propose_memory', 'arguments': args,
                'result': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(receipt)}]}}
        at = ended + timedelta(seconds=index)
        epoch_ms = round(at.timestamp() * 1000)
        event = {'type': 'event_msg', 'timestamp': at.isoformat(), 'payload': {
            'type': 'item_completed', 'thread_id': call['thread_id'], 'item': item,
            'started_at_ms': epoch_ms, 'completed_at_ms': epoch_ms}}
        with lead.open('ab') as stream:
            stream.write(memory_store.encode(event) + b'\n')
        (workspace / '.tmp' / proposal_files.DIRECTORY / (pid + '.json')).write_bytes(memory_store.encode(args))
    require(not proposal_files.collect(workspace, identity, folder), 'Failed to collect simulated proposal files')
    records.finish_call(run, {'status': 'finished', 'ended': (ended + timedelta(seconds=len(variants) + 1)).isoformat()})


def replay(root=REPO):
    original = Layout(root)
    call = Records(original.records).load_call('R0016')
    state = memory_store.load(original)
    trusted_passages(state, call['memory'])  # authenticate the archived starting fingerprint
    citations = json.loads(attachment(original, call, 'out/evidence.json', 256 << 10))
    citation, = (row for row in citations if row['doi'] == '10.1186/cc1548')
    reader = runpy.run_path(str(REPO / 'control/seed/work/tools/memory/query.py'))['Snapshot']
    with tempfile.TemporaryDirectory() as tmp:
        target = Layout(Path(tmp))
        copy_prefix(original, target, state, call['memory']['version'])
        ref = {k: citation[k] for k in (*ADDRESS, 'evidence_id', 'revision', 'source_run', 'quote')}
        ref['relation'] = 'supports'
        simulate(target, original, call, citation, {'paired': ref, 'quote': dict(ref, quote='Changed quotation.'),
                 'revision': dict(ref, revision=ref['revision'] + 1), 'alias': dict(ref, document_id='doc_' + '0' * 64)})
        report, = memory_ingest.backfill(target)
        rejected = {r['proposal_id']: r['reason'] for r in report['rejected']}
        expected = {2: 'quotation does not occur verbatim', 3: 'evidence revision was not returned',
                    4: 'evidence address or historical delivery does not match'}
        require(report['objects'] == {'question': 1, 'judgment': 1} and len(report['rejected']) == 3 and
                all(rejected.get(f'{i:032x}', '').startswith(reason) for i, reason in expected.items()),
                f'Unexpected registration result: {report}')
        require(memory_ingest.backfill(target) == [], 'Replay published a second batch')
        result = {}
        for condition in ('live', 'trial'):
            folder = target.root / condition
            folder.mkdir()
            place_snapshot(target, folder, condition, task=call['task'])
            result[condition] = list(reader(folder / 'memory').recall_judgments('judgment_paired'))
        judgment, = result['live']
        obj = judgment['object']
        proof, = obj['source_refs']
        require(judgment['freshness'] == 'current' and obj['content']['support_status'] == 'pending_review'
                and proof['quote'] == citation['quote']
                and proof['quote'] in judgment['evidence'][0]['passage']['text']
                and proof['source_run'] == 'R0013' and proof['id'] == citation['evidence_id']
                and proof['historical_invocation'] == 'dc515e24941947729c7eabb579fe5f15'
                and proof['call_id'] == 'exec-be751f38-abac-4600-aaf4-d73dacc2e160',
                'The registered judgment did not retain the R0016 citation provenance')
        require(not result['trial'], 'Author task trial retrieved its own judgment')
        return {'source_run': call['id'], 'starting_ledger': call['memory']['version'],
                'simulated_proposals': 4, 'accepted_judgments': 1, 'rejections': report['rejected'],
                'live_judgments': 1, 'same_task_trial_judgments': 0, 'matched_call': proof['call_id'],
                'support_status': obj['content']['support_status']}


def replay_literature(root=REPO):
    """Start empty; the cited complete body passage was delivered by the R0010 literature child."""
    original = Layout(root)
    call = Records(original.records).load_call('R0010')
    citations = json.loads(attachment(original, call, 'out/evidence.json', 256 << 10))
    citation, = (row for row in citations if row['doi'] == '10.1371/journal.pbio.2005282'
                 and row['locator'] == 'block:000037')
    invocation = '7f1c74670f4447ea8ce18e4b0f81172c'
    ref = {k: citation[k] for k in (*ADDRESS, 'quote')}
    ref.update(delivery_source='literature', invocation_id=invocation, relation='supports')
    with tempfile.TemporaryDirectory() as tmp:
        target = Layout(Path(tmp))
        workspace = target.root / 'starting-snapshot'
        workspace.mkdir()
        snapshot = place_snapshot(target, workspace)
        require(snapshot['version'] == 0, 'Expected a fresh ledger')
        copy_originals(original, target, {row['source_sha256'] for row in citations})
        simulate(target, original, dict(call, memory=snapshot), citation, {'paired': ref})
        report, = memory_ingest.backfill(target)
        obj, = memory_store.show(target, 'judgment_paired')['versions']
        proof, = obj['source_refs']
        passage, = memory_store.show(target, proof['id'])['versions']
        require(proof['delivery_source'] == 'literature' and proof['source_run'] == call['id']
                and proof['invocation_id'] == invocation and proof['thread'] in call['subagent_threads']
                and proof['call_id'] == 'exec-c04c7388-5d1e-44d8-b7a3-ffa059f7d620'
                and proof['revision'] == passage['revision'] and ref['quote'] in passage['content']['text'],
                'Judgment did not resolve to the cited R0010 child receipt and new passage')
        require(report['version_before'] == 0 and report['objects']['judgment'] == 1 and
                not report['rejected'] and memory_ingest.verify(target)['runs'] == 1,
                'Expected one verified batch containing the evidence and judgment')
        return {'source_run': call['id'], 'starting_ledger': 0, 'batch': report['run'], 'objects': report['objects'],
                'matched_call': proof['call_id'], 'invocation_id': proof['invocation_id'],
                'evidence_id': proof['id'], 'revision': proof['revision'], 'support_status': obj['content']['support_status']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-literature', action='store_true', help='Replay the R0010 same-run literature case')
    args = parser.parse_args()
    print(json.dumps(replay_literature() if args.current_literature else replay(), ensure_ascii=False, indent=2))
