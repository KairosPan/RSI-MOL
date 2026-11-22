import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from rsi import cli
from rsi import init as init_module
from rsi.cli import main, safe
from rsi.init import init, locked, recover
from rsi.labrun import process_recorder
from rsi.mockmodel import add_file, say, slow
from rsi.paths import Layout, Refused
from rsi.versions import VersionStore
from tests.support import ENVELOPE, Root, decision, force_remove, needs_codex

SEED = Path(__file__).resolve().parents[1] / 'seed'
LAUNCHER = Path(__file__).resolve().parents[2] / 'rsi'
CONFIG = ('approval_policy = "on-request"\n[features]\napps = true\n'
          '[apps._default]\nenabled = false\n[apps.conn_a]\nenabled = true\n[plugins."pdf@x"]\nenabled = true\n'
          '[tui]\nx = 1\n[projects."/somewhere"]\ntrust_level = "trusted"\n')
PYTHON = '/opt/homebrew/bin/python3'
HAS_CODEX = shutil.which(os.environ.get('RSI_CODEX', 'codex')) is not None


def bare(test: unittest.TestCase, name: str = 'root', env: bool = True) -> Layout:
    """A folder for a new RSI root, removed after the test; with an empty env/ unless env is false (init then makes
    a virtual environment, which takes a few seconds)."""
    tmp = Path(tempfile.mkdtemp()).resolve()
    test.addCleanup(force_remove, tmp)
    (tmp / name).mkdir()
    if env:
        (tmp / name / 'env').mkdir()
    return Layout(tmp / name)


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def group_alive(pgid: int, seconds: float = 5) -> bool:
    """Whether the process group still has a live member after waiting up to `seconds` for it to go."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:                 # ESRCH; on macOS EPERM when only an exited leader is left
            return False
        time.sleep(0.1)
    return True


def notes(layout: Layout, kind: str) -> list[dict]:
    lines = (layout.records / 'index.jsonl').read_text().splitlines()
    return [line for line in map(json.loads, lines) if line['kind'] == kind]


def a_seed(test: unittest.TestCase) -> Path:
    """A seed with two work files, one in a sub-folder, and the owner's files of the real seed."""
    seed = Path(tempfile.mkdtemp()).resolve()
    test.addCleanup(force_remove, seed)
    shutil.copytree(SEED / 'owner', seed / 'owner')
    (seed / 'work' / 'sub').mkdir(parents=True)
    (seed / 'work' / 'AGENTS.md').write_text('seed\n')
    (seed / 'work' / 'sub' / 'a.md').write_text('seed a\n')
    return seed


