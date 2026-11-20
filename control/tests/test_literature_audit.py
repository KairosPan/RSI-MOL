"""Exercise native session attribution, independently replayed source evidence and file boundaries."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.test_literature_runtime import Runtime

AUDIT_PATH = Path(__file__).resolve().parents[2] / "tasks/lit-service/audit.py"
spec = importlib.util.spec_from_file_location("lit_service_audit", AUDIT_PATH)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

PASSAGE = "There are header lines beginning with @ and alignment lines without that character."
TITLE = "A verified reference title"
XML = (f'<article><front><article-meta><article-id pub-id-type="doi">{audit.DOI}</article-id>'
       f'<article-id pub-id-type="pmcid">{audit.PMC}</article-id></article-meta></front>'
       '<body><sec><title>Format</title><p>First passage for pagination.</p>'
       f'<p>{PASSAGE}</p></sec></body></article>').encode()


XML_QUOTE_IN_ABSTRACT = (f'<article><front><article-meta><article-id pub-id-type="doi">{audit.DOI}</article-id>'
                         f'<article-id pub-id-type="pmcid">{audit.PMC}</article-id></article-meta>'
                         f'<abstract><p>{PASSAGE}</p></abstract></front>'
                         '<body><sec><title>Format</title><p>First passage for pagination.</p></sec></body></article>').encode()


class Fixture(unittest.TestCase):
    xml = XML
    task = "lit-service"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        work = self.root / "work"
        work.mkdir()
        self.runtime = Runtime(work)
        self.record = self.root / "records/runs/R0007"
        self.record.mkdir(parents=True)
        (self.record / "out").mkdir()
        (self.record / "sessions").mkdir()
        self.library = self.root / "library"
        self.meta = {"provider": "europe_pmc", "source_url":
                     "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC2723002/fullTextXML"}
        self.manifest = self.runtime.ingest(self.xml, "application/xml", self.meta)
        self.block = next(b for b in self.manifest["blocks"] if b["text"] == PASSAGE)
        self.doc = self.manifest["document_id"]
        self.child = "child-thread"
        self.lead = "lead-thread"
        self.events = []
        self.answer = {"answer": "Only header lines begin with @; alignment lines do not.",
                       "quote": PASSAGE, "locator": self.block["locator"], "coverage": "Read body passage",
                       "reference_title": TITLE, "doi": audit.DOI, "pmcid": audit.PMC,
                       **{k: self.manifest[k] for k in ("document_id", "document_version", "source_sha256")}}
        self.call("search_papers", {"query": "The Sequence Alignment/Map format and SAMtools"},
                  {"papers": [{"paper_id": "MED:19505943", "doi": audit.DOI, "pmcid": audit.PMC}]})
        self.call("acquire_fulltext", {"paper_ref": audit.DOI}, self.runtime.store.reading_package(self.doc))
        first = self.runtime.store.read(self.doc, max_bytes=10)
        self.call("read_document", {"document_id": self.doc, "max_bytes": 10}, first)
        self.call("read_document", {"document_id": self.doc, "cursor": first["next_cursor"]},
                  self.runtime.store.read(self.doc, cursor=first["next_cursor"]))
        self.call("get_citations", {"paper_ref": audit.DOI, "direction": "references"},
                  {"direction": "references", "papers": [{"paper_id": "MED:1", "title": TITLE}]})
        self.parent_events = [self.item({"type": "SubAgentActivity", "id": "spawn-id", "kind": "started",
                                         "agent_thread_id": self.child})]
        self.child_meta = {"type": "session_meta", "payload": {"id": self.child, "source": {"subagent":
                           {"thread_spawn": {"parent_thread_id": self.lead, "agent_role": "literature"}}}}}
        self.save()

    @staticmethod
    def item(data):
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": data}}

    def call(self, name, args, data):
        invocation = uuid.uuid4().hex
        source = "local" if name == "read_document" else "europe_pmc"
        result = {"status": "ok", "data": data, "provenance": {"provider": source, "attempt_count": 1},
                  "continuation": {"next_cursor": data.get("next_cursor")}, "errors": [], "invocation_id": invocation}
        self.runtime.log(invocation, name, args, result)
        self.events.append(self.item({"id": "exec-" + invocation, "type": "McpToolCall", "status": "completed",
                                      "server": "literature", "tool": name, "arguments": args,
                                      "result": {"content": [{"type": "text", "text": json.dumps(result)}],
                                                 "isError": False}}))

    def outputs(self):
        return {"out/answer.json": json.dumps(self.answer).encode()}

    def save(self):
        files = {**self.outputs(), "literature.jsonl": self.runtime.logfile.read_bytes()}
        for name, ev in ((self.lead, self.parent_events), (self.child, [self.child_meta, *self.events])):
            files[f"sessions/rollout-dummy-{name}.jsonl"] = b"".join(json.dumps(row).encode() + b"\n" for row in ev)
        hashes = {}
        for name, raw in files.items():
            (self.record / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        call = {"id": self.record.name, "version_before": "V0001", "status": "finished", "purpose": "task",
                "task": self.task, "check": {"passed": True}, "files": hashes,
                "subagent_threads": [self.child], "thread_id": self.lead}
        (self.record / "call.json").write_text(json.dumps(call))

    def edit_log(self, tool, change):
        """Rewrite the log rows of one tool, then reseal the record so only the content differs."""
        rows = [json.loads(line) for line in self.runtime.logfile.read_text().splitlines()]
        for row in rows:
            if row["tool"] == tool:
                change(row)
        self.runtime.logfile.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.save()

    def edit_receipt(self, tool, change):
        for event in self.events:
            item = event["payload"]["item"]
            if item["tool"] == tool:
                body = json.loads(item["result"]["content"][0]["text"])
                change(body)
                item["result"]["content"][0]["text"] = json.dumps(body)
        self.save()

    def checks(self):
        verdict = audit.audit(self.record, self.library)
        return verdict["children"][0]["checks"] if not verdict["passed"] else verdict["checks"]


class Audit(Fixture):
    def test_full_chain_and_filtered_evidence(self):
        result = audit.audit(self.record, self.library)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["source_sha256"], hashlib.sha256(XML).hexdigest())
        self.assertEqual(len(result["invocations"]), 5)

    def test_inherited_or_wrong_role_calls_do_not_count(self):
        self.parent_events.extend(self.events)
        self.save()
        self.assertFalse(audit.audit(self.record, self.library)["passed"])
        self.parent_events = self.parent_events[:1]
        self.child_meta["payload"]["source"]["subagent"]["thread_spawn"]["agent_role"] = "worker"
        self.save()
        self.assertFalse(audit.audit(self.record, self.library)["passed"])

    def test_wrong_reference_cursor_and_unread_quote(self):
        self.answer["reference_title"] = "Title only mentioned in the final answer"
        self.save()
        verdict = audit.audit(self.record, self.library)
        self.assertFalse(verdict["children"][0]["checks"]["reference_returned"])
        self.events.pop(3)
        self.save()
        checks = audit.audit(self.record, self.library)["children"][0]["checks"]
        self.assertFalse(checks["quoted_range_delivered"])
        self.assertFalse(checks["cursor_consumed"])

    def test_log_tampering_or_receipt_from_another_call_fails(self):
        (self.record / "literature.jsonl").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            audit.audit(self.record, self.library)
        self.events[0]["payload"]["item"]["arguments"]["query"] = "something else"
        self.save()
        with self.assertRaisesRegex(ValueError, "logged invocation"):
            audit.audit(self.record, self.library)

    def test_archive_bytes_and_attachment_links(self):
        (self.record / "out/answer.json").rename(self.root / "temp-answer")
        (self.record / "out/answer.json").symlink_to(self.root / "temp-answer")
        with self.assertRaises(OSError):
            audit.audit(self.record, self.library)
        (self.record / "out/answer.json").unlink()
        os.link(self.root / "temp-answer", self.record / "out/answer.json")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            audit.audit(self.record, self.library)
        (self.record / "out/answer.json").unlink()
        self.save()
        obj = self.library / "objects/sha256" / self.answer["source_sha256"]
        obj.write_bytes(XML.replace(b"Format", b"Forged"))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            audit.audit(self.record, self.library)

    def test_each_chain_step_needs_evidence_in_the_log_row(self):
        for tool, key in (("search_papers", "title_search"), ("acquire_fulltext", "xml_acquired"),
                          ("get_citations", "reference_returned")):
            with self.subTest(tool=tool):
                pristine = self.runtime.logfile.read_bytes()
                self.edit_log(tool, lambda row: row.update(evidence=[]))
                self.assertFalse(self.checks()[key])
                self.runtime.logfile.write_bytes(pristine)
                self.edit_log(tool, lambda row: row.update(evidence=[{"unrelated": "value"}]))
                self.assertFalse(self.checks()[key])
                self.runtime.logfile.write_bytes(pristine)
                self.save()
                self.assertTrue(self.checks()[key])

    def test_a_cursor_that_was_not_issued_by_the_previous_read_does_not_count(self):
        self.assertTrue(self.checks()["cursor_consumed"])
        self.edit_receipt("read_document", lambda body: body["data"].update(next_cursor="cursor-never-issued"))
        self.assertFalse(self.checks()["cursor_consumed"])

    def test_read_passage_needs_matching_evidence_in_the_log_row(self):
        self.edit_log("read_document", lambda row: row.update(evidence=[]))
        self.assertFalse(self.checks()["quoted_range_delivered"])

    def test_search_must_name_the_paper_and_acquisition_must_follow_it(self):
        self.edit_receipt("search_papers", lambda body: body["data"]["papers"][0].update(pmcid="PMC0000001"))
        self.assertFalse(self.checks()["title_search"])
        self.edit_receipt("search_papers", lambda body: body["data"]["papers"][0].update(pmcid=audit.PMC))
        self.assertTrue(self.checks()["title_search"])
        self.edit_receipt("acquire_fulltext", lambda body: body["data"].update(document_version="0" * 64))
        self.assertFalse(self.checks()["xml_acquired"])

    def test_reference_must_be_returned_in_the_references_direction(self):
        self.edit_receipt("get_citations", lambda body: body["data"].update(direction="citations"))
        self.assertFalse(self.checks()["reference_returned"])
        self.save()
        arguments = {"paper_ref": audit.DOI, "direction": "citations"}
        for event in self.events:
            if event["payload"]["item"]["tool"] == "get_citations":
                event["payload"]["item"]["arguments"] = arguments
        self.edit_log("get_citations", lambda row: row.update(inputs=arguments))
        self.assertFalse(self.checks()["reference_returned"])

    def test_original_must_come_from_the_europe_pmc_endpoint(self):
        manifest = self.library / "documents" / self.doc / "manifest.json"
        for change in ({"provider": "other_provider"},
                       {"source_url": "https://example.org/europepmc/webservices/rest/PMC2723002/fullTextXML"},
                       {"source_url": "http://www.ebi.ac.uk/europepmc/webservices/rest/PMC2723002/fullTextXML"},
                       {"source_url": "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0000001/fullTextXML"}):
            with self.subTest(change=change):
                data = json.loads(manifest.read_text())
                data["source_metadata"].update(change)
                manifest.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "Europe PMC"):
                    audit.audit(self.record, self.library)
                data["source_metadata"].update(self.meta)
                manifest.write_text(json.dumps(data))
        self.assertTrue(audit.audit(self.record, self.library)["passed"])

    def test_archived_normalized_text_must_match_a_fresh_parse(self):
        text = self.library / "documents" / self.doc / "text.txt"
        text.write_bytes(text.read_bytes() + b" extra")
        with self.assertRaisesRegex(ValueError, "normalized text differs"):
            audit.audit(self.record, self.library)

    def test_quote_must_occur_at_the_cited_locator(self):
        self.answer["quote"] = "Invented header and alignment text with @ that is not in the document."
        self.save()
        with self.assertRaisesRegex(ValueError, "body locator"):
            audit.audit(self.record, self.library)

    def test_answer_must_name_the_target_paper_and_terms(self):
        self.answer["pmcid"] = "PMC0000001"
        self.save()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            audit.audit(self.record, self.library)
        self.answer["pmcid"] = audit.PMC
        self.answer["answer"] = "A sentence without the required terms."
        self.save()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            audit.audit(self.record, self.library)

    def test_call_record_must_be_a_finished_passing_lit_service_task(self):
        path = self.record / "call.json"
        original = json.loads(path.read_text())
        for key, value in (("status", "failed"), ("task", "other"), ("purpose", "trial"), ("check", {"passed": False})):
            with self.subTest(key=key):
                path.write_text(json.dumps({**original, key: value}))
                with self.assertRaisesRegex(ValueError, "did not finish"):
                    audit.audit(self.record, self.library)
        path.write_text(json.dumps(original))
        self.assertTrue(audit.audit(self.record, self.library)["passed"])

    def test_failed_or_unmatched_receipts_do_not_count(self):
        for change in (lambda item: item.update(status="failed"),
                       lambda item: item["result"].update(isError=True),
                       lambda item: item.update(server="other")):
            with self.subTest():
                saved = json.dumps(self.events)
                change(self.events[1]["payload"]["item"])
                self.save()
                self.assertFalse(self.checks()["xml_acquired"])
                self.events[:] = json.loads(saved)
        self.save()
        self.edit_log("acquire_fulltext", lambda row: row.update(status="partial"))
        with self.assertRaisesRegex(ValueError, "logged invocation"):
            audit.audit(self.record, self.library)


class QuoteOutsideBody(Fixture):
    xml = XML_QUOTE_IN_ABSTRACT

    def test_quote_in_an_abstract_block_is_not_a_body_locator(self):
        self.assertEqual(self.block["section_path"][:1], ["Abstract"])
        with self.assertRaisesRegex(ValueError, "body locator"):
            audit.audit(self.record, self.library)


class PairedEvidence(Fixture):
    task = "paired-validation"

    def outputs(self):
        return {"out/evidence.json": json.dumps(self.evidence).encode(), "out/report.md": "研究备忘录".encode()}

    def setUp(self):
        self.evidence = []
        super().setUp()
        self.evidence.append({**self.answer, "claim": "Format of the first paper"})
        doi, quote = "10.1234/second", "paired β measurements"
        raw = (f'<article><front><article-meta><article-id pub-id-type="doi">{doi}</article-id>'
               f'</article-meta></front><body><sec><title>Étude</title><p>Café: {quote}.</p>'
               '</sec></body></article>').encode()
        article = self.runtime.ingest(raw, "application/xml", {})
        doc = article["document_id"]
        self.block2 = next(b for b in article["blocks"] if quote in b["text"])
        self.evidence.append({"doi": doi, "quote": quote, "locator": self.block2["locator"], "claim": "Paired measures",
                              **{k: article[k] for k in ("document_id", "document_version", "source_sha256")}})
        first = self.runtime.store.read(doc, locator=self.block2["locator"], max_bytes=12)
        self.call("read_document", {"document_id": doc, "locator": self.block2["locator"], "max_bytes": 12}, first)
        args = {"document_id": doc, "cursor": first["next_cursor"]}
        self.call("read_document", args, self.runtime.store.read(doc, cursor=args["cursor"]))
        self.save()

    def replay(self):
        return audit.audit(self.record, self.library, self.task)

    def test_two_sources_and_paginated_child_receipts(self):
        result = self.replay()
        self.assertTrue(result["passed"])
        self.assertEqual(result["papers"], 2)
        verified = result["rows"][1]
        self.assertEqual(verified["child"], self.child)
        self.assertEqual(len(verified["invocations"]), 2)
        quote = self.evidence[1]["quote"]
        position = self.block2["text"].index(quote)
        at = self.block2["char_range"][0] + position
        byte_at = self.block2["byte_range"][0] + len(self.block2["text"][:position].encode())
        self.assertEqual(verified["char_range"], [at, at + len(quote)])
        self.assertEqual(verified["byte_range"], [byte_at, byte_at + len(quote.encode())])

    def test_lead_reread_and_matching_tool_log_cannot_fill_a_child_receipt_gap(self):
        self.parent_events.append(self.events[-1])
        self.save()
        with self.assertRaisesRegex(ValueError, "child read receipt"):
            self.replay()

    def test_correct_child_receipt_still_needs_its_own_matching_logged_passage(self):
        invocation = json.loads(self.events[-1]["payload"]["item"]["result"]["content"][0]["text"])["invocation_id"]

        def drop_passage(row):
            if row["invocation_id"] == invocation:
                row["evidence"] = []

        self.edit_log("read_document", drop_passage)
        with self.assertRaisesRegex(ValueError, "child read receipt"):
            self.replay()

    def test_byte_range_must_match_source_even_when_receipt_and_log_agree(self):
        item = self.events[-1]["payload"]["item"]
        part = item["result"]["content"][0]
        result = json.loads(part["text"])
        result["data"]["passages"][0]["byte_range"] = [0, 1]
        part["text"] = json.dumps(result)
        invocation = result["invocation_id"]

        def wrong_bytes(row):
            if row["invocation_id"] == invocation:
                for entry in row["evidence"]:
                    if "byte_range" in entry:
                        entry["byte_range"] = [0, 1]

        self.edit_log("read_document", wrong_bytes)
        with self.assertRaisesRegex(ValueError, "child read receipt"):
            self.replay()

    def test_each_evidence_row_must_match_jats_doi_and_two_distinct_papers_are_required(self):
        self.evidence[1]["doi"] = "10.1234/wrong"
        self.save()
        with self.assertRaisesRegex(ValueError, "Cited DOI"):
            self.replay()
        self.evidence[1] = self.evidence[0]
        self.save()
        with self.assertRaisesRegex(ValueError, "two distinct"):
            self.replay()
