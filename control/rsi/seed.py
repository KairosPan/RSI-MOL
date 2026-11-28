"""Prepare the independently installable MCP packages for the current platform."""
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def windows_seed(work: Path, staging: Path):
    """Ship the controller's standalone filesystem adapter with each Windows service.

    A service can only import from its own workspace. Its private copy is versioned
    along with the service implementation, including across trials and rollback.
    """
    if os.name != 'nt':
        yield work
        return
    with tempfile.TemporaryDirectory(prefix='native-seed-', dir=staging) as temporary:
        prepared = Path(temporary) / 'work'
        shutil.copytree(work, prepared, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
        for package in ('literature', 'memory'):
            folder = prepared / 'tools' / package
            if (folder / 'server.py').is_file():
                shutil.copyfile(Path(__file__).with_name('fs.py'), folder / 'rsi_fs.py')
                if package == 'literature':
                    shutil.copyfile(Path(__file__).with_name('processes.py'), folder / '_processes.py')
        yield prepared
