#!/usr/bin/env python3
"""The hub API's request limits (#461).

``Content-Length: -1`` once passed both checks (``== 0`` and ``> max``) and
``rfile.read(-1)`` read to EOF: any Tor client could fill a hub's or
OnionHome's memory before a signature was checked. Now do_POST checks the
length of every POST before a handler runs (digits only, the path's limit,
no chunked bodies), bodies are read under a deadline, and the server
holds at most MAX_CONNECTIONS connections.
"""

import importlib.util
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _load("web_server_request_limits", "web-server.py")
ws.OnionHeavenHandler.log_message = lambda *a: None  # quiet test output


class _Server:
    def __init__(self, test):
        self.server = ws.BoundedThreadingHTTPServer(("127.0.0.1", 0), ws.OnionHeavenHandler)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        return s


def _status(sock):
    data = b""
    while b"\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return int(data.split(b" ", 2)[1]) if data else None


class ContentLengthTest(unittest.TestCase):
    def setUp(self):
        # Not a hub: /online answers 403 after reading what it admitted.
        p = mock.patch.object(ws, "_accepts_registrations", lambda: False)
        p.start()
        self.addCleanup(p.stop)
        self.srv = _Server(self)

    def _post(self, path, headers, body=b""):
        s = self.srv.connect()
        self.addCleanup(s.close)
        head = f"POST {path} HTTP/1.1\r\nHost: x\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
        s.sendall(head.encode() + body)
        return s

    def test_negative_and_non_numeric_lengths_are_refused_unread(self):
        for value in ("-1", "abc", "+5", " 1e3", "0x10", "1,1"):
            for path in ("/online", "/logs/upload", "/api/name/register"):
                with self.subTest(value=value, path=path):
                    s = self._post(path, [("Content-Length", value)])
                    # No body sent and the socket left open: an answer
                    # proves nothing was waiting on a read to EOF.
                    self.assertEqual(_status(s), 400)

    def test_duplicate_lengths_and_chunked_are_refused(self):
        s = self._post("/online", [("Content-Length", "2"), ("Content-Length", "3")])
        self.assertEqual(_status(s), 400)
        s = self._post("/online", [("Transfer-Encoding", "chunked")])
        self.assertEqual(_status(s), 411)

    def test_too_large_is_refused_before_reading(self):
        s = self._post("/online", [("Content-Length", str(ws.MAX_REQUEST_BODY + 1))])
        self.assertEqual(_status(s), 413)
        s = self._post("/logs/upload", [("Content-Length", str(ws.MAX_LOG_UPLOAD_BODY + 1))])
        self.assertEqual(_status(s), 413)

    def test_a_good_body_is_read(self):
        s = self._post("/online", [("Content-Length", "2")], b"{}")
        self.assertEqual(_status(s), 403)

    def test_slow_body_is_cut_off_at_the_deadline(self):
        with mock.patch.object(ws, "BODY_DEADLINE_FLOOR", 0.5):
            s = self._post("/online", [("Content-Length", "100")], b"{")
            start = time.monotonic()
            for _ in range(3):
                time.sleep(0.3)
                try:
                    s.sendall(b" ")
                except OSError:
                    break
            self.assertEqual(_status(s), 408)
            self.assertLess(time.monotonic() - start, 5)


class ConnectionCapTest(unittest.TestCase):
    def test_connections_beyond_the_cap_are_closed(self):
        with mock.patch.object(ws, "MAX_CONNECTIONS", 2):
            srv = _Server(self)
        held = [srv.connect() for _ in range(2)]
        for s in held:
            self.addCleanup(s.close)
        time.sleep(0.2)
        extra = srv.connect()
        self.addCleanup(extra.close)
        extra.sendall(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            self.assertEqual(extra.recv(1), b"")
        except ConnectionResetError:
            pass
        held[0].close()
        time.sleep(0.2)
        again = srv.connect()
        self.addCleanup(again.close)
        again.sendall(b"POST /nowhere HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual(_status(again), 404)


class SourceTest(unittest.TestCase):
    def test_every_post_handler_checks_for_a_refusal(self):
        with open(os.path.join(DOCKER_TOR_DIR, "web-server.py"), encoding="utf-8") as f:
            src = f.read()
        calls = src.count("= self._read_json(")
        self.assertGreater(calls, 0)
        self.assertEqual(src.count("if data is False:"), calls)
        self.assertNotIn("int(self.headers.get(\"Content-Length\"", src)
        self.assertNotIn("self.rfile.read(length)", src)


if __name__ == "__main__":
    unittest.main()
