"""Real filesystem operations the Windows controller and its seed servers share."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from rsi import fs


class NativeFilesystemTests(unittest.TestCase):
    def test_stage_rename_read_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as opened:
            root = fs.open(tmp, fs.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
            opened.callback(fs.close, root)
            fs.mkdir('stage', dir_fd=root)
            stage = fs.open('stage', fs.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW, dir_fd=root)
            opened.callback(fs.close, stage)
            flags = fs.O_WRONLY | fs.O_CREAT | fs.O_EXCL | fs.O_NOFOLLOW
            with fs.fdopen(fs.open('data', flags, dir_fd=stage), 'wb') as stream:
                stream.write(b'rsi\r\n\x1a\n')
                stream.flush()
                fs.fsync(stream.fileno())
            before = fs.stat('data', dir_fd=stage, follow_symlinks=False)
            fs.rename('data', 'released', src_dir_fd=stage, dst_dir_fd=root)
            after = fs.stat('released', dir_fd=root, follow_symlinks=False)
            self.assertTrue(os.path.samestat(before, after))
            self.assertEqual((Path(tmp) / 'released').read_bytes(), b'rsi\r\n\x1a\n')
            with fs.scandir(root) as entries:
                self.assertEqual({entry.name for entry in entries}, {'released', 'stage'})
            self.assertEqual(fs.listdir(stage), [])
            fs.rename('released', 'final', src_dir_fd=root, dst_dir_fd=root)
            fs.unlink('final', dir_fd=root)
            fs.fsync(root)
            self.assertEqual(fs.listdir(root), ['stage'])

    def test_two_writers_cannot_hold_same_lock(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as opened:
            path = Path(tmp) / 'lock'
            flags = fs.O_RDWR | fs.O_CREAT | fs.O_NOFOLLOW
            first, second = fs.open(path, flags), fs.open(path, flags)
            opened.callback(fs.close, second)
            try:
                fs.lock_exclusive(first, wait=False)
                with self.assertRaises(BlockingIOError):
                    fs.lock_exclusive(second, wait=False)
            finally:
                fs.close(first)
            fs.lock_exclusive(second, wait=False)
