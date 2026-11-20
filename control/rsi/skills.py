"""The workspace's skills, and where Codex loads them.

Codex 0.170.0 loads a workspace's skills only from <working folder>/.agents/skills/, and no agent can create a
`.agents` entry or write in one (measured by `rsi check`). So the skills live in a plain, versioned folder of the
workspace, skills/<skill>/ with a SKILL.md each, which the Lab (in work/) and Meta (in candidate/) edit. Whenever the
control program stages a workspace a Lab Codex will run in (work/ or trial/, never candidate/), it copies skills/ to
.agents/skills/ in the staged tree before renaming that into place (materialise, called by rsi.labrun's move-in). A
staged tree is not agent-writable, so plain path operations are fine there. `.agents` itself is never versioned
(rsi.versions.VersionStore.EXCLUDED).

What is copied is decided by one walk (scan), which Meta's checks of candidate/ share (rsi.settings.workspace_problems):
regular files and folders only, never following a link, within the caps below. Everything it leaves out is named in a
Chinese line.
"""
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from rsi.settings import show
from rsi.workspace import Lines, open_folder, remove

SKILLS = 'skills'                   # the versioned folder: skills/<skill>/SKILL.md
SKILL_FILE = 'SKILL.md'             # a skill is a top-level folder of skills/ holding a regular file by this name
AGENTS = '.agents'                  # top-level, any letter case: built by the control program only
TARGET = Path(AGENTS, 'skills')     # where Codex loads the skills from
WHERE = '.agents/skills/'           # how lines name TARGET
MAX_SKILLS = 64                     # skills copied
MAX_FILES = 200                     # regular files copied, over all skills
MAX_BYTES = 20 << 20                # bytes of those files, over all skills
MAX_DEPTH = 8                       # folder levels below skills/ (skills/<skill>/ is level 1)


@dataclass
class _Totals:
    """What has been taken so far, over all skills."""
    files: int = 0
    size: int = 0

    def take(self, size: int) -> str | None:
        """Count one more regular file of that size, or say why it does not fit (then nothing is counted)."""
        if self.files >= MAX_FILES:
            return f'技能里的文件已到 {MAX_FILES} 个的上限'
        if self.size + size > MAX_BYTES:
            return f'技能里的文件加起来会超过 {MAX_BYTES} 字节的上限'
        self.files += 1
        self.size += size
        return None


def _kind(info: os.stat_result) -> str:
    """What an entry that is not a folder is, for a line."""
    if stat.S_ISLNK(info.st_mode):
        return '是符号链接'
    return '是文件' if stat.S_ISREG(info.st_mode) else '是特殊文件'


def _names(folder: Path, shown: str, lines: Lines) -> list[str] | None:
    """The entry names of a folder, sorted; None (with a line) when it cannot be listed."""
    try:
        return sorted(os.listdir(folder))
    except OSError as error:
        lines.add(f'{show(shown)}：读不了（{error.strerror or error}），里面的内容不会放进 {WHERE}')
        return None


def _walk(top: Path, relative: str, level: int, names: list[str], totals: _Totals, copied: list[str],
          lines: Lines) -> None:
    """Take the entries `names` of the folder skills/<relative>, which is `level` levels below skills/."""
    for name in names:
        path = f'{relative}/{name}'
        shown = show(f'{SKILLS}/{path}')
        try:
            info = os.lstat(top / path)
        except OSError as error:
            lines.add(f'{shown}：读不了（{error.strerror or error}），不会放进 {WHERE}')
            continue
        if stat.S_ISDIR(info.st_mode):
            if level + 1 > MAX_DEPTH:
                lines.add(f'{shown}：文件夹嵌套超过 {MAX_DEPTH} 层，不会放进 {WHERE}')
                continue
            inner = _names(top / path, f'{SKILLS}/{path}', lines)
            if inner is not None:
                _walk(top, path, level + 1, inner, totals, copied, lines)
        elif stat.S_ISREG(info.st_mode):
            reason = totals.take(info.st_size)
            if reason is None:
                copied.append(path)
            else:
                lines.add(f'{shown}：{reason}，不会放进 {WHERE}')
        else:
            lines.add(f'{shown}：{_kind(info)}，不会放进 {WHERE}（只放普通文件和文件夹）')