class Init(unittest.TestCase):
    def test_init_imports_the_existing_configuration(self):
        layout = bare(self, env=False); layout.lab_home.mkdir()
        (layout.lab_home / 'config.toml').write_text(CONFIG)
        first = init(layout, SEED); second = init(layout, SEED)
        envelope = tomllib.loads((layout.owner / 'lab-envelope.toml').read_text())
        self.assertEqual(envelope['apps']['conn_a'], {'enabled': True}); self.assertNotIn('tui', envelope)
        self.assertNotIn('projects', envelope); self.assertEqual(envelope['plugins'], {'pdf@x': {'enabled': True}})
        self.assertIn('extra_read', envelope['rsi']); self.assertEqual(envelope['rsi']['python'], PYTHON)
        self.assertEqual((layout.lab_home / 'config.toml.before-rsi').read_text(), CONFIG)
        store = VersionStore(layout.versions)
        self.assertEqual((store.head(), store.log()[0]['author'], len(store.log())), ('V0001', 'init', 1))
        self.assertTrue((layout.work / 'AGENTS.md').is_file())
        self.assertTrue((layout.owner / 'meta' / 'AGENTS.md').is_file())
        self.assertTrue((layout.owner / 'meta' / 'settings.toml').is_file())
        self.assertTrue((layout.owner / 'limits.toml').is_file())
        generated = tomllib.loads((layout.lab_home / 'config.toml').read_text())
        self.assertEqual((generated['default_permissions'], generated['approval_policy']), ('lab', 'on-request'))
        self.assertNotIn('projects', generated); self.assertEqual(generated['apps']['conn_a'], {'enabled': True})
        for folder in (layout.owner, layout.tasks, layout.meta, layout.records, layout.library, layout.versions,
                       layout.staging, layout.sealed, layout.lab_home, layout.meta_home):
            self.assertTrue(folder.is_dir() and not folder.is_symlink(), folder)
        self.assertFalse(layout.trial.exists()); self.assertFalse(layout.candidate.exists())
        self.assertEqual(os.listdir(layout.staging), [])
        self.assertEqual((first['first_version'], second['first_version'], second['version']), ('V0001', None, 'V0001'))
        self.assertEqual((first['envelope'], second['envelope'], second['work']), ('imported', 'kept', 'kept'))
        self.assertEqual(first['original'],
                         {'path': 'codex-home/config.toml.before-rsi', 'dropped': ['projects', 'tui']})
        self.assertEqual(first['envelope_source'], 'codex-home/config.toml.before-rsi')
        if os.path.isfile(PYTHON):
            self.assertTrue((layout.env / 'pyvenv.cfg').is_file()); self.assertEqual(second['env'], 'kept')
        if HAS_CODEX:
            self.assertEqual(first['signed_in'], {'codex-home': False, 'codex-home-meta': False})

    def test_init_without_a_configuration_and_never_replacing_the_owners_files(self):
        layout = bare(self)
        report = init(layout, SEED)
        envelope = tomllib.loads((layout.owner / 'lab-envelope.toml').read_text())
        self.assertEqual((report['envelope'], envelope['approval_policy']), ('default', 'on-request'))
        self.assertFalse((layout.lab_home / 'config.toml.before-rsi').exists())
        (layout.owner / 'limits.toml').write_text('[day]\ncalls = 5\n')
        (layout.lab_home / 'config.toml').write_text('approval_policy = "never"\n')     # the owner's own file now
        init(layout, SEED)
        self.assertEqual((layout.owner / 'limits.toml').read_text(), '[day]\ncalls = 5\n')
        self.assertEqual(tomllib.loads((layout.owner / 'lab-envelope.toml').read_text())['approval_policy'],
                         'on-request')
        self.assertEqual((layout.lab_home / 'config.toml.before-rsi').read_text(), 'approval_policy = "never"\n')

    def test_the_owners_config_is_kept_even_when_the_envelope_is_written_by_hand(self):
        layout = bare(self); layout.lab_home.mkdir(); layout.owner.mkdir()
        (layout.lab_home / 'config.toml').write_text(CONFIG)
        (layout.owner / 'lab-envelope.toml').write_text(ENVELOPE)
        report = init(layout, SEED)
        self.assertEqual(((layout.lab_home / 'config.toml.before-rsi').read_text(), report['envelope']),
                         (CONFIG, 'kept'))
        self.assertEqual((layout.owner / 'lab-envelope.toml').read_text(), ENVELOPE)
        self.assertEqual(tomllib.loads((layout.lab_home / 'config.toml').read_text())['approval_policy'], 'never')

    def test_an_invalid_config_is_kept_and_reported_never_imported(self):
        layout = bare(self); layout.lab_home.mkdir()
        (layout.lab_home / 'config.toml').write_text('model = [unclosed\n')
        report = init(layout, SEED)
        self.assertEqual((layout.lab_home / 'config.toml.before-rsi').read_text(), 'model = [unclosed\n')
        self.assertEqual((report['envelope'], report['original']), ('default', None))
        self.assertTrue(any('before-rsi is not valid TOML' in line for line in report['problems']), report['problems'])
        self.assertTrue(tomllib.loads((layout.lab_home / 'config.toml').read_text())['permissions'])
        init(layout, SEED)
        self.assertEqual((layout.lab_home / 'config.toml.before-rsi').read_text(), 'model = [unclosed\n')
        self.assertFalse((layout.lab_home / 'config.toml.before-rsi-2').exists())

    def test_an_empty_copy_is_reported_never_taken_as_nothing_to_import(self):
        layout = bare(self); layout.lab_home.mkdir()
        (layout.lab_home / 'config.toml.before-rsi').write_bytes(b'')      # what the old copy left on a full disk
        (layout.lab_home / 'config.toml').write_text(CONFIG)
        report = init(layout, SEED)
        self.assertEqual((layout.lab_home / 'config.toml.before-rsi-2').read_text(), CONFIG)
        self.assertEqual((report['envelope'], report['envelope_source'], report['original']['dropped']),
                         ('imported', 'codex-home/config.toml.before-rsi-2', ['projects', 'tui']))
        lone = bare(self, 'lone'); lone.lab_home.mkdir()
        (lone.lab_home / 'config.toml.before-rsi').write_bytes(b'')
        report = init(lone, SEED)
        self.assertEqual(report['envelope'], 'default')
        self.assertTrue(any('config.toml.before-rsi is empty' in line for line in report['problems']),
                        report['problems'])

    def test_a_deleted_envelope_is_imported_again_from_the_kept_original_never_from_the_generated_file(self):
        layout = bare(self); layout.lab_home.mkdir()
        (layout.lab_home / 'config.toml').write_text(CONFIG)
        init(layout, SEED)
        (layout.owner / 'lab-envelope.toml').unlink()
        (layout.lab_home / 'config.toml').write_text('# generated by rsi\napproval_policy = "never"\n')
        self.assertEqual(init(layout, SEED)['envelope'], 'imported')
        envelope = tomllib.loads((layout.owner / 'lab-envelope.toml').read_text())
        self.assertEqual((envelope['approval_policy'], envelope['apps']['conn_a']), ('on-request', {'enabled': True}))
        fresh = bare(self, 'fresh')                                                 # no original at all
        init(fresh, SEED); (fresh.owner / 'lab-envelope.toml').unlink()
        self.assertEqual(init(fresh, SEED)['envelope'], 'default')
        self.assertFalse((fresh.lab_home / 'config.toml.before-rsi').exists())

    def test_init_names_the_original_keys_it_does_not_carry_over(self):
        layout = bare(self); layout.lab_home.mkdir()
        (layout.lab_home / 'config.toml').write_text(CONFIG)
        with mock.patch.dict(os.environ, {'RSI_ROOT': str(layout.root)}):
            code, out, _ = run('init')
        line = next(line for line in out.splitlines() if 'were not carried into' in line)
        self.assertEqual(code, 0); self.assertIn('config.toml: projects, tui (', line)
        self.assertNotIn('trusted', out); self.assertNotIn('/somewhere', out)

    def test_a_refused_seal_of_work_leaves_nothing_half_done(self):
        layout = bare(self)
        layout.work.mkdir(); (layout.work / 'mine.md').write_text('by hand\n')
        real, tries = init_module.seal, []

        def seal_once_refused(root_fd, name, label):
            tries.append(name)
            if len(tries) == 1:
                raise PermissionError(1, 'Operation not permitted', name)
            return real(root_fd, name, label)
        with mock.patch.object(init_module, 'seal', seal_once_refused):
            with self.assertRaises(Refused):
                init(layout, SEED)
            self.assertIsNone(VersionStore(layout.versions).head())
            with mock.patch.object(init_module, '_first_version', side_effect=RuntimeError('store failed')):
                with self.assertRaises(Refused):
                    init(layout, SEED)
            self.assertEqual(((layout.work / 'mine.md').read_text(), VersionStore(layout.versions).head()),
                             ('by hand\n', None))
            self.assertFalse(any(name.startswith('.sealed-') for name in os.listdir(layout.root)))
            init(layout, SEED)
        self.assertEqual([entry['author'] for entry in VersionStore(layout.versions).log()],
                         ['manual or leftover', 'init'])
        self.assertTrue((layout.work / 'mine.md').is_file()); self.assertTrue((layout.work / 'AGENTS.md').is_file())

    def test_init_adds_missing_seed_files_to_what_sits_in_work_and_never_follows_a_link(self):
        layout, seed = bare(self), a_seed(self)
        outside = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(force_remove, outside)
        layout.work.mkdir(); (layout.work / 'mine.md').write_text('by hand\n')
        (layout.work / 'sub').symlink_to(outside)
        report = init(layout, seed)
        store = VersionStore(layout.versions)
        self.assertEqual([entry['author'] for entry in store.log()], ['manual or leftover', 'init'])
        self.assertEqual(((layout.work / 'mine.md').read_text(), (layout.work / 'AGENTS.md').read_text()),
                         ('by hand\n', 'seed\n'))
        self.assertTrue((layout.work / 'sub').is_symlink()); self.assertEqual(os.listdir(outside), [])
        self.assertTrue(any('sub/a.md' in line for line in report['problems']), report['problems'])

    def test_what_sits_in_work_wins_and_an_empty_work_adds_no_version(self):
        layout, seed = bare(self), a_seed(self)
        layout.work.mkdir(); (layout.work / 'AGENTS.md').write_text('mine\n')
        init(layout, seed)
        self.assertEqual((layout.work / 'AGENTS.md').read_text(), 'mine\n')
        self.assertEqual((layout.work / 'sub' / 'a.md').read_text(), 'seed a\n')
        empty, seed = bare(self, 'other'), a_seed(self)
        empty.work.mkdir()
        init(empty, seed)
        store = VersionStore(empty.versions)
        self.assertEqual(([entry['author'] for entry in store.log()], (empty.work / 'AGENTS.md').read_text()),
                         (['init'], 'seed\n'))
        self.assertFalse(any(name.startswith('.sealed-') for name in os.listdir(empty.root)))

    def test_a_work_link_is_kept_aside_never_followed(self):
        layout = bare(self)
        outside = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(force_remove, outside)
        (outside / 'x.txt').write_text('outside')
        layout.work.symlink_to(outside)
        init(layout, SEED)
        self.assertEqual(os.listdir(outside), ['x.txt'])
        self.assertTrue(layout.work.is_dir() and not layout.work.is_symlink())
        self.assertEqual(VersionStore(layout.versions).log()[0]['author'], 'init')
        self.assertEqual(len(notes(layout, 'sealed')), 1)

    def test_a_root_holding_what_codex_loads_is_refused_before_anything_is_made(self):
        # The root is a git repository, so Codex may take it for the project root of work/ and load these from it.
        for name in ('AGENTS.md', 'AGENTS.override.md', '.agents', '.codex'):
            layout = bare(self, env=False)
            (layout.root / name).mkdir() if name.startswith('.') else (layout.root / name).write_text('x')
            with self.assertRaises(Refused) as refusal:
                init(layout, SEED)
            self.assertIn(name, str(refusal.exception)); self.assertIn('git', str(refusal.exception))
            self.assertEqual(os.listdir(layout.root), [name])

    def test_a_root_codex_cannot_sandbox_is_refused_before_anything_is_made(self):
        layout = bare(self, 'a[b', env=False)
        with self.assertRaises(Refused):
            init(layout, SEED)
        self.assertEqual(os.listdir(layout.root), [])


