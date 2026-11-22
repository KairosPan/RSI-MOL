import os
import unittest
from pathlib import Path
from unittest import mock

from rsi import tasks
from rsi.tasks import SECTIONS, collect_out, load_task, place_inputs, prompt_for, run_check
from tests.support import PASS_CHECK, Root


def sealed(root: Root) -> Path:
    """A folder standing in for a sealed workspace (its out/ is collected)."""
    tree = root.layout.root / '.sealed-work-R0001'; tree.mkdir()
    return tree


class TaskFiles(unittest.TestCase):
    def test_task_ids_use_the_directory_spelling_on_every_filesystem(self):
        with Root() as root:
            root.add_task('Readout')
            self.assertEqual(load_task(root.layout.tasks, 'Readout').id, 'Readout')
            with self.assertRaisesRegex(ValueError, 'Use Readout'):
                load_task(root.layout.tasks, 'readout')

    def test_sections(self):
        with Root() as root:
            root.add_task('T1')
            self.assertEqual(sorted(load_task(root.layout.tasks, 'T1').sections), sorted(SECTIONS))
            root.add_task('T2', sections={'Goal': 'g', 'Input': 'i', 'Requirements': '', 'Completion criteria': 'c'})
            with self.assertRaisesRegex(ValueError, 'no "Requirements" section'): load_task(root.layout.tasks, 'T2')
            for bad in ('../T1', 'T1/x', ''):
                with self.assertRaises(ValueError): load_task(root.layout.tasks, bad)

    def test_check_script_limits(self):
        with Root() as root:
            root.add_task('T1', check='import time\nprint("x" * 100000, flush=True)\ntime.sleep(600)\n')
            result = run_check(load_task(root.layout.tasks, 'T1'), root.layout.root, timeout=2)
            self.assertEqual((result['passed'], len(result['message']) <= 500), (False, True))
            self.assertEqual((result['message'], result['exit_code']), ('the check script timed out', None))

    def test_collect_out_never_follows_links(self):
        with Root() as root:
            tree = sealed(root)
            out = tree / 'out' / 'T1'; out.mkdir(parents=True); secret = root.layout.owner / 'limits.toml'
            os.symlink(secret, out / 'link'); (out / 'a.txt').write_text('a'); dest = root.layout.root / 'dest'
            problems = collect_out(tree, 'T1', dest)                       # ruling: links are dropped, not kept
            self.assertEqual(len(problems), 1); self.assertIn('link', problems[0])
            self.assertFalse(os.path.lexists(dest / 'link')); self.assertEqual((dest / 'a.txt').read_text(), 'a')
            self.assertTrue(secret.is_file()); self.assertTrue(os.path.islink(out / 'link'))   # nothing removed
            os.symlink(root.layout.owner, tree / 'out' / 'T2')
            self.assertTrue(collect_out(tree, 'T2', root.layout.root / 'dest2'))
            self.assertFalse((root.layout.root / 'dest2' / 'limits.toml').exists())


