import errno
import importlib.machinery
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from rsi import cli
from rsi.check import (CHECK_FILE, PROBE, SHIPPED_SERVER, _command_layer, _Context, _edit_layer, _make_scratch,
                       _remove_scratch, _Sockets, _static_problems, gate, owner_fingerprints, program_fingerprints,
                       run_check)
from rsi.codexrun import PROXY_VARIABLES, codex_version
from rsi.compose import NETWORK_ON, SYSTEM_PATH, load_envelope, native_codex
from rsi.labrun import run_task
from rsi.meta import run_round
from rsi.paths import Layout, Refused
from tests.support import Root, needs_codex, plant_programs
from tests.test_cli import run

WANTED = (('lab', 'read_records'), ('lab_net', 'local_tcp'), ('lab_net', 'local_unix_socket'), ('trial', 'write_work'),
          ('meta', 'write_records'), ('lab', 'edit_via_symlink'), ('meta', 'edit_existing_record'),
          ('trial', 'write_env'), ('meta', 'replace_own_root'), ('lab', 'dot_codex_link'), ('meta', 'dot_codex_link'),
          ('meta', 'meta_agents_md_not_loaded'), ('lab', 'lab_agents_md_loaded'),
          ('trial_net', 'write_library'), ('meta', 'write_candidate'), ('lab', 'write_user_temp'),
          ('meta', 'zsh_heredoc'), ('lab', 'tool_server_wrapper'), ('lab_net', 'tool_server_wrapper'),
          ('lab_net', 'literature_env'), ('lab_net', 'literature_library'), ('lab_net', 'literature_local_tcp'),
          ('trial_net', 'literature_env'), ('trial_net', 'literature_library'), ('trial_net', 'literature_local_tcp'),
          ('lab', 'delete_existing_record'), ('meta', 'edit_dot_codex'), ('lab', 'edit_dot_codex'),
          ('lab', 'edit_dot_agents'), ('meta', 'edit_dot_agents'), ('lab', 'update_inside'), ('meta', 'delete_inside'),
          ('lab', 'read_control'), ('meta', 'read_control'), ('lab', 'read_meta'), ('trial_net', 'read_meta'),
          ('lab_net', 'read_trial'), ('meta', 'read_trial'), ('trial', 'write_tools'), ('meta', 'write_tools'),
          # shell commands through `codex exec`, the path the agents' commands take
          ('lab', 'shell_write_inside'), ('lab', 'shell_write_records'), ('lab', 'shell_read_task_check'),
          ('lab', 'shell_read_owner'), ('lab', 'shell_allow_rule_outside'), ('lab', 'shell_rules_not_loaded'),
          ('meta', 'shell_write_inside'), ('meta', 'shell_write_work'), ('meta', 'shell_write_records'),
          ('meta', 'shell_read_owner'), ('meta', 'shell_allow_rule_outside'), ('meta', 'shell_rules_not_loaded'),
          # without network: no proxy, no way out; the seed's networked tool server: its proxy, no local service
          ('lab', 'proxy_variables'), ('lab', 'connect_out'), ('trial', 'proxy_variables'), ('trial', 'connect_out'),
          ('meta', 'proxy_variables'), ('meta', 'connect_out'), ('lab', 'shell_proxy_variables'),
          ('lab', 'shell_connect_out'), ('lab', 'shell_local_tcp'), ('meta', 'shell_proxy_variables'),
          ('lab', 'tool_server_proxy'), ('lab', 'tool_server_local_tcp'), ('lab_net', 'literature_proxy_variables'),
          ('lab_net', 'literature_read_task_check'), ('lab_net', 'literature_read_owner'),
          ('trial_net', 'literature_read_task_check'), ('trial_net', 'literature_read_owner'))
# Every create case tries mkdir, rename-to and a link, for each spelling (three letter cases; for .agents also .agentſ,
# with a long s, which the file system takes for .agents), in every writable root of its profile: lab work/, library/,
# env/; trial trial/; meta meta/, candidate/.
CREATE_ATTEMPTS = {'create_dot_codex': {'lab': 27, 'lab_net': 27, 'trial': 9, 'trial_net': 9, 'meta': 18},
                   'create_dot_agents': {'lab': 36, 'lab_net': 36, 'trial': 12, 'trial_net': 12, 'meta': 24}}
