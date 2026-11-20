"""`rsi init` and `rsi recover`, and the lock that every command but `status` holds.

init builds an RSI root, or completes one: it is safe to run again and never replaces what is there. It makes the
folders and, before anything can rewrite it, keeps a verified copy of the owner's own codex-home/config.toml
(compose.keep_owner_config: config.toml.before-rsi, -2, …). It writes the owner's files that are missing (the
envelope, imported from the newest usable copy, and the seed's limits and Meta files), creates the version store
and, when the store has no version, seals what sits in work/, makes V0001 from the seed in staging/ (author
`init`), and then adds to the sealed tree the seed files it lacks (nothing in it is replaced) before committing it
as `manual or leftover`, as before a task run; then main's head is staged and renamed in as work/. It creates env/
with `<python> -m venv` when env/ is missing, writes the Lab home's
config.toml (naming the top-level keys of the owner's original it does not carry over), and reports whether each
Codex home is signed in.

work/, meta/, library/ and env/ are agent-writable: init never reads, walks or runs git on them in place (the rule of
rsi.labrun). It only looks at the entry itself in the root (lstat), creates a missing one, and seals work/ before
handling what was in it. trial/ and candidate/ are made by the runs that use them.

recover marks every call still `started` as `interrupted` (a call is never started again), first stopping the process
group recorded in the call's process.json when it is provably that call's (SIGTERM, then SIGKILL after STOP_GRACE
seconds); it moves what only an interrupted run leaves (entries of staging/ and sealed entries in the root) under
sealed/, never deleting or reading them, and puts main's head back into a missing work/. Until it has run, rsi task,
meta and rollback refuse to start (rsi.labrun.unrecovered).
"""
import fcntl
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from rsi.codexrun import codex_environment, codex_program
from rsi.compose import (ENVELOPE, HOME_CONFIG, TOOL_FOLDERS, backup_names, keep_owner_config, load_envelope,
                         read_regular, write_home_config)
from rsi.labrun import (MANUAL, PROCESS_FILE, WORKSPACE_ERRORS, discard, move_in, now, process_start, refusing,
                        restore_work, root_descriptor, settle, stage, stamp)
from rsi.paths import Layout, Refused
from rsi.records import STARTED, Records
from rsi.settings import Settings
from rsi.tomlout import render_file
from rsi.versions import VersionStore
from rsi.workspace import SEALED, has_entry, keep, kind, open_folder, seal

DEFAULT_PYTHON = '/opt/homebrew/bin/python3'
IMPORTED = ('features', 'apps', 'plugins')      # tables of that file the envelope takes (and approval_policy)
UNSAFE_IN_ROOT = '"[]*?\n\r'                    # Codex's own sandbox cannot confine a path with these
# What Codex loads from a project root. The RSI root is a git repository, so Codex may take it for the project root of
# work/ (and meta/): none of these may be in it.
LOADED_AT_ROOT = ('AGENTS.md', 'AGENTS.override.md', '.agents', '.codex')
OWNER_FOLDERS = ('owner', 'tasks', 'records', 'versions', 'staging', 'sealed', 'codex-home', 'codex-home-meta')
AGENT_FOLDERS = ('meta', 'library')             # env/ is made by venv; work/ is renamed in; trial/, candidate/ later
OWNER_SEED = (Path('limits.toml'), Path('meta', 'AGENTS.md'), Path('meta', 'settings.toml'))
LOGIN_SECONDS = 60
VENV_SECONDS = 600
# A call's process group is signalled only when the process with the recorded pid started within this many seconds
# of the recorded start time (process.json): the pid may since have been reused by an unrelated process.
SAME_PROCESS_SECONDS = 2
STOP_GRACE = 5                  # seconds recover waits for a call's process group after SIGTERM, before SIGKILL
ENVELOPE_HEADER = ('# Lab 的对外范围。负责人的文件，agent 改不了。由 rsi init 写成：approval_policy、features、apps、plugins\n'
                   '# 取自负责人原来的 codex-home/config.toml 的备份（config.toml.before-rsi…），[rsi] 是控制程序的设置。\n')


@contextmanager
def locked(layout: Layout):
    """An exclusive flock on layout.lock, held until the block ends; Refused if another rsi holds it. The
    descriptor is not inherited by the programs rsi starts, so a leftover process never keeps the lock."""
    try:
        fd = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise Refused(f'锁文件 {layout.lock} 打不开（{error.strerror or error}）') from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('另一个 rsi 正在运行，请等它结束再试') from None
        yield
    finally:
        os.close(fd)


# init

