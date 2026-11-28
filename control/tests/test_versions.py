import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.versions import VersionStore
from tests.links import make_link


class Versions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.tmp = Path(temp.name).resolve()
        self.addCleanup(temp.cleanup)
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

    def test_agents_is_never_versioned_and_never_checked_out(self):
        (self.a / 'f.txt').write_text('1')
        without = self.store.tree_of_dir(self.a)
        for name in ('.agents', '.Agents'):                        # any letter case, like .tmp
            (self.a / name / 'skills' / 'x').mkdir(parents=True)
            (self.a / name / 'skills' / 'x' / 'SKILL.md').write_text('s')
            self.assertEqual(self.store.tree_of_dir(self.a), without, name)
            shutil.rmtree(self.a / name)
        (self.a / '.AGENTS').write_text('a file, not a folder')
        self.assertEqual(self.store.tree_of_dir(self.a), without)
        (self.a / '.AGENTS').unlink(); (self.a / '.agents' / 'skills').mkdir(parents=True)
        (self.a / '.agents' / 'skills' / 'SKILL.md').write_text('s')
        self.assertEqual(self.store.commit(self.a, 'init', 'one'), 'V0001')
        self.assertEqual(self.store.tree_of('V0001'), without)
        self.store.checkout('V0001', self.b)
        self.assertEqual(os.listdir(self.b), ['f.txt'])
        (self.a / 'sub' / '.agents').mkdir(parents=True); (self.a / 'sub' / '.agents' / 'k').write_text('k')
        self.assertEqual(self.store.commit(self.a, 'x', 'two'), 'V0002')     # only the top-level entry is excluded
        self.assertIn('b/sub/.agents/k', self.store.diff('V0001', 'V0002'))

    def test_a_name_the_file_system_takes_for_agents_is_never_versioned(self):
        probe = self.tmp / 'probe'; (probe / '.agentſ').mkdir(parents=True)
        if not os.path.lexists(probe / '.agents'):
            self.skipTest("the file system here does not take '.agentſ' for '.agents', so there is no alias to test")
        (self.a / 'f.txt').write_text('1')
        without = self.store.tree_of_dir(self.a)
        alias = self.a / '.agentſ'
        (alias / 'sub' / '.git').mkdir(parents=True); (alias / 'x').write_text('x')    # .git: never even looked at
        self.assertEqual(self.store.tree_of_dir(self.a), without)
        self.assertEqual(self.store.commit(self.a, 'init', 'one'), 'V0001')
        self.assertEqual(self.store.tree_of('V0001'), without)
        self.assertEqual(self.store.tree_of(self.store.snapshot(self.a, 'M0001', 's')), without)
        shutil.rmtree(alias); make_link(self.tmp, alias)
        self.assertEqual(self.store.tree_of_dir(self.a), without)
        self.store.checkout('V0001', self.b)
        self.assertEqual(os.listdir(self.b), ['f.txt'])

    def test_snapshot_does_not_move_main(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b)
        (self.b / 'f.txt').write_text('changed'); commit = self.store.snapshot(self.b, 'M0001', 'trial 1')
        self.assertEqual(self.store.head(), 'V0001')
        self.assertEqual(self.store.tree_of(commit), self.store.tree_of_dir(self.b))
        self.assertIn('changed', self.store.diff('V0001', commit))

    def test_commit_tree_puts_an_existing_commits_content_on_main(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b); (self.b / 'g.txt').write_text('g')
        snapshot = self.store.snapshot(self.b, 'M0001', 'candidate')
        self.assertEqual(self.store.commit_tree(snapshot, 'meta M0001', 'release'), 'V0002')
        self.assertEqual(self.store.tree_of('V0002'), self.store.tree_of(snapshot))
        self.assertEqual((self.store.log()[0]['author'], self.store.log()[0]['message']), ('meta M0001', 'release'))
        self.assertIsNone(self.store.commit_tree(snapshot, 'meta M0001', 'again'))       # equals head: no version
        self.assertEqual(self.store.commit_tree('V0001', 'owner', 'back'), 'V0003')       # a rollback
        self.assertEqual(self.store.tree_of('V0003'), self.store.tree_of('V0001'))
        for name in ('../V0001', 'V0009', 'HEAD', self.store.tree_of('V0001'), None):
            with self.assertRaises(ValueError):
                self.store.commit_tree(name, 'owner', 'x')
        for author in ('', 'a\nb', 'a <b@c>'):
            with self.assertRaises(ValueError):
                self.store.commit_tree('V0002', author, 'x')
        self.assertEqual(self.store.head(), 'V0003')

    def test_symlink_is_kept_as_a_link(self):
        make_link('target', self.a / 'link'); self.store.commit(self.a, 'init', 'one')
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
        self.assertEqual(sorted(p.relative_to(self.b).as_posix() for p in self.b.rglob('*')),
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
        make_link(self.a, self.tmp / 'link'); (self.tmp / 'file').write_text('x')
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
        self.assertEqual(sorted(p.relative_to(self.b).as_posix() for p in self.b.rglob('*')), ['pkg', 'pkg/m.py', 'pkg/n.py'])
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
        shutil.rmtree(self.b / 'd'); make_link(outside, self.b / 'd')
        self.store.checkout('V0001', self.b)
        self.assertFalse((self.b / 'd').is_symlink()); self.assertEqual((self.b / 'd' / 'f').read_text(), '1')
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['f', 'x'])
        self.assertEqual((outside / 'f').read_text(), 'outside')

    def test_git_is_never_looked_up_in_the_path(self):
        planted = self.tmp / 'bin'; planted.mkdir()
        (planted / 'git').write_text(f'#!/bin/sh\ntouch {self.tmp / "planted-git-ran"}\nexit 1\n')
        (planted / 'git').chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': f'{planted}{os.pathsep}{os.environ["PATH"]}'}):
            (self.a / 'f.txt').write_text('1')
            self.assertEqual(self.store.commit(self.a, 'init', 'one'), 'V0001')
            self.assertEqual(self.store.diff('V0001', 'V0001'), '')
        self.assertFalse((self.tmp / 'planted-git-ran').exists())

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
        # A message in Chinese, as the versions of a root made before the program spoke English have: read back as is.
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
        self.assertTrue(cut.endswith('\n... (the diff is longer than 100 bytes; the rest was cut off)\n'), cut)

    def test_no_index_is_left_behind(self):
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        self.store.checkout('V0001', self.b); self.store.tree_of_dir(self.b); self.store.snapshot(self.b, 'M0001', 's')
        self.assertEqual(os.listdir(self.store.git_dir / 'indexes'), [])

    def test_check_writable_locks_nothing_and_changes_nothing(self):
        with self.assertRaisesRegex(RuntimeError, 'has no version yet'): self.store.check_writable()
        (self.a / 'f.txt').write_text('1'); self.store.commit(self.a, 'init', 'one')
        refs = self.store._out('for-each-ref')
        self.store.check_writable(); self.store.check_writable()
        self.assertEqual(self.store._out('for-each-ref'), refs)
        lock = self.store.git_dir / 'refs' / 'heads' / 'main.lock'; lock.touch()     # a git that crashed
        with self.assertRaises(RuntimeError): self.store.check_writable()
        lock.unlink()
        if os.name == 'nt':
            # A Windows directory's read-only attribute does not change its ACL.
            with mock.patch('rsi.versions.tempfile.TemporaryFile', side_effect=PermissionError):
                with self.assertRaisesRegex(RuntimeError, 'objects'): self.store.check_writable()
            self.store.check_writable()
            return
        (self.store.git_dir / 'objects').chmod(0o555)
        self.addCleanup((self.store.git_dir / 'objects').chmod, 0o755)
        with self.assertRaisesRegex(RuntimeError, 'objects'): self.store.check_writable()
        (self.store.git_dir / 'objects').chmod(0o755); self.store.check_writable()


if __name__ == '__main__':
    unittest.main()
