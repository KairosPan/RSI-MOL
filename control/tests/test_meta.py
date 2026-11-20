import json
import os
import stat
import tomllib
import unittest
from unittest import mock

from rsi import meta
from rsi.labrun import run_task
from rsi.meta import release_problems, rollback, run_round
from rsi.mockmodel import add_file, say
from rsi.paths import Refused
from tests.support import PASS_CHECK, Root, decision, needs_codex


def index(root: Root, kind: str) -> list[dict]:
    lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
    return [line for line in map(json.loads, lines) if line['kind'] == kind]


def prompts(root: Root) -> str:
    """The last input item of every request the scripted model got: a prompt, a reply, or a tool's answer."""
    return ' '.join(str(request['input'][-1]) for request in root.model.requests)


def purposes(root: Root) -> list[str]:
    return [call['purpose'] for call in root.records.calls()]


def planting(plant):
    """A stand-in for meta.seal that calls plant(root) before it seals the candidate for a decision (not when the
    round starts or after the candidate was handled)."""
    original = meta.seal

    def seal(root_fd, name, label):
        if name == 'candidate' and label.startswith('M'):
            plant()
        return original(root_fd, name, label)
    return mock.patch.object(meta, 'seal', seal)


@needs_codex
class MetaRounds(unittest.TestCase):
    TRIAL = [{'task': 'T1', 'probe': None, 'version': 'candidate'}]
    LAB_OK = (add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), say('done'))

    def test_no_change(self):
        with Root() as root:
            root.model.push(say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('no_change', 'V0001'))
            call = root.records.load_call('R0001')
            self.assertEqual((call['role'], call['purpose'], call['cost_class'], call['round']),
                             ('meta', 'meta', 'meta', 'M0001'))
            self.assertIn('Meta', (root.records.call_dir('R0001') / 'prompt.txt').read_text())

    def test_trial_then_release(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('../candidate/RULES.md', 'new rule'), say(decision('trial', trials=self.TRIAL)),
                            *self.LAB_OK, say(decision('release', evidence=['R0002'])))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('released', 'V0002'))
            self.assertEqual((root.layout.work / 'RULES.md').read_text().strip(), 'new rule')
            release = json.loads((root.layout.records / 'releases' / 'V0002.json').read_text())
            self.assertEqual((release['kind'], release['benefit'], release['evidence'], release['parent']),
                             ('release', '未评估', ['R0002'], 'V0001'))
            self.assertEqual([(t['id'], t['task_seen']) for t in release['trials']], [('R0002', 'new')])
            self.assertEqual(root.store.log()[0]['author'], 'meta M0001')
            self.assertIn('lab', tomllib.loads((root.layout.lab_home / 'config.toml').read_text())['permissions'])

    def refused(self, steps, word):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); root.model.push(*steps, say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('no_change', 'V0001'))
            self.assertIn(word, prompts(root))

    def test_release_refusals(self):
        edit, trial = add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL))
        self.refused([edit, say(decision('release', evidence=['R0099']))], 'R0099')
        self.refused([edit, say(decision('release', evidence=['R0001']))], 'R0001')       # a Meta call is not evidence
        self.refused([edit, trial, *self.LAB_OK, add_file('../candidate/MORE.md', 'm'),
                      say(decision('release', evidence=['R0002']))], 'R0002')
        self.refused([say(decision('release', evidence=['R0001']))], '没有改动')
        self.refused([add_file('../candidate/codex/settings.toml', 'sandbox_mode = "x"'), trial], 'sandbox_mode')

    def test_invalid_twice_ends_the_round(self):
        with Root() as root:
            root.model.push(say('not json'), say('still not json'))
            self.assertEqual(run_round(root.layout, [])['outcome'], 'invalid_decision')
            self.assertEqual(len(root.model.requests), 2)

    def test_limits(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.owner / 'limits.toml').write_text('[meta]\ncalls_per_round = 1\n')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)))
            self.assertEqual(run_round(root.layout, [])['outcome'], 'limit_reached')
            self.assertEqual(purposes(root), ['meta'])
        with Root() as root:
            root.add_task('T1'); (root.layout.owner / 'limits.toml').write_text('[meta]\ntrials_per_round = 1\n')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL * 2)),
                            say('lab done'), say(decision('no_change')))
            run_round(root.layout, [])
            self.assertEqual(purposes(root).count('trial'), 1)

    def test_applications_and_current_version_trials(self):
        with Root() as root:
            root.add_task('T1')
            application = [{'what': '连接器 X', 'why': 'w', 'records': []}]
            root.model.push(say(decision('trial', trials=[{'task': 'T1', 'probe': None, 'version': 'current'}],
                                         applications=application)),
                            say('lab done'), say(decision('no_change')))
            run_round(root.layout, [])
            saved = json.loads((root.layout.records / 'applications' / 'A0001.json').read_text())
            self.assertEqual((saved['status'], saved['what'], saved['round']), ('pending', '连接器 X', 'M0001'))
            self.assertEqual(root.records.load_call('R0002')['tree'], root.store.tree_of('V0001'))

    def test_seen_task_label_and_rollback(self):
        with Root() as root:
            root.add_task('T1'); root.model.push(say('first run'))
            run_task(root.layout, 'T1')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            say('lab'), say(decision('release', evidence=['R0003'])))
            run_round(root.layout, ['R0001'])
            release = json.loads((root.layout.records / 'releases' / 'V0002.json').read_text())
            self.assertEqual(release['trials'][0]['task_seen'], 'seen')
            self.assertEqual(rollback(root.layout, 'V0001', 'owner'), 'V0003')
            self.assertEqual(root.store.tree_of('V0003'), root.store.tree_of('V0001'))
            self.assertFalse((root.layout.work / 'RULES.md').exists())
            self.assertEqual(json.loads((root.layout.records / 'releases' / 'V0003.json').read_text())['kind'],
                             'rollback')
            with self.assertRaises(ValueError): rollback(root.layout, '../V0001', 'owner')


