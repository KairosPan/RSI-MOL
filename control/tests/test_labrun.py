import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.codexrun import codex_version
from rsi.labrun import run_task, run_trial
from rsi.mockmodel import add_file, say, slow
from rsi.paths import Refused
from rsi.sessions import collect_sessions
from rsi.versions import VersionStore
from tests.support import PASS_CHECK, Root, needs_codex


@needs_codex
class LabRuns(unittest.TestCase):
    def test_task_run_end_to_end(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), add_file('notes/n.md', 'kept'),
                            say('done'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['purpose'], call['cost_class'], call['role']),
                             ('finished', 'task', 'online', 'lab'))
            self.assertEqual((call['version_before'], call['version_after'], root.store.head()),
                             ('V0001', 'V0002', 'V0002'))
            self.assertEqual(call['check'], {'passed': True, 'message': '通过', 'exit_code': 0})
            run_dir = root.records.call_dir(call['id'])
            self.assertTrue((run_dir / 'out' / 'answer.json').is_file())
            self.assertTrue((run_dir / 'diff.patch').is_file())
            self.assertFalse((root.layout.work / 'out' / 'T1').exists())
            self.assertTrue((root.layout.work / 'notes' / 'n.md').is_file())
            self.assertIn('目标', (run_dir / 'prompt.txt').read_text())
            self.assertEqual(call['codex_version'], codex_version())
            copies = [p.name for p in (run_dir / 'sessions').iterdir()]
            self.assertEqual((len(copies), copies[0].endswith(call['thread_id'] + '.jsonl'), call['subagent_threads']),
                             (1, True, []))

    def test_sessions_are_fetched_by_exact_id_only(self):
        home = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, home, True)
        day = home / 'sessions' / '2026' / '11' / '20'; day.mkdir(parents=True)
        lead, child, other = 'aaaa-lead', 'bbbb-child', 'cccc-owner'
        activity = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'SubAgentActivity', 'kind': 'started', 'agent_thread_id': child}}}
        (day / f'rollout-1-{lead}.jsonl').write_text(json.dumps(activity) + '\nnot json\n')
        (day / f'rollout-2-{child}.jsonl').write_text('{}\n'); (day / f'rollout-3-{other}.jsonl').write_text('{}\n')
        dest = home / 'dest'
        self.assertEqual(collect_sessions(home, lead, dest), [child])
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         [f'rollout-1-{lead}.jsonl', f'rollout-2-{child}.jsonl'])
        self.assertEqual(collect_sessions(home, 'no-such-thread', home / 'dest2'), [])

    def test_failed_check_and_inputs(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK, inputs={'data.txt': 'd'})
            root.model.push(say('nothing written'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['check']['passed'], call['check']['message']), (False, '缺少 answer.json'))
            self.assertFalse((root.layout.work / 'in' / 'T1').exists()); self.assertIsNone(call['version_after'])

    def test_manual_edit_becomes_an_owner_version(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'AGENTS.md').write_text('edited by hand\n')
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual(call['version_before'], 'V0002')
            self.assertEqual(root.store.log()[0]['author'], 'owner (manual)')

    def test_refusals_start_no_call(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / '.codex').mkdir()
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            (root.layout.work / '.codex').rmdir(); (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 0\n')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            self.assertEqual(root.records.calls(), []); self.assertEqual(root.model.requests, [])

    def test_settings_problems_are_recorded_not_fatal(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'codex').mkdir()
            (root.layout.work / 'codex' / 'settings.toml').write_text('sandbox_mode = "x"\n')
            root.model.push(say('ok'))
            self.assertTrue(any('sandbox_mode' in p for p in run_task(root.layout, 'T1')['settings_problems']))

    def test_timed_out_run_has_unknown_usage(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.owner / 'limits.toml').write_text('[lab]\nseconds_per_run = 3\n')
            root.model.push(slow(30, say('late')))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['usage']), ('timed_out', None))

    def test_trial_leaves_main_and_work_alone(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); root.store.checkout('V0001', root.layout.candidate)
            (root.layout.candidate / 'AGENTS.md').write_text('candidate\n')
            commit = root.store.snapshot(root.layout.candidate, 'M0001', 't')
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), say('done'))
            call = run_trial(root.layout, commit=commit, round_id='M0001', task_id='T1')
            self.assertEqual((call['purpose'], call['cost_class'], call['round'], call['tree']),
                             ('trial', 'meta', 'M0001', root.store.tree_of(commit)))
            self.assertEqual((root.store.head(), (root.layout.work / 'AGENTS.md').read_text()), ('V0001', 'hello\n'))
            self.assertEqual((root.layout.trial / 'AGENTS.md').read_text(), 'candidate\n')
            self.assertTrue(call['check']['passed'])
            root.model.push(say('hi'))
            probe = run_trial(root.layout, commit=commit, round_id='M0001', probe='说一声你好')
            self.assertEqual((probe['task'], probe['check']), (None, None))


