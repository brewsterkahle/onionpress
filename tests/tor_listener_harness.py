"""Runs the tor image's small HTTP listeners (http_listener.ServiceHandler
subclasses: the healthcheck in web-server.py, onionheaven-redirect.py) on
127.0.0.1 with a canned request, and returns what a client and the
container log would see. No Docker, no Tor."""

import contextlib
import importlib.util
import io
import os
import socket
import sys
import threading
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
if TOR_DIR not in sys.path:
    sys.path.insert(0, TOR_DIR)

import http_listener  # noqa: E402


def load(name, filename):
    """A tor image script (hyphenated file name) as a module."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def serving(handler, max_connections=http_listener.SERVICE_CONNECTIONS):
    """A server for handler on a free port; yields the port."""
    server = http_listener.BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), handler, max_connections=max_connections)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def exchange(handler, request, timeout=10):
    """(response bytes, log text) of one connection that sends request
    (bytes) and closes its sending side."""
    log = io.StringIO()
    with mock.patch.object(sys, "stderr", log), serving(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(request)
            s.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    return data, log.getvalue()


def parse(response):
    """(status line, {header: value}, body) of a response; values bytes."""
    head, sep, body = response.partition(b"\r\n\r\n")
    if not sep:
        raise AssertionError(f"no header block in {response!r}")
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b": ")
        headers[name.decode()] = value
    return lines[0], headers, body, lines
