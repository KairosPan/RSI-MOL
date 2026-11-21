"""Read the native completions in a finished Lab call and the originals those completions name."""
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import tomllib
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from rsi.memory_store import DIGEST, digest
from rsi.paths import SAFE_ID
from rsi.versions import VersionStore
from rsi.workspace import open_folder

TOOLS = {'read_document', 'find_passages', 'acquire_fulltext', 'get_paper', 'search_papers', 'get_citations'}
INVOCATION = re.compile(r'[0-9a-f]{32}')
SESSION_LIMIT = 256 << 20
CALL_BYTES = 2 << 20
SOURCE_LIMIT = 32 << 20
CALLS_LIMIT = 2048


def deadline_check(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('Memory registration exceeded its time limit')


def original_problem(layout, address):
    """Hash one single-link regular object, opening every component relative to the owner's root fd."""
    if not isinstance(address, str) or not DIGEST.fullmatch(address):
        return 'invalid original SHA-256'
    try:
        with ExitStack() as stack:
            folder = open_folder(layout.root)
            stack.callback(os.close, folder)
            for name in ('library', 'objects', 'sha256'):
                folder = open_folder(name, folder)
                stack.callback(os.close, folder)
            before = os.stat(address, dir_fd=folder, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > SOURCE_LIMIT:
                return 'original is not a bounded single-link regular file'
            fd = os.open(address, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(fd)
                if not os.path.samestat(before, info) or info.st_nlink != 1 or info.st_size != before.st_size:
                    return 'original changed during verification'
                value = hashlib.sha256()
                remain = info.st_size
                while remain:
                    chunk = stream.read(min(1 << 20, remain))
                    if not chunk:
                        return 'original changed during verification'
                    value.update(chunk)
                    remain -= len(chunk)
                if stream.read(1) or value.hexdigest() != address:
                    return 'original SHA-256 mismatch'
    except FileNotFoundError:
        return 'original missing'
    except OSError:
        return 'original could not be safely opened'
    return None


def _effective_server(call):
    command = call.get('command') or []
    effective = {}
    for idx, arg in enumerate(command):
        if idx and command[idx - 1] == '-c' and isinstance(arg, str) and arg.startswith('mcp_servers='):
            effective = tomllib.loads(arg)['mcp_servers'].get('literature', {})
    return effective


def service_source(layout, call):
    """Describe the selected entry without recording environment values, bearer tokens or URL queries."""
    unknown = {'origin': 'unknown', 'changed_during_run': None}
    try:
        server = _effective_server(call)
        if not server:
            return unknown
        if 'url' in server:
            return {**unknown, 'origin': 'custom', 'transport': 'url', 'host': urlsplit(server['url']).hostname}
        args = server.get('args') or []
        if 'command' not in server or not args:
            return unknown
        command = call.get('command') or []
        workspace = layout.work
        if '-o' in command:
            output = Path(command[command.index('-o') + 1])
            if output.parts[-4:] == ('records', 'runs', call['id'], 'last-message.txt'):
                workspace = output.parents[3] / 'work'  # the recorded installation can since have been moved
        custom = {**unknown, 'origin': 'custom', 'transport': 'stdio', 'entry_sha256': digest([server['command'], args])}
        if Path(server['command']).name != 'codex' or not args or args[0] != 'sandbox' or '--' not in args:
            return custom
        inner = args[args.index('--') + 1:]
        if not inner or inner[0] != '/usr/bin/env':
            return custom
        end_env = 1
        while end_env < len(inner) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', inner[end_env]):
            end_env += 1
        if inner[end_env:] != ['python3', '-B', str(workspace / 'tools/literature/server.py')]:
            return custom
        store = VersionStore(layout.versions)
        before = store.resolve(call['version_before'])
        if call.get('commit', before) != before:
            return unknown
        if not store._out('ls-tree', before, 'tools/literature/server.py').startswith('100'):
            return unknown
        stamp = store._out('log', '-1', '--format=%H%n%an%n%s', before, '--', 'tools/literature')
        commit, author, message = stamp.split('\n', 2)
        tags = store._out('tag', '--points-at', commit).splitlines()
        version = next((s for s in tags if re.fullmatch(r'V\d+', s)), None)
        if author == 'owner' and message == 'Install the Literature tool server':
            origin = 'installed'
        elif author == 'init':
            origin = 'initial'
        elif author.startswith('lab '):
            origin = 'lab'
        elif author.startswith('meta '):
            origin = 'meta'
        else:
            origin = 'manual_or_legacy'
        changed = None
        if not call.get('commit_failed', False):
            after = store.resolve(call['version_after']) if call.get('version_after') else before
            changed = bool(store._out('diff', '--name-only', before, after, '--', 'tools/literature', 'codex/settings.toml'))
        return {'origin': origin, 'transport': 'stdio', 'entry': 'tools/literature/server.py',
                'version': version, 'commit': commit, 'changed_during_run': changed}
    except (ValueError, UnicodeError, IndexError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        return unknown


def session_paths(call):
    threads = [call.get('thread_id'), *(call.get('subagent_threads') or [])]
    valid = {t for t in threads if isinstance(t, str) and SAFE_ID.fullmatch(t)}
    paths = []
    for rel, checksum in (call.get('files') or {}).items():
        pieces = rel.split('/')
        if len(pieces) != 2 or pieces[0] != 'sessions' or not pieces[1].endswith('.jsonl') or rel in (call.get('links') or {}):
            continue
        thread = next((t for t in valid if pieces[1] == t + '.jsonl' or pieces[1].endswith('-' + t + '.jsonl')), None)
        if thread:
            paths.append((rel, thread, checksum))
    # collect_sessions walks the producing threads breadth first. This also attributes older
    # events without a thread_id to the parent before encountering a child's inherited copy.
    order = {t: n for n, t in enumerate(threads) if isinstance(t, str)}
    return sorted(paths, key=lambda entry: (order[entry[1]], entry[0]))


def _parts(event, thread):
    payload = event.get('payload')
    if event.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'item_completed':
        return None
    # The native event identifies its producing thread even when a child inherited the entire event.
    if payload.get('thread_id') not in (None, thread):
        return None
    item = payload.get('item')
    if isinstance(item, dict) and item.get('type') == 'McpToolCall':
        return item
    return None


def _receipt(item):
    reply = item.get('result') or {}
    parts = [p.get('text') for p in reply.get('content', []) if isinstance(p, dict) and p.get('type') == 'text']
    if len(parts) != 1 or not isinstance(parts[0], str) or len(parts[0].encode()) > CALL_BYTES:
        raise ValueError('invalid or oversized tool response')
    rec = json.loads(parts[0])
    if not isinstance(rec, dict) or not INVOCATION.fullmatch(str(rec.get('invocation_id', ''))):
        raise ValueError('invalid invocation_id')
    status = rec.get('status')
    good = status in ('ok', 'partial') and item.get('status') == 'completed' and not reply.get('isError')
    failure = (item['tool'] == 'acquire_fulltext' and status in ('failed', 'unavailable') and
               item.get('status') in ('completed', 'failed') and reply.get('isError') is True)
    if not (good or failure):
        raise ValueError('tool or native completion status does not qualify')
    if not isinstance(rec.get('data'), dict) or not isinstance(item.get('arguments'), dict):
        raise ValueError('invalid data or arguments')
    return rec


def native_calls(layout, call, reject, deadline, pending_proposals=None):
    """Verify archived attachment hashes, then return each original call once in chronological order."""
    calls, fingerprints, conflicts = {}, {}, set()
    consumed = 0
    folder = layout.records / 'runs' / call['id']
    for rel, thread, expected in session_paths(call):
        role = 'lead' if thread == call.get('thread_id') else 'subagent'
        src = folder / rel
        if src.stat().st_size + consumed > SESSION_LIMIT:
            reject('session byte limit', thread=thread)
            continue
        hasher = hashlib.sha256()
        with src.open('rb') as lines:
            while raw := lines.readline(CALL_BYTES * 2 + 1):
                deadline_check(deadline)
                hasher.update(raw)
                consumed += len(raw)
                if len(raw) > CALL_BYTES * 2:
                    while raw and not raw.endswith(b'\n'):
                        deadline_check(deadline)
                        raw = lines.readline(CALL_BYTES)
                        consumed += len(raw)
                        hasher.update(raw)
                    reject('oversized session event', thread=thread)
                    continue
                try:
                    ev = json.loads(raw)
                    if not isinstance(ev, dict):
                        continue
                    meta = ev.get('payload') if ev.get('type') == 'session_meta' else None
                    if isinstance(meta, dict) and meta.get('id') == thread and isinstance(meta.get('source'), dict):
                        spawn = meta['source'].get('subagent', {}).get('thread_spawn', {})
                        role = spawn.get('agent_role') or role
                    item = _parts(ev, thread)
                    if not item:
                        continue
                    if item.get('server') == 'memory' and item.get('tool') == 'propose_memory':
                        if pending_proposals is not None and item.get('status') == 'completed':
                            reply = item.get('result') or {}
                            parts = reply.get('content') or []
                            if not reply.get('isError') and len(parts) == 1 and parts[0].get('type') == 'text':
                                value = json.loads(parts[0]['text'])
                                pid = value.get('proposal_id')
                                if (value.get('status') == 'pending' and isinstance(pid, str) and INVOCATION.fullmatch(pid)
                                        and len(pending_proposals) < CALLS_LIMIT):
                                    pending_proposals.add(pid)
                        continue
                    if item.get('server') != 'literature' or item.get('tool') not in TOOLS:
                        continue
                    rec = _receipt(item)
                    key = rec['invocation_id']
                    fp = digest([item['tool'], item['arguments'], rec])
                    if key in fingerprints:
                        if fp != fingerprints[key] and key not in conflicts:
                            reject('conflicting invocation_id', invocation_id=key)
                            conflicts.add(key)
                        continue
                    if len(calls) >= CALLS_LIMIT:
                        reject('invocation limit', thread=thread)
                        continue
                    fingerprints[key] = fp
                    calls[key] = (ev.get('timestamp', ''), thread, role, item['tool'], item['arguments'], rec,
                                  item.get('id'))
                except (ValueError, RecursionError, AttributeError, TypeError, KeyError) as exc:
                    reject(str(exc)[:180] if type(exc) is ValueError else 'invalid session event', thread=thread)
        if hasher.hexdigest() != expected:
            raise ValueError(f'Archived session checksum mismatch for {call["id"]}/{rel}')
    return sorted((c for key, c in calls.items() if key not in conflicts), key=lambda c: (c[0], c[5]['invocation_id']))
