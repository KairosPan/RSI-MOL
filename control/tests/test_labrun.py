import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from rsi import labrun, workspace
from rsi.codexrun import codex_version
from rsi.init import recover
from rsi.labrun import run_task, run_trial
from rsi.mockmodel import add_file, say, slow, with_usage
from rsi.paths import Refused
from rsi.sessions import collect_sessions
from rsi.versions import VersionStore
from tests.support import PASS_CHECK, Root, needs_codex, plant_programs
from tests.test_workspace import DEPTH, deny, few_open_files


def index(root: Root, kind: str) -> list[dict]:
    lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
    return [line for line in map(json.loads, lines) if line['kind'] == kind]


def leftovers(root: Root) -> list[str]:
    """What a run must never leave behind: staged folders, sealed entries in the root, indexes of the store."""
    staged = os.listdir(root.layout.staging) if root.layout.staging.exists() else []
    sealed = [name for name in os.listdir(root.layout.root) if name.startswith(workspace.SEALED)]
    return staged + sealed + os.listdir(root.store.git_dir / 'indexes')


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
            self.assertEqual(call['check'], {'passed': True, 'message': 'passed', 'exit_code': 0})
            run_dir = root.records.call_dir(call['id'])
            self.assertTrue((run_dir / 'out' / 'answer.json').is_file())
            self.assertTrue((run_dir / 'diff.patch').is_file())
            self.assertFalse((root.layout.work / 'out' / 'T1').exists())
            self.assertTrue((root.layout.work / 'notes' / 'n.md').is_file())
            self.assertIn('## Goal', (run_dir / 'prompt.txt').read_text())
            self.assertEqual(call['codex_version'], codex_version())
            copies = [p.name for p in (run_dir / 'sessions').iterdir()]
            self.assertEqual((len(copies), copies[0].endswith(call['thread_id'] + '.jsonl'), call['subagent_threads']),
                             (1, True, []))
            self.assertEqual((call['problems'], leftovers(root)), ([], []))
            self.assertFalse(root.layout.sealed.exists())

    def test_programs_planted_in_env_bin_are_never_run(self):
        # The owner's shell may have <root>/env/bin first on PATH (an activated venv), and the Lab writes env/.
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            marker = root.path / 'planted-ran'
            plant_programs(root.layout.env / 'bin', marker)
            os.environ['PATH'] = f'{root.layout.env / "bin"}{os.pathsep}{os.environ["PATH"]}'     # Root restores it
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), add_file('notes/n.md', 'n'),
                            say('done'))
            call = run_task(root.layout, 'T1')
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else '')
            self.assertEqual((call['status'], call['version_after'], call['check']['passed']),
                             ('finished', 'V0002', True))

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
            self.assertEqual((call['check']['passed'], call['check']['message']), (False, 'answer.json is missing'))
            self.assertFalse((root.layout.work / 'in' / 'T1').exists()); self.assertIsNone(call['version_after'])
            self.assertIn('in/T1/', (root.records.call_dir(call['id']) / 'prompt.txt').read_text())

    def test_manual_edit_becomes_a_version(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'AGENTS.md').write_text('edited by hand\n')
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual(call['version_before'], 'V0002')
            self.assertEqual(root.store.log()[0]['author'], 'manual or leftover')     # it cannot tell which

    def test_task_before_install_preserves_legacy_memory_link_without_following_it(self):
        with Root() as root:
            root.add_task('T1')
            notes = root.layout.owner / 'notes'
            notes.write_text('Only the owner can read this.')
            (root.layout.work / 'memory').symlink_to(notes)
            root.model.push(say('done'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            kept, = root.layout.sealed.glob('*/legacy-work/memory')
            self.assertEqual(kept.readlink(), notes)
            self.assertEqual(notes.read_text(), 'Only the owner can read this.')
            self.assertTrue(any(str(kept.relative_to(root.path)) in line for line in call['problems']))
            self.assertTrue((root.layout.work / 'memory/status.json').is_file())

    def test_refusals_start_no_call(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / '.codex').mkdir()
            (root.layout.work / '.codex' / 'config.toml').write_text('x = 1\n')
            self.assertEqual(root.store.commit(root.layout.work, 'owner', 'with .codex'), 'V0002')
            with self.assertRaisesRegex(Refused, '.codex'): run_task(root.layout, 'T1')     # head holds .codex
            self.assertFalse(os.path.lexists(root.layout.work))          # head cannot be staged back either ...
            self.assertIn('there is no usable work/ now', index(root, 'refusal')[0]['reason'])     # ... and says so
            (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 0\n')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            self.assertEqual(root.records.calls(), []); self.assertEqual(root.model.requests, [])
            self.assertEqual(len(index(root, 'refusal')), 2)

    def test_the_owners_lab_config_follows_the_version_put_back_into_work(self):
        with Root() as root:
            root.add_task('T1')
            root.model.push(add_file('codex/settings.toml', 'network = true\n'), say('done'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['version_after'], call['problems']), ('V0002', []))
            config = tomllib.loads((root.layout.lab_home / 'config.toml').read_text())
            self.assertEqual((config['default_permissions'], config['approval_policy']), ('lab_net', 'never'))

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

    def test_trial_is_staged_from_its_commit_and_leaves_main_and_work_alone(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); root.store.checkout('V0001', root.layout.candidate)
            (root.layout.candidate / 'AGENTS.md').write_text('candidate\n')
            commit = root.store.snapshot(root.layout.candidate, 'M0001', 't')
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), say('done'))
            call = run_trial(root.layout, commit=commit, round_id='M0001', task_id='T1')
            self.assertEqual((call['purpose'], call['cost_class'], call['round'], call['tree']),
                             ('trial', 'meta', 'M0001', root.store.tree_of(commit)))
            self.assertEqual((root.store.head(), (root.layout.work / 'AGENTS.md').read_text()), ('V0001', 'hello\n'))
            self.assertEqual(call['tree_after'], root.store.tree_of(commit))      # staged from the commit, not head
            self.assertNotEqual(call['tree_after'], root.store.tree_of('V0001'))
            self.assertTrue(call['check']['passed']); self.assertFalse(os.path.lexists(root.layout.trial))
            root.model.push(say('hi'))
            probe = run_trial(root.layout, commit=commit, round_id='M0001', probe='say hello')
            self.assertEqual((probe['task'], probe['check']), (None, None))
            self.assertEqual(([entry['version'] for entry in root.store.log()], leftovers(root)), (['V0001'], []))


@needs_codex
class MoreLabRuns(unittest.TestCase):
    """Beyond the brief: refusals are noted, what cannot be versioned is a problem, links in the workspace are never
    followed, and stale results do not count."""

    def test_refusals_are_noted_and_bad_requests_refused_before_any_call(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 0\n')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            notes = index(root, 'refusal')
            self.assertEqual((len(notes), notes[0]['task'], notes[0]['purpose']), (1, 'T1', 'task'))
            self.assertIn('day.calls', notes[0]['reason'])
            commit = root.store.resolve('V0001')
            for bad in ({'task_id': 'T1', 'probe': 'x'}, {}, {'probe': ''}, {'task_id': '../T1'}):
                with self.assertRaises(ValueError, msg=bad):
                    run_trial(root.layout, commit=commit, round_id='M0001', **bad)
            with self.assertRaises(ValueError): run_trial(root.layout, commit='HEAD', round_id='M0001', probe='x')
            with self.assertRaises(ValueError): run_task(root.layout, 'T2')
            self.assertEqual(root.records.calls(), []); self.assertEqual(root.model.requests, [])
            self.assertEqual((root.layout.work / 'AGENTS.md').read_text(), 'hello\n')    # refused before sealing

    def test_a_workspace_that_cannot_be_versioned_is_a_problem_not_a_crash(self):
        with Root() as root:
            root.add_task('T1'); root.model.push(add_file('notes/n.md', 'x'), say('ok'))
            original = VersionStore.commit

            def commit(store, folder, author, message):
                if author.startswith('lab '):
                    raise ValueError('The folder holds .git, so it cannot be saved as a version: repo/.git')
                return original(store, folder, author, message)

            with mock.patch.object(VersionStore, 'commit', commit):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_after'], root.store.head()), ('finished', None, 'V0001'))
            self.assertTrue(any('.git' in problem for problem in call['problems']))
            self.assertEqual(root.records.load_call(call['id'])['status'], 'finished')
            self.assertTrue((root.layout.sealed / call['id'] / 'work' / 'notes' / 'n.md').is_file())

    def test_links_in_the_workspace_are_never_followed(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK, inputs={'data.txt': 'd'})
            elsewhere = root.layout.root / 'elsewhere'; (elsewhere / 'T1').mkdir(parents=True)
            (elsewhere / 'T1' / 'keep.txt').write_text('keep')
            os.symlink(elsewhere, root.layout.work / 'in'); os.symlink(elsewhere, root.layout.work / 'linked')
            (root.layout.work / 'out' / 'T1').mkdir(parents=True)
            (root.layout.work / 'out' / 'T1' / 'answer.json').write_text('{"x": 3, "y": -1}')   # stale: not this run's
            root.model.push(say('nothing written'))
            call = run_task(root.layout, 'T1')
            self.assertEqual(sorted(os.listdir(elsewhere)), ['T1']); self.assertEqual(os.listdir(elsewhere / 'T1'),
                                                                                     ['keep.txt'])
            self.assertFalse(os.path.lexists(root.layout.work / 'in'))
            self.assertTrue((root.layout.work / 'linked').is_symlink())          # versioned as a link
            self.assertFalse(call['check']['passed'])

    def test_a_linked_tmp_is_replaced_by_a_real_folder(self):
        with Root() as root:
            root.add_task('T1'); outside = root.layout.root / 'outside'; outside.mkdir()
            (outside / 'x').write_text('x'); os.symlink(outside, root.layout.work / '.tmp'); root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], os.listdir(outside)), ('finished', ['x']))
            self.assertTrue((root.layout.work / '.tmp').is_dir())
            self.assertFalse((root.layout.work / '.tmp').is_symlink())

    def test_a_nested_git_never_blocks_a_run(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'sub' / '.git').mkdir(parents=True)
            (root.layout.work / 'sub' / '.git' / 'config').write_text('[core]\n')
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            self.assertEqual(root.store.log()[0]['author'], 'manual or leftover')
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
            self.assertEqual(root.store.log()[0]['author'], 'manual or leftover')
            self.assertIn(first['id'], root.store.log()[0]['message'])
            self.assertEqual((first['status'], first['usage']), ('interrupted', None))
            # Codex had ended: the call keeps its status and usage, and says what was not done.
            self.assertEqual((second['status'], second['usage']['input_tokens']), ('finished', 0))
            self.assertTrue(any('KeyboardInterrupt' in line for line in second['problems']))
            # The collection left its sealed tree in the root and no work/: nothing runs until rsi recover has kept
            # the tree under sealed/ and put main's head back.
            with self.assertRaisesRegex(Refused, 'rsi recover'):
                run_task(root.layout, 'T1')
            recover(root.layout)
            root.model.push(say('ok'))
            self.assertEqual(run_task(root.layout, 'T1')['status'], 'finished')
            self.assertEqual((root.layout.work / 'left.txt').read_text(), 'what the interrupted run left')

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
            self.assertEqual(leftovers(root), [])

    def test_a_deep_tree_never_blocks_a_run_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            root.add_task('T1'); work = root.layout.work
            (work / ('a/' * DEPTH) / '.git').mkdir(parents=True); (work / 'a' / 'keep.txt').write_text('k')
            (work / 'out' / 'T1' / ('b/' * DEPTH)).mkdir(parents=True)                # stale and deep
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            self.assertTrue(any('/left-out/1-a' in line for line in call['problems']))
            self.assertEqual(os.listdir(work / 'out'), [])

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
            [label] = os.listdir(root.layout.sealed)
            self.assertTrue((root.layout.sealed / label / 'left-out' / '1-huge.bin').is_file())
            self.assertTrue(any('data/huge.bin' in line for line in call['problems']))
            self.assertIn('b/notes/x.txt', root.store.diff('V0001', 'V0002'))
            self.assertFalse(os.path.lexists(work / 'data'))                    # git keeps no empty folder

    def test_a_trial_starts_from_a_fresh_trial_folder(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); trial = root.layout.trial
            (trial / '.tmp').mkdir(); (trial / '.tmp' / 'junk').write_text('j'); (trial / '.tmp').chmod(0)
            (trial / 'out' / 'T1').mkdir(parents=True)
            (trial / 'out' / 'T1' / 'answer.json').write_text('{"x": 3, "y": -1}')     # an earlier trial's answer
            root.model.push(say('nothing written'))
            call = run_trial(root.layout, commit=root.store.resolve('V0001'), round_id='M0001', task_id='T1')
            self.assertEqual((call['status'], call['check']['passed']), ('finished', False))
            self.assertEqual((leftovers(root), root.layout.sealed.exists()), ([], False))

    def test_probe_results_are_collected(self):
        with Root() as root:
            commit = root.store.resolve('V0001')
            root.model.push(add_file('out/probe/hello.txt', 'hello'), say('done'))
            call = run_trial(root.layout, commit=commit, round_id='M0001', probe='write a file')
            self.assertEqual((call['status'], call['probe'], call['task']), ('finished', 'write a file', None))
            self.assertTrue((root.records.call_dir(call['id']) / 'out' / 'hello.txt').is_file())
            self.assertIn('out/probe/', (root.records.call_dir(call['id']) / 'prompt.txt').read_text())
            self.assertFalse(os.path.lexists(root.layout.trial))
            self.assertEqual(call['tree_after'], call['tree'])                 # out/ is never part of a tree


