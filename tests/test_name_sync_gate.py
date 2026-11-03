#!/usr/bin/env python3
"""The name-sync plugin registers onionnames only while OnionHeaven is on.

onionpress-name-sync.php registers every WordPress user's login with
OnionHome, at creation and in a daily reconcile. It follows the host's
rule (onionnames_registrar.configured_onionhome): nothing while
REGISTER_WITH_ONIONHEAVEN is not on, or ONIONHOME_ADDRESS is set empty,
read from the host's config-current.json; unreadable is no. The gate runs
under php where it is installed; the rest reads the source.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "app", "Resources", "plugins", "onionpress-name-sync.php")
PHP = shutil.which("php")

HARNESS = r"""<?php
define( 'ABSPATH', '/' );
define( 'MINUTE_IN_SECONDS', 60 );
define( 'ONIONPRESS_NAMESYNC_CONFIG', $argv[1] );
function add_action() {}
require $argv[2];
echo onionpress_namesync_allowed() ? 'yes' : 'no';
"""


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestSource(unittest.TestCase):

    def setUp(self):
        self.src = _read(PLUGIN)

    def test_both_registrations_are_gated(self):
        call = self.src[self.src.index("function onionpress_namesync_call("):]
        self.assertLess(call.index("onionpress_namesync_allowed()"), call.index("curl_init"))
        reconcile = self.src[self.src.index("function onionpress_namesync_reconcile_all("):]
        self.assertLess(reconcile.index("onionpress_namesync_allowed()"),
                        reconcile.index("get_users("))

    def test_it_reads_the_hosts_config(self):
        self.assertIn("'/var/lib/onionpress/config-current.json'", self.src)

    def test_the_keys_reach_wordpress(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from onionpress import config
        self.assertIn("REGISTER_WITH_ONIONHEAVEN", config.SAFE_CONFIG_KEYS)
        self.assertIn("ONIONHOME_ADDRESS", config.SAFE_CONFIG_KEYS)


@unittest.skipUnless(PHP, "php not installed")
class TestUnderPhp(unittest.TestCase):

    def allowed(self, config):
        with tempfile.TemporaryDirectory() as d:
            harness = os.path.join(d, "h.php")
            with open(harness, "w") as f:
                f.write(HARNESS)
            path = os.path.join(d, "config-current.json")
            if config is not None:
                with open(path, "w") as f:
                    f.write(config if isinstance(config, str) else json.dumps(config))
            r = subprocess.run([PHP, harness, path, PLUGIN], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip() == "yes"

    def test_on_by_default(self):
        self.assertTrue(self.allowed({}))
        self.assertTrue(self.allowed({"REGISTER_WITH_ONIONHEAVEN": "yes"}))
        self.assertTrue(self.allowed({"REGISTER_WITH_ONIONHEAVEN": ""}))

    def test_off_or_unrecognized(self):
        for value in ("no", "off", "0", "maybe"):
            with self.subTest(value=value):
                self.assertFalse(self.allowed({"REGISTER_WITH_ONIONHEAVEN": value}))

    def test_no_onionhome(self):
        self.assertFalse(self.allowed({"ONIONHOME_ADDRESS": ""}))

    def test_unreadable_is_no(self):
        self.assertFalse(self.allowed(None))
        self.assertFalse(self.allowed("not json"))


if __name__ == "__main__":
    unittest.main()
