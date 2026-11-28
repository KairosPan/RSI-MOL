"""The literature server's real HTTP transports against a local host that misbehaves on purpose (127.0.0.1 only;
no request leaves this machine), and the server itself staying up through every fault."""
import hashlib
import json
import os
import queue
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from rsi.check import _pdf

from tests.installed_seed import WORK

SEED = WORK / 'tools/literature'
_PYPDF = sys.modules.get('pypdf', 'absent')
with mock.patch.object(sys, 'path', [str(SEED), *sys.path]), mock.patch.object(sys, 'dont_write_bytecode', True):
    import transport
    from dispatch import Literature, RESPONSE_BYTES
    from rsi_literature import providers, remote
    from runtime import Runtime
    import server
if _PYPDF == 'absent':                             # importing server blocks pypdf; not for the other tests
    sys.modules.pop('pypdf', None)
else:
    sys.modules['pypdf'] = _PYPDF

CAP = 1024                      # the byte limit of the direct transport tests
ANSWER_SECONDS = 60             # how long the server test waits for one answer of the server
PDF = _pdf('Fetched Page')
PROXIES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
           'no_proxy')


def no_proxy_environment() -> dict:
    """This process's environment without proxies, and with every host bypassing any system proxy."""
    env = {key: value for key, value in os.environ.items() if key not in PROXIES}
    env['no_proxy'] = '*'
    return env


def head(status: str, *fields: str) -> bytes:
    return ('HTTP/1.1 ' + status + '\r\n' + ''.join(field + '\r\n' for field in fields) + '\r\n').encode('latin-1')


def complete(body: bytes, media: str = 'application/json') -> bytes:
    return head('200 OK', 'Content-Type: ' + media, f'Content-Length: {len(body)}', 'Connection: close') + body


# What the host sends for each fault: the bytes, written as they are, after which it closes the connection.
FAULTS = {
    'cut': head('200 OK', 'Content-Type: application/json', 'Transfer-Encoding: chunked') + b'5\r\n{"a":\r\n',
    'garbled': b'HTTP/1.1 2x0 OK\r\nContent-Length: 0\r\n\r\n',
    'long_header': b'HTTP/1.1 200 OK\r\nX-Long: ' + b'a' * 70000 + b'\r\n\r\n',
    'short': head('200 OK', 'Content-Type: application/pdf', 'Content-Length: 100') + PDF[:20],
    # Neither a length nor chunks: the body ends where the connection does, here cleanly in the middle of the PDF.
    'cut_close': b'HTTP/1.0 200 OK\r\nContent-Type: application/pdf\r\nConnection: close\r\n\r\n' + PDF[:40],
    'whole_close': b'HTTP/1.0 200 OK\r\nContent-Type: application/pdf\r\nConnection: close\r\n\r\n' + PDF,
    'not_xml': complete(b'this is not XML at all', 'application/xml'),
    'big': complete(b'x' * (CAP + 1)),
    'redirect': head('302 Found', 'Location: /ok', 'Content-Length: 0'),
    'ok': complete(b'{"fine": true}'),
}


