#!/usr/bin/env python3
"""The Following list: one stored list, a fresh cache, a complete follow id.

Issue #315, three defects found together:

1. Settings read onionpress_following with the default follows (OnionHome,
   OnionHeaven) as its fallback; the sidebar, the Blogroll page and the
   aggregator read it with an empty one, and nothing ever stored the
   defaults. A fresh site showed two follows in Settings, stuck at "Not
   checked yet", and nothing in public. Now the defaults are stored once,
   on a site's first request, and every reader falls back to empty.
2. The aggregated feed cache was cleared on update_option_onionpress_following
   only. A site's first write adds the option, which fires add_option_*, so
   the first follow stayed out of the sidebar for up to an hour.
3. OnionHome's /follow?name= page showed and deep-linked the bare onion
   address, which is the follow key of the network root, not of the name's
   subsite (#260). It now gives <addr>.onion/<onionname>, as the site's own
   /follow page does. Its deep link also went through esc_url(), which
   empties an onionpress: URL.

There is no PHP in the test environment, so these read the source.
"""

import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESOURCES = os.path.join(ROOT, "app", "Resources")
PLUGINS = os.path.join(RESOURCES, "plugins")
BLOGROLL = os.path.join(PLUGINS, "onionpress-blogroll.php")
SETTINGS = os.path.join(PLUGINS, "onionpress-settings.php")
DIRECTORY = os.path.join(PLUGINS, "onionpress-directory.php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(src):
    """PHP source without its docblocks and comment lines."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("//"))


def _function(src, name):
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _php_sources():
    for base in (os.path.join(RESOURCES, "plugins"),
                 os.path.join(RESOURCES, "themes")):
        for dirpath, _, files in os.walk(base):
            for name in files:
                if name.endswith(".php"):
                    yield os.path.join(dirpath, name)


class TestDefaultFollowsAreStored(unittest.TestCase):

    def setUp(self):
        self.blogroll = _code(_read(BLOGROLL))

    def test_seeded_on_init_only_when_absent(self):
        body = _function(self.blogroll, "onionpress_follow_seed_defaults")
        self.assertIn("false === get_option( 'onionpress_following', false )", body)
        self.assertIn(
            "add_option( 'onionpress_following', onionpress_default_follows() )",
            body,
            "add_option, never update_option: a list the administrator "
            "emptied must stay empty.")
        self.assertNotIn("update_option", body)
        self.assertIn("add_action( 'init', 'onionpress_follow_seed_defaults' )",
                      self.blogroll)

    def test_every_reader_falls_back_to_empty(self):
        """The stored option is the one list. A reader that falls back to
        the defaults shows follows the public pages do not have."""
        allowed = re.compile(
            r"get_option\(\s*'onionpress_following'\s*,\s*"
            r"(?:array\(\s*\)|false|null)\s*\)")
        found = 0
        for path in _php_sources():
            code = _code(_read(path))
            for m in re.finditer(r"get_option\(\s*'onionpress_following'\s*[,)]",
                                 code):
                found += 1
                self.assertTrue(
                    allowed.match(code, m.start()),
                    f"{os.path.relpath(path, ROOT)} reads onionpress_following "
                    f"with a default other than empty: "
                    f"{code[m.start():m.start() + 80]!r}")
        self.assertGreaterEqual(found, 4)


class TestFirstFollowClearsTheCache(unittest.TestCase):

    def test_add_and_update_both_flush(self):
        code = _code(_read(BLOGROLL))
        for hook in ("add_option_onionpress_following",
                     "update_option_onionpress_following"):
            self.assertIn(
                f"add_action( '{hook}', 'onionpress_blogroll_flush_items' )",
                code)
        body = _function(code, "onionpress_blogroll_flush_items")
        self.assertIn("delete_transient( 'onionpress_blogroll_items' )", body)

    def test_follow_handler_flushes_after_its_probe(self):
        """The probe stores the feed URL and stats after the list itself."""
        code = _code(_read(SETTINGS))
        start = code.index("if ( ! empty( $_POST['onionpress_follow_address'] ) )")
        handler = code[start:code.index("exit;", start)]
        probe = handler.index("update_option( 'onionpress_following_feeds', $feeds )")
        flush = handler.rindex("delete_transient( 'onionpress_blogroll_items' )")
        self.assertLess(probe, flush)


class TestOnionHomeFollowPageGivesTheOnionname(unittest.TestCase):

    def setUp(self):
        self.body = _function(_code(_read(DIRECTORY)),
                              "onionpress_directory_handle_follow_by_name")

    def test_follow_id_carries_the_name(self):
        self.assertIn("$follow_id .= '/' . $onionname;", self.body)
        self.assertIn("$addr_h = esc_html( $follow_id );", self.body)
        self.assertIn("$deep_link = 'onionpress://follow/' . $follow_id;",
                      self.body)
        self.assertNotIn("'onionpress://follow/' . $info['onionaddress']",
                         self.body)

    def test_name_is_checked_before_it_joins_the_link(self):
        m = re.search(r"preg_match\( '(/[^']*/D)', \$onionname \)", self.body)
        self.assertTrue(m, "the registry's onionname must be checked")
        pattern = m.group(1)[1:-2].replace("$", r"\Z")
        rx = re.compile(pattern)
        for good in ("alice", "bob.smith", "a-b_c9"):
            self.assertTrue(rx.match(good), good)
        for bad in ("", "a/../b", "x y", "-lead", "trail-", 'a"b', "a\n",
                    "Alice"):
            self.assertFalse(rx.match(bad), repr(bad))
        self.assertIn("strtolower( (string) ( $info['onionname'] ?? '' ) )",
                      self.body)

    def test_deep_link_is_not_emptied_by_esc_url(self):
        """esc_url() returns '' for a scheme outside wp_allowed_protocols()."""
        self.assertNotIn("esc_url( $deep_link )", self.body)
        self.assertIn("esc_attr( $deep_link )", self.body)


if __name__ == "__main__":
    unittest.main()
