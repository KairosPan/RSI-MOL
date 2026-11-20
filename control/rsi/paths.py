"""Folders of the RSI root, and the check every id must pass before it becomes part of a path."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


class Refused(Exception):
    """A request the program declines; str() is the Chinese message shown to the owner."""


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def control(self) -> Path:
        return self.root / 'control'

    @property
    def owner(self) -> Path:
        return self.root / 'owner'

    @property
    def tasks(self) -> Path:
        return self.root / 'tasks'

    @property
    def work(self) -> Path:
        return self.root / 'work'

    @property
    def trial(self) -> Path:
        return self.root / 'trial'

    @property
    def meta(self) -> Path:
        return self.root / 'meta'

    @property
    def candidate(self) -> Path:
        """Meta's candidate copy of the Lab workspace. It sits in the root, beside work/ and trial/, so the control
        program can seal it by a rename in the root (a folder inside meta/ could not be sealed that way)."""
        return self.root / 'candidate'

    @property
    def notes(self) -> Path:
        return self.meta / 'notes'

    @property
    def records(self) -> Path:
        return self.root / 'records'

    @property
    def library(self) -> Path:
        return self.root / 'library'

    @property
    def env(self) -> Path:
        return self.root / 'env'

    @property
    def staging(self) -> Path:
        """Where the next work/ or trial/ is built before it is renamed into place; no agent profile grants it."""
        return self.root / 'staging'

    @property
    def sealed(self) -> Path:
        """Where sealed workspaces that could not be versioned whole are kept; no agent profile grants it."""
        return self.root / 'sealed'

    @property
    def versions(self) -> Path:
        return self.root / 'versions' / 'lab.git'

    @property
    def lab_home(self) -> Path:
        return self.root / 'codex-home'

    @property
    def meta_home(self) -> Path:
        return self.root / 'codex-home-meta'

    @property
    def lock(self) -> Path:
        return self.root / '.rsi.lock'


def find_layout() -> Layout:
    """$RSI_ROOT if set, else the folder that holds the `rsi` launcher (two levels above this package); resolved."""
    root = os.environ.get('RSI_ROOT')
    if root:
        return Layout(Path(root).resolve())
    return Layout(Path(__file__).resolve().parents[2])


def safe_id(text: str, what: str) -> str:
    """Return text if it is one plain path component (never `..`, hidden or nested); else raise ValueError."""
    if not isinstance(text, str) or not SAFE_ID.fullmatch(text):
        raise ValueError(f'{what} 不合规：{text!r}。只能用字母、数字、_ 和 -，以字母或数字开头，最长 64 个字符。')
    return text
