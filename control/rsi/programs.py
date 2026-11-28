"""The programs the control program starts, never taken from where an agent (or anyone) can write.

Agents write folders of the RSI root (env/bin among them, which the owner may have put first on PATH by activating
the venv), and anyone may write the shared temp folders. So Codex is the validated native program (native_codex,
looked up past such PATH entries), git comes from a fixed list of absolute paths (git_program), and the PATH that
child processes get has no entry that is relative or lies inside the root or a shared temp folder (clean_path). The
launcher `rsi` picks the control program's own Python from a fixed list in the same way.
"""
import functools
import json
import os
import platform
import shutil
from pathlib import Path

from rsi.paths import Refused

# Shared temp folders: anyone may write them.
TEMP_FOLDERS = ('/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp')
if os.name == 'nt':
    # Include the usual per-user temp directory even if a launcher sets TEMP to a run-specific folder.
    TEMP_FOLDERS = tuple(dict.fromkeys(os.path.abspath(p) for p in (
        os.environ.get('TEMP'), os.environ.get('TMP'), str(Path.home() / 'AppData/Local/Temp'),
        str(Path(os.environ['SystemRoot']) / 'Temp')) if p and Path(p).is_absolute()))
# (system, machine) → (npm platform package, target triple), as in the npm package's bin/codex.js.
NATIVE_TARGETS = {('Darwin', 'arm64'): ('codex-darwin-arm64', 'aarch64-apple-darwin'),
                  ('Darwin', 'x86_64'): ('codex-darwin-x64', 'x86_64-apple-darwin'),
                  ('Linux', 'x86_64'): ('codex-linux-x64', 'x86_64-unknown-linux-musl'),
                  ('Linux', 'aarch64'): ('codex-linux-arm64', 'aarch64-unknown-linux-musl'),
                  ('Linux', 'arm64'): ('codex-linux-arm64', 'aarch64-unknown-linux-musl'),
                  ('Windows', 'AMD64'): ('codex-win32-x64', 'x86_64-pc-windows-msvc'),
                  ('Windows', 'ARM64'): ('codex-win32-arm64', 'aarch64-pc-windows-msvc')}
HOW_TO_SET_CODEX = ('Set $RSI_CODEX to the absolute path of the native Codex program (an npm install has it at '
                    '.../@openai/codex/node_modules/@openai/codex-<platform>/vendor/<target>/bin/codex); it must not '
                    'be inside the RSI folder or in a shared temporary folder such as /tmp.')
GIT_PROGRAMS = ('/usr/bin/git', '/opt/homebrew/bin/git', '/usr/local/bin/git')     # tried in this order
if os.name == 'nt':
    GIT_PROGRAMS = tuple(str(Path(p) / 'Git/cmd/git.exe') for p in
                         (os.environ.get('ProgramW6432'), os.environ.get('ProgramFiles'),
                          os.environ.get('ProgramFiles(x86)'), str(Path.home() / 'AppData/Local/Programs'))
                         if p and Path(p).is_absolute())


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def inside(path: str, folder: str) -> bool:
    """Whether path is folder or lies under it, never judged by spelling alone: by file identity (os.path.samestat
    of the path and each of its ancestors against the folder) where they exist, and also by case-insensitive
    spelling of their absolute and real forms, so a path in another letter case is caught; it refuses more, not
    less."""
    forms = {os.path.abspath(path), os.path.realpath(path)}
    limits = {os.path.abspath(folder), os.path.realpath(folder)}
    if any(_within(form.casefold(), limit.casefold()) for form in forms for limit in limits):
        return True
    try:
        target = os.stat(folder)
    except OSError:
        return False
    for form in forms:
        for ancestor in (form, *map(str, Path(form).parents)):
            try:
                if os.path.samestat(os.stat(ancestor), target):
                    return True
            except OSError:
                continue
    return False


def temp_folders() -> list[str]:
    """The real paths of the shared temp folders that exist, each once."""
    out = []
    for folder in TEMP_FOLDERS:
        if os.path.exists(folder) and os.path.realpath(folder) not in out:
            out.append(os.path.realpath(folder))
    return out


