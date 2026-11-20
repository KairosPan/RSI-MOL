import os
import resource
import time
import unittest
from unittest import mock

import stat
import subprocess
import sys

from rsi import workspace
from rsi.workspace import (clear, empty_folder, prepare_for_commit, quarantine, raise_open_files_limit, real_folder,
                           remove)
from tests.support import Root

DEPTH = 300


def few_open_files(test: unittest.TestCase) -> None:
    """macOS's default for a program: 256 open files, for the rest of the test."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    test.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))


class ControlFolders(unittest.TestCase):
    def test_control_folders_are_made_real(self):
        with Root() as root:
            work, outside = root.layout.work, root.layout.root / 'outside'
            outside.mkdir(); (outside / 'x').write_text('x')
            os.symlink(outside, work / '.tmp')
            self.assertEqual(len(real_folder(work, '.tmp')), 1)
            self.assertTrue((work / '.tmp').is_dir()); self.assertFalse((work / '.tmp').is_symlink())
            self.assertEqual(os.listdir(outside), ['x']); self.assertEqual(real_folder(work, '.tmp'), [])
            (work / '.tmp' / 'y').write_text('kept')                            # a real folder is left as it is
            (work / '.tmp').chmod(0)                                            # ... and opened again for the owner
            self.assertEqual(real_folder(work, '.tmp'), []); self.assertTrue((work / '.tmp' / 'y').is_file())

    def test_deep_trees_are_removed_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            deep = root.layout.trial / ('a/' * DEPTH); deep.mkdir(parents=True); (deep / 'f').write_text('f')
            (root.layout.trial / 'a' / 'a').chmod(0)
            empty_folder(root.layout.trial)
            self.assertEqual(os.listdir(root.layout.trial), [])
            (root.layout.work / ('b/' * DEPTH)).mkdir(parents=True)
            fd = workspace.open_folder(root.layout.work)
            try:
                remove('b', fd)
            finally:
                os.close(fd)
            self.assertFalse(os.path.lexists(root.layout.work / 'b'))

    def test_open_files_limit_is_raised(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        raise_open_files_limit()
        self.assertGreater(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 256)


class BeforeCommit(unittest.TestCase):
    def test_nested_git_entries_are_set_aside(self):
        with Root() as root:
            work, outside = root.layout.work, root.layout.root / 'outside'
            (work / 'sub' / '.git').mkdir(parents=True); (work / 'sub' / '.git' / 'config').write_text('c')
            (work / 'sub' / '.git.rsi-kept').mkdir()                           # the name is taken: -2
            (work / 'a' / 'b').mkdir(parents=True); (work / 'a' / 'b' / '.GIT').write_text('gitdir: x')
            (work / 'odd').mkdir(); (work / 'odd' / 'git~1').write_text('x'); (work / 'odd' / '.git ').mkdir()
            os.symlink('target', work / 'odd' / '.gitmodules')
            (outside / '.git').mkdir(parents=True); os.symlink(outside, work / 'linked')
            (work / 'out').mkdir(); (work / 'out' / '.git').mkdir()                # never versioned: left alone
            problems = prepare_for_commit(work, root.layout.library, 'manual')
            self.assertEqual(len(problems), 5)
            self.assertTrue(any('sub/.git' in line and 'sub/.git.rsi-kept-2' in line for line in problems))
            self.assertEqual((work / 'sub' / '.git.rsi-kept-2' / 'config').read_text(), 'c')
            self.assertTrue((work / 'a' / 'b' / '.git.rsi-kept').is_file())
            self.assertEqual(sorted(os.listdir(work / 'odd')),
                             ['.git.rsi-kept', '.git.rsi-kept-2', '.gitmodules.rsi-kept'])
            self.assertTrue((outside / '.git').is_dir()); self.assertTrue((work / 'out' / '.git').is_dir())
            self.assertIsNotNone(root.store.commit(work, 'owner (manual)', 'x'))   # versioning goes through now
            self.assertEqual(prepare_for_commit(work, root.layout.library, 'manual'), [])

    def test_thousands_of_look_alikes_are_quick_and_reported_in_few_lines(self):
        with Root() as root:
            many = root.layout.work / 'many'; many.mkdir()
            names = ['.git' + format(number, '012b').replace('0', ' ').replace('1', '.') for number in range(3000)]
            for name in names:
                (many / name).write_text('x')
            started = time.monotonic()
            problems = prepare_for_commit(root.layout.work, root.layout.library, 'manual')
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(len(problems), workspace.MAX_LINES + 1); self.assertIn('2900', problems[-1])
            self.assertEqual(sorted(os.listdir(many)),
                             sorted(['.git.rsi-kept'] + [f'.git.rsi-kept-{n}' for n in range(2, 3001)]))

    def test_a_deep_tree_is_moved_aside_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            deep = root.layout.work / ('a/' * DEPTH); (deep / '.git').mkdir(parents=True)
            (deep / '.git' / 'config').write_text('c'); (root.layout.work / 'a' / 'keep.txt').write_text('k')
            problems = prepare_for_commit(root.layout.work, root.layout.library, 'manual')
            self.assertEqual(len(problems), 1); self.assertIn('library/moved/manual/a', problems[0])
            moved = root.layout.library / 'moved' / 'manual' / ('a/' * (workspace.MAX_DEPTH + 1))
            self.assertTrue(moved.is_dir())
            self.assertEqual(len(str(root.layout.work / 'a').split('/')) + workspace.MAX_DEPTH - 1,
                             max(len(top.split('/')) for top, _, _ in os.walk(root.layout.work)))
            self.assertIsNotNone(root.store.commit(root.layout.work, 'owner (manual)', 'deep'))

    def test_unreadable_entries_are_opened_or_moved_aside(self):
        with Root() as root:
            work = root.layout.work; (work / 'notes' / 'locked').mkdir(parents=True)
            (work / 'notes' / 'locked' / 'a.txt').write_text('a'); (work / 'notes' / 'locked' / 'a.txt').chmod(0)
            (work / 'notes' / 'locked').chmod(0)
            (work / 'notes' / 'shared.txt').write_text('s')
            os.link(work / 'notes' / 'shared.txt', root.layout.env / 's')
            (work / 'notes' / 'shared.txt').chmod(0)
            problems = prepare_for_commit(work, root.layout.library, 'manual')
            self.assertEqual(len(problems), 1); self.assertIn('notes/shared.txt', problems[0])
            self.assertEqual((work / 'notes' / 'locked' / 'a.txt').read_text(), 'a')
            moved = root.layout.library / 'moved' / 'manual' / 'notes' / 'shared.txt'
            self.assertEqual(os.stat(moved).st_mode & 0o777, 0)                 # a shared inode is never changed
            moved.chmod(0o600)
            self.assertIsNotNone(root.store.commit(work, 'owner (manual)', 'readable'))

    def test_big_files_are_moved_aside_and_library_links_are_not_followed(self):
        with Root() as root:
            work, outside = root.layout.work, root.layout.root / 'outside'
            outside.mkdir(); (work / 'data').mkdir()
            with open(work / 'data' / 'huge.bin', 'wb') as stream:
                stream.truncate(workspace.BIG_FILE + 1)                         # sparse: nothing allocated
            (work / 'data' / 'small.txt').write_text('s')
            os.symlink(outside, root.layout.library / 'moved')                  # planted by the Lab
            problems = prepare_for_commit(work, root.layout.library, 'R0007')
            self.assertEqual(len(problems), 1); self.assertIn('library/moved-2/R0007/data/huge.bin', problems[0])
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual((root.layout.library / 'moved-2' / 'R0007' / 'data' / 'huge.bin').stat().st_size,
                             workspace.BIG_FILE + 1)
            self.assertEqual(os.listdir(work / 'data'), ['small.txt'])
            with mock.patch.object(workspace, 'BIG_FILE', 0):
                prepare_for_commit(work, root.layout.library, 'R0007')
            self.assertTrue((root.layout.library / 'moved-2' / 'R0007' / 'data' / 'small.txt').is_file())



class NothingTheLabLeavesBlocks(unittest.TestCase):
    """Fix round 2: taken names never run out, unwritable deep folders still move, an unusable library/ is avoided."""

    def test_a_thousand_taken_names_do_not_stop_the_set_aside(self):
        with Root() as root:
            work = root.layout.work; (work / '.git').mkdir(); (work / '.git' / 'config').write_text('c')
            for number in range(1, 1001):
                (work / ('.git.rsi-kept' if number == 1 else f'.git.rsi-kept-{number}')).write_text('taken')
            problems = prepare_for_commit(work, root.layout.library, 'manual')
            self.assertEqual(len(problems), 1); self.assertFalse(os.path.lexists(work / '.git'))
            self.assertIsNotNone(root.store.commit(work, 'owner (manual)', 'kept'))

    def test_a_thousand_taken_names_do_not_stop_a_removal(self):
        with Root() as root:
            out = root.layout.work / 'out' / 'T1'                 # '-' sorts before '.': the chain is emptied first
            (out / '-chain' / ('a/' * workspace.MAX_DEPTH)).mkdir(parents=True)
            for number in range(1, 1001):
                (out / ('.rsi-deep' if number == 1 else f'.rsi-deep-{number}')).write_text('taken')
            self.assertEqual(clear(root.layout.work, 'out', 'T1'), [])
            self.assertFalse(os.path.lexists(root.layout.work / 'out'))

    def test_a_thousand_taken_library_names_do_not_stop_a_move(self):
        with Root() as root:
            library = root.layout.library
            for number in range(1, 1001):
                (library / ('moved' if number == 1 else f'moved-{number}')).write_text('taken')
            (root.layout.work / 'big.bin').write_text('x' * 20)
            with mock.patch.object(workspace, 'BIG_FILE', 10):
                problems = prepare_for_commit(root.layout.work, library, 'manual')
            self.assertEqual(len(problems), 1); self.assertIn('library/moved-', problems[0])
            self.assertFalse(os.path.lexists(root.layout.work / 'big.bin'))

    def test_an_unwritable_folder_at_the_depth_limit_still_moves(self):
        with Root() as root:
            trial = root.layout.trial; (trial / ('a/' * 51)).mkdir(parents=True)
            (trial / ('a/' * 48)).chmod(0)
            empty_folder(trial); self.assertEqual(os.listdir(trial), [])
            work = root.layout.work; (work / ('b/' * 51)).mkdir(parents=True); (work / 'b' / 'keep.txt').write_text('k')
            (work / ('b/' * 49)).chmod(0)
            problems = prepare_for_commit(work, root.layout.library, 'manual')
            self.assertEqual(len(problems), 1); self.assertIn('library/moved/manual/b', problems[0])
            self.assertIsNotNone(root.store.commit(work, 'owner (manual)', 'moved'))

    def test_an_unusable_library_is_opened_or_avoided(self):
        with Root() as root:
            work, library = root.layout.work, root.layout.library
            for case in ('mode 000', 'moved 555', 'a link'):
                with open(work / 'big.bin', 'wb') as stream:
                    stream.truncate(11)
                if case == 'mode 000':
                    library.chmod(0)
                elif case == 'moved 555':
                    (library / 'moved').chmod(0o555)
                else:
                    outside = root.layout.root / 'outside'; outside.mkdir()
                    os.rename(library, root.layout.root / 'library-old'); os.symlink(outside, library)
                with mock.patch.object(workspace, 'BIG_FILE', 10):
                    problems = prepare_for_commit(work, library, 'manual')
                self.assertEqual(len(problems), 1, case); self.assertFalse(os.path.lexists(work / 'big.bin'), case)
            self.assertIn('.tmp/rsi-moved-big.bin', problems[0]); self.assertEqual(os.listdir(outside), [])
            self.assertTrue((work / '.tmp' / 'rsi-moved-big.bin').is_file())
            self.assertEqual(sorted(os.listdir(root.layout.root / 'library-old' / 'moved' / 'manual')),
                             ['big.bin', 'big.bin-2'])
            root.store.commit(work, 'owner (manual)', 'no big file')                  # nothing left to refuse


class Quarantine(unittest.TestCase):
    def test_any_entry_is_set_apart_and_replaced_by_a_fresh_folder(self):
        with Root() as root:
            outside = root.layout.root / 'outside'; outside.mkdir(); (outside / 'x').write_text('x')
            work = root.layout.work
            cases = []
            work.rename(root.layout.root / 'work-old'); os.symlink(outside, work); cases.append('link')
            moved = quarantine(root.layout, work, 'work')
            self.assertEqual(moved, root.layout.root / 'quarantine' / 'work-1'); self.assertTrue(moved.is_symlink())
            self.assertEqual(os.listdir(outside), ['x'])
            self.assertTrue(work.is_dir() and not work.is_symlink()); self.assertEqual(os.listdir(work), [])
            (work / 'f').write_text('f'); work.chmod(0)                                 # mode 000
            self.assertEqual(quarantine(root.layout, work, 'work'), root.layout.root / 'quarantine' / 'work-2')
            (work / 'f').write_text('f'); os.chflags(work, stat.UF_IMMUTABLE)            # chflags uchg
            self.assertEqual(quarantine(root.layout, work, 'work'), root.layout.root / 'quarantine' / 'work-3')
            os.rmdir(work); work.write_text('a file')                                    # a file
            self.assertEqual(quarantine(root.layout, work, 'work'), root.layout.root / 'quarantine' / 'work-4')
            self.assertTrue(work.is_dir()); self.assertEqual(os.listdir(work), [])
            os.rmdir(work)                                                               # nothing at all
            self.assertIsNone(quarantine(root.layout, work, 'work')); self.assertTrue(work.is_dir())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS ACLs')
    def test_a_deny_acl_does_not_stop_it(self):
        with Root() as root:
            work = root.layout.work; (work / 'f').write_text('f')
            rights = ('write,delete,add_subdirectory,add_file,delete_child,writeattr,writeextattr,writesecurity,'
                      'readsecurity,list,search')
            subprocess.run(['/bin/chmod', '+a', f'{os.environ["USER"]} deny {rights}', str(work)], check=True)
            moved = quarantine(root.layout, work, 'work')
            self.assertEqual(sorted(os.listdir(moved)), ['AGENTS.md', 'f']); self.assertEqual(os.listdir(work), [])


if __name__ == '__main__':
    unittest.main()
