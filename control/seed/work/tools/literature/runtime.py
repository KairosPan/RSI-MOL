"""Run-local literature store, shared original archive, and the server's own call log.

The log is what this server reports about its calls. The Lab's commands can write the same folder, so a row is not
proof that a call happened; the originals in library/ are what a row can be checked against."""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from rsi_literature.documents import DocumentError, DocumentStore, MAX_SOURCE_BYTES

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DOCUMENT = re.compile(r"doc_[0-9a-f]{64}")
_MAX_INDEX = 128 * 1024 * 1024
_MAX_LOG = 256 * 1024
_PRIVATE = re.compile(r"authorization|credential|cookie|password|secret|token|api.?key|signature|headers|base64|"
                      r"image_data", re.I)
_SECRET_IN_STRING = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[^\s,;]+|"
    r"(?:[\w.-]*(?:token|password|api[-_]?key|signature|secret|credential)[\w.-]*)=[^&#\s]*)")
_URL_USER = re.compile(r"(https?://)[^/@\s]+@", re.I)


def _integrity(detail):
    return DocumentError("store_integrity", detail)


def _directory(path, create=False):
    if create:
        path.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise _integrity("Expected a regular directory: " + path.name)
    return path


def _read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise _integrity("Invalid or oversized artifact: " + path.name)
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise _integrity("Artifact grew beyond its limit: " + path.name)
        return raw


def _clean(value, depth=0):
    if depth > 8:
        return "[omitted]"
    if isinstance(value, dict):
        return {str(key)[:128]: _clean(item, depth + 1) for key, item in list(value.items())[:128]
                if not _PRIVATE.search(str(key))}
    if isinstance(value, list):
        return [_clean(item, depth + 1) for item in value[:128]]
    if isinstance(value, str):
        if value.lower().startswith("data:"):
            return "[omitted]"
        return _SECRET_IN_STRING.sub("[redacted]", _URL_USER.sub(r"\1", value))[:8192]
    return value if isinstance(value, (int, bool, float)) or value is None else "[omitted]"


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


_EVIDENCE_KEYS = ("document_id", "document_version", "source_sha256", "source_document_id", "original_sha256",
                  "source_document_version", "paper_id", "paper_ref", "doi", "pmid", "pmcid",
                  "locator", "char_range", "byte_range", "page", "image_sha256", "direction")


def _evidence(result):
    data = result.get("data", {})
    if not isinstance(data, dict):
        return [], False
    candidates = [result.get("provenance", {}), data, data.get("paper", {}), data.get("source_metadata", {})]
    for key in ("passages", "papers", "citations", "searched_documents"):
        values = data.get(key)
        if isinstance(values, list):
            candidates.extend(values)
    image = data.get("image")
    if isinstance(image, dict):
        candidates.append({**image, "image_sha256": image.get("sha256")})
    rows = []
    size = 0
    for item in candidates:
        if not isinstance(item, dict):
            continue
        row = {key: _clean(item[key]) for key in _EVIDENCE_KEYS if item.get(key) is not None}
        location = item.get("source_locator")
        if isinstance(location, dict) and type(location.get("page")) is int:
            row["page"] = location["page"]
        if row:
            size += len(_json(row))
            if size > _MAX_LOG // 2:
                return rows, True
            rows.append(row)
    return rows, False