def loaded_at_root(layout: Layout) -> list[str]:
    """One Chinese line for each name of LOADED_AT_ROOT the RSI root holds, in whatever spelling the file system takes
    for it (workspace.has_entry, which also says yes when it cannot tell)."""
    return [f'RSI 文件夹里有 {name}：这个文件夹是 git 版本库，Codex 可能把它当作 work/ 的项目根目录，从这里读入说明、'
            f'技能或配置；请把它移出 RSI 文件夹' for name in LOADED_AT_ROOT if has_entry(layout.root, name)]


def _report() -> dict:
    return {'folders': [], 'files': [], 'envelope': None, 'first_version': None, 'version': None, 'work': None,
            'env': None, 'signed_in': {}, 'original': None, 'envelope_source': None, 'problems': []}


def _folders(layout: Layout, root_fd: int, report: dict) -> None:
    """Create the missing folders of the root. One of the owner's that is a link or a file is refused; one an agent
    writes that is not a folder is left as it is (the run that uses it seals it) and reported."""
    for name in OWNER_FOLDERS + AGENT_FOLDERS:
        found = kind(name, root_fd)
        if found is None:
            os.mkdir(name, 0o700 if name.startswith('codex-home') else 0o755, dir_fd=root_fd)
            report['folders'].append(name)
        elif found != 'folder' and name in OWNER_FOLDERS:
            raise Refused(f'{layout.root / name} 不是文件夹（可能是符号链接）；控制程序不会跟着它走，请改成普通文件夹')
        elif found != 'folder':
            report['problems'].append(f'{name}/ 不是文件夹（可能是符号链接），没有动它')
    meta = layout.owner / 'meta'
    if meta.is_symlink() or (os.path.lexists(meta) and not meta.is_dir()):
        raise Refused(f'{meta} 不是文件夹（可能是符号链接），请改成普通文件夹')
    if not meta.exists():
        meta.mkdir()
        report['folders'].append('owner/meta')


