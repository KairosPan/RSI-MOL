import os
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
from rsi.labrun import run_task
from rsi.limits import Limits, load_limits
from rsi.meta import DECISION_GUIDE, DEFAULT_INSTRUCTIONS
from rsi.mockmodel import add_file, say
from rsi.paths import Layout
from rsi.records import CALL_FILE, KINDS
from rsi.settings import SETTINGS, Settings, load_settings, workspace_problems
from rsi.tasks import SECTIONS, load_task, run_check
from rsi.versions import VersionStore
from tests.support import ENVELOPE, PASS_CHECK, Root, force_remove, needs_codex, write_passing_check

SEED = Path(__file__).resolve().parents[1] / 'seed'
TASKS = SEED.parents[1] / 'tasks'
META_TEXT = SEED / 'owner' / 'meta' / 'AGENTS.md'
WORK_FILES = ['AGENTS.md', 'codex/agents/literature.toml', 'codex/settings.toml', 'notes/README.md']
OWNER_FILES = ['limits.toml', 'meta/AGENTS.md', 'meta/settings.toml']
# What the English Lab seed calls the human owner and the Lab lead, and the files that name each (the instructions
# and the role file speak of the lead; only the settings file speaks of the owner).
NAMES = re.compile(r'\bthe (owner|lead)\b', re.IGNORECASE)
NAMED = {'AGENTS.md': ['lead'], 'codex/agents/literature.toml': ['lead'], 'codex/settings.toml': ['owner'],
         'notes/README.md': []}
# The rule of Meta's standing instructions that keeps the Lab workspace in English, and the bullet it follows.
ENGLISH_RULE = ('- Lab 工作区里留下的文件一律用英文写，文件名和文件夹名也用英文：指示、技能、角色、设置里的注释、脚本。'
                '你在 `../candidate/` 里的改动用英文，你自己的笔记也用英文。')
DISABLE_RULE = '- 要关掉已允许的某个连接器或插件'
# A commented-out example line of the seed's settings.toml: a table header or `key = value`, after '# '.
EXAMPLE = re.compile(r'# (\[[a-z_.]+\]|[a-z_]+ = .+)')
IDS = '\n[apps.conn_a]\nenabled = true\n\n[plugins."pdf@x"]\nenabled = true\n'     # allowed connector and plugin


def files(folder: Path) -> list[str]:
    """The regular files under folder, as sorted relative paths."""
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


def observe_step(text: str) -> str:
    """Step 1 of Meta's standing instructions: what a run record holds."""
    return text[text.index('1. 观察'):text.index('2. 诊断')]


def meta_paths() -> tuple[str, str]:
    """A call's record folder and the candidate as seen from Meta's working folder, from the layout itself."""
    layout = Layout(Path('/RSI'))
    return (f'{os.path.relpath(layout.records, layout.meta)}/{KINDS["R"]}/<编号>/',
            f'{os.path.relpath(layout.candidate, layout.meta)}/')


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
        loose = [line for line in text.splitlines() if re.match(r'#\s*(\[|[a-z_]+\s*=)', line)
                 and not EXAMPLE.fullmatch(line)]
        self.assertEqual(loose, [])                     # an example spelled otherwise would be skipped below
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

    def test_the_meta_instructions_name_the_record_files_and_the_paths_of_the_layout(self):
        text = META_TEXT.read_text(encoding='utf-8')
        runs, candidate = meta_paths()
        self.assertIn(f'`{runs}`', observe_step(text)); self.assertIn(f'`{candidate}`', text)
        named = set(re.findall(r'`([\w.-]+\.(?:json|jsonl|txt|patch))`', text))
        self.assertIn(CALL_FILE, named); self.assertLessEqual(named, {CALL_FILE, *RECORD_FILES, 'diff.patch'})

    @needs_codex
    def test_what_meta_is_told_a_record_holds_is_in_a_lab_run_record(self):
        """Every name of step 1 but the path is an entry of a real task run's record folder or a key of its
        call.json, and the connector and plugin ids are where the [disable] line says."""
        text = META_TEXT.read_text(encoding='utf-8')
        runs, _ = meta_paths()
        with Root() as root:
            (root.layout.owner / 'lab-envelope.toml').write_text(ENVELOPE + IDS, encoding='utf-8')
            write_passing_check(root.layout)                # the envelope changed since the check
            root.add_task('T1', check=PASS_CHECK)
            root.model.push(add_file('out/T1/answer.json', '{"x": 3, "y": -1}'), say('done'))
            call = run_task(root.layout, 'T1')
            folder = root.records.call_dir(call['id'])
            self.assertEqual(os.path.normpath(root.layout.meta / runs.replace('<编号>', call['id'])), str(folder))
            entries = {path.name + ('/' if path.is_dir() else '') for path in folder.iterdir()}
        named = set(re.findall(r'`([^`]+)`', observe_step(text))) - {runs}
        self.assertEqual(call['status'], 'finished'); self.assertLessEqual({'out/', 'sessions/', 'stderr.txt'}, named)
        for name in sorted(named):
            self.assertTrue(name in entries or name in call, name)
        for word in ('`[disable]`', '`call.json`', '`command`', '`apps=`', '`plugins=`'):
            self.assertIn(word, text)
        flags = [flag for flag in call['command'] if flag.startswith(('apps=', 'plugins='))]
        self.assertEqual([(flag.split('=')[0], 'conn_a' in flag or 'pdf@x' in flag) for flag in flags],
                         [('apps', True), ('plugins', True)])

    def test_the_lab_leads_word_is_never_used_for_the_owner(self):
        """负责人 (the Lab lead) appears nowhere in the program, the tasks or the seed: the owner is 主人 wherever Meta
        reads it. In the English Lab seed the owner is "the owner", and "the lead" is only ever the Lab lead."""
        repo = SEED.parents[1]
        paths = [repo / 'rsi', *(path for top in ('control/rsi', 'control/seed', 'tasks')
                                 for path in (repo / top).rglob('*'))]
        found = sorted(path.relative_to(repo).as_posix() for path in paths
                       if path.is_file() and '__pycache__' not in path.parts and '负责人'.encode() in path.read_bytes())
        self.assertEqual(found, [])
        for text in (DECISION_GUIDE, DEFAULT_INSTRUCTIONS, META_TEXT.read_text(encoding='utf-8')):
            self.assertIn('由主人决定', text)
        texts = {name: (SEED / 'work' / name).read_text(encoding='utf-8') for name in WORK_FILES}
        named = {name: sorted({word.lower() for word in NAMES.findall(text)}) for name, text in texts.items()}
        self.assertEqual(named, NAMED)
        self.assertIn('the owner decides', (SEED / 'work' / SETTINGS).read_text(encoding='utf-8'))

    def test_the_lab_seed_is_all_english(self):
        """Every file of the Lab's seed workspace is plain ASCII: no Chinese, nor any other non-ASCII character."""
        for name in files(SEED / 'work'):
            with self.subTest(name=name):
                self.assertTrue((SEED / 'work' / name).read_bytes().isascii())

    def test_meta_is_told_to_keep_the_lab_workspace_in_english(self):
        """The rule is the bullet right after the one on turning off a connector or plugin."""
        lines = META_TEXT.read_text(encoding='utf-8').splitlines()
        disable = next(index for index, line in enumerate(lines) if line.startswith(DISABLE_RULE))
        self.assertEqual(next(line for line in lines[disable + 1:] if line.startswith('- ')), ENGLISH_RULE)
        self.assertEqual(lines.count(ENGLISH_RULE), 1)

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
