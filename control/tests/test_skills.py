import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import labrun, meta, skills
from rsi.settings import workspace_problems
from rsi.skills import materialise, scan
from tests.support import Root


def tree(test: unittest.TestCase) -> Path:
    folder = Path(tempfile.mkdtemp()).resolve()
    test.addCleanup(shutil.rmtree, folder, True)
    return folder


def write(path: Path, content: str | bytes = 'x') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding='utf-8')
    return path


def files(folder: Path) -> list[str]:
    """Every entry under folder, relative, sorted (links not followed)."""
    if not os.path.lexists(folder):
        return []
    return sorted(str(Path(top, name).relative_to(folder)) for top, dirs, names in os.walk(folder)
                  for name in dirs + names)


def skill(folder: Path, name: str, text: str | None = None) -> Path:
    return write(folder / 'skills' / name / 'SKILL.md', text or f'---\nname: {name}\ndescription: d\n---\n')


class Materialise(unittest.TestCase):
    def test_regular_skills_are_copied_and_the_rest_named(self):
        staged = tree(self)
        skill(staged, 'a', '---\nname: a\ndescription: 技能 a\n---\n正文\n')
        write(staged / 'skills' / 'a' / 'ref.md', b'\x00ref\xff')
        write(staged / 'skills' / 'b' / 'README.md', 'no SKILL.md here')
        os.symlink('/etc', staged / 'skills' / 'c')
        os.symlink('/etc/hosts', staged / 'skills' / 'a' / 'link')
        lines = materialise(staged)
        target = staged / '.agents' / 'skills'
        self.assertEqual(files(target), ['a', 'a/SKILL.md', 'a/ref.md'])
        for name in ('SKILL.md', 'ref.md'):
            self.assertEqual((target / 'a' / name).read_bytes(), (staged / 'skills' / 'a' / name).read_bytes())
        self.assertEqual(len(lines), 3, lines)
        for name in ('skills/b', 'skills/c', 'skills/a/link'):
            self.assertEqual(sum(name in line.split('：')[0] for line in lines), 1, (name, lines))
        self.assertEqual(lines, scan(staged)[1])                        # the same walk names the same entries

    def test_caps_leave_out_skills_and_files_with_a_line(self):
        staged = tree(self)
        skill(staged, 'a'); skill(staged, 'b')
        with mock.patch.object(skills, 'MAX_SKILLS', 1):
            lines = materialise(staged)
        self.assertEqual(files(staged / '.agents' / 'skills'), ['a', 'a/SKILL.md'])
        self.assertTrue(any(line.startswith('skills/b：') and '1' in line for line in lines), lines)
        # (constant, value), files of skill a besides SKILL.md, what is copied besides a/SKILL.md, what is left out.
        # With MAX_FILES a second skill b follows: its SKILL.md no longer fits, so all of b is left out.
        for patch, extra, kept, gone in (
                (('MAX_FILES', 3), {'f1': 'x', 'f2': 'x', 'f3': 'x'}, ['a/f1', 'a/f2'], ['skills/a/f3', 'skills/b']),
                (('MAX_BYTES', 100), {'big.bin': 'x' * 200, 'small.txt': 's'}, ['a/small.txt'], ['skills/a/big.bin']),
                (('MAX_DEPTH', 2), {'x/ok.md': 'o', 'x/y/deep.md': 'd'}, ['a/x', 'a/x/ok.md'], ['skills/a/x/y'])):
            staged = tree(self)
            skill(staged, 'a')
            for name, content in extra.items():
                write(staged / 'skills' / 'a' / name, content)
            if patch[0] == 'MAX_FILES':
                skill(staged, 'b')
            with mock.patch.object(skills, *patch):
                lines = materialise(staged)
            self.assertEqual(files(staged / '.agents' / 'skills'), sorted(['a', 'a/SKILL.md'] + kept), patch)
            self.assertEqual([line.split('：')[0] for line in lines], gone, (patch, lines))

    def test_an_agents_entry_from_a_checkout_is_removed_first(self):
        for name in ('.agents', '.Agents', '.AGENTS'):
            staged = tree(self)
            write(staged / name / 'skills' / 'old' / 'SKILL.md', 'old')
            skill(staged, 'new')
            materialise(staged)
            self.assertEqual(os.listdir(staged / '.agents' / 'skills'), ['new'], name)
            self.assertEqual(sorted(entry for entry in os.listdir(staged) if entry.lower() == '.agents'), ['.agents'])
        staged = tree(self)
        write(staged / '.agents', 'a file')                                 # not even a folder
        self.assertEqual((materialise(staged), os.listdir(staged)), ([], []))   # no skills/: no .agents at all

    def test_nothing_to_copy_makes_no_agents_folder(self):
        staged = tree(self)
        self.assertEqual((materialise(staged), os.listdir(staged)), ([], []))
        write(staged / 'skills' / 'b' / 'README.md')
        self.assertEqual((len(materialise(staged)), sorted(os.listdir(staged))), (1, ['skills']))
        for odd in ('file', 'link'):
            staged = tree(self)
            if odd == 'file':
                write(staged / 'skills', 'a file')
            else:
                os.symlink(tree(self), staged / 'skills')
            lines = materialise(staged)
            self.assertEqual((len(lines), os.path.lexists(staged / '.agents')), (1, False), odd)
            self.assertTrue(lines[0].startswith('skills：'), lines)

    def test_meta_is_told_through_workspace_problems_and_nothing_is_copied(self):
        candidate = tree(self)
        skill(candidate, 'a'); write(candidate / 'skills' / 'b' / 'README.md')
        os.symlink('/etc/hosts', candidate / 'skills' / 'a' / 'link')
        problems = workspace_problems(candidate)
        self.assertEqual([line.split('：')[0] for line in problems], ['skills/a/link', 'skills/b'])
        self.assertFalse(os.path.lexists(candidate / '.agents'))


class WhereSkillsGo(unittest.TestCase):
    """work/ and trial/ get .agents/skills/ when they are moved in; candidate/ never does."""

    def test_work_gets_the_skills_and_candidate_does_not(self):
        with Root(passed_check=False) as root:
            skill(root.layout.work, 'demo')
            self.assertEqual(root.store.commit(root.layout.work, 'owner', 'skills'), 'V0002')
            problems = []
            with labrun.root_descriptor(root.layout) as root_fd:
                os.rename(root.layout.work, root.layout.root / 'old-work')
                staged, _ = labrun.stage(root.layout, root.store, 'V0002')
                labrun.move_in(root.layout, root.records, root_fd, staged, 'work', {}, problems)
                meta._put_candidate(root.layout, root.records, root.store, root_fd, 'V0002', None, {}, problems)
            self.assertEqual(problems, [])
            self.assertTrue((root.layout.work / '.agents' / 'skills' / 'demo' / 'SKILL.md').is_file())
            self.assertTrue((root.layout.candidate / 'skills' / 'demo' / 'SKILL.md').is_file())
            self.assertFalse(os.path.lexists(root.layout.candidate / '.agents'))
            self.assertEqual(root.store.tree_of_dir(root.layout.work), root.store.tree_of('V0002'))


if __name__ == '__main__':
    unittest.main()
