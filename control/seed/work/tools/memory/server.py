"""Offline Scientific Memory MCP server. Its input is the snapshot beside the workspace tools directory."""

import json
import os
import re
import secrets
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

if os.name == 'nt':
    import rsi_fs as os
else:
    import fcntl

from query import DEFAULT_BYTES, NATIVE_RECEIPT_BYTES, Snapshot, bounded, challenge_address, encode, exact_page

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_REQUEST = 64 * 1024
MAX_PROPOSALS = 64
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
# Older callers request up to 16 MiB; accept those requests and apply the receipt cap.
BYTES = {'type': 'integer', 'minimum': 64, 'maximum': 16 << 20,
         'description': 'Maximum UTF-8 result bytes. The MCP receipt is capped at 48 KiB and its native '
                        'JSON-wrapped representation must fit 64 KiB. '
                        'Whole items that do not fit count as omitted.'}
OFFSET = {'type': 'integer', 'minimum': 0}
SEARCH = {'query': TEXT, 'max_bytes': BYTES, 'offset': OFFSET}
CITATION_FIELDS = {key: TEXT for key in ('document_id', 'document_version', 'source_sha256', 'locator', 'quote')}
CITATION_FIELDS['relation'] = {'type': 'string', 'enum': ['supports', 'challenges', 'background', 'derived_from']}
HISTORICAL_FIELDS = dict(CITATION_FIELDS, evidence_id=TEXT, revision={'type': 'integer', 'minimum': 1}, source_run=TEXT)
LITERATURE_FIELDS = dict(CITATION_FIELDS, delivery_source={'type': 'string', 'enum': ['literature']},
                         invocation_id={'type': 'string', 'pattern': '^[a-f0-9]{32}$',
                                        'description': 'The invocation_id of the earlier literature passage response.'})
REFERENCE = {'oneOf': [
    {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}
    for fields in (HISTORICAL_FIELDS, LITERATURE_FIELDS)]}
OBJECT_FIELDS = {'id': TEXT, 'revision': {'type': 'integer', 'minimum': 1}, 'relation': CITATION_FIELDS['relation']}
CONTENT_FIELDS = {'question': ('text',), 'judgment': ('statement', 'scope'), 'decision': ('decision', 'scope'),
                  'investigation': ('description', 'scope'), 'method_experience': ('change', 'rationale', 'outcome', 'scope')}
COMMON = ('kind', 'id', 'expected_revision', 'question_id')
SUFFIX = '[A-Za-z0-9][A-Za-z0-9_-]{0,95}'
PROPOSAL_FIELDS = {
    'kind': {'type': 'string', 'enum': list(CONTENT_FIELDS)},
    'id': {'type': 'string', 'pattern': '^(subquestion|judgment|decision|investigation|method_experience)_' + SUFFIX + '$'},
    'expected_revision': {'type': 'integer', 'minimum': 0},
    'question_id': {'type': 'string', 'pattern': '^([A-Za-z0-9][A-Za-z0-9_-]{0,63}|subquestion_' + SUFFIX + ')$',
                    'description': 'Current task ID, or a retrieved subquestion pinned in object_refs.'},
    **{k: TEXT for k in ('statement', 'decision', 'description', 'text', 'change', 'rationale')},
    'scope': {'type': 'string', 'maxLength': 4096, 'description': 'Applicable conditions. An empty string means unknown.'},
    'outcome': {'type': 'string', 'enum': ['improved', 'degraded', 'unchanged', 'failed']},
    'source_refs': {'type': 'array', 'minItems': 0, 'maxItems': 16, 'items': REFERENCE},
    'object_refs': {'type': 'array', 'minItems': 0, 'maxItems': 16, 'items': {
        'type': 'object', 'properties': OBJECT_FIELDS, 'required': list(OBJECT_FIELDS), 'additionalProperties': False}},
}


