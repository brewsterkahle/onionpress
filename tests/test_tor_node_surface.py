"""Issue #393: a plain node's onion address carries no hub API, and the
two small listeners are bounded.

Every node's onion service mapped port 8083 to web-server.py (the hub's
HTTP API: stdlib HTTP, JSON, SQLite, PEM parsing), running as root in
the container that holds the site's key and Tor's control cookie,
whether or not the node was a hub. The healthcheck and redirect services
forked a root sh per connection under socat with no idle timeout and no
cap, and the healthcheck GET made three requests to WordPress per
anonymous hit.

Now: 8083 goes on the onion address only when onionheaven_common's
hub_api_exposed says the node is a hub (the opt-in or the canonical
address) or OnionHome. The healthcheck is a thread of web-server.py
(HealthcheckHandler on 127.0.0.1:8081) and the redirect is
onionheaven-redirect.py, both http_listener.ServiceHandler: at most
SERVICE_CONNECTIONS (64) connections, each closed after IDLE_TIMEOUT (30 s)
without a request, as socat's `max-children=64` and `-T 30` were. The
healthcheck answers a plain 200 and asks WordPress nothing (its JSON body
had no reader; see HealthcheckHandler's docstring).
Running web-server.py as a dedicated uid is still open in the issue.
"""

import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOR_DIR = os.path.join(PROJECT_ROOT, "app/Resources/docker/tor")
sys.path.insert(0, TOR_DIR)
import onionheaven_common as oc  # noqa: E402

HUB = oc.ONIONHEAVEN_HUB_ADDRESS
HOME = oc.ONIONHOME_ADDRESS
OTHER = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz2345.onion"


def _read(name):
    with open(os.path.join(TOR_DIR, name), encoding="utf-8") as f:
        return f.read()


class TestHubApiExposed(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-node-surface-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hostname = os.path.join(self.tmp, "hostname")
        p = mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": ""})
        p.start()
        self.addCleanup(p.stop)

    def _write(self, text):
        with open(self.hostname, "w") as f:
            f.write(text)

    def test_only_the_hub_and_onionhome_expose_it(self):
        for addr, exposed in ((HUB, True), (HOME, True), (OTHER, False)):
            with self.subTest(address=addr[:8]):
                self._write(addr + "\n")
                self.assertIs(oc.hub_api_exposed(path=self.hostname), exposed)
                self.assertIs(oc.hub_api_exposed(addr), exposed)

    def test_no_address_or_garbage_is_no_hub(self):
        self.assertFalse(oc.hub_api_exposed(path=self.hostname))      # no file
        self._write("not an address\n")
        self.assertFalse(oc.hub_api_exposed(path=self.hostname))
        self.assertIsNone(oc.read_own_address(self.hostname))

    def test_the_opt_in_exposes_it_on_any_address(self):
        self._write(OTHER + "\n")
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"}):
            self.assertTrue(oc.hub_api_exposed(path=self.hostname))
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "Yes "}):
            self.assertTrue(oc.hub_api_exposed(path=self.hostname))
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "true"}):
            self.assertFalse(oc.hub_api_exposed(path=self.hostname))

    def test_web_server_takes_the_address_from_the_shared_module(self):
        src = _read("web-server.py")
        self.assertIn("_ONIONHOME_ADDRESS = ONIONHOME_ADDRESS", src)
        self.assertNotIn('_ONIONHOME_ADDRESS = "op2home', src)


class TestEntrypoint(unittest.TestCase):

    def setUp(self):
        self.src = _read("entrypoint.sh")
        start = self.src.index("HUB_API_PORT=\"\"")
        end = self.src.index("\nSERVICES_EOF\n", start)
        self.block = self.src[start:end + len("\nSERVICES_EOF\n")]

    def test_8083_is_conditional_on_hub_api_exposed(self):
        self.assertIn("from onionheaven_common import hub_api_exposed", self.block)
        self.assertIn('HUB_API_PORT=\', "8083,127.0.0.1:8083"\'', self.block)
        self.assertIn('{"name": "wordpress", "ports": ["80,127.0.0.1:8080"${HUB_API_PORT}]}', self.block)
        self.assertEqual(self.src.count("8083,127.0.0.1:8083"), 1)
        self.assertIn('{"name": "healthcheck", "ports": ["80,127.0.0.1:8081"]}', self.block)

    def test_the_heredoc_expands_and_writes_valid_json_both_ways(self):
        heredoc = self.block[self.block.index("cat > /etc/tor/onion-services.json"):]
        for port, want in (("", ["80,127.0.0.1:8080"]),
                           (', "8083,127.0.0.1:8083"', ["80,127.0.0.1:8080", "8083,127.0.0.1:8083"])):
            with self.subTest(port=port or "(none)"):
                with tempfile.TemporaryDirectory() as tmp:
                    script = heredoc.replace("/etc/tor/onion-services.json",
                                             os.path.join(tmp, "s.json"))
                    r = subprocess.run(["sh", "-c", f"HUB_API_PORT='{port}'\n" + script],
                                       capture_output=True, text=True, timeout=30)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    import json
                    with open(os.path.join(tmp, "s.json")) as f:
                        services = json.load(f)
                self.assertEqual(services[0], {"name": "wordpress", "ports": want})

    def test_web_server_still_listens_inside_the_network(self):
        # The directory plugin and name sync on OnionHome call
        # onionpress-tor:8083 directly; the listener itself is unchanged.
        self.assertIn("python3 /web-server.py &", self.src)


