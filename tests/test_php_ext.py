"""tests/php_ext.py: a php without an extension a harness needs is a skip
that names the extension, never a failure (a bare php-cli on a developer's
box failed 22 tests on `ZipArchive` and `CURLOPT_PROXY`)."""

import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import php_ext  # noqa: E402


class TestPhpWith(unittest.TestCase):

    def test_no_php(self):
        with mock.patch.object(php_ext, "PHP", None):
            self.assertEqual(php_ext.php_with("zip"), (False, "php not installed"))

    def test_a_missing_extension_is_named(self):
        with mock.patch.object(php_ext, "PHP", "/usr/bin/php"), \
                mock.patch.object(php_ext, "php_has", lambda e: e == "curl"):
            self.assertEqual(php_ext.php_with("curl"), (True, ""))
            ok, reason = php_ext.php_with("curl", "zip")
            self.assertFalse(ok)
            self.assertIn("zip extension", reason)
            self.assertNotIn("curl", reason)

    @unittest.skipUnless(php_ext.PHP, "php not installed")
    def test_asks_the_real_php(self):
        self.assertTrue(php_ext.php_has("standard"))
        self.assertFalse(php_ext.php_has("no_such_extension_onionpress"))


class TestTheHarnessesSayWhatTheyNeed(unittest.TestCase):
    """A module whose php harness reaches ZipArchive or curl skips through
    php_with, not on `which("php")` alone."""

    NEEDS = {
        "test_blogroll_refresh.py": "curl",
        "test_core_update_verified.py": "zip",
        "test_social_zip_limits.py": "zip",
    }

    def test_each_module_asks_for_its_extension(self):
        for name, ext in self.NEEDS.items():
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                src = f.read()
            with self.subTest(name):
                self.assertRegex(src, r'skipUnless\(\*php_with\("%s"\)\)' % re.escape(ext))
