import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from rsi.settings import load_settings, workspace_problems
from tests.links import make_link

GOOD_ROLE = 'name = "literature"\ndescription = "d"\ndeveloper_instructions = "i"\n'


class SettingsTests(unittest.TestCase):
    def ws(self, settings=None, roles=None):
        w = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, w, True)
        if settings is not None:
            (w / 'codex').mkdir(); (w / 'codex' / 'settings.toml').write_text(settings)
        for name, text in (roles or {}).items():
            (w / 'codex' / 'agents').mkdir(parents=True, exist_ok=True)
            (w / 'codex' / 'agents' / (name + '.toml')).write_text(text)
        return w

    def test_defaults_without_a_file(self):
        s = load_settings(self.ws())
        self.assertEqual((s.network, s.max_depth, s.max_threads, s.tool_servers, s.roles, s.problems),
                         (False, 1, 3, {}, {}, []))

    def test_free_keys(self):
        s = load_settings(self.ws('model = "m"\nnetwork = true\n[agents]\nmax_depth = 2\n[tool_servers.lit]\n'
                                  'command = "python3"\nargs = ["tools/lit/server.py"]\nnetwork = true\n'))
        self.assertEqual((s.model, s.network, s.max_depth, s.problems), ('m', True, 2, []))
        self.assertEqual(s.tool_servers['lit'],
                         {'command': 'python3', 'args': ['tools/lit/server.py'], 'network': True})

    def test_everything_else_is_a_problem(self):
        cases = {'sandbox_mode = "danger-full-access"\n': 'sandbox_mode', '[apps.x]\nenabled = true\n': 'apps',
                 '[permissions.lab.network]\nenabled = true\n': 'permissions',
                 '[mcp_servers.x]\ncommand = "sh"\n': 'mcp_servers', 'network = "yes"\n': 'network',
                 '[agents]\nmax_depth = 9\n': 'max_depth', 'not toml [[[': 'settings.toml',
                 '[tool_servers."a/b"]\ncommand = "x"\n': 'a/b',
                 '[tool_servers.t]\ncommand = "x"\nurl = "https://e"\n': 't',
                 '[tool_servers.t]\ncommand = "python3"\nargs = ["../../escape.py"]\n': 'escape'}
        for text, word in cases.items():
            problems = load_settings(self.ws(text)).problems
            self.assertTrue(any(word in p for p in problems), (text, problems))

    def test_settings_path_that_is_a_folder(self):
        w = self.ws(); (w / 'codex' / 'settings.toml').mkdir(parents=True)
        self.assertTrue(load_settings(w).problems)

    def test_roles(self):
        good = GOOD_ROLE
        s = load_settings(self.ws('', {'literature': good}))
        self.assertEqual(s.roles['literature']['description'], 'd'); self.assertEqual(s.problems, [])
        for name, text in {'default': good.replace('literature', 'default'),
                           'literature': good + 'sandbox_mode = "x"\n', 'other': good, 'Bad Name': good}.items():
            self.assertTrue(load_settings(self.ws('', {name: text})).problems, name)

    def test_dot_codex_is_a_problem(self):
        w = self.ws(''); (w / '.codex').mkdir()
        self.assertTrue(any('.codex' in p for p in workspace_problems(w)))

    # Beyond the brief: odd files, odd values, and what a bad part leaves behind.

    def problems(self, text):
        return load_settings(self.ws(text)).problems

    def test_empty_file_and_missing_codex_folder_are_defaults(self):
        for w in (self.ws(''), self.ws()):
            s = load_settings(w)
            self.assertEqual((s.model, s.effort, s.web_search, s.problems), (None, None, None, []))
            self.assertEqual(workspace_problems(w), [])

    def test_a_bad_part_keeps_the_good_parts(self):
        s = load_settings(self.ws('model = "m"\nsandbox_mode = "x"\n[agents]\nmax_depth = 9\nmax_threads = 8\n'))
        self.assertEqual((s.model, s.max_depth, s.max_threads), ('m', 1, 8))
        self.assertEqual(len(s.problems), 2)

    def test_values(self):
        s = load_settings(self.ws('model_reasoning_effort = "high"\nweb_search = "cached"\n'
                                  '[disable]\nconnectors = ["c"]\nplugins = ["p@x"]\n'))
        self.assertEqual((s.effort, s.web_search, s.disabled_connectors, s.disabled_plugins, s.problems),
                         ('high', 'cached', ['c'], ['p@x'], []))
        cases = {'model_reasoning_effort = "loud"\n': 'model_reasoning_effort', 'web_search = "always"\n': 'web_search',
                 'model = ""\n': 'model', 'model = 3\n': 'model', '[agents]\nmax_threads = true\n': 'max_threads',
                 '[agents]\nmax_threads = 0\n': 'max_threads', '[agents]\nextra = 1\n': 'extra',
                 'agents = 3\n': 'agents',
                 '[disable]\nconnectors = "c"\n': 'connectors', '[disable]\nplugins = [1]\n': 'plugins',
                 '[disable]\nskills = ["s"]\n': 'skills', 'tool_servers = 1\n': 'tool_servers'}
        for text, word in cases.items():
            self.assertTrue(any(word in p for p in self.problems(text)), text)

    def test_tool_servers(self):
        s = load_settings(self.ws('[tool_servers.far]\nurl = "https://e/mcp"\nbearer_token_env_var = "RSI_TOOL_FAR"\n'
                                  '[tool_servers.near]\ncommand = "python3"\n'))
        self.assertEqual(s.tool_servers, {'far': {'url': 'https://e/mcp', 'bearer_token_env_var': 'RSI_TOOL_FAR'},
                                          'near': {'command': 'python3', 'args': [], 'network': False}})
        self.assertEqual(s.problems, [])
        local, remote = '[tool_servers.t]\ncommand = "x"\n', '[tool_servers.t]\nurl = "https://e"\n'
        cases = {'[tool_servers.t]\n': 't', local + 'port = 1\n': 'port', remote + 'network = true\n': 'network',
                 local + 'args = ["/etc/passwd"]\n': '/etc/passwd', local + 'args = "a"\n': 'args',
                 local + 'network = 1\n': 'network', '[tool_servers.t]\ncommand = "../up/x"\n': '../up/x',
                 '[tool_servers.t]\ncommand = ""\n': 'command', local + 'args = ["a\\u0000b"]\n': 'args',
                 '[tool_servers.t]\nurl = "http://e/mcp"\n': 'http://e/mcp',
                 '[tool_servers.t]\nurl = "https://127.0.0.1:3845/mcp"\n': '127.0.0.1',
                 '[tool_servers.t]\nurl = "https://localhost/mcp"\n': 'localhost',
                 '[tool_servers.t]\nurl = "https://u:p@e/mcp"\n': 'url',
                 remote + 'bearer_token_env_var = "OPENAI_API_KEY"\n': 'OPENAI_API_KEY',
                 '[tool_servers.codex_apps]\nurl = "https://e"\n': 'codex_apps'}
        for text, word in cases.items():
            s = load_settings(self.ws(text))
            self.assertTrue(any(word in p for p in s.problems), (text, s.problems))
            self.assertEqual(s.tool_servers, {}, text)

    def test_tool_server_paths(self):
        w = self.ws('[tool_servers.t]\ncommand = "tools/t/run"\nargs = ["-m", "./tools/t/x.py", "plain", "notes.md"]\n')
        (w / 'notes.md').write_text('n')
        s = load_settings(w)
        self.assertEqual((s.problems, s.tool_servers['t']['args']), ([], ['-m', './tools/t/x.py', 'plain', 'notes.md']))
        for arg in ('..', 'a/../../x'):
            self.assertTrue(self.problems(f'[tool_servers.t]\ncommand = "x"\nargs = ["{arg}"]\n'), arg)

    def test_linked_tool_server_path(self):
        w = self.ws('[tool_servers.t]\ncommand = "tools/t/run"\n')
        outside = self.ws()
        make_link(outside, w / 'tools')
        s = load_settings(w)
        self.assertTrue(any('tools/t' in p for p in s.problems), s.problems); self.assertEqual(s.tool_servers, {})

    def test_unreadable_settings_files(self):
        w = self.ws(); (w / 'codex').mkdir(); (w / 'codex' / 'settings.toml').write_bytes(b'model = "\xff"\n')
        self.assertTrue(any('settings.toml' in p for p in load_settings(w).problems))
        w = self.ws(); (w / 'codex').mkdir()
        (w / 'codex' / 'settings.toml').write_text('model = "' + 'm' * (1 << 20) + '"\n')
        self.assertTrue(any('settings.toml' in p for p in load_settings(w).problems))
        w = self.ws(); (w / 'codex').write_text('')
        self.assertTrue(load_settings(w).problems)

    def test_linked_settings_files(self):
        outside = self.ws(); (outside / 'secret.toml').write_text('model = "leaked"\n')
        w = self.ws(); (w / 'codex').mkdir(); make_link(outside / 'secret.toml', w / 'codex' / 'settings.toml')
        s = load_settings(w)
        self.assertEqual(s.model, None); self.assertTrue(any('settings.toml' in p for p in s.problems))
        w = self.ws(); make_link(self.ws('model = "leaked"\n') / 'codex', w / 'codex')
        s = load_settings(w)
        self.assertEqual(s.model, None); self.assertTrue(any('codex' in p for p in s.problems))

    def test_deep_nesting_is_a_problem(self):
        for value in ('[' * 5000 + ']' * 5000, '{a = ' * 5000 + '1' + '}' * 5000):
            s = load_settings(self.ws('model = ' + value + '\n'))
            self.assertEqual(s.model, None)
            self.assertTrue(any('settings.toml' in p for p in s.problems), s.problems)
            s = load_settings(self.ws('', {'literature': GOOD_ROLE + 'model = ' + value + '\n'}))
            self.assertEqual(s.roles, {})
            self.assertTrue(any('literature.toml' in p for p in s.problems), s.problems)
        s = load_settings(self.ws('[tool_servers.t]\nurl = ' + '[' * 200 + ']' * 200 + '\n'))
        self.assertTrue(any('<list>' in p for p in s.problems), s.problems)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires a filesystem FIFO')
    def test_a_fifo_does_not_hang(self):
        w = self.ws(); (w / 'codex').mkdir(); os.mkfifo(w / 'codex' / 'settings.toml')
        result = []
        reader = threading.Thread(target=lambda: result.append(load_settings(w)), daemon=True)
        reader.start(); reader.join(10)
        self.assertTrue(result, 'load_settings blocked on a FIFO')
        self.assertTrue(any('settings.toml' in p for p in result[0].problems))

    def test_role_files(self):
        good = GOOD_ROLE
        s = load_settings(self.ws('', {'literature': good + 'model = "m"\nmodel_reasoning_effort = "low"\n'}))
        role = s.roles['literature']
        self.assertEqual((role['model'], role['effort'], role['file'].name, role['developer_instructions'], s.problems),
                         ('m', 'low', 'literature.toml', 'i', []))
        bad = {'max_depth': good.replace('literature', 'max_depth'),
               'literature': good.replace('developer_instructions', 'x'),
               '.literature': good, 'worker': good.replace('literature', 'worker')}
        for name, text in bad.items():
            s = load_settings(self.ws('', {name: text}))
            self.assertTrue(s.problems, name); self.assertEqual(s.roles, {}, name)
        self.assertTrue(load_settings(self.ws('', {'literature': good + 'model_reasoning_effort = "loud"\n'})).problems)
        w = self.ws('', {'literature': good}); (w / 'codex' / 'agents' / 'notes.md').write_text('ignored')
        self.assertEqual(load_settings(w).problems, [])

    def test_linked_role_files(self):
        elsewhere = self.ws('', {'literature': GOOD_ROLE}) / 'codex' / 'agents'
        w = self.ws(''); (w / 'codex' / 'agents').mkdir()
        make_link(elsewhere / 'literature.toml', w / 'codex' / 'agents' / 'literature.toml')
        s = load_settings(w)
        self.assertEqual(s.roles, {}); self.assertTrue(any('literature.toml' in p for p in s.problems))
        w = self.ws(''); make_link(elsewhere, w / 'codex' / 'agents')
        s = load_settings(w)
        self.assertEqual(s.roles, {}); self.assertTrue(any('agents' in p for p in s.problems))

    def test_problems_are_single_lines(self):
        s = load_settings(self.ws('"a\\nb" = 1\n[tool_servers."x\\ny"]\ncommand = "c"\n'))
        self.assertEqual(len(s.problems), 2)
        self.assertTrue(all('\n' not in p for p in s.problems), s.problems)


if __name__ == '__main__':
    unittest.main()
