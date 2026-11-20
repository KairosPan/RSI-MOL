"""The workspace's skills, and where Codex loads them.

Codex 0.170.0 loads a workspace's skills only from <working folder>/.agents/skills/, and no agent can create a
`.agents` entry or write in one (measured by `rsi check`). So the skills live in a plain, versioned folder of the
workspace, skills/<skill>/ with a SKILL.md each, which the Lab (in work/) and Meta (in candidate/) edit. Whenever the
control program stages a workspace a Lab Codex will run in (work/ or trial/, never candidate/), it copies skills/ to
.agents/skills/ in the staged tree before renaming that into place (materialise, called by rsi.labrun's move-in).
`.agents` itself is never versioned (rsi.versions.VersionStore.EXCLUDED).

The staged tree is checked out from a version the agents wrote, so its names are not trusted: on APFS `.AGENTS`, and
`.agentſ` with a long s, are the same entry as `.agents`, and any of them may be a link. So nothing here goes by path
or by comparing listed names. Everything is looked up relative to an opened folder, never following a link: what the
file system returns for `.agents` is removed first; `.agents` and every folder below it are made exclusively (never
over something that is there) and then opened; files are created exclusively; and the walk of skills/ starts from
skills/ opened without following a link.

What is copied is decided by one walk (_walk), which Meta's checks of candidate/ share (scan, used by
rsi.settings.workspace_problems): regular files and folders only, within the caps below. Everything it leaves out is
named in a Chinese line, except regular files directly in skills/ (a README, a .DS_Store): they are no skills and are
ignored. Once nothing more can be taken the walk stops, with one line saying where.
"""
import errno
import os
import stat
from pathlib import Path

from rsi.settings import show
from rsi.workspace import Lines, open_folder, remove

SKILLS = 'skills'                   # the versioned folder: skills/<skill>/SKILL.md
SKILL_FILE = 'SKILL.md'             # a skill is a folder directly in skills/ holding a regular file by this name
AGENTS = '.agents'                  # top-level: built by the control program only
WHERE = '.agents/skills/'           # how lines name where Codex loads the skills from
MAX_SKILLS = 64                     # skills copied
MAX_FILES = 200                     # regular files copied, over all skills
MAX_BYTES = 20 << 20                # bytes of those files, over all skills
MAX_DEPTH = 8                       # folder levels below skills/ (skills/<skill>/ is level 1)
CLEAR_TRIES = 4                     # lookups of .agents in a staged tree, each removing what it found
CHUNK = 1 << 16
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class _Target:
    """A folder of the copy, made on first use: exclusively (os.mkdir relative to its parent's descriptor, which
    fails on any entry the file system finds there) and then opened without following a link. The first one hangs
    off the staged tree's descriptor, base."""

    def __init__(self, name: str, parent: '_Target | None' = None, base: int | None = None) -> None:
        self.name, self.parent, self.base, self.fd = name, parent, base, None

    def open(self) -> int:
        if self.fd is None:
            base = self.parent.open() if self.parent is not None else self.base
            os.mkdir(self.name, 0o755, dir_fd=base)
            self.fd = open_folder(self.name, base)
        return self.fd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _Walk:
    """One walk of skills/: its lines, what it has taken, and whether it has stopped."""

    def __init__(self) -> None:
        self.lines, self.files, self.size, self.skills, self.stopped = Lines(), 0, 0, 0, False

    def take(self, size: int) -> str | None:
        """Count one more regular file of that size, or say why it does not fit (then nothing is counted)."""
        if self.files >= MAX_FILES:
            return f'技能里的文件已到 {MAX_FILES} 个的上限'
        if self.size + size > MAX_BYTES:
            return f'技能里的文件加起来会超过 {MAX_BYTES} 字节的上限'
        self.files += 1
        self.size += size
        return None

    def halt(self, path: str, top: bool = False) -> bool:
        """Whether the walk ends before skills/<path>. Once nothing more can be taken (the files or the bytes are
        full, or, between skills (top), the skills are), one line names this first entry not looked at, and the
        walk stops."""
        if self.stopped:
            return True
        if self.files >= MAX_FILES:
            reason = f'技能里的文件已到 {MAX_FILES} 个的上限'
        elif self.size >= MAX_BYTES:
            reason = f'技能里的文件已到 {MAX_BYTES} 字节的上限'
        elif top and self.skills >= MAX_SKILLS:
            reason = f'技能已到 {MAX_SKILLS} 个的上限'
        else:
            return False
        self.stopped = True
        self.line(path, f'{reason}，从这里起其余的内容都不再看，不会放进 {WHERE}')
        return True

    def line(self, path: str, what: str) -> None:
        """A line about skills/<path>, within the cap of rsi.workspace.Lines."""
        self.lines.add(f'{show(f"{SKILLS}/{path}")}：{what}')