class TestSmallListeners(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests"))
        import tor_listener_harness as harness
        import http_listener
        cls.harness = harness
        cls.hl = http_listener
        cls.ws = harness.load("web_server_node_surface", "web-server.py")
        cls.rd = harness.load("onionheaven_redirect_node_surface", "onionheaven-redirect.py")

    def test_both_listeners_are_bounded_as_socat_was(self):
        self.assertEqual(self.hl.SERVICE_CONNECTIONS, 64)
        self.assertEqual(self.hl.IDLE_TIMEOUT, 30)
        for handler in (self.ws.HealthcheckHandler, self.rd.RedirectHandler):
            with self.subTest(handler=handler.SERVICE):
                self.assertTrue(issubclass(handler, self.hl.ServiceHandler))
                self.assertEqual(handler.timeout, 30)
        self.assertIn("max_connections=SERVICE_CONNECTIONS", _read("web-server.py"))
        serve = _read("http_listener.py")
        serve = serve[serve.index("def serve("):]
        self.assertIn("max_connections=SERVICE_CONNECTIONS", serve)
        self.assertIn("http_listener.serve(RedirectHandler", _read("onionheaven-redirect.py"))

    def test_a_listener_binds_without_a_reverse_lookup(self):
        """HTTPServer.server_bind asks socket.getfqdn for the listen
        address, seconds each on a macOS runner; nothing reads the name."""
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("getfqdn")):
            server = self.hl.BoundedThreadingHTTPServer(("127.0.0.1", 0), self.ws.HealthcheckHandler)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_name, "127.0.0.1")
        self.assertEqual(server.server_port, server.server_address[1])

    def test_connections_beyond_the_cap_are_closed(self):
        with mock.patch.object(sys, "stderr", io.StringIO()), \
                self.harness.serving(self.ws.HealthcheckHandler, max_connections=2) as port:
            held = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(2)]
            for s in held:
                self.addCleanup(s.close)
            time.sleep(0.2)
            extra = socket.create_connection(("127.0.0.1", port), timeout=10)
            self.addCleanup(extra.close)
            extra.sendall(b"GET / HTTP/1.0\r\n\r\n")
            try:
                self.assertEqual(extra.recv(1), b"")
            except ConnectionResetError:
                pass

    def test_an_idle_connection_is_closed(self):
        with mock.patch.object(self.ws.HealthcheckHandler, "timeout", 0.3), \
                self.harness.serving(self.ws.HealthcheckHandler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                start = time.monotonic()
                self.assertEqual(s.recv(1), b"")   # closed unanswered
                self.assertLess(time.monotonic() - start, 5)

    def test_an_endless_header_block_is_refused(self):
        request = b"GET / HTTP/1.0\r\n" + b"X: y\r\n" * 200 + b"\r\n"
        out, _err = self.harness.exchange(self.ws.HealthcheckHandler, request)
        self.assertEqual(out, b"")
        out, _err = self.harness.exchange(self.ws.HealthcheckHandler,
                                          b"GET /" + b"a" * 9000 + b" HTTP/1.0\r\n\r\n")
        self.assertEqual(out, b"")

    def test_the_wordpress_forward_is_untouched(self):
        self.assertIn("socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &", _read("entrypoint.sh"))

    def test_the_shell_listeners_are_gone(self):
        for name in ("healthcheck-server.sh", "onionheaven-redirect.sh"):
            self.assertFalse(os.path.exists(os.path.join(TOR_DIR, name)), name)
            self.assertNotIn(name, _read("entrypoint.sh"))
            self.assertNotIn(name, _read("Dockerfile"))
        self.assertIn("python3 /onionheaven-redirect.py &", _read("entrypoint.sh"))
        for name in ("http_listener.py", "onionheaven-redirect.py", "tor_control.py"):
            self.assertIn(f"COPY {name} /{name}", _read("Dockerfile"))


class TestHealthcheck(unittest.TestCase):
    """A plain 200 to a GET, whatever WordPress is doing: it asks WordPress
    nothing and names no address."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests"))
        import tor_listener_harness as harness
        cls.harness = harness
        cls.ws = harness.load("web_server_healthcheck", "web-server.py")

    def test_a_get_is_a_plain_200(self):
        out, _err = self.harness.exchange(self.ws.HealthcheckHandler,
                                          b"GET / HTTP/1.1\r\nHost: x.onion\r\n\r\n")
        status, headers, body, _lines = self.harness.parse(out)
        self.assertEqual(status, b"HTTP/1.0 200 OK")
        self.assertEqual(body, b"OK\n")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertNotIn(b".onion", out)

    def test_a_head_is_a_200_without_a_body(self):
        out, _err = self.harness.exchange(self.ws.HealthcheckHandler, b"HEAD / HTTP/1.0\r\n\r\n")
        status, _headers, body, _lines = self.harness.parse(out)
        self.assertEqual(status, b"HTTP/1.0 200 OK")
        self.assertEqual(body, b"")

    def test_it_asks_wordpress_nothing(self):
        handler = _read("web-server.py")
        handler = handler[handler.index("class HealthcheckHandler"):handler.index("def start_healthcheck")]
        for word in ("wordpress", "urlopen", "wget", "subprocess"):
            self.assertNotIn(word, handler.split('"""')[2])

    def test_tor_maps_the_healthcheck_to_its_listener(self):
        self.assertEqual((self.ws.HEALTHCHECK_HOST, self.ws.HEALTHCHECK_PORT), ("127.0.0.1", 8081))
        self.assertIn('{"name": "healthcheck", "ports": ["80,127.0.0.1:8081"]}', _read("entrypoint.sh"))

    def test_it_starts_before_the_registry(self):
        main = _read("web-server.py")
        main = main[main.index("def main():"):]
        self.assertLess(main.index("start_healthcheck()"), main.index("db_connect()"))


if __name__ == "__main__":
    unittest.main()