class Runtime:
    def __init__(self, workspace, *, local_sources=None):
        self.workspace = Path(workspace).resolve()
        if self.workspace.name not in {"work", "trial"}:
            raise ValueError("Literature must start in the active work or trial workspace")
        self.trial = self.workspace.name == "trial"
        self.library = self.workspace.parent / "library"
        temporary = _directory(self.workspace / ".tmp", create=True)
        self.root = _directory(temporary / "literature", create=True)
        self.calls = _directory(self.root / "calls", create=True)
        self.logfile = self.calls / (uuid.uuid4().hex + ".jsonl")
        self._local_sources = local_sources
        with self._lock():
            self.store = DocumentStore(_directory(self.root / "store", create=True))

    @contextmanager
    def _lock(self):
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _integrity("Invalid literature store lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _paths(self, *, create):
        if create and self.trial:
            raise DocumentError("trial_cache_miss", "A trial can only read shared originals")
        # Trials, including metadata-only runs with an empty library, create nothing here.
        if not create and not (self.library / "documents").exists():
            raise DocumentError("unknown_document", "The shared archive does not contain this document")
        library = _directory(self.library, create)
        objects = _directory(_directory(library / "objects", create) / "sha256", create)
        return objects, _directory(library / "documents", create)

    def _archive(self, manifest):
        document_id = manifest["document_id"]
        digest = manifest["source_sha256"]
        objects, documents = self._paths(create=True)
        current = self.store.root / document_id
        target = objects / digest
        raw = _read(current / "source.bin", MAX_SOURCE_BYTES)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise _integrity("Original changed before publication")
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=objects, prefix=".object-") as tmp:
                tmp.write(raw)
                tmp.flush()
                # A completed immutable original becomes visible in one filesystem operation.
                try:
                    os.link(tmp.name, target)
                except FileExistsError:
                    pass
        if _read(target, MAX_SOURCE_BYTES) != raw:
            raise _integrity("Archived original differs from its digest")
        files = {name: _read(current / name, _MAX_INDEX) for name in ("manifest.json", "text.txt")}
        destination = documents / document_id
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=documents, prefix=".publish-") as staging:
                entry = Path(staging) / document_id
                entry.mkdir()
                for name, value in files.items():
                    (entry / name).write_bytes(value)
                entry.rename(destination)
        _directory(destination)
        if any(_read(destination / name, _MAX_INDEX) != value for name, value in files.items()):
            raise _integrity("Archived document differs from the verified workspace copy")
        return manifest

    def ingest(self, raw, media_type, metadata):
        """Acquire a new original, or match a previously archived one in a trial."""
        with self._lock():
            if not self.trial:
                return self._archive(self.store.ingest(raw, media_type, metadata))
            # Resolve the metadata-dependent identity without registering a trial cache miss in the reader.
            with tempfile.TemporaryDirectory(dir=self.root) as scratch:
                proposed = DocumentStore(scratch).ingest(raw, media_type, metadata)
            try:
                cached = self._hydrate(proposed["document_id"])
            except DocumentError as exc:
                if exc.code != "unknown_document":
                    raise
                raise DocumentError("trial_cache_miss", "This original is not archived; acquire it in a task") from exc
            if cached != proposed:
                raise _integrity("Archived document differs from the acquired original")
            return cached

    def _hydrate(self, document_id):
        objects, documents = self._paths(create=False)
        source = documents / document_id
        if not source.exists():
            raise DocumentError("unknown_document", "The shared archive does not contain this document")
        _directory(source)
        serialized = _read(source / "manifest.json", _MAX_INDEX)
        try:
            digest = json.loads(serialized)["source_sha256"]
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError
        except (ValueError, TypeError, KeyError) as exc:
            raise _integrity("Invalid archived source digest") from exc
        destination = self.store.root / document_id
        with tempfile.TemporaryDirectory(dir=self.store.root, prefix=".hydrate-") as staging:
            verifier = DocumentStore(staging)
            entry = (Path(staging) / document_id)
            entry.mkdir()
            (entry / "manifest.json").write_bytes(serialized)
            (entry / "source.bin").write_bytes(_read(objects / digest, MAX_SOURCE_BYTES))
            (entry / "text.txt").write_bytes(_read(source / "text.txt", _MAX_INDEX))
            archived = verifier.manifest(document_id)
            if destination.exists():
                if archived != self.store.manifest(document_id):
                    raise _integrity("Cached and archived document versions differ")
            else:
                entry.rename(destination)
        return archived

    def ensure(self, document_id):
        """Open a run-local document or hydrate a verified shared copy and its parent PDF."""
        if not isinstance(document_id, str) or not _DOCUMENT.fullmatch(document_id):
            raise DocumentError("invalid_document_id", "Supply a registered document identifier")
        with self._lock():
            def open_one(doc):
                if (self.store.root / doc).exists():
                    return self.store.manifest(doc)
                return self._hydrate(doc)
            manifest = open_one(document_id)
            parent = manifest["source_metadata"].get("derived_from")
            if parent:
                if not isinstance(parent, dict) or not _DOCUMENT.fullmatch(str(parent.get("document_id"))):
                    raise _integrity("Invalid derived document parent")
                original = open_one(parent["document_id"])
                if any(parent.get(k) != original[k] for k in ("source_sha256", "document_version")):
                    raise _integrity("Derived document no longer matches its original")
            return manifest

    def publish_derived(self, document_id):
        manifest = self.ensure(document_id)
        if not manifest["source_metadata"].get("derived_from"):
            raise _integrity("A derivative must identify its original")
        with self._lock():
            return manifest if self.trial else self._archive(manifest)

    def local_source(self, source_id):
        sources = self._local_sources
        if sources is None:
            registry = self.workspace / "tools" / "literature" / "sources.json"
            sources = json.loads(_read(registry, 64 * 1024)) if registry.exists() else {}
        if not isinstance(source_id, str) or not isinstance(sources, dict) or source_id not in sources:
            raise DocumentError("unknown_source", "The source ID is not registered")
        spec = sources[source_id]
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise _integrity("Invalid registered source")
        media_type = spec.get("media_type")
        if not isinstance(media_type, str) or not media_type:
            raise _integrity("A registered source needs a media_type")
        relative = Path(spec["path"])
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise _integrity("Registered source paths must be workspace-relative")
        folder = self.workspace
        for name in relative.parts[:-1]:
            folder = _directory(folder / name)
        raw = _read(folder / relative.name, MAX_SOURCE_BYTES)
        if hashlib.sha256(raw).hexdigest() != spec.get("sha256"):
            raise _integrity("Registered source content has changed")
        metadata = {k: spec.get(k) for k in ("source_url", "paper_ref", "license", "content_scope") if k in spec}
        return raw, media_type, {**metadata, "provider": "local", "source_id": source_id}

    def log(self, call_id, operation, inputs, result):
        provenance = result.get("provenance") or {}
        evidence, truncated = _evidence(result)
        provider = provenance.get("provider")
        attempts = provenance.get("attempt_count", 0)
        requested = inputs.get("provider") if isinstance(inputs, dict) else None
        hosts = set(provenance.get("download_hosts", []))
        for request in provenance.get("requests", []):
            host = urlsplit(request.get("url", "")).hostname
            if host:
                hosts.add(host)
        errors = [{"code": _clean(str(error.get("code", "")))[:128],
                   "detail": _clean(str(error.get("detail", "")))[:512]} for error in result.get("errors", [])[:32]]
        row = {"at": datetime.now(timezone.utc).isoformat(), "invocation_id": call_id, "tool": _clean(operation)[:128],
               "inputs": _clean(inputs),
               "requested_source": _clean(requested)[:128] if isinstance(requested, str) else provider,
               "contacted_source": provider if attempts or provider in {"local", "poppler"} else None,
               "contacted_hosts": sorted(hosts)[:32], "status": result["status"], "errors": errors,
               "evidence": evidence, "next_cursor": _clean((result.get("continuation") or {}).get("next_cursor"))}
        if truncated:
            row["evidence_truncated"] = True
        if downloads := provenance.get("download_attempts"):
            row["download_attempts"] = _clean(downloads)
            if len(downloads) > len(row["download_attempts"]) or len(_json(row["download_attempts"])) > _MAX_LOG // 4:
                row["download_attempts"] = _clean([downloads[0], downloads[-1]])
                row["download_attempts_total"] = len(downloads)
        if len(_json(row)) >= _MAX_LOG:
            row["inputs"] = {"omitted": "Inputs exceed the log row limit",
                             "sha256": hashlib.sha256(_json(row["inputs"])).hexdigest()}
        data = _json(row) + b"\n"
        with open(self.logfile, "ab", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)) as out:
            out.write(data)
