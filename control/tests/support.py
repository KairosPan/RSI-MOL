"""Helpers shared by the tests: a skip for machines without Codex, a confined config, a temporary RSI root, and the
text of a Meta decision."""
import functools
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.check import CHECK_FILE, owner_fingerprints, program_fingerprints
from rsi.codexrun import codex_version
from rsi.mockmodel import ScriptedModel
from rsi.paths import Layout
from rsi.records import Records
from rsi.versions import VersionStore
from rsi.workspace import clear_acl, raise_open_files_limit

needs_codex = unittest.skipUnless(shutil.which(os.environ.get('RSI_CODEX', 'codex')), 'Codex is not on the path')

# The check script of the test task: passes when out/answer.json holds the maximum of f.
PASS_CHECK = ('import json,sys,pathlib\np = pathlib.Path(sys.argv[1]) / "answer.json"\n'
              'if not p.is_file(): print("缺少 answer.json"); sys.exit(1)\n'
              'd = json.loads(p.read_text())\nok = d == {"x": 3, "y": -1}\n'
              'print("通过" if ok else "x、y 没有使 f 取到最大值"); sys.exit(0 if ok else 1)\n')

TASK_SECTIONS = {
    '目标': '找出使 f(x, y) = -(x - 3)² - (y + 1)² 取到最大值的整数 x、y。',
    '输入': '没有输入文件。',
    '要求': '把答案写成 answer.json，形如 {"x": 0, "y": 0}。',
    '完成判据': 'answer.json 里的 x、y 使 f 取到最大值。',
}
ENVELOPE = 'approval_policy = "never"\n\n[features]\napps = false\nplugins = false\n'
LIMITS = ('[lab]\nseconds_per_run = 1800\n\n'
          '[meta]\ncalls_per_round = 8\ntrials_per_round = 4\nseconds_per_call = 900\n\n'
          '[day]\ncalls = 60\ntokens = 0\n')


def plant_programs(folder: Path, marker: Path) -> list[str]:
    """Scripts in folder named like the programs the control program, Codex or the npm shim could start through a
    PATH lookup (codex, git, python3, node, ...); each appends its name to marker when run, then fails. Their names."""
    folder.mkdir(parents=True, exist_ok=True)
    names = ['codex', 'git', 'python3', 'python', 'node', 'readlink', 'dirname', 'xcrun', 'ps']
    for name in names:
        (folder / name).write_text(f'#!/bin/sh\necho {name} >> "{marker}"\nexit 1\n')
        (folder / name).chmod(0o755)
    return names


def decision(kind: str, **more) -> str:
    """The JSON text of a Meta decision of that kind: every key with a placeholder value, then `more` over them."""
    base = {'decision': kind, 'observations': [], 'problem': 'p', 'likely_cause': 'c', 'other_explanations': 'o',
            'change': 'ch', 'expected_effect': 'e', 'risks': 'r', 'uncertain': 'u', 'trials': [], 'evidence': [],
            'applications': []}
    return json.dumps({**base, **more}, ensure_ascii=False)


def scratch_config(root: Path, workspace: Path) -> dict:
    """Never ask for approval; the `lab` profile reads the minimum, denies `root` and writes `workspace`, no network."""
    filesystem = {':minimal': 'read', str(root): 'deny', str(workspace): 'write'}
    return {'approval_policy': 'never', 'default_permissions': 'lab',
            'permissions': {'lab': {'filesystem': filesystem, 'network': {'enabled': False}}}}


def force_remove(path: Path) -> None:
    """Remove a test folder whatever the test left in it: flags, ACLs, folders with mode 000 (links are not
    followed)."""
    def unlock(entry: str) -> None:
        try:
            os.chflags(entry, 0, follow_symlinks=False)
        except (AttributeError, NotImplementedError, OSError):
            pass
        if not os.path.islink(entry):
            if not clear_acl(entry) and os.path.exists('/bin/chmod'):         # an ACL that also denies reading it
                subprocess.run(['/bin/chmod', '-N', entry], capture_output=True)
            try:
                os.chmod(entry, 0o700)
            except OSError:
                pass

    raise_open_files_limit()        # a test may have lowered it, and rmtree holds a descriptor per level
    unlock(str(path))
    for top, folders, files in os.walk(path):
        for name in folders + files:
            unlock(os.path.join(top, name))
    shutil.rmtree(path, ignore_errors=True)