def _reachable(root: Path) -> list[str]:
    """What an agent or anyone may write: the RSI root and the shared temp folders."""
    return [str(root), *TEMP_FOLDERS, *temp_folders()]


def clean_path(root: Path, value: str | None = None) -> str:
    """value (default: this process's PATH) without the entries that are empty or relative, or lie inside the RSI
    root or a shared temp folder; each entry kept once, in order."""
    reachable, kept = _reachable(root), []
    for entry in (os.environ.get('PATH', '') if value is None else value).split(os.pathsep):
        if entry and Path(entry).is_absolute() and entry not in kept and \
                not any(inside(entry, folder) for folder in reachable):
            kept.append(entry)
    return os.pathsep.join(kept)


def _script(path: str) -> bool:
    with open(path, 'rb') as stream:
        return stream.read(2) == b'#!'


def _shim_targets(shim: str) -> list[str]:
    """Where the npm package that owns the shim `<package>/bin/codex.js` keeps the native program."""
    package = os.path.dirname(os.path.dirname(shim))
    try:
        name = json.loads(Path(package, 'package.json').read_text(encoding='utf-8')).get('name')
    except (OSError, ValueError, AttributeError):
        return []
    target = NATIVE_TARGETS.get((platform.system(), platform.machine()))
    if name != '@openai/codex' or target is None:
        return []
    platform_package, triple = target
    tail = os.path.join('vendor', triple, 'bin', 'codex.exe' if os.name == 'nt' else 'codex')
    return [os.path.join(package, 'node_modules', '@openai', platform_package, tail),
            os.path.join(os.path.dirname(package), platform_package, tail), os.path.join(package, tail)]


def native_codex(root: Path) -> str:
    """The real path of Codex's native program: the only Codex the control program ever starts.

    $RSI_CODEX must be absolute when set; otherwise `codex` is looked up in the control program's PATH without the
    entries clean_path drops. An npm shim (`#!/usr/bin/env node`, which would look node up in a PATH) is followed to
    its package's native program, never run. The result must exist, be executable, not be a script, and lie outside
    the RSI root (which agents can write) and outside the shared temp folders (which anyone can write); anything else
    is Refused rather than guessed.
    """
    program = os.environ.get('RSI_CODEX')
    if program is not None and not Path(program).is_absolute():
        raise Refused(f'$RSI_CODEX must be an absolute path, but it is {program!r}. {HOW_TO_SET_CODEX}')
    if os.name == 'nt' and program is None:
        # which() can consult the current directory on Windows; an owner running rsi from work/ must never
        # execute a program found there. npm's .cmd shim is identified but never executed.
        names = ('codex.exe', 'node_modules/@openai/codex/bin/codex.js')
        found = next((str(Path(directory) / name) for directory in clean_path(root).split(os.pathsep) if directory
                      for name in names if (Path(directory) / name).is_file()), None)
    else:
        found = program if program is not None else shutil.which('codex', path=clean_path(root))
    reachable = _reachable(root)

    def outside(path: str) -> bool:
        return not any(inside(path, folder) for folder in reachable)

    if not found or not os.path.isfile(found) or not outside(found):
        raise Refused(f'No usable Codex program was found ({program or "codex"!r}). {HOW_TO_SET_CODEX}')
    real = os.path.realpath(found)
    candidates = _shim_targets(real) if _script(real) else [real]
    for candidate in candidates:
        if (os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK)
                and outside(candidate) and not _script(candidate)
                and (os.name != 'nt' or Path(candidate).suffix.lower() == '.exe')):
            if os.name == 'nt':
                with open(candidate, 'rb') as stream:
                    if stream.read(2) != b'MZ':
                        continue
            return os.path.realpath(candidate)
    raise Refused(f'{found} is not the native Codex program, and no native program was found for it. '
                  f'{HOW_TO_SET_CODEX}')


@functools.cache
def git_program() -> str:
    """git: the first of GIT_PROGRAMS that is an executable file, resolved once (never looked up in a PATH);
    RuntimeError when none is."""
    for program in GIT_PROGRAMS:
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return program
    raise RuntimeError(f'git was not found (tried {", ".join(GIT_PROGRAMS)})')
