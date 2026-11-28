import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.codexrun import codex_environment
from rsi.compose import WINDOWS_SERVER_START, lab_config, meta_config, tool_server_problems
from rsi.paths import Layout
from rsi.settings import Settings
from rsi.tomlout import render_file


@unittest.skipUnless(os.name == 'nt', 'native Windows profile and command-line conventions')
class WindowsProfileTests(unittest.TestCase):
    def test_mxc_host_environment_reaches_native_process(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory).resolve())
            system_root = os.environ['SYSTEMROOT']
            expected = [os.environ.get('SYSTEMDRIVE') or Path(system_root).drive, os.environ['LOCALAPPDATA'], system_root]
            settings = Settings(tool_servers={'memory': {'command': 'python3', 'args': [], 'network': False}})
            for trial in (False, True):
                config = lab_config(layout, {}, settings, trial=trial, codex=sys.executable)
                host = codex_environment(layout.lab_home, secrets=False, root=layout.root)
                for env in (host, {**host, **config['shell_environment_policy']['set']},
                            config['mcp_servers']['memory']['env']):
                    child = subprocess.run(
                        [sys.executable, '-I', '-X', 'utf8', '-c', 'import os; '
                         'print(*(os.getenv(k, "") for k in ("SystemDrive", "LOCALAPPDATA", "SystemRoot")), sep="\\n")'],
                        env=env, capture_output=True, encoding='utf-8', check=True)
                    self.assertEqual(child.stdout.splitlines(), expected)
            with patch.dict(os.environ, SYSTEMDRIVE='', SYSTEMROOT=r'Q:\Windows'):
                env = codex_environment(layout.meta_home, secrets=False, root=layout.root)
                self.assertEqual(env['SYSTEMDRIVE'], 'Q:')

    def test_role_profiles_and_local_server_commands(self):
        with tempfile.TemporaryDirectory(prefix='RSI profile ') as directory:
            layout = Layout(Path(directory).resolve())
            settings = Settings(network=True, web_search='live', tool_servers={
                'memory': {'command': 'python3', 'args': ['tools/memory/server.py'], 'network': False},
                'literature': {'command': 'python3', 'args': ['tools/literature/server.py'], 'network': True},
                'remote': {'url': 'https://example.org/mcp'}})
            envelope = {'approval_policy': 'on-request', 'apps': {'a': {'enabled': True}},
                        'features': {'plugins': True, 'apps': True, 'network_proxy': True}}
            for trial in (False, True):
                workspace, role = (layout.trial, 'trial') if trial else (layout.work, 'lab')
                config = lab_config(layout, envelope, settings,
                                    trial=trial, codex=str(layout.root.parent / 'codex.exe'))
                self.assertEqual(tomllib.loads(render_file(config)), config)
                self.assertEqual(config['windows']['sandbox'], 'mxc')
                self.assertEqual(config['web_search'], 'disabled')
                self.assertEqual(config['default_permissions'], role)
                self.assertEqual(set(config['permissions']), {'lab', 'trial'})
                self.assertEqual(config['apps'], {})
                self.assertFalse(any(config['features'][key] for key in ('apps', 'plugins', 'network_proxy')))
                self.assertNotIn('remote', config['mcp_servers'])
                self.assertIn('windows_offline', tool_server_problems(envelope, settings)[0])
                literature = config['mcp_servers']['literature']['args']
                self.assertEqual(literature[literature.index('-P') + 1], role)
                table = config['permissions'][role]['filesystem']
                self.assertEqual(table[str(workspace)], 'write')
                self.assertEqual(table[str(layout.library)], 'read' if trial else 'write')
                self.assertEqual(table[str(layout.root)], 'deny')
                shell = config['shell_environment_policy']['set']
                self.assertEqual(shell['TMP'], str(workspace / '.tmp'))
                self.assertEqual(shell['PATH'].split(';')[0], str(layout.env / 'Scripts'))
                wrapper = config['mcp_servers']['memory']
                self.assertNotIn(str(layout.env), wrapper['env']['PATH'])
                self.assertIn('--include-managed-config', wrapper['args'])
                self.assertEqual(wrapper['args'][-2:], [str(layout.env / 'Scripts/python.exe'),
                                                        str(workspace / 'tools/memory/server.py')])
            meta = meta_config(layout, {}, {})
            self.assertEqual(meta['windows']['sandbox'], 'mxc')
            self.assertEqual(meta['web_search'], 'disabled')
            self.assertEqual(meta['permissions']['meta']['filesystem'][str(layout.work)], 'read')
            self.assertEqual(meta['permissions']['meta']['filesystem'][str(layout.candidate)], 'write')
            full = lab_config(layout, {'approval_policy': 'never', 'rsi': {'windows_offline': False}},
                              Settings(network=True), trial=False)
            self.assertEqual(full['default_permissions'], 'lab_net')
            self.assertTrue(full['permissions']['lab_net']['network']['enabled'])

    def test_server_argument_boundaries_survive_windows_quoting(self):
        value = 'RSI test with spaces & punctuation'
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', WINDOWS_SERVER_START,
                                 os.environ['PATH'], sys.executable, '-I', '-c', 'import sys; print(sys.argv[1])',
                                 value], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), value)