class Recover(unittest.TestCase):
    def test_recover_marks_and_never_reruns(self):
        with Root() as root:
            call = root.records.new_id('R')
            root.records.start_call(call, {'role': 'lab', 'purpose': 'task', 'pid': 999999})
            self.assertEqual(recover(root.layout), [call])
            loaded = root.records.load_call(call)
            self.assertEqual((loaded['status'], loaded['usage']), ('interrupted', None))
            self.assertEqual(root.model.requests, [])
            self.assertEqual(recover(root.layout), [])

    def test_recover_moves_what_an_interrupted_run_left_and_restores_work(self):
        with Root() as root:
            layout = root.layout
            (layout.staging / 'S1').mkdir(parents=True); (layout.staging / 'S1' / 'x.txt').write_text('x')
            layout.work.rename(layout.root / '.sealed-work-R0009')
            moved = []
            self.assertEqual(recover(layout, moved), [])
            self.assertEqual(os.listdir(layout.staging), [])
            self.assertFalse(any(name.startswith('.sealed-') for name in os.listdir(layout.root)))
            kept = list(layout.sealed.glob('recover-*'))
            self.assertEqual(len(kept), 1)
            self.assertEqual((kept[0] / 'staging' / 'S1' / 'x.txt').read_text(), 'x')
            self.assertEqual((kept[0] / 'sealed-work-R0009' / 'AGENTS.md').read_text(), 'hello\n')
            self.assertEqual(sorted(note['workspace'] for note in notes(layout, 'sealed')), ['staging', 'work'])
            self.assertTrue(all(note['command'] == 'recover' and 'rsi recover' in note['reasons'][0]
                                for note in notes(layout, 'sealed')))
            self.assertEqual(len(moved), 3)                                          # two moves and work/ restored
            self.assertEqual((layout.work / 'AGENTS.md').read_text(), 'hello\n')
            self.assertEqual(root.store.tree_of_dir(layout.work), root.store.tree_of(root.store.head()))

    def test_recover_kills_a_process_group_that_ignores_sigterm(self):
        with Root() as root:
            stubborn = subprocess.Popen(['/bin/sh', '-c', 'trap "" TERM; echo ready; exec /bin/sleep 60'],
                                        start_new_session=True, stdout=subprocess.PIPE)
            self.addCleanup(stubborn.stdout.close)
            stubborn.stdout.readline()                                  # SIGTERM is ignored from here on
            self.addCleanup(stubborn.wait); self.addCleanup(stubborn.kill)
            root.records.start_call(root.records.new_id('R'), {'role': 'lab', 'purpose': 'task'})
            process_recorder(root.records, 'R0001')(stubborn.pid)
            with mock.patch.object(init_module, 'STOP_GRACE', 1):
                self.assertEqual(recover(root.layout), ['R0001'])
            self.assertEqual(stubborn.wait(timeout=10), -signal.SIGKILL)
            self.assertEqual(root.records.load_call('R0001')['status'], 'interrupted')
            [note] = notes(root.layout, 'recovered')
            self.assertEqual((note['signalled'], note['killed']), (True, True))

    def test_task_meta_and_rollback_wait_until_recover_has_run(self):
        with Root() as root:
            root.add_task('T1')
            call = root.records.new_id('R')
            root.records.start_call(call, {'role': 'lab', 'purpose': 'task', 'task': 'T1'})
            for leftover in ('started', 'sealed'):
                if leftover == 'sealed':
                    recover(root.layout)
                    (root.layout.root / '.sealed-work-x').mkdir()
                for args in (('task', 'T1'), ('task', 'T1', '--no-meta'), ('meta',), ('rollback', 'V0001')):
                    code, out, err = run(*args)
                    self.assertEqual((code, out), (3, ''), (leftover, args, err))
                    self.assertIn('rsi recover', err); self.assertEqual(len(err.splitlines()), 1, err)
                    self.assertIn(call if leftover == 'started' else '.sealed-work-x', err)
            self.assertEqual(len(root.records.calls()), 1); self.assertEqual(root.model.requests, [])
            self.assertEqual(len(notes(root.layout, 'refusal')), 8)
            recover(root.layout)
            self.assertFalse(os.path.lexists(root.layout.root / '.sealed-work-x'))
            root.model.push(say('ok'))
            self.assertEqual(run('task', 'T1', '--no-meta')[0], 0)

    @needs_codex
    def test_a_terminated_rsi_stops_its_codex_and_tells_the_owner_to_recover(self):
        with Root() as root:
            root.add_task('T1')
            root.model.push(slow(120, say('late')))
            process = subprocess.Popen([str(LAUNCHER), 'task', 'T1', '--no-meta'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(process.kill)
            deadline = time.monotonic() + 60
            while not root.model.requests:                              # Codex has started and asked the model
                self.assertIsNone(process.poll(), process.stderr.read() if process.poll() is not None else '')
                self.assertLess(time.monotonic(), deadline); time.sleep(0.1)
            pid = json.loads((root.records.call_dir('R0001') / 'process.json').read_text())['pid']
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 1, err); self.assertIn('rsi recover', err)
            self.assertFalse(group_alive(pid))
            self.assertEqual(root.records.load_call('R0001')['status'], 'interrupted')

    def test_sighup_ends_a_command_the_way_ctrl_c_does(self):
        def hang_up(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(10)
        hit = []

        def guard(*args):                       # the test runner's own: SIGHUP must never end the test run
            hit.append(args)
        previous = signal.signal(signal.SIGHUP, guard); self.addCleanup(signal.signal, signal.SIGHUP, previous)
        term = signal.getsignal(signal.SIGTERM)
        with Root() as root:
            root.add_task('T1')
            with mock.patch.object(cli, 'run_task', hang_up):
                started = time.monotonic(); code, _, err = run('task', 'T1')
            self.assertEqual(code, 1); self.assertIn('rsi recover', err); self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(hit, [])                                       # rsi's own handler took it
        self.assertEqual((signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM)), (guard, term))

    def test_recover_stops_only_the_process_group_the_call_started(self):
        with Root() as root:
            ours = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
            other = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
            for process in (ours, other):
                self.addCleanup(process.wait); self.addCleanup(process.kill)
            for _ in range(2):
                root.records.start_call(root.records.new_id('R'), {'role': 'lab', 'purpose': 'task'})
            process_recorder(root.records, 'R0001')(ours.pid)               # as labrun and meta record a call
            an_hour_ago = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec='seconds')
            root.records.write_json('runs/R0002/process.json', {'pid': other.pid, 'started': an_hour_ago})
            self.assertEqual(recover(root.layout), ['R0001', 'R0002'])
            self.assertEqual(ours.wait(timeout=10), -signal.SIGTERM)
            time.sleep(0.2)
            self.assertIsNone(other.poll())
            self.assertEqual([note['signalled'] for note in notes(root.layout, 'recovered')], [True, False])