def _create(path: Path, data: bytes, mode: int = 0o644) -> None:
    """A new file holding data, all at once: written to a synced temporary file in the same folder, then linked to
    path, so a crash or a full disk never leaves a partial file under that name. FileExistsError if anything (a link
    too) has that name."""
    fd, temporary = tempfile.mkstemp(prefix='.rsi-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _keep_owner_config(layout: Layout, report: dict) -> None:
    """A verified copy of the owner's codex-home/config.toml before anything can rewrite it (Refused, and nothing
    rewritten, when none can be made). A link there is never followed: reported, and only the link is replaced."""
    if os.path.islink(layout.lab_home / HOME_CONFIG):
        report['problems'].append('codex-home/config.toml 是符号链接，没有备份，也没有从它导入；'
                                  '生成的配置只会替换这个链接本身，不会改它指向的文件')
    kept, made = keep_owner_config(layout)
    if made:
        report['files'].append(f'codex-home/{kept.name}')


def _original(layout: Layout, problems: list[str]) -> tuple[dict | None, Path | None]:
    """The owner's original config as a table, from the newest copy (backup_names, newest first) that is a regular,
    non-empty file holding valid TOML, and that copy's path; (None, None) when there is none. Each copy passed over
    on the way is a problem line: an empty or broken copy is never taken as "nothing to import"."""
    for kept in reversed(backup_names(layout)):
        try:
            data = read_regular(kept)
        except OSError as error:
            problems.append(f'codex-home/{kept.name} 读不了（{error.strerror or error}；可能是符号链接），没有从它导入')
            continue
        if data is None:
            continue
        if not data.strip():
            problems.append(f'codex-home/{kept.name} 是空的（{len(data)} 字节），可能没写完，没有从它导入')
            continue
        try:
            return tomllib.loads(data.decode('utf-8')), kept
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
            problems.append(f'codex-home/{kept.name} 不是合法的 TOML（{" ".join(str(error).split())}），没有从它导入')
    return None, None


def _envelope(layout: Layout, report: dict, original: dict | None, source: Path | None) -> None:
    """owner/lab-envelope.toml when missing: approval_policy, features, apps and plugins of the owner's original
    config (the newest usable copy, never the config.toml rsi generates), and [rsi] extra_read (the tool folders
    that exist) and python. Nothing else of that file is taken: no projects, no tui, no model."""
    path = layout.owner / ENVELOPE
    if os.path.lexists(path):
        report['envelope'] = 'kept'
        return
    table = original or {}
    envelope = {'approval_policy': table.get('approval_policy', 'on-request')}
    envelope.update({key: table[key] for key in IMPORTED if key in table})
    envelope['rsi'] = {'extra_read': [folder for folder in TOOL_FOLDERS if os.path.exists(folder)],
                       'python': DEFAULT_PYTHON}
    try:
        text = ENVELOPE_HEADER + render_file(envelope)
    except (TypeError, ValueError) as error:
        raise Refused(f'{source} 里有写不进 {ENVELOPE} 的值（{error}）；请自己先写好 {path}') from None
    _create(path, text.encode('utf-8'))
    report['envelope'] = 'default' if original is None else 'imported'
    report['envelope_source'] = None if source is None else f'codex-home/{source.name}'
    report['files'].append(f'owner/{ENVELOPE}')


def _owner_seed(layout: Layout, seed: Path, report: dict) -> None:
    """The owner's limits and Meta files from the seed, each only when missing."""
    for relative in OWNER_SEED:
        target = layout.owner / relative
        if os.path.lexists(target):
            continue
        _create(target, (seed / 'owner' / relative).read_bytes())
        report['files'].append(f'owner/{relative.as_posix()}')


def _first_version(layout: Layout, store: VersionStore, seed: Path, report: dict) -> None:
    """V0001 by `init`: the seed's work files, copied into staging/ and committed from there."""
    layout.staging.mkdir(exist_ok=True)
    folder = layout.staging / stamp()
    try:
        shutil.copytree(seed / 'work', folder, symlinks=True)
        report['first_version'] = store.commit(folder, 'init', '初始版本（rsi init 用种子文件建立）')
    finally:
        discard(folder)


def _add_seed_files(source: Path, tree: Path) -> list[str]:
    """Copy each file of the seed's work/ into the sealed tree where nothing has its name; never replaces and never
    follows a link (every folder is opened with O_NOFOLLOW relative to its parent). Lines for what was not added."""
    lines = []
    for path in sorted(source.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        relative, fds = path.relative_to(source), []
        try:
            fds.append(open_folder(tree))
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, 0o755, dir_fd=fds[-1])
                except FileExistsError:
                    pass
                fds.append(open_folder(part, fds[-1]))
            try:
                fd = os.open(relative.parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=fds[-1])
            except FileExistsError:
                continue                                    # what was there wins
            with os.fdopen(fd, 'wb') as stream:
                stream.write(path.read_bytes())
        except OSError as error:
            lines.append(f'种子文件 {relative.as_posix()} 没能补进 work/ 里原有的内容（{error.strerror or error}）')
        finally:
            for fd in reversed(fds):
                os.close(fd)
    return lines


def _first_versions(layout: Layout, records: Records, store: VersionStore, root_fd: int, seed: Path, context: dict,
                    report: dict) -> None:
    """The first init: what sits in work/ is sealed first (if that is refused, nothing has been committed and the
    next init starts over), then V0001 is made from the seed (if that fails, the sealed entry is renamed back to
    work/). Then the missing seed files are added on the sealed tree and it is committed as `manual or leftover` when
    it differs from V0001 (labrun's handling of a found work/). A link or anything that cannot be versioned is kept
    under sealed/ with a note."""
    label = stamp()
    with refusing('work/ 封存不了，初始化没有完成（请检查 work/ 本身的标志和 ACL）'):
        entry = seal(root_fd, 'work', label)
    try:
        with refusing('版本库存不了第一个版本'):
            _first_version(layout, store, seed, report)
    except BaseException:
        if entry is not None:
            try:
                os.rename(entry, 'work', src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except OSError:
                pass                                # it stays sealed in the root; rsi recover keeps it under sealed/
        raise
    if entry is None:
        return
    problems = report['problems']

    def save(tree: Path) -> None:
        problems.extend(_add_seed_files(seed / 'work', tree))
        store.commit(tree, MANUAL, '初始化时在 work/ 里发现的内容（人工放的，或以前的进程留下的），缺的种子文件已补上')

    settle(layout, records, root_fd, entry, 'work', label, save, context, problems)


def _work(layout: Layout, records: Records, store: VersionStore, root_fd: int, context: dict,
          report: dict) -> Settings:
    """main's head staged and renamed in as work/ when work/ is missing (an existing one is left alone: the next run
    seals it); the settings of main's head either way, for the Lab home's config."""
    with refusing('版本库用不了'):
        staged, settings = stage(layout, store, store.head())
    if kind('work', root_fd) is not None:
        discard(staged)
        report['work'] = 'kept'
        return settings
    with refusing('准备好的工作区放不进 work/'):
        move_in(layout, records, root_fd, staged, 'work', context, report['problems'])
    report['work'] = 'staged'
    return settings


def _inside(path: str, folder: Path) -> bool:
    folder = os.path.realpath(folder)
    return any(form == folder or form.startswith(folder + os.sep) for form in (os.path.abspath(path),
                                                                              os.path.realpath(path)))


def _env(layout: Layout, root_fd: int, envelope: dict, report: dict) -> None:
    """env/ by `<python> -I -m venv` when the entry is missing; never read or walked otherwise."""
    if kind('env', root_fd) is not None:
        report['env'] = 'kept'
        return
    python, problems = envelope['rsi'].get('python', DEFAULT_PYTHON), report['problems']
    report['env'] = 'skipped'
    if not isinstance(python, str) or not os.path.isabs(python):
        problems.append(f'owner/{ENVELOPE} 的 rsi.python 应是解释器的绝对路径，现在是 {python!r}；没有建 env/')
        return
    if _inside(python, layout.root):
        problems.append(f'{python} 在 RSI 文件夹里，agent 改得到，不能用它建 env/')
        return
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        problems.append(f'没有找到 {python}，没有建 env/；装好它（或在 owner/{ENVELOPE} 的 rsi.python 写另一个解释器）'
                        '以后再运行 rsi init')
        return
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('PYTHON') and key != '__PYVENV_LAUNCHER__'}
    try:
        done = subprocess.run([python, '-I', '-m', 'venv', str(layout.env)], cwd='/', env=environment,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=VENV_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        problems.append(f'env/ 没能建好（{type(error).__name__}: {error}）；删掉 env/ 再运行 rsi init')
        report['env'] = 'failed'
        return
    if done.returncode != 0:
        detail = ' '.join(done.stderr.decode(errors='replace').split())[-300:]
        problems.append(f'env/ 没能建好（{python} -m venv 退出码 {done.returncode}：{detail}）；删掉 env/ 再运行 rsi init')
        report['env'] = 'failed'
        return
    report['env'] = 'created'


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def signed_in(home: Path, root: Path) -> bool | None:
    """Whether `codex login status` (the validated Codex of the RSI root `root`), run with CODEX_HOME=home, exits 0;
    None when it cannot be run or does not end. Only the exit code is used: its output is discarded and no file of
    the home is read here."""
    try:
        process = subprocess.Popen([codex_program(root), 'login', 'status'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                   env=codex_environment(home, secrets=False, root=root))
    except (OSError, Refused):
        return None
    try:
        code = process.wait(timeout=LOGIN_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    finally:
        _kill_group(process.pid)
        process.wait()
    return None if code is None else code == 0


def init(layout: Layout, seed: Path) -> dict:
    """Build or complete the RSI root at layout; what was made, and what was found, as a dict. Refused when the
    root's path is one Codex's sandbox cannot confine, or something of the owner's is not what it must be."""
    bad = sorted({char for char in str(layout.root) if char in UNSAFE_IN_ROOT})
    if bad:
        raise Refused(f'RSI 文件夹的路径 {str(layout.root)!r} 里有 {"".join(bad)!r}，Codex 的沙箱处理不了这样的路径；'
                      '请换一个路径')
    if layout.root.is_symlink() or not layout.root.is_dir():
        raise Refused(f'{layout.root} 不是文件夹')
    found = loaded_at_root(layout)
    if found:
        raise Refused('；'.join(found))
    report, context = _report(), {'command': 'init'}
    with root_descriptor(layout) as root_fd:
        _folders(layout, root_fd, report)
        _keep_owner_config(layout, report)
        original, source = _original(layout, report['problems'])
        _envelope(layout, report, original, source)
        _owner_seed(layout, seed, report)
        envelope = load_envelope(layout.owner)
        records = Records(layout.records)
        store = VersionStore(layout.versions)
        with refusing('版本库建不起来'):
            store.init()
            first = store.head() is None
        if first:
            _first_versions(layout, records, store, root_fd, seed, context, report)
        settings = _work(layout, records, store, root_fd, context, report)
        _env(layout, root_fd, envelope, report)
        write_home_config(layout, envelope, settings)
        report['version'] = store.head()
        if original is not None:
            report['original'] = {'path': f'codex-home/{source.name}',
                                  'dropped': sorted(set(original) - {'approval_policy', *IMPORTED})}
    report['signed_in'] = {home.name: signed_in(home, layout.root) for home in (layout.lab_home, layout.meta_home)}
    return report


# recover

def _moment(text) -> datetime | None:
    try:
        return datetime.fromisoformat(text).astimezone()
    except (TypeError, ValueError):
        return None


def _group_gone(pid: int) -> bool:
    """Whether no process of the group is left to signal (ESRCH; on macOS EPERM when only an exited leader is)."""
    try:
        os.killpg(pid, 0)
    except OSError:
        return True
    return False


def _stop_group(records: Records, call_id: str) -> tuple[bool, bool]:
    """Stop the process group of the call's Codex process, as runs/<call>/process.json records it (the pid leads its
    own group), when that group is alive and the process with that pid started when the record says (to within
    SAME_PROCESS_SECONDS): SIGTERM, up to STOP_GRACE seconds for it to go, then SIGKILL. (whether it was signalled,
    whether it needed the SIGKILL). A pid that may have been reused is left alone."""
    try:
        data = json.loads((records.call_dir(call_id) / PROCESS_FILE).read_bytes())
    except (OSError, ValueError, RecursionError):
        return False, False
    pid = data.get('pid') if isinstance(data, dict) else None
    started = _moment(data.get('started')) if isinstance(data, dict) else None
    if type(pid) is not int or pid <= 1 or pid in (os.getpid(), os.getpgrp()) or started is None or _group_gone(pid):
        return False, False
    began = _moment(process_start(pid))
    if began is None or abs((began - started).total_seconds()) > SAME_PROCESS_SECONDS:
        return False, False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False, False
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline:
        if _group_gone(pid):
            return True, False
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return True, True


def _move_aside(layout: Layout, records: Records, dir_fd: int, name: str, path: Path, relative: Path, workspace: str,
                label: str, lines: list[str]) -> None:
    """Move one leftover entry to sealed/<label>/<relative> without reading it, with a `sealed` note."""
    shown, where = f'{layout.sealed.name}/{label}/{relative.as_posix()}', path.relative_to(layout.root).as_posix()
    reasons = ['中断的运行留下的；rsi recover 把它原样移到这里，没有读过，也没有存成版本']
    try:
        keep(dir_fd, name, str(path), layout.sealed / label / relative)
    except OSError as error:
        reasons.append(f'没能移走（{error.strerror or error}），还在 {where}')
        records.note('sealed', {'workspace': workspace, 'path': where, 'reasons': reasons, 'command': 'recover'})
        lines.append(f'{where} 没能移到 {layout.sealed.name}/（{error.strerror or error}），还在原处')
        return
    records.note('sealed', {'workspace': workspace, 'path': shown, 'reasons': reasons, 'command': 'recover'})
    lines.append(f'{where} → {shown}')


def _leftovers(layout: Layout, records: Records) -> list[str]:
    """Move every entry of staging/ and every sealed entry of the root under sealed/recover-<stamp>/; then, if work/
    is missing, stage main's head into it. One line for each thing done."""
    lines, label = [], f'recover-{stamp()}'
    with root_descriptor(layout) as root_fd:
        if kind('staging', root_fd) == 'folder':
            fd = open_folder('staging', root_fd)
            try:
                for name in sorted(os.listdir(fd)):
                    _move_aside(layout, records, fd, name, layout.staging / name, Path('staging', name), 'staging',
                                label, lines)
            finally:
                os.close(fd)
        for name in sorted(os.listdir(root_fd)):
            if name.startswith(SEALED):
                workspace = name[len(SEALED):].split('-', 1)[0]
                _move_aside(layout, records, root_fd, name, layout.root / name, Path(name[1:]), workspace, label,
                            lines)
        store = VersionStore(layout.versions)
        if kind('work', root_fd) is None and (store.git_dir / 'HEAD').is_file():
            try:
                head = store.head()
            except WORKSPACE_ERRORS as error:
                lines.append(f'work/ 不在了，版本库也用不了（{type(error).__name__}: {error}）')
                return lines
            if head is not None:
                failed = restore_work(layout, records, store, root_fd, {'command': 'recover'})
                lines.append(f'work/ 不在了{failed}' if failed else f'work/ 不在了，已换成最新版本 {head}')
    return lines


def recover(layout: Layout, moved: list[str] | None = None) -> list[str]:
    """Mark every `started` call `interrupted` (usage unknown) and return their ids; never starts a call. Also moves
    what an interrupted run left under sealed/ (one line per thing done appended to moved, if given)."""
    records = Records(layout.records)
    changed = []
    for call in records.calls():
        if call.get('status') != STARTED:
            continue
        signalled, killed = _stop_group(records, call['id'])
        try:
            records.finish_call(call['id'], {'status': 'interrupted', 'ended': now(), 'usage': None})
        except (KeyError, ValueError):
            continue
        records.note('recovered', {'call': call['id'], 'signalled': signalled, 'killed': killed})
        changed.append(call['id'])
    lines = _leftovers(layout, records)
    if moved is not None:
        moved.extend(lines)
    return changed
