"""Exercise the real stdio service with recorded-size, offline provider responses."""
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rsi.check import _pdf

SEED = Path(__file__).resolve().parents[1] / 'seed' / 'work' / 'tools' / 'literature'
ARTICLE = ('<article><body><sec><title>Methods</title><p>Offset checking uses '
           'caf\u00e9. Each record has a location.</p></sec></body></article>').encode()
DRIVER = '''
import json, sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from dispatch import Literature
from runtime import Runtime
from server import serve
from rsi_literature import providers

root = Path(__file__).resolve().parents[2]
paper = {'source': 'MED', 'id': '123', 'pmcid': 'PMC456', 'doi': '10.1234/Example', 'isOpenAccess': 'Y',
         'title': 'A paper',
         'fullTextUrlList': {'fullTextUrl': [{'availability':'Free', 'url':'https://paper.example/file.pdf',
                                              'documentStyle':'pdf'}]}}

def native(method, url, headers, timeout, maximum):
    parsed = urlsplit(url)
    if parsed.path.endswith('/citations'):
        data = {'hitCount':1, 'citationList':{'citation':[paper]}}
    elif 'missing' in parse_qs(parsed.query).get('query', [''])[0]:
        data = {'hitCount':0, 'resultList':{'result':[]}}
    else:
        data = {'hitCount':1, 'resultList':{'result':[paper]}}
    return {'status':200, 'body':json.dumps(data).encode(), 'headers':{}}

def http(method, url, headers, body, timeout, maximum):
    if method == 'GET':
        if url.endswith('/file.pdf'):
            if (root/'denied').exists():
                return {'status':403, 'headers':{}, 'body':b''}
            return {'status':200, 'headers':{'content-type':'application/pdf'}, 'body':b'<html>not PDF</html>'}
        return {'status':200, 'headers':{'content-type':'application/xml'}, 'body':(root/'article.xml').read_bytes()}
    msg = json.loads(body)
    name = msg['method']
    if (root/'asta_denied').exists():
        return {'status':503, 'headers':{}, 'body':b''}
    if name == 'initialize':
        value = {'protocolVersion':'2025-03-26', 'capabilities':{'tools':{}}}
    elif name == 'notifications/initialized':
        return {'status':202, 'headers':{}, 'body':b''}
    elif name == 'tools/list':
        value = {'tools':[{'name':'get_paper', 'inputSchema':{}}, {'name':'upload'}], 'nextCursor':'next'}
    else:
        value = {'content':[{'type':'text', 'text':'Asta paper details'}]}
    return {'status':200, 'headers':{}, 'body':json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':value}).encode()}

runtime = Runtime(root)
serve(runtime, sys.stdin.buffer, sys.stdout.buffer,
      service=Literature(runtime, native=providers.ProviderClient(native, env={}), http=http,
                         resolve=lambda url: urlsplit(url).hostname))
'''