INFO = {('meta', 'meta_planted_skill_loaded'), ('lab', 'lab_can_create_dot_agents'),
        ('lab', 'lab_can_write_in_existing_dot_agents'), ('lab', 'edit_layer_without_apps_and_plugins'),
        ('meta', 'edit_layer_without_apps_and_plugins')}
# The owner's connectors on: the edit layer must still run (with them off) and the check pass.
APPS_ON = 'approval_policy = "never"\n\n[features]\napps = true\nplugins = false\n\n[apps.x]\nenabled = true\n'


def tree(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob('*')}


@contextmanager
def check_context(root: Root):
    """What run_check gives its layers, for one layer at a time: (context, scratch folder), removed afterwards."""
    layout = root.layout
    scratch, sockets = _make_scratch(layout), _Sockets()
    try:
        yield _Context(layout.root, native_codex(layout), shutil.which('python3', path=SYSTEM_PATH),
                       load_envelope(layout.owner), f'RSI-CHECK-TOKEN-{secrets.token_hex(8)}', sockets,
                       shutil.which('pdftotext', path=SYSTEM_PATH), [], []), scratch
    finally:
        sockets.remove()
        _remove_scratch(layout, scratch)


def scratch_left(root: Root) -> list[str]:
    staging = root.layout.staging
    return [name for name in os.listdir(staging) if name.startswith('check-')] if staging.exists() else []


def sockets_left() -> set[str]:
    return {name for name in os.listdir('/tmp') if name.startswith('rsi-check-')}


