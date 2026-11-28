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
from tests.installed_seed import WORK as INSTALLED_WORK
from tests.support import ENVELOPE, PASS_CHECK, Root, force_remove, needs_codex, write_passing_check

SEED = Path(__file__).resolve().parents[1] / 'seed'
TASKS = SEED.parents[1] / 'tasks'
META_TEXT = SEED / 'owner' / 'meta' / 'AGENTS.md'
WORK_FILES = ['AGENTS.md', 'codex/agents/literature.toml', 'codex/settings.toml', 'notes/README.md']
LIT_FILES = ['tools/literature/' + name for name in
             ('UPSTREAM.md', 'dispatch.py', 'runtime.py', 'server.py', 'transport.py', 'windows_poppler.py')]
UPSTREAM_MODULES = ('__init__', 'catalog', 'documents', 'parsers', 'poppler', 'projects', 'providers', 'remote',
                    'sdk_tools', 'services', 'worker')
LIT_FILES += ['tools/literature/rsi_literature/' + name + '.py' for name in UPSTREAM_MODULES]
MEMORY_FILES = ['tools/memory/server.py', 'tools/memory/query.py', 'skills/scientific-memory/SKILL.md']
OWNER_FILES = ['limits.toml', 'meta/AGENTS.md', 'meta/settings.toml']
# What the English Lab seed calls the human owner and the Lab lead, and the files that name each (the instructions
# and the role file speak of the lead; only the settings file speaks of the owner).
NAMES = re.compile(r'\bthe (owner|lead)\b', re.IGNORECASE)
NAMED = {'AGENTS.md': ['lead'], 'codex/agents/literature.toml': ['lead'], 'codex/settings.toml': ['owner'],
         'notes/README.md': []}
# The ranges no file of the program, the tasks or the seed may use a character of: CJK symbols and punctuation, CJK
# ideographs, and halfwidth and fullwidth forms.
CJK = re.compile('[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]')
# The rule of Meta's standing instructions that keeps the Lab workspace in English (one bullet, its lines joined
# by a space), and the bullet it follows.
ENGLISH_RULE = ('- Every file kept in the Lab workspace is written in English, with English file and folder names: '
                'instructions, skills, roles, comments in the settings, scripts. Your changes in `../candidate/` are '
                'in English, and so are your own notes.')
DISABLE_RULE = '- To switch off one of the allowed connectors or plugins'
# The lines of the grid task's check, by the content of answer.json (None: no file, bytes: written as they are).
GRID_LINES = {None: 'answer.json is missing', 'not json': 'answer.json is not a valid JSON object',
              '[]': 'the fields do not meet the requirements',
              '{"x": 0, "y": 0}': 'x and y do not give f its largest value', '{"x": 3, "y": -1}': 'passed'}
# Meta's standing instructions in the seed, word for word.
META_SEED = """# Meta

You improve how the Lab works. You do not do the Lab's tasks for it.

Each round:

1. Observe. Read the run records handed to you this round; each is under `../records/runs/<id>/`:
   `call.json` (the check result is its `check`; `problems` are problems the control program met before or after
   the run; `settings_problems` are settings, tool servers and skills that did not take effect), `prompt.txt`,
   `events.jsonl`, `stderr.txt` (Codex's errors; when a run could not start, the cause is only here),
   `last-message.txt`, `out/`, `sessions/` (the session and the sub-agents' work), and for a task run `diff.patch`.
   Check which task and version a record belongs to before you judge.
2. Diagnose. Write down the problem, the evidence (cite record ids), the likely cause, and other explanations.
   Do not treat something seen once as a pattern. If the evidence is not enough, decide to change nothing.
3. Improve. Edit only in `../candidate/` (its full path is in this round's prompt), one small change at a time.
   You may change instructions, skills (`skills/<name>/SKILL.md`), roles, settings, scripts, and tool servers.
   Do not write the answer to any task into them.
4. Validate. Ask for trial runs when you need them: which task, or a short test you write yourself; on the
   candidate or on the current version. During a trial `env/` and `library/` are read-only: a candidate cannot
   install packages into `env/` or save files to `library/` in a trial.
5. Decide. Whether to release or to change nothing is your decision. A release must cite the ids of Lab run
   records (usually trials), and those runs must have run exactly what you last left in the candidate. If you
   change it, run again.

Rules:

- State possible regressions and what you are unsure of. Do not claim a benefit.
- You cannot read or change a task's check script, the budget limits, or the control program. Records are
  read-only.
- To give the Lab a new connector or plugin, to let it read or write files outside its workspace, or to raise a
  budget limit, write an application; the owner decides. Tool servers and command networking you may change
  directly in the settings.
- To switch off one of the allowed connectors or plugins, write its id under `[disable]` in the settings. The ids
  are in a Lab run record's `call.json`, field `command`: the `apps=` and `plugins=` parts list them.
- Every file kept in the Lab workspace is written in English, with English file and folder names: instructions,
  skills, roles, comments in the settings, scripts. Your changes in `../candidate/` are in English, and so are
  your own notes.
- Your notes go in `notes/`; they are still there next round.
- Every reply must be a JSON object in the required format.
"""
# A commented-out example line of the seed's settings.toml: a table header or `key = value`, after '# '.
EXAMPLE = re.compile(r'# (\[[a-z_.]+\]|[a-z_]+ = .+)')
IDS = '\n[apps.conn_a]\nenabled = true\n\n[plugins."pdf@x"]\nenabled = true\n'     # allowed connector and plugin


