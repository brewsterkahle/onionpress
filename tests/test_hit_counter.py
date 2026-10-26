"""Issue #398: the hit counter has one writer, and it checks its nonce.

The plugin minted `onionpress_counter_nonce` for its script and never
checked it, and `standalone-counter.php` in the same folder was a second,
unauthenticated writer: reachable directly under SHORTINIT, a raw
`INSERT ... ON DUPLICATE KEY UPDATE` on the main site's wp_options for
anyone, whatever site shows the counter.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(PROJECT_ROOT, "app/Resources/plugins/onionpress-hit-counter")
PLUGIN = os.path.join(PLUGIN_DIR, "onionpress-hit-counter.php")
SCRIPT = os.path.join(PLUGIN_DIR, "assets/hit-counter.js")
README = os.path.join(PLUGIN_DIR, "README.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestOneWriter(unittest.TestCase):

    def test_no_standalone_endpoint(self):
        self.assertFalse(os.path.exists(os.path.join(PLUGIN_DIR, "standalone-counter.php")))
        for name in os.listdir(PLUGIN_DIR):
            with self.subTest(file=name):
                if name.endswith(".php"):
                    self.assertEqual(name, "onionpress-hit-counter.php",
                                     "the plugin file is the only PHP: anything else "
                                     "under wp-content/plugins is reachable directly")

    def test_the_plugin_file_exits_without_wordpress(self):
        self.assertIn("if (!defined('ABSPATH')) {\n    exit;", _read(PLUGIN))

    def test_the_plugin_reads_no_host_file(self):
        text = _read(PLUGIN)
        self.assertNotIn("/var/lib/onionpress", text)
        self.assertNotIn("file_get_contents", text)
        self.assertNotIn("$wpdb", text)


class TestNonce(unittest.TestCase):

    def setUp(self):
        self.text = _read(PLUGIN)
        start = self.text.index("public function ajax_increment_counter()")
        self.handler = self.text[start:self.text.index("\n    }\n", start)]

    def test_the_increment_checks_the_nonce_the_page_was_given(self):
        self.assertIn("wp_create_nonce('onionpress_counter_nonce')", self.text)
        self.assertIn("check_ajax_referer('onionpress_counter_nonce', 'nonce', false)", self.handler)
        # A failed check reads the count instead of dying: a page cached
        # past the nonce's life still shows a counter, and moves nothing.
        self.assertLess(self.handler.index("check_ajax_referer"),
                        self.handler.index("$this->increment_counter()"))
        self.assertIn("$this->get_counter()", self.handler)

    def test_the_script_sends_it(self):
        js = _read(SCRIPT)
        self.assertIn("action: 'increment_counter'", js)
        self.assertIn("nonce: onionpressCounter.nonce", js)

    def test_the_read_endpoint_writes_nothing(self):
        start = self.text.index("public function ajax_get_counter()")
        body = self.text[start:self.text.index("\n    }\n", start)]
        self.assertNotIn("increment", body)
        self.assertNotIn("update_option", body)

    def test_readme_describes_the_option_not_a_file(self):
        text = _read(README)
        self.assertNotIn("hit-counter.txt", text)
        self.assertNotIn("/var/lib/onionpress", text)
        self.assertIn("onionpress_hit_counter", text)
        self.assertIn("standalone-counter.php", text)        # the note that it is gone


if __name__ == "__main__":
    unittest.main()
