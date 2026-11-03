#!/usr/bin/env python3
"""Linux runs the #266 recovery step (issues #310, #407).

The dual probe, the RepublishTrigger, the wake reset and the request to
tor-watchdog lived in linux/onionpress-service.py, which no installer
shipped and no unit started; then in onionpress.reachability's
ReachabilityLoop, run by the heartbeat client; since #407 the daemon
(onionpress.daemon) runs the one status pass on both platforms, with the
one RecoveryWindow (tests/test_mac_daemon.py drives it, as the Mac;
tests/test_linux_daemon.py the Linux seams). What stays here: the #480
rule that neither platform touches the trigger outside RecoveryWindow,
and the census that keeps any Linux entry point from being left out of
the installers.
"""
import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as fh:
        return fh.read()


class TestOneRecoveryDriver(unittest.TestCase):
    """Issue #480: the #266 decision (trigger, request, wake and offline
    resets) is reachability.RecoveryWindow's; the daemon, the one driver
    on both platforms, neither builds nor feeds a RepublishTrigger."""

    def test_the_daemon_owns_one_recovery_window(self):
        daemon = _read("src/onionpress/daemon.py")
        self.assertEqual(daemon.count("RecoveryWindow("), 1)
        self.assertIn("self._recovery.step(self._tor_internally_ready, tor_reachable,", daemon)
        self.assertIn("self._recovery.note_wake()", daemon)
        self.assertIn("self._recovery.note_offline()", daemon)

    def test_no_second_driver(self):
        reach = _read("src/onionpress/reachability.py")
        self.assertNotIn("class ReachabilityLoop", reach)
        self.assertNotIn("reachability.json", reach.replace("reachability.json for the bash", ""))
        self.assertFalse(os.path.exists(os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py")))
        self.assertNotIn("reachability.json", _read("linux/onionpress"))
        self.assertNotIn("reachability.json", _read("src/onionpress/status_writer.py"))

    def test_the_driver_touches_no_trigger(self):
        daemon = _read("src/onionpress/daemon.py")
        self.assertNotIn("RepublishTrigger(", daemon)
        self.assertNotIn(".observe(", daemon)
        self.assertNotIn("request_watchdog_republish", daemon)
        self.assertNotIn('"REPUBLISH_ON_UNREACHABLE",', daemon)


class TestLinuxEntryPointsAreInstalled(unittest.TestCase):
    """Every Python program in linux/ is installed by both installers.

    onionpress-service.py was the whole Linux half of #266 and neither
    installer copied it. A file that is deliberately not shipped goes in
    DEV_ONLY with the reason.
    """

    DEV_ONLY = {}

    def _entry_points(self):
        out = []
        for name in sorted(os.listdir(os.path.join(PROJECT_DIR, "linux"))):
            path = os.path.join(PROJECT_DIR, "linux", name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                first = fh.readline()
            if name.endswith(".py") or re.match(rb"#!.*python", first):
                out.append(name)
        return out

    def test_found_the_known_ones(self):
        # The tray is the one Python program left in linux/ (#407: the
        # heartbeat client is the daemon's, in the package).
        self.assertEqual(self._entry_points(), ["onionpress-tray"])

    def test_each_is_installed(self):
        # linux/install-tree.sh is the one list both installers (and
        # `onionpress update`) install from.
        tree = _read("linux/install-tree.sh")
        self.assertIn("linux/install-tree.sh", _read("linux/install.sh"))
        self.assertIn("linux/install-tree.sh", _read("build/build-linux.sh"))
        for name in self._entry_points():
            if name in self.DEV_ONLY:
                continue
            with self.subTest(name):
                self.assertRegex(tree, rf"(?m)^file\s+linux/{re.escape(name)}\s",
                                 "linux/install-tree.sh")

    def test_dev_only_entries_exist(self):
        for name in self.DEV_ONLY:
            self.assertTrue(os.path.isfile(os.path.join(PROJECT_DIR, "linux", name)))


if __name__ == "__main__":
    unittest.main()
