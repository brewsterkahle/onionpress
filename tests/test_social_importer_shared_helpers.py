#!/usr/bin/env python3
"""The social importers fetch, store media and look up posts through one
set of helpers in onionpress-social-archive.php (issue #409).

Twitter, Mastodon, Bluesky, Reddit and the blog importer each carried a
copy of the same curl-over-SOCKS GET, file download, `_source_id` lookup
and upload-into-uploads code, byte-identical after renaming. Now:

  * onionpress_social_tor_get / onionpress_social_fetch_file are the only
    curl in the importers: socks5h through onionheaven, http(s) only,
    redirects included;
  * onionpress_social_find_post_by_source_id is the one lookup;
  * onionpress_social_sideload_url / onionpress_social_store_upload put
    media into uploads (the type checks are tests/test_uploads_no_php.py's).

What stays per importer differs in substance (each API's paging and
cursors, the import of a post, Bluesky's media_handle_sideload) and is
not held here.
"""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(ROOT, "app", "Resources", "plugins")
SHARED = os.path.join(PLUGINS, "onionpress-social-archive.php")
SOURCES = ("twitter", "mastodon", "bluesky", "reddit", "blog")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _importer(source):
    return os.path.join(PLUGINS, f"onionpress-social-archive-{source}.php")


def _function(src, name):
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


class TestOneSetOfHelpers(unittest.TestCase):

    def test_no_importer_has_its_own_transport_or_lookup(self):
        for source in SOURCES:
            src = _read(_importer(source))
            with self.subTest(source=source):
                self.assertNotIn("curl_init(", src)
                self.assertNotIn("CURLOPT_", src)
                self.assertIsNone(re.search(r"^const \w+_SOCKS_PROXY", src, re.M))
                for helper in ("api_get", "fetch_file", "find_post_by_source_id", "sideload_url"):
                    self.assertNotIn(f"function onionpress_{source}_{helper}(", src)

    def test_every_importer_uses_the_shared_lookup(self):
        for source in SOURCES:
            with self.subTest(source=source):
                self.assertIn("onionpress_social_find_post_by_source_id(", _read(_importer(source)))

    def test_the_transport_is_socks5h_and_http_only(self):
        shared = _read(SHARED)
        self.assertIn("const ONIONPRESS_SOCIAL_SOCKS_PROXY  = 'onionheaven:9050';", shared)
        for name in ("onionpress_social_tor_get", "onionpress_social_fetch_file"):
            fn = _function(shared, name)
            with self.subTest(function=name):
                self.assertIn("CURLOPT_PROXY           => ONIONPRESS_SOCIAL_SOCKS_PROXY,", fn)
                self.assertIn("CURLOPT_PROXYTYPE       => CURLPROXY_SOCKS5_HOSTNAME,", fn)
                self.assertIn("CURLOPT_PROTOCOLS       => CURLPROTO_HTTP | CURLPROTO_HTTPS,", fn)
                self.assertIn("CURLOPT_REDIR_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,", fn)

    def test_a_failed_download_leaves_nothing(self):
        fn = _function(_read(SHARED), "onionpress_social_fetch_file")
        self.assertIn("if ( ! $ok || $code < 200 || $code >= 300 ) {\n        @unlink( $dest_path );", fn)

    def test_a_failed_attachment_leaves_nothing_in_uploads(self):
        fn = _function(_read(SHARED), "onionpress_social_store_upload")
        self.assertIn("if ( is_wp_error( $attach_id ) || ! $attach_id ) {\n        @unlink( $dest_path );", fn)


@unittest.skipUnless(shutil.which("php"), "php not installed")
class TestTheyParse(unittest.TestCase):

    def test_php_lint(self):
        for path in (SHARED, *map(_importer, SOURCES)):
            with self.subTest(file=os.path.basename(path)):
                r = subprocess.run(["php", "-l", path], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
