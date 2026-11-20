"""`rsi install-literature`: install Stage L into an existing, versioned Lab without replacing its other settings or
roles.

As before a task run, what sits in work/ is sealed and committed first (`manual or leftover`); the newest version is
then checked out in staging/, where tools/literature/ becomes exactly the seed's (what the version had there that the
seed lacks or has otherwise is listed, and stays reachable by `rsi rollback`), the seed's `[tool_servers.literature]`
is merged into the settings and its instructions into the literature role, and the result is committed as the owner's.
Afterwards the newest version is put back into work/ in every case, refused or not."""
import os
import re
import shutil
import tomllib
from pathlib import Path

from rsi.compose import load_envelope, write_home_config
from rsi.labrun import (discard, move_in, noting_refusals, open_store, problem, root_descriptor, stage, take_work,
                        unrecovered)
from rsi.paths import Layout, Refused
from rsi.records import Records
from rsi.settings import SETTINGS, load_settings
from rsi.tomlout import toml_value

ROLE = 'codex/agents/literature.toml'
PACKAGE = 'tools/literature'
NOTE = 'literature_install'


def _no_links(staged: Path, relative: Path) -> Path:
    """staged/relative, after refusing a link in any of its components. The checkout is private and static."""
    path = staged
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise Refused(f'Literature install found a link at {path.relative_to(staged)}')
    return path


def _target(staged: Path, relative: Path) -> Path:
    """A file of the checkout to read or write: no link in its path, and a file if it exists."""
    path = _no_links(staged, relative)
    path.parent.mkdir(exist_ok=True, parents=True)
    if path.exists() and not path.is_file():
        raise Refused(f'Literature install expected a file at {relative}')
    return path


def _toml(path: Path, name: str) -> tuple[str, dict]:
    """The text of a TOML file of the version and what it holds; Refused, naming the file, when it is not TOML."""
    try:
        text = path.read_text(encoding='utf-8')
        return text, tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Refused(f'{name} in the current version is not valid TOML ({" ".join(str(error).split())}), so '
                      'Literature was not installed. Fix the file, then run rsi install-literature again.') from None


def _extend_role(text: str, role: dict, addition: str) -> str:
    old = role.get('developer_instructions')
    if not isinstance(old, str):
        raise Refused(f'{ROLE} needs developer_instructions before installing Literature')
    if addition in old:
        return text
    expected = {**role, 'developer_instructions': old.rstrip() + '\n\n' + addition + '\n'}
    # Edit only the TOML string containing the instructions; keep all other fields and comments verbatim.
    key = r"""(?m)^[ \t]*["']?developer_instructions["']?[ \t]*=[ \t]*"""
    for match in re.finditer(key, text):
        start = match.end()
        for quote in ('"""', "'''", '"', "'"):
            if not text.startswith(quote, start):
                continue
            for close in re.finditer(re.escape(quote), text[start + len(quote):]):
                end = start + len(quote) + close.end()
                value = toml_value(expected['developer_instructions'])
                if len(quote) == 3:
                    value = text[start:end - len(quote)].rstrip() + '\n\n' + addition + '\n' + quote
                edited = text[:start] + value + text[end:]
                try:
                    if tomllib.loads(edited) == expected:
                        return edited
                except tomllib.TOMLDecodeError:
                    continue
            break
    raise Refused(f'Could not merge the Literature instructions in {ROLE}')


def _install_package(staged: Path, seed: Path) -> list[str]:
    """Make the checkout's tools/literature/ exactly the seed's (__pycache__ left out). One line for each entry the
    version had there that is gone now (the seed lacks it) or holds other bytes now."""
    source = seed / PACKAGE
    wanted = {}
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if path.is_file() and not path.is_symlink() and '__pycache__' not in relative.parts:
            wanted[relative.as_posix()] = path.read_bytes()
    if 'server.py' not in wanted:
        raise Refused('The Literature service entry point is missing from the seed')
    folder = _no_links(staged, Path(PACKAGE))
    found = {}
    if os.path.lexists(folder):
        if not folder.is_dir():
            raise Refused(f'Literature install expected a folder at {PACKAGE}')
        for top, folders, names in os.walk(folder):         # a link is listed, never followed
            for name in folders + names:
                path = Path(top) / name
                if path.is_symlink() or path.is_file():
                    found[path.relative_to(folder).as_posix()] = None if path.is_symlink() else path.read_bytes()
        shutil.rmtree(folder)
    for relative, data in wanted.items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    lines = [f'{PACKAGE}/{name} was removed (the seed does not have it)'
             for name in sorted(found.keys() - wanted.keys())]
    lines += [f"{PACKAGE}/{name} was replaced by the seed's" for name in sorted(found.keys() & wanted.keys())
              if found[name] != wanted[name]]
    return lines


