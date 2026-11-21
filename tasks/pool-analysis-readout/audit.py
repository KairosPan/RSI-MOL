"""Read-only receipt audit. Run with the protocol run ID, then optionally its readout run ID."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'control'))
from rsi.memory_history import (ADDRESS, attachment, passage_index, read, require, returned_memory,
                                returned_objects, timeline, trusted_rows)
from rsi.memory_receipts import session_paths
from rsi.memory_store import encode, load
from rsi.paths import Layout
from rsi.records import CALL_ID

BIB = re.compile(r'(?<![A-Za-z0-9_-])(judgment_[A-Za-z0-9_-]+)@([1-9][0-9]*)')
LAYOUT = Layout(ROOT)


def response(item):
    reply = item.get('result') or {}
    parts = reply.get('content', [])
    if item.get('status') != 'completed' or reply.get('isError') or len(parts) != 1:
        return {}
    try:
        body = json.loads(parts[0]['text'])
    except (KeyError, TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def run(state, run_id, task, output):
    require(CALL_ID.fullmatch(run_id), 'Expected an R-number')
    raw = read(ROOT, f'records/runs/{run_id}/call.json', 2 << 20)
    call = json.loads(raw)
    require((call['id'], call['task'], call['purpose']) == (run_id, task, 'task'), f'Expected {task} task run')
    require(call['memory']['condition'] == 'live', 'Expected live memory')
    rows = trusted_rows(state, call['memory'])
    items = {(thread, item['id']): item for _, _, thread, item, _ in timeline(LAYOUT, call)}
    participants = []
    for path, thread, sha in session_paths(call):
        events = (json.loads(line) for line in attachment(LAYOUT, call, path, 128 << 20).splitlines())
        provider, models = None, set()
        for event in events:
            payload = event.get('payload') or {}
            if event['type'] == 'session_meta' and payload.get('id') == thread:
                provider = payload.get('model_provider')
            if event['type'] == 'turn_context' and payload.get('model'):
                models.add(payload['model'])
        participants.append(dict(thread=thread, provider=provider, models=sorted(models), session_sha256=sha))
    report = state.reports.get(run_id)
    require(report is not None and run_id not in state.missing_reports, 'Missing independent registration report')
    memo = attachment(LAYOUT, call, output, 2 << 20).decode() if output in call['files'] else ''
    counts = Counter(item['server'] + '.' + item['tool'] for item in items.values())
    check = call.get('check')
    summary = dict(run=run_id, task=task, status=call['status'], lab=call['version_before'], tree=call['tree'],
                   started=call['started'], ended=call.get('ended'), check=check, lead_usage=call.get('usage'),
                   snapshot_version=call['memory']['version'], snapshot_sha256=call['memory']['fingerprint'],
                   call_sha256=hashlib.sha256(raw).hexdigest(), registration_tail=report['tail_sha256'],
                   participants=participants, output=output, output_sha256=call['files'].get(output), tool_calls=dict(counts),
                   eligible=(call['status'] == 'finished' and bool(memo.strip()) and (check or {}).get('passed', True)
                             and bool(participants) and all(p['provider'] == 'openai' and p['models'] for p in participants)))
    return call, rows, items, memo, summary


def verify_source(ref, items, passages, revisions):
    key = ref['id'], ref['revision']
    block = revisions[key]['content']
    address = tuple(ref[k] for k in ADDRESS)
    quote = ref['quote']
    item = items[ref['thread'], ref['call_id']]
    body = response(item)
    require(block['type'] == 'passage' and quote and quote in block['text']
            and (block['source_sha256'], block['locator']) == address[-2:], 'Cited passage does not match the ledger')
    if ref.get('delivery_source') == 'literature':
        matches = (item['server'] == 'literature' and item['tool'] in ('find_passages', 'read_document') and
                   body.get('invocation_id') == ref['invocation_id'] and any(
                       p.get('block_complete') is True and tuple(p.get(k) for k in ADDRESS) == address
                       and p.get('text') == block['text'] for p in body.get('data', {}).get('passages', [])))
    else:
        matches = item['server'] == 'memory' and any(
            found == address and (m['id'], m['revision']) == key and quote in m['text']
            and m['source_run'] == ref['source_run'] and m['historical_invocation'] == ref['historical_invocation']
            for found, m in returned_memory(item['tool'], body, passages))
    require(matches, 'Cited passage differs from its recorded native return')
    return dict(ref, tool=item['tool'])


def author(state, run_id):
    call, rows, items, _, result = run(state, run_id, 'pool-analysis-protocol', 'out/plan.md')
    revisions = {(o['id'], o['revision']): o for o in state.objects}
    passages = passage_index(rows)
    judgments = []
    for key, obj in revisions.items():
        if obj['kind'] != 'judgment' or obj['origin']['run'] != run_id:
            continue
        delivery, = (d for d in state.deliveries if (d['id'], d['revision']) == key and d['run'] == run_id)
        proposal = items[delivery['thread'], delivery['call_id']]
        pid = delivery['invocation_id']
        args = json.loads(attachment(LAYOUT, call, 'proposals/' + pid + '.json', 64 << 10))
        require(proposal['server'] == 'memory' and proposal['tool'] == 'propose_memory'
                and response(proposal) == {'status': 'pending', 'proposal_id': pid}
                and encode(proposal['arguments']) == encode(args), 'Sealed proposal differs from its native submission')
        content = obj['content']
        require(args['kind'] == 'judgment' and args['id'] == key[0] and args['expected_revision'] + 1 == key[1]
                and all(content[k] == args[k] for k in ('statement', 'scope')), 'Registered judgment differs from proposal')
        refs = [verify_source(ref, items, passages, revisions) for ref in obj['source_refs']]
        judgments.append(dict(id=key[0], revision=key[1], statement=content['statement'], scope=content['scope'],
                              support_status=content['support_status'], source_refs=refs,
                              proposal_call=delivery['call_id'], proposal_id=pid,
                              scoped_and_sourced=bool(refs and content['scope'].strip())))
    registration = state.reports[run_id]
    require(len(judgments) == registration['objects'].get('judgment', 0), 'Judgment count differs from report')
    result.update(judgments=judgments, submitted_attachments=[name for name in call['files'] if name.startswith('proposals/')],
                  rejected=registration['rejected'])
    return result


def audit(first, second=None):
    state = load(LAYOUT)
    a = author(state, first)
    eligible = {(j['id'], j['revision']) for j in a['judgments'] if a['eligible'] and j['scoped_and_sourced']}
    result = dict(author=a, ready_for_scientific_review=bool(eligible))
    if not second:
        result['outcome'] = 'review_author_before_readout' if eligible else 'author_gate_unmet'
        return result
    require(CALL_ID.fullmatch(second) and int(second[1:]) > int(first[1:]), 'Expected subsequent readout run')
    call, rows, items, memo, b = run(state, second, 'pool-analysis-readout', 'out/analysis.md')
    require(call['memory']['version'] >= state.reports[first]['version_after'], 'Readout started before author registration')
    targets = {}
    for row in rows:
        obj = row['object']
        key = obj['id'], obj['revision']
        if key in eligible:
            targets[key] = encode(obj)
    prompt = attachment(LAYOUT, call, 'prompt.txt', 2 << 20).decode()
    require(not any(identity in prompt for identity, _ in eligible), 'Readout prompt supplied an author judgment ID')
    cited = {(m[1], int(m[2])) for m in BIB.finditer(memo)}
    matches = []
    for (thread, cid), item in items.items():
        if item['server'] == 'memory':
            for identity, revision in returned_objects(item['tool'], response(item), targets):
                matches.append(dict(id=identity, revision=revision, thread=thread, call_id=cid,
                                    tool=item['tool'], cited_in_memo=(identity, revision) in cited))
    b.update(native_retrievals=matches, cited_judgments=sorted(cited))
    result['readout'] = b
    result['same_starting_tree'] = bool(a['tree']) and a['tree'] == b['tree']
    result['outcome'] = ('review_scientific_application' if b['eligible'] and any(m['cited_in_memo'] for m in matches)
                         else 'retrieval_or_final_citation_unconfirmed')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('author')
    parser.add_argument('readout', nargs='?')
    args = parser.parse_args()
    try:
        report = audit(args.author, args.readout)
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        report = {'outcome': 'audit_error', 'error': str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report['outcome'].startswith('review_') else 1)