@needs_codex
class SealFirst(unittest.TestCase):
    """Fix round 3: a workspace is sealed before it is handled, and the next one is built in staging/."""

    def test_a_workspace_replaced_by_a_link_is_sealed_and_kept(self):
        with Root() as root:
            root.add_task('T1'); outside = root.layout.root / 'outside'; outside.mkdir()
            (outside / 'x').write_text('x')
            os.rename(root.layout.work, root.layout.root / 'work-old'); os.symlink(outside, root.layout.work)
            root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            self.assertEqual(os.listdir(outside), ['x'])
            [note] = index(root, 'sealed')
            self.assertTrue(note['path'].startswith('sealed/')); self.assertEqual(note['workspace'], 'work')
            self.assertTrue((root.layout.root / note['path']).is_symlink())
            self.assertTrue(any(note['path'] in line for line in call['problems']))
            self.assertFalse(root.layout.work.is_symlink())
            self.assertEqual((root.layout.work / 'AGENTS.md').read_text(), 'hello\n')

    def test_a_locked_workspace_never_blocks_a_run(self):
        with Root() as root:
            root.add_task('T1')
            cases = ['mode 000', 'mode 600'] + (['deny-search ACL'] if sys.platform == 'darwin' else [])
            for number, case in enumerate(cases, start=2):
                (root.layout.work / 'mine.txt').write_text(case)
                if case == 'mode 000':
                    root.layout.work.chmod(0)
                elif case == 'mode 600':
                    root.layout.work.chmod(0o600)
                else:
                    deny(root.layout.work, 'list,search')
                root.model.push(say('ok'))
                call = run_task(root.layout, 'T1')
                self.assertEqual((call['status'], call['version_before']), ('finished', f'V{number:04d}'), case)
                self.assertEqual((root.layout.work / 'mine.txt').read_text(), case)
            self.assertEqual((index(root, 'sealed'), leftovers(root)), ([], []))

    def test_a_link_planted_between_the_seal_and_the_rename_in_replaces_nothing(self):
        with Root() as root:
            root.add_task('T1', inputs={'data.txt': 'd'}); outside = root.layout.root / 'outside'; outside.mkdir()
            (outside / 'x').write_text('x')
            original = labrun._rename_in

            def plant_then_rename(staged, root_fd, name):        # a leftover process, just after the seal
                os.symlink(outside, root.layout.root / name)
                return original(staged, root_fd, name)

            with mock.patch.object(labrun, '_rename_in', plant_then_rename):     # the restore meets one too
                with self.assertRaises(Refused): run_task(root.layout, 'T1')
            self.assertEqual((os.listdir(outside), (outside / 'x').read_text()), (['x'], 'x'))
            self.assertEqual((root.records.calls(), root.model.requests, leftovers(root)), ([], [], []))
            [refusal] = index(root, 'refusal'); self.assertIn('there is no usable work/ now', refusal['reason'])
            self.assertTrue(root.layout.work.is_symlink())
            [note] = index(root, 'sealed'); self.assertTrue((root.layout.root / note['path']).is_symlink())
            root.model.push(say('ok'))                                 # the next run seals the link and keeps it
            self.assertEqual(run_task(root.layout, 'T1')['status'], 'finished')
            note = index(root, 'sealed')[-1]; self.assertTrue((root.layout.root / note['path']).is_symlink())
            self.assertEqual(os.listdir(outside), ['x'])
            original_stage, planted = labrun._stage, []

            def stage_then_plant(*args, **kwargs):                     # work/ appears again after it was sealed
                staged = original_stage(*args, **kwargs)
                if not planted:
                    planted.append(True); root.layout.work.mkdir(); (root.layout.work / 'late.txt').write_text('l')
                return staged

            root.model.push(say('ok'))
            with mock.patch.object(labrun, '_stage', stage_then_plant):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            late = index(root, 'sealed')[-1]
            self.assertEqual(((root.layout.root / late['path'] / 'late.txt').read_text(), len(index(root, 'sealed'))),
                             ('l', 3))
            self.assertFalse((root.layout.work / 'late.txt').exists())

    def test_codex_in_a_sealed_tree_is_kept_and_never_versioned(self):     # in a version: test_refusals_...
        with Root() as root:
            root.add_task('T1'); (root.layout.work / '.codex').mkdir()
            (root.layout.work / '.codex' / 'config.toml').write_text('x = 1\n'); root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')                          # found before the run: kept, not versioned
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0001'))
            self.assertTrue(any('.codex' in line for line in call['problems']))
            self.assertFalse(os.path.lexists(root.layout.work / '.codex'))
            original = labrun.run_codex

            def run_then_plant(**options):                              # appears after the call
                result = original(**options)
                (root.layout.work / '.codex').mkdir(); (root.layout.work / '.codex' / 'config.toml').write_text('y')
                return result

            root.model.push(add_file('notes/n.md', 'x'), say('ok'))
            with mock.patch.object(labrun, 'run_codex', run_then_plant):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_after'], root.store.head()), ('finished', None, 'V0001'))
            self.assertTrue(any('.codex' in line for line in call['problems']))
            notes = index(root, 'sealed'); self.assertEqual((len(notes), notes[-1]['call']), (2, call['id']))
            self.assertTrue((root.layout.root / notes[-1]['path'] / '.codex' / 'config.toml').is_file())

    def test_a_failed_commit_keeps_the_tree_and_work_is_main_head_again(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'mine.txt').write_text('an owner edit')
            root.model.push(add_file('notes/n.md', 'x'), with_usage(say('ok'), 5, 2))
            original, failures = VersionStore.commit, []

            def commit(store, folder, author, message):          # the manual commit and the Lab's both time out
                if len(failures) < 2:
                    failures.append(author)
                    raise subprocess.TimeoutExpired(['git', 'add'], 600)
                return original(store, folder, author, message)

            with mock.patch.object(VersionStore, 'commit', commit):
                call = run_task(root.layout, 'T1')
            self.assertEqual(failures, ['manual or leftover', f'lab {call["id"]}'])
            self.assertEqual((call['status'], call['usage']['input_tokens'], call['usage']['output_tokens']),
                             ('finished', 5, 2))
            self.assertEqual((call['version_before'], call['version_after'], call['commit_failed']),
                             ('V0001', None, True))
            self.assertEqual(sum('TimeoutExpired' in line for line in call['problems']), 2)
            before, after = index(root, 'sealed')
            self.assertEqual((root.layout.root / before['path'] / 'mine.txt').read_text(), 'an owner edit')
            self.assertEqual((after['call'], after['path']), (call['id'], f'sealed/{call["id"]}/work'))
            self.assertTrue((root.layout.root / after['path'] / 'notes' / 'n.md').is_file())
            self.assertEqual(sorted(os.listdir(root.layout.work)), ['.tmp', 'AGENTS.md', 'memory', 'out'])
            self.assertEqual(json.loads((root.layout.work / 'memory/status.json').read_bytes())['condition'], 'live')
            root.model.push(say('again'))
            second = run_task(root.layout, 'T1')
            self.assertEqual((second['status'], second['version_before']), ('finished', 'V0001'))
            self.assertEqual([entry['author'] for entry in root.store.log()], ['owner'])     # nothing as manual

    def test_a_store_that_cannot_take_a_version_refuses_before_any_call(self):
        with Root() as root:
            root.add_task('T1'); (root.store.git_dir / 'refs' / 'heads' / 'main.lock').touch()
            with self.assertRaisesRegex(Refused, 'versions/lab.git'): run_task(root.layout, 'T1')
            with self.assertRaises(Refused):
                run_trial(root.layout, commit=root.store.resolve('V0001'), round_id='M0001', probe='x')
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))
            self.assertEqual([note['purpose'] for note in index(root, 'refusal')], ['task', 'trial'])
            self.assertEqual(os.listdir(root.layout.work), ['AGENTS.md'])           # not even sealed

    def test_nothing_grows_over_ten_runs(self):
        with Root() as root:
            root.add_task('T1'); commit = root.store.resolve('V0001')
            for number in range(5):
                root.model.push(add_file(f'notes/{number}.md', 'x'), say('ok'))
                self.assertIsNotNone(run_task(root.layout, 'T1')['version_after'])
                root.model.push(say('hi'))
                self.assertEqual(run_trial(root.layout, commit=commit, round_id='M0001', probe='hello')['status'],
                                 'finished')
            self.assertEqual((os.listdir(root.layout.staging), leftovers(root)), ([], []))
            self.assertFalse(root.layout.sealed.exists()); self.assertEqual(root.store.head(), 'V0006')

    def test_a_refusal_after_the_seal_puts_main_head_back_into_work(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'mine.txt').write_text('an owner edit')
            original, calls = labrun._stage, []

            def fail_once(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    raise RuntimeError('git checkout failed (128)')
                return original(*args, **kwargs)

            with mock.patch.object(labrun, '_stage', fail_once):
                with self.assertRaisesRegex(Refused, 'git checkout failed'): run_task(root.layout, 'T1')
            self.assertEqual((root.store.head(), (root.layout.work / 'mine.txt').read_text()),
                             ('V0002', 'an owner edit'))
            self.assertEqual(sorted(os.listdir(root.layout.work)), ['.tmp', 'AGENTS.md', 'memory', 'mine.txt', 'out'])
            self.assertEqual((root.records.calls(), root.model.requests, leftovers(root)), ([], [], []))

    def test_an_oversize_file_with_a_long_chinese_name_never_reaches_a_version(self):
        with Root() as root:
            root.add_task('T1'); name = '数' * 70 + '.bin'                    # a cut at 200 bytes splits a 数
            (root.layout.work / name).write_text('x' * 20); (root.layout.work / 'mine.txt').write_text('m')
            root.model.push(say('ok'))
            with mock.patch.object(workspace, 'BIG_FILE', 10):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['version_before']), ('finished', 'V0002'))
            diff = root.store.diff('V0001', 'V0002')
            self.assertIn('b/mine.txt', diff); self.assertNotIn('数', diff)
            [note] = index(root, 'sealed')
            [label] = os.listdir(root.layout.sealed)
            self.assertEqual(os.listdir(root.layout.sealed / label / 'left-out'), ['1-' + '数' * 66])

    def test_what_cannot_be_moved_out_blocks_the_version(self):
        with Root() as root:
            root.add_task('T1')
            root.model.push(add_file('data/huge.bin', 'x' * 20), add_file('notes/n.md', 'x'),
                            with_usage(say('ok'), 5, 2))
            original = os.rename

            def refuse(source, *args, **options):                    # the oversize file cannot be moved out
                if source == 'huge.bin':
                    raise OSError(92, 'Illegal byte sequence')
                return original(source, *args, **options)

            with mock.patch.object(workspace, 'BIG_FILE', 10), mock.patch.object(os, 'rename', refuse):
                call = run_task(root.layout, 'T1')
            self.assertEqual((call['status'], call['usage']['input_tokens'], call['usage']['output_tokens']),
                             ('finished', 5, 2))
            self.assertEqual((call['version_after'], call['commit_failed'], root.store.head()), (None, True, 'V0001'))
            self.assertTrue(any('the version was not saved' in line for line in call['problems']))
            [note] = index(root, 'sealed')
            self.assertEqual((note['call'], note['path']), (call['id'], f'sealed/{call["id"]}/work'))
            self.assertTrue(any('the version was not saved' in reason for reason in note['reasons']))
            self.assertTrue((root.layout.root / note['path'] / 'data' / 'huge.bin').is_file())
            self.assertFalse(os.path.lexists(root.layout.work / 'data'))     # work/ is main's head again

    def test_a_trial_folder_replaced_by_a_link_is_removed_not_followed(self):
        with Root() as root:
            outside = root.layout.root / 'outside'; outside.mkdir(); (outside / 'x').write_text('x')
            root.layout.trial.rmdir(); os.symlink(outside, root.layout.trial); root.model.push(say('hi'))
            call = run_trial(root.layout, commit=root.store.resolve('V0001'), round_id='M0001', probe='say hello')
            self.assertEqual((call['status'], os.listdir(outside)), ('finished', ['x']))
            self.assertFalse(os.path.lexists(root.layout.trial)); self.assertEqual(leftovers(root), [])


