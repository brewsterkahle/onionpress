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
import subprocess
import sys
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

from onionpress import onion_proxy  # noqa: E402

XSS = '"><script>alert(1)</script>'


def _handler(body=b""):
    logs, sent = [], []
    h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
    h.server = types.SimpleNamespace(docker_bin="/x/docker", docker_env={},
                                     log_func=logs.append)
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.send_response = sent.append
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.send_error = lambda *a: sent.append(a)
    h._fetch_archive_s3_keys = lambda e, p: None
    return h, logs, sent


def _run(stderr="", install_rc=0, installed=False):
    def run(argv, **kwargs):
        if "is-installed" in argv:
            return subprocess.CompletedProcess(argv, 0 if installed else 1, "", "")
        if "multisite-install" in argv:
            return subprocess.CompletedProcess(argv, install_rc, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


class TestSetupFailurePage(unittest.TestCase):

    def _post(self, fields, run):
        body = urlencode(fields).encode()
        h, logs, sent = _handler(body)
        with mock.patch.object(onion_proxy.subprocess, "run", run):
            h._handle_setup_post()
        return sent, h.wfile.getvalue().decode()

    def test_wp_cli_stderr_is_escaped(self):
        stderr = f"Error: The '{XSS}' email address is invalid & <b>bold</b>."
        sent, page = self._post(
            {"blog_title": "T", "user_name": "alice", "admin_password": "pw",
             "admin_email": "alice@example.org"},
            _run(stderr=stderr, install_rc=1))
        self.assertEqual(sent, [500])
        self.assertIn("<h1>Setup Failed</h1>", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&amp;", page)

    def test_validation_errors_are_text(self):
        sent, page = self._post(
            {"blog_title": "-T", "user_name": XSS, "admin_password": "pw",
             "admin_email": "x"},
            _run())
        self.assertEqual(sent, [400])
        self.assertNotIn("<script>", page)
        self.assertIn("Username may only contain", page)


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