@needs_codex
class SealedCandidate(unittest.TestCase):
    """The amendment's tests: the candidate is sealed before it is handled, like work/ and trial/."""
    TRIAL = MetaRounds.TRIAL

    def outside(self, root: Root):
        folder = root.layout.root / 'outside'; folder.mkdir()
        (folder / 'x').write_text('x'); (folder / 'RULES.md').write_text('outside')
        return folder

    def test_a_candidate_replaced_by_a_link_is_never_followed(self):
        with Root() as root:
            root.add_task('T1'); outside = self.outside(root); moved = []

            def plant():                                     # what "Meta" could do while it was running
                moved.append(root.layout.root / f'moved-{len(moved)}')
                os.rename(root.layout.candidate, moved[-1]); os.symlink(outside, root.layout.candidate)

            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            say(decision('release', evidence=['R0001'])), say(decision('no_change')))
            with planting(plant):
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head(), purposes(root)),
                             ('no_change', 'V0001', ['meta', 'meta', 'meta']))
            self.assertEqual((sorted(os.listdir(outside)), (outside / 'RULES.md').read_text()), (['RULES.md', 'x'],
                                                                                                  'outside'))
            self.assertIn('符号链接', prompts(root))
            self.assertFalse((root.store.git_dir / 'refs' / 'rounds' / 'M0001').exists())     # no snapshot at all
            notes = [note for note in index(root, 'sealed') if note['workspace'] == 'candidate']
            self.assertEqual(len(notes), 2)
            self.assertTrue(all((root.layout.root / note['path']).is_symlink() for note in notes))
            self.assertTrue(root.layout.candidate.is_dir()); self.assertFalse(root.layout.candidate.is_symlink())
            self.assertEqual(root.store.tree_of_dir(root.layout.candidate), root.store.tree_of('V0001'))

    def test_a_candidate_holding_codex_is_not_snapshotted(self):
        with Root() as root:
            root.add_task('T1')

            def plant():
                (root.layout.candidate / '.codex').mkdir()
                (root.layout.candidate / '.codex' / 'config.toml').write_text('sandbox_mode = "danger-full-access"\n')

            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            say(decision('no_change')))
            with planting(plant):
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], purposes(root)), ('no_change', ['meta', 'meta']))
            self.assertIn('.codex', str(root.model.requests[-1]['input'][-1]))          # the reply to the trial
            self.assertFalse((root.store.git_dir / 'refs' / 'rounds' / 'M0001').exists())
            self.assertFalse(os.path.lexists(root.layout.candidate / '.codex'))

    def test_after_a_trial_the_candidate_is_the_snapshot_and_a_later_edit_is_not_evidence(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            *MetaRounds.LAB_OK, say(decision('no_change')))
            run_round(root.layout, [])
            trial = root.records.load_call('R0002')
            self.assertEqual(root.store.tree_of_dir(root.layout.candidate), trial['tree'])
            self.assertEqual(trial['tree'], root.store.tree_of(root.store._out('rev-parse', 'refs/rounds/M0001')))
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            *MetaRounds.LAB_OK, add_file('../candidate/MORE.md', 'm'),
                            say(decision('release', evidence=['R0002'])), say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('no_change', 'V0001'))
            refusal = rnd['decisions'][1]['result']['refused']
            self.assertTrue(any('R0002' in line for line in refusal), refusal)

    def test_a_linked_meta_tmp_is_left_alone_and_the_call_still_runs(self):
        with Root() as root:
            outside = self.outside(root)
            os.symlink(outside, root.layout.meta / '.tmp')
            root.model.push(say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.records.load_call('R0001')['status']), ('no_change', 'finished'))
            self.assertTrue((root.layout.meta / '.tmp').is_symlink())
            self.assertEqual(sorted(os.listdir(outside)), ['RULES.md', 'x'])
            self.assertTrue((root.layout.meta / 'notes').is_dir())

    def test_a_linked_meta_folder_is_sealed_and_replaced(self):
        with Root() as root:
            outside = self.outside(root)
            os.rename(root.layout.meta, root.layout.root / 'meta-old'); os.symlink(outside, root.layout.meta)
            root.model.push(say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.records.load_call('R0001')['status']), ('no_change', 'finished'))
            self.assertEqual(sorted(os.listdir(outside)), ['RULES.md', 'x'])
            self.assertFalse(root.layout.meta.is_symlink())
            self.assertEqual(sorted(os.listdir(root.layout.meta)), ['.tmp', 'notes'])
            [note] = [note for note in index(root, 'sealed') if note['workspace'] == 'meta']
            self.assertTrue((root.layout.root / note['path']).is_symlink())
            self.assertTrue(any(note['path'] in line for line in rnd['problems']))


