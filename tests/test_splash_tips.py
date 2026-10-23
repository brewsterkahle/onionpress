#!/usr/bin/env python3
"""The launch splash rotates "what you can do" tips while it waits (#166).

The splash is AppKit and only runs inside the built app, so what can be
pinned here is the part that was pulled out of it: the tip copy and the
rotation order (``onionpress.splash_tips``), plus text-level checks that
``src/menubar.py`` wires the timer up and tears it down, and that the
module is bundled by py2app.
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import splash_tips  # noqa: E402
from onionpress.splash_tips import SPLASH_TIPS, TIP_MAX_CHARS, tip_at  # noqa: E402


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class TestTipRotation(unittest.TestCase):
    def test_starts_with_first_tip_and_cycles_in_order(self):
        tips = ("a", "b", "c")
        self.assertEqual([tip_at(i, tips) for i in range(7)],
                         ["a", "b", "c", "a", "b", "c", "a"])

    def test_default_tips_wrap_around(self):
        n = len(SPLASH_TIPS)
        self.assertEqual(tip_at(0), SPLASH_TIPS[0])
        self.assertEqual(tip_at(n), SPLASH_TIPS[0])
        self.assertEqual(tip_at(n - 1), SPLASH_TIPS[-1])
        self.assertEqual(tip_at(3 * n + 2), SPLASH_TIPS[2])

    def test_empty_tips_never_raise(self):
        self.assertEqual(tip_at(0, ()), "")
        self.assertEqual(tip_at(5, []), "")


class TestTipCopy(unittest.TestCase):
    def test_four_to_six_short_distinct_tips(self):
        self.assertGreaterEqual(len(SPLASH_TIPS), 4)
        self.assertLessEqual(len(SPLASH_TIPS), 6)
        self.assertEqual(len(set(SPLASH_TIPS)), len(SPLASH_TIPS))
        for tip in SPLASH_TIPS:
            self.assertTrue(tip.strip(), "empty tip")
            self.assertLessEqual(
                len(tip), TIP_MAX_CHARS,
                f"tip too long for the splash's two-line label: {tip!r}")

    def test_naming_rules(self):
        """CLAUDE.md: 'OnionPress', 'onion service', never 'hidden service'."""
        for tip in SPLASH_TIPS:
            self.assertNotIn("hidden service", tip.lower(), tip)
            self.assertNotRegex(tip, r"[Oo]nion[.-][Pp]ress", tip)

    def test_tips_name_real_menu_items_and_paths(self):
        """Every menu title or path a tip mentions must exist in menubar.py."""
        menubar = _read("src/menubar.py")
        for needle in ('"Open in Tor Browser"', '"Copy Onion Address"',
                       '"~/OnionPress/backups"'):
            self.assertIn(needle, menubar, needle)
        # The thumbnails (and that folder) are the daemon's (onionpress.daemon).
        self.assertIn('"~/OnionPress/Creations/My Creations"', _read("src/onionpress/daemon.py"))
        joined = " ".join(SPLASH_TIPS)
        self.assertIn("Open in Tor Browser", joined)
        self.assertIn("Copy Onion Address", joined)
        self.assertIn("~/OnionPress", joined)

    def test_interval_is_a_few_seconds(self):
        self.assertGreaterEqual(splash_tips.TIP_INTERVAL_SECONDS, 4)
        self.assertLessEqual(splash_tips.TIP_INTERVAL_SECONDS, 10)


class TestMenubarWiring(unittest.TestCase):
    """Text-level invariants on src/menubar.py (AppKit cannot be imported here)."""

    def setUp(self):
        self.menubar = _read("src/menubar.py")

    def test_splash_shows_first_tip_and_starts_timer(self):
        self.assertIn("splash_tips.tip_at(0)", self.menubar)
        self.assertRegex(
            self.menubar,
            r"def show_launch_splash\(self\):[\s\S]*?self\.start_splash_tip_rotation\(\)"
            r"[\s\S]*?def dismiss_launch_splash",
            "show_launch_splash() must start the tip rotation")

    def test_timer_uses_tip_interval_and_common_modes(self):
        self.assertIn("splash_tips.TIP_INTERVAL_SECONDS", self.menubar)
        self.assertIn("NSRunLoopCommonModes", self.menubar)

    def test_dismiss_stops_timer(self):
        m = re.search(r"def dismiss_launch_splash\(self\):([\s\S]*?)\n    def ", self.menubar)
        self.assertIsNotNone(m)
        self.assertIn("self.stop_splash_tip_rotation()", m.group(1),
                      "dismiss_launch_splash() must invalidate the tip timer")
        m = re.search(r"def stop_splash_tip_rotation\(self\):([\s\S]*?)\n    def ", self.menubar)
        self.assertIsNotNone(m)
        self.assertIn("invalidate()", m.group(1))

    def test_module_is_bundled_by_py2app(self):
        self.assertIn("'onionpress.splash_tips'", _read("setup.py"))


if __name__ == "__main__":
    unittest.main()
