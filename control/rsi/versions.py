"""Versions of the Lab workspace, kept in one git store outside the folders it versions.

The store is a bare repository serving several folders: staged and sealed copies of work/ and trial/ (the control
program never runs git on a folder an agent can still write), and meta/candidate/. Every git call that touches a
folder sets GIT_WORK_TREE to it and GIT_INDEX_FILE to that folder's own index inside the store, so no folder ever
gets a `.git` entry; the index is removed again once the call is done. Versions are commits on refs/heads/main,
each tagged V0001, V0002, ...; a snapshot is a commit on refs/rounds/<round> whose parent is the head of main.

Agents write the folders, so their content is untrusted: git reads no configuration, hooks, ignore rules or
attributes from them or from the owner's git setup, content is stored byte for byte, and no name reaches a git
command line before it passes a strict pattern.
"""
import hashlib
import os
import re
import subprocess
from pathlib import Path

from rsi.paths import safe_id

VERSION = re.compile(r'V[0-9]{4,}')
COMMIT_ID = re.compile(r'[0-9a-f]{40}')
# A name git stores unchanged: no control characters or angle brackets, nothing git trims at either end.
AUTHOR = re.compile(r'[^\x00-\x20\x7f<>,:;"\'\\](?:[^\x00-\x1f\x7f<>]*[^\x00-\x20\x7f<>,:;"\'\\])?')
MAIN = 'refs/heads/main'
TIMEOUT = 600  # seconds for one git call
# With the system and global configuration switched off, these are all the settings git reads. The reflog keeps
# every snapshot a round ref once pointed at.
STORE_CONFIG = (
    ('user.name', 'rsi'), ('user.email', 'rsi@localhost'),
    ('core.hooksPath', os.devnull), ('core.excludesFile', os.devnull), ('core.attributesFile', os.devnull),
    ('core.quotePath', 'false'), ('core.logAllRefUpdates', 'always'),
    ('gc.reflogExpire', 'never'), ('gc.reflogExpireUnreachable', 'never'),
)
# info/attributes outranks every .gitattributes: no end-of-line, ident, filter or encoding conversion, and diff
# decides text or binary by itself.
ATTRIBUTES = '* -text -ident -filter -working-tree-encoding !diff !merge\n'


def _any_case(name: str) -> str:
    """A gitignore glob matching name in any letter case, e.g. 'out' -> '[oO][uU][tT]'."""
    return ''.join(f'[{c.lower()}{c.upper()}]' if c.isalpha() else c for c in name)