def scan(workspace: Path) -> tuple[list[str], list[str]]:
    """What materialise copies from the workspace's skills/: paths of regular files relative to skills/, each
    skill's SKILL.md first and then the rest of it in name order; and the Chinese lines naming everything left out
    (rsi.workspace.Lines: at most MAX_LINES of them plus one counting the rest). Copies nothing.

    A skill is a top-level folder of skills/ holding a regular file named exactly SKILL.md; every other top-level
    entry is left out, and so is a skill beyond MAX_SKILLS or one whose SKILL.md no longer fits MAX_FILES or
    MAX_BYTES. Inside a skill, links, special files, folders more than MAX_DEPTH levels below skills/, files beyond
    MAX_FILES or MAX_BYTES (counted over all skills) and entries that cannot be read are left out. No link is
    followed. A missing skills/ is no skills and no line. Never raises OSError for anything at or below skills/."""
    top = Path(workspace) / SKILLS
    try:
        info = os.lstat(top)
    except FileNotFoundError:
        return [], []
    except OSError as error:
        return [], [f'{SKILLS}：读不了（{error.strerror or error}），里面的技能都不会生效']
    if not stat.S_ISDIR(info.st_mode):
        return [], [f'{SKILLS}：{_kind(info)}，不是文件夹，里面的技能都不会生效']
    lines, totals, copied, skills = Lines(), _Totals(), [], 0
    for name in _names(top, SKILLS, lines) or []:
        shown = show(f'{SKILLS}/{name}')
        try:
            info = os.lstat(top / name)
        except OSError as error:
            lines.add(f'{shown}：读不了（{error.strerror or error}），不会放进 {WHERE}')
            continue
        if not stat.S_ISDIR(info.st_mode):
            lines.add(f'{shown}：{_kind(info)}，不是技能（技能是带 {SKILL_FILE} 的文件夹），不会放进 {WHERE}')
            continue
        names = _names(top / name, f'{SKILLS}/{name}', lines)
        if names is None:
            continue
        try:
            entry = os.lstat(top / name / SKILL_FILE) if SKILL_FILE in names else None
        except OSError:
            entry = None
        if entry is None or not stat.S_ISREG(entry.st_mode):
            lines.add(f'{shown}：里面没有普通文件 {SKILL_FILE}，不是技能，不会放进 {WHERE}')
            continue
        if skills >= MAX_SKILLS:
            lines.add(f'{shown}：技能已到 {MAX_SKILLS} 个的上限，这个不会放进 {WHERE}')
            continue
        reason = totals.take(entry.st_size)
        if reason is not None:
            lines.add(f'{shown}：{reason}，整个技能不会放进 {WHERE}')
            continue
        skills += 1
        copied.append(f'{name}/{SKILL_FILE}')
        _walk(top, name, 1, [entry for entry in names if entry != SKILL_FILE], totals, copied, lines)
    return copied, lines.done()


def _clear(workspace: Path) -> None:
    """Remove every top-level entry named .agents in any letter case, never following a link: only the control
    program builds one (a checkout of a version from before .agents was left out of versions could bring one)."""
    fd = open_folder(workspace)
    try:
        for name in os.listdir(fd):
            if name.lower() == AGENTS:
                remove(name, fd, str(Path(workspace, name)))
    finally:
        os.close(fd)


def materialise(workspace: Path) -> list[str]:
    """Copy the skills of a staged workspace (scan) to its .agents/skills/, after removing any .agents found there;
    the Chinese lines of what was left out or could not be copied. Only folders that hold a copied file are made,
    so nothing to copy leaves no .agents at all. OSError when a .agents found there cannot be removed."""
    _clear(workspace)
    copied, lines = scan(workspace)
    source, target = Path(workspace) / SKILLS, Path(workspace) / TARGET
    for relative in copied:
        try:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source / relative, target / relative, follow_symlinks=False)
        except OSError as error:
            lines.append(f'{show(f"{SKILLS}/{relative}")}：没能放进 {WHERE}（{error.strerror or error}）')
    return lines