def _install_files(staged: Path, seed: Path) -> list[str]:
    """Install the seed's Literature into the checkout; the lines of _install_package."""
    lines = _install_package(staged, seed)

    path = _target(staged, Path(SETTINGS))
    seed_settings = (seed / SETTINGS).read_text(encoding='utf-8')
    header = '[tool_servers.literature]'
    block = seed_settings[seed_settings.index('\n' + header):].strip()
    service = tomllib.loads(block)['tool_servers']['literature']
    text, current = _toml(path, SETTINGS) if path.exists() else ('', {})
    servers = current.get('tool_servers', {})
    if not isinstance(servers, dict):
        raise Refused('Literature install requires tool_servers to be a TOML table')
    if 'literature' in servers:
        if servers['literature'] != service:
            raise Refused('Literature install conflict: a different tool_servers.literature is already configured')
    else:
        merged = text.rstrip() + '\n\n' + block + '\n'
        try:
            tomllib.loads(merged)
        except tomllib.TOMLDecodeError:
            # A TOML inline table is closed. Extend it in place and verify the parsed edit before applying it.
            expected = {**current, 'tool_servers': {**servers, 'literature': service}}
            for match in re.finditer(r"""(?m)^[ \t]*["']?tool_servers["']?\s*=\s*\{""", text):
                value = 'literature = ' + toml_value(service) + (', ' if servers else '')
                merged = text[:match.end()] + value + text[match.end():]
                try:
                    if tomllib.loads(merged) == expected:
                        break
                except tomllib.TOMLDecodeError:
                    continue
            else:
                raise Refused('Could not merge Literature into the existing tool_servers table') from None
        path.write_text(merged, encoding='utf-8')

    path = _target(staged, Path(ROLE))
    seed_role = (seed / ROLE).read_text(encoding='utf-8')
    if not path.exists():
        path.write_text(seed_role, encoding='utf-8')
    else:
        text, role = _toml(path, ROLE)
        addition = tomllib.loads(seed_role)['developer_instructions'].rstrip().rsplit('\n\n', 1)[1]
        path.write_text(_extend_role(text, role, addition), encoding='utf-8')
    return lines


def _restage(layout: Layout, records: Records, store, envelope: dict, root_fd: int, context: dict) -> list[str]:
    """Put the newest version back into work/ (sealed before the install) and rewrite the Lab home's config.toml for
    it; one line per problem. Never raises an Exception, so it cannot hide the install's own error."""
    lines, settings = [], None
    with problem(lines, 'work/ could not be replaced by the newest version'):
        staged, settings = stage(layout, store, store.head())
        move_in(layout, records, root_fd, staged, 'work', context, lines)
    if settings is not None:
        with problem(lines, "The Lab's Codex configuration (codex-home/config.toml) could not be updated"):
            write_home_config(layout, envelope, settings)
    return lines


def _note(records: Records, data: dict, lines: list[str]) -> None:
    with problem(lines, f'The install could not be noted in records/ ({NOTE})'):
        records.note(NOTE, data)


def install_literature(layout: Layout, seed: Path, lines: list[str] | None = None) -> tuple[str, str]:
    """Install the seed's Literature (seed: the seed's work/ folder) into a new version; (the version before, the
    version after), equal when nothing changed. Called under the normal command lock.

    lines, if given, gets one English line for each entry of tools/literature/ that was removed or replaced, and for
    each problem met while putting the newest version back into work/. Each outcome (installed, unchanged, refused,
    failed) is noted in records/index.jsonl as a `literature_install` line with those lines, and a refusal also as
    every command's is (`refusal`). A refusal whose clean-up met problems names them too."""
    lines = [] if lines is None else lines
    records = Records(layout.records)
    context = {'command': 'install-literature'}
    with noting_refusals(records, context), root_descriptor(layout) as root_fd:
        unrecovered(layout, records)
        store, envelope = open_store(layout), load_envelope(layout.owner)
        notes = []
        take_work(layout, records, store, root_fd, records.calls(), context, notes)
        baseline = store.head()
        try:
            if notes:
                raise Refused('The pre-install workspace needed attention: ' + ' '.join(notes))
            staged, _ = stage(layout, store, baseline)
            try:
                changed = _install_files(staged, seed)
                settings = load_settings(staged)
                if settings.problems:
                    raise Refused('Literature install found invalid workspace settings: ' + ' '.join(settings.problems))
                version = store.commit(staged, 'owner', 'Install the Literature tool server') or baseline
            finally:
                discard(staged)
        except BaseException as error:
            restaged = _restage(layout, records, store, envelope, root_fd, context)
            outcome = 'refused' if isinstance(error, Refused) else 'failed'
            _note(records, {'outcome': outcome, 'baseline': baseline, 'version': None, 'lines': restaged,
                            **context}, restaged)
            lines += restaged
            if isinstance(error, Refused) and restaged:
                raise Refused(f'{error} Afterwards: {"; ".join(restaged)}') from None
            raise
        restaged = _restage(layout, records, store, envelope, root_fd, context)
        lines += changed + restaged
        _note(records, {'outcome': 'installed' if version != baseline else 'unchanged', 'baseline': baseline,
                        'version': version, 'lines': changed + restaged, **context}, lines)
    return baseline, version
