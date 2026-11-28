import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import literature_logs
from rsi.codexrun import CallResult
from rsi.init import recover
from rsi.labrun import run_task, run_trial
from rsi.mockmodel import ScriptedModel, run_js, say
from tests.support import Root, needs_codex


def spool(workspace: Path, suffix: int = 1) -> Path:
    folder = workspace / '.tmp/literature/calls'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{suffix:032x}.jsonl'


def row(suffix: int = 1) -> dict:
    return {'invocation_id': f'{suffix:032x}', 'tool': 'read_document', 'status': 'ok'}


def write_row(workspace: Path, suffix: int = 1):
    spool(workspace, suffix).write_text(json.dumps(row(suffix)) + '\n')


class Collection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name) / 'sealed'
        self.work.mkdir()
        self.record = self.work.parent / 'record'
        self.record.mkdir()

    def collect(self):
        return literature_logs.collect(self.work, literature_logs.identity(self.work), self.record)

    def test_collect_partial_and_concurrent_process_files_without_following_links(self):
        write_row(self.work, 1)
        write_row(self.work, 2)
        with spool(self.work, 1).open('a') as stream:
            stream.write('{}\n' + json.dumps(row(1)) + '\n' + json.dumps(row(3))[:-1])
        secret = self.work.parent / 'secret'
        secret.write_text(json.dumps(row(99)) + '\n')
        spool(self.work, 3).symlink_to(secret)
        os.link(secret, spool(self.work, 4))
        os.mkfifo(spool(self.work, 5))
        problems = '\n'.join(self.collect())
        reports = [json.loads(line) for line in (self.record / literature_logs.LOG).read_text().splitlines()]
        self.assertEqual(reports, [row(1), row(2)])
        for expected in ('duplicate', 'invocation_id', 'incomplete', 'single-link regular file'):
            self.assertIn(expected, problems)

    def reports(self) -> list[dict]:
        return [json.loads(line) for line in (self.record / literature_logs.LOG).read_text().splitlines()]

    def test_a_row_over_the_row_limit_is_left_out_and_named(self):
        long = json.dumps({**row(1), 'pad': 'x' * 200})
        spool(self.work, 1).write_text(long + '\n' + json.dumps(row(2)) + '\n')
        with mock.patch.object(literature_logs, 'ROW_BYTES', 100):
            problems = self.collect()
        self.assertEqual(self.reports(), [row(2)])
        self.assertEqual(problems, [f'Literature log {1:032x}.jsonl line 1 exceeds 100 bytes'])

    def test_the_byte_limit_stops_at_a_whole_row_and_skips_the_remaining_files(self):
        for number in (1, 2):
            spool(self.work, number).write_text(''.join(json.dumps(row(number * 10 + index)) + '\n'
                                                        for index in range(3)))
        size = len(json.dumps(row(10))) + 1
        with mock.patch.object(literature_logs, 'TOTAL_BYTES', size * 2 + 5):
            problems = self.collect()
        self.assertEqual(self.reports(), [row(10), row(11)])          # the first file's first two rows only
        self.assertIn(f'Literature log collection reached its {size * 2 + 5}-byte limit', problems)
        self.assertIn(f'Literature log {1:032x}.jsonl line 3 is incomplete', problems)
        self.assertIn('Literature log collection reached its byte limit; remaining files were skipped', problems)

    def test_the_file_limit_reads_no_more_files(self):
        for number in range(1, 6):
            write_row(self.work, number)
        with mock.patch.object(literature_logs, 'MAX_FILES', 3):
            problems = self.collect()
        self.assertEqual(len(self.reports()), 3)
        self.assertIn('Literature log collection reached its 3-file limit', problems)

    def test_the_attachment_is_written_whole_and_a_stale_temporary_file_is_removed(self):
        write_row(self.work, 1)
        (self.record / literature_logs.PENDING).write_text('left by an interrupted collection\n')
        self.assertEqual(self.collect(), [])
        self.assertEqual(sorted(path.name for path in self.record.iterdir()), [literature_logs.LOG])
        self.assertEqual(self.reports(), [row(1)])
        (self.record / literature_logs.PENDING).write_text('left again\n')
        self.assertEqual(self.collect(), [])                    # already published: kept as it is
        self.assertEqual(sorted(path.name for path in self.record.iterdir()), [literature_logs.LOG])

    def test_directory_link_is_not_opened_and_unused_server_gets_an_empty_attachment(self):
        (self.work / '.tmp').symlink_to(self.work.parent)
        with self.assertRaises(OSError):
            self.collect()
        self.assertEqual(list(self.record.iterdir()), [])
        (self.work / '.tmp').unlink()
        self.assertEqual(self.collect(), [])
        self.assertEqual((self.record / literature_logs.LOG).read_bytes(), b'')


