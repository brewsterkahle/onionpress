"""Issue #399: My Creations serves owner-dropped documents sandboxed.

serve_file() sent text/html, text/plain and every image/* (SVG included)
inline on the WordPress origin, and Apache serves the same files raw at
/wp-content/creations/..., so a saved web page or an SVG dropped into
~/OnionPress/Creations/My Creations ran script with the owner's cookies
when opened at localhost:8080. Both paths now send
X-Content-Type-Options: nosniff, and Content-Security-Policy: sandbox for
anything a browser renders as a scriptable document.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(PROJECT_ROOT, "app/Resources/plugins/onionpress-creations/onionpress-creations.php")
CONF = os.path.join(PROJECT_ROOT, "app/Resources/docker/wordpress/onionpress-creations.conf")
DOCKERFILE = os.path.join(PROJECT_ROOT, "app/Resources/docker/wordpress/Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _php_function(text, name):
    start = text.index(f"function {name}(")
    return text[start:text.index("\n    }\n", start)]


class TestServeFile(unittest.TestCase):

    def setUp(self):
        self.text = _read(PLUGIN)
        self.body = _php_function(self.text, "serve_file")

    def test_headers_are_sent_after_the_type_and_before_any_byte(self):
        ct = self.body.index("header('Content-Type: ' . $content_type);")
        nosniff = self.body.index("header('X-Content-Type-Options: nosniff');")
        csp = self.body.index("header('Content-Security-Policy: sandbox');")
        self.assertLess(ct, nosniff)
        self.assertLess(nosniff, csp)
        self.assertLess(csp, self.body.index("Content-Disposition"))
        self.assertLess(csp, self.body.index("send_bytes("))
        self.assertIn("if (self::is_sandboxed($basename, $content_type))", self.body)

    def test_which_files_are_sandboxed(self):
        m = re.search(r"const SANDBOXED_EXTENSIONS = array\(([^;]*)\);", self.text)
        exts = set(re.findall(r"'([a-z]+)'", m.group(1)))
        self.assertEqual(exts, {"htm", "html", "xhtml", "xht", "shtml", "mht", "mhtml",
                                "svg", "svgz", "xml", "xsl", "xslt"})
        fn = _php_function(self.text, "is_sandboxed")
        self.assertIn("in_array($ext, self::SANDBOXED_EXTENSIONS, true)", fn)
        for t in ("'text/html'", "'application/xhtml+xml'", "'image/svg+xml'", "'/xml'", "'+xml'"):
            self.assertIn(t, fn)
        # Lower-cased, parameters dropped: "Text/HTML; charset=utf-8" counts.
        self.assertIn("strtolower(strtok($content_type, ';'))", fn)
        self.assertIn("strtolower(pathinfo($basename, PATHINFO_EXTENSION))", fn)

    def test_html_and_svg_still_go_inline(self):
        # Sandboxed, not turned into downloads: a published page still shows.
        self.assertIn("'text/html'", self.body[self.body.index("$inline_types"):])


class TestApacheConf(unittest.TestCase):

    def setUp(self):
        self.conf = _read(CONF)
        start = self.conf.index("<Directory /var/www/html/wp-content/creations>")
        self.directory = self.conf[start:self.conf.index("</Directory>", start)]

    def test_the_raw_path_sends_both_headers(self):
        self.assertIn('Header always set X-Content-Type-Options "nosniff"', self.directory)
        m = re.search(r'<FilesMatch "\(\?i\)\\\.\(([a-z|]+)\)\$">\s*'
                      r'Header always set Content-Security-Policy "sandbox"\s*</FilesMatch>',
                      self.directory)
        self.assertIsNotNone(m, self.directory)
        self.conf_exts = set(m.group(1).split("|"))

    def test_the_two_lists_are_the_same(self):
        self.test_the_raw_path_sends_both_headers()
        m = re.search(r"const SANDBOXED_EXTENSIONS = array\(([^;]*)\);", _read(PLUGIN))
        self.assertEqual(self.conf_exts, set(re.findall(r"'([a-z]+)'", m.group(1))))

    def test_mod_headers_is_enabled_in_the_image(self):
        self.assertRegex(_read(DOCKERFILE), r"a2enmod[^\n]*\bheaders\b")


if __name__ == "__main__":
    unittest.main()
