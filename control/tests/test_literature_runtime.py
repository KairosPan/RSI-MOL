"""Storage and evidence contracts shared by the literature server's MCP processes."""

import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.installed_seed import WORK

SEED = WORK / "tools/literature"
with patch.object(sys, "path", [str(SEED), *sys.path]), patch.object(sys, "dont_write_bytecode", True):
    from runtime import Runtime
    from rsi_literature.documents import DocumentError
    from dispatch import parsers, poppler

XML = ("<article><body><sec><title>Results</title><p>Café, 茶, and apples.</p>"
       "<p>The sample has two stages.</p></sec></body></article>").encode()
XML_TYPE = "application/xml"


def two_pages():
    content = [b"0 0 1 rg 10 10 60 60 re f BT /F1 10 Tf 10 90 Td (First) Tj ET",
               b"1 0 0 rg 30 30 50 50 re f BT /F1 10 Tf 10 90 Td (Second) Tj ET"]
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /Font << /F1 7 0 R >> >> "
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
               page + b"/Contents 5 0 R >>", page + b"/Contents 6 0 R >>",
               *(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream) for stream in content),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(b"%d 0 obj\n%s\nendobj\n" % (number, obj))
    start = len(out)
    out.extend(b"xref\n0 8\n0000000000 65535 f \n")
    out.extend(b"".join(b"%010d 00000 n \n" % offset for offset in offsets))
    out.extend(b"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % start)
    return bytes(out)


def contents(folder):
    return {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


class LiteratureRuntime(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.work, self.trial, self.library = (root / name for name in ("work", "trial", "library"))
        for folder in (self.work, self.trial, self.library):
            folder.mkdir()
        self.runtime = Runtime(self.work)

    @unittest.skipUnless(sys.platform == 'win32', 'native Windows subprocess adapter')
    def test_windows_parser_drains_binary_output_and_bounds_the_job(self):
        def execute(code, **settings):
            return parsers._execute([sys.executable, '-I', '-c', code], self.work, settings, {'attempt_count': 0})

        self.assertEqual(execute('import os; os.write(1, bytes(range(256)))'), bytes(range(256)))
        with self.assertRaises(parsers._ParserError) as over:
            execute("import os; os.write(1, b'x' * 4096)", max_log_bytes=1024)
        self.assertEqual(over.exception.code, 'output_limit')
        with self.assertRaises(parsers._ParserError) as slow:
            execute('import time; time.sleep(4)', timeout=1)
        self.assertEqual(slow.exception.code, 'parser_timeout')

    def test_multiple_processes_share_one_original_and_run_cursor(self):
        src = ("from runtime import Runtime\n"
               "import sys\n"
               "r=Runtime(sys.argv[1]); "
               "print(r.ingest(bytes.fromhex(sys.argv[2]),'application/xml',{})['document_id'])")
        cmd = [sys.executable, "-B", "-c", src, str(self.work), XML.hex()]
        def invoke(_):
            return subprocess.check_output(cmd, cwd=SEED, text=True).strip()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(invoke, range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(list((self.library / "objects/sha256").iterdir())), 1)
        self.assertEqual(len(list((self.library / "documents").iterdir())), 1)
        original = self.runtime.ensure(ids[0])
        self.assertEqual(original["source_sha256"], hashlib.sha256(XML).hexdigest())
        self.assertEqual((self.library / "objects/sha256" / original["source_sha256"]).read_bytes(), XML)
        reader = Runtime(self.work)
        first = self.runtime.store.read(ids[0], max_bytes=12)
        second = reader.store.read(ids[0], cursor=first["next_cursor"], max_bytes=12)
        whole = reader.store.read(ids[0])["text"]
        for result in (first, second):
            start, end = result["char_range"]
            self.assertEqual(result["text"], whole[start:end])
            for passage in result["passages"]:
                lo, hi = passage["byte_range"]
                self.assertEqual(whole.encode()[lo:hi].decode(), passage["text"])
        self.assertEqual(first["char_range"][1], second["char_range"][0])
        alternate = reader.ingest(XML, XML_TYPE, {"source_id": "another-location"})
        self.assertNotEqual(original["document_id"], alternate["document_id"])
        self.assertEqual(len(list((self.library / "objects/sha256").iterdir())), 1)
        with self.assertRaisesRegex(DocumentError, "Cursor"):
            reader.store.read(alternate["document_id"], cursor=first["next_cursor"])

    def test_trial_miss_then_archive_hit_and_run_cursor_expiry(self):
        trial = Runtime(self.trial)
        self.assertEqual(list(self.library.iterdir()), [])
        with self.assertRaises(DocumentError) as miss:
            trial.ingest(XML, XML_TYPE, {})
        self.assertEqual(miss.exception.code, "trial_cache_miss")
        self.assertEqual(list(self.library.iterdir()), [])
        manifest = self.runtime.ingest(XML, XML_TYPE, {})
        before = contents(self.library)
        self.assertEqual(manifest, trial.ensure(manifest["document_id"]))
        self.assertEqual(manifest, trial.ingest(XML, XML_TYPE, {}))
        old = self.runtime.store.read(manifest["document_id"], max_bytes=6)
        with self.assertRaisesRegex(DocumentError, "Cursor"):
            trial.store.read(manifest["document_id"], cursor=old["next_cursor"])
        with self.assertRaises(DocumentError) as miss:
            trial.ingest(XML + b" ", XML_TYPE, {})
        self.assertEqual(miss.exception.code, "trial_cache_miss")
        self.assertEqual(before, contents(self.library))

    def test_changed_original_cannot_be_reused_or_hydrated(self):
        manifest = self.runtime.ingest(XML, XML_TYPE, {})
        original = self.library / "objects/sha256" / manifest["source_sha256"]
        original.write_bytes(XML.replace(b"apple", b"peach"))
        with self.assertRaisesRegex(DocumentError, "digest"):
            self.runtime.ingest(XML, XML_TYPE, {})
        trial = Runtime(self.trial)
        with self.assertRaisesRegex(DocumentError, "hash"):
            trial.ensure(manifest["document_id"])
        self.assertFalse((trial.store.root / manifest["document_id"]).exists())

    def test_registered_source_requires_fixed_identity(self):
        sources = {"article": {"path": "fixture.xml", "media_type": XML_TYPE,
                               "sha256": hashlib.sha256(XML).hexdigest()}}
        registry = self.work / "tools/literature/sources.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps(sources))
        (self.work / "fixture.xml").write_bytes(XML)
        raw, media, metadata = self.runtime.local_source("article")
        self.assertEqual((raw, media, metadata["source_id"]), (XML, XML_TYPE, "article"))
        with self.assertRaises(DocumentError) as absent:
            self.runtime.local_source("../unknown.xml")
        self.assertEqual(absent.exception.code, "unknown_source")
        (self.work / "fixture.xml").write_bytes(b"different")
        with self.assertRaisesRegex(DocumentError, "changed"):
            self.runtime.local_source("article")

    @unittest.skipUnless(all(shutil.which(cmd) for cmd in ("pdfinfo", "pdftotext", "pdftoppm")), "requires Poppler")
    def test_pdf_derivative_hydrates_its_source_and_trial_renders_in_scratch(self):
        pdf = self.runtime.ingest(two_pages(), "application/pdf", {"content_scope": "full_text"})
        derived = parsers.run_parser("poppler", {}, self.runtime.store, pdf["document_id"], "task-extract")
        self.assertIn(derived["status"], ("ok", "partial"), derived["errors"])
        doc = self.runtime.publish_derived(derived["data"]["document_id"])
        self.assertEqual(doc["source_metadata"]["derived_from"]["source_sha256"], pdf["source_sha256"])
        before = contents(self.library)
        trial = Runtime(self.trial)
        self.assertEqual(trial.ensure(doc["document_id"]), doc)
        extracted = parsers.run_parser("poppler", {}, trial.store, pdf["document_id"], "trial-extract")
        self.assertIn(extracted["status"], ("ok", "partial"), extracted["errors"])
        trial.publish_derived(extracted["data"]["document_id"])
        digests = []
        for page in [1, 2]:
            rendered = poppler.render_document_page({}, {"document_id": doc["document_id"], "page": page},
                                                    trial.store, "trial-render-" + str(page))
            self.assertEqual(rendered["status"], "ok", rendered["errors"])
            image = rendered["data"]["image"]
            png = Path(image["artifact_path"])
            self.assertTrue(png.is_relative_to(trial.root))
            self.assertEqual((image["page"], image["source_sha256"]), (page, pdf["source_sha256"]))
            digests.append(hashlib.sha256(png.read_bytes()).hexdigest())
            self.assertEqual(image["sha256"], digests[-1])
        self.assertNotEqual(digests[0], digests[1])
        self.assertEqual(before, contents(self.library))

    def test_log_is_per_process_bounded_and_does_not_copy_secrets_or_payloads(self):
        a, b = self.runtime, Runtime(self.work)
        inputs = {"provider": "europe_pmc", "paper_ref": "https://someone:PRIVATE@example.org/a?API_KEY=PRIVATE",
                  "unknown": {"Authorization": "Bearer PRIVATE"}, "cursor": "first"}
        result = {"provenance": {"provider": "europe_pmc", "attempt_count": 1}, "status": "ok",
                  "data": {"passages": [{"locator": "body.2", "char_range": [10, 16], "text": "PRIVATE"}],
                           "image": {"page": 2, "sha256": "abc", "base64": "PRIVATE"}},
                  "continuation": {"next_cursor": "second"}, "errors": []}
        a.log("a" * 32, "read_document", inputs, result)
        b.log("b" * 32, "get_paper", {}, {"status": "failed", "errors": [{"code": "invalid_input"}]})
        self.assertEqual(len(list(a.calls.glob("*.jsonl"))), 2)
        raw = a.logfile.read_text()
        self.assertNotIn("PRIVATE", raw)
        row = json.loads(raw)
        self.assertEqual(row["inputs"]["cursor"], "first")
        self.assertEqual(row["next_cursor"], "second")
        self.assertEqual(row["evidence"][0]["locator"], "body.2")
        self.assertEqual(row["evidence"][1]["image_sha256"], "abc")
        self.assertEqual(row["contacted_source"], "europe_pmc")
