"""The Literature stdio MCP server. stdout carries newline-delimited JSON-RPC messages.

A call that fails in any way is answered with a tool error and still gets its log row; the process ends only at the
end of its input.
"""
from __future__ import annotations

import sys

# PDFs go through Poppler only (design 5.10). The pinned reader would parse a PDF in-process with pypdf if the Lab
# installed it in env/; block the import before anything can make it, so the upstream modules stay unchanged.
sys.modules['pypdf'] = None

import hashlib  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

from dispatch import Literature, RESPONSE_BYTES, TOOLS, failure, local  # noqa: E402
from rsi_literature.documents import DocumentError  # noqa: E402
from runtime import Runtime  # noqa: E402

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_MESSAGE = 256 * 1024
KEPT_DATA = ('document_id', 'document_version', 'source_sha256')     # what a replaced reply keeps of its data
MEMORY_EVIDENCE_TOOLS = ('read_document', 'find_passages', 'get_paper', 'search_papers', 'get_citations',
                         'acquire_fulltext')
# Keep the independently installable memory reader and literature server under the
# same native record limits, including JSON wrapping of an escape-heavy MCP result.
NATIVE_RECEIPT_BYTES = 48 * 1024
NATIVE_TEXT_BYTES = 64 * 1024 - 256


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def text_result(response, invocation_id):
    text = encode({**response, 'invocation_id': invocation_id}).decode()
    return {'content': [{'type': 'text', 'text': text}], 'isError': response['status'] in {'failed', 'unavailable'}}


def fits_receipt(response, invocation_id):
    wire = encode(text_result(response, invocation_id))
    return (len(wire) <= NATIVE_RECEIPT_BYTES
            and len(wire) + wire.count(b'"') + wire.count(b'\\') <= NATIVE_TEXT_BYTES)


