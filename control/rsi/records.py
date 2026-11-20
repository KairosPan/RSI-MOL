"""The append-only evidence of every run, kept in `records/`.

Only the control program writes here; the Meta agent reads it. Layout:

    runs/R0001/              one folder per Codex call: call.json plus whatever the call left (prompt, events, ...)
    rounds/M0001/            one folder per Meta round
    applications/A0001.json  one file per application, created by write_json
    <any>/<name>.json        written once by write_json
    index.jsonl              one line per finished call, written file and note, oldest first

No record file is ever overwritten. The one exception by design is a call's call.json, which is written exactly
twice: by start_call (status `started`) and by finish_call (an end status, plus the sha256 of every file the call
folder holds). Every other file is created once, completely or not at all, and refused if the name is taken.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path

KINDS = {'R': 'runs', 'M': 'rounds', 'A': 'applications'}
CALL_ID = re.compile(r'R[0-9]{4,}')
CALL_FILE = 'call.json'
INDEX = 'index.jsonl'
STARTED = 'started'
ENDED = ('finished', 'failed', 'timed_out', 'interrupted')
# One component of a path written by write_json: never empty, `.`, `..` or hidden.
COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
NOTE_KIND = re.compile(r'[a-z][a-z_]{0,31}')
CHUNK = 1 << 20


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _encode(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _sha_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _raise(error: OSError) -> None:
    raise error


def _hashes(folder: Path) -> tuple[dict, dict]:
    """sha256 of every regular file and link under folder except its own call.json, and the text of every link.

    A link is recorded as a link, by the sha256 of its target text, and never followed: a link to a folder is not
    entered. Folders themselves are not listed; anything that is neither a regular file nor a link is skipped.
    """
    files, links = {}, {}
    for top, folders, names in os.walk(folder, onerror=_raise):     # os.walk does not enter links to folders
        for name in folders + names:
            path = Path(top) / name
            relative = path.relative_to(folder).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                links[relative] = os.readlink(path)
                files[relative] = hashlib.sha256(os.fsencode(links[relative])).hexdigest()
            elif stat.S_ISREG(mode) and relative != CALL_FILE:
                files[relative] = _sha_of_file(path)
    return dict(sorted(files.items())), dict(sorted(links.items()))


class Records:
    def __init__(self, root: Path) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def new_id(self, prefix: str) -> str:
        """The next free id of a kind: 'R' call, 'M' round, 'A' application.

        Any entry named for the kind, with or without a suffix (`A0001.json`), counts as taken. 'R' and 'M' reserve
        the id by creating its folder exclusively, so two processes never get the same one. 'A' creates nothing: the
        application is the single file applications/<id>.json, and write_json, which never overwrites, claims it.
        """
        if prefix not in KINDS:
            raise ValueError(f'不认识的记录编号前缀：{prefix!r}。只能是 R、M 或 A。')
        folder = self.root / KINDS[prefix]
        pattern = re.compile(rf'{prefix}([0-9]{{4,}})(?:\..*)?')
        names = os.listdir(folder) if folder.is_dir() else []
        taken = [int(match.group(1)) for name in names if (match := pattern.fullmatch(name))]
        number = max(taken, default=0) + 1
        if prefix == 'A':
            return f'{prefix}{number:04d}'
        folder.mkdir(exist_ok=True)
        while True:
            name = f'{prefix}{number:04d}'
            try:
                os.mkdir(folder / name)
                return name
            except FileExistsError:
                number += 1

    def call_dir(self, call_id: str) -> Path:
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise ValueError(f'调用编号不合规：{call_id!r}。应为 R 加至少四位数字，例如 R0001。')
        return self.root / KINDS['R'] / call_id

    def start_call(self, call_id: str, fields: dict) -> None:
        """Write call.json with status `started`; FileExistsError if the call was started before."""
        folder = self.call_dir(call_id)
        self._same_id(call_id, fields)
        folder.mkdir(parents=True, exist_ok=True)
        record = {'id': call_id, **fields, 'status': STARTED}
        self._create(folder / CALL_FILE, _encode(record))

    def finish_call(self, call_id: str, fields: dict) -> dict:
        """Write the final call.json once and index it; return it.

        The final record is the started one updated with fields, plus `files` (relative path -> sha256 of every
        file the call folder holds) and `links` (relative path -> target text of every link in it). Its status must
        be an end status. KeyError if the call was never started, ValueError if it has already ended.
        """
        record = self.load_call(call_id)
        self._same_id(call_id, fields)
        if record.get('status') != STARTED:
            raise ValueError(f'调用 {call_id} 已经结束（{record.get("status")}），它的记录不能再改。')
        record.update(fields)
        if record.get('status') not in ENDED:
            raise ValueError(f'调用 {call_id} 的结束状态不合规：{record.get("status")!r}。应为 {"、".join(ENDED)} 之一。')
        folder = self.call_dir(call_id)
        record['files'], record['links'] = _hashes(folder)
        content = _encode(record)
        self._replace(folder / CALL_FILE, content)
        self._append({'kind': 'call', 'at': _now(), 'id': call_id, 'status': record['status'],
                      'path': f'{KINDS["R"]}/{call_id}/{CALL_FILE}', 'sha256': hashlib.sha256(content).hexdigest()})
        return record

    def load_call(self, call_id: str) -> dict:
        path = self.call_dir(call_id) / CALL_FILE
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            raise KeyError(call_id) from None
        return json.loads(text)

    def calls(self) -> list[dict]:
        """Every started call, oldest (lowest number) first, whatever its status; reserved ids are left out."""
        folder = self.root / KINDS['R']
        if not folder.is_dir():
            return []
        ids = sorted((name for name in os.listdir(folder) if CALL_ID.fullmatch(name)), key=lambda name: int(name[1:]))
        return [json.loads((folder / name / CALL_FILE).read_text(encoding='utf-8'))
                for name in ids if (folder / name / CALL_FILE).is_file()]

    def write_json(self, relative: str, data: dict) -> Path:
        """Create records/<relative> holding data; FileExistsError rather than overwrite. Indexed with its sha256."""
        parts = relative.split('/') if isinstance(relative, str) else []
        if not parts or not all(COMPONENT.fullmatch(part) for part in parts) or relative == INDEX \
                or (len(parts) == 3 and parts[0] == KINDS['R'] and parts[2] == CALL_FILE):
            raise ValueError(f'记录路径不合规：{relative!r}。')
        folder = self.root
        for part in parts[:-1]:
            folder = folder / part
            try:
                os.mkdir(folder)
            except FileExistsError:
                if folder.is_symlink() or not folder.is_dir():
                    raise ValueError(f'记录路径不合规：{relative!r}，其中 {part} 不是普通文件夹。') from None
        content = _encode(data)
        target = folder / parts[-1]
        self._create(target, content)
        self._append({'kind': 'file', 'at': _now(), 'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
        return target

    def note(self, kind: str, data: dict) -> None:
        """Append one index line {kind, at, **data}, e.g. kind 'refusal'."""
        if not isinstance(kind, str) or not NOTE_KIND.fullmatch(kind) or kind in ('call', 'file'):
            raise ValueError(f'记录种类不合规：{kind!r}。')
        if 'kind' in data or 'at' in data:
            raise ValueError('记录内容不能带 kind 或 at 键，它们由记录程序填写。')
        self._append({'kind': kind, 'at': _now(), **data})

    @staticmethod
    def _same_id(call_id: str, fields: dict) -> None:
        if fields.get('id', call_id) != call_id:
            raise ValueError(f'调用 {call_id} 的记录里写了另一个编号：{fields["id"]!r}。')

    @staticmethod
    def _staged(target: Path, content: bytes) -> str:
        """A complete, synced temporary copy of content next to target; the caller links or moves it into place."""
        fd, temporary = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(temporary)
            raise
        return temporary

    def _create(self, target: Path, content: bytes) -> None:
        """Create target with content, all at once; FileExistsError if any entry (a link too) has that name."""
        temporary = self._staged(target, content)
        try:
            os.link(temporary, target, follow_symlinks=False)
        finally:
            os.unlink(temporary)

    def _replace(self, target: Path, content: bytes) -> None:
        temporary = self._staged(target, content)
        try:
            os.replace(temporary, target)
        except BaseException:
            os.unlink(temporary)
            raise

    def _append(self, line: dict) -> None:
        content = (json.dumps(line, ensure_ascii=False) + '\n').encode('utf-8')
        fd = os.open(self.root / INDEX, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
