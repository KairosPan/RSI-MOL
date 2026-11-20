"""Helpers shared by the tests: a skip for machines without Codex, a confined config, and a temporary RSI root."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.mockmodel import ScriptedModel
from rsi.paths import Layout
from rsi.records import Records
from rsi.versions import VersionStore

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


def scratch_config(root: Path, workspace: Path) -> dict:
    """Never ask for approval; the `lab` profile reads the minimum, denies `root` and writes `workspace`, no network."""
    filesystem = {':minimal': 'read', str(root): 'deny', str(workspace): 'write'}
    return {'approval_policy': 'never', 'default_permissions': 'lab',
            'permissions': {'lab': {'filesystem': filesystem, 'network': {'enabled': False}}}}


class Root:
    """A temporary RSI root, as a context manager: every folder of the layout; owner/lab-envelope.toml (approval
    never, no apps, no plugins); owner/limits.toml with the defaults; a version store whose V0001 is work/ holding
    AGENTS.md ('hello'); a started ScriptedModel in .model, with $RSI_TEST_MODEL_ENDPOINT and $RSI_ROOT set until
    the end. The environment is restored and the folder removed on exit.

    passed_check is for the gate of a later task (whether `rsi check` counts as passed); unused for now.
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
        shutil.rmtree(self.path, ignore_errors=True)
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
