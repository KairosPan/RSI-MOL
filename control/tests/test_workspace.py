import os
import resource
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from rsi import workspace
from rsi.workspace import (free_name, keep, open_folder, prepare_for_commit, raise_open_files_limit, remove, seal)
from tests.support import Root

DEPTH = 300
DENY_ALL = ('write,delete,add_subdirectory,add_file,delete_child,writeattr,writeextattr,writesecurity,readsecurity,'
            'list,search')
macos = unittest.skipUnless(sys.platform == 'darwin', 'macOS flags and ACLs')


def few_open_files(test: unittest.TestCase) -> None:
    """macOS's default for a program: 256 open files, for the rest of the test."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    test.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))


def deny(path: Path, rights: str) -> None:
    subprocess.run(['/bin/chmod', '+a', f'{os.environ["USER"]} deny {rights}', str(path)], check=True)


def prepare(root: Root, tree: Path) -> tuple[list[str], int, int]:
    return prepare_for_commit(tree, root.layout.sealed / 'L' / 'left-out', 'sealed/L/left-out')


def removed(test: unittest.TestCase, parent: Path, name: str) -> None:
    fd = open_folder(parent)
    try:
        remove(name, fd, str(parent / name))
    finally:
        os.close(fd)
    test.assertFalse(os.path.lexists(parent / name))


class Seal(unittest.TestCase):
    def test_any_entry_is_sealed_by_one_rename_in_the_root(self):
        with Root() as root:
            fd = open_folder(root.layout.root); self.addCleanup(os.close, fd)
            outside = root.layout.root / 'outside'; outside.mkdir(); (outside / 'x').write_text('x')
            work = root.layout.work
            self.assertEqual(seal(fd, 'work', 'a'), '.sealed-work-a')               # a folder
            self.assertEqual((root.layout.root / '.sealed-work-a' / 'AGENTS.md').read_text(), 'hello\n')
            self.assertIsNone(seal(fd, 'work', 'b'))                                 # nothing there
            os.symlink(outside, work); self.assertEqual(seal(fd, 'work', 'c'), '.sealed-work-c')   # a link
            self.assertTrue((root.layout.root / '.sealed-work-c').is_symlink())
            self.assertEqual(os.listdir(outside), ['x'])
            work.mkdir(); (work / 'f').write_text('f'); work.chmod(0)                # mode 000
            self.assertEqual(seal(fd, 'work', 'd'), '.sealed-work-d')
            work.write_text('a file'); self.assertEqual(seal(fd, 'work', 'e'), '.sealed-work-e')
            work.mkdir()
            with self.assertRaises(FileExistsError): seal(fd, 'work', 'a')           # never over an earlier seal
            self.assertTrue(work.is_dir())

    @macos
    def test_acls_and_flags_on_the_entry(self):
        with Root() as root:
            fd = open_folder(root.layout.root); self.addCleanup(os.close, fd)
            work = root.layout.work; deny(work, 'list,search')
            self.assertEqual(seal(fd, 'work', 'a'), '.sealed-work-a')                # a deny-search ACL: sealed
            work.mkdir(); os.chflags(work, stat.UF_IMMUTABLE)
            with self.assertRaises(PermissionError): seal(fd, 'work', 'b')           # an error, never a loop
            os.chflags(work, 0); deny(work, 'delete')
            with self.assertRaises(PermissionError): seal(fd, 'work', 'c')

    def test_keep_moves_any_sealed_entry_aside(self):
        with Root() as root:
            fd = open_folder(root.layout.root); self.addCleanup(os.close, fd)
            outside = root.layout.root / 'outside'; outside.mkdir()
            (root.layout.work / 'f').write_text('f'); root.layout.work.chmod(0)
            entry = seal(fd, 'work', 'a'); path = str(root.layout.root / entry)
            keep(fd, entry, path, root.layout.sealed / 'a' / 'work')
            self.assertEqual((root.layout.sealed / 'a' / 'work' / 'f').read_text(), 'f')
            os.symlink(outside, root.layout.work); entry = seal(fd, 'work', 'b')
            keep(fd, entry, str(root.layout.root / entry), root.layout.sealed / 'b' / 'work')
            self.assertTrue((root.layout.sealed / 'b' / 'work').is_symlink()); self.assertEqual(os.listdir(outside), [])


class Removal(unittest.TestCase):
    def test_deep_trees_are_removed_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            tree = root.layout.root / '.sealed-trial-a'
            deep = tree / ('a/' * DEPTH); deep.mkdir(parents=True); (deep / 'f').write_text('f')
            (tree / 'a' / 'a').chmod(0)
            removed(self, root.layout.root, '.sealed-trial-a')

    def test_links_are_removed_not_followed(self):
        with Root() as root:
            outside = root.layout.root / 'outside'; outside.mkdir(); (outside / 'x').write_text('x')
            tree = root.layout.root / 'tree'; tree.mkdir(); os.symlink(outside, tree / 'in')
            removed(self, root.layout.root, 'tree'); self.assertEqual(os.listdir(outside), ['x'])

    def test_a_thousand_taken_names_do_not_stop_a_removal(self):
        with Root() as root:
            tree = root.layout.root / 'tree'                       # '-' sorts before '.': the chain is emptied first
            (tree / '-chain' / ('a/' * workspace.MAX_DEPTH)).mkdir(parents=True)
            for number in range(1, 1001):
                (tree / ('.rsi-deep' if number == 1 else f'.rsi-deep-{number}')).write_text('taken')
            removed(self, root.layout.root, 'tree')

    def test_every_loop_has_a_cap(self):
        with Root() as root:
            tree = root.layout.root / 'tree'; (tree / ('a/' * (2 * workspace.MAX_DEPTH + 1))).mkdir(parents=True)
            fd = open_folder(root.layout.root); self.addCleanup(os.close, fd)
            with mock.patch.object(workspace, 'MAX_PASSES', 1):
                with self.assertRaises(OSError): remove('tree', fd, str(tree))
            for name in ['x'] + [f'x-{number}' for number in range(2, 70)]:
                (root.layout.root / name).write_text('taken')
            with mock.patch.object(workspace.secrets, 'token_hex', return_value='2'):
                with self.assertRaises(FileExistsError): free_name(fd, 'x')

    @macos
    def test_flags_and_acls_do_not_stop_a_removal(self):
        with Root() as root:
            tree = root.layout.root / 'tree'; (tree / '.tmp' / 'sub').mkdir(parents=True)
            (tree / '.tmp' / 'sub' / 'f').write_text('f'); os.chflags(tree / '.tmp' / 'sub' / 'f', stat.UF_IMMUTABLE)
            os.chflags(tree / '.tmp' / 'sub', stat.UF_APPEND); deny(tree / '.tmp', 'list,search,delete_child')
            (tree / 'out').mkdir(); (tree / 'out' / 'kept.txt').write_text('k')
            deny(tree / 'out' / 'kept.txt', 'delete')                         # one deny-delete ACE on a file
            deny(tree / 'out', 'delete')
            removed(self, root.layout.root, 'tree')


class BeforeCommit(unittest.TestCase):
    def test_nested_git_entries_are_set_aside(self):
        with Root() as root:
            tree, outside = root.layout.work, root.layout.root / 'outside'
            (tree / 'sub' / '.git').mkdir(parents=True); (tree / 'sub' / '.git' / 'config').write_text('c')
            (tree / 'sub' / '.git.rsi-kept').mkdir()                           # the name is taken: -2
            (tree / 'a' / 'b').mkdir(parents=True); (tree / 'a' / 'b' / '.GIT').write_text('gitdir: x')
            (tree / 'odd').mkdir(); (tree / 'odd' / 'git~1').write_text('x'); (tree / 'odd' / '.git ').mkdir()
            os.symlink('target', tree / 'odd' / '.gitmodules')
            (outside / '.git').mkdir(parents=True); os.symlink(outside, tree / 'linked')
            (tree / 'out').mkdir(); (tree / 'out' / '.git').mkdir()                # never versioned: left alone
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (5, 0, 0))
            self.assertTrue(any('sub/.git' in line and 'sub/.git.rsi-kept-2' in line for line in lines))
            self.assertEqual((tree / 'sub' / '.git.rsi-kept-2' / 'config').read_text(), 'c')
            self.assertTrue((tree / 'a' / 'b' / '.git.rsi-kept').is_file())
            self.assertEqual(sorted(os.listdir(tree / 'odd')),
                             ['.git.rsi-kept', '.git.rsi-kept-2', '.gitmodules.rsi-kept'])
            self.assertTrue((outside / '.git').is_dir()); self.assertTrue((tree / 'out' / '.git').is_dir())
            self.assertIsNotNone(root.store.commit(tree, 'manual or leftover', 'x'))   # versioning goes through
            self.assertEqual(prepare(root, tree), ([], 0, 0))

    def test_thousands_of_look_alikes_are_quick_and_reported_in_few_lines(self):
        with Root() as root:
            many = root.layout.work / 'many'; many.mkdir()
            names = ['.git' + format(number, '012b').replace('0', ' ').replace('1', '.') for number in range(3000)]
            for name in names:
                (many / name).write_text('x')
            started = time.monotonic()
            lines, moved, blocked = prepare(root, root.layout.work)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual((len(lines), moved, blocked), (workspace.MAX_LINES + 1, 0, 0))
            self.assertIn('2900', lines[-1])
            self.assertEqual(sorted(os.listdir(many)),
                             sorted(['.git.rsi-kept'] + [f'.git.rsi-kept-{n}' for n in range(2, 3001)]))

    def test_a_thousand_taken_names_do_not_stop_the_set_aside(self):
        with Root() as root:
            tree = root.layout.work; (tree / '.git').mkdir(); (tree / '.git' / 'config').write_text('c')
            for number in range(1, 1001):
                (tree / ('.git.rsi-kept' if number == 1 else f'.git.rsi-kept-{number}')).write_text('taken')
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 0, 0)); self.assertFalse(os.path.lexists(tree / '.git'))
            self.assertIsNotNone(root.store.commit(tree, 'manual or leftover', 'kept'))

    def test_a_deep_tree_is_left_out_with_few_open_files(self):
        few_open_files(self)
        with Root() as root:
            tree = root.layout.work
            deep = tree / ('a/' * DEPTH); (deep / '.git').mkdir(parents=True)
            (deep / '.git' / 'config').write_text('c'); (tree / 'a' / 'keep.txt').write_text('k')
            (tree / ('a/' * workspace.MAX_DEPTH)).chmod(0)                       # unwritable at the limit
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 1, 0)); self.assertIn('sealed/L/left-out/1-a', lines[0])
            moved = root.layout.sealed / 'L' / 'left-out' / '1-a' / ('a/' * (DEPTH - workspace.MAX_DEPTH - 1))
            self.assertTrue((moved / '.git' / 'config').is_file())
            self.assertIsNotNone(root.store.commit(tree, 'manual or leftover', 'deep'))

    def test_unreadable_entries_are_opened_or_left_out(self):
        with Root() as root:
            tree = root.layout.work; (tree / 'notes' / 'locked').mkdir(parents=True)
            (tree / 'notes' / 'locked' / 'a.txt').write_text('a'); (tree / 'notes' / 'locked' / 'a.txt').chmod(0)
            (tree / 'notes' / 'locked').chmod(0)
            (tree / 'notes' / 'shared.txt').write_text('s')
            os.link(tree / 'notes' / 'shared.txt', root.layout.env / 's')
            (tree / 'notes' / 'shared.txt').chmod(0)
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 1, 0)); self.assertIn('notes/shared.txt', lines[0])
            self.assertEqual((tree / 'notes' / 'locked' / 'a.txt').read_text(), 'a')
            moved = root.layout.sealed / 'L' / 'left-out' / '1-shared.txt'
            self.assertEqual(os.stat(moved).st_mode & 0o777, 0)                 # a shared inode is never changed
            self.assertIsNotNone(root.store.commit(tree, 'manual or leftover', 'readable'))

    @macos
    def test_flags_and_acls_are_cleared_on_the_entries_themselves(self):
        with Root() as root:
            tree = root.layout.work; (tree / 'locked').mkdir(); (tree / 'locked' / 'f.txt').write_text('f')
            (tree / 'locked' / '.git').mkdir(); os.chflags(tree / 'locked', stat.UF_IMMUTABLE)
            (tree / 'secret.txt').write_text('s'); deny(tree / 'secret.txt', 'read')
            (tree / 'hidden').mkdir(); (tree / 'hidden' / 'h.txt').write_text('h'); deny(tree / 'hidden', 'list,search')
            deny(tree, 'list,search')
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 0, 0)); self.assertIn('locked/.git.rsi-kept', lines[0])
            self.assertEqual(root.store.commit(tree, 'manual or leftover', 'unlocked'), 'V0002')
            self.assertIn('b/hidden/h.txt', root.store.diff('V0001', 'V0002'))
            self.assertIn('b/secret.txt', root.store.diff('V0001', 'V0002'))
            # An ACL that also denies reading itself cannot be cleared without following a link, and it denies
            # moving the folder too: it blocks the version (git would silently skip the folder).
            (tree / 'vault').mkdir(); (tree / 'vault' / 'v.txt').write_text('v'); deny(tree / 'vault', DENY_ALL)
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((moved, blocked), (0, 1)); self.assertTrue(any('vault' in line for line in lines))

    def test_big_files_are_left_out(self):
        with Root() as root:
            tree = root.layout.work; (tree / 'data').mkdir()
            with open(tree / 'data' / 'huge.bin', 'wb') as stream:
                stream.truncate(workspace.BIG_FILE + 1)                         # sparse: nothing allocated
            (tree / 'data' / 'small.txt').write_text('s')
            lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 1, 0)); self.assertIn('data/huge.bin', lines[0])
            self.assertEqual((root.layout.sealed / 'L' / 'left-out' / '1-huge.bin').stat().st_size,
                             workspace.BIG_FILE + 1)
            self.assertEqual(os.listdir(tree / 'data'), ['small.txt'])

    def test_a_name_is_cut_on_a_character_boundary(self):
        with Root() as root:
            tree, name = root.layout.work, '数' * 70 + '.bin'                  # 214 bytes: a cut at 200 splits a 数
            (tree / name).write_text('x' * 20)
            with mock.patch.object(workspace, 'BIG_FILE', 10):
                lines, moved, blocked = prepare(root, tree)
            self.assertEqual((moved, blocked), (1, 0)); self.assertFalse(os.path.lexists(tree / name))
            self.assertEqual(os.listdir(root.layout.sealed / 'L' / 'left-out'), ['1-' + '数' * 66])

    def test_what_cannot_be_moved_out_blocks_and_is_counted_once(self):
        with Root() as root:
            tree = root.layout.work; (tree / 'big.bin').write_text('x' * 20); (tree / 'ok.txt').write_text('ok')
            original = os.rename

            def refuse(source, *args, **options):
                if source == 'big.bin':
                    raise OSError(92, 'Illegal byte sequence')
                return original(source, *args, **options)

            with mock.patch.object(workspace, 'BIG_FILE', 10), mock.patch.object(os, 'rename', refuse):
                lines, moved, blocked = prepare(root, tree)
            self.assertEqual((len(lines), moved, blocked), (1, 0, 1)); self.assertIn('所以这次不存版本', lines[0])
            self.assertTrue((tree / 'big.bin').is_file())

    @macos
    def test_an_acl_or_a_flag_on_what_must_move_out(self):
        with Root() as root:
            tree = root.layout.work; (tree / 'denied.bin').write_text('x' * 20); deny(tree / 'denied.bin', 'delete')
            (tree / 'shared.bin').write_text('x' * 20); os.link(tree / 'shared.bin', root.layout.env / 'shared')
            os.chflags(tree / 'shared.bin', stat.UF_IMMUTABLE)               # uchg on an inode shared elsewhere
            with mock.patch.object(workspace, 'BIG_FILE', 10):
                lines, moved, blocked = prepare(root, tree)
            self.assertEqual((moved, blocked), (1, 1))
            self.assertEqual(os.listdir(root.layout.sealed / 'L' / 'left-out'), ['1-denied.bin'])
            self.assertTrue((tree / 'shared.bin').is_file())
            self.assertEqual(os.stat(root.layout.env / 'shared').st_flags & stat.UF_IMMUTABLE, stat.UF_IMMUTABLE)

    def test_a_tree_that_is_not_a_folder_is_an_error(self):
        with Root() as root:
            outside = root.layout.root / 'outside'; outside.mkdir()
            os.symlink(outside, root.layout.root / 'tree')
            with self.assertRaises(OSError): prepare(root, root.layout.root / 'tree')
            self.assertEqual(os.listdir(outside), [])


class Limits(unittest.TestCase):
    def test_open_files_limit_is_raised(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        raise_open_files_limit()
        self.assertGreater(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 256)


if __name__ == '__main__':
    unittest.main()
