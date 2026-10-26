#!/usr/bin/env python3
"""The onionname directory's clearnet guard.

`https://onionpress.org/<name>` is the URL a publisher shows the user as
their onion name, so it is the product's human-readable identity. It was
302ing clearnet browsers straight at a raw .onion URL, which no clearnet
browser can open — the plugin has a dedicated branch to serve a "you need
Tor Browser" page instead, and that branch was dead code: it tested `$own`,
a variable assigned only in a *different* function, so the comparison was
always false and control fell through to the redirect.

An undefined variable in PHP is a warning, not an error, which is exactly
why this survived: the page still rendered, the redirect still fired, and
nothing in the suite looked at the host. These tests run the real helper
under the real PHP, and pin the two call sites that must route through it.

php is preinstalled on ubuntu-24.04, so this runs in the existing Python CI
job — no separate PHP job to collide with the receiver work in flight.
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
PLUGIN = os.path.join(
    PROJECT_ROOT, "app", "Resources", "plugins", "onionpress-directory.php"
)

PHP = shutil.which("php")


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(PHP, "php not installed")
class TestClearnetHostDetection(unittest.TestCase):
    """The helper itself, under real PHP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-directory-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _is_clearnet(self, host):
        """Load the real plugin with a minimal WordPress stub and ask it."""
        harness = textwrap.dedent(f"""\
            <?php
            // The plugin is a mu-plugin: it refuses to load outside WP and
            // registers one hook at the bottom. Both are cheap to satisfy.
            define('ABSPATH', __DIR__);
            function add_action(...$args) {{}}

            $_SERVER['HTTP_HOST'] = {json.dumps(host)};
            require {json.dumps(PLUGIN)};

            echo json_encode([
                'host' => onionpress_directory_request_host(),
                'clearnet' => onionpress_directory_request_is_clearnet(),
            ]);
        """)
        path = os.path.join(self.tmp, "harness.php")
        with open(path, "w") as f:
            f.write(harness)

        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             path],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # An undefined variable is only a warning; surface it as a failure so
        # this class of bug cannot come back quietly.
        self.assertNotIn("Warning", proc.stderr, proc.stderr)
        self.assertNotIn("Undefined", proc.stderr, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_clearnet_bridge_is_recognised(self):
        self.assertTrue(self._is_clearnet("onionpress.org")["clearnet"])

    def test_the_www_form_is_recognised(self):
        self.assertTrue(self._is_clearnet("www.onionpress.org")["clearnet"])

    def test_case_and_port_do_not_defeat_it(self):
        result = self._is_clearnet("OnionPress.org:8443")
        self.assertEqual(result["host"], "onionpress.org")
        self.assertTrue(result["clearnet"])

    def test_an_onion_host_is_not_clearnet(self):
        """On the onion side the redirect is the correct behaviour — the
        visitor is already in Tor Browser."""
        onion = "op2ykvbdwzg75f3pifywmwdjue5utsg4yi4j72wadpk32varfcdk6uad.onion"
        self.assertFalse(self._is_clearnet(onion)["clearnet"])

    def test_a_missing_host_is_not_clearnet(self):
        self.assertFalse(self._is_clearnet("")["clearnet"])


@unittest.skipUnless(PHP, "php not installed")
class TestNameResolvesToTheOnionRoot(unittest.TestCase):
    """Where the redirect points.

    The target was built as <onion>/<name>/ on the theory that
    onionpress-user-path.php would rewrite that into an author archive. That
    rewriter only runs on a network-root multisite install, so on a
    self-hosted node serving one site at the onion root every claimed name
    404'd. A name is registry state, not site state: it resolves to the
    site's root, and any path after it belongs to the target.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-target-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addr = (
            "op2ykvbdwzg75f3pifywmwdjue5utsg4yi4j72wadpk32varfcdk6uad.onion"
        )

    def _target(self, suffix="", query=""):
        harness = textwrap.dedent(f"""\
            <?php
            define('ABSPATH', __DIR__);
            function add_action(...$args) {{}}
            $_SERVER['HTTP_HOST'] = 'onionpress.org';
            $_SERVER['QUERY_STRING'] = {json.dumps(query)};
            require {json.dumps(PLUGIN)};
            echo onionpress_directory_target_url(
                {json.dumps(self.addr)}, {json.dumps(suffix)}
            );
        """)
        path = os.path.join(self.tmp, "target.php")
        with open(path, "w") as f:
            f.write(harness)
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             path],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Warning", proc.stderr, proc.stderr)
        return proc.stdout

    def test_a_bare_name_resolves_to_the_site_root(self):
        self.assertEqual(self._target(), f"http://{self.addr}/")

    def test_a_deep_path_is_carried_through(self):
        """A name is only worth having if you can link to a page."""
        self.assertEqual(
            self._target("posts/hello"),
            f"http://{self.addr}/posts/hello",
        )

    def test_a_query_string_survives(self):
        self.assertEqual(
            self._target("posts/hello", "utm_source=x"),
            f"http://{self.addr}/posts/hello?utm_source=x",
        )

    def test_a_leading_slash_on_the_suffix_does_not_double_up(self):
        self.assertEqual(
            self._target("/posts/hello"), f"http://{self.addr}/posts/hello"
        )


@unittest.skipUnless(PHP, "php not installed")
class TestOnlyOnionHomeAnswersVisitors(unittest.TestCase):
    """Issue #394: the two request hooks run only on the node whose own
    address is OnionHome's. The Host header is the visitor's to choose,
    so `Host: onionpress.org` used to make every node look names up
    (through Tor, 30 s, inside an Apache worker) and redirect."""

    ONIONHOME = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
    OTHER = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz2345.onion"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-directory-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _request_path(self, own_address, host, uri="/alice/posts/1"):
        addr_file = os.path.join(self.tmp, "onion_address")
        if own_address is None:
            if os.path.exists(addr_file):
                os.remove(addr_file)
        else:
            with open(addr_file, "w") as f:
                f.write(own_address + "\n")
        harness = textwrap.dedent(f"""\
            <?php
            define('ABSPATH', __DIR__);
            define('ONIONPRESS_ONION_ADDRESS_FILE', {json.dumps(addr_file)});
            function add_action(...$args) {{}}
            $_SERVER['HTTP_HOST'] = {json.dumps(host)};
            $_SERVER['REQUEST_METHOD'] = 'GET';
            $_SERVER['REQUEST_URI'] = {json.dumps(uri)};
            require {json.dumps(PLUGIN)};
            echo json_encode([
                'node' => onionpress_directory_is_onionhome_node(),
                'path' => onionpress_directory_request_path(),
            ]);
        """)
        path = os.path.join(self.tmp, "harness.php")
        with open(path, "w") as f:
            f.write(harness)
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr", path],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Warning", proc.stderr, proc.stderr)
        return json.loads(proc.stdout)

    def test_onionhome_answers_on_its_hosts(self):
        for host in (self.ONIONHOME, "onionpress.org", "www.onionpress.org"):
            with self.subTest(host=host):
                self.assertEqual(self._request_path(self.ONIONHOME, host),
                                 {"node": True, "path": "alice/posts/1"})

    def test_another_node_answers_nobody_whatever_the_host(self):
        for host in ("onionpress.org", self.ONIONHOME, self.OTHER):
            with self.subTest(host=host):
                self.assertEqual(self._request_path(self.OTHER, host),
                                 {"node": False, "path": None})

    def test_a_node_without_an_address_yet_answers_nobody(self):
        self.assertEqual(self._request_path(None, "onionpress.org"),
                         {"node": False, "path": None})
        self.assertEqual(self._request_path("garbage", "onionpress.org"),
                         {"node": False, "path": None})


class TestVisitorLookupsAreGatedAndCached(unittest.TestCase):
    """The static half of #394: the hooks go through request_path() (the
    gate), the visitor handlers through the negative cache, and the
    Settings page's own calls stay direct."""

    def setUp(self):
        self.src = _read("app/Resources/plugins/onionpress-directory.php")

    def test_the_gate_is_the_own_address_not_the_host(self):
        fn = self.src[self.src.index("function onionpress_directory_request_path()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertLess(fn.index("onionpress_directory_is_onionhome_node()"),
                        fn.index("onionpress_directory_is_onionhome_host("))
        node = self.src[self.src.index("function onionpress_directory_is_onionhome_node()"):]
        node = node[:node.index("\n}\n")]
        self.assertIn("onionpress_directory_own_address() === ONIONPRESS_ONIONHOME_ADDRESS", node)
        self.assertNotIn("HTTP_HOST", node)

    def test_both_hooks_start_from_request_path(self):
        for hook in ("add_action( 'parse_request'", "add_action( 'template_redirect'"):
            body = self.src[self.src.index(hook):]
            body = body[:body.index("onionpress_directory_handle_")]
            self.assertIn("onionpress_directory_request_path()", body)

    def test_visitor_handlers_use_the_negative_cache(self):
        for fn in ("onionpress_directory_handle_follow_by_name",
                   "onionpress_directory_handle_name_lookup"):
            body = self.src[self.src.index(f"function {fn}("):]
            body = body[:body.index("\n}\n")]
            with self.subTest(handler=fn):
                self.assertIn("onionpress_directory_lookup_for_visitor( $name )", body)
                self.assertNotIn("onionpress_directory_lookup( $name )", body)
        cache = self.src[self.src.index("function onionpress_directory_lookup_for_visitor("):]
        cache = cache[:cache.index("\n}\n")]
        self.assertIn("get_transient( $key )", cache)
        self.assertIn("set_transient( $key, 1, ONIONPRESS_DIRECTORY_MISS_TTL )", cache)
        self.assertIn("const ONIONPRESS_DIRECTORY_MISS_TTL = 60;", self.src)

    def test_the_settings_page_keeps_the_direct_lookup(self):
        settings = _read("app/Resources/plugins/onionpress-settings.php")
        self.assertIn("onionpress_directory_lookup( $name )", settings)
        self.assertNotIn("lookup_for_visitor", settings)


class TestNameLookupIsGatedOn404(unittest.TestCase):
    """The /NAME[/REST] dispatch only runs once WordPress itself has failed
    to find anything at the URL — otherwise every WordPress route (however
    it's based: categories, authors, search, child pages, sitemaps) would
    cost a registry round-trip, and a registered name could shadow the
    site's own page at the same slug."""

    def setUp(self):
        self.src = _read("app/Resources/plugins/onionpress-directory.php")

    def test_the_dispatcher_is_gated_on_404(self):
        dispatch = self.src.index("add_action( 'template_redirect'")
        body = self.src[dispatch:]
        self.assertIn(
            "is_404()", body,
            "The name dispatch must only run once WordPress has already "
            "failed to serve the URL, or it would shadow the site's own "
            "routes.",
        )
        self.assertIn("onionpress_directory_handle_name_lookup", body)
        self.assertTrue(
            body.rstrip().endswith("}, 1 );"),
            "Must run at priority 1, before core's redirect_canonical "
            "(priority 10), or a guessed nearby post wins the 404 first.",
        )

    def test_deep_paths_reach_the_name_handler(self):
        """The old dispatcher skipped anything containing a slash, so deep
        paths got no redirect at all."""
        dispatch = self.src.index("add_action( 'template_redirect'")
        body = self.src[dispatch:]
        self.assertNotIn(
            "strpos( $path, '/' ) === false", body,
            "The single-segment restriction is what stopped deep paths from "
            "resolving; it must not come back.",
        )
        self.assertIn("explode( '/', $path, 2 )", body)


class TestClearnetGuardIsWiredUp(unittest.TestCase):
    """The call sites. The helper being right is worth nothing if the
    name-lookup path does not consult it before redirecting."""

    def setUp(self):
        self.src = _read("app/Resources/plugins/onionpress-directory.php")

    def test_no_undefined_own_variable_remains(self):
        """The original bug, stated directly: $own is assigned in
        handle_follow_by_name and was read in handle_name_lookup."""
        reads = [
            line for line in self.src.splitlines()
            if re.search(r"\$own\b", line) and not line.strip().startswith("*")
        ]
        self.assertEqual(
            reads, [],
            "$own is back. Use onionpress_directory_request_is_clearnet() "
            "rather than re-deriving the host in a second place.",
        )

    def test_the_guard_precedes_the_onion_redirect(self):
        """A clearnet visitor must be answered with the Tor Browser page, not
        handed a .onion URL their browser cannot open."""
        lookup = self.src.index("function onionpress_directory_handle_name_lookup")
        body = self.src[lookup:]
        guard = body.index("onionpress_directory_request_is_clearnet()")
        redirect = body.index("wp_redirect(")
        self.assertLess(
            guard, redirect,
            "The clearnet guard must run before wp_redirect(), or clearnet "
            "visitors are redirected to a raw .onion URL.",
        )


if __name__ == "__main__":
    unittest.main()
