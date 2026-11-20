import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi import labrun, meta, skills, versions, workspace
from rsi.settings import workspace_problems
from rsi.skills import materialise, scan
from rsi.workspace import prepare_for_commit
from tests.support import Root

ALIAS = '.agentſ'          # `.agentſ` (LONG S): APFS takes it for `.agents`


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


def folds(test: unittest.TestCase, alias: str, name: str) -> bool:
    """Whether the file system of the temporary folder takes `alias` for `name` (APFS's default, case-insensitive
    format does for `.agentſ` and `.agents`)."""
    probe = tree(test)
    (probe / alias).mkdir()
    return os.path.lexists(probe / name)


def needs_folding(test: unittest.TestCase, alias: str = ALIAS, name: str = '.agents') -> None:
    if not folds(test, alias, name):
        test.skipTest(f'the file system here does not take {alias!r} for {name!r}, so there is no alias to test')


class Materialise(unittest.TestCase):
    def test_regular_skills_are_copied_and_the_rest_named(self):
        staged = tree(self)
        skill(staged, 'a', '---\nname: a\ndescription: skill a\n---\nBody.\n')
        write(staged / 'skills' / 'a' / 'ref.md', b'\x00ref\xff')
        write(staged / 'skills' / 'a' / 'run.sh', '#!/bin/sh\n').chmod(0o755)
        write(staged / 'skills' / 'b' / 'README.md', 'no SKILL.md here')
        os.symlink('/etc', staged / 'skills' / 'c')
        os.symlink('/etc/hosts', staged / 'skills' / 'a' / 'link')
        lines = materialise(staged)
        target = staged / '.agents' / 'skills'
        self.assertEqual(files(target), ['a', 'a/SKILL.md', 'a/ref.md', 'a/run.sh'])
        for name in ('SKILL.md', 'ref.md', 'run.sh'):
            self.assertEqual((target / 'a' / name).read_bytes(), (staged / 'skills' / 'a' / name).read_bytes())
        self.assertTrue(os.access(target / 'a' / 'run.sh', os.X_OK))
        self.assertEqual(len(lines), 3, lines)
        for name in ('skills/b', 'skills/c', 'skills/a/link'):
            self.assertEqual(sum(name in line.split(': ')[0] for line in lines), 1, (name, lines))
        self.assertEqual(lines, scan(staged))                           # the same walk names the same entries

    def test_files_beside_the_skills_are_ignored_without_a_line(self):
        staged = tree(self)
        skill(staged, 'a'); write(staged / 'skills' / 'README.md', 'index')
        write(staged / 'skills' / '.DS_Store', b'\0')
        self.assertEqual((materialise(staged), files(staged / '.agents' / 'skills')), ([], ['a', 'a/SKILL.md']))
        self.assertEqual(workspace_problems(staged), [])               # nothing blocks a candidate's snapshot

    def test_caps_leave_out_skills_and_files_with_a_line(self):
        staged = tree(self)
        skill(staged, 'a'); skill(staged, 'b'); os.symlink('/etc', staged / 'skills' / 'c')
        with mock.patch.object(skills, 'MAX_SKILLS', 1):
            lines = materialise(staged)
        self.assertEqual(files(staged / '.agents' / 'skills'), ['a', 'a/SKILL.md'])
        [line] = lines                                                  # one line, then the walk stops
        self.assertTrue(line.startswith('skills/b: the skills have reached the limit of 1 skills;'), lines)
        # (constant, value), files of skill a besides SKILL.md, what is copied besides a/SKILL.md, what is named.
        # Once the files are full the walk stops: one line names where, and skill b is not looked at.
        for patch, extra, kept, named in (
                (('MAX_FILES', 3), {'f1': 'x', 'f2': 'x', 'f3': 'x'}, ['a/f1', 'a/f2'], ['skills/a/f3']),
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
            self.assertEqual([line.split(': ')[0] for line in lines], named, (patch, lines))
        visited = []
        original = os.stat

        def counting(name, *args, **kwargs):
            visited.append(name)
            return original(name, *args, **kwargs)

        staged = tree(self)
        skill(staged, 'a')
        for number in range(50):
            write(staged / 'skills' / 'a' / f'f{number:02d}')
        with mock.patch.object(skills, 'MAX_FILES', 2), mock.patch.object(skills.os, 'stat', counting):
            self.assertEqual([line.split(': ')[0] for line in materialise(staged)], ['skills/a/f01'])
        self.assertLess(len(visited), 10, visited)                      # it stopped looking

    def test_copy_failures_are_lines_within_the_cap(self):
        staged = tree(self)
        for name in 'abcde':
            skill(staged, name)

        def refuse(self):
            raise OSError(errno.ENOSPC, 'No space left on device')

        with mock.patch.object(workspace, 'MAX_LINES', 2), mock.patch.object(skills._Target, 'open', refuse):
            lines = materialise(staged)
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(all('could not be put into .agents/skills/ (No space' in line for line in lines[:2]), lines)
        self.assertEqual(lines[2], 'and 3 more of the same kind, not listed one by one')
        self.assertFalse(os.path.lexists(staged / '.agents'))

    def test_an_agents_entry_from_a_checkout_is_removed_first(self):
        names = ['.agents'] + (['.Agents', '.AGENTS', ALIAS] if folds(self, ALIAS, '.agents') else [])
        for name in names:
            staged = tree(self)
            write(staged / name / 'skills' / 'old' / 'SKILL.md', 'old')
            skill(staged, 'new')
            materialise(staged)
            self.assertEqual(os.listdir(staged / '.agents' / 'skills'), ['new'], name)
            self.assertEqual(sorted(os.listdir(staged)), ['.agents', 'skills'], name)
        staged = tree(self)
        write(staged / '.agents', 'a file')                                 # not even a folder
        self.assertEqual((materialise(staged), os.listdir(staged)), ([], []))   # no skills/: no .agents at all

    def test_an_agents_entry_that_does_not_go_away_stops_the_staging(self):
        staged = tree(self)
        skill(staged, 'a'); (staged / '.agents').mkdir()
        with mock.patch.object(skills, 'remove', lambda *args: None):      # it is still there after each removal
            with self.assertRaisesRegex(OSError, '.agents'):
                materialise(staged)
        with mock.patch.object(skills, 'remove', side_effect=PermissionError(errno.EPERM, 'Operation not permitted')):
            with self.assertRaises(PermissionError):
                materialise(staged)
        self.assertEqual(os.listdir(staged / '.agents'), [])             # nothing was copied into it

    def test_nothing_to_copy_makes_no_agents_folder(self):
        staged = tree(self)
        self.assertEqual((materialise(staged), os.listdir(staged)), ([], []))
        write(staged / 'skills' / 'b' / 'README.md')
        self.assertEqual((len(materialise(staged)), sorted(os.listdir(staged))), (1, ['skills']))
        outside = tree(self)
        skill(outside, 'a')                                                 # outside/skills/a/SKILL.md
        for odd in ('file', 'link', 'link to skills'):
            staged = tree(self)
            if odd == 'file':
                write(staged / 'skills', 'a file')
            elif odd == 'link':
                os.symlink(outside / 'skills', staged / 'skills')
            else:
                os.symlink(outside / 'skills', staged / 'skills')
                if folds(self, 'ſkills', 'skills'):                     # ſkills: the same entry as skills
                    os.unlink(staged / 'skills'); os.symlink(outside / 'skills', staged / 'ſkills')
            lines = materialise(staged)
            self.assertEqual((len(lines), os.path.lexists(staged / '.agents')), (1, False), odd)
            self.assertTrue(lines[0].startswith('skills: '), lines)
            self.assertEqual(lines, workspace_problems(staged))
        self.assertEqual(files(outside), ['skills', 'skills/a', 'skills/a/SKILL.md'])

    def test_meta_is_told_through_workspace_problems_and_nothing_is_copied(self):
        candidate = tree(self)
        skill(candidate, 'a'); write(candidate / 'skills' / 'b' / 'README.md')
        os.symlink('/etc/hosts', candidate / 'skills' / 'a' / 'link')
        problems = workspace_problems(candidate)
        self.assertEqual([line.split(': ')[0] for line in problems], ['skills/a/link', 'skills/b'])
        self.assertFalse(os.path.lexists(candidate / '.agents'))


class Aliases(unittest.TestCase):
    """Fix round 1, C1: a name the file system takes for `.agents` never leads the copy out of the staged tree, and
    never reaches a version."""

    def setUp(self):
        needs_folding(self)

    def test_an_alias_link_never_leads_the_copy_outside(self):
        for variant in ('alias to outside', 'alias to a folder whose skills is outside'):
            staged, outside = tree(self), tree(self)
            skill(staged, 'LaunchAgents'); write(staged / 'skills' / 'LaunchAgents' / 'payload.plist', 'p')
            if variant == 'alias to outside':
                os.symlink(outside, staged / ALIAS)
            else:
                (staged / 'foo').mkdir(); os.symlink('foo', staged / ALIAS)
                os.symlink(outside, staged / 'foo' / 'skills')
            self.assertEqual(materialise(staged), [], variant)
            self.assertEqual(os.listdir(outside), [], variant)
            self.assertFalse((staged / '.agents').is_symlink())
            expected = ['.agents', 'skills'] + (['foo'] if 'folder' in variant else [])
            self.assertEqual(sorted(os.listdir(staged)), sorted(expected), variant)
            self.assertEqual(files(staged / '.agents'), ['skills', 'skills/LaunchAgents',
                                                         'skills/LaunchAgents/SKILL.md',
                                                         'skills/LaunchAgents/payload.plist'], variant)

    def test_a_version_holding_an_alias_stages_nothing_outside(self):
        with Root(passed_check=False) as root:
            outside = tree(self)
            skill(root.layout.work, 'LaunchAgents'); write(root.layout.work / 'skills' / 'LaunchAgents' / 'x.plist')
            os.symlink(outside, root.layout.work / ALIAS)
            with mock.patch.object(versions, 'same_entries', lambda fd, names: set()):     # as before the fix
                self.assertEqual(root.store.commit(root.layout.work, 'owner', 'an old version with an alias'), 'V0002')
            self.assertIn(ALIAS, root.store.diff('V0001', 'V0002'))
            os.rename(root.layout.work, root.layout.root / 'old-work')
            problems = []
            with labrun.root_descriptor(root.layout) as root_fd:
                staged, _ = labrun.stage(root.layout, root.store, 'V0002')
                labrun.move_in(root.layout, root.records, root_fd, staged, 'work', {}, problems)
            self.assertEqual((os.listdir(outside), problems), ([], []))
            self.assertFalse((root.layout.work / '.agents').is_symlink())
            self.assertTrue((root.layout.work / '.agents' / 'skills' / 'LaunchAgents' / 'x.plist').is_file())

    def test_an_alias_is_never_entered_or_versioned(self):
        with Root(passed_check=False) as root:
            sealed = root.layout.root / 'sealed-tree'
            write(sealed / 'f.txt', 'f')
            without = root.store.tree_of_dir(sealed)
            write(sealed / ALIAS / 'deep' / '.git' / 'config', '[core]\n')      # a nested .git in it blocks nothing
            os.chmod(sealed / ALIAS / 'deep', 0o500)                     # Root's cleanup unlocks it again
            lines, moved, blocked = prepare_for_commit(sealed, root.layout.sealed / 'L' / 'left-out', 'left-out')
            self.assertEqual((lines, moved, blocked), ([], 0, 0))
            self.assertEqual(os.stat(sealed / ALIAS / 'deep').st_mode & 0o777, 0o500)    # not entered, not unlocked
            self.assertEqual(root.store.commit(sealed, 'owner', 'x'), 'V0002')
            self.assertEqual(root.store.tree_of('V0002'), without)

    def test_codex_is_found_by_asking_the_file_system(self):
        needs_folding(self, '.CODEX', '.codex')
        candidate = tree(self)
        (candidate / '.CODEX').mkdir()
        self.assertIn('The workspace holds .codex', workspace_problems(candidate))
        self.assertTrue(workspace.has_entry(candidate, '.codex'))
        self.assertFalse(workspace.has_entry(candidate, '.missing'))


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
