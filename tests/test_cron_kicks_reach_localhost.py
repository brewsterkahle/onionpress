#!/usr/bin/env python3
"""Every "run it now" kick of wp-cron is spawn_cron() (issue #409).

The Mastodon, Bluesky and blog importers' Sync now (and the blog's
Start archiving), and the Wayback page's Kick, scheduled their event and
then posted to site_url( 'wp-cron.php?doing_wp_cron=…' ) themselves.
WP_SITEURL is the request's own host: the owner's http://localhost:8080
names a port nothing listens on inside the wordpress container, and an
onion or clearnet host went out through Tor with a 0.01 s timeout. The
kick did nothing; the sync waited for the next page view's cron.

spawn_cron() builds its loopback through the `cron_request` filter, which
onionpress-tor-proxy.php points at http://localhost (held by
tests/test_blogroll_refresh.py). So no PHP OnionPress ships names
wp-cron.php in code: a kick is spawn_cron(), for the importers through
onionpress_social_run_now().
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES = os.path.join(ROOT, "app", "Resources")
PLUGINS = os.path.join(RESOURCES, "plugins")
SHARED = os.path.join(PLUGINS, "onionpress-social-archive.php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(src):
    """PHP source without its docblocks and comment lines."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith(("//", "#", "*")))


def _function(src, name):
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _php_files():
    for dirpath, _dirs, files in os.walk(RESOURCES):
        for name in files:
            if name.endswith(".php"):
                yield os.path.join(dirpath, name)


class TestCronKicks(unittest.TestCase):

    def test_no_php_builds_a_wp_cron_url(self):
        found = []
        for path in _php_files():
            for line in _code(_read(path)).splitlines():
                if "wp-cron.php" in line:
                    found.append((os.path.relpath(path, ROOT), line.strip()))
        self.assertEqual(found, [], "a wp-cron kick is spawn_cron(), whose "
                         "loopback the cron_request filter sends to http://localhost")

    def test_the_importers_kick_is_spawn_cron(self):
        fn = _function(_read(SHARED), "onionpress_social_run_now")
        steps = ["delete_transient( 'doing_cron' );",
                 "wp_schedule_single_event( time(), $hook );",
                 "spawn_cron();"]
        at = [fn.index(s) for s in steps]
        self.assertEqual(at, sorted(at))

    def test_each_sync_now_goes_through_the_helper(self):
        expected = {"mastodon": 1, "bluesky": 1, "blog": 2}
        for source, count in expected.items():
            src = _code(_read(os.path.join(PLUGINS, f"onionpress-social-archive-{source}.php")))
            hook = f"ONIONPRESS_{source.upper()}_CRON_HOOK"
            with self.subTest(source=source):
                self.assertEqual(src.count(f"onionpress_social_run_now( {hook} );"), count)
                self.assertNotIn("delete_transient( 'doing_cron' )", src)
                self.assertNotIn("wp_schedule_single_event( time(), " + hook, src)

    def test_the_tor_proxy_sends_the_loopback_to_localhost(self):
        proxy = _read(os.path.join(PLUGINS, "onionpress-tor-proxy.php"))
        self.assertIn("add_filter( 'cron_request', 'onionpress_tor_proxy_cron_request', PHP_INT_MAX );",
                      proxy)


if __name__ == "__main__":
    unittest.main()