@functools.cache
def _installed_codex(program: str) -> str | None:
    """The version the Codex program reports (codex_version), once per program name; None when it reports none."""
    try:
        return codex_version()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None


def write_passing_check(layout: Layout) -> None:
    """records/check.json as a passed `rsi check` for the installed Codex, the owner files and the control program's
    code as they are now would leave it (without its cases), so that the gate lets agents start; nothing when Codex
    reports no version."""
    version = _installed_codex(os.environ.get('RSI_CODEX', 'codex'))
    if version is not None:
        (layout.records / CHECK_FILE).write_text(json.dumps(
            {'codex_version': version, 'owner_files': owner_fingerprints(layout), 'program': program_fingerprints(),
             'passed': True, 'at': '2026-01-01T00:00:00+00:00', 'results': [], 'problems': [], 'notes': [],
             'info': []}), encoding='utf-8')


class Root:
    """A temporary RSI root, as a context manager: every folder of the layout; owner/lab-envelope.toml (approval
    never, no apps, no plugins); owner/limits.toml with the defaults; a version store whose V0001 is work/ holding
    AGENTS.md ('hello'); a started ScriptedModel in .model, with $RSI_TEST_MODEL_ENDPOINT and $RSI_ROOT set until
    the end. The environment is restored and the folder removed on exit.

    passed_check: whether records/check.json holds a passed `rsi check` for the installed Codex (write_passing_check),
    so that the gate lets agents start; with False there is no check.json and every agent run is refused.
    """

    def __init__(self, passed_check: bool = True) -> None:
        self.passed_check = passed_check
        self.model: ScriptedModel | None = None

    def __enter__(self) -> 'Root':
        self._environment = mock.patch.dict(os.environ)
        self._environment.start()
        self.path = Path(tempfile.mkdtemp()).resolve()
        try:
            self.layout = layout = Layout(self.path)
            for folder in (layout.owner, layout.tasks, layout.work, layout.trial, layout.candidate, layout.notes,
                           layout.library, layout.env, layout.lab_home, layout.meta_home):
                folder.mkdir(parents=True, exist_ok=True)
            (layout.owner / 'lab-envelope.toml').write_text(ENVELOPE, encoding='utf-8')
            (layout.owner / 'limits.toml').write_text(LIMITS, encoding='utf-8')
            self.store = VersionStore(layout.versions)
            self.store.init()
            (layout.work / 'AGENTS.md').write_text('hello\n', encoding='utf-8')
            self.store.commit(layout.work, 'owner', '初始版本')
            self.records = Records(layout.records)
            if self.passed_check:
                write_passing_check(layout)
            self.model = ScriptedModel()
            os.environ['RSI_TEST_MODEL_ENDPOINT'] = self.model.start()
            os.environ['RSI_ROOT'] = str(self.path)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc_info) -> bool:
        if self.model is not None:
            self.model.stop()
        self._environment.stop()
        force_remove(self.path)
        return False

    def add_task(self, task_id: str, check: str | None = None, sections: dict | None = None,
                 inputs: dict | None = None) -> Path:
        """tasks/<task_id>/ with task.md (the sections, default TASK_SECTIONS), check.py if given, and input/
        holding inputs (file name -> text) if given."""
        folder = self.layout.tasks / task_id
        folder.mkdir(parents=True)
        sections = TASK_SECTIONS if sections is None else sections
        text = f'# 任务 {task_id}\n\n' + ''.join(f'## {name}\n\n{body}\n\n' for name, body in sections.items())
        (folder / 'task.md').write_text(text, encoding='utf-8')
        if check is not None:
            (folder / 'check.py').write_text(check, encoding='utf-8')
        for name, content in (inputs or {}).items():
            (folder / 'input').mkdir(exist_ok=True)
            (folder / 'input' / name).write_text(content, encoding='utf-8')
        return folder
