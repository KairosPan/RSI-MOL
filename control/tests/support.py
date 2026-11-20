"""Helpers shared by the tests: a skip for machines without Codex, and a confined config."""
import os
import shutil
import unittest
from pathlib import Path

needs_codex = unittest.skipUnless(shutil.which(os.environ.get('RSI_CODEX', 'codex')), 'Codex is not on the path')


def scratch_config(root: Path, workspace: Path) -> dict:
    """Never ask for approval; the `lab` profile reads the minimum, denies `root` and writes `workspace`, no network."""
    filesystem = {':minimal': 'read', str(root): 'deny', str(workspace): 'write'}
    return {'approval_policy': 'never', 'default_permissions': 'lab',
            'permissions': {'lab': {'filesystem': filesystem, 'network': {'enabled': False}}}}
