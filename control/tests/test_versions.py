import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.versions import VersionStore


class Versions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.a = self.tmp / 'a'; self.b = self.tmp / 'b'; self.a.mkdir(); self.b.mkdir()
        self.store = VersionStore(self.tmp / 'versions' / 'lab.git'); self.store.init(); self.store.init()

    def test_commit_and_head(self):
        self.assertIsNone(self.store.head())
        (self.a / 'f.txt').write_text('1')
        self.assertEqual(self.store.commit(self.a, 'init', 'first'), 'V0001')
        self.assertIsNone(self.store.commit(self.a, 'lab R0001', 'same'))
        (self.a / 'f.txt').write_text('2')
        self.assertEqual(self.store.commit(self.a, 'lab R0001', 'second'), 'V0002')
        self.assertEqual(self.store.head(), 'V0002'); self.assertEqual(self.store.log()[0]['author'], 'lab R0001')
        self.assertEqual(self.store.tree_of_dir(self.a), self.store.tree_of('V0002'))
        self.assertFalse((self.a / '.git').exists())

    def test_checkout_makes_the_folder_exact(self):
        (self.a / 'f.txt').write_text('1'); (self.a / 'gone.txt').write_text('x')
        self.store.commit(self.a, 'init', 'one')
        (self.b / 'stray.txt').write_text('s'); (self.b / '.tmp').mkdir()
        (self.b / '.tmp' / 'keep').write_text('k')
        self.store.checkout('V0001', self.b)
        self.assertEqual(sorted(p.name for p in self.b.iterdir()), ['.tmp', 'f.txt', 'gone.txt'])
        (self.a / 'gone.txt').unlink(); self.store.commit(self.a, 'x', 'two'); self.store.checkout('V0002', self.b)
        self.assertFalse((self.b / 'gone.txt').exists()); self.assertTrue((self.b / '.tmp' / 'keep').exists())
        self.assertEqual(self.store.tree_of_dir(self.a), self.store.tree_of('V0002'))   # folder a is undisturbed

    def test_excluded_folders_are_not_versioned(self):
        (self.a / 'f.txt').write_text('1'); (self.a / 'out').mkdir(); (self.a / 'out' / 'r').write_text('r')
        self.store.commit(self.a, 'init', 'one'); self.store.checkout('V0001', self.b)
        self.assertFalse((self.b / 'out').exists())

    def test_snapshot_does_not_move_main(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b)
        (self.b / 'f.txt').write_text('changed'); commit = self.store.snapshot(self.b, 'M0001', 'trial 1')
        self.assertEqual(self.store.head(), 'V0001')
        self.assertEqual(self.store.tree_of(commit), self.store.tree_of_dir(self.b))
        self.assertIn('changed', self.store.diff('V0001', commit))

    def test_symlink_is_kept_as_a_link(self):
        os.symlink('/etc/hosts', self.a / 'link'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b)
        self.assertTrue(os.path.islink(self.b / 'link'))

    def test_resolve_refuses_other_names(self):
        for bad in ('../x', '--help', 'V1; rm'):
            with self.assertRaises(ValueError):
                self.store.resolve(bad)

    def test_resolve_accepts_versions_and_commit_ids_only_when_they_exist(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        commit = self.store.resolve('V0001')
        self.assertRegex(commit, r'^[0-9a-f]{40}$'); self.assertEqual(self.store.resolve(commit), commit)
        for bad in ('V0002', '0' * 40, self.store.tree_of('V0001'), commit[:12], 'V0001\n', 'refs/tags/V0001', 'HEAD'):
            with self.assertRaises(ValueError):
                self.store.resolve(bad)

    def test_in_folder_git_files_are_plain_content(self):
        (self.a / '.gitignore').write_text('*.csv\n'); (self.a / '.gitattributes').write_text('* ident text eol=lf\n')
        (self.a / 'd.csv').write_text('1,2\n'); (self.a / 'f.txt').write_bytes(b'x$Id$y\r\n')
        self.store.commit(self.a, 'init', 'one')
        (self.b / 'stray.csv').write_text('s'); self.store.checkout('V0001', self.b)
        self.assertEqual(sorted(p.name for p in self.b.iterdir()), ['.gitattributes', '.gitignore', 'd.csv', 'f.txt'])
        self.assertEqual((self.b / 'f.txt').read_bytes(), b'x$Id$y\r\n')

    def test_excluded_names_are_top_level_only_of_any_type_and_case(self):
        (self.a / 'sub' / 'out').mkdir(parents=True); (self.a / 'sub' / 'out' / 'r').write_text('r')
        (self.a / 'in').write_text('a file'); (self.a / 'OUT').write_text('a file'); (self.a / '.Tmp').mkdir()
        (self.a / '.Tmp' / 't').write_text('t')
        self.store.commit(self.a, 'init', 'one')
        (self.b / 'out').mkdir(); (self.b / 'out' / 'r').write_text('kept'); (self.b / 'in').write_text('kept')
        self.store.checkout('V0001', self.b)
        self.assertEqual(sorted(str(p.relative_to(self.b)) for p in self.b.rglob('*')),
                         ['in', 'out', 'out/r', 'sub', 'sub/out', 'sub/out/r'])
        self.assertEqual((self.b / 'out' / 'r').read_text(), 'kept')

    def test_nested_git_entries_are_refused_and_checkout_clears_them(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        (self.a / 'sub' / '.git').mkdir(parents=True); (self.a / 'sub' / '.git' / 'HEAD').write_text('x')
        with self.assertRaises(ValueError):
            self.store.commit(self.a, 'x', 'two')
        with self.assertRaises(ValueError):
            self.store.tree_of_dir(self.a)
        with self.assertRaises(ValueError):
            self.store.snapshot(self.a, 'M0001', 'x')
        self.assertEqual(self.store.head(), 'V0001')
        self.store.checkout('V0001', self.a)
        self.assertEqual(sorted(p.name for p in self.a.iterdir()), ['f.txt'])

    def test_unsafe_folders_rounds_and_authors_are_refused(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        (self.tmp / 'link').symlink_to(self.a); (self.tmp / 'file').write_text('x')
        for folder in (self.tmp / 'link', self.tmp, self.tmp / 'versions', self.tmp / 'file', self.tmp / 'missing'):
            with self.assertRaises(ValueError):
                self.store.checkout('V0001', folder)
            with self.assertRaises(ValueError):
                self.store.tree_of_dir(folder)
        self.assertTrue((self.store.git_dir / 'HEAD').is_file()); self.assertTrue((self.a / 'f.txt').is_file())
        for ref in ('../x', '-x', 'a/b', 'x.lock', ''):
            with self.assertRaises(ValueError):
                self.store.snapshot(self.a, ref, 'x')
        for author in ('', 'a\nb', 'a <b@c>', ' a', 'a\x01', 'a,'):
            with self.assertRaises(ValueError):
                self.store.commit(self.b, author, 'x')
        self.assertEqual(self.store.head(), 'V0001')

    def test_a_case_only_rename_of_a_file_is_a_new_version(self):
        (self.a / 'Model.py').write_text('1'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b)
        (self.a / 'Model.py').rename(self.a / 'model.py')
        self.assertEqual(self.store.commit(self.a, 'x', 'renamed'), 'V0002')
        self.store.checkout('V0002', self.b)
        self.assertEqual(os.listdir(self.b), ['model.py'])
        self.assertEqual(self.store.tree_of_dir(self.b), self.store.tree_of('V0002'))

    def test_a_case_only_rename_of_a_folder_is_a_new_version(self):
        (self.a / 'Pkg').mkdir(); (self.a / 'Pkg' / 'm.py').write_text('m'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b)
        (self.a / 'Pkg').rename(self.a / 'pkg')
        self.assertEqual(self.store.commit(self.a, 'x', 'renamed'), 'V0002')
        (self.a / 'pkg' / 'n.py').write_text('n'); self.assertEqual(self.store.commit(self.a, 'x', 'added'), 'V0003')
        self.store.checkout('V0003', self.b)
        self.assertEqual(sorted(str(p.relative_to(self.b)) for p in self.b.rglob('*')), ['pkg', 'pkg/m.py', 'pkg/n.py'])
        self.assertEqual(self.store.tree_of_dir(self.b), self.store.tree_of('V0003'))

    def test_a_case_variant_path_to_the_store_or_around_it_is_refused(self):
        variant = self.tmp.parent / self.tmp.name.swapcase()
        if not variant.exists():
            self.skipTest('the filesystem is case-sensitive')
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        for folder in (variant, variant / 'VERSIONS', variant / 'versions' / 'LAB.GIT',
                       variant / 'Versions' / 'lab.git' / 'REFS'):
            with self.assertRaises(ValueError):
                self.store.checkout('V0001', folder)
            with self.assertRaises(ValueError):
                self.store.tree_of_dir(folder)
        self.assertEqual(self.store.head(), 'V0001'); self.assertTrue((self.a / 'f.txt').is_file())

    def test_checkout_over_a_folder_replaced_by_a_link_leaves_the_link_target_alone(self):
        (self.a / 'd').mkdir(); (self.a / 'd' / 'f').write_text('1'); self.store.commit(self.a, 'init', 'one')
        outside = self.tmp / 'outside'; outside.mkdir()
        (outside / 'f').write_text('outside'); (outside / 'x').write_text('x')
        self.store.checkout('V0001', self.b)
        shutil.rmtree(self.b / 'd'); (self.b / 'd').symlink_to(outside)
        self.store.checkout('V0001', self.b)
        self.assertFalse((self.b / 'd').is_symlink()); self.assertEqual((self.b / 'd' / 'f').read_text(), '1')
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['f', 'x'])
        self.assertEqual((outside / 'f').read_text(), 'outside')

    def test_inherited_git_variables_and_the_owners_git_setup_are_ignored(self):
        home = self.tmp / 'home'; (home / 'hooks').mkdir(parents=True); (home / 'git').mkdir()
        hook = home / 'hooks' / 'reference-transaction'
        hook.write_text(f'#!/bin/sh\ntouch {self.tmp / "fired"}\n'); hook.chmod(0o755)
        (home / '.gitconfig').write_text(f'[core]\n\thooksPath = {home / "hooks"}\n')
        (home / 'git' / 'attributes').write_text('* ident\n')
        elsewhere = {'GIT_DIR': str(self.tmp / 'other.git'), 'GIT_WORK_TREE': str(self.tmp / 'other'),
                     'GIT_INDEX_FILE': str(self.tmp / 'other.index'), 'GIT_CONFIG_GLOBAL': str(home / '.gitconfig'),
                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                     'GIT_CONFIG_VALUE_0': str(home / 'hooks'), 'HOME': str(home), 'XDG_CONFIG_HOME': str(home)}
        with mock.patch.dict(os.environ, elsewhere):
            (self.a / 'f.txt').write_text('$Id$'); self.store.commit(self.a, 'init', 'one')
            self.store.snapshot(self.a, 'M0001', 'x'); self.store.checkout('V0001', self.b)
        self.assertEqual((self.b / 'f.txt').read_text(), '$Id$')
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['a', 'b', 'home', 'versions'])

    def test_log_and_a_cut_diff(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', '第一版')
        (self.a / 'f.txt').write_text('2' * 5000); self.store.commit(self.a, 'lab R0002', 'second\nline')
        log = self.store.log()
        self.assertEqual([entry['version'] for entry in log], ['V0002', 'V0001'])
        self.assertEqual([entry['message'] for entry in log], ['second\nline', '第一版'])
        self.assertEqual(log[0]['commit'], self.store.resolve('V0002'))
        self.assertEqual(log[0]['tree'], self.store.tree_of('V0002'))
        self.assertRegex(log[0]['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d')
        self.assertEqual(set(log[0]), {'version', 'commit', 'tree', 'author', 'message', 'time'})
        full = self.store.diff('V0001', 'V0002')
        self.assertIn('+' + '2' * 5000, full)
        cut = self.store.diff('V0001', 'V0002', limit=100)
        self.assertTrue(cut.startswith(full[:100])); self.assertLess(len(cut), 200)


if __name__ == '__main__':
    unittest.main()
