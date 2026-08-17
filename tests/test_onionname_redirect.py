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

php is preinstalled on ubuntu-latest, so this runs in the existing Python CI
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

    def test_the_name_is_not_appended_as_a_path(self):
        """The bug itself, stated as an assertion."""
        self.assertNotIn("william-blake", self._target())

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


class TestReservedSegmentsAreNotNames(unittest.TestCase):
    """Treating the first segment as a candidate name means WordPress's own
    paths would otherwise be looked up in the registry."""

    def setUp(self):
        self.src = _read("app/Resources/plugins/onionpress-directory.php")

    def test_wordpress_paths_are_excluded(self):
        for segment in ("wp-admin", "wp-json", "wp-login", "feed"):
            self.assertIn(
                f"'{segment}'", self.src,
                f"{segment} must be reserved, not treated as an onionname.",
            )

    def test_the_dispatcher_consults_the_reserved_list(self):
        dispatch = self.src.index("add_action( 'parse_request'")
        self.assertIn(
            "onionpress_directory_is_reserved_segment", self.src[dispatch:],
            "The dispatcher must skip reserved segments before looking a "
            "name up.",
        )

    def test_deep_paths_reach_the_name_handler(self):
        """The old dispatcher skipped anything containing a slash, so deep
        paths got no redirect at all."""
        dispatch = self.src.index("add_action( 'parse_request'")
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

    def test_both_clearnet_checks_use_the_shared_helper(self):
        self.assertEqual(
            self.src.count("onionpress_directory_request_is_clearnet()"), 3,
            "Expected one definition and two call sites (the follow page and "
            "the name lookup).",
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