@needs_codex
class MoreMetaRounds(unittest.TestCase):
    """Beyond the brief and the amendment: Review Focus 4 and 5, the round record, and what the round leaves."""
    TRIAL = MetaRounds.TRIAL

    def test_a_bad_settings_file_in_the_candidate_is_a_problem_never_a_crash(self):
        with Root() as root:
            root.add_task('T1'); takes = []

            def plant():                                     # settings.toml becomes a folder before the release
                takes.append(True)
                if len(takes) == 2:
                    settings = root.layout.candidate / 'codex' / 'settings.toml'
                    settings.unlink(); settings.mkdir()

            root.model.push(add_file('../candidate/codex/settings.toml', 'not toml [[['),
                            say(decision('trial', trials=self.TRIAL)),
                            say(decision('release', evidence=['R0001'])),
                            say(decision('no_change')))
            with planting(plant):
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], purposes(root), root.store.head()),
                             ('no_change', ['meta', 'meta', 'meta'], 'V0001'))
            first, second = (decision_['result']['refused'] for decision_ in rnd['decisions'][:2])
            self.assertTrue(any('settings.toml' in line and 'TOML' in line for line in first), first)
            self.assertTrue(any('settings.toml' in line for line in second), second)
        with Root() as root:                                 # an empty settings file means the defaults
            root.add_task('T1')
            root.model.push(add_file('../candidate/codex/settings.toml', ''), say(decision('trial', trials=self.TRIAL)),
                            say('lab'), say(decision('release', evidence=['R0002'])))
            self.assertEqual(run_round(root.layout, [])['outcome'], 'released')

    def test_an_interrupted_call_is_neither_evidence_nor_more_than_a_status(self):
        with Root() as root:
            root.add_task('T1')
            with mock.patch('rsi.labrun.run_codex', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): run_task(root.layout, 'T1')
            interrupted = root.records.load_call('R0001')
            self.assertEqual(interrupted['status'], 'interrupted')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('release', evidence=['R0001'])),
                            say(decision('no_change')))
            rnd = run_round(root.layout, ['R0001'])
            self.assertEqual((rnd['outcome'], root.store.head()), ('no_change', 'V0001'))
            first = (root.records.call_dir('R0002') / 'prompt.txt').read_text()
            self.assertIn('R0001：状态 interrupted', first)
            self.assertTrue(any('R0001' in line and 'interrupted' in line
                                for line in rnd['decisions'][0]['result']['refused']))

    def test_an_unreadable_call_record_never_crashes_a_round(self):
        with Root() as root:
            (root.layout.records / 'runs' / 'R0001').mkdir(parents=True)
            (root.layout.records / 'runs' / 'R0001' / 'call.json').write_text('not json')
            self.assertEqual(root.records.calls()[0]['status'], 'unreadable')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('release', evidence=['R0001'])),
                            say(decision('no_change')))
            rnd = run_round(root.layout, ['R0001'])
            self.assertEqual((rnd['outcome'], rnd['calls']), ('no_change', ['R0002', 'R0003']))
            self.assertIn('R0001：记录读不出来', (root.records.call_dir('R0002') / 'prompt.txt').read_text())
            refused = rnd['decisions'][0]['result']['refused']
            self.assertTrue(any('R0001' in line and '读不出来' in line for line in refused), refused)

    def test_release_problems_names_what_it_is_about(self):
        with Root() as root:
            root.add_task('T1'); root.model.push(add_file('notes/n.md', 'n'), say('ok'))
            self.assertEqual(run_task(root.layout, 'T1')['version_after'], 'V0002')        # R0001 ran on V0001
            old, head, base = root.store.tree_of('V0001'), root.store.tree_of('V0002'), root.store.resolve('V0002')
            problems = release_problems(root.store, root.records, evidence=['R0001', 'R0007', 'X1'], tree=head,
                                        base=base, round_id='M0001', problems=['codex/settings.toml：不允许的键 x'])
            self.assertTrue(any('没有改动' in line for line in problems))
            for word in ('R0001', 'R0007', 'X1', 'codex/settings.toml'):
                self.assertTrue(any(word in line for line in problems), word)
            # The old version, released on its own earlier run (design §5.6, last paragraph).
            self.assertEqual(release_problems(root.store, root.records, evidence=['R0001'], tree=old, base=base,
                                              round_id='M0001'), [])
            moved = release_problems(root.store, root.records, evidence=['R0001'], tree=old, base='1' * 40,
                                     round_id='M0001')
            self.assertTrue(len(moved) == 1 and 'V0002' in moved[0], moved)

    def test_the_round_record_and_what_the_round_leaves(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(say('not json'), add_file('../candidate/RULES.md', 'r'),
                            say(decision('trial', trials=self.TRIAL + [{'task': 'T9', 'probe': None,
                                                                          'version': 'candidate'}])),
                            *MetaRounds.LAB_OK, say(decision('release', evidence=['R0003'])))
            rnd = run_round(root.layout, [])
            saved = json.loads((root.layout.records / 'rounds' / 'M0001' / 'round.json').read_text())
            self.assertEqual(saved, rnd)
            self.assertEqual((rnd['outcome'], rnd['version_before'], rnd['version_after']),
                             ('released', 'V0001', 'V0002'))
            self.assertEqual(rnd['calls'], ['R0001', 'R0002', 'R0004'])
            self.assertIn('JSON', rnd['decisions'][0]['invalid'])
            self.assertIn('T9', ' '.join(rnd['decisions'][1]['result']['lines']))
            self.assertEqual([trial['id'] for trial in rnd['trials']], ['R0003'])
            self.assertTrue((root.layout.records / 'rounds' / 'M0001' / 'schema.json').is_file())
            release = json.loads((root.layout.records / 'releases' / 'V0002.json').read_text())
            self.assertIn('RULES.md', (root.layout.records / release['diff']).read_text())
            self.assertEqual((release['round'], release['decision']['problem']), ('M0001', 'p'))
            meta_call = root.records.load_call('R0004')
            self.assertEqual(meta_call['resume'], root.records.load_call('R0001')['thread_id'])
            for name in ('staging', 'sealed'):
                self.assertEqual(os.listdir(root.layout.root / name) if (root.layout.root / name).exists() else [],
                                 [], name)
            self.assertEqual([name for name in os.listdir(root.layout.root) if name.startswith('.sealed-')], [])
            self.assertEqual((root.layout.work / 'RULES.md').read_text(), 'r\n')

    def test_a_candidate_that_was_not_released_is_kept(self):
        with Root() as root:                                 # snapshotted on the round's ref
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('no_change')))
            rnd = run_round(root.layout, [])
            final = rnd['final_candidate']
            self.assertEqual((rnd['outcome'], root.store.resolve(final)), ('no_change', final))
            self.assertIn('RULES.md', root.store.diff('V0001', final)); self.assertEqual(root.store.head(), 'V0001')
            self.assertEqual(json.loads((root.layout.records / 'rounds' / 'M0001' / 'round.json').read_text())
                             ['final_candidate'], final)
            self.assertEqual((root.layout.candidate / 'RULES.md').read_text(), 'r\n')     # candidate/ as Meta left it
            self.assertEqual((rnd['problems'], index(root, 'sealed')), ([], []))
            self.assertEqual([name for name in os.listdir(root.layout.root) if name.startswith('.sealed-')], [])
        with Root() as root:                                 # one that cannot be snapshotted: kept under sealed/
            root.model.push(add_file('../candidate/codex/settings.toml', 'not toml [[['), say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertIsNone(rnd['final_candidate'])
            [kept] = root.layout.sealed.glob('*/candidate/codex/settings.toml')
            self.assertEqual(kept.read_text(), 'not toml [[[\n')
            [note] = index(root, 'sealed')
            self.assertEqual((note['workspace'], note['round']), ('candidate', 'M0001'))
            self.assertTrue(any('candidate/' in line for line in rnd['problems']), rnd['problems'])
        with Root() as root:                                 # unchanged since the round's last snapshot: nothing new
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('../candidate/RULES.md', 'r'),
                            say(decision('trial', trials=MetaRounds.TRIAL)), *MetaRounds.LAB_OK,
                            say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], len(rnd['trials']), rnd['final_candidate']), ('no_change', 1, None))

    def test_owner_edits_in_work_become_a_version_before_the_candidate_is_cut(self):
        with Root() as root:
            root.add_task('T1'); (root.layout.work / 'mine.txt').write_text('by hand')
            root.model.push(say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['version_before'], root.store.log()[0]['author']), ('V0002', 'manual or leftover'))
            self.assertEqual((root.layout.candidate / 'mine.txt').read_text(), 'by hand')

    def test_the_daily_cap_reached_during_a_round_ends_it(self):
        with Root() as root:
            (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 1\n')
            root.model.push(say('not json'), say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], rnd['calls'], len(root.model.requests)), ('limit_reached', ['R0001'], 1))
            self.assertIn('day.calls', rnd['reason'])
            [note] = index(root, 'refusal'); self.assertEqual((note['purpose'], note['round']), ('meta', 'M0001'))

    def test_a_refused_round_starts_no_call(self):
        with Root() as root:
            (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 0\n')
            with self.assertRaises(Refused): run_round(root.layout, [])
            with self.assertRaises(ValueError): run_round(root.layout, ['../R0001'])
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))
            self.assertEqual([note['purpose'] for note in index(root, 'refusal')], ['meta'])
            self.assertFalse((root.layout.records / 'rounds').exists())

    def test_rollback_versions_owner_edits_first_and_refuses_bad_requests(self):
        with Root() as root:
            (root.layout.work / 'mine.txt').write_text('by hand')
            self.assertEqual(rollback(root.layout, 'V0001', 'owner'), 'V0003')      # V0002 is the owner's edit
            self.assertEqual([entry['author'] for entry in root.store.log()], ['owner', 'manual or leftover', 'owner'])
            self.assertFalse((root.layout.work / 'mine.txt').exists())
            record = json.loads((root.layout.records / 'releases' / 'V0003.json').read_text())
            self.assertEqual((record['kind'], record['version'], record['parent'], record['benefit']),
                             ('rollback', 'V0003', 'V0002', '未评估'))
            self.assertIn('mine.txt', (root.layout.records / record['diff']).read_text())
            for version in ('V0003', 'V0009', 'HEAD', '0' * 40):
                with self.assertRaises(ValueError, msg=version): rollback(root.layout, version, 'owner')
            self.assertEqual((root.store.head(), (root.layout.work / 'AGENTS.md').read_text()), ('V0003', 'hello\n'))


