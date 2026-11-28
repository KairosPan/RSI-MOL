"""A scripted model endpoint, so tests run the real Codex without calling a model.

Codex (with `model_provider = "mock"`, see provider_config) posts each model request to `<base_url>/responses`.
The endpoint answers request i with step i of its queue as server-sent events; an empty queue answers
say('script finished'). A step is a list of output items. In Codex 0.170.0 the model calls tools through one
custom tool, `exec`, whose input is JavaScript (`await tools.apply_patch(...)`); the tool's answer comes back
in the next request's `input` as an item whose type ends in `_call_output`.

slow(), with_usage() and hang_up() add marker items (type `rsi.…`) that steer the endpoint; they are never sent.
"""
import itertools
import json
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DELAY, USAGE, HANG_UP = 'rsi.delay', 'rsi.usage', 'rsi.hang_up'
_ids = itertools.count(1)
# Disable host-side exports for local validation, independently of the agent's sandbox.
LOCAL_DIAGNOSTICS = {'analytics': {'enabled': False},
                     'otel': {'exporter': 'none', 'trace_exporter': 'none', 'metrics_exporter': 'none'}}


def say(text: str) -> list[dict]:
    """One assistant message."""
    return [{'type': 'message', 'role': 'assistant', 'id': f'msg_{next(_ids)}',
             'content': [{'type': 'output_text', 'text': text}]}]


def run_js(source: str) -> list[dict]:
    """One call of the `exec` tool with JavaScript source."""
    number = next(_ids)
    return [{'type': 'custom_tool_call', 'id': f'ctc_{number}', 'call_id': f'call_{number}', 'name': 'exec',
             'input': source}]


def run_command(command: str) -> list[dict]:
    """One call of the `exec` tool that runs a shell command through Codex's shell tool, `exec_command` (the path
    the model's commands take), without a login shell; the tool's result (exit code, output) is the exec output."""
    return run_js('text(JSON.stringify(await tools.exec_command(' + json.dumps({'cmd': command, 'login': False})
                  + ')));\n')


def _apply_patch(body: str) -> list[dict]:
    patch = '*** Begin Patch\n' + body + '*** End Patch\n'
    return run_js('await tools.apply_patch(' + json.dumps(patch) + ');\n')


def add_file(path: str, content: str) -> list[dict]:
    """The model adds a file through apply_patch; every line of content becomes a `+` line."""
    lines = content.split('\n')
    if lines[-1] == '':
        lines.pop()
    return _apply_patch('*** Add File: ' + path + '\n' + ''.join('+' + line + '\n' for line in lines))


def delete_file(path: str) -> list[dict]:
    """The model deletes a file through apply_patch."""
    return _apply_patch('*** Delete File: ' + path + '\n')


def slow(seconds: float, step: list[dict]) -> list[dict]:
    """The same step, answered after the endpoint has waited `seconds` (or not at all if it is stopped first)."""
    return [{'type': DELAY, 'seconds': seconds}] + step


def with_usage(step: list[dict], input_tokens: int = 0, output_tokens: int = 0) -> list[dict]:
    """The same step, with this usage in its response.completed (the default usage is all zeros)."""
    return [{'type': USAGE, 'input_tokens': input_tokens, 'output_tokens': output_tokens}] + step


def hang_up() -> list[dict]:
    """A step that closes the connection without answering; Codex 0.170.0 then fails the turn (exit code 1)."""
    return [{'type': HANG_UP}]


def provider_config(base_url: str) -> dict:
    """Config keys that point Codex at the endpoint, with no retries."""
    return {**LOCAL_DIAGNOSTICS, 'model_provider': 'mock', 'model_providers': {'mock': {
        'name': 'mock', 'base_url': base_url, 'wire_api': 'responses',
        'request_max_retries': 0, 'stream_max_retries': 0}}}


def _hang_up(handler: BaseHTTPRequestHandler) -> None:
    """Close the request's connection without writing anything."""
    handler.close_connection = True
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _text(output) -> str:
    """The text of a tool output: a string, or a list of content parts with `text`."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return ''.join(_text(part) for part in output)
    if isinstance(output, dict):
        return _text(output.get('text', output.get('content', '')))
    return ''


class ScriptedModel:
    """Serves the scripted steps on 127.0.0.1 in a background thread; records every request body."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._steps: deque[list[dict]] = deque()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        if self._server is not None:
            raise RuntimeError('the scripted model is already running')
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self.send_error(404)

            def do_POST(self) -> None:
                model._answer(self)

        self._stopping.clear()
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        return f'http://127.0.0.1:{self._server.server_address[1]}/v1'

    def stop(self) -> None:
        """Stop serving and release any request waiting in slow(); safe to call more than once."""
        self._stopping.set()
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def push(self, *steps: list[dict]) -> None:
        with self._lock:
            self._steps.extend(steps)

    def tool_outputs(self) -> list[str]:
        """The text of every `*_call_output` item in the last request's input."""
        with self._lock:
            last = self.requests[-1] if self.requests else {}
        items = last.get('input') if isinstance(last, dict) else None
        return [_text(item.get('output')) for item in items or []
                if isinstance(item, dict) and str(item.get('type', '')).endswith('_call_output')]

    def _answer(self, handler: BaseHTTPRequestHandler) -> None:
        if urlsplit(handler.path).path != '/v1/responses':
            handler.send_error(404)
            return
        body = handler.rfile.read(int(handler.headers.get('content-length') or 0))
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {'unparsed': body[:200].decode('utf-8', 'replace')}
        with self._lock:
            self.requests.append(parsed)
            ident = f'resp_{len(self.requests)}'
            step = self._steps.popleft() if self._steps else say('script finished')
        markers = {item['type']: item for item in step if str(item.get('type', '')).startswith('rsi.')}
        delay = markers.get(DELAY, {}).get('seconds', 0)
        stopped = self._stopping.wait(delay) if delay else self._stopping.is_set()
        if stopped or HANG_UP in markers:
            _hang_up(handler)  # no answer at all; also what a request waiting in slow() gets from stop()
            return
        counts = markers.get(USAGE, {})
        tokens_in, tokens_out = counts.get('input_tokens', 0), counts.get('output_tokens', 0)
        usage = {'input_tokens': tokens_in, 'input_tokens_details': None, 'output_tokens': tokens_out,
                 'output_tokens_details': None, 'total_tokens': tokens_in + tokens_out}
        events = [{'type': 'response.created', 'response': {'id': ident}}]
        events += [{'type': 'response.output_item.done', 'item': item} for item in step
                   if not str(item.get('type', '')).startswith('rsi.')]
        events.append({'type': 'response.completed', 'response': {'id': ident, 'usage': usage}})
        payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
        try:
            handler.send_response(200)
            handler.send_header('content-type', 'text/event-stream')
            handler.send_header('content-length', str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            pass  # Codex went away (killed after a timeout); nothing to answer