@needs_codex
class Check(unittest.TestCase):
    def test_a_fresh_root_passes_with_the_owners_connectors_on(self):
        home, sockets = set(os.listdir(Path.home())), sockets_left()
        with Root(passed_check=False) as root:
            (root.layout.owner / 'lab-envelope.toml').write_text(APPS_ON)
            marker = root.path / 'planted-ran'                      # programs an agent could plant on the PATH
            plant_programs(root.layout.env / 'bin', marker)
            os.environ['PATH'] = f'{root.layout.env / "bin"}{os.pathsep}{os.environ["PATH"]}'     # Root restores it
            self.assertIsNotNone(gate(root.layout))
            work, records = tree(root.layout.work), tree(root.layout.records)
            report = run_check(root.layout)
            failed = [(r['profile'], r['name'], r['observed'], r['detail']) for r in report['results'] if not r['ok']]
            self.assertEqual(failed, [])
            self.assertEqual(report['problems'], []); self.assertTrue(report['passed'])
            self.assertEqual(report['codex_version'], codex_version())
            names = {(r['profile'], r['name']) for r in report['results']}
            for wanted in WANTED:
                self.assertIn(wanted, names)
            self.assertFalse({('lab_net', 'proxy_variables'), ('trial_net', 'connect_out')} & names)
            self.assertEqual(report['codex_program'], native_codex(root.layout))
            self.assertTrue(all(r['expected'] in ('allowed', 'blocked') for r in report['results']))
            for name, attempts in CREATE_ATTEMPTS.items():
                for profile, count in attempts.items():
                    [r] = [r for r in report['results'] if (r['profile'], r['name']) == (profile, name)]
                    self.assertEqual(r['detail'].count(' blocked'), count, r)
            self.assertEqual({(i['profile'], i['name']) for i in report['info']}, INFO)
            self.assertTrue(all(i['value'] in (True, False, None) for i in report['info']))
            [apps] = [i for i in report['info']
                      if (i['profile'], i['name']) == ('lab', 'edit_layer_without_apps_and_plugins')]
            self.assertTrue(apps['value']); self.assertIn('features.apps = true', apps['detail'])
            self.assertEqual(report['owner_files'], owner_fingerprints(root.layout))
            self.assertIsNone(report['owner_files']['meta/settings.toml'])
            self.assertEqual(report['program'], program_fingerprints())
            self.assertIn('check.py', report['program'])
            self.assertIsNone(gate(root.layout))
            path = root.layout.records / CHECK_FILE
            self.assertEqual(json.loads(path.read_text()), report)
            # A passing check holds only for the code that ran it.
            text = path.read_text(); data = json.loads(text); data['program']['labrun.py'] = '0' * 64
            path.write_text(json.dumps(data))
            reason = gate(root.layout)
            self.assertIn('labrun.py', reason); self.assertIn('rsi check', reason)
            path.write_text(text)
            self.assertIsNone(gate(root.layout))
            self.assertEqual([p.name for p in root.layout.records.iterdir() if p.name.startswith('.rsi-check')], [])
            # The check worked in a scratch root that is gone now, and left the real folders alone.
            self.assertEqual(scratch_left(root), [])
            self.assertEqual(tree(root.layout.work), work)
            self.assertEqual(tree(root.layout.records) - records, {CHECK_FILE, 'index.jsonl'})
            lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
            self.assertEqual([json.loads(line)['kind'] for line in lines], ['check'])
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else '')
        self.assertEqual(sockets_left(), sockets)
        new = set(os.listdir(Path.home())) - home
        self.assertEqual([name for name in new if 'rsi' in name.lower() or 'codex' in name.lower()], [])

    def test_a_hole_is_found(self):
        with Root(passed_check=False) as root, mock.patch('rsi.compose.root_rule', return_value='read'):
            report = run_check(root.layout)
            self.assertFalse(report['passed'])
            observed = [(r['profile'], r['name'], r['observed']) for r in report['results']]
            self.assertIn(('lab', 'read_records', 'allowed'), observed)
            self.assertIn(('lab', 'shell_read_owner', 'allowed'), observed)          # seen through `codex exec` too
            self.assertIsNotNone(gate(root.layout))
            self.assertEqual(scratch_left(root), [])

    def test_the_network_cases_find_a_proxy_where_there_must_be_none_and_none_where_there_must_be_one(self):
        """The cases of the configuration the seed ships cannot pass vacuously. Weakened: the Lab's commands get
        network (and the `lab` profile a network table), and the seed's tool server loses its own."""
        weakened = 'network = true\n' + SHIPPED_SERVER.replace('network = true', 'network = false')
        with Root(passed_check=False) as root, check_context(root) as (context, scratch), \
                mock.patch('rsi.check.SHIPPED_SERVER', weakened):
            with mock.patch('rsi.compose.NETWORK_OFF', NETWORK_ON):
                commands = {r['name']: r for r in _command_layer(context, scratch, 'lab')}
            self.assertEqual(commands['proxy_variables']['observed'], 'allowed', commands['proxy_variables'])
            self.assertFalse(commands['proxy_variables']['ok'])
            edits = {r['name']: r for r in _edit_layer(context, scratch, 'lab')}
            self.assertEqual(edits['shell_proxy_variables']['observed'], 'allowed', edits['shell_proxy_variables'])
            self.assertEqual(edits['tool_server_proxy']['observed'], 'blocked', edits['tool_server_proxy'])
            self.assertFalse(edits['shell_proxy_variables']['ok'] or edits['tool_server_proxy']['ok'])
            self.assertEqual(edits['edit_inside']['observed'], 'allowed', edits['edit_inside'])    # the run worked

    def test_a_codex_inside_the_root_is_refused_before_any_case(self):
        with Root(passed_check=False) as root:
            native = native_codex(root.layout)
            link = root.path / 'tools' / 'codex'; link.parent.mkdir(); link.symlink_to(native)
            os.environ['RSI_CODEX'] = str(link)                     # Root restores the environment
            # Judged against a scratch root inside the real one, it would have passed:
            self.assertEqual(native_codex(Layout(root.layout.staging / 'check-x')), native)
            report = run_check(root.layout)
            self.assertEqual((report['passed'], report['results']), (False, []))
            self.assertTrue(any('RSI_CODEX' in line for line in report['problems']), report['problems'])
            self.assertFalse(root.layout.staging.exists())
            self.assertIsNotNone(gate(root.layout))