def read_coverage(coverage):
    """Keep extraction flags and give explicit omission counts for verbose document-wide coverage."""
    budget = NATIVE_RECEIPT_BYTES // 8
    if len(encode(coverage)) <= budget:
        return coverage
    compact = dict(coverage)
    # Poppler can emit one warning per scanned page. Even a single large warning
    # (or one large entry in a derived document's coverage) must leave room for text.
    limitations = compact.get('limitations', [])
    if limitations:
        kept = [message for message in limitations[:2] if len(encode(message)) <= budget // 4]
        compact.update(limitations=kept, omitted_limitations=len(limitations) - len(kept))
    omitted = {}
    for key in sorted(compact, key=lambda k: len(encode(compact[k])), reverse=True):
        if len(encode(compact)) <= budget:
            break
        value = compact.pop(key)
        omitted[key] = len(value) if isinstance(value, list) else 1
    if omitted:
        compact['omitted_coverage_fields'] = omitted
    return compact


def read_page(store, response, invocation_id):
    """Fit a contiguous source range and issue a store cursor for exactly the text delivered."""
    data = response['data']
    passages = data.get('passages', [])
    # A caller's text budget can already have cut the last block before the MCP budget is applied.
    count = len(passages)
    if count > 1 and not passages[-1]['block_complete']:
        count -= 1
    if count == len(passages) and fits_receipt(response, invocation_id):
        return response

    coverage = read_coverage(data['coverage'])
    data = {**data, 'coverage': coverage}
    if len(passages) > 1:
        # The normalized PDF stores all word boxes. On an oversized multi-line
        # page keep each line's coordinates; a read of its locator returns the words.
        compact = []
        for row in passages:
            location = row.get('source_locator')
            if isinstance(location, dict) and location.get('words'):
                line = {k: v for k, v in location.items() if k != 'words'}
                row = {**row, 'source_locator': {**line, 'omitted_word_boxes': len(location['words'])}}
            compact.append(row)
        passages = compact
        data = {**data, 'passages': passages}
    # The store repeats the same document metadata on every passage. An optional
    # title, license or extraction report can itself exceed a complete receipt.
    # Preserve the actual paper/source binding and the parent of any derivation.
    if passages:
        metadata = passages[0]['source_metadata']
        if len(encode(metadata)) > NATIVE_RECEIPT_BYTES // 4:
            fields = ('provider', 'paper_id', 'paper_ref', 'source_id', 'source_url', 'derived_from',
                      'parser_identity', 'extraction_coverage', 'content_scope')
            kept = {key: metadata[key] for key in fields if key in metadata}
            if isinstance(kept.get('extraction_coverage'), dict):
                kept['extraction_coverage'] = read_coverage(kept['extraction_coverage'])
            omitted = sorted(metadata.keys() - fields)
            if kept != metadata:
                passages = [{**p, 'source_metadata': kept, 'omitted_source_metadata_fields': omitted} for p in passages]
                data = {**data, 'passages': passages}
    if count == len(passages) and fits_receipt(local(data), invocation_id):
        return local(data)
    if not passages:
        return local(data)

    start = data['char_range'][0]
    selection = data['selection']

    def prefix(end, rows):
        if end == data['char_range'][1]:
            return local({**data, 'passages': rows})
        chunk = data['text'][:end - start]
        low, high = selection['char_range']
        cursor = store._cursor({'document_id': data['document_id'], 'document_version': data['document_version'],
                                'start': low, 'end': high, 'offset': end, 'locator': selection['locator']})
        return local({**data, 'status': 'partial', 'text': chunk, 'returned_bytes': len(chunk.encode()),
                      'char_range': [start, end], 'passages': rows, 'next_cursor': cursor,
                      'coverage': {**coverage, 'selected_range_complete': False, 'entire_normalized_text_returned': False}})

    # Each proper prefix ends immediately before the following block, including
    # the separator in its text. The next page then starts at the exact block start.
    low, high = 1, min(count, len(passages) - 1)
    best = None
    while low <= high:
        mid = (low + high) // 2
        page = prefix(passages[mid]['char_range'][0], passages[:mid])
        if fits_receipt(page, invocation_id):
            best, low = page, mid + 1
        else:
            high = mid - 1
    if best is not None:
        return best

    # The leading block alone exceeds the receipt. Keep the canonical source
    # ranges and PDF word boxes from the pinned reader even for a partial block.
    first = passages[0]
    manifest, text = store._load(data['document_id'])
    block = next(b for b in manifest['blocks'] if b['locator'] == first['locator'])
    begin, high = first['char_range']
    low = begin + 1
    while low <= high:
        end = (low + high) // 2
        row = store._passage(manifest, text, block, begin, end)
        if 'omitted_source_metadata_fields' in first:
            row.update(source_metadata=first['source_metadata'],
                       omitted_source_metadata_fields=first['omitted_source_metadata_fields'])
        page = prefix(end, [row])
        if fits_receipt(page, invocation_id):
            best, low = page, end + 1
        else:
            high = end - 1
    if best is not None:
        return best
    # Some originals carry an enormous locator or section path even for one
    # character. Continue the readable text without claiming a provenanced passage.
    end = min(begin + 1024, first['char_range'][1])
    page = prefix(end, [])
    page['data']['omitted_passage'] = {'locator': first['locator'], 'reason': 'Passage metadata exceeds the receipt budget.'}
    return page


def paper_page(name, response, invocation_id, cursor):
    """Keep the provider page and cursor intact, shortening individual abstracts only as necessary."""
    if response['status'] not in ('ok', 'partial') or (not cursor and fits_receipt(response, invocation_id)):
        return response
    key = 'paper' if name == 'get_paper' else 'papers'
    papers = [response['data'][key]] if key == 'paper' else response['data'][key]
    rows = [dict(paper) for paper in papers]
    page = {**response, 'data': {**response['data'], key: rows[0] if key == 'paper' else rows}}
    abstracts = [(i, p['abstract']) for i, p in enumerate(papers) if p.get('abstract')]
    digests = {i: hashlib.sha256(encode([papers[i]['paper_id'], text])).hexdigest() for i, text in abstracts}
    start = 0
    if cursor:
        digest, _, offset = cursor.partition(':')
        if (not offset.isascii() or not offset.isdecimal() or digest != digests.get(0)
                or not 0 < int(offset) < len(papers[0]['abstract'])):
            raise DocumentError('invalid_cursor', 'The abstract cursor does not match this paper and text. '
                                                   'Call get_paper again without the cursor.')
        start = int(offset)

    def show(i, text, length):
        end = min(len(text), start + length)
        row = rows[i]
        row['abstract'] = text[start:end]
        if start or end < len(text):
            row.update(abstract_truncated=bool(papers[i].get('abstract_truncated') or end < len(text)),
                       abstract_range=[start, end], abstract_length=len(text))
            if end < len(text):
                row['next_abstract_cursor'] = f'{digests[i]}:{end}'
            else:
                row.pop('next_abstract_cursor', None)
        else:
            for field in ('abstract_range', 'abstract_length', 'next_abstract_cursor', 'abstract_truncated'):
                row.pop(field, None)
            if 'abstract_truncated' in papers[i]:
                row['abstract_truncated'] = papers[i]['abstract_truncated']

    # Reserve a readable excerpt for every paper. Spend remaining space on whole
    # abstracts, shortest first; ties retain the provider's relevance ordering.
    preview = 160
    for i, text in abstracts:
        show(i, text, preview)
    if not fits_receipt(page, invocation_id):
        keep = {'paper_id', 'provider', 'provider_id', 'doi', 'pmid', 'pmcid', 'year',
                'abstract', 'abstract_truncated', 'content_scope'}
        # Keep titles longest. If they alone overflow, omit entire fields so the
        # registrar never treats a clipped title as a new paper observation.
        optional = sorted((field == 'title', field == 'url', -len(encode(value)), i, field)
                          for i, paper in enumerate(papers) for field, value in paper.items() if field not in keep)
        for _, _, _, i, field in optional:
            if rows[i].pop(field) not in (None, '', [], {}):
                rows[i].setdefault('omitted_fields', []).append(field)
            if fits_receipt(page, invocation_id):
                break
        while preview > 1 and not fits_receipt(page, invocation_id):
            preview = max(1, preview // 2)
            for i, text in abstracts:
                show(i, text, preview)
    for i, text in sorted(abstracts, key=lambda entry: len(encode(entry[1]))):
        length = len(text) - start
        if length <= preview:
            continue
        show(i, text, length)
        if fits_receipt(page, invocation_id):
            continue
        low, high = preview, length - 1
        while low < high:
            mid = (low + high + 1) // 2
            show(i, text, mid)
            if fits_receipt(page, invocation_id):
                low = mid
            else:
                high = mid - 1
        show(i, text, low)
    return page


def compact_metadata(name, response):
    """Keep the same acquired document or the complete provider page and its original cursor."""
    if response['status'] not in ('ok', 'partial'):
        return response
    data = dict(response['data'])
    if name == 'acquire_fulltext':
        # build_reading_package can fetch the full navigation index separately.
        omitted = {}
        for key in ('references', 'sections', 'pages'):
            if isinstance(data.get(key), list):
                omitted[key] = len(data.pop(key))
        if omitted:
            data['omitted_reading_index_items'] = omitted
    return {**response, 'data': data}


def reject_constant(_):
    raise ValueError('Non-finite number')


def error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class Session:
    def __init__(self, service):
        self.service = service
        self.initialized = False

    def _contacted(self, response):
        """A failure made here names the source the call addressed and the hosts it contacted, as its log row must."""
        response['provenance'] = self.service.contacted()
        return response

    def _replaced(self, response, invocation_id):
        """The reply that stands in for one over RESPONSE_BYTES: the same provenance (the sources and hosts that
        were contacted) and the identity of a document that was archived, so the model can still read it."""
        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        kept = {key: data[key] for key in KEPT_DATA if isinstance(data.get(key), str) and len(data[key]) <= 100}
        detail = 'Narrow the request to obtain a bounded result.'
        if 'document_id' in kept:
            detail += ' The document was archived; read it with read_document or find_passages.'
        substitute = failure('response_too_large', detail)
        substitute['data'] = kept
        if isinstance(response.get('provenance'), dict):
            substitute['provenance'] = response['provenance']
        if not fits_receipt(substitute, invocation_id):
            substitute['provenance'] = self.service.contacted()
        return substitute

    def tool_call(self, name, args):
        invocation_id = uuid.uuid4().hex
        image = None
        try:
            response, image = self.service.call(name, args, invocation_id)
            if name in ('get_paper', 'search_papers', 'get_citations'):
                response = paper_page(name, response, invocation_id, args.get('abstract_cursor'))
            elif name == 'read_document':
                response = read_page(self.service.runtime.store, response, invocation_id)
            elif name == 'find_passages':
                budget = args.get('max_bytes', 8000)
                while budget > 4 and not fits_receipt(response, invocation_id):
                    budget = max(4, budget // 2)
                    response, image = self.service.call(name, {**args, 'max_bytes': budget}, invocation_id)
            if len(encode(response)) > RESPONSE_BYTES:
                response, image = self._replaced(response, invocation_id), None
            elif name in MEMORY_EVIDENCE_TOOLS and not fits_receipt(response, invocation_id):
                response = compact_metadata(name, response)
                if not fits_receipt(response, invocation_id):
                    response, image = self._replaced(response, invocation_id), None
        except DocumentError as exc:
            missing = exc.code in {'trial_cache_miss', 'unknown_document', 'unknown_source'}
            status = 'unavailable' if missing else 'failed'
            response = self._contacted(failure(exc.code, str(exc), status=status))
        except (ValueError, TypeError, RecursionError):
            # Validation errors do not echo arbitrary caller strings, URLs or operating-system paths.
            response = self._contacted(failure('invalid_arguments',
                                               'The tool arguments are invalid for this operation.'))
        except (OSError, ImportError):
            response = self._contacted(failure('runtime_unavailable', 'The local source, runtime or required program '
                                               'is unavailable.', status='unavailable'))
        except Exception:
            # Anything else is a fault of this server or of a source: a tool error, never the end of the server.
            response = self._contacted(failure('internal_error', 'The literature server could not complete this call.'))
        try:
            self.service.runtime.log(invocation_id, name if isinstance(name, str) else 'unknown', args, response)
        except Exception:
            response = failure('log_unavailable', 'The tool invocation could not be recorded in this workspace.')
            image = None
        result = text_result(response, invocation_id)
        if image is not None:
            result['content'].append(image)
        return result

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' \
                or not isinstance(message.get('method'), str):
            return error(None, -32600, 'Invalid JSON-RPC request')
        method = message['method']
        if 'id' not in message:
            return None
        request_id = message['id']
        if type(request_id) not in (int, str):
            return error(None, -32600, 'Invalid request ID')
        params = message.get('params', {})
        if not isinstance(params, dict):
            return error(request_id, -32602, 'Expected an object for params')
        if method == 'initialize':
            proposed = params.get('protocolVersion')
            if not isinstance(proposed, str):
                return error(request_id, -32602, 'A protocol version is required')
            version = proposed if proposed in PROTOCOLS else PROTOCOLS[0]
            result = {'protocolVersion': version, 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'literature', 'version': '1.0.0'}}
            self.initialized = True
        elif method == 'ping':
            result = {}
        elif not self.initialized:
            return error(request_id, -32002, 'Initialize this connection first')
        elif method == 'tools/list':
            result = {'tools': [{'name': name, **tool} for name, tool in TOOLS.items()]}
        elif method == 'tools/call':
            result = self.tool_call(params.get('name'), params.get('arguments', {}))
        else:
            return error(request_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def serve(runtime, source, sink, *, service=None):
    session = Session(service or Literature(runtime))
    while line := source.readline(MAX_MESSAGE + 1):
        if len(line) > MAX_MESSAGE:
            while line and not line.endswith(b'\n'):
                line = source.readline(MAX_MESSAGE + 1)
            reply = error(None, -32600, 'Request exceeds the message limit')
        else:
            try:
                message = json.loads(line.decode('utf-8'), parse_constant=reject_constant)
            except (UnicodeError, ValueError, RecursionError):
                reply = error(None, -32700, 'Invalid JSON')
            else:
                try:
                    reply = session.handle(message)
                except Exception:
                    request_id = message.get('id') if isinstance(message, dict) else None
                    reply = error(request_id if type(request_id) in (int, str) else None, -32603, 'Internal error')
        if reply is not None:
            sink.write(encode(reply) + b'\n')
            sink.flush()


if __name__ == '__main__':
    serve(Runtime(Path(__file__).resolve().parents[2]), sys.stdin.buffer, sys.stdout.buffer)
