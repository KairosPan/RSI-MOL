"""The Literature stdio MCP server. stdout carries newline-delimited JSON-RPC messages.

A call that fails in any way is answered with a tool error and still gets its log row; the process ends only at the
end of its input.
"""
from __future__ import annotations

import sys

# PDFs go through Poppler only (design 5.10). The pinned reader would parse a PDF in-process with pypdf if the Lab
# installed it in env/; block the import before anything can make it, so the upstream modules stay unchanged.
sys.modules['pypdf'] = None

import json  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

from dispatch import Literature, RESPONSE_BYTES, TOOLS, failure  # noqa: E402
from rsi_literature.documents import DocumentError  # noqa: E402
from runtime import Runtime  # noqa: E402

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_MESSAGE = 256 * 1024
KEPT_DATA = ('document_id', 'document_version', 'source_sha256')     # what a replaced reply keeps of its data


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


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

    def _replaced(self, response):
        """The reply that stands in for one over RESPONSE_BYTES: the same provenance (the sources and hosts that
        were contacted) and the identity of a document that was archived, so the model can still read it."""
        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        kept = {key: data[key] for key in KEPT_DATA if isinstance(data.get(key), str)}
        detail = 'Narrow the request to obtain a bounded result.'
        if 'document_id' in kept:
            detail += ' The document was archived; read it with read_document or find_passages.'
        substitute = failure('response_too_large', detail)
        substitute['data'] = kept
        if isinstance(response.get('provenance'), dict):
            substitute['provenance'] = response['provenance']
        if len(encode(substitute)) > RESPONSE_BYTES // 2:
            substitute['provenance'] = self.service.contacted()
        return substitute

    def tool_call(self, name, args):
        invocation_id = uuid.uuid4().hex
        image = None
        try:
            response, image = self.service.call(name, args, invocation_id)
            if len(encode(response)) > RESPONSE_BYTES:
                response, image = self._replaced(response), None
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
        response['invocation_id'] = invocation_id
        content = [{'type': 'text', 'text': encode(response).decode()}]
        if image is not None:
            content.append(image)
        return {'content': content, 'isError': response['status'] in {'failed', 'unavailable'}}

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
