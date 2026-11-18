"""HTTP plumbing for the github-core-clone-01 forge.

One ThreadingHTTPServer, HTTP/1.1 keep-alive, JSON everywhere. Routing:

  GET  /healthz                 -> {"ok": true}
  /{owner}/{repo}.git/*         -> git_http.handle
  /api/v3/*                     -> rest.handle
  /api/graphql                  -> graphql.handle
  /mcp                          -> mcp.handle
  anything else                 -> 404 JSON {"message": "Not Found"}

Every REST reply carries a non-empty X-GitHub-Request-Id. Auth is
Authorization: Bearer <MAKEFUN_SHOP_TOKEN>; a missing or wrong token is 401
with a JSON body. A malformed JSON body is 400 with a JSON body. Unknown
routes are 404 JSON, never HTML.

Surface modules are imported lazily inside dispatch so a missing surface
fails loudly at request time instead of at import time.
"""

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from store import STORE

JSON_TYPE = 'application/json; charset=utf-8'


class ApiError(Exception):
    """An error that maps straight onto a JSON response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Request:
    """One parsed request: method, path segments, query, headers, body."""

    def __init__(self, handler):
        self.handler = handler
        self.method = handler.command
        parsed = urlparse(handler.path)
        self.path = unquote(parsed.path)
        self.segments = [s for s in self.path.split('/') if s != '']
        self.query = {}
        for pair in parsed.query.split('&'):
            if not pair:
                continue
            key, _, value = pair.partition('=')
            self.query[unquote(key)] = unquote(value)
        self.headers = handler.headers
        self.body = b''
        self.json = None
        self._json_parsed = False

    def read_body(self):
        if self.body:
            return self.body
        length = self.headers.get('Content-Length')
        if length is not None:
            try:
                n = int(length)
            except ValueError:
                n = 0
            self.body = self.handler.rfile.read(max(0, n)) if n > 0 else b''
            return self.body
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            chunks = []
            while True:
                line = self.handler.rfile.readline(8192).strip()
                try:
                    size = int(line.split(b';')[0], 16)
                except ValueError:
                    break
                if size == 0:
                    break
                chunks.append(self.handler.rfile.read(size))
                self.handler.rfile.readline(8192)
            self.body = b''.join(chunks)
            return self.body
        return b''

    def parse_json(self):
        """Parse the body as JSON. Raises ApiError(400) on malformed JSON."""
        if self._json_parsed:
            return self.json
        self._json_parsed = True
        raw = self.read_body()
        if not raw:
            self.json = None
            return None
        try:
            self.json = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'Problems parsing JSON')
        return self.json

    def bearer(self):
        auth = self.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            return auth[7:].strip()
        return None

    def require_auth(self):
        """401 unless the bearer token is the shop token."""
        if not STORE.check_token(self.bearer()):
            raise ApiError(401, 'Requires authentication')


def _send(handler, status, body_bytes, content_type, extra_headers):
    headers = {
        'Content-Type': content_type,
        'Content-Length': str(len(body_bytes)),
        'X-GitHub-Request-Id': STORE.next_request_id(),
    }
    headers.update(extra_headers or {})
    handler.send_response(status)
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body_bytes)


def send_json(handler, status, obj, extra_headers=None):
    body = (json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
            + '\n').encode('utf-8')
    _send(handler, status, body, JSON_TYPE, extra_headers)


def send_text(handler, status, body_bytes, content_type, extra_headers=None):
    _send(handler, status, body_bytes, content_type, extra_headers)


def send_empty(handler, status, extra_headers=None):
    """A bodiless reply (e.g. 204 No Content): Content-Length 0, still with
    the request id header."""
    _send(handler, status, b'', JSON_TYPE, extra_headers)


def send_error(handler, status, message):
    send_json(handler, status, {'message': message})


def dispatch(req, handler):
    """Route one request to its surface. Never raises out of here."""
    try:
        return _dispatch_inner(req, handler)
    except ApiError as err:
        send_error(handler, err.status, err.message)
    except BrokenPipeError:
        pass
    except Exception as exc:  # never leak a stack or a 500 page shape
        try:
            send_error(handler, 500, 'Internal Server Error: %s' % exc)
        except Exception:
            pass


def _dispatch_inner(req, handler):
    method = req.method
    path = req.path

    if method == 'GET' and path == '/healthz':
        send_json(handler, 200, {'ok': True})
        return

    if len(req.segments) >= 2 and req.segments[1].endswith('.git'):
        import git_http
        git_http.handle(req, handler)
        return

    if path == '/api/v3' or path.startswith('/api/v3/'):
        import rest
        rest.handle(req, handler)
        return

    if path == '/api/graphql':
        import graphql
        graphql.handle(req, handler)
        return

    if path == '/mcp':
        import mcp
        mcp.handle(req, handler)
        return

    send_error(handler, 404, 'Not Found')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Forge/1.0'

    def log_message(self, *args):
        pass

    def _handle(self):
        req = Request(self)
        # Drain the request body up front. A handler that answers without
        # reading its body would leave those bytes in the socket, and the
        # next request on the keep-alive connection would parse as garbage
        # (BaseHTTPRequestHandler's 501 page). parse_json() later reuses the
        # bytes read here.
        req.read_body()
        dispatch(req, self)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


def create_server(host='0.0.0.0', port=0):
    server = ForgeServer((host, port), Handler)
    server.daemon_threads = True
    return server


class ForgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with the two socket tunings a fast forge needs.

    TCP_NODELAY on every accepted socket: without it, Nagle on the server
    side plus the client's delayed ACK stretches every small request/response
    round trip to tens of milliseconds (the bench's 2,000 sequential reads
    measured ~21 ms each before this). A deep listen backlog: the clone
    workload opens 50 connections at once and the default backlog of 5
    drops most of them into retransmits.
    """

    request_queue_size = 128

    def server_bind(self):
        super().server_bind()
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def get_request(self):
        sock, addr = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock, addr
