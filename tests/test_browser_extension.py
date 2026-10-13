#!/usr/bin/env python3
"""The browser extension reaches Tor, finds its port, is detected, and has
an offline fallback (issue #318).

- .onion goes to the menubar's HTTP proxy (onion_proxy.py), never to Tor's
  SOCKS port: on a Mac that is forwarded out of the Colima VM, where SOCKS
  through the port forward accepts and then closes the connection.
- The proxy port is not fixed: a second macOS user's is 19077. The native
  messaging host reads it from Docker's published WordPress port.
- ~/.onionpress/extension-connected has one format, JSON with a timestamp
  and a browser name. The native host wrote a bare integer, which
  browser.extension_connected_recently could not parse.
- A .onion page load while OnionPress is down opens offline.html; one whose
  onion service does not answer gets the proxy's page. Both link to the
  Wayback Machine (issue #99).
"""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import browser, native_messaging_host, onion_proxy  # noqa: E402

ONION = "a" * 56 + ".onion"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


class TestExtensionMarker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.marker = os.path.join(self.dir, browser.EXTENSION_MARKER)

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_host_marker_is_detected(self):
        with mock.patch.object(native_messaging_host, "APP_SUPPORT", self.dir):
            self.assertTrue(native_messaging_host.write_extension_marker("Firefox"))
        self.assertEqual(browser.extension_connected_recently(self.dir), "Firefox")
        self.assertTrue(browser.wait_for_extension_active(self.dir, timeout=1))

    def test_both_writers_write_the_same_object(self):
        browser.write_extension_marker(self.dir, "Google Chrome")
        with open(self.marker) as f:
            ours = json.load(f)
        with mock.patch.object(native_messaging_host, "APP_SUPPORT", self.dir):
            native_messaging_host.write_extension_marker("Google Chrome")
        with open(self.marker) as f:
            theirs = json.load(f)
        self.assertEqual(set(ours), {"timestamp", "browser"})
        self.assertEqual(set(ours), set(theirs))
        self.assertEqual(ours["browser"], theirs["browser"])

    def test_allowlists_agree(self):
        self.assertEqual(native_messaging_host.ALLOWED_BROWSERS,
                         browser.ALLOWED_BROWSERS)

    def test_unknown_browser_is_not_written(self):
        self.assertFalse(browser.write_extension_marker(self.dir, "Evil\nBrowser"))
        with mock.patch.object(native_messaging_host, "APP_SUPPORT", self.dir):
            self.assertFalse(native_messaging_host.write_extension_marker(None))
        self.assertFalse(os.path.exists(self.marker))

    def test_old_bare_integer_and_junk_read_as_absent(self):
        for content in (str(int(time.time())), "", "[]", "{", '{"timestamp": "x"}',
                        json.dumps({"timestamp": time.time(), "browser": "Lynx"})):
            with self.subTest(content=content):
                with open(self.marker, "w") as f:
                    f.write(content)
                self.assertIsNone(browser.extension_connected_recently(self.dir))

    def test_stale_marker_is_not_recent(self):
        with open(self.marker, "w") as f:
            json.dump({"timestamp": int(time.time()) - 60, "browser": "Firefox"}, f)
        self.assertIsNone(browser.extension_connected_recently(self.dir))


class TestNativeHostProxyPort(unittest.TestCase):

    def test_offsets(self):
        f = native_messaging_host.proxy_port_from_docker_port
        self.assertEqual(f("127.0.0.1:8080\n"), 9077)
        self.assertEqual(f("0.0.0.0:18080\n[::]:18080\n"), 19077)
        self.assertEqual(f("127.0.0.1:28080"), 29077)
        for junk in ("", "nonsense", "127.0.0.1:8081", "127.0.0.1:80",
                     "127.0.0.1:98080"):
            with self.subTest(junk=junk):
                self.assertEqual(f(junk), 9077)

    def test_get_config_reports_the_resolved_port(self):
        with mock.patch.object(native_messaging_host, "get_proxy_port", return_value=19077), \
             mock.patch.object(native_messaging_host, "is_service_running", return_value=False):
            reply = native_messaging_host.handle_message({"type": "get_config"})
        self.assertEqual(reply["proxy_port"], 19077)

    def test_docker_port_is_asked(self):
        done = types.SimpleNamespace(returncode=0, stdout="127.0.0.1:18080\n")
        env = {k: v for k, v in os.environ.items() if k != "ONIONPRESS_PROXY_PORT"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(native_messaging_host.subprocess, "run",
                               return_value=done) as run:
            self.assertEqual(native_messaging_host.get_proxy_port(), 19077)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:], ["port", "onionpress-wordpress", "80"])


