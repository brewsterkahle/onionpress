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
- The proxy answers only a request whose Host names it (issue #465: a
  page at a name its DNS points at 127.0.0.1 looks like the proxy's own
  page), and a /proxy/ page is sandboxed off the proxy's origin; the links
  rewritten into it carry a token so its own requests still pass.
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

    def request(self, method, path, headers=None, host=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        if host:
            conn.request(method, path, headers=headers or {})
        else:   # no Host header at all
            conn.putrequest(method, path, skip_host=True)
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
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

    # What a browser adds to another site's page's request, and what the
    # extension's own fetch carries.
    PAGE = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    EXT = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}

    def test_status_answers_the_extension_and_no_other_sites_page(self):
        resp, body = self.proxy.request("GET", "/status")
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        resp, body = self.proxy.request("GET", "/status", self.EXT)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), self.EXT["Origin"])
        self.assertIn("onion_address", json.loads(body))
        port = self.proxy.server.server_port
        for headers in ({"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},   # the setup page
                        {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"},      # the address bar
                        {"Origin": f"http://127.0.0.1:{port}"},
                        {"Referer": f"http://localhost:{port}/setup"}):
            with self.subTest(headers=headers):
                resp, body = self.proxy.request("GET", "/status", headers)
                self.assertEqual(resp.status, 200)
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        for headers in (self.PAGE,
                        {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors",
                         "Sec-Fetch-Dest": "image"},                       # localhost:8080's page
                        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                         "Sec-Fetch-Dest": "iframe"},
                        {"Referer": "https://evil.example/"},
                        {"Origin": "http://localhost:8080"}):
            with self.subTest(headers=headers):
                resp, body = self.proxy.request("GET", "/status", headers)
                self.assertEqual(resp.status, 403)
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
                self.assertNotIn(b"onion_address", body)

    def test_proxy_path_is_not_for_another_sites_page(self):
        def fetch(*a, **k):
            return 200, {"content-type": "text/plain"}, b"hello"
        with mock.patch.object(onion_proxy.OnionProxyHandler, "_fetch_via_php", fetch):
            for headers in ({}, self.EXT, {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}):
                with self.subTest(headers=headers):
                    resp, body = self.proxy.request("GET", f"/proxy/{ONION}/x", headers)
                    self.assertEqual((resp.status, body), (200, b"hello"))
            for headers in (self.PAGE, {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "image"}):
                with self.subTest(headers=headers):
                    resp, body = self.proxy.request("GET", f"/proxy/{ONION}/x", headers)
                    self.assertEqual(resp.status, 403)
                    self.assertNotEqual(body, b"hello")

    def test_the_proxy_carries_onion_sites_only(self):
        fetched = []
        def fetch(*a, **k):
            fetched.append(a[1])
            return 200, {}, b""
        with mock.patch.object(onion_proxy.OnionProxyHandler, "_fetch_via_php", fetch):
            for path in ("http://example.com/", "http://127.0.0.1:8080/wp-admin/",
                         "http://192.168.1.1/", "http://localhost:9051/"):
                with self.subTest(path=path):
                    resp, _ = self.proxy.request("GET", path)
                    self.assertEqual(resp.status, 403)
            for path in ("/proxy/example.com/", "/proxy/127.0.0.1/"):
                with self.subTest(path=path):
                    resp, _ = self.proxy.request("GET", path)
                    self.assertEqual(resp.status, 400)
            for target in ("example.com:443", "127.0.0.1:8080", f"{ONION}:443"):
                with self.subTest(target=target):
                    resp, _ = self.proxy.request("CONNECT", target)
                    self.assertEqual(resp.status, 403)
            resp, _ = self.proxy.request("GET", f"http://{ONION}/")
            self.assertEqual(resp.status, 200)
        self.assertEqual(fetched, [f"http://{ONION}/"])
        self.assertFalse(hasattr(onion_proxy.OnionProxyHandler, "_fetch_direct"))
        src = open(onion_proxy.__file__).read()
        self.assertNotIn("create_connection", src)
        self.assertNotIn("'Access-Control-Allow-Origin', '*'", src)

    # Issue #465: a page at a name its DNS points at 127.0.0.1 reaches the
    # proxy with that name as Host, and its same-origin fetches carry no
    # Origin and Sec-Fetch-Site: same-origin, what the setup page's carry.
    REBOUND = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}

    def _hello_and_installed(self):
        def fetch(*a, **k):
            return 200, {"content-type": "text/plain"}, b"hello"
        return (mock.patch.object(onion_proxy.OnionProxyHandler, "_fetch_via_php", fetch),
                mock.patch.object(onion_proxy.OnionProxyHandler, "_wp_installed",
                                  lambda self: True))

    def test_a_request_for_another_host_is_refused(self):
        port = self.proxy.server.server_port
        fetch, installed = self._hello_and_installed()
        requests = (("GET", "/status"), ("HEAD", "/status"), ("OPTIONS", "/status"),
                    ("GET", f"/proxy/{ONION}/x"),
                    ("GET", f"/proxy/{onion_proxy.PROXY_PAGE_TOKEN}/{ONION}/x"),
                    ("GET", "/setup"), ("POST", "/setup"), ("GET", "/nothing"))
        with fetch, installed:
            for host in (f"rebind.attacker.example:{port}", "rebind.attacker.example",
                         f"evil.example:{port}", "127.0.0.1", f"127.0.0.1:{port + 1}",
                         "localhost", f"localhost:{port}x"):
                for method, path in requests:
                    with self.subTest(host=host, method=method, path=path):
                        resp, body = self.proxy.request(method, path,
                                                        {"Host": host, **self.REBOUND})
                        self.assertEqual(resp.status, 403)
                        self.assertNotIn(b"onion_address", body)
                        self.assertNotIn(b"hello", body)
                        self.assertNotIn(host.encode(), body)
            for method, path in requests:   # no Host at all
                with self.subTest(method=method, path=path):
                    resp, body = self.proxy.request(method, path, self.REBOUND, host=False)
                    self.assertEqual(resp.status, 403)
                    self.assertNotIn(b"onion_address", body)
                    self.assertNotIn(b"hello", body)
            # The Hosts a browser sends for the proxy's own pages.
            for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
                         f"LOCALHOST:{port}"):
                with self.subTest(host=host):
                    resp, body = self.proxy.request("GET", "/status",
                                                    {"Host": host, **self.REBOUND})
                    self.assertEqual(resp.status, 200)
                    self.assertIn("onion_address", json.loads(body))
                    resp, body = self.proxy.request("GET", f"/proxy/{ONION}/x", {"Host": host})
                    self.assertEqual((resp.status, body), (200, b"hello"))
                    resp, body = self.proxy.request(
                        "POST", "/setup", {"Host": host, "Origin": f"http://127.0.0.1:{port}",
                                           "Sec-Fetch-Site": "same-origin"})
                    self.assertEqual(resp.status, 409)   # past the origin check
                    resp, body = self.proxy.request("OPTIONS", "/status",
                                                    {"Host": host, **self.EXT})
                    self.assertEqual(resp.status, 204)
            # An absolute-form request names its target in Host.
            resp, body = self.proxy.request("GET", f"http://{ONION}/x", {"Host": ONION})
            self.assertEqual((resp.status, body), (200, b"hello"))
        self.assertFalse([l for l in self.proxy.logs if "attacker" in l or "evil" in l],
                         self.proxy.logs)

    def test_proxied_pages_are_sandboxed_off_the_proxys_origin(self):
        port = self.proxy.server.server_port
        page = (f'<a href="http://{ONION}/p">x</a><img src="/i.png">'
                '<form action="/post"></form>').encode()

        def fetch(self_, url, **k):
            if url.endswith("/i.png"):
                return 200, {"content-type": "image/png"}, b"\x89PNG"
            if url.endswith("/down"):
                return 502, {"content-type": "text/plain"}, b"Tor fetch failed: timeout"
            return 200, {"content-type": "text/html; charset=utf-8"}, page

        with mock.patch.object(onion_proxy.OnionProxyHandler, "_fetch_via_php", fetch):
            resp, body = self.proxy.request("GET", f"/proxy/{ONION}/")
            self.assertEqual(resp.status, 200)
            csp = resp.getheader("Content-Security-Policy")
            self.assertEqual(csp, onion_proxy.PROXIED_PAGE_CSP)
            self.assertTrue(csp.startswith("sandbox "), csp)
            self.assertNotIn("allow-same-origin", csp)
            for needed in ("allow-scripts", "allow-forms", "allow-popups"):
                self.assertIn(needed, csp.split())
            # Every response under /proxy/: a subresource, the unreachable
            # page, an error, a refusal. Not the proxy's own pages, nor an
            # absolute-form request's (that page is the onion's own origin).
            for path, headers, status in (
                    (f"/proxy/{ONION}/i.png", {}, 200),
                    (f"/proxy/{ONION}/down", {"Accept": "text/html"}, 502),
                    ("/proxy/example.com/", {}, 400),
                    (f"/proxy/{ONION}/x", self.PAGE, 403),
                    (f"/proxy/{ONION}/x", {"Host": f"evil.example:{port}"}, 403)):
                with self.subTest(path=path, status=status):
                    resp, _ = self.proxy.request("GET", path, headers)
                    self.assertEqual(resp.status, status)
                    self.assertEqual(resp.getheader("Content-Security-Policy"), csp)
            for path, headers in (("/status", {}), (f"http://{ONION}/", {"Host": ONION}),
                                  (f"http://{ONION}/down", {"Host": ONION, "Accept": "text/html"})):
                with self.subTest(path=path):
                    resp, _ = self.proxy.request("GET", path, headers)
                    self.assertIsNone(resp.getheader("Content-Security-Policy"))

            # The links the proxy rewrote carry the token, and the sandboxed
            # page's own requests (an opaque origin: cross-site, Origin null
            # or none, as another site's page sends) pass only with it.
            resp, body = self.proxy.request("GET", f"/proxy/{ONION}/")
            token = onion_proxy.PROXY_PAGE_TOKEN
            self.assertIn(f'href="/proxy/{token}/{ONION}/p"'.encode(), body)
            self.assertIn(f'src="/proxy/{token}/{ONION}/i.png"'.encode(), body)
            self.assertIn(f'action="/proxy/{token}/{ONION}/post"'.encode(), body)
            self.assertNotIn(f'"/proxy/{ONION}/'.encode(), body)
            for headers in ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                             "Sec-Fetch-Dest": "image"},
                            {"Origin": "null", "Sec-Fetch-Site": "cross-site",
                             "Sec-Fetch-Mode": "cors"},
                            {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                             "Sec-Fetch-Dest": "document"}):
                with self.subTest(headers=headers):
                    resp, body = self.proxy.request("GET", f"/proxy/{token}/{ONION}/i.png", headers)
                    self.assertEqual((resp.status, body), (200, b"\x89PNG"))
                    self.assertEqual(resp.getheader("Content-Security-Policy"), csp)
                    for path in (f"/proxy/{ONION}/i.png",
                                 f"/proxy/{'x' * len(token)}/{ONION}/i.png",
                                 f"/proxy/{token[:-1]}/{ONION}/i.png"):
                        resp, body = self.proxy.request("GET", path, headers)
                        self.assertEqual(resp.status, 403, path)
                        self.assertNotEqual(body, b"\x89PNG")
            resp, _ = self.proxy.request("GET", f"/proxy/{'x' * len(token)}/{ONION}/i.png")
            self.assertEqual(resp.status, 400)   # a tool's request: not an onion host

    def test_setup_form_takes_only_the_setup_pages_own_post(self):
        port = self.proxy.server.server_port
        own = f"http://127.0.0.1:{port}"
        fetch, installed = self._hello_and_installed()
        with fetch, installed:
            for headers in ({"Origin": own, "Sec-Fetch-Site": "same-origin"},
                            {"Origin": f"http://localhost:{port}", "Sec-Fetch-Site": "same-origin"},
                            {"Origin": own},   # an older browser
                            {}):               # not a browser
                with self.subTest(headers=headers):
                    resp, _ = self.proxy.request("POST", "/setup", headers)
                    self.assertEqual(resp.status, 409)   # past the origin check
            for headers in ({"Origin": "null", "Sec-Fetch-Site": "cross-site"},   # a /proxy/ page
                            {"Origin": own, "Sec-Fetch-Site": "cross-site"},
                            {"Origin": own, "Sec-Fetch-Site": "same-site"},
                            {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                            {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-site"}):
                with self.subTest(headers=headers):
                    resp, _ = self.proxy.request("POST", "/setup", headers)
                    self.assertEqual(resp.status, 403)

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
