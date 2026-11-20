import os
import unittest
from pathlib import Path
from unittest import mock

from rsi.paths import Layout, find_layout, safe_id


class Paths(unittest.TestCase):
    def test_layout(self):
        layout = Layout(Path('/x'))
        self.assertEqual(layout.candidate, Path('/x/meta/candidate'))
        self.assertEqual(layout.versions, Path('/x/versions/lab.git'))
        self.assertEqual(layout.lab_home, Path('/x/codex-home'))
        self.assertEqual(layout.meta_home, Path('/x/codex-home-meta'))

    def test_safe_id(self):
        self.assertEqual(safe_id('grid-1', 'task'), 'grid-1')
        for bad in ('', '../x', 'a/b', '.hidden', 'a b', 'x' * 65):
            with self.assertRaises(ValueError):
                safe_id(bad, 'task')

    def test_find_layout(self):
        with mock.patch.dict(os.environ, {'RSI_ROOT': '/somewhere/else'}):
            self.assertEqual(find_layout().root, Path('/somewhere/else'))
        with mock.patch.dict(os.environ):
            os.environ.pop('RSI_ROOT', None)
            self.assertTrue((find_layout().root / 'rsi').is_file())


if __name__ == '__main__':
    unittest.main()