class MoreTaskFiles(unittest.TestCase):
    """Beyond the brief: headings, prompts, check output and the folders the control program owns."""

    def test_unknown_or_repeated_headings_are_refused(self):
        with Root() as root:
            folder = root.add_task('T1')
            text = (folder / 'task.md').read_text()
            for extra in ('## Notes\n\nx\n', '## Goal\n\nonce more\n'):
                (folder / 'task.md').write_text(text + extra)
                with self.assertRaises(ValueError): load_task(root.layout.tasks, 'T1')
            (folder / 'task.md').write_text(text + '```\n## Notes\n```\n')     # a heading inside a code block is text
            self.assertIn('## Notes', load_task(root.layout.tasks, 'T1').sections['Completion criteria'])
            with self.assertRaisesRegex(ValueError, 'T9'): load_task(root.layout.tasks, 'T9')

    def test_the_old_chinese_headings_are_refused_naming_the_english_ones(self):
        """Input data in Chinese: the four headings task files had before the program spoke English."""
        with Root() as root:
            root.add_task('T1', sections=dict(zip(('目标', '输入', '要求', '完成判据'), 'abcd')))
            with self.assertRaises(ValueError) as caught:
                load_task(root.layout.tasks, 'T1')
            message = str(caught.exception)
            self.assertIn('unknown section', message)
            self.assertIn('## Goal, ## Input, ## Requirements, ## Completion criteria', message)

    def test_prompt_names_inputs_and_results(self):
        with Root() as root:
            root.add_task('T1'); root.add_task('T2', inputs={'data.txt': 'd'})
            plain, with_inputs = (prompt_for(load_task(root.layout.tasks, name)) for name in ('T1', 'T2'))
            self.assertLess(plain.index('## Goal'), plain.index('## Completion criteria'))
            self.assertTrue(plain.endswith('\n\nWrite the results to out/T1/.\n'), plain)
            self.assertNotIn('in/T1/', plain)
            self.assertTrue(with_inputs.endswith('\n\nInput files are in in/T2/.\n\nWrite the results to out/T2/.\n'))

    def test_check_gets_the_folder_and_a_closed_environment(self):
        with Root() as root:
            os.environ['PYTHONPATH'] = '/nowhere'
            root.add_task('T1', check='import os, sys\nprint(os.getcwd(), sys.argv[1], os.environ.get("PYTHONPATH"),'
                                      ' os.environ["PATH"], sys.flags.no_user_site)\nprint("second line")\n'
                                      'sys.exit(3)\n')
            task = load_task(root.layout.tasks, 'T1')
            result = run_check(task, root.layout.root / 'some out')
            self.assertEqual(result, {'passed': False, 'exit_code': 3, 'message': f'{task.folder} '
                                      f'{root.layout.root / "some out"} None /usr/bin:/bin:/usr/sbin:/sbin 1'})

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
            self.assertEqual(run_check(task, out)['message'], 'answer.json is missing')
            (out / 'answer.json').write_text('{"x": 3, "y": -1}')
            self.assertEqual(run_check(task, out), {'passed': True, 'message': 'passed', 'exit_code': 0})

    def test_inputs_are_copied_into_a_staged_folder_with_links_kept(self):
        with Root() as root:
            folder = root.add_task('T1', inputs={'data.txt': 'd'}); root.add_task('T2')
            os.symlink(root.layout.owner / 'limits.toml', folder / 'input' / 'link')
            staged = root.layout.staging / 'x'; staged.mkdir(parents=True)
            place_inputs(load_task(root.layout.tasks, 'T1'), staged)
            self.assertEqual((staged / 'in' / 'T1' / 'data.txt').read_text(), 'd')
            self.assertTrue(os.path.islink(staged / 'in' / 'T1' / 'link'))
            place_inputs(load_task(root.layout.tasks, 'T2'), staged)              # no inputs: nothing to copy
            self.assertEqual(os.listdir(staged / 'in'), ['T1'])
            with self.assertRaises(FileExistsError): place_inputs(load_task(root.layout.tasks, 'T1'), staged)

    def test_collected_results_hold_only_regular_files_and_folders(self):
        with Root() as root:
            tree = sealed(root); out = tree / 'out' / 'T1'; (out / 'sub' / 'deeper').mkdir(parents=True)
            (out / 'sub' / 'deeper' / 'kept.txt').write_text('k')
            os.symlink(root.layout.owner, out / 'sub' / 'to-owner')
            os.mkfifo(out / 'sub' / 'pipe')
            (tree / 'notes').mkdir(); (tree / 'notes' / 'h.txt').write_text('shared')
            os.link(tree / 'notes' / 'h.txt', out / 'h.txt')
            dest = root.layout.root / 'dest'
            problems = collect_out(tree, 'T1', dest)
            self.assertEqual(len(problems), 2)
            self.assertTrue(any('out/T1/sub/to-owner is a symbolic link' in line for line in problems), problems)
            self.assertTrue(any('out/T1/sub/pipe is a named pipe' in line for line in problems), problems)
            found = sorted(os.path.relpath(os.path.join(top, name), dest)
                           for top, folders, files in os.walk(dest) for name in folders + files)
            self.assertEqual(found, ['h.txt', 'sub', 'sub/deeper', 'sub/deeper/kept.txt'])
            self.assertEqual(os.stat(dest / 'h.txt').st_nlink, 1)           # copied by content, not shared
            (tree / 'notes' / 'h.txt').write_text('changed later')
            self.assertEqual((dest / 'h.txt').read_text(), 'shared'); self.assertTrue(root.layout.owner.is_dir())

    def test_collected_results_are_made_readable(self):
        with Root() as root:
            tree = sealed(root); locked = tree / 'out' / 'T1' / 'locked'; locked.mkdir(parents=True)
            (locked / 'a.txt').write_text('a'); (locked / 'a.txt').chmod(0); locked.chmod(0)
            (tree / 'out').chmod(0); tree.chmod(0)                           # the sealed folder itself too
            dest = root.layout.root / 'dest'
            self.assertEqual(collect_out(tree, 'T1', dest), [])
            self.assertEqual((dest / 'locked' / 'a.txt').read_text(), 'a')

    def test_results_over_the_budget_are_left_out(self):
        with Root() as root:
            tree = sealed(root); out = tree / 'out' / 'T1'; out.mkdir(parents=True)
            for number in range(6):
                (out / f'f{number}.txt').write_text('12345')
            (out / 'big.bin').write_text('x' * 40)
            with mock.patch.object(tasks, 'MAX_RESULT_BYTES', 30), mock.patch.object(tasks, 'MAX_RESULT_ENTRIES', 4):
                problems = collect_out(tree, 'T1', root.layout.root / 'dest')
            kept = sorted(os.listdir(root.layout.root / 'dest'))
            self.assertEqual(kept, ['f0.txt', 'f1.txt', 'f2.txt', 'f3.txt'])        # 'big.bin' sorts first: 40 > 30
            self.assertTrue(any('big.bin has 40 bytes' in line for line in problems), problems)
            self.assertEqual(sum('files and folders' in line for line in problems), 2)
            folders = tree / 'out' / 'T3'                                         # folders count as entries too
            for name in ('a', 'b', 'c'):
                (folders / name).mkdir(parents=True); (folders / name / 'f').write_text('f')
            with mock.patch.object(tasks, 'MAX_RESULT_ENTRIES', 4):
                problems = collect_out(tree, 'T3', root.layout.root / 'dest3')
            found = sorted(os.path.relpath(os.path.join(top, name), root.layout.root / 'dest3')
                           for top, dirs, files in os.walk(root.layout.root / 'dest3') for name in dirs + files)
            self.assertEqual((found, len(problems)), (['a', 'a/f', 'b', 'b/f'], 1))      # c is left out whole
            sparse = tree / 'out' / 'T2'; sparse.mkdir(parents=True)
            with open(sparse / 'huge', 'wb') as stream:
                stream.truncate((1 << 40))                                         # 1 TiB, nothing allocated
            problems = collect_out(tree, 'T2', root.layout.root / 'dest2')
            self.assertEqual((os.listdir(root.layout.root / 'dest2'), len(problems)), ([], 1))

    def test_out_that_is_a_link_or_a_file_is_not_collected(self):
        with Root() as root:
            tree = sealed(root)
            os.symlink(root.layout.owner, tree / 'out')
            problems = collect_out(tree, 'T1', root.layout.root / 'd1')
            self.assertEqual(len(problems), 1); self.assertTrue((tree / 'out').is_symlink())
            self.assertTrue(root.layout.owner.is_dir()); self.assertEqual(os.listdir(root.layout.root / 'd1'), [])
            (tree / 'out').unlink(); (tree / 'out').mkdir(); (tree / 'out' / 'T1').write_text('not a folder')
            self.assertEqual(len(collect_out(tree, 'T1', root.layout.root / 'd2')), 1)
            self.assertEqual(collect_out(tree, 'T9', root.layout.root / 'd3'), [])      # nothing there: no problem
            self.assertTrue((root.layout.root / 'd3').is_dir())
            with self.assertRaises(ValueError): collect_out(tree, '../owner', root.layout.root / 'd4')
            os.symlink(root.layout.owner, root.layout.root / 'linked')                 # a sealed link: an error
            with self.assertRaises(NotADirectoryError): collect_out(root.layout.root / 'linked', 'T1',
                                                                    root.layout.root / 'd5')


if __name__ == '__main__':
    unittest.main()
