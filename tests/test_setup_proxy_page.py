#!/usr/bin/env python3
"""The menubar proxy's /setup pages put nothing unescaped into HTML.

The proxy (127.0.0.1:PROXY_PORT, src/onionpress/onion_proxy.py) serves a
first-run setup form. Its pages echo request data and wp-cli's stderr, and
wp-cli's errors quote the submitted title, user name and email back, so all
of it is escaped for where it lands: element text, a value="..." attribute,
an inline <script>.

Nothing here talks to Docker: subprocess is mocked.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import urlencode

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import onion_proxy, setup_logic  # noqa: E402

XSS = '"><script>alert(1)</script>'


class _SetupTest(unittest.TestCase):
    """A POST that gets through runs setup_logic.install_fresh_wordpress:
    its data dir is a temp dir, and nothing it starts outlives the test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="setup-proxy-page-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        for p in (mock.patch("threading.Thread"),
                  mock.patch.object(setup_logic, "start_bundled_plugin_setup",
                                    return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def _handler(self, body=b""):
        h, logs, sent = _handler(body)
        h.server.data_dir = self.tmpdir
        return h, logs, sent


def _handler(body=b""):
    logs, sent = [], []
    h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
    h.server = types.SimpleNamespace(docker_bin="/x/docker", docker_env={},
                                     log_func=logs.append, onion_address=None,
                                     launcher_script="/x/onionpress",
                                     data_dir=None)
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.send_response = sent.append
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.send_error = lambda *a: sent.append(a)
    return h, logs, sent


def _run(stderr="", install_rc=0, installed=False):
    def run(argv, **kwargs):
        if "is-installed" in argv:
            return subprocess.CompletedProcess(argv, 0 if installed else 1, "", "")
        if "--admin_user=alice" in argv:
            return subprocess.CompletedProcess(argv, install_rc, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


class TestSetupFailurePage(_SetupTest):

    def _post(self, fields, run):
        body = urlencode(fields).encode()
        h, logs, sent = self._handler(body)
        with mock.patch.object(onion_proxy.subprocess, "run", run):
            h._handle_setup_post()
        return sent, h.wfile.getvalue().decode()

    def test_wp_cli_stderr_is_escaped(self):
        stderr = f"Error: The '{XSS}' email address is invalid & <b>bold</b>."
        sent, page = self._post(
            {"blog_title": "T", "user_name": "alice", "admin_password": "pw"},
            _run(stderr=stderr, install_rc=1))
        self.assertEqual(sent, [500])
        self.assertIn("<h1>Setup Failed</h1>", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&amp;", page)

    def test_validation_errors_are_text(self):
        sent, page = self._post(
            {"blog_title": "-T", "user_name": XSS, "admin_password": "pw"},
            _run())
        self.assertEqual(sent, [400])
        self.assertNotIn("<script>", page)
        self.assertIn("Username: Letters, digits", page)

    def test_user_name_is_held_to_the_onionname_rule(self):
        # The user name is the onionname (issue #481), so it is also the
        # site's /<name>/ path.
        for name in ("abcd", "12345", "-alice", "alice-", "a" * 41):
            with self.subTest(name=name):
                sent, page = self._post(
                    {"blog_title": "T", "user_name": name, "admin_password": "pw"},
                    _run())
                self.assertEqual(sent, [400], page)
                self.assertIn("Username: ", page)

    def test_no_launcher_is_refused_before_anything_installs(self):
        body = urlencode({"blog_title": "T", "user_name": "alice",
                          "admin_password": "pw"}).encode()
        h, logs, sent = self._handler(body)
        h.server.launcher_script = None
        calls = []

        def record(argv, **kwargs):
            calls.append(list(argv))
            return _run()(argv, **kwargs)
        with mock.patch.object(onion_proxy.subprocess, "run", record):
            h._handle_setup_post()
        self.assertEqual(sent[0][0], 503)
        self.assertEqual([c for c in calls if "is-installed" not in c], [])


class TestSetupPostOnlyOnFirstRun(_SetupTest):
    """POST /setup on an installed site would reach the password step (wp-cli
    exits 0 when there is nothing to install) and set the password of the
    user the form names. Any local user, or any web page through the
    browser, can reach 127.0.0.1."""

    FIELDS = {"blog_title": "T", "user_name": "alice", "admin_password": "attacker-pw"}

    def _post(self, run, origin=None, port=9077):
        body = urlencode(self.FIELDS).encode()
        h, logs, sent = self._handler(body)
        h.server.server_port = port
        if origin is not None:
            h.headers["Origin"] = origin
        calls = []

        def record(argv, **kwargs):
            calls.append(list(argv))
            return run(argv, **kwargs)
        with mock.patch.object(onion_proxy.subprocess, "run", record):
            h._handle_setup_post()
        return sent, calls, logs

    def test_installed_site_is_refused_before_anything_runs(self):
        sent, calls, logs = self._post(_run(installed=True))
        self.assertEqual(sent, [(409, "WordPress is already configured")])
        self.assertEqual(len(calls), 1)
        self.assertIn("is-installed", calls[0])
        self.assertIn("Setup form refused: WordPress is already installed", logs)

    def test_unknown_install_state_is_refused(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 10)
        sent, calls, _ = self._post(run)
        self.assertEqual(sent[0][0], 503)
        self.assertEqual(len(calls), 1)

    def test_cross_origin_post_is_refused(self):
        for origin in ("https://evil.example", "null", "http://127.0.0.1:8080",
                       "http://localhost.evil.example:9077"):
            with self.subTest(origin=origin):
                sent, calls, _ = self._post(_run(), origin=origin)
                self.assertEqual(sent, [(403, "Cross-origin setup request refused")])
                self.assertEqual(calls, [])

    def test_own_page_and_non_browser_clients_proceed(self):
        for origin in (None, "http://127.0.0.1:9077", "http://localhost:9077"):
            with self.subTest(origin=origin):
                sent, calls, _ = self._post(_run(), origin=origin)
                self.assertEqual(sent, [200])
                self.assertTrue(any("--admin_user=alice" in c for c in calls))


class TestSetupFormPages(unittest.TestCase):

    def test_form_with_error_escapes_every_echoed_field(self):
        h, _, _ = _handler()
        params = {"admin_password": [XSS], "blog_title": ["</script><script>t()</script>"],
                  "user_name": ["a&b"], "theme_choice": ["</script><img src=x onerror=y>"]}
        h._send_setup_form_with_error(params, f"bad {XSS}")
        page = h.wfile.getvalue().decode()
        # The template's own script plus the restore script, nothing more.
        template = onion_proxy.SETUP_PAGE_HTML
        self.assertEqual(page.count("<script>"), template.count("<script>") + 1)
        self.assertEqual(page.count("</script>"), template.count("</script>") + 1)
        self.assertNotIn("<img", page)
        m = re.search(r'name="admin_password" id="admin_password" value="([^"]*)"', page)
        self.assertEqual(m.group(1), "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;")
        # Restored values are the submitted ones, not HTML entities.
        script = page[page.rindex("<script>") + 8:page.rindex("</script>")]
        values = re.findall(r'\.value=("(?:[^"\\]|\\.)*")', script)
        self.assertEqual([json.loads(v) for v in values],
                         ["</script><script>t()</script>", "a&b"])

    def test_generated_password_is_attribute_safe(self):
        h, _, sent = _handler()
        with mock.patch.object(onion_proxy.subprocess, "run", _run()), \
                mock.patch.object(onion_proxy.secrets, "choice", lambda chars: "&"):
            h._handle_setup_get()
        page = h.wfile.getvalue().decode()
        self.assertEqual(sent, [200])
        self.assertIn('value="' + "&amp;" * 24 + '"', page)

    def test_script_json(self):
        self.assertEqual(onion_proxy._script_json("</script>&"),
                         '"\\u003c/script\\u003e\\u0026"')
        self.assertEqual(json.loads(onion_proxy._script_json("a<b>&c")), "a<b>&c")


if __name__ == "__main__":
    unittest.main()