class Probe(unittest.TestCase):
    """The probe itself, run without a sandbox: how its outcomes are combined."""

    def probe(self, cases: list[dict], net: bool) -> dict:
        env = {key: value for key, value in os.environ.items() if key not in PROXY_VARIABLES}
        done = subprocess.run([sys.executable, '-I', '-B', '-c', PROBE,
                               json.dumps({'token': 't', 'net': net, 'cases': cases})],
                              capture_output=True, text=True, timeout=60, env=env)
        lines = [json.loads(line[len('RSI-CHECK '):]) for line in done.stdout.splitlines()]
        return {line['name']: line for line in lines}

    def test_a_proxy_route_that_cannot_be_tried_is_unknown_in_a_network_profile(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        locked = Path(folder.name) / 'locked'; locked.write_text('x'); locked.chmod(0)
        if os.access(locked, os.R_OK):
            self.skipTest('this user reads files whatever their mode')
        case = {'name': 'local_tcp', 'steps': [['read', str(locked)], ['http_proxy', 9], ['socks_proxy', 9]]}
        offline = self.probe([case], net=False)['local_tcp']
        self.assertEqual(offline['observed'], 'blocked', offline)            # no proxy to try: only the direct step
        online = self.probe([case], net=True)['local_tcp']
        self.assertEqual(online['observed'], 'unknown', online)
        self.assertIn('HTTP_PROXY', online['detail']); self.assertIn(errno.errorcode[errno.EACCES], online['detail'])


@needs_codex
class Gate(unittest.TestCase):
    def test_gate_follows_the_codex_version(self):
        with Root() as root:
            self.assertIsNone(gate(root.layout))
            path = root.layout.records / CHECK_FILE
            data = json.loads(path.read_text()); data['codex_version'] = '0.0.1'
            path.write_text(json.dumps(data))
            self.assertIn('rsi check', gate(root.layout))
            root.add_task('T1')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            with self.assertRaises(Refused): run_round(root.layout, [])
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_gate_follows_the_codex_program(self):
        with Root() as root:
            root.add_task('T1')
            path = root.layout.records / CHECK_FILE
            text = path.read_text()
            measured = json.loads(text)['codex_program']
            self.assertEqual(measured, native_codex(root.layout))
            self.assertIsNone(gate(root.layout))
            for program in ('/elsewhere/bin/codex', None):          # another program, or a check that has none
                data = json.loads(text); data['codex_program'] = program; path.write_text(json.dumps(data))
                reason = gate(root.layout)
                self.assertIn('Codex program', reason); self.assertIn('rsi check', reason)
                with self.assertRaises(Refused): run_task(root.layout, 'T1')
            path.write_text(text)
            self.assertIsNone(gate(root.layout))
            # A PATH that finds another install (here Homebrew's, when there is one): never the measured program.
            if os.path.isfile('/opt/homebrew/bin/codex'):
                os.environ.pop('RSI_CODEX', None)                   # Root restores the environment
                os.environ['PATH'] = '/opt/homebrew/bin' + os.pathsep + os.environ['PATH']
                if native_codex(root.layout) != measured:
                    self.assertIn('rsi check', gate(root.layout))
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_no_check_a_failed_check_or_an_unreadable_one_stops_every_agent(self):
        with Root(passed_check=False) as root:
            root.add_task('T1')
            path = root.layout.records / CHECK_FILE
            failed = {'codex_version': codex_version(), 'passed': False, 'at': 'x', 'results': [], 'problems': []}
            for content in (None, json.dumps(failed), '{not json', '[]', json.dumps({**failed, 'passed': 'yes'})):
                if content is not None:
                    path.write_text(content)
                reason = gate(root.layout)
                self.assertIn('rsi check', reason, content)
                with self.assertRaises(Refused, msg=content) as refusal:
                    run_task(root.layout, 'T1')
                self.assertIn('rsi check', str(refusal.exception))
                with self.assertRaises(Refused, msg=content):
                    run_round(root.layout, [])
            path.unlink(); path.symlink_to(root.layout.owner / 'limits.toml')      # never followed
            self.assertIn('rsi check', gate(root.layout))
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_gate_notices_when_the_owner_files_change(self):
        with Root() as root:
            root.add_task('T1')
            self.assertIsNone(gate(root.layout))
            envelope, settings = root.layout.owner / 'lab-envelope.toml', root.layout.owner / 'meta' / 'settings.toml'
            before = envelope.read_text()
            envelope.write_text(before + '# changed by hand\n')
            reason = gate(root.layout)
            self.assertIn('owner/lab-envelope.toml', reason); self.assertIn('rsi check', reason)
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            envelope.write_text(before)
            self.assertIsNone(gate(root.layout))
            settings.parent.mkdir(exist_ok=True); settings.write_text('model_reasoning_effort = "low"\n')
            self.assertIn('owner/meta/settings.toml', gate(root.layout))          # absent when checked, there now
            with self.assertRaises(Refused): run_round(root.layout, [])
            settings.unlink()
            self.assertIsNone(gate(root.layout))
            path = root.layout.records / CHECK_FILE
            data = json.loads(path.read_text()); del data['owner_files']; path.write_text(json.dumps(data))
            self.assertIn('rsi check', gate(root.layout))                          # a check that did not record them
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_gate_notices_when_the_programs_code_changes(self):
        with Root() as root:
            root.add_task('T1')
            path = root.layout.records / CHECK_FILE
            text = path.read_text()
            self.assertEqual(json.loads(text)['program'], program_fingerprints())
            self.assertIsNone(gate(root.layout))
            changes = {'changed': lambda program: program.update({'compose.py': '0' * 64}),
                       'missing': lambda program: program.pop('meta.py'),
                       'new': lambda program: program.update({'gone.py': '0' * 64})}
            for what, change in changes.items():
                data = json.loads(text); change(data['program']); path.write_text(json.dumps(data))
                reason = gate(root.layout)
                self.assertIn('rsi check', reason, what)
                self.assertIn({'changed': 'compose.py', 'missing': 'meta.py', 'new': 'gone.py'}[what], reason)
                with self.assertRaises(Refused, msg=what): run_task(root.layout, 'T1')
            path.write_text(text)
            self.assertIsNone(gate(root.layout))
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))


