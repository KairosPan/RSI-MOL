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
import hashlib
import json
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from rsi import fs as os
from rsi.paths import is_link
from rsi.publication import publish_new

KINDS = {'R': 'runs', 'M': 'rounds', 'A': 'applications'}
CALL_ID = re.compile(r'R[0-9]{4,}')
CALL_FILE = 'call.json'
INDEX = 'index.jsonl'
STARTED = 'started'
ENDED = ('finished', 'failed', 'timed_out', 'interrupted')
UNREADABLE = 'unreadable'      # status of the stand-in calls() returns for a call.json it cannot read
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
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or (os.name == 'nt' and
                                              info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                if name in folders:
                    folders.remove(name)
                links[relative] = os.readlink(path)
                files[relative] = hashlib.sha256(os.fsencode(links[relative])).hexdigest()
            elif stat.S_ISREG(info.st_mode) and relative != CALL_FILE:
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
            raise ValueError(f'Unknown record id prefix: {prefix!r}. It can only be R, M or A.')
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
            raise ValueError(f'The call id {call_id!r} is not valid. A call id is R and at least four digits, for '
                             'example R0001.')
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
            raise ValueError(f'The call {call_id} has already ended ({record.get("status")}); its record cannot be '
                             'changed any more.')
        record.update(fields)
        if record.get('status') not in ENDED:
            raise ValueError(f'The end status of the call {call_id} is not valid: {record.get("status")!r}. It must '
                             f'be one of {", ".join(ENDED)}.')
        folder = self.call_dir(call_id)
        record['files'], record['links'] = _hashes(folder)
        content = _encode(record)
        self._replace(folder / CALL_FILE, content)
        self._append({'kind': 'call', 'at': _now(), 'id': call_id, 'status': record['status'],
                      'path': f'{KINDS["R"]}/{call_id}/{CALL_FILE}', 'sha256': hashlib.sha256(content).hexdigest()})
        return record

    def load_call(self, call_id: str) -> dict:
        """The call's call.json. KeyError if the call was never started; ValueError, in English and naming the
        file, if it cannot be read or is not a JSON object."""
        path = self.call_dir(call_id) / CALL_FILE
        try:
            record = json.loads(path.read_bytes().decode('utf-8'))
        except FileNotFoundError:
            raise KeyError(call_id) from None
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ValueError(f'The call record {path} cannot be read: {error}') from error
        if not isinstance(record, dict):
            raise ValueError(f'The call record {path} is not a JSON object but {json.dumps(record)[:80]}.')
        return record

    def calls(self) -> list[dict]:
        """Every started call, oldest (lowest number) first, whatever its status; reserved ids are left out.

        A call.json that cannot be read stands in the list as {'id', 'status': 'unreadable', 'problem'}, the problem
        being load_call's English message, so one bad record never hides the others.
        """
        folder = self.root / KINDS['R']
        if not folder.is_dir():
            return []
        ids = sorted((name for name in os.listdir(folder) if CALL_ID.fullmatch(name)), key=lambda name: int(name[1:]))
        found = []
        for call_id in ids:
            try:
                found.append(self.load_call(call_id))
            except KeyError:
                continue                                    # reserved by new_id, not started yet
            except ValueError as error:
                found.append({'id': call_id, 'status': UNREADABLE, 'problem': str(error)})
        return found

    def write_json(self, relative: str, data: dict) -> Path:
        """Create records/<relative> holding data; FileExistsError rather than overwrite. Indexed with its sha256."""
        parts = relative.split('/') if isinstance(relative, str) else []
        canonical = [os.path.normcase(part) for part in parts]
        if (not parts or not all(COMPONENT.fullmatch(part) for part in parts) or canonical == [INDEX]
                or (len(canonical) == 3 and canonical[0] == KINDS['R'] and canonical[2] == CALL_FILE)
                or (os.name == 'nt' and any(Path(part).is_reserved() or part.endswith('.') for part in parts))):
            raise ValueError(f'The record path {relative!r} is not valid.')
        folder = self.root
        for part in parts[:-1]:
            folder = folder / part
            try:
                os.mkdir(folder)
            except FileExistsError:
                if is_link(folder) or not folder.is_dir():
                    raise ValueError(f'The record path {relative!r} is not valid: {part} in it is not a plain '
                                     'folder.') from None
        content = _encode(data)
        target = folder / parts[-1]
        self._create(target, content)
        self._append({'kind': 'file', 'at': _now(), 'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
        return target

    def note(self, kind: str, data: dict) -> None:
        """Append one index line {kind, at, **data}, e.g. kind 'refusal'."""
        if not isinstance(kind, str) or not NOTE_KIND.fullmatch(kind) or kind in ('call', 'file'):
            raise ValueError(f'The record kind {kind!r} is not valid.')
        if 'kind' in data or 'at' in data:
            raise ValueError('A record cannot have the keys kind or at; the records program fills them in.')
        self._append({'kind': kind, 'at': _now(), **data})

    @staticmethod
    def _same_id(call_id: str, fields: dict) -> None:
        if fields.get('id', call_id) != call_id:
            raise ValueError(f'The record of the call {call_id} names another id: {fields["id"]!r}.')

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
            publish_new(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

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
            os.lock_exclusive(fd)
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