class RunLogs(unittest.TestCase):
    def setUp(self):
        # Exercise real task/trial staging, sealing, versioning and record hashing with a stand-in for Codex.
        self.enterContext(mock.patch.object(ScriptedModel, 'start', return_value='http://unused.example'))
        self.root = self.enterContext(Root(passed_check=False))
        self.enterContext(mock.patch('rsi.labrun.gate', return_value=None))
        self.enterContext(mock.patch('rsi.labrun._codex_version', return_value='test'))
        self.enterContext(mock.patch('rsi.compose.native_codex', return_value='/usr/bin/codex'))
        self.root.add_task('T1')
        self.layout, self.records = self.root.layout, self.root.records
        (self.layout.work / 'codex').mkdir()
        (self.layout.work / 'codex/settings.toml').write_text('[tool_servers.literature]\ncommand="python3"\n')

    def check_record(self, call, expected):
        blob = (self.records.call_dir(call['id']) / literature_logs.LOG).read_bytes()
        self.assertEqual([json.loads(line) for line in blob.splitlines()], expected)
        self.assertEqual(call['files'][literature_logs.LOG], hashlib.sha256(blob).hexdigest())

    def test_task_trial_partial_results_and_manual_work_do_not_mix(self):
        write_row(self.layout.work, 10)
        def task(**kwargs):
            write_row(kwargs['cwd'], 20)
            return CallResult('failed', 1, None, {'input_tokens': 5}, '', [])
        with mock.patch('rsi.labrun.run_codex', side_effect=task):
            call = run_task(self.layout, 'T1')
        self.assertEqual(call['status'], 'failed')
        self.assertEqual(call['usage'], {'input_tokens': 5})
        self.check_record(call, [row(20)])

        def trial(**kwargs):
            write_row(kwargs['cwd'], 30)
            return CallResult('timed_out', None, None, None, '', [])
        with mock.patch('rsi.labrun.run_codex', side_effect=trial):
            call = run_trial(self.layout, commit=self.root.store.head(), round_id='M0001', probe='test')
        self.assertEqual(call['status'], 'timed_out')
        self.check_record(call, [row(30)])

    def test_interruption_preserves_the_live_tree_and_hashes_its_completed_rows(self):
        def interrupt(**kwargs):
            write_row(kwargs['cwd'])
            (kwargs['cwd'] / 'manual.txt').write_text('keep')
            raise KeyboardInterrupt
        with mock.patch('rsi.labrun.run_codex', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            run_task(self.layout, 'T1')
        call, = self.records.calls()
        self.assertEqual(call['status'], 'interrupted')
        self.assertEqual((self.layout.work / 'manual.txt').read_text(), 'keep')
        self.check_record(call, [row()])

    def test_recovery_never_hashes_a_temporary_file_an_interrupted_collection_left(self):
        call = self.records.new_id('R')
        self.records.start_call(call, {'workspace': 'work', 'literature_workspace':
                                       literature_logs.identity(self.layout.work)})
        folder = self.records.call_dir(call)
        (folder / literature_logs.LOG).write_text(json.dumps(row(60)) + '\n')       # published before the end
        (folder / literature_logs.PENDING).write_text(json.dumps(row(61)) + '\n')
        self.assertEqual(recover(self.layout), [call])
        finished = self.records.load_call(call)
        self.assertEqual(sorted(finished['files']), [literature_logs.LOG])
        self.check_record(finished, [row(60)])

    def test_recovery_uses_only_the_matching_workspace(self):
        for present in (True, False):
            call = self.records.new_id('R')
            self.records.start_call(call, {'workspace': 'work', 'literature_workspace':
                                           literature_logs.identity(self.layout.work)})
            write_row(self.layout.work, 40)
            if not present:
                self.layout.work.rename(self.layout.root / 'old')
                self.layout.work.mkdir()
                write_row(self.layout.work, 50)
            self.assertEqual(recover(self.layout), [call])
            finished = self.records.load_call(call)
            if present:
                self.check_record(finished, [row(40)])
            else:
                self.assertNotIn(literature_logs.LOG, finished['files'])
                self.assertIn('does not belong', ' '.join(finished['problems']))


@needs_codex
class CodexServer(unittest.TestCase):
    def test_codex_discovers_and_calls_the_seed_service_and_the_task_archives_its_report(self):
        from tests.installed_seed import WORK as seed
        with Root() as root:
            root.add_task('T1')
            shutil.copytree(seed / 'tools/literature', root.layout.work / 'tools/literature',
                            ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(seed / 'codex', root.layout.work / 'codex')
            root.model.push(run_js('text(JSON.stringify(await tools.mcp__literature__list_providers({})));'),
                            say('done'))
            call = run_task(root.layout, 'T1')
            self.assertEqual(call['status'], 'finished', call.get('problems'))
            self.assertIn('mcp__literature', json.dumps(root.model.requests[0]))
            self.assertIn('europe_pmc', ' '.join(root.model.tool_outputs()))
            data = (root.records.call_dir(call['id']) / literature_logs.LOG).read_bytes()
            report, = [json.loads(line) for line in data.splitlines()]
            self.assertEqual((report['tool'], report['status']), ('list_providers', 'ok'))
            self.assertEqual(call['files'][literature_logs.LOG], hashlib.sha256(data).hexdigest())