class Host:
    """An HTTP host on 127.0.0.1 that answers each path with scripted bytes; `trickle` sends one header line every
    0.2 seconds for up to 5 seconds, `silent` sends nothing for 5 seconds. replies: path -> bytes, or a function of
    the request target -> bytes."""

    def __init__(self) -> None:
        host = self
        self.replies, self.seen = dict(FAULTS), []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(65537).decode('latin-1')
                while self.rfile.readline(65537) not in (b'\r\n', b'\n', b''):
                    pass
                parts = line.split(' ')
                target = parts[1] if len(parts) > 1 else ''
                host.seen.append(target)
                path = urlsplit(target).path
                try:
                    if path.endswith('/silent'):
                        time.sleep(5)
                        return
                    if path.endswith('/trickle'):
                        self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                        for number in range(25):
                            time.sleep(0.2)
                            self.wfile.write(b'X-Slow-%d: 1\r\n' % number)
                            self.wfile.flush()
                        return
                    reply = host.replies.get(path.rsplit('/', 1)[-1], head('404 Not Found', 'Content-Length: 0'))
                    self.wfile.write(reply(target) if callable(reply) else reply)
                except OSError:
                    pass

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}/'

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Transports(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.close)
        self.enterContext(mock.patch.dict(os.environ, no_proxy_environment(), clear=True))

    def test_a_download_fault_is_an_error_of_the_call(self):
        expected = {'cut': 'transport_failed', 'garbled': 'transport_failed', 'long_header': 'transport_failed',
                    'short': 'incomplete_body', 'big': 'response_limit'}
        for path, code in expected.items():
            with self.subTest(path=path), self.assertRaises(remote.RemoteError) as caught:
                transport.fetch('GET', self.host.base + path, {}, None, 5, CAP)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn('127.0.0.1', str(caught.exception))
        moved = transport.fetch('GET', self.host.base + 'redirect', {}, None, 5, CAP)
        self.assertEqual((moved['status'], moved['body']), (302, b''))
        self.assertEqual({k.lower(): v for k, v in moved['headers'].items()}['location'], '/ok')
        self.assertEqual(self.host.seen[-1], '/redirect')                       # never followed
        fine = transport.fetch('GET', self.host.base + 'ok', {}, None, 5, CAP)
        self.assertEqual((fine['status'], fine['body']), (200, b'{"fine": true}'))
        missing = transport.fetch('GET', self.host.base + 'missing', {}, None, 5, CAP)
        self.assertEqual((missing['status'], missing['body']), (404, b''))

    def test_a_provider_fault_is_a_provider_error(self):
        expected = {'cut': 'transport_error', 'garbled': 'transport_error', 'long_header': 'transport_error',
                    'short': 'incomplete_body', 'big': 'response_too_large', 'redirect': 'redirect_refused'}
        for path, code in expected.items():
            with self.subTest(path=path), self.assertRaises(providers.ProviderError) as caught:
                transport.provider_fetch('GET', self.host.base + path, {}, 5, CAP)
            self.assertEqual(caught.exception.code, code)
        fine = transport.provider_fetch('GET', self.host.base + 'ok', {}, 5, CAP)
        self.assertEqual((fine['status'], fine['body']), (200, b'{"fine": true}'))

    def test_one_deadline_covers_the_time_before_the_headers_are_complete(self):
        for name, attempt, error in (
                ('fetch', lambda: transport.fetch('GET', self.host.base + 'trickle', {}, None, 1, CAP),
                 remote.RemoteError),
                ('provider_fetch', lambda: transport.provider_fetch('GET', self.host.base + 'trickle', {}, 1, CAP),
                 providers.ProviderError)):
            with self.subTest(name=name):
                started = time.monotonic()
                with self.assertRaises(error) as caught:
                    attempt()
                self.assertEqual(caught.exception.code, 'timeout')
                self.assertLess(time.monotonic() - started, 3)      # each header line came well within 1 second

    def test_a_silent_host_is_a_timeout(self):
        for name, attempt, error in (
                ('fetch', lambda: transport.fetch('GET', self.host.base + 'silent', {}, None, 1, CAP),
                 remote.RemoteError),
                ('provider_fetch', lambda: transport.provider_fetch('GET', self.host.base + 'silent', {}, 1, CAP),
                 providers.ProviderError)):
            with self.subTest(name=name):
                started = time.monotonic()
                with self.assertRaises(error) as caught:
                    attempt()
                self.assertEqual(caught.exception.code, 'timeout')
                self.assertLess(time.monotonic() - started, 3)

    def acquire(self, path: str) -> tuple[dict, Path]:
        """acquire_fulltext through the real Literature._fetch of the PDF the local host serves at path; the reply
        and the library folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        work, library = Path(folder.name) / 'work', Path(folder.name) / 'library'
        work.mkdir(); library.mkdir()

        class Native:
            def fulltext_candidates(self, provider, paper_ref):
                return {'status': 'ok', 'provenance': {'provider': provider, 'attempt_count': 1},
                        'data': {'candidates': [{'url': host.base + path, 'format': 'pdf'}]}}
        host = self.host
        service = Literature(Runtime(work), native=Native(), resolve=lambda url: urlsplit(url).hostname)
        response, _ = service.call('acquire_fulltext', {'paper_ref': 'MED:1', 'preferred_format': 'pdf'}, 'a' * 32)
        return response, library

    def test_a_truncated_download_is_never_archived(self):
        # A body shorter than its length, and one cut cleanly where only the connection marked its end: the PDF has
        # no end-of-file marker.
        for path in ('short', 'cut_close'):
            with self.subTest(path=path):
                response, library = self.acquire(path)
                self.assertEqual(response['status'], 'unavailable')
                self.assertEqual(response['errors'][0]['code'], 'incomplete_body')
                self.assertNotIn('document_id', response['data'])
                self.assertEqual(response['provenance']['download_hosts'], ['127.0.0.1'])
                self.assertEqual(list(library.rglob('*')), [])
        response, library = self.acquire('whole_close')                # the whole PDF, delimited the same way
        self.assertEqual(response['data']['source_sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertTrue((library / 'objects' / 'sha256' / hashlib.sha256(PDF).hexdigest()).is_file())


class Stub:
    """A service whose call returns a fixed reply, for the server's own handling of it."""

    def __init__(self, runtime, response):
        self.runtime, self.response = runtime, response

    def call(self, name, args, invocation_id):
        return self.response, None

    def contacted(self):
        return {'provider': 'local', 'attempt_count': 0, 'download_hosts': []}


