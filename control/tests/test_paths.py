import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi.paths import Layout, find_layout, safe_id

ROOT = Path(__file__).resolve().parents[2]


class Paths(unittest.TestCase):
    def test_layout(self):
        layout = Layout(Path('/x'))
        self.assertEqual(layout.candidate, Path('/x/candidate'))         # sealable by a rename in the root
        self.assertEqual(layout.notes, Path('/x/meta/notes'))
        self.assertEqual(layout.versions, Path('/x/versions/lab.git'))
        self.assertEqual(layout.lab_home, Path('/x/codex-home'))
        self.assertEqual(layout.meta_home, Path('/x/codex-home-meta'))
        self.assertEqual((layout.staging, layout.sealed), (Path('/x/staging'), Path('/x/sealed')))

    def test_safe_id(self):
        self.assertEqual(safe_id('grid-1', 'task'), 'grid-1')
        self.assertEqual(safe_id('x' * 64, 'task'), 'x' * 64)
        for bad in ('', '../x', 'a/b', '.hidden', 'a b', 'x' * 65, 'a\n', '-a', '_a', None):
            with self.assertRaises(ValueError):
                safe_id(bad, 'task')

    def test_find_layout(self):
        with mock.patch.dict(os.environ, {'RSI_ROOT': '/somewhere/else'}):
            self.assertEqual(find_layout().root, Path('/somewhere/else'))
        with mock.patch.dict(os.environ):
            os.environ.pop('RSI_ROOT', None)
            self.assertTrue((find_layout().root / 'rsi').is_file())

    def test_find_layout_resolves_rsi_root(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / 'real').mkdir()
        (tmp / 'link').symlink_to(tmp / 'real')
        with mock.patch.dict(os.environ, {'RSI_ROOT': str(tmp / 'link' / '..' / 'link')}):
            self.assertEqual(find_layout().root, tmp / 'real')


class Launcher(unittest.TestCase):
    def test_launcher_puts_control_first_and_drops_its_own_folder(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(ROOT / 'rsi', tmp / 'rsi')
        package = tmp / 'control' / 'rsi'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'cli.py').write_text('import json, sys\n\n\ndef main():\n    print(json.dumps(sys.path))\n'
                                        '    return 0\n')
        done = subprocess.run([sys.executable, str(tmp / 'rsi')], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        path = json.loads(done.stdout)
        self.assertEqual(path[0], str(tmp / 'control'))
        self.assertNotIn(tmp, [Path(entry or '.').resolve() for entry in path])


if __name__ == '__main__':
    unittest.main()
