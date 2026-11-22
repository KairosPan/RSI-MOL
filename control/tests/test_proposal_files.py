"""Managed MCP submission and descriptor-relative collection into a sealed call's attachments."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import proposal_files
from rsi.memory_snapshot import place_snapshot
from rsi.paths import Layout

SEED = Path(__file__).resolve().parents[1] / 'seed/work/tools/memory'


def judgment():
    ref = {'relation': 'supports', 'evidence_id': 'passage_' + '2' * 64, 'revision': 1,
           'source_run': 'R0013', 'document_id': 'doc_' + '3' * 64, 'document_version': '4' * 64,
           'source_sha256': '5' * 64, 'locator': 'block:000024', 'quote': 'paired differences'}
    return {'kind': 'judgment', 'id': 'judgment_pair', 'expected_revision': 0, 'question_id': 'task-A',
            'statement': 'Calculate a difference within each pool.', 'scope': 'Pools measured in both arms.',
            'source_refs': [ref]}


class ProposalFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.record = self.root / 'records/runs/R0001'
        self.record.mkdir(parents=True)
        self.work = self.root / 'work'
        shutil.copytree(SEED, self.work / 'tools/memory')
        (self.work / '.tmp').mkdir()
        self.spool = self.work / '.tmp' / proposal_files.DIRECTORY

    def server(self, params, enabled=True):
        messages = [{'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}, {'method': 'tools/list'},
                    {'method': 'tools/call', 'params': {'name': 'propose_memory', 'arguments': params}}]
        lines = ''.join(json.dumps(dict(numbered, jsonrpc='2.0', id=n)) + '\n' for n, numbered in enumerate(messages))
        env = dict(os.environ, RSI_MEMORY_PROPOSALS='1' if enabled else '0')
        completed = subprocess.run([sys.executable, '-B', str(self.work / 'tools/memory/server.py')],
                                   input=lines, env=env, text=True, capture_output=True, timeout=10, check=True)
        replies = [json.loads(line)['result'] for line in completed.stdout.splitlines()]
        return {tool['name']: tool['inputSchema'] for tool in replies[1]['tools']}, replies[2]

    def test_managed_tool_and_sealed_attachment_are_one_pending_proposal(self):
        identity = proposal_files.prepare(self.work)
        place_snapshot(Layout(self.root), self.work)
        args = judgment()
        names, result = self.server(args)
        self.assertIn('propose_memory', names)
        self.assertFalse(result['isError'], result)
        reply = json.loads(result['content'][0]['text'])
        self.assertEqual(reply['status'], 'pending')
        name = reply['proposal_id'] + '.json'
        self.assertRegex(name, proposal_files.FILE_NAME)
        sealed = self.root / '.sealed-R0001'
        self.work.rename(sealed)
        self.assertEqual(proposal_files.collect(sealed, identity, self.record), [])
        attached = self.record / 'proposals' / name
        self.assertEqual(json.loads(attached.read_bytes()), args)
        (sealed / '.tmp' / proposal_files.DIRECTORY / name).write_bytes(b'other')
        self.assertEqual(proposal_files.collect(sealed, identity, self.record), [])
        self.assertEqual(json.loads(attached.read_bytes()), args)

    def test_conditions_and_input_schema(self):
        identity = proposal_files.prepare(self.work)
        for condition, enabled in [('live', False), ('trial', True), ('none', True)]:
            with self.subTest(condition=condition):
                place_snapshot(Layout(self.root), self.work, condition)
                tools, result = self.server(judgment(), enabled)
                self.assertNotIn('propose_memory', tools)
                self.assertTrue(result['isError'])
                shutil.rmtree(self.work / 'memory')
        place_snapshot(Layout(self.root), self.work)
        args = judgment()
        for bad in (dict(args, verified=True), dict(args, id='../injected'), dict(args, expected_revision=True),
                    dict(args, source_refs=[]), dict(args, source_refs=[dict(args['source_refs'][0], quote=' ')])):
            self.assertTrue(self.server(bad)[1]['isError'])
        self.assertEqual(list(self.spool.iterdir()), [])
        self.assertEqual(proposal_files.collect(self.work, identity, self.record), [])

    def test_current_and_historical_passages_share_a_proposal_and_have_distinct_shapes(self):
        identity = proposal_files.prepare(self.work)
        place_snapshot(Layout(self.root), self.work)
        args = judgment()
        historical, = args['source_refs']
        current = {key: historical[key] for key in ('relation', 'document_id', 'document_version',
                                                    'source_sha256', 'locator', 'quote')}
        current.update(delivery_source='literature', invocation_id='1' * 32)
        args['source_refs'] = [current, historical]
        schemas, result = self.server(args)
        self.assertFalse(result['isError'], result)
        shapes = schemas['propose_memory']['properties']['source_refs']['items']['oneOf']
        self.assertCountEqual([set(s['required']) for s in shapes], [set(current), set(historical)])
        self.assertTrue(all(s['additionalProperties'] is False for s in shapes))
        files_before = list(self.spool.iterdir())
        for invalid in (dict(current, delivery_source='memory'), dict(current, invocation_id='exec-1234'),
                        dict(current, evidence_id=historical['evidence_id']),
                        {k: v for k, v in current.items() if k != 'invocation_id'}):
            with self.subTest(invalid=invalid):
                self.assertTrue(self.server(dict(args, source_refs=[invalid]))[1]['isError'])
        self.assertEqual(list(self.spool.iterdir()), files_before)
        self.assertEqual(proposal_files.collect(self.work, identity, self.record), [])
        path, = (self.record / 'proposals').iterdir()
        self.assertEqual(json.loads(path.read_bytes())['source_refs'], [current, historical])

    def test_kind_fields_scope_and_reference_are_checked_before_writing(self):
        proposal_files.prepare(self.work)
        place_snapshot(Layout(self.root), self.work)
        common = {'question_id': 'task-A', 'expected_revision': 0}
        subquestion = dict(common, kind='question', id='subquestion_pair', text='How comparable are the pairs?')
        experience = dict(common, kind='method_experience', id='method_experience_pair', change='Group pairs.',
                          rationale='Compare pooled scores.', outcome='unchanged', scope='',
                          object_refs=[{'id': 'judgment_pair', 'revision': 1, 'relation': 'derived_from'}])
        for proposal in (subquestion, experience):
            result = self.server(proposal)[1]
            self.assertFalse(result['isError'], result)
        for invalid in (dict(subquestion, id='judgment_other'), dict(subquestion, scope='unknown'),
                        dict(experience, object_refs=[]), dict(experience, scope=' '),
                        dict(experience, outcome='probably improved'), dict(experience, freshness='current')):
            with self.subTest(invalid=invalid):
                self.assertTrue(self.server(invalid)[1]['isError'])
        self.assertEqual(len(list(self.spool.iterdir())), 2)

    def test_sealed_file_types_identity_and_caps(self):
        identity = proposal_files.prepare(self.work)
        external = self.root / 'outside'
        external.write_bytes(b'outside content')
        names = [self.spool / f'{n:032x}.json' for n in range(5)]
        names[0].write_bytes(b'{}')
        names[1].symlink_to(external)
        os.link(external, names[2])
        os.mkfifo(names[3])
        names[4].write_bytes(b'X' * (proposal_files.MAX_BYTES + 1))
        with patch.object(proposal_files, 'MAX_FILES', 4):
            self.assertTrue(proposal_files.collect(self.work, identity, self.record))
        self.assertFalse((self.record / 'proposals').exists())
        issues = proposal_files.collect(self.work, identity, self.record)
        self.assertEqual(len(issues), 4)
        self.assertEqual([p.name for p in (self.record / 'proposals').iterdir()], [names[0].name])
        other_record = self.root / 'other-record'
        other_record.mkdir()
        self.spool.rename(self.spool.with_name('previous'))
        self.spool.mkdir()
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            proposal_files.collect(self.work, identity, other_record)
        self.spool.rmdir()
        self.spool.symlink_to(self.spool.with_name('previous'))
        with self.assertRaises(OSError):
            proposal_files.collect(self.work, identity, other_record)

    def test_unreadable_proposal_leaves_other_complete_attachments_available(self):
        identity = proposal_files.prepare(self.work)
        first, second = (f'{n:032x}.json' for n in (1, 2))
        (self.spool / first).write_bytes(b'{"first":true}')
        (self.spool / second).write_bytes(b'{"second":true}')
        read = proposal_files._read

        def read_one(folder, name):
            if name == first:
                raise OSError('temporary read failure')
            return read(folder, name)

        with patch.object(proposal_files, '_read', side_effect=read_one):
            notices = proposal_files.collect(self.work, identity, self.record)
        self.assertEqual(len(notices), 1)
        self.assertIn(first, notices[0])
        saved, = (self.record / 'proposals').iterdir()
        self.assertEqual((saved.name, saved.read_bytes()), (second, b'{"second":true}'))

    def test_contention_and_full_spool_report_a_tool_error(self):
        proposal_files.prepare(self.work)
        place_snapshot(Layout(self.root), self.work)
        fd = os.open(self.spool, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, failed = self.server(judgment())
            self.assertTrue(failed['isError'])
            self.assertIn('retry', failed['content'][0]['text'])
        finally:
            os.close(fd)
        for i in range(proposal_files.MAX_FILES):
            (self.spool / f'{i:032x}.json').touch()
        _, failed = self.server(judgment())
        self.assertTrue(failed['isError'])
        self.assertIn('limit', failed['content'][0]['text'])
        self.assertEqual(len(list(self.spool.iterdir())), proposal_files.MAX_FILES)