def _kind(info: os.stat_result) -> str:
    """What an entry that is not a folder is, for a line."""
    if stat.S_ISLNK(info.st_mode):
        return '是符号链接'
    return '是文件' if stat.S_ISREG(info.st_mode) else '是特殊文件'


def _listed(name: str, parent: int, path: str, walk: _Walk) -> tuple[int, list[str]] | None:
    """The folder `name` of an open folder, opened without following a link, and its entry names, sorted; None
    (with a line naming skills/<path>) when it cannot be opened or listed."""
    try:
        fd = open_folder(name, parent)
    except OSError as error:
        walk.line(path, f'读不了（{error.strerror or error}），不会放进 {WHERE}')
        return None
    try:
        return fd, sorted(os.listdir(fd))
    except OSError as error:
        os.close(fd)
        walk.line(path, f'读不了（{error.strerror or error}），不会放进 {WHERE}')
        return None


def _copy(source: int, name: str, info: os.stat_result, target: _Target, path: str, walk: _Walk) -> None:
    """Copy the regular file `name` of the open folder source (info: what the walk looked up) to a new file of
    that name in target. A failure is a line, and a partly written copy is removed."""
    created = None
    try:
        fd = os.open(name, _READ, dir_fd=source)
        try:
            found = os.fstat(fd)
            if not stat.S_ISREG(found.st_mode) or (found.st_dev, found.st_ino) != (info.st_dev, info.st_ino):
                raise OSError(errno.ESTALE, '不是刚才看到的那个普通文件')
            folder = target.open()
            out = os.open(name, _CREATE, 0o755 if found.st_mode & 0o111 else 0o644, dir_fd=folder)
            created = folder
            try:
                left = info.st_size
                while left > 0:
                    chunk = os.read(fd, min(CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    while chunk:
                        chunk = chunk[os.write(out, chunk):]
            finally:
                os.close(out)
            created = None
        finally:
            os.close(fd)
    except OSError as error:
        if created is not None:
            try:
                os.unlink(name, dir_fd=created)
            except OSError:
                pass
        walk.line(path, f'没能放进 {WHERE}（{error.strerror or error}）')


def _folder(source: int, relative: str, level: int, names: list[str], target: _Target | None, walk: _Walk) -> None:
    """Take the entries `names` of the open folder source, which is skills/<relative>, `level` levels below
    skills/; copy them into target unless it is None."""
    for name in names:
        path = f'{relative}/{name}'
        if walk.halt(path):
            return
        try:
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
        except OSError as error:
            walk.line(path, f'读不了（{error.strerror or error}），不会放进 {WHERE}')
            continue
        if stat.S_ISDIR(info.st_mode):
            if level + 1 > MAX_DEPTH:
                walk.line(path, f'文件夹嵌套超过 {MAX_DEPTH} 层，不会放进 {WHERE}')
                continue
            opened = _listed(name, source, path, walk)
            if opened is None:
                continue
            inner = None if target is None else _Target(name, target)
            try:
                _folder(opened[0], path, level + 1, opened[1], inner, walk)
            finally:
                os.close(opened[0])
                if inner is not None:
                    inner.close()
        elif stat.S_ISREG(info.st_mode):
            reason = walk.take(info.st_size)
            if reason is not None:
                walk.line(path, f'{reason}，不会放进 {WHERE}')
            elif target is not None:
                _copy(source, name, info, target, path, walk)
        else:
            walk.line(path, f'{_kind(info)}，不会放进 {WHERE}（只放普通文件和文件夹）')


def _skill(top: int, name: str, target: _Target | None, walk: _Walk) -> None:
    """The folder `name` directly in skills/ (open as top): a skill when it holds a regular SKILL.md that fits."""
    opened = _listed(name, top, name, walk)
    if opened is None:
        return
    fd, names = opened
    try:
        try:
            found = os.stat(SKILL_FILE, dir_fd=fd, follow_symlinks=False) if SKILL_FILE in names else None
        except OSError:
            found = None
        if found is None or not stat.S_ISREG(found.st_mode):
            walk.line(name, f'里面没有普通文件 {SKILL_FILE}，不是技能，不会放进 {WHERE}')
            return
        reason = walk.take(found.st_size)
        if reason is not None:
            walk.line(name, f'{reason}，整个技能不会放进 {WHERE}')
            return
        walk.skills += 1
        here = None if target is None else _Target(name, target)
        try:
            if here is not None:
                _copy(fd, SKILL_FILE, found, here, f'{name}/{SKILL_FILE}', walk)
            _folder(fd, name, 1, [other for other in names if other != SKILL_FILE], here, walk)
        finally:
            if here is not None:
                here.close()
    finally:
        os.close(fd)


def _walk(workspace: int, target: _Target | None) -> list[str]:
    """The one walk of skills/ in the open workspace folder, copying into target unless it is None; its lines."""
    try:
        info = os.stat(SKILLS, dir_fd=workspace, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f'{SKILLS}：读不了（{error.strerror or error}），里面的技能都不会生效']
    if not stat.S_ISDIR(info.st_mode):
        return [f'{SKILLS}：{_kind(info)}，不是文件夹，里面的技能都不会生效']
    try:
        top = open_folder(SKILLS, workspace)
    except OSError as error:
        return [f'{SKILLS}：读不了（{error.strerror or error}），里面的技能都不会生效']
    walk = _Walk()
    try:
        try:
            names = sorted(os.listdir(top))
        except OSError as error:
            return [f'{SKILLS}：读不了（{error.strerror or error}），里面的技能都不会生效']
        for name in names:
            try:
                info = os.stat(name, dir_fd=top, follow_symlinks=False)
            except OSError as error:
                walk.line(name, f'读不了（{error.strerror or error}），不会放进 {WHERE}')
                continue
            if stat.S_ISREG(info.st_mode):
                continue                            # a file beside the skills (a README, a .DS_Store): no skill
            if walk.halt(name, top=True):
                break
            if stat.S_ISDIR(info.st_mode):
                _skill(top, name, target, walk)
            else:
                walk.line(name, f'{_kind(info)}，不是技能（技能是带 {SKILL_FILE} 的文件夹），不会放进 {WHERE}')
    finally:
        os.close(top)
    return walk.lines.done()


def scan(workspace: Path) -> list[str]:
    """The Chinese lines of materialise's walk of the workspace's skills/ (what would not reach .agents/skills/),
    copying nothing: for Meta's checks of candidate/. A missing workspace has no skills; one that cannot be opened is
    one line."""
    try:
        fd = open_folder(workspace)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f'{SKILLS}：读不了（{error.strerror or error}），里面的技能都不会生效']
    try:
        return _walk(fd, None)
    finally:
        os.close(fd)


def _clear(workspace: int, path: str) -> None:
    """Remove what the file system returns for `.agents` in the open staged tree, whatever its listed spelling:
    looked up by that name and removed by it, relative to the tree's descriptor and never following a link, until
    the lookup finds nothing. OSError when an entry cannot be removed, or one is still there after CLEAR_TRIES."""
    for _ in range(CLEAR_TRIES):
        try:
            os.stat(AGENTS, dir_fd=workspace, follow_symlinks=False)
        except FileNotFoundError:
            return
        remove(AGENTS, workspace, f'{path}/{AGENTS}')
    raise OSError(errno.EBUSY, f'{AGENTS} 删了 {CLEAR_TRIES} 次还在', path)


def materialise(workspace: Path) -> list[str]:
    """Copy the skills of a staged workspace to its .agents/skills/, after removing whatever the file system returns
    for .agents there; the Chinese lines of what was left out or could not be copied. Only folders that get a copied
    file are made, so nothing to copy leaves no .agents at all. OSError when the tree cannot be opened, or a .agents
    found there cannot be removed (_clear)."""
    fd = open_folder(workspace)
    try:
        _clear(fd, str(workspace))
        agents = _Target(AGENTS, base=fd)
        skills = _Target(SKILLS, agents)
        try:
            return _walk(fd, skills)
        finally:
            skills.close()
            agents.close()
    finally:
        os.close(fd)