def spec(description, properties, required=()):
    return {'description': description, 'inputSchema': {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': False}}


TOOLS = {
    'memory_status': spec('Report the condition, ledger version, visible counts and omissions for this snapshot.', {}),
    'lookup_sources': spec('Look up a paper identifier, title or document ID. See acquisition requests, reported '
                           'identifier checks, conflicts and delivered passage addresses.',
                           SEARCH, ['query']),
    'recall_passages': spec('Retrieve complete historical passages by keywords or an exact identifier. Optionally '
                            'filter by paper, original SHA-256 or source task.',
                            {**SEARCH, 'paper': TEXT, 'source_sha256': TEXT, 'task': TEXT},
                            ['query']),
    'get_evidence': spec('Get a memory object and its visible deliveries. Omit revision for the latest visible '
                         'version. Select a historical delivery by source_run, document_id, document_version or '
                         'invocation_id. Follow next_delivery_offset for older deliveries and next_object_offset '
                         'for the rest of a large revision. Set counterevidence_offset to page through challenge '
                         'addresses and their challenged revisions; pass next_offset to continue this list.',
                         {'id': TEXT, 'revision': {'type': 'integer', 'minimum': 1},
                         'source_run': TEXT, 'document_id': TEXT, 'document_version': TEXT, 'invocation_id': TEXT,
                         'delivery_offset': OFFSET, 'object_offset': OFFSET, 'counterevidence_offset': OFFSET,
                         'max_bytes': BYTES}, ['id']),
    'recall_judgments': spec('Retrieve historical judgments with their applicable scope and evidence.',
                             SEARCH, ['query']),
    'recall_objects': spec('Search visible memory objects, optionally by kind, with freshness and supporting or '
                           'challenging references.', {**SEARCH, 'kind': {'type': 'string',
                           'enum': ['evidence', *CONTENT_FIELDS]}}, ['query']),
    'propose_memory': spec('Submit an interpretation for review after the run. Use text for question, statement for '
                           'judgment, decision for decision, description for investigation, or change, rationale and '
                           'outcome for method_experience. Each kind except question requires scope and a source_refs '
                           'or object_refs citation. Prefix the ID with its kind (subquestion_ for question).',
                           PROPOSAL_FIELDS, COMMON),
}


def arguments(name, args):
    if not isinstance(name, str) or name not in TOOLS:
        raise ValueError('Unknown memory tool')
    validate(TOOLS[name]['inputSchema'], args, name)
    if name == 'propose_memory':
        kind = args['kind']
        required = set(COMMON + CONTENT_FIELDS[kind])
        if required - args.keys() or args.keys() - required - {'source_refs', 'object_refs'}:
            raise ValueError('Missing or unexpected fields for ' + kind)
        prefix = 'subquestion' if kind == 'question' else kind
        if not args['id'].startswith(prefix + '_'):
            raise ValueError('Expected an ID beginning with ' + prefix + '_')
        if kind != 'question' and not (args.get('source_refs') or args.get('object_refs')):
            raise ValueError('Expected at least one evidence or object reference')
    return args


def validate(prop, value, key):
    if 'oneOf' in prop:
        # Reference variants have disjoint, fully required field sets.
        for option in prop['oneOf']:
            if isinstance(value, dict) and value.keys() == option['properties'].keys():
                return validate(option, value, key)
        raise ValueError('Expected a historical or current literature reference for ' + key)
    kind = prop['type']
    if kind == 'object':
        fields = prop['properties']
        if not isinstance(value, dict) or value.keys() - fields.keys() or set(prop['required']) - value.keys():
            raise ValueError('Missing or unexpected fields for ' + key)
        for field, child in value.items():
            validate(fields[field], child, field)
    elif kind == 'array':
        if not isinstance(value, list) or not prop['minItems'] <= len(value) <= prop['maxItems']:
            raise ValueError('Invalid item count for ' + key)
        for child in value:
            validate(prop['items'], child, key)
    elif kind == 'string':
        if (not isinstance(value, str) or len(value) < prop.get('minLength', 0)
                or len(value) > prop.get('maxLength', 4096) or value.isspace()
                or ('enum' in prop and value not in prop['enum'])
                or ('pattern' in prop and not re.fullmatch(prop['pattern'], value))):
            raise ValueError('Invalid string for ' + key)
    elif type(value) is not int or value < prop['minimum'] or ('maximum' in prop and value > prop['maximum']):
        raise ValueError('Integer outside the supported range for ' + key)


@contextmanager
def proposal_folder(workspace, *, writing=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        folder = None
        for entry in (workspace, '.tmp', 'memory-proposals'):
            if writing and entry == 'memory-proposals' and os.name == 'nt':
                # Windows locks regular files. A sibling lock keeps it outside the
                # proposal quota and the controller's sealed collection.
                lock = os.open('.memory-proposals.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=folder)
                stack.callback(os.close, lock)
                try:
                    os.lock_exclusive(lock, wait=False)
                except BlockingIOError:
                    raise ValueError('Another writer holds the proposal directory; retry submission shortly') from None
            folder = os.open(entry, flags, dir_fd=folder)
            stack.callback(os.close, folder)
        if writing and os.name != 'nt':
            try:
                fcntl.flock(folder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another writer holds the proposal directory; retry submission shortly') from None
        yield folder


def save_proposal(workspace, args):
    data = encode(args) + b'\n'
    if len(data) > MAX_REQUEST:
        raise ValueError('Proposal exceeds the byte limit')
    proposal_id = secrets.token_hex(16)
    name = proposal_id + '.json'
    with proposal_folder(workspace, writing=True) as folder:
        with os.scandir(folder) as entries:
            if any(count == MAX_PROPOSALS for count, _ in enumerate(entries, start=1)):
                raise ValueError(f'This task has reached its {MAX_PROPOSALS}-proposal limit')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(folder)
        except BaseException:
            os.unlink(name, dir_fd=folder)
            raise
    return {'status': 'pending', 'proposal_id': proposal_id}


class Server:
    def __init__(self, folder):
        self.folder = folder
        self.snapshot = None
        self.initialized = False

    def proposals_enabled(self):
        if os.environ.get('RSI_MEMORY_PROPOSALS') != '1':
            return False
        try:
            with proposal_folder(self.folder.parent):
                if self.snapshot is None:
                    self.snapshot = Snapshot(self.folder)
                return self.snapshot.status['condition'] == 'live'
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return False

    def call(self, name, params):
        args = arguments(name, params)
        if self.snapshot is None:
            self.snapshot = Snapshot(self.folder)
        if name == 'memory_status':
            return self.snapshot.status
        if self.snapshot.status['condition'] == 'unavailable':
            raise ValueError(self.snapshot.status['problem'])
        if name == 'propose_memory':
            if not self.proposals_enabled():
                raise ValueError('Memory proposals require a managed task with live memory')
            return save_proposal(self.folder.parent, args)
        if name == 'lookup_sources':
            items = self.snapshot.lookup(args['query'])
        elif name == 'recall_passages':
            items = self.snapshot.recall(args['query'], args.get('paper'), args.get('source_sha256'), args.get('task'))
        elif name == 'recall_judgments':
            items = self.snapshot.recall_judgments(args['query'])
        elif name == 'recall_objects':
            items = self.snapshot.recall_objects(args['query'], args.get('kind'))
        else:
            address = {key: args[key] for key in ('document_id', 'document_version', 'invocation_id') if key in args}
            if 'source_run' in args:
                address['run'] = args['source_run']
            items = self.snapshot.get_evidence(args['id'], args.get('revision'), address=address,
                                               delivery_offset=args.get('delivery_offset', 0))
            limit = args.get('max_bytes', NATIVE_RECEIPT_BYTES)
            if 'counterevidence_offset' in args:
                if 'object_offset' in args:
                    raise ValueError('Select either object parts or challenge addresses in one request')
                addresses = ({'challenge': challenge_address(c)} for item in items for c in item.get('counterevidence', ()))
                page = bounded(addresses, limit, offset=args['counterevidence_offset'])
                if not page['results'] and page['omitted']:
                    raise ValueError('Increase max_bytes to fit a challenge address and its continuation')
                return page
            return exact_page(items, limit, args.get('object_offset'))
        return bounded(items, args.get('max_bytes', DEFAULT_BYTES), offset=args.get('offset', 0))

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return error(None, -32600, 'Invalid JSON-RPC request')
        if 'id' not in message:
            return None
        req_id, method, params = message['id'], message['method'], message.get('params', {})
        if type(req_id) not in (str, int) or not isinstance(params, dict):
            return error(None, -32600, 'Invalid request id or params')
        if method == 'initialize':
            if not isinstance(params.get('protocolVersion'), str):
                return error(req_id, -32602, 'Expected protocolVersion')
            version = params['protocolVersion']
            self.initialized = True
            result = {'protocolVersion': version if version in PROTOCOLS else PROTOCOLS[0],
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'memory', 'version': '1.0'}}
        elif method == 'ping':
            result = {}
        elif not self.initialized:
            return error(req_id, -32002, 'Initialize this connection first')
        elif method == 'tools/list':
            result = {'tools': [{'name': k, **v} for k, v in TOOLS.items()
                                if k != 'propose_memory' or self.proposals_enabled()]}
        elif method == 'tools/call':
            try:
                value = self.call(params.get('name'), params.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': encode(value).decode()}], 'isError': False}
            except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
                # Workspace files can change. Return a tool error without ending the MCP session.
                result = {'content': [{'type': 'text', 'text': 'Memory tool error: ' + str(exc)[:200]}],
                          'isError': True}
        else:
            return error(req_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def error(req_id, code, message):
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


def serve(source, sink, folder):
    server = Server(folder)
    while line := source.readline(MAX_REQUEST + 1):
        if len(line) > MAX_REQUEST:
            while line and not line.endswith(b'\n'):
                line = source.readline(MAX_REQUEST + 1)
            reply = error(None, -32600, 'Request exceeds the memory service limit')
        else:
            try:
                reply = server.handle(json.loads(line))
            except (ValueError, RecursionError, UnicodeError):
                reply = error(None, -32700, 'Invalid JSON')
        if reply is not None:
            sink.write(encode(reply) + b'\n')
            sink.flush()


if __name__ == '__main__':
    serve(sys.stdin.buffer, sys.stdout.buffer, Path(__file__).resolve().parents[2] / 'memory')