class _ProxyServer:
    """A real onion_proxy server on an ephemeral port."""

    def __init__(self, data_dir):
        self.logs = []
        self.server = onion_proxy.ThreadingHTTPServer(
            ("127.0.0.1", 0), onion_proxy.OnionProxyHandler)
        self.server.data_dir = data_dir
        self.server.log_func = lambda msg, *a: self.logs.append(msg % a if a else msg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestProxy(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proxy = _ProxyServer(self.tmp.name)
        self.proxy.server.cache = None

    def tearDown(self):
        self.proxy.close()
        self.tmp.cleanup()

    def test_status_poll_writes_the_marker_and_is_not_logged(self):
        resp, body = self.proxy.request("GET", "/status",
                                        {"X-OnionPress-Browser": "Brave Browser"})
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(body)["running"])
        self.assertEqual(browser.extension_connected_recently(self.tmp.name),
                         "Brave Browser")
        self.assertFalse([l for l in self.proxy.logs if "/status" in l], self.proxy.logs)

    def _fetch_onion(self, accept, fetch):
        with mock.patch.object(onion_proxy.OnionProxyHandler, "_fetch_via_php", fetch):
            return self.proxy.request("GET", f"http://{ONION}/a?b=<x>",
                                      {"Accept": accept})

    def test_unreachable_onion_page_links_the_wayback_machine(self):
        def down(*a, **k):
            raise OSError("connection refused")
        def tor_failed(*a, **k):
            return 502, {"content-type": "text/plain"}, b"Tor fetch failed: timeout"
        for fetch in (down, tor_failed):
            with self.subTest(fetch=fetch.__name__):
                resp, body = self._fetch_onion("text/html,*/*;q=0.8", fetch)
                page = body.decode()
                self.assertEqual(resp.status, 502)
                self.assertIn("text/html", resp.getheader("Content-Type"))
                self.assertIn(f'href="https://web.archive.org/web/http://{ONION}/a?b=&lt;x&gt;"',
                              page)
                self.assertNotIn("<x>", page)

    def test_subresources_and_site_errors_are_passed_through(self):
        def tor_failed(*a, **k):
            return 502, {"content-type": "text/plain"}, b"Tor fetch failed: timeout"
        resp, body = self._fetch_onion("image/*", tor_failed)
        self.assertEqual(body, b"Tor fetch failed: timeout")

        def site_502(*a, **k):
            return 502, {"content-type": "text/html"}, b"<h1>Bad gateway</h1>"
        resp, body = self._fetch_onion("text/html", site_502)
        self.assertEqual(body, b"<h1>Bad gateway</h1>")


class TestExtensionSources(unittest.TestCase):

    def test_onion_goes_to_the_http_proxy_not_socks(self):
        js = _read("extension/background.js")
        self.assertNotIn("SOCKS", js.split("*/", 1)[1])
        self.assertNotIn('"socks"', js)
        self.assertNotIn("9050", js)
        self.assertIn('return "PROXY 127.0.0.1:${port}"', js)
        self.assertIn('{ type: "http", host: "127.0.0.1", port: proxyPort }', js)

    def test_port_is_not_fixed(self):
        for rel in ("extension/background.js", "extension/offline.js",
                    "extension/offline.html", "extension/popup.js",
                    "extension/manifest.json", "extension-firefox/manifest.json"):
            with self.subTest(rel=rel):
                self.assertNotRegex(_read(rel), r"(localhost|127\.0\.0\.1):9077")
        js = _read("extension/background.js")
        self.assertIn("msg.proxy_port", js)
        self.assertIn("browser: detectBrowser()", js,
                      "native messages must name the browser for the marker")

    def test_offline_fallback_is_wired(self):
        js = _read("extension/background.js")
        self.assertIn('api.runtime.getURL("offline.html")', js)
        self.assertIn("tabs.onUpdated", js)
        offline = _read("extension/offline.js")
        self.assertIn("https://web.archive.org/web/", offline)
        html = _read("extension/offline.html")
        self.assertIsNone(re.search(r"<script>", html),
                          "inline scripts violate the extension CSP")
        self.assertIn('<script src="offline.js"></script>', html)
        for rel in ("extension/manifest.json",):
            self.assertIn("*://*.onion/*", json.loads(_read(rel))["host_permissions"])
        self.assertIn("*://*.onion/*",
                      json.loads(_read("extension-firefox/manifest.json"))["permissions"])

    def test_firefox_overlay_holds_only_its_manifest(self):
        """Copies of shared files in extension-firefox/ win in the Firefox
        package and drift silently from extension/."""
        self.assertEqual(sorted(os.listdir(os.path.join(PROJECT_DIR, "extension-firefox"))),
                         ["manifest.json"])


if __name__ == "__main__":
    unittest.main()
