"""
What the tor image's HTTP listeners share: a threading server with a cap
on connections, a request reader with bounds, and the sanitiser for the
log lines they write.

web-server.py's hub API runs on BoundedThreadingHTTPServer. The
healthcheck (web-server.py, port 8081, every node) and the takeover
redirect (onionheaven-redirect.py, port 8082, the onionheaven container
and the takeover workers) are ServiceHandler subclasses: one HTTP/1.0
request per connection, read within IDLE_TIMEOUT, answered, then one log
line. They replace healthcheck-server.sh and onionheaven-redirect.sh,
which forked a root sh per connection under socat (`-T 30`,
`max-children=64`, issue #393); the same two bounds hold here.

Every client of these listeners is anonymous (they face Tor), so nothing a
request carries reaches a log line unsanitised (log_safe) or any other
output unescaped.
"""

import socket
import socketserver
import sys
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer

MAX_CONNECTIONS = 1024     # the hub API's (#461)
SERVICE_CONNECTIONS = 64   # each small listener's, as socat's max-children was
IDLE_TIMEOUT = 30          # seconds to send a request, as socat's -T was
LOG_FIELD_MAX = 200        # longest client-supplied text in a log line
MAX_LINE = 8192            # longest request or header line read
MAX_HEADERS = 100


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most max_connections connections; one
    more is closed at once rather than given a thread (#461)."""

    daemon_threads = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *args, max_connections=None, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections or self.max_connections)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        """HTTPServer's bind without its socket.getfqdn(host): a reverse
        lookup of the listen address that nothing here reads, and that
        takes seconds on a macOS runner (web-server.py binds two servers
        before the API answers, so the integration test's 30 s ran out)."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def log_safe(text, limit=LOG_FIELD_MAX):
    """Client-supplied text, made safe for a log line: printable ASCII
    without spaces only (no CR/LF, so a request cannot forge log lines),
    at most limit characters."""
    return "".join(c for c in text if "\x21" <= c <= "\x7e")[:limit]


def log_line(service, text):
    """One line to stderr (the container log), never failing the caller."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] {service}: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


class Request:
    """A request line and its headers. method is "" when the line was not
    an HTTP request line; path is the line's second field, "" if none.
    Text is decoded as Latin-1, so every byte is one character."""

    def __init__(self, line, method, path, headers):
        self.line = line
        self.method = method
        self.path = path
        self.headers = headers  # lower-cased name -> first value

    def header(self, name):
        return self.headers.get(name.lower())


def _read_line(rfile):
    line = rfile.readline(MAX_LINE + 1)
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    return line.decode("latin-1").rstrip("\r\n")


def read_request(rfile):
    """The Request on rfile. A line that is not "METHOD SP ..." with
    METHOD in A-Z gives method ""; the headers are not read then.
    Raises ValueError for a line or header block over the bounds, and
    OSError (socket.timeout) when the client is too slow."""
    line = _read_line(rfile)
    fields = line.strip(" \t").split(" ")
    method = fields[0]
    path = fields[1] if len(fields) > 1 else ""
    if not method or not method.isascii() or not method.isalpha() or not method.isupper():
        return Request(line, "", path, {})
    headers = {}
    for _ in range(MAX_HEADERS + 1):
        h = _read_line(rfile)
        if not h:
            return Request(line, method, path, headers)
        name, sep, value = h.partition(":")
        if sep:
            headers.setdefault(name.strip().lower(), value.strip(" \t"))
    raise ValueError("too many headers")


class ServiceHandler(socketserver.StreamRequestHandler):
    """One request, one HTTP/1.0 answer, one log line after it.

    A subclass names its SERVICE (the log line's tag) and implements
    respond(request), which returns (status, reason, headers, body,
    outcome) or None to close the connection unanswered. outcome is the
    log line's text after "->"; the request's method and path come
    before it, through log_safe.
    """

    SERVICE = "http"
    timeout = IDLE_TIMEOUT

    def respond(self, request):
        raise NotImplementedError

    def log_fields(self, request):
        """The log line's text before "->" (method and path)."""
        return f"{request.method} {log_safe(request.path or '/')}"

    def handle(self):
        try:
            request = read_request(self.rfile)
        except (OSError, ValueError):
            return  # too slow or too long: closed unanswered, as socat did
        if not request.method:
            # Logged before the reply: the peer may already be gone.
            log_line(self.SERVICE, f"- '{log_safe(request.line)}' -> 400 malformed request line")
            self.send(400, "Bad Request", {"Content-Type": "text/plain"}, b"Bad Request")
            return
        answer = self.respond(request)
        if answer is None:
            return
        status, reason, headers, body, outcome = answer
        self.send(status, reason, headers, body)
        log_line(self.SERVICE, f"{self.log_fields(request)} -> {outcome}")

    def send(self, status, reason, headers, body):
        head = [f"HTTP/1.0 {status} {reason}"]
        head += [f"{name}: {value}" for name, value in headers.items()]
        head += [f"Content-Length: {len(body)}", "Connection: close"]
        try:
            self.wfile.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
        except (OSError, socket.timeout):
            pass


def serve(handler, host, port, max_connections=SERVICE_CONNECTIONS):
    """Serve handler on host:port until the process ends."""
    server = BoundedThreadingHTTPServer((host, port), handler,
                                        max_connections=max_connections)
    log_line(handler.SERVICE, f"listening on {host}:{port}")
    server.serve_forever()
