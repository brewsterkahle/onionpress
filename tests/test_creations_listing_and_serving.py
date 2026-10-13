#!/usr/bin/env python3
"""My Creations: cached listings, ranged serving, uploads (#319).

  - get_folder_contents() scanned its folder and stat()ed every entry over
    the host mount on every page view, the access pattern behind the 34-hour
    outage that only get_files() had been fixed for. Both listings now go
    through cached_listing(): a transient per folder, rebuilt when it is old,
    when the folder's (or its thumbnails') mtime changes, or when an upload
    raises the network-wide generation; while one request rebuilds, the
    others get the previous listing. Cached entries hold no URLs: the domain
    map makes home_url() the request's host.
  - serve_file() sent every file whole: no Range, no ETag. It now answers
    If-None-Match/If-Modified-Since with 304, one byte range with 206 (416
    when unsatisfiable, If-Range respected), and streams in pieces.
  - The Mac's thumbnail generator: test_creations_thumbnails.py.
  - PHP's max_file_uploads (20) dropped the rest of a larger upload without
    a word, and a failed move_uploaded_file() was ignored. The limit is 200,
    the form checks it (and post_max_size) before sending and tells the
    server how many files it sent, and handle_upload() reports every file
    that did not arrive.

php is preinstalled on ubuntu-latest, so the PHP half runs in CI; locally
it is skipped when php is missing.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                      "onionpress-creations", "onionpress-creations.php")
THEME_PAGE = os.path.join(PROJECT_ROOT, "app", "Resources", "themes",
                          "onionpress", "page-creations.php")
UPLOADS_INI = os.path.join(PROJECT_ROOT, "app", "Resources", "docker",
                           "wordpress", "onionpress-uploads.ini")
PHP = shutil.which("php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _php_function(src, name):
    """Body of PHP function *name* (from its `{` to the matching `}`)."""
    m = re.search(r"function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found"
    depth, i = 0, m.end() - 1
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1


def _run_php(test, body, stdin=""):
    """Run *body* after loading the plugin with small WordPress stubs
    (transients and site options in memory); return its JSON output."""
    tmp = tempfile.mkdtemp(prefix="onionpress-creations-test-")
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    harness = textwrap.dedent("""\
        <?php
        define('ABSPATH', __DIR__);
        function add_action(...$a) {}
        function add_filter(...$a) {}
        function register_activation_hook(...$a) {}
        $GLOBALS['t'] = array();
        $GLOBALS['o'] = array();
        function get_transient($k) { return isset($GLOBALS['t'][$k]) ? $GLOBALS['t'][$k] : false; }
        function set_transient($k, $v, $ttl = 0) { $GLOBALS['t'][$k] = $v; return true; }
        function delete_transient($k) { unset($GLOBALS['t'][$k]); return true; }
        function get_site_option($k, $d = false) { return isset($GLOBALS['o'][$k]) ? $GLOBALS['o'][$k] : $d; }
        function update_site_option($k, $v) { $GLOBALS['o'][$k] = $v; return true; }
        require %s;
        function call_private($name, ...$args) {
            $m = new ReflectionMethod('OnionPress_Creations', $name);
            $m->setAccessible(true);
            return $m->invoke(null, ...$args);
        }
        """) % json.dumps(PLUGIN) + body
    path = os.path.join(tmp, "harness.php")
    with open(path, "w") as f:
        f.write(harness)
    proc = subprocess.run(
        [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
         path],
        input=stdin, capture_output=True, text=True, timeout=60,
    )
    test.assertEqual(proc.returncode, 0, proc.stderr)
    test.assertNotIn("Warning", proc.stderr, proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipUnless(PHP, "php not installed")
class TestRangesUnderPhp(unittest.TestCase):

    CASES = [
        # header, size, expected (None = whole file, False = 416)
        ("bytes=0-99", 1000, [0, 99]),
        ("bytes=100-", 1000, [100, 999]),
        ("bytes=-100", 1000, [900, 999]),
        ("bytes=-5000", 1000, [0, 999]),
        ("bytes=990-5000", 1000, [990, 999]),
        ("bytes=999-999", 1000, [999, 999]),
        ("BYTES = 5 - 9", 1000, [5, 9]),
        ("bytes=0-99999999999999999999999", 1000, [0, 999]),
        ("bytes=1000-", 1000, False),
        ("bytes=99999999999999999999999-", 1000, False),
        ("bytes=-0", 1000, False),
        ("bytes=0-", 0, False),
        ("bytes=-1", 0, False),
        ("bytes=5-4", 1000, None),
        ("bytes=-", 1000, None),
        ("bytes=0-1,5-9", 1000, None),
        ("items=0-1", 1000, None),
        ("bytes=a-b", 1000, None),
        ("", 1000, None),
    ]

    def test_parse_range(self):
        got = _run_php(self, """
            $out = array();
            foreach (json_decode(file_get_contents('php://stdin')) as $c) {
                $out[] = OnionPress_Creations::parse_range($c[0], $c[1]);
            }
            echo json_encode($out);
        """, json.dumps([[h, n] for h, n, _ in self.CASES]))
        for (header, size, want), have in zip(self.CASES, got):
            self.assertEqual(have, want, f"{header!r} of {size}")

    def test_etag_matching(self):
        got = _run_php(self, """
            $e = OnionPress_Creations::etag(1700000000, 12345);
            echo json_encode(array(
                $e,
                OnionPress_Creations::etag_matches($e, $e),
                OnionPress_Creations::etag_matches('W/' . $e, $e),
                OnionPress_Creations::etag_matches('"x", ' . $e, $e),
                OnionPress_Creations::etag_matches('*', $e),
                OnionPress_Creations::etag_matches('"x"', $e),
                OnionPress_Creations::etag_matches('', $e),
                OnionPress_Creations::etag(1700000001, 12345) === $e,
            ));
        """)
        self.assertEqual(got[0], '"6553f100-3039"')
        self.assertEqual(got[1:], [True, True, True, True, False, False, False])


@unittest.skipUnless(PHP, "php not installed")
class TestCachedListingUnderPhp(unittest.TestCase):

    def test_rebuilds_only_when_due(self):
        got = _run_php(self, """
            $builds = 0;
            $build = function () use (&$builds) { $builds++; return array('n' => $builds); };
            $k = 'op_test';
            $r = array();
            $r[] = call_private('cached_listing', $k, 's1', 300, $build);  // build 1
            $r[] = call_private('cached_listing', $k, 's1', 300, $build);  // cached
            $r[] = call_private('cached_listing', $k, 's2', 300, $build);  // stamp: build 2
            OnionPress_Creations::clear_listing_caches();
            $r[] = call_private('cached_listing', $k, 's2', 300, $build);  // upload: build 3
            // Expired, but another request holds the rebuild lock: stale copy.
            $GLOBALS['t'][$k]['built'] -= 1000;
            set_transient($k . '_lock', 1);
            $r[] = call_private('cached_listing', $k, 's2', 300, $build);
            delete_transient($k . '_lock');
            $r[] = call_private('cached_listing', $k, 's2', 300, $build);  // build 4
            $r[] = isset($GLOBALS['t'][$k . '_lock']);
            echo json_encode(array($r, $builds));
        """)
        results, builds = got
        self.assertEqual([x["n"] if isinstance(x, dict) else x for x in results],
                         [1, 1, 2, 3, 3, 4, False])
        self.assertEqual(builds, 4)

    def test_no_cache_without_a_previous_listing_waits_for_nothing(self):
        # A held lock with nothing cached yet still builds: the page never
        # comes out empty because another request is scanning.
        got = _run_php(self, """
            set_transient('op_x_lock', 1);
            echo json_encode(call_private('cached_listing', 'op_x', 's', 300,
                function () { return array('built'); }));
        """)
        self.assertEqual(got, ["built"])


class TestListingsAreCached(unittest.TestCase):
    def setUp(self):
        self.src = _read(PLUGIN)

    def test_both_listings_go_through_the_cache(self):
        self.assertIn("self::cached_listing(", _php_function(self.src, "get_folder_contents"))
        self.assertIn("self::cached_listing(", _php_function(self.src, "get_files"))

    def test_cached_entries_hold_no_urls(self):
        for fn in ("scan_folder", "scan_tree", "cached_listing"):
            body = _php_function(self.src, fn)
            self.assertNotIn("home_url(", body, fn)
            self.assertNotIn("add_query_arg(", body, fn)

    def test_an_upload_clears_every_sites_listings(self):
        body = _php_function(self.src, "handle_upload")
        self.assertIn("self::clear_listing_caches();", body)
        self.assertIn("update_site_option(self::CACHE_GENERATION_OPTION",
                      _php_function(self.src, "clear_listing_caches"))
        self.assertIn("get_site_option(self::CACHE_GENERATION_OPTION",
                      _php_function(self.src, "cached_listing"))

    def test_cache_key_renamed_with_its_contents(self):
        self.assertIn("const FILES_CACHE_KEY = 'op_creations_files_v3';", self.src)


class TestServeFileRanges(unittest.TestCase):
    def setUp(self):
        self.body = _php_function(_read(PLUGIN), "serve_file")

    def test_validators_and_ranges(self):
        for needle in ("header('ETag: ' . $etag);", "header('Accept-Ranges: bytes');",
                       "$_SERVER['HTTP_IF_NONE_MATCH']", "$_SERVER['HTTP_RANGE']",
                       "$_SERVER['HTTP_IF_RANGE']", "status_header(206);",
                       "status_header(416);", "self::parse_range("):
            self.assertIn(needle, self.body)

    def test_streams_instead_of_reading_whole(self):
        self.assertNotIn("readfile(", self.body)
        self.assertIn("self::send_bytes($filepath, $first, $length);", self.body)
        send = _php_function(_read(PLUGIN), "send_bytes")
        self.assertIn("fread($fh, min(self::SEND_CHUNK, $length))", send)
        self.assertIn("connection_aborted()", send)


class TestUploadsReportEveryMissingFile(unittest.TestCase):
    def test_php_accepts_more_than_twenty_files(self):
        m = re.search(r"(?m)^max_file_uploads\s*=\s*(\d+)\s*$", _read(UPLOADS_INI))
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 100)

    def test_failed_moves_and_dropped_files_are_reported(self):
        body = _php_function(_read(PLUGIN), "handle_upload")
        self.assertIn("if (!move_uploaded_file($files['tmp_name'][$i], $dest)) {", body)
        self.assertIn("$failed[] = array($shown, self::upload_error_text($error));", body)
        self.assertIn("$_POST['creations_upload_count']", body)
        self.assertIn("if ($refused || $failed || $dropped) {", body)

    def test_form_sends_its_count_and_checks_the_limits(self):
        page = _read(THEME_PAGE)
        self.assertIn('name="creations_upload_count"', page)
        self.assertIn("OnionPress_Creations::upload_limits()", page)
        self.assertIn("countField.value = files.length;", page)
        self.assertIn("files.length > maxFiles", page)
        self.assertIn("total > maxBytes", page)


if __name__ == "__main__":
    unittest.main()
