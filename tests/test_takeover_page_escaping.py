#!/usr/bin/env python3
"""The hub's takeover page escapes what the client sent.

app/Resources/docker/tor/onionheaven-redirect.sh answers visitors of a
taken-over onion address with a 302 to the Wayback mirror and a small HTML
page. The Host header and the request path went into that page and the
Location header as they came. Browsers percent-encode most of a path, but
not ' or &, and not every client is a browser: the page now gets them
HTML-escaped, and the Location header only printable ASCII with the
characters a URL may not carry literally percent-encoded. Under dash
(Debian's sh, the image's) the header loop's `echo` also turned a "\\n"
the client sent into a real line feed, which landed in Location.

The handler runs under sh and, where present, dash, on canned requests;
its registry path points at a temp dir. No Docker, no network.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor",
                      "onionheaven-redirect.sh")
WAYBACK = "web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"
SITE = "op2" + "a" * 53 + ".onion"
SHELLS = [s for s in ("sh", shutil.which("dash")) if s and shutil.which(s)]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTakeoverPage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "redirect.sh")
        with open(self.script, "w") as f:
            f.write(_read(SCRIPT).replace("/var/lib/onionheaven/registry.db",
                                          os.path.join(self.tmp, "registry.db")))

    def _handle(self, shell, request):
        r = subprocess.run([shell, self.script, "--handle-request"], input=request,
                           capture_output=True, timeout=30)
        head, sep, body = r.stdout.partition(b"\r\n\r\n")
        self.assertTrue(sep, r.stdout)
        lines = head.split(b"\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b": ")
            headers[name.decode()] = value
        return lines, headers, body

    def _check_response(self, lines, headers, body):
        self.assertEqual(lines[0], b"HTTP/1.0 302 Found")
        self.assertEqual(set(headers), {"Location", "Content-Type", "Content-Length",
                                        "X-OnionHeaven-Takeover", "Connection"})
        for line in lines:
            self.assertRegex(line, rb"^[\x20-\x7e]*$", "one header per line, printable")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertRegex(headers["Location"], rb"^[\x21-\x7e]+$")
        self.assertTrue(headers["Location"].startswith(f"http://{WAYBACK}/web/http://".encode()))
        self.assertRegex(body, rb"^[\x20-\x7e]*$")

    def _texts(self, body):
        """The page's markup with its three interpolations taken out."""
        m = re.fullmatch(rb'<html><head><title>Moved</title></head><body>'
                         rb'<h1>This site has been archived</h1>'
                         rb'<p>The onion service at <code>([^<>]*)</code> is currently offline.</p>'
                         rb'<p>An archived copy is available at:<br>'
                         rb'<a href="([^"<>]*)">([^<>]*)</a></p></body></html>', body)
        self.assertIsNotNone(m, body)
        for text in m.groups():
            self.assertNotRegex(text, rb"['\"<>]|&(?!(amp|lt|gt|quot|#39);)")
        return m.groups()

    def test_a_visitor_is_redirected_as_before(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                lines, headers, body = self._handle(
                    shell, f"GET /2024/05/caf%C3%A9/?p=1&x=2 HTTP/1.1\r\nHost: {SITE}\r\n\r\n".encode())
                self._check_response(lines, headers, body)
                url = f"http://{WAYBACK}/web/http://{SITE}/2024/05/caf%C3%A9/?p=1&x=2"
                self.assertEqual(headers["Location"], url.encode())
                code, href, text = self._texts(body)
                self.assertEqual(code, SITE.encode())
                self.assertEqual(href, url.replace("&", "&amp;").encode())
                self.assertEqual(text, href)

    def test_markup_in_the_path_and_host_is_escaped(self):
        path = "/a'b&c\"d<script>alert(1)</script>`{|}^\\e"
        host = "x\"><img src=x onerror=alert(1)>'&"
        for shell in SHELLS:
            with self.subTest(shell=shell):
                lines, headers, body = self._handle(
                    shell, f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                self._check_response(lines, headers, body)
                self.assertNotIn(b"<script", body)
                self.assertNotIn(b"<img", body)
                self.assertNotRegex(headers["Location"], rb'["<>\\^`{|}]')
                self.assertIn(b"/a'b&c%22d%3Cscript%3Ealert(1)%3C/script%3E%60%7B%7C%7D%5E%5Ce",
                              headers["Location"])
                code, href, text = self._texts(body)
                self.assertIn(b"a&#39;b&amp;c%22d%3Cscript%3E", href)
                self.assertEqual(code, b"x%22%3E%3Cimgsrc=xonerror=alert(1)%3E&#39;&amp;")

    def test_a_backslash_escape_in_the_host_stays_text(self):
        # dash's echo made a real line feed of this: "Location: ...x" then
        # a blank line, and the client's markup became the response body.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                lines, headers, body = self._handle(
                    shell, b"GET /p HTTP/1.1\r\nHost: x\\r\\n\\n<b>hi</b>\\0101\r\n\r\n")
                self._check_response(lines, headers, body)
                self.assertNotIn(b"<b>", body)
                self.assertIn(b"x\\r\\n\\n%3Cb%3Ehi%3C/b%3E\\0101", body.replace(b"%5C", b"\\"))

    def test_control_bytes_never_reach_the_response(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                lines, headers, body = self._handle(
                    shell, b"GET /a\x0b\x0cb\x7f\xc3\xa9 HTTP/1.1\r\nHost: \x1bx\ty\r\n\r\n")
                self._check_response(lines, headers, body)
                self.assertTrue(headers["Location"].endswith(b"/web/http://xy/ab"),
                                headers["Location"])

    def test_the_header_loop_has_no_echo(self):
        src = _read(SCRIPT)
        loop = src[src.index("    # Read headers to find Host"):src.index('    log_host=$(log_safe "$host")')]
        code = [l for l in loop.splitlines() if not l.lstrip().startswith("#")]
        self.assertTrue(code)
        self.assertNotIn("echo", "\n".join(code))


if __name__ == "__main__":
    unittest.main()
