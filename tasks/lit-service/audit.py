"""Independently verify a completed lit-service task using its sealed record and shared original."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

DOI, PMC = "10.1093/bioinformatics/btp352", "PMC2723002"
MIB = 1024 * 1024
TOOLS = {"search_papers", "acquire_fulltext", "read_document", "get_citations"}
THREAD = re.compile(r"[0-9a-zA-Z_-]{1,70}")


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(root, relative, limit):
    """All components below the supplied RSI root are opened relative to verified directory descriptors."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    names = relative.split("/")
    require(all(name and name not in {".", ".."} for name in names), "Invalid evidence path")
    with ExitStack() as stack:
        parent = os.open(root, flags | os.O_DIRECTORY)
        stack.callback(os.close, parent)
        for name in names[:-1]:
            parent = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(names[-1], flags | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(fd)
            require(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_size <= limit,
                    "Unsafe or oversized evidence file: " + relative)
            data = stream.read(limit + 1)
            require(len(data) == meta.st_size, "Evidence changed while reading: " + relative)
            return data


def items(events):
    for event in events:
        payload = event.get("payload", {})
        if event.get("type") == "event_msg" and payload.get("type") == "item_completed":
            yield payload["item"]


def native_calls(events, reports, excluded):
    """Only Codex's typed MCP completions provide evidence. Exclude events inherited from the lead's history."""
    matched = []
    for item in items(events):
        if (item.get("type") != "McpToolCall" or item.get("server") != "literature"
                or item.get("id") in excluded or item.get("tool") not in TOOLS):
            continue
        reply = item.get("result") or {}
        if item.get("status") != "completed" or reply.get("isError"):
            continue
        for part in reply.get("content", []):
            if part.get("type") != "text":
                continue
            result = json.loads(part["text"])
            if result.get("status") not in {"ok", "partial"}:
                continue
            stamp = result.get("invocation_id")
            row = reports.get(stamp)
            require(row is not None and row["tool"] == item["tool"] and row["status"] == result["status"]
                    and row["inputs"] == item["arguments"], "Native MCP call does not match its logged invocation")
            matched.append((item["tool"], item["arguments"], result, row))
    return matched


def original(root, answer):
    doc, digest = answer["document_id"], answer["source_sha256"]
    require(re.fullmatch(r"doc_[0-9a-f]{64}", doc) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "Invalid document or content address")
    base = "library/documents/" + doc + "/"
    manifest = json.loads(read(root, base + "manifest.json", 16 * MIB))
    raw = read(root, "library/objects/sha256/" + digest, 4 * MIB)
    require(sha(raw) == digest == manifest["source_sha256"], "Original digest mismatch")
    require(manifest["media_type"].split(";")[0] in {"application/xml", "text/xml", "application/jats+xml"},
            "The archived original is not XML")
    meta = manifest["source_metadata"]
    origin = urlsplit(meta["source_url"])
    require(meta["provider"] == "europe_pmc" and origin.scheme == "https" and origin.hostname in
            {"www.ebi.ac.uk", "europepmc.org", "www.europepmc.org"} and PMC in origin.path.upper(),
            "The original was not obtained from the Europe PMC paper endpoint")
    require(b"<!ENTITY" not in raw.replace(b"\x00", b"").upper(), "Unsupported XML entity declaration")
    xml = ET.fromstring(raw)
    require(xml.tag.rsplit("}", 1)[-1] == "article", "The XML is not a JATS article")
    ids = {(node.get("pub-id-type"), (node.text or "").strip().lower())
           for node in xml.findall("./{*}front/{*}article-meta/{*}article-id")}
    require(("doi", DOI) in ids and ({("pmcid", PMC.lower()), ("pmc", PMC[3:]), ("pmcaid", PMC[3:])} & ids),
            "The archived JATS identifiers do not identify the target article")
    # Load the repository-controlled reference parser by exact file, never the copy in work/ or trial/.
    parser = Path(__file__).resolve().parents[2] / "control/seed/work/tools/literature/rsi_literature/documents.py"
    spec = importlib.util.spec_from_file_location("stage_l_reference_documents", parser)
    module = importlib.util.module_from_spec(spec)
    exec(compile(parser.read_bytes(), str(parser), "exec"), module.__dict__)
    with tempfile.TemporaryDirectory() as scratch:
        store = module.DocumentStore(scratch)
        replay = store.ingest(raw, manifest["media_type"], meta)
        require(replay == manifest and replay["document_id"] == doc
                and replay["document_version"] == answer["document_version"], "Reparsed document identity differs")
        expected_text = (Path(scratch) / doc / "text.txt").read_bytes()
        require(read(root, base + "text.txt", 16 * MIB) == expected_text, "Archived normalized text differs")
        blocks = [b for b in replay["blocks"] if b["locator"] == answer["locator"]]
        require(len(blocks) == 1 and blocks[0]["section_path"][:1] == ["Body"]
                and answer["quote"] in blocks[0]["text"], "The quote does not occur at this body locator")
        return blocks[0]


def verify_chain(calls, answer, block):
    doc, version, digest = (answer[k] for k in ("document_id", "document_version", "source_sha256"))
    aliases = {PMC.lower(), DOI, "19505943", "pmid:19505943", "doi:" + DOI, "pmcid:" + PMC.lower()}
    searched = acquired = successive = cited = False
    expected = {}
    reads = []
    used = []
    for tool, args, receipt, row in calls:
        data = receipt["data"]
        source = receipt["provenance"]["provider"]
        evidence = row["evidence"]
        if tool == "search_papers" and source == "europe_pmc":
            query = args.get("query", "").casefold()
            for paper in data.get("papers", []):
                if (str(paper.get("doi")).lower() == DOI and str(paper.get("pmcid")).upper() == PMC
                        and "samtools" in query and "sequence" in query
                        and any(e.get("paper_id") == paper["paper_id"] for e in evidence)):
                    aliases.update(str(paper.get(k, "")).lower() for k in ("paper_id", "provider_id", "pmid"))
                    searched = True
        elif tool == "acquire_fulltext" and searched and source == "europe_pmc":
            if (args.get("paper_ref", "").lower() in aliases and data.get("document_id") == doc
                    and data.get("document_version") == version and data.get("source_sha256") == digest
                    and any(e.get("source_sha256") == digest and e.get("document_id") == doc for e in evidence)):
                acquired = True
        elif tool == "read_document" and acquired and args.get("document_id") == doc and data.get("document_version") == version:
            span = data.get("char_range")
            if not isinstance(span, list) or len(span) != 2 or not 0 <= span[0] < span[1]:
                continue
            cursor = args.get("cursor")
            if cursor and expected.get(cursor) == span[0]:
                successive = True
            next_cursor = data.get("next_cursor")
            if next_cursor and row["next_cursor"] == next_cursor:
                expected[next_cursor] = span[1]
            for passage in data["passages"]:
                if passage["locator"] != answer["locator"] or passage.get("source_sha256") != digest:
                    continue
                start, end = passage["char_range"]
                offset = block["char_range"][0]
                if (offset <= start < end <= block["char_range"][1] and
                        passage["text"] == block["text"][start-offset:end-offset] and
                        any(e.get("source_sha256") == digest and e.get("locator") == passage["locator"]
                            and e.get("char_range") == [start, end] and e.get("document_version") == version
                            for e in evidence)):
                    reads.append((start-offset, end-offset))
        elif tool == "get_citations" and acquired and args.get("paper_ref", "").lower() in aliases:
            if (args.get("direction") == "references" and data.get("direction") == "references"
                    and any(p.get("title", "").strip().casefold() == answer["reference_title"].strip().casefold()
                            and any(e.get("paper_id") == p["paper_id"] for e in evidence) for p in data.get("papers", []))):
                cited = True
        used.append(receipt["invocation_id"])
    position = block["text"].find(answer["quote"])
    end = position
    for lo, hi in sorted(reads):
        if lo <= end < hi:
            end = hi
    checks = {"title_search": searched, "xml_acquired": acquired, "cursor_consumed": successive,
              "quoted_range_delivered": end >= position + len(answer["quote"]), "reference_returned": cited}
    return checks, used


def audit(record, library):
    record, library = Path(record).absolute(), Path(library).absolute()
    root = record.parents[2]
    require(re.fullmatch(r"R[0-9]{4,}", record.name) and record.parent == root / "records/runs"
            and library == root / "library", "Expected one RSI call directory and its sibling shared library")
    local = "records/runs/" + record.name + "/"
    call = json.loads(read(root, local + "call.json", MIB))
    require(call["id"] == record.name and call["status"] == "finished" and call["task"] == "lit-service"
            and call["purpose"] == "task" and call["check"]["passed"] is True,
            "The task or task-output check did not finish successfully")

    def attachment(name, maximum):
        raw = read(root, local + name, maximum)
        require(call["files"].get(name) == sha(raw), "Recorded attachment digest mismatch: " + name)
        return raw

    answer = json.loads(attachment("out/answer.json", 65536))
    require(answer["doi"].lower() == DOI and answer["pmcid"].upper() == PMC
            and all(x in answer["answer"].lower() for x in ("header", "alignment", "@"))
            and all(x in answer["quote"].lower() for x in ("header", "alignment", "@")), "Unsupported SAM format answer")
    block = original(root, answer)
    log = attachment("literature.jsonl", 64 * MIB)
    reports = {}
    for line in log.splitlines():
        require(len(line) <= 256 * 1024, "Oversized log row")
        row = json.loads(line)
        key = row["invocation_id"]
        require(re.fullmatch(r"[0-9a-f]{32}", key) and key not in reports, "Invalid or duplicate invocation ID")
        reports[key] = row
    def session(thread_id):
        require(THREAD.fullmatch(thread_id), "Invalid thread identifier")
        names = [k for k in call["files"] if re.fullmatch(r"sessions/[^/]+-" + re.escape(thread_id) + r"\.jsonl", k)]
        require(len(names) == 1, "Missing or ambiguous recorded session for " + thread_id)
        return [json.loads(line) for line in attachment(names[0], 64 * MIB).splitlines()]

    parent = call["thread_id"]
    lead = session(parent)
    spawned = {item["agent_thread_id"] for item in items(lead)
               if item.get("type") == "SubAgentActivity" and item.get("kind") == "started"}
    inherited = {item.get("id") for item in items(lead)}
    diagnostics = []
    require(len(call["subagent_threads"]) <= 64, "Too many child sessions")
    for child in call["subagent_threads"]:
        if child not in spawned:
            continue
        events = session(child)
        meta = next((e["payload"] for e in events if e["type"] == "session_meta" and e["payload"]["id"] == child), {})
        src = meta.get("source")
        spawn = src.get("subagent", {}).get("thread_spawn", {}) if isinstance(src, dict) else {}
        if spawn.get("parent_thread_id") != parent or spawn.get("agent_role") != "literature":
            continue
        checks, invoked = verify_chain(native_calls(events, reports, inherited), answer, block)
        diagnostics.append({"child": child, "checks": checks, "invocations": invoked})
        if all(checks.values()):
            return {"passed": True, "call": record.name, "version": call["version_before"], "child": child,
                    "doi": DOI, "document_id": answer["document_id"], "source_sha256": answer["source_sha256"],
                    "document_version": answer["document_version"], "locator": answer["locator"], "quote": answer["quote"],
                    "reference_title": answer["reference_title"], "literature_log_sha256": sha(log),
                    "checks": checks, "invocations": invoked}
    return {"passed": False, "call": record.name, "reason": "No delegated Literature child completed the evidence chain",
            "children": diagnostics}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("library", type=Path)
    args = parser.parse_args()
    try:
        report = audit(args.record, args.library)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, RecursionError, ET.ParseError) as error:
        report = {"passed": False, "reason": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["passed"] else 1)
