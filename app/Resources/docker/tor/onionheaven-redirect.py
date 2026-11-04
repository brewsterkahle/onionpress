#!/usr/bin/env python3
"""
OnionHeaven 302 Redirect Service

Listens on port 8082 and answers every request with a 302 to the Internet
Archive Wayback Machine's .onion mirror, for the Host the visitor asked for.

When OnionHeaven takes over a failed .onion address, Tor routes incoming
connections here (the queue manager's ADD_ONION maps port 80 to
127.0.0.1:8082). Visitors get redirected to the archived version of the
site. Runs in the onionheaven container and in every takeover worker.

Host and path are whatever the client sent, and not every client is a
browser that percent-encodes: url_safe for the Location header, and
html_escape on top of it for the page. Replaces onionheaven-redirect.sh,
whose sed did the same escaping per connection under socat.
"""

import html
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import http_listener  # noqa: E402
from http_listener import ServiceHandler, log_safe  # noqa: E402
from onionheaven_common import DB_PATH  # noqa: E402

LISTEN_HOST = "0.0.0.0"
REDIRECT_PORT = 8082
WAYBACK_ONION = "web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"
REGISTRY_TIMEOUT = 2  # seconds; the registry is best effort here
# Only one whole v3 address is looked up in the registry.
ONION_ADDRESS_RE = re.compile(r"[a-z2-7]{56}\.onion")

# The characters a URL may not carry literally, percent-encoded. '%' is
# left alone: browsers send the path already encoded.
_URL_ESCAPES = {c: f"%{ord(c):02X}" for c in '"<>\\^`{|}'}


def url_safe(text):
    """Printable ASCII without spaces, so it stays on the Location line,
    with the characters a URL may not carry literally percent-encoded."""
    return "".join(_URL_ESCAPES.get(c, c) for c in text if "\x21" <= c <= "\x7e")


def html_escape(text):
    """&, <, >, " and ' as character references."""
    return html.escape(text, quote=True).replace("&#x27;", "&#39;")


def _registry():
    """The registry, opened for reading and writing but never created."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True,
                           timeout=REGISTRY_TIMEOUT)
    conn.execute(f"PRAGMA busy_timeout={REGISTRY_TIMEOUT * 1000}")
    return conn


def released(host):
    """Has the registry marked host online again (released)? Errors are no."""
    if not ONION_ADDRESS_RE.fullmatch(host):
        return False
    try:
        conn = _registry()
        try:
            row = conn.execute("SELECT status FROM registry WHERE content_address = ? "
                               "LIMIT 1", (host,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == "online"


def note_redirect(host):
    """Record the redirect's time on every row of host (best effort)."""
    if not ONION_ADDRESS_RE.fullmatch(host):
        return
    try:
        conn = _registry()
        try:
            conn.execute("UPDATE registry SET last_redirect = datetime('now') "
                         "WHERE content_address = ?", (host,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def page(host, wayback_url):
    """The HTML page for browsers that do not follow redirects. All ASCII."""
    page_url = html_escape(wayback_url)
    return ("<html><head><title>Moved</title></head><body>"
            "<h1>This site has been archived</h1>"
            f"<p>The onion service at <code>{html_escape(url_safe(host))}</code> "
            "is currently offline.</p>"
            "<p>An archived copy is available at:<br>"
            f'<a href="{page_url}">{page_url}</a></p>'
            "</body></html>").encode("ascii")


class RedirectHandler(ServiceHandler):

    SERVICE = "redirect"

    def log_fields(self, request):
        host = self._host(request)
        return (f"{request.method} {log_safe(request.path or '/')} "
                f"Host: {log_safe(host) or '-'}")

    @staticmethod
    def _host(request):
        # The name before any ":port"
        return (request.header("Host") or "").split(":", 1)[0].strip()

    def respond(self, request):
        host = self._host(request)
        path = request.path or "/"
        if not host:
            return (400, "Bad Request", {"Content-Type": "text/plain"},
                    b"No Host header provided.", "400 no Host header")

        # Circuit breaker: if this address has been released (status=online),
        # drop the connection. A stale descriptor may still route traffic here
        # after release — closing the TCP connection forces the Tor client to
        # mark the circuit as failed and rebuild, which may cause it to fetch
        # a fresh descriptor and reach the real service.
        # See: https://github.com/brewsterkahle/onionpress/issues/114
        if released(host):
            http_listener.log_line(self.SERVICE, f"{self.log_fields(request)} -> "
                                   "dropped (address released, status=online)")
            return None

        wayback_url = f"http://{WAYBACK_ONION}/web/http://{url_safe(host)}{url_safe(path)}"
        # X-OnionHeaven-Takeover is an explicit, version-independent marker
        # of "this response is the takeover, not the real site." Clients use
        # it as defense-in-depth alongside the 302 status check, so a future
        # change to status code / redirect shape doesn't silently flip
        # takeover responses past client reachability gates.
        headers = {"Location": wayback_url, "Content-Type": "text/html",
                   "X-OnionHeaven-Takeover": "1"}
        # The target repeats host and path: the log line has them through
        # log_safe, so it names only the fixed prefix with them.
        outcome = (f"302 http://{WAYBACK_ONION}/web/http://"
                   f"{log_safe(host)}{log_safe(path)}")
        self.redirected = host
        return 302, "Found", headers, page(host, wayback_url), outcome

    redirected = None

    def handle(self):
        super().handle()
        # After the response and its log line: the registry write.
        if self.redirected:
            note_redirect(self.redirected)


def main():
    http_listener.serve(RedirectHandler, LISTEN_HOST, REDIRECT_PORT)


if __name__ == "__main__":
    main()
