import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from rsi.codexrun import codex_program
from rsi.compose import lab_config, load_envelope, meta_config, profiles, write_home_config
from rsi.paths import Layout, Refused
from rsi.settings import load_settings
from rsi.tomlout import render_file, render_flags
from tests.support import needs_codex

LITERATURE = 'name = "literature"\ndescription = "d"\ndeveloper_instructions = "i"\n'


class ComposeTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        self.layout = Layout(root)
        self.envelope = {'approval_policy': 'on-request', 'features': {'apps': True, 'plugins': True},
                         'apps': {'_default': {'enabled': False}, 'conn_a': {'enabled': True}},
                         'plugins': {'_default': {'enabled': False}, 'pdf@x': {'enabled': True}},
                         'rsi': {'extra_read': ['/opt/x']}}
        patcher = mock.patch.dict(os.environ); patcher.start(); self.addCleanup(patcher.stop)
        os.environ.pop('RSI_CODEX', None)

    def settings(self, text=''):
        (self.layout.work / 'codex').mkdir(parents=True, exist_ok=True)
        (self.layout.work / 'codex' / 'settings.toml').write_text(text)
        return load_settings(self.layout.work)

    def test_task_run(self):
        c = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        fs = c['permissions']['lab']['filesystem']; root = str(self.layout.root)
        self.assertEqual((c['approval_policy'], c['default_permissions']), ('never', 'lab'))
        self.assertEqual((fs[':minimal'], fs[root], fs[root + '/work'], fs[root + '/work/.codex']),
                         ('read', 'deny', 'write', 'read'))
        self.assertEqual((fs[root + '/library'], fs[root + '/env'], fs['/opt/x']), ('write', 'write', 'read'))
        self.assertEqual(c['permissions']['lab']['network'], {'enabled': False})
        self.assertEqual(sorted(c['permissions']), ['lab', 'lab_net', 'trial', 'trial_net'])
        self.assertNotIn(root + '/work', c['permissions']['trial']['filesystem'])
        self.assertNotIn('network_proxy', c['features'])
        tomllib.loads(render_file(c))

    def test_network_and_trial(self):
        c = lab_config(self.layout, self.envelope, self.settings('network = true\n'), trial=True)
        self.assertEqual(c['default_permissions'], 'trial_net'); self.assertIs(c['features']['network_proxy'], True)
        self.assertEqual(c['permissions']['trial_net']['network'],
                         {'enabled': True, 'mode': 'full', 'domains': {'*': 'allow'}})
        self.assertTrue(c['shell_environment_policy']['set']['TMPDIR'].endswith('/trial/.tmp'))

    def test_tool_server_is_wrapped(self):
        text = ('[tool_servers.lit]\ncommand = "python3"\nargs = ["tools/lit/server.py"]\nnetwork = true\n'
                '[tool_servers.far]\nurl = "https://e/mcp"\n')
        c = lab_config(self.layout, self.envelope, self.settings(text), trial=False)
        lit = c['mcp_servers']['lit']
        self.assertEqual(lit['command'], 'codex')
        self.assertEqual(lit['args'][:6], ['sandbox', '-P', 'lab_net', '--enable', 'network_proxy', '--'])
        self.assertEqual(lit['args'][6:], ['python3', str(self.layout.work / 'tools/lit/server.py')])
        self.assertEqual(lit['env']['CODEX_HOME'], str(self.layout.lab_home))
        self.assertEqual(c['mcp_servers']['far'], {'url': 'https://e/mcp'})
        self.assertEqual(c['default_permissions'], 'lab')           # commands stay offline

    def test_disable_and_roles(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('[disable]\nconnectors = ["conn_a", "nope"]\nplugins = ["pdf@x"]\n')
        c = lab_config(self.layout, self.envelope, s, trial=False)
        self.assertEqual((c['apps']['conn_a'], c['plugins']['pdf@x']), ({'enabled': False}, {'enabled': False}))
        self.assertTrue(any('nope' in p for p in s.problems))
        self.assertEqual(c['agents']['literature']['config_file'],
                         str(self.layout.work / 'codex/agents/literature.toml'))

    def test_meta(self):
        c = meta_config(self.layout, self.envelope, {'model_reasoning_effort': 'low'})
        fs = c['permissions']['meta']['filesystem']
        root = str(self.layout.root)
        self.assertEqual((fs[root], fs[root + '/records'], fs[root + '/work'], fs[root + '/meta']),
                         ('deny', 'read', 'read', 'write'))
        self.assertEqual((c['features'], c['model_reasoning_effort']), ({'apps': False, 'plugins': False}, 'low'))

    @needs_codex
    def test_the_real_sandbox_accepts_the_flags(self):
        for d in ('work', 'records', 'codex-home'): (self.layout.root / d).mkdir(exist_ok=True)
        config = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        flags = render_flags({'permissions': config['permissions']})

        def sandboxed(command):
            return subprocess.run([codex_program(), 'sandbox', *flags, '-P', 'lab', '--', '/bin/sh', '-c', command],
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.lab_home)},
                                  capture_output=True).returncode
        self.assertEqual(sandboxed('echo x > ' + str(self.layout.work / 'a.txt')), 0)
        self.assertNotEqual(sandboxed('echo x > ' + str(self.layout.root / 'records' / 'a.txt')), 0)

    # Beyond the brief.

    def test_everything_in_a_task_run(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('model = "m"\nmodel_reasoning_effort = "high"\nweb_search = "cached"\n'
                          '[agents]\nmax_depth = 2\nmax_threads = 5\n')
        c = lab_config(self.layout, self.envelope, s, trial=False)
        env = str(self.layout.env)
        self.assertEqual((c['model'], c['model_reasoning_effort'], c['web_search']), ('m', 'high', 'cached'))
        self.assertEqual(c['agents'], {'max_depth': 2, 'max_concurrent_threads_per_session': 5,
                                       'literature': {'description': 'd', 'config_file':
                                                      str(self.layout.work / 'codex/agents/literature.toml')}})
        path = env + '/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
        self.assertEqual(c['shell_environment_policy'],
                         {'inherit': 'core', 'set': {'TMPDIR': str(self.layout.work / '.tmp'), 'PATH': path}})
        self.assertEqual((c['apps'], c['plugins']), (self.envelope['apps'], self.envelope['plugins']))
        self.assertEqual((c['features'], c['mcp_servers']), ({'apps': True, 'plugins': True}, {}))
        fs = c['permissions']['lab']['filesystem']; home = Path.home().resolve()
        self.assertEqual((fs[str(self.layout.lab_home / 'plugins/cache')], fs[str(home / '.codex')],
                          fs[str(home / '.codex-rsi-lab')]), ('read', 'deny', 'deny'))
        self.assertNotIn(str(self.layout.records), fs)
        self.assertEqual(c['permissions']['lab_net']['filesystem'], fs)
        for folder in ('/opt/homebrew/bin', '/opt/homebrew/etc/fonts'):
            self.assertEqual(fs.get(folder), 'read' if os.path.exists(folder) else None, folder)
        self.assertEqual(lab_config(self.layout, self.envelope, s, trial=False, interactive=True)['approval_policy'],
                         'on-request')
        self.assertEqual(self.envelope['apps']['conn_a'], {'enabled': True})

    def test_trial_paths(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('[tool_servers.lit]\ncommand = "tools/lit/run"\nargs = ["-v", "x.py", "notes.md"]\n')
        (self.layout.trial).mkdir(); (self.layout.trial / 'notes.md').write_text('n')
        c = lab_config(self.layout, self.envelope, s, trial=True)
        trial = self.layout.trial
        self.assertEqual(c['mcp_servers']['lit']['args'],
                         ['sandbox', '-P', 'trial', '--', str(trial / 'tools/lit/run'), '-v', 'x.py',
                          str(trial / 'notes.md')])
        self.assertEqual(c['mcp_servers']['lit']['env']['TMPDIR'], str(trial / '.tmp'))
        self.assertEqual((c['mcp_servers']['lit']['default_tools_approval_mode'],
                          c['mcp_servers']['lit']['startup_timeout_sec']), ('approve', 30))
        self.assertEqual(c['agents']['literature']['config_file'], str(trial / 'codex/agents/literature.toml'))
        self.assertNotIn('network_proxy', c['features'])
        fs = c['permissions']['trial']['filesystem']
        self.assertEqual((fs[str(trial)], fs[str(trial / '.codex')]), ('write', 'read'))

    def test_codex_program_names_the_wrapper(self):
        os.environ['RSI_CODEX'] = '/x/codex'
        c = lab_config(self.layout, self.envelope, self.settings('[tool_servers.t]\ncommand = "c"\n'), trial=False)
        self.assertEqual(c['mcp_servers']['t']['command'], '/x/codex')

    def test_disabling_twice_reports_once(self):
        s = self.settings('[disable]\nplugins = ["nope"]\n')
        for _ in range(2):
            lab_config(self.layout, self.envelope, s, trial=False)
        self.assertEqual(len([p for p in s.problems if 'nope' in p]), 1)

    def test_extra_read_cannot_open_the_root_or_the_homes(self):
        home = Path.home().resolve()
        for folder in (str(self.layout.records), str(self.layout.root), str(home / '.codex' / 'x'), 'relative/x'):
            envelope = dict(self.envelope, rsi={'extra_read': [folder]})
            with self.assertRaises(Refused, msg=folder):
                profiles(self.layout, envelope)

    def test_meta_everything(self):
        c = meta_config(self.layout, self.envelope, {})
        fs = c['permissions']['meta']['filesystem']; root = str(self.layout.root)
        self.assertEqual((c['approval_policy'], c['default_permissions'], c['web_search']), ('never', 'meta', 'live'))
        self.assertEqual((fs[':minimal'], fs[root + '/library'], fs[root + '/env'], fs[root + '/meta/.codex']),
                         ('read', 'read', 'read', 'read'))
        self.assertNotIn(root + '/tasks', fs); self.assertNotIn(root + '/trial', fs); self.assertNotIn('/opt/x', fs)
        self.assertEqual(c['permissions']['meta']['network'], {'enabled': False})
        self.assertEqual(c['shell_environment_policy']['set']['TMPDIR'], str(self.layout.meta / '.tmp'))
        self.assertNotIn('model', c); self.assertNotIn('model_reasoning_effort', c)
        self.assertEqual(meta_config(self.layout, self.envelope, {'model': 'm'})['model'], 'm')
        tomllib.loads(render_file(c))

    def test_home_config(self):
        write_home_config(self.layout, self.envelope, self.settings('network = true\n'))
        written = tomllib.loads((self.layout.lab_home / 'config.toml').read_text())
        expected = tomllib.loads(render_file(lab_config(self.layout, self.envelope, self.settings('network = true\n'),
                                                        trial=False, interactive=True)))
        self.assertEqual(written, expected)
        self.assertEqual((written['approval_policy'], written['default_permissions']), ('on-request', 'lab_net'))
        self.assertEqual(sorted(p.name for p in self.layout.lab_home.iterdir()), ['config.toml'])

    def test_envelope(self):
        owner = self.layout.owner; owner.mkdir()
        (owner / 'lab-envelope.toml').write_text('approval_policy = "on-request"\n[features]\napps = true\n'
                                                 '[apps.conn_a]\nenabled = true\n[rsi]\nextra_read = ["/opt/x"]\n')
        envelope = load_envelope(owner)
        self.assertEqual(envelope, {'approval_policy': 'on-request', 'features': {'apps': True},
                                    'apps': {'conn_a': {'enabled': True}}, 'plugins': {},
                                    'rsi': {'extra_read': ['/opt/x']}})
        for text in ('sandbox_mode = "x"\n', 'not toml [[[', 'apps = 1\n', '[rsi]\nextra_read = "/opt/x"\n'):
            (owner / 'lab-envelope.toml').write_text(text)
            with self.assertRaises(Refused, msg=text):
                load_envelope(owner)
        (owner / 'lab-envelope.toml').unlink()
        with self.assertRaises(Refused):
            load_envelope(owner)

    @needs_codex
    def test_the_real_codex_accepts_whole_configurations(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('model = "m"\nmodel_reasoning_effort = "low"\nweb_search = "live"\nnetwork = true\n'
                          '[agents]\nmax_depth = 2\nmax_threads = 4\n[disable]\nplugins = ["pdf@x"]\n'
                          '[tool_servers.lit]\ncommand = "python3"\nargs = ["tools/lit/server.py"]\n'
                          '[tool_servers.far]\nurl = "https://e/mcp"\nbearer_token_env_var = "RSI_TOOL_FAR"\n')
        self.layout.meta.mkdir(); (self.layout.root / 'scratch-home').mkdir()
        for config in (lab_config(self.layout, self.envelope, s, trial=False),
                       lab_config(self.layout, self.envelope, s, trial=False, interactive=True),
                       meta_config(self.layout, self.envelope, {'model': 'm', 'model_reasoning_effort': 'low'})):
            done = subprocess.run([codex_program(), 'sandbox', *render_flags(config), '--', '/usr/bin/true'],
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.root / 'scratch-home')},
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
