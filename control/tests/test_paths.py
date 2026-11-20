import importlib.util
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
from tests.support import Root, plant_programs

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


def launch(*args: str, env: dict, launcher: Path = ROOT / 'rsi') -> subprocess.CompletedProcess:
    """The launcher, started the way the owner's shell starts it (by its path, through its #! line)."""
    return subprocess.run([str(launcher), *args], capture_output=True, text=True, timeout=120, env=env,
                          stdin=subprocess.DEVNULL)


class Launcher(unittest.TestCase):
    def folder(self) -> Path:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(ROOT / 'rsi', tmp / 'rsi')
        (tmp / 'control' / 'rsi').mkdir(parents=True)
        return tmp

    def test_launcher_puts_control_first_isolated_and_without_bytecode(self):
        tmp = self.folder()
        package = tmp / 'control' / 'rsi'
        shutil.copy(ROOT / 'control' / 'rsi' / '__main__.py', package)
        (package / '__init__.py').write_text('')
        (package / 'cli.py').write_text(
            'import json, sys\n\n\ndef main():\n    print(json.dumps({"path": sys.path, '
            '"isolated": sys.flags.isolated, '
            '"no_bytecode": sys.dont_write_bytecode, "prefix": sys.pycache_prefix}))\n    return 0\n')
        done = launch(env={**os.environ, 'PYTHONPATH': str(tmp)}, launcher=tmp / 'rsi')
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertEqual(seen['path'][0], str(tmp / 'control'))
        self.assertNotIn(tmp, [Path(entry or '.').resolve() for entry in seen['path']])
        self.assertEqual((seen['isolated'], seen['no_bytecode']), (1, True))
        self.assertFalse(os.path.lexists(seen['prefix'])); self.assertFalse((package / '__pycache__').exists())

    def test_bytecode_is_never_read(self):
        # -B only stops writing bytecode: Python would still load a __pycache__ file whose stamp matches the source.
        tmp = self.folder()
        for path in (ROOT / 'control' / 'rsi').glob('*.py'):
            shutil.copy2(path, tmp / 'control' / 'rsi' / path.name)
        source = tmp / 'control' / 'rsi' / 'cli.py'
        cache = Path(importlib.util.cache_from_source(str(source))); cache.parent.mkdir()
        forged = compile('def main():\n    print("planted bytecode")\n    return 0\n', str(source), 'exec')
        info = source.stat()
        cache.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(forged, info.st_mtime, info.st_size))
        plain = subprocess.run([sys.executable, '-I', '-B', '-c', f'import sys; sys.path.insert(0, '
                                f'{str(tmp / "control")!r}); from rsi.cli import main; main()'],
                               capture_output=True, text=True, timeout=60)
        self.assertIn('planted bytecode', plain.stdout)                 # the forged file is loadable bytecode
        done = launch('--help', env={**os.environ, 'RSI_PYTHON': sys.executable}, launcher=tmp / 'rsi')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('planted bytecode', done.stdout); self.assertIn('用法：rsi', done.stdout)
        self.assertEqual(os.listdir(cache.parent), [cache.name])

    def test_nothing_the_launcher_or_the_program_starts_comes_from_the_path(self):
        with Root() as root:
            marker = root.path / 'planted-ran'
            planted = root.layout.env / 'bin'
            plant_programs(planted, marker)
            site = root.path / 'site'; site.mkdir()
            (site / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "a").write("sitecustomize\\n")\n')
            env = {**os.environ, 'PATH': f'{planted}{os.pathsep}{os.environ["PATH"]}', 'PYTHONPATH': str(site)}
            env.pop('RSI_PYTHON', None)
            for args in (['--help'], ['status']):
                done = launch(*args, env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn('当前版本：V0001', done.stdout)
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else '')

    def test_the_python_must_be_absolute_outside_the_root_and_new_enough(self):
        with Root() as root:
            marker = root.path / 'planted-ran'
            plant_programs(root.layout.env / 'bin', marker)
            link = root.path / 'python-link'; link.symlink_to(sys.executable)
            refused = ['python3', str(root.layout.env / 'bin' / 'python3'), str(link), str(root.path / 'missing')]
            old, test = '/usr/bin/python3', 'import sys; sys.exit(sys.version_info < (3, 11))'
            if os.path.isfile(old) and subprocess.run([old, '-I', '-c', test], capture_output=True,
                                                       timeout=60).returncode != 0:
                refused.append(old)                                     # older than 3.11
            for python in refused:
                done = launch('--help', env={**os.environ, 'RSI_PYTHON': python})
                self.assertEqual((done.returncode, done.stdout), (2, ''), python)
                self.assertEqual(len(done.stderr.splitlines()), 1, done.stderr)
                self.assertIn('RSI_PYTHON', done.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(launch('--help', env={**os.environ, 'RSI_PYTHON': sys.executable}).returncode, 0)


if __name__ == '__main__':
    unittest.main()