@needs_codex
class MoreLabRuns(unittest.TestCase):
    """Beyond the brief: refusals are noted, a workspace that cannot be versioned is a problem, the control program's
    own folders in the workspace are never followed, and stale results do not count."""

    def index(self, root):
        lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
        return [json.loads(line) for line in lines]

    def test_refusals_are_noted_and_bad_requests_refused_before_any_call(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 0\n')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            notes = [line for line in self.index(root) if line['kind'] == 'refusal']
            self.assertEqual((len(notes), notes[0]['task'], notes[0]['purpose']), (1, 'T1', 'task'))
            self.assertIn('day.calls', notes[0]['reason'])
            commit = root.store.resolve('V0001')
            for bad in ({'task_id': 'T1', 'probe': 'x'}, {}, {'probe': ''}, {'task_id': '../T1'}):
                with self.assertRaises(ValueError, msg=bad):
                    run_trial(root.layout, commit=commit, round_id='M0001', **bad)
            with self.assertRaises(ValueError): run_trial(root.layout, commit='HEAD', round_id='M0001', probe='x')
            with self.assertRaises(ValueError): run_task(root.layout, 'T2')
            self.assertEqual(root.records.calls(), []); self.assertEqual(root.model.requests, [])

    def test_a_workspace_that_cannot_be_versioned_is_a_problem_not_a_crash(self):
        with Root() as root:
            root.add_task('T1'); root.model.push(add_file('notes/n.md', 'x'), say('ok'))
            original = VersionStore.commit

            def commit(store, folder, author, message):
                if author.startswith('lab '):
                    raise ValueError('文件夹里有 .git，不能存为版本：repo/.git')
                return original(store, folder, author, message)

            with mock.patch.object(VersionStore, 'commit', commit):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_after'], root.store.head()), ('finished', None, 'V0001'))
            self.assertTrue(any('.git' in problem for problem in call['problems']))
            self.assertEqual(root.records.load_call(call['id'])['status'], 'finished')

    def test_links_in_the_control_programs_folders_are_never_followed(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK, inputs={'data.txt': 'd'})
            elsewhere = root.layout.root / 'elsewhere'; (elsewhere / 'T1').mkdir(parents=True)
            (elsewhere / 'T1' / 'keep.txt').write_text('keep')
            os.symlink(elsewhere, root.layout.work / 'in')
            (root.layout.work / 'out' / 'T1').mkdir(parents=True)
            (root.layout.work / 'out' / 'T1' / 'answer.json').write_text('{"x": 3, "y": -1}')   # stale: not this run's
            root.model.push(say('nothing written'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((elsewhere / 'T1' / 'keep.txt').read_text(), 'keep')
            self.assertFalse(os.path.lexists(root.layout.work / 'in'))
            self.assertFalse(call['check']['passed'])
            self.assertTrue(any('out/T1' in problem for problem in call['problems']))

    def test_a_linked_tmp_is_replaced_by_a_real_folder(self):
        with Root() as root:
            root.add_task('T1'); outside = root.layout.root / 'outside'; outside.mkdir()
            (outside / 'x').write_text('x'); os.symlink(outside, root.layout.work / '.tmp'); root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], os.listdir(outside)), ('finished', ['x']))
            self.assertTrue((root.layout.work / '.tmp').is_dir())
            self.assertFalse((root.layout.work / '.tmp').is_symlink())
            self.assertTrue(any('.tmp' in problem for problem in call['problems']))

    def test_a_nested_git_never_blocks_a_run(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'sub' / '.git').mkdir(parents=True)
            (root.layout.work / 'sub' / '.git' / 'config').write_text('[core]\n')
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            self.assertEqual(root.store.log()[0]['author'], 'owner (manual)')
            self.assertIn('b/sub/.git.rsi-kept/config', root.store.diff('V0001', 'V0002'))
            self.assertTrue(any('sub/.git' in line and 'sub/.git.rsi-kept' in line for line in call['problems']))
            root.model.push(say('again'))
            self.assertEqual(run_task(root.layout, 'T1')['status'], 'finished')

    def test_an_interrupted_run_is_finished_as_interrupted(self):
        with Root() as root:
            root.add_task('T1', inputs={'data.txt': 'd'})
            with mock.patch('rsi.labrun.run_codex', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): run_task(root.layout, 'T1')
            root.add_task('T2', check=PASS_CHECK, inputs={'data.txt': 'd'}); root.model.push(say('ok'))
            with mock.patch('rsi.labrun.run_check', side_effect=KeyboardInterrupt):     # while collecting
                with self.assertRaises(KeyboardInterrupt): run_task(root.layout, 'T2')
            self.assertEqual([(call['status'], call['usage']) for call in root.records.calls()],
                             [('interrupted', None), ('interrupted', None)])
            self.assertFalse(os.path.lexists(root.layout.work / 'in'))

    def test_probe_results_are_collected(self):
        with Root() as root:
            commit = root.store.resolve('V0001')
            root.model.push(add_file('out/probe/hello.txt', '你好'), say('done'))
            call = run_trial(root.layout, commit=commit, round_id='M0001', probe='写一个文件')
            self.assertEqual((call['status'], call['probe'], call['task']), ('finished', '写一个文件', None))
            self.assertTrue((root.records.call_dir(call['id']) / 'out' / 'hello.txt').is_file())
            self.assertIn('out/probe/', (root.records.call_dir(call['id']) / 'prompt.txt').read_text())
            self.assertFalse(os.path.lexists(root.layout.trial / 'out' / 'probe'))
            self.assertEqual(call['tree_after'], call['tree'])                 # out/ is never part of a tree


if __name__ == '__main__':
    unittest.main()
