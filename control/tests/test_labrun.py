import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import workspace
from rsi.codexrun import codex_version
from rsi.labrun import run_task, run_trial
from rsi.mockmodel import add_file, say, slow, with_usage
from rsi.paths import Refused
from rsi.sessions import collect_sessions
from rsi.versions import VersionStore
from tests.support import PASS_CHECK, Root, needs_codex
from tests.test_workspace import DEPTH, few_open_files


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
            (root.layout.work / 'left.txt').write_text('what the interrupted run left')
            with mock.patch('rsi.labrun.run_check', side_effect=KeyboardInterrupt):     # while collecting
                with self.assertRaises(KeyboardInterrupt): run_task(root.layout, 'T2')
            first, second = root.records.calls()
            self.assertEqual(root.store.log()[0]['author'], f'owner (manual) with lab {first["id"]}')
            self.assertEqual((first['status'], first['usage']), ('interrupted', None))
            # Codex had ended: the call keeps its status and usage, and says what was not done.
            self.assertEqual((second['status'], second['usage']['input_tokens']), ('finished', 0))
            self.assertTrue(any('KeyboardInterrupt' in line for line in second['problems']))
            self.assertFalse(os.path.lexists(root.layout.work / 'in'))

    def test_unusable_control_folders_never_block_a_run(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK, inputs={'data.txt': 'd'}); work = root.layout.work
            for name in ('.tmp', 'out/T1', 'in/T1'):
                (work / name).mkdir(parents=True)
            for name in ('out/T1', 'in/T1', '.tmp', 'out', 'in'):
                (work / name).chmod(0)
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), say('done'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['check']['passed']), ('finished', True))
            self.assertTrue(os.access(work / '.tmp', os.R_OK | os.W_OK | os.X_OK))

    def test_a_deep_tree_never_blocks_a_run_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            root.add_task('T1'); work = root.layout.work
            (work / ('a/' * DEPTH) / '.git').mkdir(parents=True); (work / 'a' / 'keep.txt').write_text('k')
            (work / 'out' / 'T1' / ('b/' * DEPTH)).mkdir(parents=True)                # stale and deep
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            self.assertTrue(any('library/moved/manual/a' in line for line in call['problems']))
            self.assertFalse(os.path.lexists(work / 'out'))

    def test_git_failing_before_and_after_the_call_quarantines_and_keeps_status_and_usage(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'mine.txt').write_text('an owner edit')
            root.model.push(add_file('notes/n.md', 'x'), with_usage(say('ok'), 5, 2))
            original, failures = VersionStore.commit, []

            def commit(store, folder, author, message):          # the first two commits time out
                if len(failures) < 2:
                    failures.append(author)
                    raise subprocess.TimeoutExpired(['git', 'add'], 600)
                return original(store, folder, author, message)

            with mock.patch.object(VersionStore, 'commit', commit):
                call = run_task(root.layout, 'T1')
            self.assertEqual(failures, ['owner (manual)', f'lab {call["id"]}'])
            self.assertEqual((call['status'], call['usage']['input_tokens'], call['usage']['output_tokens']),
                             ('finished', 5, 2))
            self.assertEqual((call['version_after'], call['commit_failed'], call['workspace_restored']),
                             (None, True, True))
            self.assertTrue(any('TimeoutExpired' in line for line in call['problems']))
            self.assertTrue(any('quarantine/work-1' in line for line in call['problems']))          # before the call
            self.assertTrue(any(f'quarantine/work-{call["id"]}-1' in line for line in call['problems']))   # after
            self.assertEqual(root.records.load_call(call['id'])['status'], 'finished')
            self.assertEqual(root.store.tree_of_dir(root.layout.work), root.store.tree_of('V0001'))
            self.assertEqual((root.layout.root / 'quarantine' / 'work-1' / 'mine.txt').read_text(), 'an owner edit')
            self.assertEqual(len([line for line in self.index(root) if line['kind'] == 'quarantine']), 2)
            root.model.push(say('again'))
            second = run_task(root.layout, 'T1')
            self.assertEqual((second['status'], second['version_before']), ('finished', 'V0001'))
            self.assertEqual([entry['author'] for entry in root.store.log()], ['owner'])     # no owner version

    def test_a_workspace_replaced_by_a_link_is_quarantined_and_the_run_goes_on(self):
        with Root() as root:
            root.add_task('T1'); outside = root.layout.root / 'outside'; outside.mkdir()
            (outside / 'x').write_text('x')
            os.rename(root.layout.work, root.layout.root / 'work-old'); os.symlink(outside, root.layout.work)
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            self.assertEqual(os.listdir(outside), ['x'])
            self.assertTrue((root.layout.root / 'quarantine' / 'work-1').is_symlink())
            self.assertFalse(root.layout.work.is_symlink())
            self.assertEqual((root.layout.work / 'AGENTS.md').read_text(), 'hello\n')
            notes = [line for line in self.index(root) if line['kind'] == 'quarantine']
            self.assertEqual((len(notes), notes[0]['moved_to'], notes[0]['restored']),
                             (1, 'quarantine/work-1', 'V0001'))
            self.assertTrue(any('quarantine/work-1' in line for line in call['problems']))

    def test_an_unreadable_workspace_is_quarantined_and_the_run_goes_on(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'mine.txt').write_text('m'); root.layout.work.chmod(0)
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            self.assertEqual((root.layout.root / 'quarantine' / 'work-1' / 'mine.txt').read_text(), 'm')
            self.assertTrue(any(line['kind'] == 'quarantine' for line in self.index(root)))
            self.assertTrue(any('quarantine/work-1' in line for line in call['problems']))

    def test_a_trial_folder_replaced_by_a_link_is_quarantined(self):
        with Root() as root:
            outside = root.layout.root / 'outside'; outside.mkdir(); (outside / 'x').write_text('x')
            root.layout.trial.rmdir(); os.symlink(outside, root.layout.trial); root.model.push(say('hi'))
            call = run_trial(root.layout, commit=root.store.resolve('V0001'), round_id='M0001', probe='说一声你好')
            self.assertEqual(call['status'], 'finished'); self.assertEqual(os.listdir(outside), ['x'])
            self.assertTrue((root.layout.root / 'quarantine' / 'trial-1').is_symlink())
            self.assertTrue(any('quarantine/trial-1' in line for line in call['problems']))

    def test_an_unusable_library_never_lets_a_big_file_reach_git(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'big.bin').write_text('x' * 20); root.layout.library.chmod(0)
            root.model.push(say('ok'))
            with mock.patch.object(workspace, 'BIG_FILE', 10):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            self.assertTrue((root.layout.library / 'moved' / 'manual' / 'big.bin').is_file())

    def test_unreadable_and_oversize_files_never_block_a_run(self):
        with Root() as root:
            root.add_task('T1'); work = root.layout.work
            (work / 'notes').mkdir(); (work / 'notes' / 'x.txt').write_text('x'); (work / 'notes' / 'x.txt').chmod(0)
            (work / 'data').mkdir()
            with open(work / 'data' / 'huge.bin', 'wb') as stream:
                stream.truncate(workspace.BIG_FILE + 1)                         # sparse: nothing allocated
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            self.assertTrue((root.layout.library / 'moved' / 'manual' / 'data' / 'huge.bin').is_file())
            self.assertTrue(any('data/huge.bin' in line for line in call['problems']))
            self.assertIn('b/notes/x.txt', root.store.diff('V0001', 'V0002'))

    def test_a_trial_starts_from_an_empty_trial_folder(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); trial = root.layout.trial
            (trial / '.tmp').mkdir(); (trial / '.tmp' / 'junk').write_text('j'); (trial / '.tmp').chmod(0)
            (trial / 'out' / 'T1').mkdir(parents=True)
            (trial / 'out' / 'T1' / 'answer.json').write_text('{"x": 3, "y": -1}')     # an earlier trial's answer
            root.model.push(say('nothing written'))
            call = run_trial(root.layout, commit=root.store.resolve('V0001'), round_id='M0001', task_id='T1')
            self.assertEqual((call['status'], call['check']['passed']), ('finished', False))
            self.assertEqual(os.listdir(trial / '.tmp'), [])

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
