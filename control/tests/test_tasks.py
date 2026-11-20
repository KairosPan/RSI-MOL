import os
import unittest

from rsi.tasks import (SECTIONS, clear, collect_out, load_task, place_inputs, prompt_for, real_folder, run_check,
                       set_aside_git)
from tests.support import PASS_CHECK, Root


class TaskFiles(unittest.TestCase):
    def test_sections(self):
        with Root() as root:
            root.add_task('T1')
            self.assertEqual(sorted(load_task(root.layout.tasks, 'T1').sections), sorted(SECTIONS))
            root.add_task('T2', sections={'目标': 'g', '输入': 'i', '要求': '', '完成判据': 'c'})
            with self.assertRaisesRegex(ValueError, '要求'): load_task(root.layout.tasks, 'T2')
            for bad in ('../T1', 'T1/x', ''):
                with self.assertRaises(ValueError): load_task(root.layout.tasks, bad)

    def test_check_script_limits(self):
        with Root() as root:
            root.add_task('T1', check='import time\nprint("x" * 100000, flush=True)\ntime.sleep(600)\n')
            result = run_check(load_task(root.layout.tasks, 'T1'), root.layout.root, timeout=2)
            self.assertEqual((result['passed'], len(result['message']) <= 500), (False, True))

    def test_collect_out_never_follows_links(self):
        with Root() as root:
            out = root.layout.work / 'out' / 'T1'; out.mkdir(parents=True); secret = root.layout.owner / 'limits.toml'
            os.symlink(secret, out / 'link'); (out / 'a.txt').write_text('a'); dest = root.layout.root / 'dest'
            problems = collect_out(root.layout.work, 'T1', dest)          # ruling: links are dropped, not kept
            self.assertEqual(len(problems), 1); self.assertIn('link', problems[0])
            self.assertFalse(os.path.lexists(dest / 'link')); self.assertEqual((dest / 'a.txt').read_text(), 'a')
            self.assertFalse((root.layout.work / 'out' / 'T1').exists()); self.assertTrue(secret.is_file())
            (root.layout.work / 'out').mkdir(exist_ok=True)
            os.symlink(root.layout.owner, root.layout.work / 'out' / 'T2')
            self.assertTrue(collect_out(root.layout.work, 'T2', root.layout.root / 'dest2'))
            self.assertFalse((root.layout.root / 'dest2' / 'limits.toml').exists())


