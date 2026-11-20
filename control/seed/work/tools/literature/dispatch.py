"""Stage L's public tool contracts and their bindings to the pinned Literature package."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import transport
from rsi_literature import catalog, documents, parsers, poppler, providers, remote

NATIVE = ('europe_pmc', 'pubmed', 'openalex')
ENABLED = (*NATIVE, 'asta', 'poppler')
DEFAULT = NATIVE[0]
RESPONSE_BYTES = 2 * 1024 * 1024
DOWNLOAD_BYTES = documents.MAX_SOURCE_BYTES
TIMEOUT = 20
XML_TYPES = {'application/xml', 'text/xml', 'application/jats+xml', 'application/tei+xml'}
XML_FORMATS = {'xml', 'jats', 'tei'}


def choice(*values):
    return {'type': 'string', 'enum': list(values)}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 20000}
DOC = {'type': 'string', 'minLength': 1, 'maxLength': 100}
DOCS = {'type': 'array', 'items': DOC, 'minItems': 1, 'maxItems': 25}
CURSOR = {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 20000}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 25}
BYTES = {'type': 'integer', 'minimum': 4, 'maximum': 65536}
NATIVE_PROVIDER = choice(*NATIVE)


def tool(description, required, **properties):
    return {'description': description, 'inputSchema': {'type': 'object', 'properties': properties,
            'required': required.split(), 'additionalProperties': False}}


TOOLS = {
    'list_providers': tool('List the five literature sources and their local readiness.', ''),
    'search_papers': tool('Search for candidate papers. Metadata and abstracts alone do not establish a claim.',
                          'query', query=TEXT, provider=NATIVE_PROVIDER, cursor=CURSOR, limit=LIMIT,
                          filters={'type': 'object'}),
    'get_paper': tool('Resolve a paper identifier and its source locations.', 'paper_ref',
                      paper_ref=TEXT, provider=NATIVE_PROVIDER),
    'get_citations': tool('List references or citing papers. An edge does not establish support.',
                          'paper_ref direction',
                          provider=NATIVE_PROVIDER, paper_ref=TEXT, direction=choice('references', 'citing'),
                          cursor=CURSOR, limit=LIMIT),
    'acquire_fulltext': tool('Acquire a provider-returned original by paper_ref, or a registered local source_id. '
                             'Read the returned document_id for quotable passages.', '',
                             paper_ref=TEXT, source_id=DOC, provider=choice(*NATIVE, 'local'),
                             preferred_format=choice('xml', 'pdf')),
    'read_document': tool('Return exact source passages with their versions, locations, ranges and next cursor.',
                          'document_id', document_id=DOC, document_version=DOC, locator=TEXT, cursor=CURSOR,
                          max_bytes=BYTES),
    'find_passages': tool('Find matching passages in registered documents, with source locations.',
                          'document_ids query', document_ids=DOCS, query=TEXT, limit=LIMIT, max_bytes=BYTES),
    'build_reading_package': tool('List the document reading index or extract PDF text with Poppler.',
                                  'document_id', document_id=DOC, provider=choice('local', 'poppler')),
    'render_document_page': tool('Render one numbered PDF page and deliver its PNG for visual inspection.',
                                 'document_id page', document_id=DOC, page={'type': 'integer', 'minimum': 1,
                                 'maximum': 1000}, provider=choice('poppler')),
    'provider_tools': tool('Discover Asta literature tools from the allowed list.', 'provider',
                           provider=choice('asta'), cursor=CURSOR),
    'call_provider_tool': tool('Call a discovered Asta literature tool using its declared arguments.',
                               'provider tool arguments', provider=choice('asta'),
                               tool=choice(*catalog.PROVIDERS['asta']['tools']), arguments={'type': 'object'}),
}
TOOLS['acquire_fulltext']['inputSchema']['oneOf'] = [
    {'required': ['paper_ref'], 'not': {'required': ['source_id']}},
    {'required': ['source_id'], 'not': {'anyOf': [{'required': ['paper_ref']}, {'required': ['preferred_format']}]}}
]


def validate(value, schema, field='arguments'):
    kinds = {'object': dict, 'integer': int, 'array': list, 'string': str, 'null': type(None)}
    allowed = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    if type(value) not in [kinds[kind] for kind in allowed]:
        raise ValueError(f'{field} has the wrong type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{field} is outside the supported values')
    if isinstance(value, dict):
        if not set(schema.get('required', ())).issubset(value):
            raise ValueError(f'{field} lacks a required field')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - props.keys():
            raise ValueError(f'{field} has unexpected fields')
        for key in props.keys() & value.keys():
            validate(value[key], props[key], key)
    elif isinstance(value, list):
        if not schema['minItems'] <= len(value) <= schema['maxItems']:
            raise ValueError(f'{field} has the wrong number of items')
        for item in value:
            validate(item, schema['items'], field)
    elif isinstance(value, str):
        if not value.strip() or len(value) > schema.get('maxLength', 20000):
            raise ValueError(f'{field} must contain bounded text')
    elif type(value) is int and not schema['minimum'] <= value <= schema['maximum']:
        raise ValueError(f'{field} is outside the allowed range')


def result(status, data=None, *, provider='local', error=None, attempts=0):
    return {'status': status, 'data': data or {}, 'coverage': {},
            'provenance': {'provider': provider, 'attempt_count': attempts},
            'continuation': {'next_cursor': None, 'complete_for_query': status == 'ok'},
            'errors': [error] if error else []}


def failure(code, detail, *, provider='local', status='failed'):
    return result(status, provider=provider, error={'code': code, 'detail': detail, 'retryability': 'unknown'})


def local(value, provider='local'):
    status = value.get('status', 'available')
    response = result({'available': 'ok', 'complete': 'ok'}.get(status, status), value, provider=provider)
    response['coverage'] = value.get('coverage', {})
    response['continuation'] = {'next_cursor': value.get('next_cursor'),
                                'complete_for_query': value.get('complete', status in {'complete', 'available'})}
    return response


def public_location(url):
    if not providers._public_url(url):
        raise ValueError('The provider returned an invalid or credential-bearing location')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.fragment:
        raise ValueError('Full-text locations must use public HTTPS')
    host = parsed.hostname.rstrip('.').lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(labels) < 2 or labels[-1] in {'localhost', 'local', 'internal', 'home', 'lan'}
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
            raise ValueError('Full-text location must name a public Internet host')
    else:
        if not literal.is_global:
            raise ValueError('Full-text location is outside the public network')
        return host
    # Direct DNS is unavailable in a Codex network sandbox. Its loopback HTTP CONNECT
    # proxy resolves public names and enforces the network policy at connection time.
    # Honor that boundary only for a request which urllib will send through the proxy.
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        proxy_url = urllib.request.getproxies().get('https', '')
        proxy = urllib.parse.urlsplit(proxy_url)
        if (proxy.scheme != 'http' or proxy.hostname not in {'127.0.0.1', '::1'}
                or not proxy.port or proxy.username is not None or urllib.request.proxy_bypass(host)):
            raise ValueError('Cannot verify public routing for the full-text location')
        return host
    if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
        raise ValueError('Full-text location resolves outside the public network')
    return host


def media_format(raw, content_type, declared):
    media = content_type.split(';')[0].strip().lower()
    if declared == 'pdf':
        if media == 'application/pdf' and raw.lstrip().startswith(b'%PDF-'):
            return media
    elif declared in XML_FORMATS:
        if media in XML_TYPES and b'<!ENTITY' not in raw.replace(b'\0', b'').upper():
            # The upstream reader also validates the document. Check the root here to avoid
            # treating an HTML error page or a non-article XML response as a full-text original.
            parser = ET.XMLPullParser(events=('start',))
            for step in range(0, len(raw), 4096):
                parser.feed(raw[step:step + 4096])
                for _, root in parser.read_events():
                    if root.tag.split('}')[-1].lower() in {'article', 'article-set', 'tei', 'pmc-articleset'}:
                        return 'application/xml'
                    raise ValueError('The XML response does not contain an article')
    elif declared == 'html' and media in {'text/html', 'application/xhtml+xml'}:
        return 'text/html'
    raise ValueError('Full-text bytes or media type do not match the selected format')


CREDENTIALS = 'none: this server reads no API key, e-mail address or other credential and sends none'
PDF_NEXT_STEP = ('This server reads PDFs with Poppler only (pypdf is never used): call build_reading_package with '
                 'provider "poppler" for the text, or render_document_page for a page image.')
SEARCH_TOOLS = ('search_papers', 'get_paper', 'get_citations')
ASTA_TOOLS = ('provider_tools', 'call_provider_tool')


def providers_list():
    """The enabled sources as list_providers reports them, without credentials this server never reads."""
    rows = []
    for row in catalog.provider_catalog({'enabled_providers': list(ENABLED)}, env={}):
        if row['provider'] not in ENABLED:
            continue
        row = {key: value for key, value in row.items() if key not in ('optional_env', 'required_env')}
        row['credentials'] = CREDENTIALS
        if row['provider'] == 'openalex':
            row['limitation'] = ('Anonymous access only: this server sends no API key, so continuation and content '
                                 'access may be unavailable.')
        rows.append(row)
    return rows


def _pdf_next_step(response):
    data = response.get('data')
    reason = data.get('reason') if isinstance(data, dict) else None
    if isinstance(reason, dict) and reason.get('code') == 'dependency_missing':
        data['next_step'] = PDF_NEXT_STEP
    return response


class Literature:
    def __init__(self, runtime, *, native=None, http=None, resolve=None):
        self.runtime = runtime
        self.hosts = []         # the hosts the current call contacted, for its log row if it ends in an exception
        self.addressed = 'local'
        if native is None:
            # Local servers use no inherited provider credentials. Network routing remains the sandbox proxy's job.
            native = providers.ProviderClient(self._provider_http, timeout=TIMEOUT, max_response_bytes=RESPONSE_BYTES,
                                              env={})
            native._rate_limit = True      # upstream paces requests only for its own transport; keep that pacing
        self.native = native
        self._http = http if http is not None else transport.fetch
        self.resolve = resolve if resolve is not None else public_location

    def _contact(self, url):
        try:
            host = urllib.parse.urlsplit(url).hostname
        except (TypeError, ValueError):
            host = None
        self.hosts.append(host or 'unknown')

    def http(self, method, url, headers, body, timeout, max_bytes):
        self._contact(url)
        return self._http(method, url, headers, body, timeout, max_bytes)

    def _provider_http(self, method, url, headers, timeout, max_bytes):
        self._contact(url)
        return transport.provider_fetch(method, url, headers, timeout, max_bytes)

    def contacted(self):
        """Provenance for a call that ended without its own: the source it addressed and the hosts it contacted."""
        hosts = sorted(set(self.hosts))[:32]
        return {'provider': self.addressed, 'attempt_count': len(self.hosts), 'download_hosts': hosts}

    def _acquire(self, args):
        if ('paper_ref' in args) == ('source_id' in args):
            raise ValueError('Supply exactly one of paper_ref or source_id')
        if 'source_id' in args:
            if 'preferred_format' in args or args.get('provider', 'local') != 'local':
                raise ValueError('Local source_id accepts only the local provider and no format preference')
            raw, media, metadata = self.runtime.local_source(args['source_id'])
            manifest = self.runtime.ingest(raw, media, metadata)
            return _pdf_next_step(local(self.runtime.store.reading_package(manifest['document_id'])))
        provider = args.get('provider', DEFAULT)
        if provider not in NATIVE:
            raise ValueError('Select a native provider for paper_ref')
        found = self.native.fulltext_candidates(provider, args['paper_ref'])
        if found['status'] not in {'ok', 'partial'}:
            return found
        preferred = args.get('preferred_format')
        candidates = [c for c in found['data'].get('candidates', [])
                      if not preferred or c['format'] == preferred or preferred == 'xml' and c['format'] in XML_FORMATS]
        available = [c for c in candidates if not c.get('required_configuration')]
        receipt = dict(found['provenance'])
        receipt['download_hosts'] = []
        if not available:
            code = 'configuration_required' if candidates else 'fulltext_format_unavailable'
            response = failure(code, 'No accessible candidate matches this request.', provider=provider,
                               status='unavailable')
        else:
            candidate = min(available, key=lambda c: (c['format'] not in XML_FORMATS, c['format'] != 'pdf'))
            try:
                response = self._fetch(candidate, args['paper_ref'], provider, receipt)
            except documents.DocumentError as exc:
                status = 'unavailable' if exc.code == 'trial_cache_miss' else 'failed'
                response = failure(exc.code, str(exc), provider=provider, status=status)
            except OSError:
                response = failure('source_storage_error', 'The downloaded original could not be stored.',
                                   provider=provider)
        response['provenance'].update(receipt)
        return response

    def _fetch(self, candidate, paper_ref, provider, receipt):
        url = candidate['url']
        try:
            for _ in range(3):
                host = self.resolve(url)
                receipt['download_hosts'].append(host)
                receipt['attempt_count'] += 1
                reply = self.http('GET', url, {'Accept': 'application/xml, application/pdf, text/html',
                                  'User-Agent': 'RSI-MOL-Literature/1.0'}, None, TIMEOUT, DOWNLOAD_BYTES)
                headers = {k.lower(): v for k, v in reply['headers'].items()}
                if reply['status'] in {301, 302, 303, 307, 308} and headers.get('location'):
                    url = urllib.parse.urljoin(url, headers['location'])
                    continue
                if reply['status'] != 200:
                    return failure('fulltext_unavailable', f"Full-text server returned HTTP {reply['status']}.",
                                   provider=provider, status='unavailable')
                raw = reply['body']
                if not isinstance(raw, bytes) or not 0 < len(raw) <= DOWNLOAD_BYTES:
                    raise ValueError('Full-text response is empty or exceeds its byte limit')
                content_type = headers.get('content-type', '')
                media = media_format(raw, content_type, candidate['format'])
                break
            else:
                raise ValueError('Full-text redirect limit reached')
        except (remote.RemoteError, OSError, ValueError, ET.ParseError) as exc:
            return failure(getattr(exc, 'code', 'fulltext_unavailable'),
                           'The public endpoint or its response did not meet the download contract.',
                           provider=provider, status='unavailable')
        manifest = self.runtime.ingest(raw, media, {'provider': provider, 'paper_ref': paper_ref,
            'source_url': url, 'acquired_media_type': content_type, 'content_scope': 'unknown',
            'provider_reported_scope': candidate.get('content_scope', 'unknown')})
        return _pdf_next_step(local(self.runtime.store.reading_package(manifest['document_id']), provider))

    def _asta(self, name, args):
        spec = catalog.PROVIDERS['asta']
        client = remote.MCPClient({'endpoint': spec['endpoint'], 'tools': spec['tools'],
                                  'timeout': TIMEOUT, 'max_response_bytes': RESPONSE_BYTES},
                                 transport=self.http, env={})
        try:
            data = (client.list_tools(args.get('cursor')) if name == 'provider_tools'
                    else client.call_tool(args['tool'], args['arguments']))
        except remote.RemoteError as exc:
            response = failure(exc.code, str(exc), provider='asta',
                               status='failed' if exc.dispatched else 'unavailable')
        else:
            response = result('failed' if data.get('isError') else 'ok', data, provider='asta')
            response['coverage'] = {'scope': 'provider_tool_response', 'original_paper_read': False}
            response['continuation'] = {'next_cursor': data.get('next_cursor'),
                                        'complete_for_query': not data.get('next_cursor') and not data.get('isError')}
        response['provenance']['attempt_count'] = client.attempt_count
        return response

    def _document(self, name, args, invocation_id):
        store = self.runtime.store
        if name == 'find_passages':
            for document_id in args['document_ids']:
                self.runtime.ensure(document_id)
            return local(store.find(args['document_ids'], args['query'], limit=args.get('limit', 5),
                                    max_bytes=args.get('max_bytes', 8000))), None
        document_id = args['document_id']
        manifest = self.runtime.ensure(document_id)
        if name == 'read_document':
            if args.get('document_version', manifest['document_version']) != manifest['document_version']:
                raise ValueError('The requested document version does not match the original')
            return local(store.read(document_id, locator=args.get('locator'), cursor=args.get('cursor'),
                                    max_bytes=args.get('max_bytes', 8000))), None
        if name == 'build_reading_package':
            if args.get('provider', 'local') == 'local':
                return local(store.reading_package(document_id)), None
            response = parsers.run_parser('poppler', {}, store, document_id, invocation_id)
            if derived_id := response['data'].get('document_id'):
                derived = self.runtime.publish_derived(derived_id)
                response['data'].update(source_sha256=derived['source_sha256'],
                                        original_sha256=manifest['source_sha256'])
            return response, None
        response = poppler.render_document_page({}, {'document_id': document_id, 'page': args['page']},
                                                 store, invocation_id)
        if response['status'] != 'ok':
            return response, None
        descriptor = response['data']['image']
        relative = 'jobs/' + invocation_id + '/page.png'
        if Path(descriptor.pop('artifact_path')) != store.root / relative or descriptor['page'] != args['page']:
            raise ValueError('Rendered page identity does not match this request')
        raw = parsers._read_file(store.root, relative, maximum=1024 * 1024)
        if len(raw) != descriptor['bytes'] or hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
            raise ValueError('Rendered page no longer matches its recorded hash')
        # The upstream renderer only describes the file; this server attaches the PNG to the reply.
        response['coverage']['image_delivered'] = True
        return response, {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(raw).decode('ascii')}

    @staticmethod
    def _addressing(name, args):
        """The source a call addresses, as its log row names it when the call ends without a reply of its own."""
        if name in ASTA_TOOLS:
            return 'asta'
        provider = args.get('provider') if isinstance(args, dict) else None
        if name in SEARCH_TOOLS or name == 'acquire_fulltext' and 'paper_ref' in args:
            return provider if provider in NATIVE else DEFAULT
        return provider if provider in ('local', 'poppler') else 'local'

    def call(self, name, args, invocation_id):
        self.hosts, self.addressed = [], 'local'
        if not isinstance(name, str) or name not in TOOLS:
            raise ValueError('Unknown literature tool')
        validate(args, TOOLS[name]['inputSchema'])
        if len(json.dumps(args, ensure_ascii=False).encode()) > 128 * 1024:
            raise ValueError('Literature arguments exceed 128 KiB')
        self.addressed = self._addressing(name, args)
        if name == 'list_providers':
            return result('ok', {'providers': providers_list()}), None
        provider = args.get('provider', DEFAULT)
        if name == 'search_papers':
            return self.native.search(provider, args['query'], args.get('filters'), args.get('cursor'),
                                      args.get('limit', 10)), None
        if name == 'get_paper':
            return self.native.get_paper(provider, args['paper_ref']), None
        if name == 'get_citations':
            direction = 'cited_by' if args['direction'] == 'citing' else 'references'
            response = self.native.get_citations(provider, args['paper_ref'], direction,
                                                 args.get('cursor'), args.get('limit', 10))
            response['provenance']['direction'] = args['direction']
            return response, None
        if name == 'acquire_fulltext':
            return self._acquire(args), None
        if name in {'provider_tools', 'call_provider_tool'}:
            return self._asta(name, args), None
        return self._document(name, args, invocation_id)