class Replies(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name) / 'work'
        self.work.mkdir()
        self.runtime = Runtime(self.work)

    def rows(self):
        return [json.loads(line) for line in self.runtime.logfile.read_text().splitlines()]

    def read_pages(self, manifest, **selection):
        session = server.Session(Literature(self.runtime))
        args = {'document_id': manifest['document_id'], **selection}
        pages = []
        while True:
            result = session.tool_call('read_document', args)
            self.assertLessEqual(len(server.encode(result)), server.NATIVE_RECEIPT_BYTES)
            reply = json.loads(result['content'][0]['text'])
            self.assertFalse(result['isError'], reply.get('errors'))
            page = reply['data']
            if pages:
                self.assertEqual(pages[-1]['char_range'][1], page['char_range'][0])
            self.assertGreater(page['char_range'][1], page['char_range'][0])
            self.assertEqual(reply['continuation']['next_cursor'], page['next_cursor'])
            pages.append(page)
            if not page['next_cursor']:
                return pages
            args['cursor'] = page['next_cursor']

    def test_read_cursor_delivers_each_fitting_block_whole(self):
        paragraphs = [f'Method {n}: ' + '"中\\' * 72 for n in range(48)]
        xml = '<article><body><sec>' + ''.join('<p>' + p + '</p>' for p in paragraphs) + '</sec></body></article>'
        manifest = self.runtime.ingest(xml.encode(), 'application/xml', {})
        for budget in (8000, 15000, 65536):
            pages = self.read_pages(manifest, max_bytes=budget)
            self.assertGreater(len(pages), 1)
            self.assertEqual(''.join(p['text'] for p in pages), '\n\n'.join(paragraphs))
            rows = [r for p in pages for r in p['passages']]
            self.assertTrue(all(r['block_complete'] for r in rows))
            self.assertEqual([r['locator'] for r in rows], [b['locator'] for b in manifest['blocks']])

    def test_single_oversized_block_progresses_with_correct_utf8_byte_ranges(self):
        text = '中"\\☃' * 6000
        xml = '<article><body><p>' + text + '</p></body></article>'
        metadata = {'license': 'License terms. ' * 3000, 'paper_ref': 'PMC123'}
        manifest = self.runtime.ingest(xml.encode(), 'application/xml', metadata)
        pages = self.read_pages(manifest, locator=manifest['blocks'][0]['locator'], max_bytes=65536)
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(p['text'] for p in pages), text)
        for page in pages:
            row, = page['passages']
            self.assertFalse(row['block_complete'])
            self.assertEqual(row['source_metadata'], {'paper_ref': 'PMC123'})
            self.assertEqual(row['omitted_source_metadata_fields'], ['license'])
            low, high = row['byte_range']
            self.assertEqual(text.encode()[low:high].decode(), page['text'])

    def test_fixed_coverage_warnings_leave_room_for_a_readable_pdf_line(self):
        source = self.runtime.ingest(PDF, 'application/pdf', {})
        binding = {k: source[k] for k in ('document_id', 'document_version', 'source_sha256')}
        line = '<page width="100" height="100"><line xMin="0" xMax="8" yMin="0" yMax="8">'
        line += '<word xMin="0" xMax="8" yMin="0" yMax="8">Known</word></line></page>'
        bbox = ('<doc>' + line + '<page width="100" height="100"/>' * 180 + '</doc>').encode()
        parser_coverage = {'unread_pages': list(range(2, 182)), 'review_status': 'unreviewed',
                           'limitations': ['x' * 30000], 'parser_notes': ['y' * 10000]}
        manifest = self.runtime.ingest(bbox, 'application/vnd.rsi.poppler-bbox+xml',
                                       {'derived_from': binding, 'extraction_coverage': parser_coverage})
        page, = self.read_pages(manifest, locator=manifest['blocks'][0]['locator'])
        row, = page['passages']
        self.assertTrue(row['block_complete'])
        self.assertEqual(row['text'], 'Known')
        self.assertEqual(row['source_metadata']['derived_from'], binding)
        extracted = row['source_metadata']['extraction_coverage']
        self.assertEqual(extracted['review_status'], 'unreviewed')
        self.assertEqual(extracted['omitted_limitations'], 1)
        self.assertEqual(extracted['omitted_coverage_fields'], {'parser_notes': 1})
        self.assertEqual(page['coverage']['extraction_status'], 'partial')
        self.assertEqual(page['coverage']['omitted_limitations'], len(manifest['coverage']['limitations']) - 2)

    def test_fixed_optional_metadata_does_not_hide_a_short_source_block(self):
        metadata = {'license': 'Readable license text. ' * 2800, 'paper_ref': 'PMC123', 'provider': 'local'}
        manifest = self.runtime.ingest(b'<article><body><p>Readable.</p></body></article>', 'application/xml', metadata)
        page, = self.read_pages(manifest)
        row, = page['passages']
        self.assertEqual(row['text'], 'Readable.')
        self.assertTrue(row['block_complete'])
        self.assertEqual(row['source_metadata'], {'paper_ref': 'PMC123', 'provider': 'local'})
        self.assertEqual(row['omitted_source_metadata_fields'], ['license'])

    def test_unrepresentable_passage_metadata_allows_continued_text_without_an_invalid_receipt(self):
        text = 'word ' * 1000 + 'end'
        xml = '<article><body><p id="' + ('x' * 60000) + '">' + text + '</p></body></article>'
        manifest = self.runtime.ingest(xml.encode(), 'application/xml', {})
        pages = self.read_pages(manifest)
        self.assertEqual(''.join(p['text'] for p in pages), text)
        for page in pages:
            self.assertEqual(page['passages'], [])
            self.assertEqual(page['omitted_passage']['locator'], manifest['blocks'][0]['locator'])

    def test_large_reading_index_keeps_its_acquisition_receipt(self):
        data = {'document_id': 'doc_' + 'a' * 64, 'document_version': 'b' * 64, 'source_sha256': 'c' * 64,
                'media_type': 'application/xml', 'content_scope': 'full_text', 'references': ['ref' * 200] * 100,
                'sections': ['Methods'], 'pages': []}
        response = {'status': 'ok', 'coverage': {'verified_paper_identifiers': ['doi']}, 'data': data,
                    'provenance': {'provider': 'local'}, 'continuation': {}, 'errors': []}
        reply = server.Session(Stub(self.runtime, response)).tool_call('acquire_fulltext', {'source_id': 'large'})
        self.assertFalse(reply['isError'])
        self.assertLessEqual(len(server.encode(reply)), server.NATIVE_RECEIPT_BYTES)
        shown = json.loads(reply['content'][0]['text'])
        self.assertEqual(shown['coverage'], response['coverage'])
        self.assertEqual(shown['data']['omitted_reading_index_items'], {'references': 100, 'sections': 1, 'pages': 0})
        for key in ('document_id', 'document_version', 'source_sha256', 'media_type', 'content_scope'):
            self.assertEqual(shown['data'][key], data[key])

    def test_oversized_paper_page_retains_all_identities_and_the_provider_cursor(self):
        papers = [{'paper_id': f'europe_pmc:MED:{i}', 'provider': 'europe_pmc', 'pmid': str(i),
                   'abstract': 'Large abstract. ' * 900, 'title': f'Paper {i}'} for i in range(8)]
        continuation = {'next_cursor': 'next-complete-page', 'complete_for_query': False}
        response = {'status': 'partial', 'data': {'papers': papers}, 'coverage': {'returned': 8},
                    'continuation': continuation, 'provenance': {'provider': 'europe_pmc'}, 'errors': []}
        for name in ('search_papers', 'get_citations'):
            reply = server.Session(Stub(self.runtime, response)).tool_call(name, {})
            shown = json.loads(reply['content'][0]['text'])
            self.assertFalse(reply['isError'])
            self.assertEqual(shown['continuation'], continuation)
            self.assertEqual(shown['coverage'], response['coverage'])
            returned = shown['data']['papers']
            self.assertTrue(any(p['abstract'] == papers[0]['abstract'] for p in returned))
            self.assertTrue(any('next_abstract_cursor' in p for p in returned))
            for original, row in zip(papers, shown['data']['papers'], strict=True):
                self.assertEqual(row['paper_id'], original['paper_id'])
                self.assertTrue(row['abstract'])
                self.assertTrue(original['abstract'].startswith(row['abstract']))
            self.assertLessEqual(len(server.encode(reply)), server.NATIVE_RECEIPT_BYTES)

    def test_abstract_can_be_read_to_the_end_without_mixing_provider_versions(self):
        text = 'Multi-byte 中\"\\🌳' * 2400
        paper = dict(paper_id='pubmed:1', provider='pubmed', title='Article', abstract=text,
                     abstract_truncated=False, authors=['Author'], content_scope='abstract')
        response = dict(status='ok', data={'paper': paper}, coverage={}, errors=[], continuation={}, provenance={})
        session = server.Session(Stub(self.runtime, response))
        args = {'provider': 'pubmed', 'paper_ref': paper['paper_id']}
        full = ''
        while True:
            reply = session.tool_call('get_paper', args)
            self.assertFalse(reply['isError'])
            self.assertLessEqual(len(server.encode(server.encode(reply).decode())), 64 * 1024)
            shown = json.loads(reply['content'][0]['text'])['data']['paper']
            self.assertEqual(shown['abstract_range'][0], len(full))
            full += shown['abstract']
            self.assertEqual(shown['abstract_range'][1], len(full))
            self.assertEqual(shown['authors'], paper['authors'])
            if 'next_abstract_cursor' not in shown:
                self.assertFalse(shown['abstract_truncated'])
                break
            args['abstract_cursor'] = shown['next_abstract_cursor']
        self.assertEqual(full, text)
        paper['abstract_truncated'] = True
        last = session.tool_call('get_paper', args)
        self.assertTrue(json.loads(last['content'][0]['text'])['data']['paper']['abstract_truncated'])
        paper['abstract'] += 'Correction.'
        self.assertTrue(session.tool_call('get_paper', args)['isError'])

    def test_search_page_above_32_kib_keeps_its_abstracts_and_authors(self):
        from seed.work.tools.memory import query

        self.assertEqual((server.NATIVE_RECEIPT_BYTES, server.NATIVE_TEXT_BYTES),
                         (query.NATIVE_RECEIPT_BYTES, query.NATIVE_TEXT_BYTES))
        papers = [dict(paper_id=f'pubmed:{n}', provider='pubmed', abstract='Ordinary abstract. ' * 190,
                       authors=['First author'], content_scope='abstract') for n in range(10)]
        response = dict(status='ok', data={'papers': papers}, coverage={}, provenance={}, errors=[], continuation={})
        reply = server.Session(Stub(self.runtime, response)).tool_call('search_papers', {})
        self.assertGreater(len(server.encode(reply)), 32 * 1024)
        self.assertEqual(json.loads(reply['content'][0]['text'])['data']['papers'], papers)

    def test_default_read_delivers_a_large_single_block_whole(self):
        text = 'Methods for this group. ' * 850
        manifest = self.runtime.ingest(f'<article><body><p>{text}</p></body></article>'.encode(), 'application/xml', {})
        page, = self.read_pages(manifest)
        row, = page['passages']
        self.assertEqual(row['text'], text.rstrip())
        self.assertTrue(row['block_complete'])

    def test_titles_that_exceed_a_page_can_be_retrieved_individually(self):
        papers = [dict(paper_id=f'pubmed:{i}', provider='pubmed', title='文' * 4000 + str(i),
                       abstract='一组摘要。' * 600, url='https://example.org/' + 'a' * 400) for i in range(10)]
        cursor = {'next_cursor': 'next-provider-page', 'complete_for_query': False}
        response = dict(status='partial', data={'papers': papers}, coverage={}, provenance={}, errors=[], continuation=cursor)
        reply = server.Session(Stub(self.runtime, response)).tool_call('search_papers', {})
        self.assertFalse(reply['isError'])
        page = json.loads(reply['content'][0]['text'])
        self.assertEqual(page['continuation'], cursor)
        self.assertEqual(len(page['data']['papers']), len(papers))
        self.assertTrue(any('title' in row.get('omitted_fields', []) for row in page['data']['papers']))
        for row, paper in zip(page['data']['papers'], papers):
            self.assertEqual(row['paper_id'], paper['paper_id'])
            self.assertTrue(row['abstract'])
            if 'title' in row:
                self.assertEqual(row['title'], paper['title'])
            else:
                self.assertIn('title', row['omitted_fields'])
            if len(row['abstract']) < len(paper['abstract']):
                self.assertIn('next_abstract_cursor', row)
        single = {**response, 'data': {'paper': papers[0]}}
        reply = server.Session(Stub(self.runtime, single)).tool_call('get_paper', {'paper_ref': papers[0]['paper_id']})
        self.assertEqual(json.loads(reply['content'][0]['text'])['data']['paper'], papers[0])

    def test_broad_pdf_read_retains_line_boxes_and_locator_restores_word_boxes(self):
        word = '<word xMin="5" xMax="15" yMin="10" yMax="25">Text</word>'
        line = '<line xMin="5" xMax="90" yMin="10" yMax="25">' + word * 24 + '</line>'
        bbox = ('<doc><page width="100" height="100">' + line * 40 + '</page></doc>').encode()
        source = self.runtime.ingest(PDF, 'application/pdf', {})
        binding = {k: source[k] for k in ('document_id', 'document_version', 'source_sha256')}
        manifest = self.runtime.ingest(bbox, 'application/vnd.rsi.poppler-bbox+xml', {'derived_from': binding})
        pages = self.read_pages(manifest)
        rows = [r for p in pages for r in p['passages']]
        self.assertEqual([r['locator'] for r in rows], [b['locator'] for b in manifest['blocks']])
        self.assertTrue(all(r['block_complete'] for r in rows))
        self.assertLessEqual(len(pages), 3)
        first = rows[0]
        self.assertEqual(first['source_locator']['omitted_word_boxes'], 24)
        detail, = self.read_pages(manifest, locator=first['locator'])
        coords = detail['passages'][0]['source_locator']
        self.assertEqual(len(coords['words']), 24)
        self.assertEqual(first['source_locator']['bbox'], coords['bbox'])

    def test_a_replaced_reply_keeps_its_sources_hosts_and_archived_document(self):
        provenance = {'provider': 'europe_pmc', 'attempt_count': 2, 'download_hosts': ['files.example.org'],
                      'requests': [{'url': 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=x'}]}
        large = {'status': 'ok', 'data': {'document_id': 'doc_' + 'a' * 64, 'source_sha256': 'b' * 64,
                                          'document_version': 'c' * 64, 'sections': ['x' * RESPONSE_BYTES]},
                 'coverage': {}, 'provenance': provenance, 'continuation': {}, 'errors': []}
        reply = server.Session(Stub(self.runtime, large)).tool_call('acquire_fulltext', {'paper_ref': 'MED:1'})
        shown = json.loads(reply['content'][0]['text'])
        self.assertTrue(reply['isError'])
        self.assertEqual(shown['errors'][0]['code'], 'response_too_large')
        self.assertEqual(shown['data'], {'document_id': 'doc_' + 'a' * 64, 'source_sha256': 'b' * 64,
                                         'document_version': 'c' * 64})
        self.assertIn('read_document', shown['errors'][0]['detail'])
        row, = self.rows()
        self.assertEqual((row['contacted_source'], row['invocation_id']), ('europe_pmc', shown['invocation_id']))
        self.assertEqual(row['contacted_hosts'], ['files.example.org', 'www.ebi.ac.uk'])
        self.assertIn({'document_id': 'doc_' + 'a' * 64, 'source_sha256': 'b' * 64, 'document_version': 'c' * 64},
                      row['evidence'])

    def test_an_unexpected_exception_is_a_tool_error_with_a_log_row(self):
        class Broken(Stub):
            def call(self, name, args, invocation_id):
                raise LookupError('a fault of the server')

            def contacted(self):
                return {'provider': 'openalex', 'attempt_count': 1, 'download_hosts': ['api.openalex.org']}
        reply = server.Session(Broken(self.runtime, None)).tool_call('get_paper', {'paper_ref': 'W1'})
        shown = json.loads(reply['content'][0]['text'])
        self.assertTrue(reply['isError'])
        self.assertEqual(shown['errors'][0]['code'], 'internal_error')
        self.assertNotIn('a fault of the server', reply['content'][0]['text'])
        row, = self.rows()
        self.assertEqual((row['contacted_source'], row['contacted_hosts'], row['status']),
                         ('openalex', ['api.openalex.org'], 'failed'))

    def test_mirrors_share_a_download_deadline(self):
        native = mock.Mock()
        candidates = [{'url': f'https://mirror{i}.example.org/paper', 'format': 'pdf'} for i in range(3)]
        native.fulltext_candidates.return_value = {'status': 'ok', 'data': {'candidates': candidates},
                                                   'provenance': {'provider': 'openalex', 'attempt_count': 1}}
        with mock.patch('dispatch.time') as clock:
            clock.monotonic.return_value = 0
            def refuse(method, url, headers, body, timeout, limit):
                clock.monotonic.return_value += min(12, timeout)
                return {'status': 403, 'headers': {}, 'body': b''}
            http = mock.Mock(side_effect=refuse)
            service = Literature(self.runtime, native=native, http=http, resolve=lambda url: urlsplit(url).hostname)
            response, _ = service.call('acquire_fulltext', {'provider': 'openalex', 'paper_ref': 'W1'}, 'a' * 32)
        self.assertEqual([call.args[4] for call in http.call_args_list], [20, 8])
        self.assertEqual(response['errors'][0]['code'], 'timeout')
        self.assertEqual(response['provenance']['download_hosts'], ['mirror0.example.org', 'mirror1.example.org'])

    def test_a_well_formed_but_unreadable_xml_is_only_a_fallback(self):
        native = mock.Mock()
        candidates = [{'format': 'xml', 'url': f'https://mirror{i}.example.org/paper'} for i in range(2)]
        native.fulltext_candidates.return_value = {'status': 'ok', 'data': {'candidates': candidates},
                                                   'provenance': {'provider': 'europe_pmc', 'attempt_count': 1}}
        empty = b'<article/>'
        readable = b'<article><body><p>The paper text.</p></body></article>'
        for status, text, expected in [(200, readable, readable), (403, b'', empty)]:
            with self.subTest(last_status=status):
                http = mock.Mock(side_effect=[
                    {'status': 200, 'headers': {'content-type': 'application/xml'}, 'body': empty},
                    {'status': status, 'headers': {'content-type': 'application/xml'}, 'body': text}])
                service = Literature(self.runtime, native=native, http=http, resolve=lambda url: urlsplit(url).hostname)
                reply, _ = service.call('acquire_fulltext', {'paper_ref': 'MED:1', 'preferred_format': 'xml'}, 'a' * 32)
                self.assertEqual(reply['data']['source_sha256'], hashlib.sha256(expected).hexdigest())
                self.assertEqual(reply['status'], 'ok' if status == 200 else 'unavailable')
                attempts = reply['provenance']['download_attempts']
                self.assertEqual(attempts[0]['outcome'], 'no_readable_text')
                self.assertEqual(attempts[1]['http_status'], status)

    def test_trial_cache_miss_survives_a_failed_mirror_and_a_cached_mirror_is_usable(self):
        trial_path = self.work.parent / 'trial'
        trial_path.mkdir()
        trial = Runtime(trial_path)
        native = mock.Mock()
        candidates = [{'format': 'xml', 'url': f'https://mirror{i}.example.org/paper'} for i in range(2)]
        native.fulltext_candidates.return_value = {'status': 'ok', 'data': {'candidates': candidates},
                                                   'provenance': {'provider': 'europe_pmc', 'attempt_count': 1}}
        def acquire(runtime, statuses):
            http = mock.Mock(side_effect=[{'status': s, 'headers': {'content-type': 'application/xml'},
                                           'body': b'<article><body><p>Text.</p></body></article>'} for s in statuses])
            service = Literature(runtime, native=native, http=http, resolve=lambda url: urlsplit(url).hostname)
            return service.call('acquire_fulltext', {'paper_ref': 'MED:1', 'preferred_format': 'xml'}, 'a' * 32)[0]

        miss = acquire(trial, [200, 403])
        self.assertEqual(miss['errors'][0]['code'], 'trial_cache_miss')
        self.assertFalse(trial.library.exists())
        archived = acquire(self.runtime, [403, 200])
        found = acquire(trial, [200, 200])
        self.assertEqual(found['status'], 'ok')
        self.assertEqual(found['data']['document_id'], archived['data']['document_id'])
        self.assertEqual([a['outcome'] for a in found['provenance']['download_attempts']],
                         ['trial_cache_miss', 'acquired'])


DRIVER = '''
import sys
from pathlib import Path
from urllib.parse import urlsplit
from rsi_literature import providers
providers.BASES['europe_pmc'] = sys.argv[1]
from dispatch import Literature
from runtime import Runtime
from server import serve
import runtime as runtime_module

original = runtime_module.Runtime.ensure
def ensure(self, document_id):
    if document_id == 'doc_' + 'f' * 64:
        raise LookupError('a fault of the server')
    return original(self, document_id)
runtime_module.Runtime.ensure = ensure

work = Runtime(Path(__file__).resolve().parents[2])
serve(work, sys.stdin.buffer, sys.stdout.buffer, service=Literature(work, resolve=lambda url: urlsplit(url).hostname))
'''
# A pypdf the Lab could have installed: if the server ever imported it, a PDF's parser version would name it.
FAKE_PYPDF = ('__version__ = "9.9.9"\n\n\nclass PdfReader:\n    is_encrypted = False\n\n'
              '    def __init__(self, stream):\n        self.pages = []\n')


class Server(unittest.TestCase):
    """The real server process, with its real transports, against the local host."""

    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name).resolve() / 'work'
        tools = self.work / 'tools' / 'literature'
        shutil.copytree(SEED, tools, ignore=shutil.ignore_patterns('__pycache__'))
        (tools / 'fault_server.py').write_text(DRIVER)
        (tools / 'pypdf.py').write_text(FAKE_PYPDF)
        self.library = self.work.parent / 'library'
        self.library.mkdir()
        self.host.replies['search'] = self.search
        for name, fault in (('cut.pdf', 'cut'), ('garbled.pdf', 'garbled'), ('short.pdf', 'short'),
                            ('closed.pdf', 'cut_close'), ('fullTextXML', 'not_xml')):
            self.host.replies[name] = FAULTS[fault]
        self.host.replies['moved.pdf'] = head('302 Found', 'Location: /fine.pdf', 'Content-Length: 0')
        self.host.replies['fine.pdf'] = complete(PDF, 'application/pdf')
        self.proc = subprocess.Popen([sys.executable, '-B', str(tools / 'fault_server.py'),
                                      self.host.base + 'europepmc/'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=no_proxy_environment())
        self.lines = queue.Queue()                  # the server's stdout, line by line, read by a thread
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.addCleanup(self.close)
        self.number = 0

    def read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put('')

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=10)
        error = self.proc.stderr.read()
        self.proc.stderr.close()
        self.proc.stdout.close()
        self.assertEqual(self.proc.returncode, 0, error)
        self.assertNotIn('Traceback', error)

    def search(self, target: str) -> bytes:
        """Europe PMC's search, as the local host plays it: the query names the fault or the full-text file."""
        query = parse_qs(urlsplit(target).query).get('query', [''])[0]
        words = query.replace('(', ' ').replace(')', ' ').split()
        fault = next((word for word in words if word in FAULTS), None)
        if fault is not None:
            return FAULTS[fault]
        number = query.removeprefix('EXT_ID:').split(' ')[0]
        if number == '5':                   # an open-access PMC article: its XML full text, at .../PMC5/fullTextXML
            paper = {'source': 'MED', 'id': number, 'title': 'A paper', 'pmcid': 'PMC5', 'isOpenAccess': 'Y'}
            return complete(json.dumps({'hitCount': 1, 'resultList': {'result': [paper]}}).encode())
        if number == '7':
            # The first PDF mirror is unavailable, and a third mirror should be unnecessary.
            locations = [{'availability': 'Free', 'documentStyle': 'pdf', 'url': self.host.base + path}
                         for path in ('forbidden.pdf', 'fine.pdf', 'unused.pdf')]
            paper = {'source': 'MED', 'id': number, 'title': 'A paper', 'pmcid': 'PMC7', 'isOpenAccess': 'Y',
                     'fullTextUrlList': {'fullTextUrl': locations}}
            return complete(json.dumps({'hitCount': 1, 'resultList': {'result': [paper]}}).encode())
        paper = {'source': 'MED', 'id': number, 'title': 'A paper', 'fullTextUrlList': {'fullTextUrl': [
            {'availability': 'Free', 'documentStyle': 'pdf', 'url': self.host.base + FILES[number]}]}}
        return complete(json.dumps({'hitCount': 1, 'resultList': {'result': [paper]}}).encode())

    def rpc(self, method, params=None):
        self.number += 1
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.number, 'method': method,
                                          'params': params or {}}) + '\n')
        self.proc.stdin.flush()
        try:
            line = self.lines.get(timeout=ANSWER_SECONDS)
        except queue.Empty:
            self.fail(f'the server did not answer {method} within {ANSWER_SECONDS} seconds')
        self.assertTrue(line, 'the server ended')
        reply = json.loads(line)
        self.assertEqual(reply['id'], self.number)
        return reply

    def call(self, name, **arguments):
        result = self.rpc('tools/call', {'name': name, 'arguments': arguments})['result']
        return result['isError'], json.loads(result['content'][0]['text'])

    def rows(self):
        found = {}
        for path in (self.work / '.tmp' / 'literature' / 'calls').glob('*.jsonl'):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                found[row['invocation_id']] = row
        return found

    def test_a_truncated_xml_with_a_valid_start_never_enters_the_library(self):
        self.rpc('initialize', {'protocolVersion': '2025-06-18'})
        raw = b'<article><body><p>' + b'words ' * 1000
        self.host.replies['fullTextXML'] = head('200 OK', 'Content-Type: application/xml', 'Connection: close') + raw
        failed, shown = self.call('acquire_fulltext', paper_ref='MED:5', preferred_format='xml')
        self.assertTrue(failed)
        self.assertNotIn('document_id', shown['data'])
        self.assertEqual(list(self.library.rglob('*')), [])
        self.assertEqual(self.rows()[shown['invocation_id']]['contacted_hosts'], ['127.0.0.1'])

    def test_a_second_pdf_mirror_succeeds_and_both_attempts_are_recorded(self):
        self.rpc('initialize', {'protocolVersion': '2025-06-18'})
        self.host.replies['forbidden.pdf'] = head('403 Forbidden', 'Content-Length: 0')
        _, shown = self.call('acquire_fulltext', paper_ref='MED:7', preferred_format='pdf')
        self.assertEqual(shown['data']['source_sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(shown['coverage']['verified_paper_identifiers'], [])
        self.assertEqual(self.host.seen[1:], ['/forbidden.pdf', '/fine.pdf'])
        provenance = shown['provenance']
        self.assertEqual(provenance['attempt_count'], 3)   # one paper lookup and two downloads
        self.assertEqual(provenance['download_hosts'], ['127.0.0.1', '127.0.0.1'])
        attempts = provenance['download_attempts']
        self.assertEqual([(a['http_status'], a['outcome']) for a in attempts],
                         [(403, 'fulltext_unavailable'), (200, 'acquired')])
        self.assertEqual(self.rows()[shown['invocation_id']]['download_attempts'], attempts)

    def test_the_server_answers_after_every_fault_and_logs_the_faulted_call(self):
        self.assertEqual(self.rpc('initialize', {'protocolVersion': '2025-06-18'})['result']['serverInfo']['name'],
                         'literature')
        faults = [('search_papers', {'query': 'cut'}, 'transport_error'),
                  ('search_papers', {'query': 'garbled'}, 'transport_error'),
                  ('search_papers', {'query': 'long_header'}, 'transport_error'),
                  ('search_papers', {'query': 'short'}, 'incomplete_body'),
                  ('acquire_fulltext', {'paper_ref': 'MED:1', 'preferred_format': 'pdf'}, 'transport_failed'),
                  ('acquire_fulltext', {'paper_ref': 'MED:2', 'preferred_format': 'pdf'}, 'transport_failed'),
                  ('acquire_fulltext', {'paper_ref': 'MED:3', 'preferred_format': 'pdf'}, 'incomplete_body'),
                  ('acquire_fulltext', {'paper_ref': 'MED:6', 'preferred_format': 'pdf'}, 'incomplete_body'),
                  # a body that is not XML, served as XML: the parser's error is no code of its own
                  ('acquire_fulltext', {'paper_ref': 'MED:5', 'preferred_format': 'xml'}, 'fulltext_unavailable'),
                  ('read_document', {'document_id': 'doc_' + 'f' * 64}, 'internal_error')]
        for name, arguments, code in faults:
            with self.subTest(name=name, arguments=arguments):
                failed, shown = self.call(name, **arguments)
                self.assertTrue(failed)
                self.assertEqual(shown['errors'][0]['code'], code)
                self.assertEqual(self.rpc('ping')['result'], {})
                row = self.rows()[shown['invocation_id']]
                self.assertEqual((row['tool'], row['status']), (name, shown['status']))
                if name != 'read_document':
                    self.assertEqual(row['contacted_hosts'], ['127.0.0.1'])
        self.assertEqual(list(self.library.rglob('*')), [])         # no incomplete original was archived
        failed, shown = self.call('acquire_fulltext', paper_ref='MED:4', preferred_format='pdf')
        self.assertEqual(shown['provenance']['download_hosts'], ['127.0.0.1', '127.0.0.1'])     # one redirect
        self.assertEqual(shown['data']['source_sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertTrue((self.library / 'objects' / 'sha256' / hashlib.sha256(PDF).hexdigest()).is_file())
        # PDFs go through Poppler only, even with a pypdf the server could import.
        self.assertEqual(shown['data']['reason']['code'], 'dependency_missing')
        self.assertNotIn('pypdf', shown['data']['parser_version'])
        self.assertIn('poppler', shown['data']['next_step'])
        self.assertEqual(shown['data']['status'], 'unavailable')
        self.assertTrue(failed)
        listed = self.call('list_providers')[1]['data']['providers']
        self.assertFalse(any('optional_env' in row or 'required_env' in row for row in listed))
        self.assertTrue(all(row['credentials'].startswith('none') for row in listed))
        self.assertNotIn('OPENALEX_API_KEY', json.dumps(listed))


FILES = {'1': 'cut.pdf', '2': 'garbled.pdf', '3': 'short.pdf', '4': 'moved.pdf', '6': 'closed.pdf'}


if __name__ == '__main__':
    unittest.main()