class VersionStore:
    EXCLUDED = ('.tmp/', 'out/', 'in/')       # never versioned, never removed by checkout

    def __init__(self, git_dir: Path) -> None:
        self.git_dir = Path(git_dir).resolve()
        # The excluded entries are top-level names of any type and any letter case, so a version can never hold
        # something checkout would write over them.
        self._excluded = [entry.rstrip('/') for entry in self.EXCLUDED]
        self._ignore = [f'/{_any_case(name)}' for name in self._excluded]

    def init(self) -> None:
        """Create the store if missing; idempotent. Its settings and info files are rewritten every time."""
        if not (self.git_dir / 'HEAD').is_file():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            self._git('init', '--quiet', '--bare', '--initial-branch=main', '--object-format=sha1', '--template=')
        for key, value in STORE_CONFIG:
            self._git('config', key, value)
        (self.git_dir / 'info').mkdir(exist_ok=True)
        (self.git_dir / 'info' / 'exclude').write_text(''.join(f'{pattern}\n' for pattern in self._ignore))
        (self.git_dir / 'info' / 'attributes').write_text(ATTRIBUTES)
        (self.git_dir / 'indexes').mkdir(exist_ok=True)

    def head(self) -> str | None:
        """The newest version name, e.g. 'V0003'; None before the first."""
        names = self._versions()
        return names[-1] if names else None

    def resolve(self, name: str) -> str:
        """A version name or a full commit id of this store -> commit id; ValueError for anything else."""
        if isinstance(name, str) and VERSION.fullmatch(name):
            ref = f'refs/tags/{name}'
        elif isinstance(name, str) and COMMIT_ID.fullmatch(name):
            ref = name
        else:
            raise ValueError(f'不是版本名或提交号：{name!r}。版本名形如 V0001，提交号是 40 位十六进制。')
        done = self._git('rev-parse', '--verify', '--quiet', '--end-of-options', f'{ref}^{{commit}}', check=False)
        if done.returncode != 0:
            raise ValueError(f'版本库里没有这个版本或提交：{name}')
        return done.stdout.decode().strip()

    def tree_of(self, name: str) -> str:
        """The tree id of a version or commit."""
        return self._out('rev-parse', '--verify', f'{self.resolve(name)}^{{tree}}')

    def tree_of_dir(self, folder: Path) -> str:
        """The tree id of the folder's content as it is now; commits nothing."""
        return self._stage(folder)

    def commit(self, folder: Path, author: str, message: str) -> str | None:
        """A new version on main from the folder's content; None if that content equals the head version."""
        if not isinstance(author, str) or not AUTHOR.fullmatch(author):
            raise ValueError(f'作者名不合规：{author!r}')
        tree = self._stage(folder)
        parent = self._main()
        if parent is not None and tree == self._out('rev-parse', '--verify', f'{parent}^{{tree}}'):
            return None
        commit = self._commit_tree(tree, parent, message, author)
        names = self._versions()
        number = int(names[-1][1:]) + 1 if names else 1
        version = f'V{number:04d}'
        main = f'update {MAIN} {commit} {parent}' if parent is not None else f'create {MAIN} {commit}'
        # One transaction: the tag and main move together or not at all.
        self._git('update-ref', '-m', f'version {version}', '--stdin',
                  stdin=f'create refs/tags/{version} {commit}\n{main}\n'.encode())
        return version

    def snapshot(self, folder: Path, ref: str, message: str) -> str:
        """A commit of the folder on refs/rounds/<ref> whose parent is main's head; main does not move."""
        safe_id(ref, 'round')
        tree = self._stage(folder)
        commit = self._commit_tree(tree, self._main(), message, None)
        self._git('update-ref', '-m', f'snapshot {ref}', f'refs/rounds/{ref}', commit)
        return commit

    def checkout(self, name: str, folder: Path) -> None:
        """Make the folder exactly that content; the EXCLUDED entries are left alone.

        With an empty index everything else in the folder is untracked, so it is all removed first (nested git
        repositories and files a .gitignore names included; links are removed, never followed), then the version is
        written. Writing into an emptied folder gives every name the letter case of the version: on a
        case-insensitive filesystem git would otherwise write into an existing folder whose name differs in case.
        """
        commit = self.resolve(name)
        folder = self._folder(folder)
        index = self._index(folder)
        index.unlink(missing_ok=True)
        try:
            self._git('clean', '-q', '-ffdx', *(f'--exclude={pattern}' for pattern in self._ignore), folder=folder)
            self._git('read-tree', '--reset', '-u', commit, folder=folder)
        finally:
            index.unlink(missing_ok=True)

    def check_writable(self) -> None:
        """RuntimeError (or a git time-out) unless a new version could be recorded now: main and the next version's
        tag can be locked (a ref transaction that is prepared and then aborted, so nothing changes; a stale
        `main.lock` fails it), and the object and index folders are writable."""
        main = self._main()
        if main is None:
            raise RuntimeError('版本库里还没有任何版本')
        names = self._versions()
        tag = f'V{int(names[-1][1:]) + 1:04d}' if names else 'V0001'
        self._git('update-ref', '--stdin',
                  stdin=f'start\nverify {MAIN} {main}\ncreate refs/tags/{tag} {main}\nprepare\nabort\n'.encode())
        for folder in ('objects', 'indexes'):
            if not os.access(self.git_dir / folder, os.W_OK):
                raise RuntimeError(f'{self.git_dir / folder} 不可写')

    def diff(self, old: str, new: str, limit: int = 1_000_000) -> str:
        """`git diff` from one version or commit to another, cut after `limit` bytes of output (with a note)."""
        old, new = self.resolve(old), self.resolve(new)
        command = ['git', 'diff', '--no-color', '--no-ext-diff', '--no-textconv', old, new, '--']
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(None),
                              cwd=self.git_dir) as process:
            data = process.stdout.read(limit + 1)
            cut = len(data) > limit
            if cut:
                process.kill()
            error = process.communicate(timeout=TIMEOUT)[1]
        if not cut and process.returncode != 0:
            raise RuntimeError(f'git diff failed ({process.returncode}): {error.decode(errors="replace").strip()}')
        text = data[:limit].decode('utf-8', errors='replace')
        return text + f'\n…（差异超过 {limit} 字节，其余部分已截去）\n' if cut else text

    def log(self) -> list[dict]:
        """All versions, newest first: version, commit, tree, author, message, time (ISO 8601)."""
        out = self._git('for-each-ref', '--format=%(refname:lstrip=2)%00%(objectname)%00%(tree)%00%(authorname)'
                        '%00%(authordate:iso-strict)%00%(contents)%00', 'refs/tags/').stdout
        entries = []
        for record in out.decode('utf-8', errors='replace').split('\0\n'):
            fields = record.split('\0')
            if len(fields) == 6 and VERSION.fullmatch(fields[0]):
                version, commit, tree, author, time, message = fields
                entries.append({'version': version, 'commit': commit, 'tree': tree, 'author': author,
                                'message': message, 'time': time})
        return sorted(entries, key=lambda entry: int(entry['version'][1:]), reverse=True)

    # Internals.

    def _versions(self) -> list[str]:
        """Version names, oldest first."""
        out = self._out('for-each-ref', '--format=%(refname:lstrip=2)', 'refs/tags/')
        return sorted((name for name in out.splitlines() if VERSION.fullmatch(name)), key=lambda name: int(name[1:]))

    def _main(self) -> str | None:
        done = self._git('rev-parse', '--verify', '--quiet', f'{MAIN}^{{commit}}', check=False)
        return done.stdout.decode().strip() if done.returncode == 0 else None

    def _commit_tree(self, tree: str, parent: str | None, message: str, author: str | None) -> str:
        """A commit object; the message goes in on stdin, verbatim, and the author through the environment."""
        return self._out('commit-tree', tree, *(['-p', parent] if parent is not None else []),
                         stdin=message.encode(), author=author)

    def _stage(self, folder: Path) -> str:
        """Build the folder's index afresh from the folder's content and return its tree id.

        A fresh index takes every name as it is on disk: on a case-insensitive filesystem git would otherwise keep
        an old index entry through a case-only rename. --force makes a .gitignore in the folder ineffective; the
        excluded names are left out by pathspec instead.
        """
        folder = self._folder(folder)
        self._refuse_git_entries(folder)
        index = self._index(folder)
        index.unlink(missing_ok=True)
        try:
            self._git('add', '--all', '--force', '--', '.',
                      *(f':(top,exclude,icase){name}' for name in self._excluded), folder=folder)
            return self._out('write-tree', folder=folder)
        finally:
            index.unlink(missing_ok=True)

    def _folder(self, folder: Path) -> Path:
        """The folder, absolute and resolved; ValueError unless it is a real folder apart from the store.

        Overlap is judged by file identity, not by path text: a path may name the same folder in another letter case.
        """
        folder = Path(folder)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'不是文件夹（或是链接）：{folder}')
        real = folder.resolve()
        here, store = os.stat(real), os.stat(self.git_dir)
        if any(os.path.samestat(here, os.stat(path)) for path in (self.git_dir, *self.git_dir.parents)) or \
                any(os.path.samestat(os.stat(path), store) for path in real.parents):
            raise ValueError(f'文件夹和版本库互相包含：{folder}')
        return real

    def _refuse_git_entries(self, folder: Path) -> None:
        """ValueError if the folder holds a `.git` entry: git would skip it or keep a nested repository as a bare
        reference, so the version would silently lack that content."""
        for top, dirs, files in os.walk(folder):
            if top == str(folder):
                dirs[:] = [name for name in dirs if name.lower() not in self._excluded]
            for name in dirs + files:
                if name.lower() == '.git':
                    raise ValueError(f'文件夹里有 .git，不能存为版本：{Path(top, name)}')

    def _index(self, folder: Path) -> Path:
        return self.git_dir / 'indexes' / hashlib.sha256(str(folder).encode()).hexdigest()[:32]

    def _env(self, folder: Path | None, author: str | None = None) -> dict:
        """The caller's environment without any GIT_* variable, pointed at this store only."""
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_DIR=str(self.git_dir), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        if folder is not None:
            env.update(GIT_WORK_TREE=str(folder), GIT_INDEX_FILE=str(self._index(folder)))
        if author is not None:
            env['GIT_AUTHOR_NAME'] = author
        return env

    def _git(self, *args: str, folder: Path | None = None, stdin: bytes = b'', author: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(['git', *args], input=stdin, capture_output=True, env=self._env(folder, author),
                              cwd=folder or self.git_dir, timeout=TIMEOUT)
        if check and done.returncode != 0:
            raise RuntimeError(f'git {args[0]} failed ({done.returncode}): '
                               f'{done.stderr.decode(errors="replace").strip()}')
        return done

    def _out(self, *args: str, **options) -> str:
        return self._git(*args, **options).stdout.decode().strip()
