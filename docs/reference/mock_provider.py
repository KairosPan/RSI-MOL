"""A scripted stand-in for the model endpoint, for offline checks of Codex's own tools.

Serves POST /v1/responses as server-sent events. The script file is a JSON list
of steps; step i is returned to the i-th request. Every request body is logged.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, script_path, log_path = int(sys.argv[1]), sys.argv[2], sys.argv[3]
state = {'n': 0}


def completed(ident):
    return {'type': 'response.completed', 'response': {'id': ident, 'usage': {
        'input_tokens': 0, 'input_tokens_details': None, 'output_tokens': 0, 'output_tokens_details': None,
        'total_tokens': 0}}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with open(log_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'get': self.path}) + '\n')
        self.send_response(404); self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('content-length') or 0))
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {'unparsed': body[:200].decode('utf-8', 'replace')}
        with open(log_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'post': self.path, 'n': state['n'], 'body': parsed}, ensure_ascii=False) + '\n')
        steps = json.load(open(script_path, encoding='utf-8'))
        index = state['n']; state['n'] += 1
        ident = 'resp_%d' % index
        items = steps[index] if index < len(steps) else [
            {'type': 'message', 'role': 'assistant', 'id': 'msg_%d' % index,
             'content': [{'type': 'output_text', 'text': 'script finished'}]}]
        events = [{'type': 'response.created', 'response': {'id': ident}}]
        events += [{'type': 'response.output_item.done', 'item': item} for item in items]
        events.append(completed(ident))
        payload = ''.join('event: %s\ndata: %s\n\n' % (e['type'], json.dumps(e)) for e in events).encode()
        self.send_response(200)
        self.send_header('content-type', 'text/event-stream')
        self.send_header('content-length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
