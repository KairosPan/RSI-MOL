"""The literature server's two HTTP transports, with one time limit for each exchange.

fetch keeps the contract of rsi_literature.remote.http_request (full-text downloads and Asta): it returns
{status, headers, body}, gives a redirect back as its status, and raises remote.RemoteError. provider_fetch keeps
the contract of rsi_literature.providers.http_transport (the provider APIs): a redirect is refused, and it raises
providers.ProviderError. The pinned upstream modules stay unchanged; this server uses these transports instead of
theirs, because those let some faults through:

- Every fault of the exchange is an error of the call, never an exception that ends the server: OSError, urllib's
  errors, and http.client.HTTPException (IncompleteRead, BadStatusLine, LineTooLong, RemoteDisconnected, ...).
- A body that ends before its Content-Length is `incomplete_body`, never a complete response.
- The time limit covers the whole exchange: the connection, a proxy's CONNECT, the TLS handshake, the status line,
  the headers and the body. When it passes, the exchange's socket is shut down, so a step blocked anywhere returns.
"""
from __future__ import annotations

import http.client
import math
import socket
import threading
import time
import urllib.error
import urllib.request

from rsi_literature import providers, remote

CHUNK = 65536
MAX_BYTES = 32 * 1024 * 1024


class _Fault(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Deadline:
    """One time limit for an exchange. When it passes, every socket of the exchange is shut down."""

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds
        self.expired = False
        self._lock = threading.Lock()
        self._sockets = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self):
        return self.end - time.monotonic()

    def watch(self, sock):
        # A duplicate keeps the socket's descriptor from being reused while the timer may still shut it down.
        copy = sock.dup()
        with self._lock:
            self._sockets.append(copy)
            expired = self.expired
        if expired:
            _shutdown(copy)

    def _expire(self):
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)

    def close(self):
        self._timer.cancel()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _connection(base, deadline):
    class Connection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = self._watched

        def _watched(self, address, timeout=None, source_address=None):
            left = deadline.remaining()
            if left <= 0:
                raise TimeoutError('The exchange ran out of time before connecting')
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                left = min(left, timeout)
            sock = socket.create_connection(address, left, source_address)
            try:
                deadline.watch(sock)
            except BaseException:
                sock.close()
                raise
            return sock
    return Connection


def _opener(deadline, redirect):
    plain = _connection(http.client.HTTPConnection, deadline)
    secure = _connection(http.client.HTTPSConnection, deadline)

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(plain, req)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(secure, req, context=self._context)

    return urllib.request.build_opener(redirect, HTTP, HTTPS)


class _ProviderRedirect(providers._NoRedirect):
    """Upstream's refusal of a provider redirect, closing the refused response first."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _body(response, deadline, max_bytes):
    chunks, size = [], 0
    while True:
        chunk = response.read1(min(CHUNK, max_bytes + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise _Fault('response_too_large')
        chunks.append(chunk)
    if deadline.expired:
        raise _Fault('timeout')
    left = getattr(response, 'length', None)
    if not getattr(response, 'chunked', False) and type(left) is int and left > 0:
        raise _Fault('incomplete_body')
    return b''.join(chunks)


def _timed_out(error, deadline):
    """Whether a fault is the end of the exchange's time: the deadline passed, or a socket's own time-out (never set
    to end before the deadline) ended a step, as error, its reason or a cause of it."""
    if deadline.expired or deadline.remaining() <= 0:
        return True
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
            return True
        error = error.__cause__ or error.__context__
    return False


def _exchange(method, url, headers, body, timeout, max_bytes, redirect):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
            or not 0 < timeout <= 120:
        raise ValueError('HTTP timeout must be finite and between 0 and 120 seconds')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError('HTTP byte limit must be from 1 byte to 32 MiB')
    deadline = _Deadline(timeout)
    try:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            response = _opener(deadline, redirect).open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            with error:
                return {'status': error.code, 'headers': dict(error.headers or {}), 'body': b''}
        with response:
            data = _body(response, deadline, max_bytes)
            return {'status': response.status, 'headers': dict(response.headers), 'body': data}
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        if isinstance(error, (remote.RemoteError, providers.ProviderError)):
            raise
        raise _Fault('timeout' if _timed_out(error, deadline) else 'transport_failed') from error
    finally:
        deadline.close()


_REMOTE = {'timeout': 'Remote response exceeded its total time limit',
           'transport_failed': 'Remote response was not received completely',
           'incomplete_body': 'Remote response body ended before its declared length',
           'response_limit': 'Remote response exceeded its byte limit'}


def fetch(method, url, headers, body, timeout, max_bytes):
    """One exchange without redirecting (remote.http_request's contract); remote.RemoteError on any fault."""
    try:
        return _exchange(method, url, headers, body, timeout, max_bytes, remote._NoRedirect())
    except _Fault as fault:
        code = 'response_limit' if fault.code == 'response_too_large' else fault.code
        # Never serialize urllib's exception, which can contain a credential URL.
        raise remote.RemoteError(code, _REMOTE[code], dispatched=True) from None


_PROVIDER = {'timeout': ('Provider response exceeded the time budget', 'yes'),
             'transport_error': ('Provider network request failed', 'yes'),
             'incomplete_body': ('Provider response ended before its declared length', 'yes'),
             'response_too_large': ('Provider response exceeded the byte limit', 'no')}


def provider_fetch(method, url, headers, timeout, max_bytes):
    """One exchange with a provider API (providers.http_transport's contract); providers.ProviderError on any fault,
    a redirect included."""
    try:
        return _exchange(method, url, headers, None, timeout, max_bytes, _ProviderRedirect())
    except _Fault as fault:
        code = 'transport_error' if fault.code == 'transport_failed' else fault.code
        detail, retry = _PROVIDER[code]
        raise providers.ProviderError(code, detail, retryability=retry) from None
