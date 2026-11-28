import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from rsi.limits import Limits, refusal, usage_by_class
from rsi.records import Records
from tests.links import make_link


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.records = Records(self.tmp / 'records')

    def index(self) -> list[dict]:
        text = (self.records.root / 'index.jsonl').read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines()]

    def test_ids_and_lifecycle(self):
        first, second = self.records.new_id('R'), self.records.new_id('R')
        self.assertEqual((first, second, self.records.new_id('M')), ('R0001', 'R0002', 'M0001'))
        self.records.start_call(first, {'role': 'lab', 'purpose': 'task'})
        self.assertEqual(self.records.load_call(first)['status'], 'started')
        (self.records.call_dir(first) / 'prompt.txt').write_text('p')
        done = self.records.finish_call(first, {'status': 'finished', 'usage': None})
        self.assertEqual(done['files']['prompt.txt'], hashlib.sha256(b'p').hexdigest())
        self.assertEqual([c['id'] for c in self.records.calls()], ['R0001'])
        lines = (self.records.root / 'index.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])['id'], 'R0001')

    def test_nothing_is_overwritten_and_ids_are_checked(self):
        self.records.write_json('releases/V0002.json', {'a': 1})
        with self.assertRaises(FileExistsError): self.records.write_json('releases/V0002.json', {'a': 2})
        for bad in ('../R0001', 'M0001', 'R1', 'R0001/x'):
            with self.assertRaises(ValueError): self.records.call_dir(bad)
        with self.assertRaises(KeyError): self.records.load_call('R0099')

    def test_a_call_record_is_started_once_and_finished_once(self):
        call = self.records.new_id('R')
        self.records.start_call(call, {'role': 'lab', 'purpose': 'task'})
        with self.assertRaises(FileExistsError): self.records.start_call(call, {'role': 'meta'})
        with self.assertRaises(ValueError): self.records.finish_call(call, {'usage': None})     # no end status
        with self.assertRaises(ValueError): self.records.finish_call(call, {'status': 'done'})
        with self.assertRaises(ValueError): self.records.finish_call(call, {'id': 'R0002', 'status': 'failed'})
        done = self.records.finish_call(call, {'status': 'failed', 'exit_code': 1})
        self.assertEqual((done['id'], done['role'], done['status'], done['exit_code']), (call, 'lab', 'failed', 1))
        with self.assertRaises(ValueError): self.records.finish_call(call, {'status': 'finished'})
        self.assertEqual(self.records.load_call(call), done)
        with self.assertRaises(KeyError): self.records.finish_call(self.records.new_id('R'), {'status': 'finished'})
        with self.assertRaises(ValueError): self.records.start_call(self.records.new_id('R'), {'id': 'R0001'})
        line = self.index()[-1]
        self.assertEqual((line['kind'], line['id'], line['status'], line['path']), ('call', call, 'failed',
                                                                                   f'runs/{call}/call.json'))
        self.assertEqual(line['sha256'], sha((self.records.call_dir(call) / 'call.json').read_bytes()))
        self.assertIsNotNone(datetime.fromisoformat(line['at']).utcoffset())

    def test_files_cover_sub_folders_and_keep_links_as_links(self):
        outside = self.tmp / 'outside'
        outside.mkdir(); (outside / 'secret.txt').write_text('secret')
        call = self.records.new_id('R')
        folder = self.records.call_dir(call)
        self.records.start_call(call, {'role': 'lab'})
        (folder / 'out' / 'deep').mkdir(parents=True); (folder / 'out' / 'deep' / 'a.txt').write_bytes(b'a')
        (folder / 'out' / 'deep' / 'call.json').write_bytes(b'{}')       # only the top call.json is the record
        (folder / 'empty').mkdir()
        make_link(outside / 'secret.txt', folder / 'out' / 'file-link')
        make_link(outside, folder / 'out' / 'dir-link')
        make_link('missing', folder / 'dangling')
        file_target = os.readlink(folder / 'out' / 'file-link')
        dir_target = os.readlink(folder / 'out' / 'dir-link')
        done = self.records.finish_call(call, {'status': 'finished'})
        self.assertEqual(done['files'], {'dangling': sha(b'missing'), 'out/deep/a.txt': sha(b'a'),
                                         'out/deep/call.json': sha(b'{}'),
                                         'out/dir-link': sha(os.fsencode(dir_target)),
                                         'out/file-link': sha(os.fsencode(file_target))})
        self.assertEqual(done['links'], {'dangling': 'missing', 'out/dir-link': dir_target,
                                         'out/file-link': file_target})
        self.assertEqual(self.records.load_call(call)['files'], done['files'])

    def test_ids_are_reserved_and_calls_are_in_numeric_order(self):
        for bad in ('X', 'V', '', 'RR'):
            with self.assertRaises(ValueError): self.records.new_id(bad)
        (self.records.root / 'runs' / 'R10000').mkdir(parents=True)
        self.records.start_call('R10000', {'role': 'lab'})
        self.records.start_call('R9999', {'role': 'meta'})
        self.assertEqual(self.records.new_id('R'), 'R10001')         # reserved but not started: not a call yet
        self.assertEqual([c['id'] for c in self.records.calls()], ['R9999', 'R10000'])
        self.assertEqual({c['status'] for c in self.records.calls()}, {'started'})

    def test_a_bad_record_does_not_hide_the_others(self):
        before = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
        for _ in range(6):
            fields = {'role': 'lab', 'cost_class': 'online', 'started': before}
            self.records.start_call(self.records.new_id('R'), fields)
        self.records.finish_call('R0001', {'status': 'finished', 'usage': {'input_tokens': 5, 'output_tokens': 1}})
        bad = {call: self.records.call_dir(call) / 'call.json' for call in ('R0002', 'R0003', 'R0004', 'R0005')}
        bad['R0002'].write_text('{"id": "R0002", "sta')                # cut short
        bad['R0003'].write_text('null')
        bad['R0004'].write_bytes(b'{"id": "\xff"}')                    # not UTF-8
        bad['R0005'].unlink(); bad['R0005'].mkdir()                     # cannot be read as a file
        calls = self.records.calls()
        self.assertEqual([c['id'] for c in calls], ['R0001', 'R0002', 'R0003', 'R0004', 'R0005', 'R0006'])
        self.assertEqual([c['status'] for c in calls], ['finished'] + ['unreadable'] * 4 + ['started'])
        for call, path in bad.items():
            stand_in = calls[int(call[1:]) - 1]
            self.assertEqual(sorted(stand_in), ['id', 'problem', 'status'])
            self.assertIn(str(path), stand_in['problem'])
            with self.assertRaises(ValueError) as caught: self.records.load_call(call)
            self.assertEqual(str(caught.exception), stand_in['problem'])
        with self.assertRaises(ValueError): self.records.finish_call('R0003', {'status': 'interrupted'})
        now = datetime.now().astimezone()
        self.assertIsNone(refusal(Limits(day_calls=5), calls, now))       # only the four unreadable count today
        self.assertIn("Unreadable call records among them, counted as today's: 4.",
                      refusal(Limits(day_calls=4), calls, now))
        summary = usage_by_class(calls)
        self.assertEqual(summary['unknown'], {'calls': 4, 'input_tokens': 0, 'output_tokens': 0,
                                              'unknown_usage_calls': 4})
        self.assertEqual(summary['online'], {'calls': 2, 'input_tokens': 5, 'output_tokens': 1,
                                             'unknown_usage_calls': 1})

    def test_application_ids_are_claimed_by_their_file(self):
        applications = self.records.root / 'applications'
        self.assertEqual((self.records.new_id('A'), self.records.new_id('A')), ('A0001', 'A0001'))
        self.assertFalse(applications.exists())                       # nothing is created for an application id
        self.records.write_json('applications/A0001.json', {'request': 'x'})
        self.assertEqual(self.records.new_id('A'), 'A0002')
        with self.assertRaises(FileExistsError): self.records.write_json('applications/A0001.json', {'request': 'y'})
        (applications / 'A0004').mkdir()                              # a leftover entry also counts as taken
        self.assertEqual(self.records.new_id('A'), 'A0005')
        self.assertEqual(sorted(path.name for path in applications.iterdir()), ['A0001.json', 'A0004'])

    def test_write_json_and_notes_are_indexed(self):
        # A key and a value in Chinese: records are written as UTF-8 text, never as escapes.
        path = self.records.write_json('rounds/M0001/decision-1.json', {'decision': 'no_change', '诊断': '无'})
        self.assertEqual(path, self.records.root / 'rounds' / 'M0001' / 'decision-1.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'decision': 'no_change', '诊断': '无'})
        self.assertIn('"诊断": "无"', path.read_text(encoding='utf-8'))
        self.records.note('refusal', {'reason': 'The calls of today reached the limit'})
        lines = self.index()
        self.assertEqual([line['kind'] for line in lines], ['file', 'refusal'])
        self.assertEqual((lines[0]['path'], lines[0]['sha256']), ('rounds/M0001/decision-1.json',
                                                                  sha(path.read_bytes())))
        self.assertEqual(lines[1]['reason'], 'The calls of today reached the limit')
        self.assertIsNotNone(datetime.fromisoformat(lines[1]['at']).utcoffset())
        with self.assertRaises(ValueError): self.records.note('refusal', {'kind': 'call'})
        for bad in ('../x', 'call', 'file', ''):
            with self.assertRaises(ValueError): self.records.note(bad, {})
        for bad in ('../x.json', '/tmp/x.json', 'a/../b.json', '.hidden.json', 'a/.git/x', 'a//b.json', '',
                    'index.jsonl', 'runs/R0001/call.json'):
            with self.assertRaises(ValueError, msg=bad): self.records.write_json(bad, {})
        self.assertEqual(len(self.index()), 2)

    def test_independent_writers_append_whole_records(self):
        script = ('from pathlib import Path\n'
                  'import sys\n'
                  'from rsi.records import Records\n'
                  'records = Records(Path(sys.argv[1]))\n'
                  'for number in range(20):\n'
                  '    records.note("test", {"writer": sys.argv[2], "number": number})\n')

        def write(writer):
            subprocess.run([sys.executable, '-B', '-c', script, str(self.records.root), str(writer)],
                           cwd=Path(__file__).resolve().parents[1], check=True, timeout=30)

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(write, range(3)))
        rows = self.index()
        self.assertCountEqual([(entry['writer'], entry['number']) for entry in rows],
                              [(str(writer), number) for writer in range(3) for number in range(20)])

    def test_write_json_never_writes_through_a_link(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        make_link(outside, self.records.root / 'linked')
        with self.assertRaises(ValueError): self.records.write_json('linked/x.json', {})
        make_link(outside / 'y.json', self.records.root / 'y.json')
        with self.assertRaises(FileExistsError): self.records.write_json('y.json', {})
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