class LiteratureServer(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve() / 'work'
        path = self.work / 'tools' / 'literature'
        shutil.copytree(SEED, path, ignore=shutil.ignore_patterns('__pycache__'))
        (path / 'fixture_server.py').write_text(DRIVER)
        (self.work.parent / 'library').mkdir()
        (self.work / 'article.xml').write_bytes(ARTICLE)
        pdf = _pdf('Known Page One')
        self.pdf_hash = hashlib.sha256(pdf).hexdigest()
        (self.work / 'known.pdf').write_bytes(pdf)
        (path / 'sources.json').write_text(json.dumps({'known': {'path': 'known.pdf', 'sha256': self.pdf_hash,
                                                                 'media_type': 'application/pdf'}}))
        self.proc = subprocess.Popen([sys.executable, '-B', str(path / 'fixture_server.py')], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close)
        self.counter = 0

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        error = self.proc.stderr.read()
        self.proc.stderr.close()
        self.proc.stdout.close()
        self.assertEqual(self.proc.returncode, 0, error)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + '\n')
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.counter += 1
        self.send({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params})
        reply = self.proc.stdout.readline()
        self.assertTrue(reply, self.proc.stderr.read() if not reply else '')
        reply = json.loads(reply)
        self.assertEqual(reply['id'], self.counter)
        return reply

    def initialize(self):
        init = self.rpc('initialize', {'protocolVersion': '2025-06-18'})['result']
        self.assertEqual(init['protocolVersion'], '2025-06-18')
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def call(self, name, **arguments):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments})['result']

    def payload(self, name, **arguments):
        return json.loads(self.call(name, **arguments)['content'][0]['text'])

    def rows(self):
        return {row['invocation_id']: row for path in (self.work / '.tmp/literature/calls').glob('*.jsonl')
                for row in map(json.loads, path.read_text().splitlines())}

    def test_wrong_article_ids_are_refused_before_archiving(self):
        self.initialize()
        for kind, other in [('doi', '10.1234/other'), ('pmid', '999'), ('pmc', '999'), ('pmcid', 'PMC999')]:
            with self.subTest(kind=kind):
                article = (f'<article><front><article-meta><article-id pub-id-type="{kind}">{other}</article-id>'
                           '</article-meta></front><body><p>Another paper.</p></body></article>')
                (self.work / 'article.xml').write_text(article)
                response = self.payload('acquire_fulltext', paper_ref='MED:123', preferred_format='xml')
                self.assertEqual(response['errors'][0]['code'], 'identity_mismatch')
                self.assertNotIn('document_id', response['data'])
                self.assertEqual(list((self.work.parent / 'library').iterdir()), [])

    def test_fulltext_compares_only_identifiers_of_the_article_itself(self):
        self.initialize()
        article = ('<article-set xmlns="urn:jats"><article><front><article-meta>'
                   '<article-id pub-id-type="DOI">https://doi.org/10.1234/EXAMPLE</article-id>'
                   '<article-id pub-id-type="pmid">123</article-id>'
                   '<article-id pub-id-type="pmcaid">456</article-id>'
                   '<article-id pub-id-type="pmcid">pmc456</article-id></article-meta></front>'
                   '<body><p>Original article.</p></body><back><ref-list><ref><element-citation>'
                   '<pub-id pub-id-type="doi">10.1234/other</pub-id></element-citation></ref></ref-list></back>'
                   '<sub-article><front-stub><article-id pub-id-type="pmid">999</article-id></front-stub>'
                   '</sub-article></article></article-set>')
        (self.work / 'article.xml').write_text(article)
        response = self.payload('acquire_fulltext', paper_ref='MED:123', preferred_format='xml')
        self.assertEqual(response['status'], 'ok', response)
        self.assertEqual(response['coverage']['verified_paper_identifiers'], ['doi', 'pmcid', 'pmid'])
        self.assertEqual(response['data']['source_sha256'], hashlib.sha256(article.encode()).hexdigest())

    def test_asta_records_contacted_hosts_on_success_and_failure(self):
        self.initialize()
        listed = self.payload('provider_tools', provider='asta')
        called = self.payload('call_provider_tool', provider='asta', tool='get_paper', arguments={'paper_id': '123'})
        (self.work / 'asta_denied').touch()
        denied = self.payload('provider_tools', provider='asta')
        rows = self.rows()
        for result, expected_attempts in ((listed, 3), (called, 3), (denied, 1)):
            self.assertEqual(result['provenance']['attempt_count'], expected_attempts)
            self.assertEqual(rows[result['invocation_id']]['contacted_hosts'], ['asta-tools.allen.ai'])
        self.assertNotEqual(denied['status'], 'ok')

    def test_session_and_all_reading_operations(self):
        self.initialize()
        self.assertEqual(len(self.rpc('tools/list', {})['result']['tools']), 11)
        self.assertEqual(self.rpc('unknown', {})['error']['code'], -32601)
        providers = self.payload('list_providers')['data']['providers']
        self.assertEqual({row['provider'] for row in providers},
                         {'europe_pmc', 'pubmed', 'openalex', 'asta', 'poppler'})
        self.assertEqual(self.payload('search_papers', query='a question')['data']['papers'][0]['pmcid'], 'PMC456')
        self.assertEqual(self.payload('search_papers', query='missing')['data']['papers'], [])
        self.assertEqual(self.payload('get_paper', paper_ref='PMID:123')['data']['paper']['provider_id'], 'MED:123')
        response = self.payload('acquire_fulltext', paper_ref='PMC456', preferred_format='xml')
        self.assertEqual(response['status'], 'ok', response)
        self.assertEqual(response['coverage']['verified_paper_identifiers'], [])
        self.assertRegex(response['invocation_id'], r'^[a-f0-9]{32}$')
        doc = response['data']['document_id']
        digest = hashlib.sha256(ARTICLE).hexdigest()
        self.assertEqual(response['data']['source_sha256'], digest)
        self.assertEqual(response['provenance']['download_hosts'], ['www.ebi.ac.uk'])
        page = self.payload('read_document', document_id=doc, max_bytes=20)
        first = page['data']['passages'][0]
        self.assertEqual(first['source_sha256'], digest)
        self.assertTrue(first['locator'])
        cursor = page['continuation']['next_cursor']
        second = self.payload('read_document', document_id=doc, max_bytes=20, cursor=cursor)
        self.assertNotEqual(first['char_range'], second['data']['passages'][0]['char_range'])
        self.assertTrue(self.call('read_document', document_id=doc, document_version='wrong')['isError'])
        matches = self.payload('find_passages', document_ids=[doc], query='record')
        self.assertTrue(matches['data']['passages'])
        citations = self.payload('get_citations', paper_ref='MED:123', direction='citing')
        self.assertEqual(citations['data']['papers'][0]['pmcid'], 'PMC456')
        self.assertEqual(citations['provenance']['direction'], 'citing')
        asta = self.payload('provider_tools', provider='asta')
        self.assertEqual([t['name'] for t in asta['data']['tools']], ['get_paper'])
        self.assertEqual(asta['continuation']['next_cursor'], 'next')
        self.assertTrue(self.call('call_provider_tool', provider='asta', tool='upload', arguments={})['isError'])
        allowed = self.payload('call_provider_tool', provider='asta', tool='get_paper', arguments={'paper_id': '123'})
        self.assertEqual(allowed['status'], 'ok', allowed)
        self.assertTrue(self.call('acquire_fulltext', paper_ref='PMC456', preferred_format='pdf')['isError'])
        (self.work / 'denied').touch()
        forbidden = self.payload('acquire_fulltext', paper_ref='PMC456', preferred_format='pdf')
        self.assertIn('403', forbidden['errors'][0]['detail'])

    def test_protocol_errors_allow_another_call(self):
        self.proc.stdin.write('{no json}\n')
        self.proc.stdin.flush()
        self.assertEqual(json.loads(self.proc.stdout.readline())['error']['code'], -32700)
        self.initialize()
        for name, fields in [('missing', {}), ('search_papers', {'query': 'a', 'limit': True}),
                             ('acquire_fulltext', {}), ('get_paper', {'provider': [], 'paper_ref': 'x'})]:
            self.assertTrue(self.call(name, **fields)['isError'])
        self.assertFalse(self.call('list_providers')['isError'])

    def test_download_checks_every_destination_and_uses_sandbox_proxy_dns(self):
        # Codex resolves names in its managed HTTP CONNECT proxy. A denied direct
        # DNS lookup may only proceed there when urllib would use that proxy.
        script = """
from unittest.mock import patch
import socket
from dispatch import public_location
external = 'https://www.ebi.ac.uk/fulltext'
proxy = {'https': 'http://127.0.0.1:32000'}
def denied(url):
    try:
        public_location(url)
    except ValueError:
        return
    raise AssertionError('Location accepted')
with patch('dispatch.socket.getaddrinfo', side_effect=socket.gaierror()):
    with patch('dispatch.urllib.request.getproxies', return_value=proxy):
        with patch('dispatch.urllib.request.proxy_bypass', return_value=False):
            assert public_location(external) == 'www.ebi.ac.uk'
            for url in ('http://example.org/a', 'https://127.0.0.1/a', 'https://local/a',
                        'https://research.local/a', 'https://user:password@example.org/a',
                        'https://example.org/a?api_key=private'):
                denied(url)
        with patch('dispatch.urllib.request.proxy_bypass', return_value=True):
            denied(external)
    with patch('dispatch.urllib.request.getproxies', return_value={}):
        denied(external)
with patch('dispatch.socket.getaddrinfo', return_value=[(None,None,None,None,('127.0.0.1',443))]):
    denied(external)
"""
        run = subprocess.run([sys.executable, '-B', '-c', script], cwd=self.work / 'tools' / 'literature',
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(all(shutil.which(name) for name in ('pdftotext', 'pdfinfo', 'pdftoppm')), 'requires Poppler')
    def test_real_poppler_returns_page_bytes_and_derived_text(self):
        self.initialize()
        pdf = self.payload('acquire_fulltext', source_id='known')
        doc = pdf['data']['document_id']
        extracted = self.payload('build_reading_package', provider='poppler', document_id=doc)
        self.assertEqual(extracted['data']['original_sha256'], self.pdf_hash, extracted)
        read = self.payload('read_document', document_id=extracted['data']['document_id'])
        self.assertIn('Known Page One', read['data']['text'])
        page = self.call('render_document_page', document_id=doc, page=1)
        self.assertFalse(page['isError'], page)
        data = json.loads(page['content'][0]['text'])['data']['image']
        self.assertIs(json.loads(page['content'][0]['text'])['coverage']['image_delivered'], True)
        raw = base64.b64decode(page['content'][1]['data'], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), data['sha256'])
        self.assertEqual((data['page'], data['source_sha256']), (1, self.pdf_hash))
        self.assertEqual(page['content'][1]['mimeType'], 'image/png')