class MoreTaskFiles(unittest.TestCase):
    """Beyond the brief: headings, prompts, check output and the folders the control program owns."""

    def test_unknown_or_repeated_headings_are_refused(self):
        with Root() as root:
            folder = root.add_task('T1')
            text = (folder / 'task.md').read_text()
            for extra in ('## 备注\n\nx\n', '## 目标\n\n再一次\n'):
                (folder / 'task.md').write_text(text + extra)
                with self.assertRaises(ValueError): load_task(root.layout.tasks, 'T1')
            (folder / 'task.md').write_text(text + '```\n## 备注\n```\n')     # a heading inside a code block is text
            self.assertIn('## 备注', load_task(root.layout.tasks, 'T1').sections['完成判据'])
            with self.assertRaisesRegex(ValueError, 'T9'): load_task(root.layout.tasks, 'T9')

    def test_prompt_names_inputs_and_results(self):
        with Root() as root:
            root.add_task('T1'); root.add_task('T2', inputs={'data.txt': 'd'})
            plain, with_inputs = (prompt_for(load_task(root.layout.tasks, name)) for name in ('T1', 'T2'))
            self.assertLess(plain.index('## 目标'), plain.index('## 完成判据')); self.assertIn('out/T1/', plain)
            self.assertNotIn('in/T1/', plain); self.assertIn('in/T2/', with_inputs)

    def test_check_gets_the_folder_and_a_closed_environment(self):
        with Root() as root:
            os.environ['PYTHONPATH'] = '/nowhere'
            root.add_task('T1', check='import os, sys\nprint(os.getcwd(), sys.argv[1], os.environ.get("PYTHONPATH"),'
                                      ' os.environ["PATH"])\nprint("second line")\nsys.exit(3)\n')
            task = load_task(root.layout.tasks, 'T1')
            result = run_check(task, root.layout.root / 'some out')
            self.assertEqual(result, {'passed': False, 'exit_code': 3, 'message': f'{task.folder} '
                                      f'{root.layout.root / "some out"} None /usr/bin:/bin:/usr/sbin:/sbin'})

    def test_check_output_is_cut_and_stderr_is_not_reported(self):
        with Root() as root:
            root.add_task('T1', check='import sys\nsys.stderr.write("secret answer 42\\n")\nprint("长" * 2000)\n')
            result = run_check(load_task(root.layout.tasks, 'T1'), root.layout.root)
            self.assertEqual((result['passed'], result['message']), (True, '长' * 500))
            root.add_task('T2', check='raise SystemExit("expected x = 3")\n')
            result = run_check(load_task(root.layout.tasks, 'T2'), root.layout.root)
            self.assertEqual((result['passed'], result['message'], result['exit_code']), (False, '', 1))
            root.add_task('T3')
            self.assertIsNone(run_check(load_task(root.layout.tasks, 'T3'), root.layout.root))

    def test_pass_check(self):
        with Root() as root:
            root.add_task('T1', check=PASS_CHECK); task = load_task(root.layout.tasks, 'T1')
            out = root.layout.root / 'o'; out.mkdir()
            self.assertEqual(run_check(task, out)['message'], '缺少 answer.json')
            (out / 'answer.json').write_text('{"x": 3, "y": -1}')
            self.assertEqual(run_check(task, out), {'passed': True, 'message': '通过', 'exit_code': 0})

    def test_inputs_are_placed_afresh_with_links_kept(self):
        with Root() as root:
            folder = root.add_task('T1', inputs={'data.txt': 'd'})
            os.symlink(root.layout.owner / 'limits.toml', folder / 'input' / 'link')
            work = root.layout.work
            (work / 'in').write_text('a file where the folder belongs')
            self.assertEqual(len(place_inputs(load_task(root.layout.tasks, 'T1'), work)), 1)
            self.assertEqual((work / 'in' / 'T1' / 'data.txt').read_text(), 'd')
            self.assertTrue(os.path.islink(work / 'in' / 'T1' / 'link'))
            (work / 'in' / 'T1' / 'stale.txt').write_text('left by an earlier run')
            self.assertEqual(place_inputs(load_task(root.layout.tasks, 'T1'), work), [])
            self.assertEqual(sorted(os.listdir(work / 'in' / 'T1')), ['data.txt', 'link'])
            self.assertEqual(clear(work, 'in', 'T1'), []); self.assertFalse(os.path.lexists(work / 'in'))

    def test_collected_results_hold_only_regular_files_and_folders(self):
        with Root() as root:
            work = root.layout.work; out = work / 'out' / 'T1'; (out / 'sub' / 'deeper').mkdir(parents=True)
            (out / 'sub' / 'deeper' / 'kept.txt').write_text('k')
            os.symlink(root.layout.owner, out / 'sub' / 'to-owner')
            os.mkfifo(out / 'sub' / 'pipe')
            (work / 'notes').mkdir(); (work / 'notes' / 'h.txt').write_text('shared')
            os.link(work / 'notes' / 'h.txt', out / 'h.txt')
            dest = root.layout.root / 'dest'
            problems = collect_out(work, 'T1', dest)
            self.assertEqual(len(problems), 2)
            self.assertTrue(any('out/T1/sub/to-owner' in line and '符号链接' in line for line in problems))
            self.assertTrue(any('out/T1/sub/pipe' in line and '命名管道' in line for line in problems))
            found = sorted(os.path.relpath(os.path.join(top, name), dest)
                           for top, folders, files in os.walk(dest) for name in folders + files)
            self.assertEqual(found, ['h.txt', 'sub', 'sub/deeper', 'sub/deeper/kept.txt'])
            self.assertEqual(os.stat(dest / 'h.txt').st_nlink, 1)           # copied by content, not shared
            (work / 'notes' / 'h.txt').write_text('changed later')
            self.assertEqual((dest / 'h.txt').read_text(), 'shared')
            self.assertFalse(os.path.lexists(out)); self.assertTrue(root.layout.owner.is_dir())

    def test_control_folders_are_made_real(self):
        with Root() as root:
            work, outside = root.layout.work, root.layout.root / 'outside'
            outside.mkdir(); (outside / 'x').write_text('x')
            os.symlink(outside, work / '.tmp')
            self.assertEqual(len(real_folder(work, '.tmp')), 1)
            self.assertTrue((work / '.tmp').is_dir()); self.assertFalse((work / '.tmp').is_symlink())
            self.assertEqual(os.listdir(outside), ['x']); self.assertEqual(real_folder(work, '.tmp'), [])
            (work / '.tmp' / 'y').write_text('kept')                            # a real folder is left as it is
            self.assertEqual(real_folder(work, '.tmp'), []); self.assertTrue((work / '.tmp' / 'y').is_file())

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
            problems = set_aside_git(work)
            self.assertEqual(len(problems), 5)
            self.assertTrue(any('sub/.git' in line and 'sub/.git.rsi-kept-2' in line for line in problems))
            self.assertEqual((work / 'sub' / '.git.rsi-kept-2' / 'config').read_text(), 'c')
            self.assertTrue((work / 'a' / 'b' / '.git.rsi-kept').is_file())
            self.assertEqual(sorted(os.listdir(work / 'odd')),
                             ['.git.rsi-kept', '.git.rsi-kept-2', '.gitmodules.rsi-kept'])
            self.assertTrue((outside / '.git').is_dir()); self.assertTrue((work / 'out' / '.git').is_dir())
            self.assertIsNotNone(root.store.commit(work, 'owner (manual)', 'x'))   # versioning goes through now
            self.assertEqual(set_aside_git(work), [])

    def test_collected_results_are_made_readable(self):
        with Root() as root:
            locked = root.layout.work / 'out' / 'T1' / 'locked'; locked.mkdir(parents=True)
            (locked / 'a.txt').write_text('a'); (locked / 'a.txt').chmod(0); locked.chmod(0)
            dest = root.layout.root / 'dest'
            self.assertEqual(collect_out(root.layout.work, 'T1', dest), [])
            self.assertEqual((dest / 'locked' / 'a.txt').read_text(), 'a')

    def test_out_that_is_a_link_or_a_file_is_refused(self):
        with Root() as root:
            work = root.layout.work
            os.symlink(root.layout.owner, work / 'out')
            problems = collect_out(work, 'T1', root.layout.root / 'd1')
            self.assertEqual(len(problems), 1); self.assertFalse(os.path.lexists(work / 'out'))
            self.assertTrue(root.layout.owner.is_dir()); self.assertEqual(os.listdir(root.layout.root / 'd1'), [])
            (work / 'out').mkdir(); (work / 'out' / 'T1').write_text('not a folder')
            self.assertEqual(len(collect_out(work, 'T1', root.layout.root / 'd2')), 1)
            self.assertFalse(os.path.lexists(work / 'out' / 'T1'))
            self.assertEqual(collect_out(work, 'T1', root.layout.root / 'd3'), [])      # nothing there: no problem
            self.assertTrue((root.layout.root / 'd3').is_dir())
            with self.assertRaises(ValueError): collect_out(work, '../owner', root.layout.root / 'd4')


if __name__ == '__main__':
    unittest.main()