def write_skill(workspace: Path, folder: str, name: str) -> None:
    path = workspace / 'skills' / folder / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: a skill for the demo {name}\n---\n\nA demo.\n', encoding='utf-8')


def body(request: dict) -> str:
    return json.dumps(request, ensure_ascii=False)


@needs_codex
class Skills(unittest.TestCase):
    """Task 9b: the workspace's skills/ becomes .agents/skills/ wherever a Lab Codex runs, and is never versioned."""

    def test_a_task_run_loads_the_versions_skills_and_the_lab_can_add_one(self):
        with Root() as root:
            root.add_task('T1'); write_skill(root.layout.work, 'demo', 'demo-skill-marker')
            self.assertEqual(root.store.commit(root.layout.work, 'owner', 'a skill'), 'V0002')
            new = '---\nname: new-skill-marker\ndescription: a skill the Lab added itself\n---\n\nA new skill.\n'
            root.model.push(add_file('skills/new/SKILL.md', new), add_file('skills/loose/x.md', 'no SKILL.md'),
                            say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertIn('demo-skill-marker', body(root.model.requests[0]))
            self.assertNotIn('new-skill-marker', body(root.model.requests[0]))
            self.assertEqual((call['status'], call['version_before'], call['version_after']),
                             ('finished', 'V0002', 'V0003'))
            diff = root.store.diff('V0002', 'V0003')
            self.assertIn('b/skills/new/SKILL.md', diff); self.assertNotIn('.agents', diff)
            # The re-staged work/ holds V0003: the skill it drops is told at once, in this call's problems.
            [line] = call['problems']
            self.assertEqual((call['settings_problems'], line.startswith(labrun.RESTAGED)), ([], True))
            self.assertTrue(line.removeprefix(labrun.RESTAGED).startswith('skills/loose: '), line)
            for folder in ('demo', 'new'):
                self.assertTrue((root.layout.work / '.agents' / 'skills' / folder / 'SKILL.md').is_file(), folder)
            first = len(root.model.requests); root.model.push(say('ok'))
            call = run_task(root.layout, 'T1')
            self.assertIn('new-skill-marker', body(root.model.requests[first]))
            self.assertIn('demo-skill-marker', body(root.model.requests[first]))
            self.assertEqual((call['version_before'], call['version_after'], root.store.head()),
                             ('V0003', None, 'V0003'))                      # .agents never makes a version
            self.assertEqual([line.split(': ')[0] for line in call['settings_problems']], ['skills/loose'])
            self.assertEqual(leftovers(root), [])

    def test_an_agents_folder_that_cannot_be_cleared_is_a_noted_refusal(self):
        with Root() as root:
            root.add_task('T1')
            with mock.patch('rsi.skills._clear', side_effect=PermissionError(1, 'Operation not permitted')):
                with self.assertRaisesRegex(Refused, 'Operation not permitted'): run_task(root.layout, 'T1')
            self.assertEqual((root.records.calls(), root.model.requests, leftovers(root)), ([], [], []))
            [refusal] = index(root, 'refusal')
            self.assertIn('Operation not permitted', refusal['reason'])
            self.assertIn('there is no usable work/ now', refusal['reason'])      # the restore met the same failure

    def test_a_trial_loads_the_candidates_skills_not_heads(self):
        with Root() as root:
            write_skill(root.layout.work, 'demo', 'head-skill-marker')
            self.assertEqual(root.store.commit(root.layout.work, 'owner', 'a skill'), 'V0002')
            root.store.checkout('V0002', root.layout.candidate)
            write_skill(root.layout.candidate, 'demo', 'candidate-skill-marker')
            loose = root.layout.candidate / 'skills' / 'loose'                 # no SKILL.md: not a skill
            loose.mkdir(); (loose / 'x.md').write_text('x')
            commit = root.store.snapshot(root.layout.candidate, 'M0001', 't')
            root.model.push(say('hi'))
            call = run_trial(root.layout, commit=commit, round_id='M0001', probe='say hello')
            self.assertEqual(call['status'], 'finished')
            self.assertIn('candidate-skill-marker', body(root.model.requests[0]))
            self.assertNotIn('head-skill-marker', body(root.model.requests[0]))
            self.assertEqual([line.split(': ')[0] for line in call['settings_problems']], ['skills/loose'])
            self.assertEqual((call['tree_after'], call['problems']), (root.store.tree_of(commit), []))
            self.assertFalse(os.path.lexists(root.layout.trial)); self.assertEqual(leftovers(root), [])


if __name__ == '__main__':
    unittest.main()
