"""Tests for src/onionpress/analytics_sharing.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.analytics_sharing import _LOG_NAME_RE


class TestLogNameRegex(unittest.TestCase):
    """The client-side name check is intentionally permissive: it exists
    to stop the server from inducing the client to open files outside
    the log directory. Naming conventions are policed server-side."""

    # fullmatch, as upload_logs() uses it: `$` alone would also match
    # before a trailing newline.
    def _assert_match(self, name):
        self.assertIsNotNone(
            _LOG_NAME_RE.fullmatch(name), f"expected {name!r} to match"
        )

    def _assert_no_match(self, name):
        self.assertIsNone(
            _LOG_NAME_RE.fullmatch(name), f"expected {name!r} not to match"
        )

    def test_dated_log_accepted(self):
        for prefix in (
            "onionpress",
            "wordpress-access",
            "wordpress-visitors",
            "container-onionpress-tor",
            "container-tor",
            "container-onionheaven",
            "container-wordpress",
            "clearnet",
            "launcher",
        ):
            self._assert_match(f"{prefix}-2026-04-21-001.log")
            self._assert_match(f"{prefix}-2026-04-21-001.log.gz")

    def test_takeover_numbered_variant(self):
        self._assert_match(
            "container-onionheaven-takeover-5-2026-04-21-001.log"
        )
        self._assert_match(
            "container-onionheaven-takeover-42-2026-04-21-001.log.gz"
        )

    def test_launcher_legacy_symlink_name(self):
        self._assert_match("launcher.log")

    def test_path_traversal_rejected(self):
        self._assert_no_match("../etc/passwd")
        self._assert_no_match("../onionpress-2026-04-21-001.log")
        self._assert_no_match("subdir/onionpress-2026-04-21-001.log")
        self._assert_no_match("a\\b.log")

    def test_hidden_and_empty_rejected(self):
        self._assert_no_match("")
        self._assert_no_match(".hidden.log")
        self._assert_no_match("-dashstart.log")

    def test_oversized_rejected(self):
        self._assert_no_match("a" + "b" * 200 + ".log")

    def test_trailing_newline_rejected(self):
        self._assert_no_match("onionpress-2026-04-21-001.log\n")

    def test_future_naming_variants_accepted(self):
        # Previously these were rejected by the strict regex; we now
        # accept anything that can't escape the site directory so the
        # server can store unknown-but-useful logs without a redeploy.
        self._assert_match("onionpress-2026-4-21-001.log")
        self._assert_match("container-tor-2026-04-21-001.log")
        self._assert_match("container-wordpress-2026-04-21-001.log.gz")
        self._assert_match("newlogtype-2026-04-21.log")


if __name__ == "__main__":
    unittest.main()