def after_each_meta_call(root: Root, plant):
    """A stand-in for meta.run_codex that notes, when each Meta call starts, whether candidate/ is a real folder, and
    calls plant(number) after call `number` (what Meta could do while it ran). Returns (the patch, the notes)."""
    original, real = meta.run_codex, []

    def run(**options):
        real.append(root.layout.candidate.is_dir() and not root.layout.candidate.is_symlink())
        result = original(**options)
        plant(len(real))
        return result
    return mock.patch.object(meta, 'run_codex', run), real


@needs_codex
class FixRound1(unittest.TestCase):
    """Review fix round 1: candidate/ before every Meta call, text that cannot be recorded, Meta's own AGENTS.md."""
    TRIAL = MetaRounds.TRIAL

    def link_candidate(self, root: Root, outside, immutable=False):
        os.rename(root.layout.candidate, root.layout.root / 'moved'); os.symlink(outside, root.layout.candidate)
        if immutable:                                 # support.force_remove clears the flag when the root goes
            os.chflags(root.layout.candidate, stat.UF_IMMUTABLE, follow_symlinks=False)

    def test_a_link_planted_before_an_invalid_answer_is_sealed_before_the_next_call(self):
        with Root() as root:
            outside = SealedCandidate.outside(self, root)
            patch, real = after_each_meta_call(root, lambda number: number == 1 and self.link_candidate(root, outside))
            root.model.push(say('not json'), say(decision('no_change')))
            with patch:
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], real), ('no_change', [True, True]))
            self.assertEqual(sorted(os.listdir(outside)), ['RULES.md', 'x'])
            [note] = [note for note in index(root, 'sealed') if note['workspace'] == 'candidate']
            self.assertTrue((root.layout.root / note['path']).is_symlink())
            self.assertIn('符号链接', str(root.model.requests[-1]['input'][-1]))          # Meta is told
            self.assertEqual(root.store.tree_of_dir(root.layout.candidate), root.store.tree_of('V0001'))

    def test_an_immutable_link_that_cannot_be_sealed_stops_the_round_before_another_call(self):
        with Root() as root:
            root.add_task('T1'); outside = SealedCandidate.outside(self, root)
            patch, real = after_each_meta_call(
                root, lambda number: number == 1 and self.link_candidate(root, outside, immutable=True))
            root.model.push(say(decision('trial', trials=self.TRIAL)), say(decision('no_change')))
            with patch:
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], rnd['calls'], real, len(root.model.requests)),
                             ('limit_reached', ['R0001'], [True], 1))
            self.assertIn('candidate/', rnd['reason']); self.assertEqual(purposes(root), ['meta'])
            [note] = index(root, 'refusal'); self.assertEqual((note['round'], note['reason']), ('M0001', rnd['reason']))
            self.assertEqual(sorted(os.listdir(outside)), ['RULES.md', 'x'])
            self.assertTrue((root.layout.records / 'rounds' / 'M0001' / 'round.json').is_file())

    def test_an_answer_with_text_that_cannot_be_recorded_is_not_carried_out(self):
        with Root() as root:
            root.add_task('T1')
            bad = decision('release', evidence=['R0002']).replace('"problem": "p"', '"problem": "\\ud800"')
            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            say('lab'), say(bad), say(decision('no_change')))
            rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('no_change', 'V0001'))
            self.assertIn('UTF-8', rnd['decisions'][1]['invalid'])
            saved = json.loads((root.layout.records / 'rounds' / 'M0001' / 'round.json').read_text())
            self.assertEqual(saved, rnd)

    def test_records_written_after_a_release_are_escaped_never_lost(self):
        with Root() as root:
            root.add_task('T1'); real = meta.parse_decision

            def unrecordable(text):                       # as if a lone surrogate had got past parse_decision
                parsed = real(text)
                parsed.notes['problem'] = '坏\ud800'
                return parsed

            root.model.push(add_file('../candidate/RULES.md', 'r'), say(decision('trial', trials=self.TRIAL)),
                            say('lab'), say(decision('release', evidence=['R0002'])))
            with mock.patch.object(meta, 'parse_decision', unrecordable):
                rnd = run_round(root.layout, [])
            self.assertEqual((rnd['outcome'], root.store.head()), ('released', 'V0002'))
            release = json.loads((root.layout.records / 'releases' / 'V0002.json').read_text())
            self.assertEqual((release['decision']['problem'], release['evidence']), ('坏\\ud800', ['R0002']))
            saved = json.loads((root.layout.records / 'rounds' / 'M0001' / 'round.json').read_text())
            self.assertEqual((saved['outcome'], saved['version_after']), ('released', 'V0002'))
            self.assertTrue(any('UTF-8' in line for line in rnd['problems']), rnd['problems'])

    def test_meta_never_loads_standing_instructions_from_its_own_folder(self):
        with Root() as root:
            for name in ('AGENTS.md', 'AGENTS.override.md'):
                (root.layout.meta / name).write_text(f'Meta 自己写的指示 MARKER-7f3a-{name}\n')
            root.model.push(say('not json'), say(decision('no_change')))
            self.assertEqual(run_round(root.layout, [])['outcome'], 'no_change')
            self.assertEqual(len(root.model.requests), 2)
            self.assertNotIn('MARKER-7f3a', json.dumps(root.model.requests, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
