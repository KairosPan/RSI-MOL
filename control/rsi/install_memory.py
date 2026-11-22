"""Install the memory reader, skill and instructions into an existing versioned Lab."""
import hashlib
import os
import tomllib
from pathlib import Path

from rsi.install_literature import ROLE, _extend_role, _no_links, _target, _toml, install_feature, merge_service
from rsi.paths import Layout, Refused

# Released templates eligible for a versioned upgrade.
PREVIOUS_TEMPLATE_HASHES = {
    'tools/memory/query.py': {
        '78b6f1a05e73ae35db02d851bb6e8c536a563903683d87e583d2eb3fce020ff4',
        '44790f30a9a0f477bce2f21e6fa3082d0f1ee565d2101a9e973dbce15b0517a8',
        '2bec02410796d92ef11c5fa932b7d07b4adf7e632289fb0858fa263345a473ce',
        '73c14c6591606dd73b8fd825b61e1e589108d5b1336c9eaa53c3cd4a1ca604b1',
        'b19a5c57040dbe020a3028e15fb6844ec0d27bf9b86d20cc39f2f8f8152215f7',
        '654fcd548e1e0d7554f8a1f15b08c4055488e0b83afbda9858910843c58a172c',
    },
    'tools/memory/server.py': {
        '54064b893cacd97eadd1cecceb254ab39e85c7c1db31cc43531cd5250b307b8a',
        'fd8027c3f0c305540ef4f51313d3ee5b9541aadb3cef18f9a587cc13da578fb1',
        'bb9c460e67eee77937abc22003d587f48d0f1b585970d70c4d5c2043615bf055',
        '7f3a2921f6630e6bc4215059696b038fc875d69466bb683ee35d7fb7743c11ab',
        'f0abb1664efac87fca892f15a26a4fffd9aed7818073d6c1ca58423388ee0105',
    },
    'skills/scientific-memory/SKILL.md': {
        'fdc5abe161eb6d9cd1e5abcc15f23e1241e0adc2a1260f917ec81df74a72f541',
        'f5170261e0c6d3d8672c618375a5dfee8bda8e01074dde5297c971c894614775',
        'ffa143fbc8e353c40a410a9a179744146a87da839e910f900e73902d3f16a199',
        'b995423358a7e32f0d1e97c8485f8fddcfbc7d6096f904bb832fa101f1ca5b4b',
        '461ed3807139e00ac2390e53a1c0f0efac2248dc7d874adad3cda10a944cbd72',
        '90124fa625ca23305090d8efea45fcdbebc6093e38277c4f8ab3a295b122722d',
    },
}


def _install_folder(staged: Path, seed: Path, relative: str, required: str) -> None:
    """Copy the shipped files into an absent or compatible package without displacing workspace edits."""
    source = seed / relative
    if not (source / required).is_file():
        raise Refused(f'The memory seed is missing {relative}/{required}')
    wanted = {p.relative_to(source): p for p in source.rglob('*')
              if '__pycache__' not in p.parts and p.name != '.DS_Store' and p.is_file() and not p.is_symlink()}
    target = _no_links(staged, Path(relative), 'Memory')
    if os.path.lexists(target):
        if not target.is_dir():
            raise Refused(f'Memory install conflict: {relative} is not a directory')
        for directory, folders, files in os.walk(target):
            for name in folders + files:
                entry = Path(directory) / name
                key = entry.relative_to(target)
                generated = name == '.DS_Store' or ('__pycache__' in key.parts[:-1] and key.suffix == '.pyc')
                if generated and not entry.is_symlink() and entry.is_file():
                    entry.unlink()
                    continue
                if entry.is_symlink() or (not entry.is_dir() and key not in wanted):
                    raise Refused(f'Memory install conflict: preserve the workspace entry {relative}/{key}')
    for key, path in wanted.items():
        destination = _target(staged, Path(relative) / key, 'Memory')
        new = path.read_bytes()
        if destination.exists():
            old = destination.read_bytes()
            if old == new:
                continue
            if hashlib.sha256(old).hexdigest() not in PREVIOUS_TEMPLATE_HASHES.get(f'{relative}/{key}', ()):
                raise Refused(f'Memory install conflict: preserve the workspace entry {relative}/{key}')
        destination.write_bytes(new)


def _instructions(staged: Path, seed: Path, relative: str, notes: list[str]) -> None:
    path = _target(staged, Path(relative), 'Memory')
    template = (seed / relative).read_text(encoding='utf-8')
    if not path.exists():
        path.write_text(template, encoding='utf-8')
        return
    if relative == ROLE:
        template = tomllib.loads(template)['developer_instructions']
        original, role = _toml(path, relative, 'Memory')
        current = role.get('developer_instructions')
    else:
        original = current = path.read_text(encoding='utf-8')
    if not isinstance(current, str):
        raise Refused(f'Memory install requires instructions in {relative}')
    prefix = 'Use the scientific-memory skill'
    paragraphs = [p for p in template.strip().split('\n\n') if p.startswith(prefix)]
    if len(paragraphs) != 1:
        raise Refused(f'The memory instructions are missing or ambiguous in seed {relative}')
    addition = paragraphs[0]
    if addition in current:
        return
    if 'scientific-memory' in current:
        notes.append(f'Kept the workspace scientific-memory instructions in {relative}.')
        return
    merged = (_extend_role(original, role, addition, 'Memory') if relative == ROLE else
              original.rstrip() + '\n\n' + addition + '\n')
    path.write_text(merged, encoding='utf-8')


def _install_files(staged: Path, seed: Path) -> list[str]:
    _install_folder(staged, seed, 'tools/memory', 'server.py')
    _install_folder(staged, seed, 'skills/scientific-memory', 'SKILL.md')
    merge_service(staged, seed, 'memory')
    notes = []
    for relative in ('AGENTS.md', ROLE):
        _instructions(staged, seed, relative, notes)
    return notes


def install_memory(layout: Layout, seed: Path, lines: list[str] | None = None) -> tuple[str, str]:
    return install_feature(layout, seed, lines, name='memory', install_files=_install_files,
                           message='Install the scientific memory reader')