class Commands(unittest.TestCase):
    def test_design_lists_the_cold_start_and_owner_commands_in_their_tables(self):
        design = (LAUNCHER.parent / 'docs/design-memory.md').read_text()
        conditions = design.split('### 5.5 ', 1)[1].split('### 5.6 ', 1)[0]
        rows = [line.strip('|').split('|') for line in conditions.splitlines() if line.startswith('|')]
        self.assertTrue(rows and all(len(row) == 4 for row in rows))
        none, = [row for row in rows if '`none`' in row[0]]
        args = none[1].strip().strip('`').split()
        self.assertEqual(args[0], 'rsi')
        self.assertEqual(cli._parse(args[1:]), ('task', {'task': args[2], 'memory': 'none', 'meta': True}))
        commands = design.split('### 5.7 ', 1)[1].split('### ', 1)[0]
        for action in ('source-changed', 'withdraw'):
            entry, = [line for line in commands.splitlines() if line.startswith(f'| `rsi memory {action} ')]
            self.assertEqual(len(entry.strip('|').split('|')), 2)

    def test_lock(self):
        with Root() as root, locked(root.layout):
            with self.assertRaises(Refused):
                with locked(root.layout): pass

    def test_usage_and_the_lock(self):
        for args in ([], ['nope'], ['task'], ['task', 'a', 'b'], ['status', '--x'], ['rollback'], ['init', 'x']):
            self.assertEqual(run(*args)[0], 2, args)
        with Root() as root:
            root.add_task('T1')
            with locked(root.layout):
                code, _, err = run('task', 'T1')
                self.assertEqual((code, 'Another rsi is running' in err), (3, True))
                self.assertEqual(run('status')[0], 0)
            self.assertEqual(root.records.calls(), [])

    def test_status(self):
        with Root() as root:
            root.records.write_json('applications/A0001.json', {'id': 'A0001', 'status': 'pending', 'round': 'M0001',
                                                                 'call': 'R0001', 'what': 'open a connector',
                                                                 'why': 'w', 'records': []})
            root.records.write_json('applications/A0002.json', {'id': 'A0002', 'status': 'granted', 'what': 'x'})
            code, out, _ = run('status', '--json')
            status = json.loads(out)
            self.assertEqual((code, status['version'], status['versions'][0]['version'], status['calls']),
                             (0, 'V0001', 'V0001', []))
            self.assertEqual(([a['id'] for a in status['applications']], status['releases']), (['A0001'], []))
            self.assertEqual((status['check'] or {}).get('passed'), True if HAS_CODEX else None)   # Root's check.json
            self.assertEqual(status['usage']['online']['calls'], 0)
            code, out, _ = run('status')
            self.assertEqual(code, 0); self.assertIn('Current version: V0001', out)
            self.assertIn('Pending applications: A0001 open a connector', out)
            code, out, _ = run('meta')                                  # no Lab task call yet: no round
            self.assertEqual((code, 'There are no new task runs; Meta was not started.' in out), (0, True))
            self.assertFalse((root.layout.records / 'rounds').exists())

    def test_only_a_request_that_fails_its_checks_is_refused(self):
        with Root() as root:
            root.add_task('T1')
            for args in (('task', 'no-such-task'), ('task', '..'), ('rollback', '../x'), ('rollback', 'V0009'),
                         ('rollback', 'V0001')):                     # V0001 is already the current content
                self.assertEqual(run(*args)[0], 3, args)
            # The error text of a UnicodeEncodeError holds the character it could not encode: a Chinese one here.
            for error in (ValueError('an internal error'), UnicodeEncodeError('ascii', '中', 0, 1, 'x')):
                with mock.patch.object(cli, 'run_task', side_effect=error):
                    code, out, err = run('task', 'T1')
                self.assertEqual((code, err.startswith('Error: '), 'Traceback' in err + out), (1, True, False), error)
            self.assertEqual(root.records.calls(), [])

    def test_text_status_survives_odd_and_unreadable_records(self):
        with Root() as root:
            folder = root.records.call_dir('R0001'); folder.mkdir(parents=True)
            (folder / 'call.json').write_text(json.dumps({'id': 'R0001', 'role': 'lab', 'purpose': 'task',
                                                         'status': ['finished'], 'task': {'a': 1}}))
            folder = root.records.call_dir('R0002'); folder.mkdir(parents=True)
            (folder / 'call.json').write_text('{not json\x1b[2J')
            code, out, _ = run('status')
            self.assertEqual(code, 0); self.assertNotIn('None None', out); self.assertNotIn('\x1b', out)
            self.assertIn('R0002: the record cannot be read (', out); self.assertIn("['finished']", out)
            self.assertEqual(run('status', '--json')[0], 0)
            lines = cli._round_lines(root.layout, {'id': 'M0001', 'outcome': ['x'], 'trials': 5, 'calls': [1]})
            self.assertIn("['x']", lines[0])

    def test_text_from_records_is_printed_without_control_characters(self):
        hostile = 'x\x1b[2J\ry\u202ez\x07\x9b\u2066\n' + 'w' * 1000
        with Root() as root:
            root.records.write_json('applications/A0001.json', {'id': 'A0001', 'status': 'pending', 'what': hostile})
            call = root.records.new_id('R')
            root.records.start_call(call, {'role': 'lab', 'purpose': 'task', 'task': 'T\x1b]0;t\x07'})
            root.records.finish_call(call, {'status': 'finished', 'check': {'passed': False, 'message': hostile}})
            code, out, _ = run('status')
            self.assertEqual(code, 0); self.assertIn('\\x1b[2J', out); self.assertIn('\\u202e', out)
            for char in ('\x1b', '\r', '\u202e', '\x07', '\x9b', '\u2066'):
                self.assertNotIn(char, out)
            self.assertTrue(all(len(line) < 1000 for line in out.splitlines()))
            code, out, _ = run('status', '--json')
            self.assertEqual(json.loads(out)['applications'][0]['what'], hostile)
            for char in ('\x1b', '\r', '\u202e', '\x07', '\x9b', '\u2066'):
                self.assertNotIn(char, out)
        self.assertEqual(safe('a\nb\ud800', None), 'a\\x0ab\\ud800'); self.assertEqual(len(safe('w' * 999)), 300)

    @needs_codex
    def test_task_command_end_to_end(self):
        with Root() as root:
            root.add_task('T1'); root.model.push(say('lab done'), say(decision('no_change')))
            self.assertEqual(run('task', 'T1')[0], 0)
            self.assertEqual([c['purpose'] for c in root.records.calls()], ['task', 'meta'])
            for call in ('R0001', 'R0002'):                                  # a Lab call and a Meta call
                process = json.loads((root.records.call_dir(call) / 'process.json').read_text())
                self.assertEqual((type(process['pid']), type(datetime.fromisoformat(process['started']))),
                                 (int, datetime))
            root.model.push(add_file('NOTES.md', 'n'), say('lab again'))
            code, out, _ = run('task', 'T1', '--no-meta')
            self.assertEqual(code, 0); self.assertIn('R0003', out); self.assertIn('records/runs/R0003', out)
            self.assertEqual(run('task', 'no-such-task')[0], 3)
            code, out, _ = run('status', '--json')
            status = json.loads(out)
            self.assertEqual((status['version'], len(status['calls']), status['usage']['online']['calls']),
                             ('V0002', 3, 2))
            self.assertEqual([c['id'] for c in status['calls']], ['R0003', 'R0002', 'R0001'])
            self.assertEqual(run('rollback', 'V0001')[0], 0); self.assertEqual(run('rollback', '../x')[0], 3)
            self.assertEqual((root.store.head(), (root.layout.work / 'NOTES.md').exists()), ('V0003', False))
            self.assertEqual(json.loads(run('status', '--json')[1])['releases'][0]['kind'], 'rollback')

    @needs_codex
    def test_a_task_run_that_ended_exits_0_even_when_the_meta_round_after_it_is_refused(self):
        with Root() as root:
            root.add_task('T1')
            (root.layout.owner / 'limits.toml').write_text('[day]\ncalls = 1\n')       # the task run takes it
            root.model.push(say('lab done'))
            code, out, err = run('task', 'T1')
            self.assertEqual((code, err), (0, ''))
            self.assertIn('R0001 (Lab task T1): finished\n', out)
            line = next(line for line in out.splitlines() if 'Meta' in line)
            self.assertIn('day.calls', line)
            self.assertEqual(len(root.records.calls()), 1); self.assertFalse((root.layout.records / 'rounds').exists())

    @needs_codex
    def test_corrupt_memory_refuses_automatic_and_manual_meta_before_a_call(self):
        with Root() as root:
            root.add_task('T1')
            root.records.write_json('memory/R0010.json', {})
            root.model.push(say('lab done'), say(decision('no_change')))
            code, out, _ = run('task', 'T1', '--memory', 'none')
            self.assertEqual(code, 0)
            self.assertIn('No Meta round was run', out)
            self.assertIn('no matching memory batch', out)

            notes = root.layout.work / 'notes.md'
            notes.write_text('owner edit')
            code, out, err = run('meta')
            self.assertEqual(code, 3)
            self.assertIn('no matching memory batch', out + err)
            self.assertEqual(notes.read_text(), 'owner edit')
            self.assertEqual(json.loads((root.layout.work / 'memory/status.json').read_bytes())['condition'], 'unavailable')
            self.assertEqual(len(root.model.requests), 1)
            self.assertEqual([c['purpose'] for c in root.records.calls()], ['task'])
            self.assertFalse((root.layout.records / 'rounds').exists())

            (root.layout.records / 'memory/R0010.json').unlink()
            self.assertEqual(run('meta')[0], 0)
            self.assertEqual([c['purpose'] for c in root.records.calls()], ['task', 'meta'])
            self.assertEqual(json.loads((root.layout.work / 'memory/status.json').read_bytes())['condition'], 'live')

    @needs_codex
    def test_meta_command_takes_every_task_call_no_round_had(self):
        with Root() as root:
            root.add_task('T1')
            root.model.push(say('a'), say('b'), say(decision('no_change')), say(decision('no_change')))
            for _ in range(2):
                self.assertEqual(run('task', 'T1', '--no-meta')[0], 0)
            self.assertEqual(run('meta')[0], 0)
            code, out, _ = run('meta')
            self.assertEqual((code, 'There are no new task runs' in out), (0, True))
            self.assertEqual(os.listdir(root.layout.records / 'rounds'), ['M0001'])
            round_record = json.loads((root.layout.records / 'rounds' / 'M0001' / 'round.json').read_text())
            self.assertEqual(round_record['trigger'], ['R0001', 'R0002'])


class CheckWords(unittest.TestCase):
    def test_a_message_that_only_repeats_the_verdict_is_not_shown_again(self):
        """Input data in Chinese: the messages the check scripts printed before the program spoke English, which the
        records of an older root still hold."""
        for message in ('passed', '通过'):
            self.assertEqual(cli._check_words({'passed': True, 'message': message}), 'check passed')
        for message in ('not passed', '没通过'):
            self.assertEqual(cli._check_words({'passed': False, 'message': message}), 'check not passed')
        self.assertEqual(cli._check_words({'passed': False, 'message': 'answer.json is missing'}),
                         'check not passed: answer.json is missing')
        self.assertEqual(cli._check_words(None), '')


if __name__ == '__main__':
    unittest.main()