class Shipped(unittest.TestCase):
    def test_the_check_measures_the_tool_servers_the_seed_ships(self):
        seed = Path(__file__).resolve().parents[1] / 'seed' / 'work' / 'codex' / 'settings.toml'
        shipped = tomllib.loads(seed.read_text())['tool_servers']
        self.assertEqual(tomllib.loads(SHIPPED_SERVER), {'tool_servers': shipped})


class Static(unittest.TestCase):
    def test_a_root_holding_what_codex_loads_fails_the_check(self):
        # The root is a git repository, so Codex may take it for the project root of work/ and load these from it.
        with Root(passed_check=False) as root:
            self.assertEqual(_static_problems(root.layout, None), [])
            for name in ('AGENTS.md', 'AGENTS.override.md', '.agents', '.codex'):
                path = root.path / name
                path.mkdir() if name.startswith('.') else path.write_text('x')
                problems = _static_problems(root.layout, None)
                self.assertTrue(any(name in line and 'git' in line for line in problems), problems)
                path.rmdir() if name.startswith('.') else path.unlink()
            (root.path / 'AGENTS.md').write_text('x')
            with mock.patch('rsi.check._cases', return_value=[Command.RESULT]):
                report = run_check(root.layout)
            self.assertFalse(report['passed'])
            self.assertTrue(any('AGENTS.md' in line for line in report['problems']), report['problems'])


class Fingerprints(unittest.TestCase):
    def test_everything_python_could_import_from_the_program_folder_is_covered(self):
        tmp = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, tmp)
        (tmp / 'a.py').write_text('x = 1\n')
        with mock.patch('rsi.check.PROGRAM', tmp):
            before = program_fingerprints()
            self.assertEqual(list(before), ['a.py'])
            (tmp / '__pycache__').mkdir(); (tmp / '__pycache__' / 'a.cpython-314.pyc').write_bytes(b'x')   # never read
            (tmp / '.DS_Store').write_bytes(b'x')                                                    # never imported
            self.assertEqual(program_fingerprints(), before)
            planted = {'a.pyc': lambda path: path.write_bytes(b'x'),               # loaded beside its source too
                       'a' + importlib.machinery.EXTENSION_SUFFIXES[0]: lambda path: path.write_bytes(b'x'),
                       'b': lambda path: path.mkdir(),                              # a package or namespace portion
                       'c.py': lambda path: path.symlink_to(tmp / 'a.py')}
            for name, make in planted.items():
                make(tmp / name)
                self.assertIn(name, program_fingerprints())
            self.assertEqual(program_fingerprints()['a.py'], before['a.py'])


