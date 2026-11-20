import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from rsi import init as init_module
from rsi.codexrun import RECORD_FILES
from rsi.init import init
from rsi.limits import Limits, load_limits
from rsi.paths import Layout
from rsi.records import CALL_FILE
from rsi.settings import SETTINGS, Settings, load_settings, workspace_problems
from rsi.tasks import SECTIONS, load_task, run_check
from rsi.versions import VersionStore
from tests.support import force_remove

SEED = Path(__file__).resolve().parents[1] / 'seed'
TASKS = SEED.parents[1] / 'tasks'
WORK_FILES = ['AGENTS.md', 'codex/agents/literature.toml', 'codex/settings.toml', 'notes/README.md']
OWNER_FILES = ['limits.toml', 'meta/AGENTS.md', 'meta/settings.toml']
# A commented-out example line of the seed's settings.toml: a table header or `key = value`, after '# '.
EXAMPLE = re.compile(r'# (\[[a-z_.]+\]|[a-z_]+ = .+)')


def files(folder: Path) -> list[str]:
    """The regular files under folder, as sorted relative paths."""
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


class Seed(unittest.TestCase):
    def test_seed_workspace_has_no_problems(self):
        self.assertEqual(workspace_problems(SEED / 'work'), [])
        self.assertEqual(sorted(load_settings(SEED / 'work').roles), ['literature'])
        self.assertEqual(files(SEED / 'work'), WORK_FILES)

    def test_grid_check(self):
        check, task = TASKS / 'grid' / 'check.py', load_task(TASKS, 'grid')
        cases = ((None, '缺少', 1), ('[]', '字段', 1), ('{"x": 3}', '字段', 1), ('{"x": 0, "y": 0}', '最大值', 1),
                 ('{"x": 3, "y": -1}', '通过', 0), ('not json', 'JSON', 1),
                 # beyond the plan: extra or repeated keys, values that are not integers, outside the range, bytes
                 # that are not UTF-8, nesting too deep for the parser
                 ('{"x": 3, "y": -1, "z": 0}', '字段', 1), ('{"x": 3, "x": 3, "y": -1}', '字段', 1),
                 ('{"x": true, "y": -1}', '字段', 1), ('{"x": 3.0, "y": -1}', '字段', 1),
                 ('{"x": 13, "y": -1}', '最大值', 1), (b'{"x": 3, "y": -1}\xff', 'JSON', 1), ('[' * 100_000, 'JSON', 1))
        for content, word, code in cases:
            with self.subTest(content=str(content)[:40]), tempfile.TemporaryDirectory() as folder:
                out = Path(folder)
                if isinstance(content, bytes):
                    (out / 'answer.json').write_bytes(content)
                elif content is not None:
                    (out / 'answer.json').write_text(content, encoding='utf-8')
                done = subprocess.run([sys.executable, '-X', 'utf8', str(check), str(out)], capture_output=True,
                                      encoding='utf-8', stdin=subprocess.DEVNULL)
                self.assertEqual((done.returncode, done.stderr), (code, '')); self.assertIn(word, done.stdout)
                self.assertEqual(done.stdout.count('\n'), 1)                # exactly one line
                self.assertNotIn('-1', done.stdout); self.assertNotRegex(done.stdout, '[0-9]')     # never a value
                result = run_check(task, out)                              # as the control program runs it
                self.assertEqual(result, {'passed': code == 0, 'message': done.stdout.strip(), 'exit_code': code})

    def test_grid_task_loads_with_its_four_sections(self):
        task = load_task(TASKS, 'grid')
        self.assertEqual(list(task.sections), list(SECTIONS)); self.assertTrue(all(task.sections.values()))
        self.assertEqual((task.check, task.inputs), (TASKS / 'grid' / 'check.py', None))

    def test_the_settings_file_is_comments_only_and_its_examples_are_the_free_keys(self):
        text = (SEED / 'work' / SETTINGS).read_text(encoding='utf-8')
        self.assertEqual(tomllib.loads(text), {})
        self.assertEqual(load_settings(SEED / 'work').problems, [])
        examples = [match.group(1) for line in text.splitlines() if (match := EXAMPLE.fullmatch(line))]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'codex').mkdir()
            (Path(folder) / SETTINGS).write_text('\n'.join(examples) + '\n', encoding='utf-8')
            settings = load_settings(Path(folder))
        # Uncommented, every example is taken except the model's, which stands for a model name and is refused.
        self.assertEqual(len(settings.problems), 1, settings.problems); self.assertIn('：model ', settings.problems[0])
        default = Settings()
        for name in ('effort', 'web_search', 'network', 'max_depth', 'max_threads', 'disabled_connectors',
                     'disabled_plugins'):
            self.assertNotEqual(getattr(settings, name), getattr(default, name), name)
        self.assertEqual(sorted(sorted(server) for server in settings.tool_servers.values()),
                         [['args', 'command', 'network'], ['bearer_token_env_var', 'url']])

    def test_the_owner_seed_files_hold_the_defaults(self):
        self.assertEqual(load_limits(SEED / 'owner'), Limits())
        self.assertEqual(tomllib.loads((SEED / 'owner' / 'meta' / 'settings.toml').read_text(encoding='utf-8')), {})

    def test_the_meta_instructions_name_the_files_a_call_record_holds(self):
        text = (SEED / 'owner' / 'meta' / 'AGENTS.md').read_text(encoding='utf-8')
        named = set(re.findall(r'`([\w.-]+\.(?:json|jsonl|txt|patch))`', text))
        self.assertIn(CALL_FILE, named); self.assertLessEqual(named, {CALL_FILE, *RECORD_FILES, 'diff.patch'})
        self.assertIn('`../candidate/`', text)

    def test_a_root_initialised_from_the_seed_has_it_as_v0001(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(force_remove, tmp)
        layout = Layout(tmp / 'root')
        layout.env.mkdir(parents=True)                      # an env/ already there: init makes no venv
        with mock.patch.object(init_module, 'signed_in', return_value=None):       # and starts no Codex
            report = init(layout, SEED)
        self.assertEqual((report['first_version'], report['version'], report['problems']), ('V0001', 'V0001', []))
        store = VersionStore(layout.versions)
        self.assertEqual([entry['author'] for entry in store.log()], ['init'])
        checkout = tmp / 'V0001'
        checkout.mkdir()
        store.checkout('V0001', checkout)
        self.assertEqual(files(checkout), WORK_FILES); self.assertEqual(files(layout.work), WORK_FILES)
        for name in WORK_FILES:
            self.assertEqual((checkout / name).read_bytes(), (SEED / 'work' / name).read_bytes(), name)
        for name in OWNER_FILES:
            self.assertEqual((layout.owner / name).read_bytes(), (SEED / 'owner' / name).read_bytes(), name)