def files(folder: Path) -> list[str]:
    """The regular files under folder, as sorted relative paths."""
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


def observe_step(text: str) -> str:
    """Step 1 of Meta's standing instructions: what a run record holds."""
    return text[text.index('1. Observe'):text.index('2. Diagnose')]


def bullets(text: str) -> list[str]:
    """The `- ` bullets of a text, each with its indented continuation lines joined to it by a space."""
    out = []
    for line in text.splitlines():
        if line.startswith('- '):
            out.append(line)
        elif out and line.startswith('  ') and line.strip():
            out[-1] += ' ' + line.strip()
        elif out and out[-1] is not None:
            out.append(None)                                    # anything else ends the bullet
    return [bullet for bullet in out if bullet is not None]


def meta_paths() -> tuple[str, str]:
    """A call's record folder and the candidate as seen from Meta's working folder, from the layout itself."""
    layout = Layout(Path('/RSI'))
    return (f'{os.path.relpath(layout.records, layout.meta)}/{KINDS["R"]}/<id>/',
            f'{os.path.relpath(layout.candidate, layout.meta)}/')


class Seed(unittest.TestCase):
    def test_seed_workspace_has_no_problems(self):
        self.assertEqual(workspace_problems(SEED / 'work'), [])
        self.assertEqual(sorted(load_settings(SEED / 'work').roles), ['literature'])
        self.assertEqual(files(SEED / 'work'), sorted(WORK_FILES + LIT_FILES + MEMORY_FILES))

    def test_grid_check(self):
        check, task = TASKS / 'grid' / 'check.py', load_task(TASKS, 'grid')
        cases = ((None, None, 1), ('[]', '[]', 1), ('{"x": 3}', '[]', 1), ('{"x": 0, "y": 0}', '{"x": 0, "y": 0}', 1),
                 ('{"x": 3, "y": -1}', '{"x": 3, "y": -1}', 0), ('not json', 'not json', 1),
                 # beyond the plan: extra or repeated keys, values that are not integers, outside the range, bytes
                 # that are not UTF-8, nesting too deep for the parser
                 ('{"x": 3, "y": -1, "z": 0}', '[]', 1), ('{"x": 3, "x": 3, "y": -1}', '[]', 1),
                 ('{"x": true, "y": -1}', '[]', 1), ('{"x": 3.0, "y": -1}', '[]', 1),
                 ('{"x": 13, "y": -1}', '{"x": 0, "y": 0}', 1), (b'{"x": 3, "y": -1}\xff', 'not json', 1),
                 ('[' * 100_000, 'not json', 1))
        self.assertEqual({GRID_LINES[like] for _, like, _ in cases}, set(GRID_LINES.values()))     # all five lines
        for content, like, code in cases:
            with self.subTest(content=str(content)[:40]), tempfile.TemporaryDirectory() as folder:
                out = Path(folder)
                if isinstance(content, bytes):
                    (out / 'answer.json').write_bytes(content)
                elif content is not None:
                    (out / 'answer.json').write_text(content, encoding='utf-8')
                done = subprocess.run([sys.executable, '-X', 'utf8', str(check), str(out)], capture_output=True,
                                      encoding='utf-8', stdin=subprocess.DEVNULL)
                self.assertEqual((done.returncode, done.stderr), (code, ''))
                self.assertEqual(done.stdout, GRID_LINES[like] + '\n')      # exactly one line, the one for this case
                self.assertNotIn('-1', done.stdout); self.assertNotRegex(done.stdout, '[0-9]')     # never a value
                result = run_check(task, out)                              # as the control program runs it
                self.assertEqual(result, {'passed': code == 0, 'message': done.stdout.strip(), 'exit_code': code})

    def test_grid_task_loads_with_its_four_sections(self):
        task = load_task(TASKS, 'grid')
        self.assertEqual(list(task.sections), list(SECTIONS)); self.assertTrue(all(task.sections.values()))
        self.assertEqual((task.check, task.inputs), (TASKS / 'grid' / 'check.py', None))

    def test_the_settings_file_enables_literature_and_memory_and_its_examples_are_the_free_keys(self):
        text = (SEED / 'work' / SETTINGS).read_text(encoding='utf-8')
        self.assertEqual(tomllib.loads(text), {'tool_servers': {
            'literature': {'command': 'python3', 'args': ['-B', 'tools/literature/server.py'], 'network': True},
            'memory': {'command': 'python3', 'args': ['-B', 'tools/memory/server.py']}}})
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
        self.assertEqual(len(settings.problems), 1, settings.problems); self.assertIn(': model ', settings.problems[0])
        default = Settings()
        for name in ('effort', 'web_search', 'network', 'max_depth', 'max_threads', 'disabled_connectors',
                     'disabled_plugins'):
            self.assertNotEqual(getattr(settings, name), getattr(default, name), name)
        self.assertEqual(sorted(sorted(server) for server in settings.tool_servers.values()),
                         [['args', 'command', 'network'], ['bearer_token_env_var', 'url']])

    def test_the_meta_seed_is_the_english_text(self):
        self.assertEqual(META_TEXT.read_text(encoding='utf-8'), META_SEED)

    def test_the_owner_seed_files_hold_the_defaults(self):
        self.assertEqual(load_limits(SEED / 'owner'), Limits())
        self.assertEqual(tomllib.loads((SEED / 'owner' / 'meta' / 'settings.toml').read_text(encoding='utf-8')), {})

    def test_the_meta_instructions_name_the_record_files_and_the_paths_of_the_layout(self):
        text = META_TEXT.read_text(encoding='utf-8')
        runs, candidate = meta_paths()
        self.assertIn(f'`{runs}`', observe_step(text)); self.assertIn(f'`{candidate}`', text)
        named = set(re.findall(r'`([\w.-]+\.(?:json|jsonl|txt|patch))`', text))
        self.assertIn(CALL_FILE, named)
        self.assertLessEqual(named, {CALL_FILE, *RECORD_FILES, 'diff.patch'})

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
            self.assertEqual(os.path.normpath(root.layout.meta / runs.replace('<id>', call['id'])), str(folder))
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

    def test_no_chinese_in_the_program_the_tasks_or_the_seed(self):
        """No character of U+3000-U+303F, U+4E00-U+9FFF or U+FF00-U+FFEF (CJK punctuation, ideographs, fullwidth
        forms) is in the launcher or in any file under control/rsi, control/seed or tasks. Other non-ASCII characters
        the program needs (the long s of `.agentſ`) are not looked for."""
        repo = SEED.parents[1]
        paths = [repo / 'rsi', *(path for top in ('control/rsi', 'control/seed', 'tasks')
                                 for path in (repo / top).rglob('*'))]
        found = {}
        for path in paths:
            if path.is_file() and '__pycache__' not in path.parts:
                text = path.read_bytes().decode('utf-8')
                lines = [number for number, line in enumerate(text.splitlines(), 1) if CJK.search(line)]
                if lines:
                    found[path.relative_to(repo).as_posix()] = lines
        self.assertEqual(found, {})
        self.assertGreater(len(paths), 30)                   # the folders were found and walked

    def test_the_owner_decides_and_the_lead_is_only_the_lab_lead(self):
        """In the English Lab seed the owner is "the owner", and "the lead" is only ever the Lab lead."""
        for text in (DECISION_GUIDE, DEFAULT_INSTRUCTIONS, META_TEXT.read_text(encoding='utf-8')):
            self.assertIn('the owner decides', text)
        texts = {name: (SEED / 'work' / name).read_text(encoding='utf-8') for name in WORK_FILES}
        named = {name: sorted({word.lower() for word in NAMES.findall(text)}) for name, text in texts.items()}
        self.assertEqual(named, NAMED)
        self.assertIn('the owner decides', (SEED / 'work' / SETTINGS).read_text(encoding='utf-8'))

    def test_the_lab_seed_is_all_english(self):
        """MOL-authored seed files use ASCII. The pinned upstream reader contains Unicode for text normalization."""
        for name in files(SEED / 'work'):
            if name.startswith('tools/literature/rsi_literature/'):
                continue
            with self.subTest(name=name):
                self.assertTrue((SEED / 'work' / name).read_bytes().isascii())

    def test_meta_is_told_to_keep_the_lab_workspace_in_english(self):
        """The rule is the bullet right after the one on turning off a connector or plugin."""
        found = bullets(META_TEXT.read_text(encoding='utf-8'))
        disable = next(index for index, bullet in enumerate(found) if bullet.startswith(DISABLE_RULE))
        self.assertEqual(found[disable + 1], ENGLISH_RULE)
        self.assertEqual(found.count(ENGLISH_RULE), 1)

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
        expected = files(INSTALLED_WORK)
        self.assertEqual(files(checkout), sorted(expected))
        self.assertEqual(files(layout.work), sorted(expected + ['.agents/skills/scientific-memory/SKILL.md',
                                                               'memory/status.json', 'memory/evidence.jsonl']))
        for name in expected:
            self.assertEqual((checkout / name).read_bytes(), (INSTALLED_WORK / name).read_bytes(), name)
        for name in OWNER_FILES:
            self.assertEqual((layout.owner / name).read_bytes(), (SEED / 'owner' / name).read_bytes(), name)