class Command(unittest.TestCase):
    RESULT ={'name': 'read_records', 'profile': 'lab', 'expected': 'blocked', 'observed': 'blocked', 'ok': True,
              'detail': ''}

    def report(self, *results, problems=()):
        return {'codex_version': '9.9.9', 'owner_files': {}, 'program': {},
                'passed': all(r['ok'] for r in results) and not problems, 'at': 'x', 'results': list(results),
                'problems': list(problems), 'notes': [],
                'info': [{'name': 'meta_planted_skill_loaded', 'profile': 'meta', 'value': True, 'detail': 'd'}]}

    def test_check_prints_each_failed_case_or_all_passed(self):
        with Root(passed_check=False) as root:
            with mock.patch.object(cli, 'run_check', return_value=self.report(self.RESULT)) as check:
                code, out, _ = run('check')
            self.assertEqual((code, check.call_args.args[0].root), (0, root.layout.root))
            self.assertIn('All passed (Codex 9.9.9, 1 cases)', out)
            self.assertIn('Information (not a case): meta meta_planted_skill_loaded = yes (d)', out)
            hole = dict(self.RESULT, observed='allowed', ok=False, detail='read it\x1b[2J')
            gone = dict(self.RESULT, name='local_tcp', profile='lab_net', observed='unknown', ok=False,
                        detail='timed out')
            with mock.patch.object(cli, 'run_check', return_value=self.report(self.RESULT, hole, gone,
                                                                              problems=['the root path is bad'])):
                code, out, _ = run('check')
            self.assertEqual(code, 1); self.assertNotIn('All passed', out); self.assertNotIn('\x1b', out)
            failed = [line for line in out.splitlines() if 'read_records' in line or 'local_tcp' in line]
            self.assertEqual(len(failed), 2, out)
            self.assertEqual(failed, ['lab read_records: expected blocked, observed allowed (read it\\x1b[2J)',
                                      'lab_net local_tcp: expected blocked, observed unknown (timed out)'])
            self.assertIn('Problem: the root path is bad', out)

    def test_status_shows_the_gates_verdict_not_the_files_word(self):
        with Root(passed_check=False) as root:
            self.assertIsNone(json.loads(run('status', '--json')[1])['check'])
            self.assertIn('Isolation check: rsi check has not been run yet', run('status')[1])
            # It says passed, but not for these owner files, this code or this Codex: the gate's reason is shown.
            (root.layout.records / CHECK_FILE).write_text(json.dumps(self.report(self.RESULT)))
            check = json.loads(run('status', '--json')[1])['check']
            self.assertEqual((check['codex_version'], check['passed']), ('9.9.9', False))
            self.assertIn('rsi check', check['reason'])
            out = run('status')[1]
            self.assertIn('Isolation check: not passed: ', out); self.assertNotIn('Isolation check: passed', out)
            (root.layout.records / CHECK_FILE).write_text('{not json')
            self.assertIn('Isolation check: not passed: ', run('status')[1])

    @needs_codex
    def test_status_says_passed_only_when_the_gate_does_and_never_follows_a_link(self):
        with Root() as root:
            check = json.loads(run('status', '--json')[1])['check']
            self.assertEqual((check['passed'], check['reason'], check['codex_version']), (True, None, codex_version()))
            self.assertIn(f'Isolation check: passed (Codex {codex_version()}, ', run('status')[1])
            path = root.layout.records / CHECK_FILE
            elsewhere = root.path / 'elsewhere.json'; elsewhere.write_text(path.read_text())
            path.unlink(); path.symlink_to(elsewhere)
            check = json.loads(run('status', '--json')[1])['check']
            self.assertEqual((check['passed'], check['codex_version']), (False, None))
            self.assertIn('Isolation check: not passed: ', run('status')[1])


if __name__ == '__main__':
    unittest.main()
