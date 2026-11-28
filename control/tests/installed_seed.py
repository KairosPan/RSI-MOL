"""Read-only seed used when exercising the installed MCP services."""
import atexit
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from rsi.seed import windows_seed


WORK = Path(__file__).resolve().parents[1] / 'seed' / 'work'
if os.name == 'nt':
    _resources = ExitStack()
    atexit.register(_resources.close)
    _scratch = Path(_resources.enter_context(tempfile.TemporaryDirectory()))
    WORK = _resources.enter_context(windows_seed(WORK, _scratch))
