#!/usr/bin/env python3
"""The Linux Settings page's Stop and Restart (issue #354).

Both go through `onionpress handle-action` to the launcher's own `stop`
and `restart` cases (tests/test_handle_action.py: `test_stop`,
`test_restart_applies_config_then_restarts`), and those cases stop the
heartbeat client before the containers go down (so it sends /offline to
the OnionHeaven hub) and start it again after a restart. The page used to
reach only the containers, leaving the heartbeat beating for a site that
was down.

After the page's Stop the `onionpress.service` user unit is still active
(its `start` process keeps running its status loop and holds the PID
lock), so `systemctl --user start onionpress` does nothing and
`onionpress start` is refused; what works is `onionpress restart`. The
page's hint used to name `sudo systemctl start onionpress`, a system unit
current installs do not have.
"""
import os
import re
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import handle_action  # noqa: E402


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel)) as fh:
        return fh.read()


def _case(main, name):
    m = re.search(rf"\n        {re.escape(name)}\)\n(.*?)\n            ;;", main, re.S)
    if m is None:
        raise AssertionError(f"no {name}) case in the Linux launcher's main()")
    return m.group(1)


class TestLinuxLauncherStopAndRestart(unittest.TestCase):

    def setUp(self):
        src = _read("linux/onionpress")
        self.main = src[src.index("\nmain() {"):]

    def test_stop_stops_the_heartbeat_client_before_the_containers(self):
        body = _case(self.main, "stop")
        self.assertIn("stop_heartbeat_client", body)
        self.assertLess(body.index("stop_heartbeat_client"), body.index("stop_containers"))

    def test_restart_stops_and_starts_the_heartbeat_client_around_the_containers(self):
        body = _case(self.main, "restart")
        self.assertLess(body.index("stop_heartbeat_client"), body.index("stop_containers"))
        self.assertLess(body.index("start_containers"), body.index("start_heartbeat_client"))

    def test_stop_heartbeat_client_stops_the_user_unit_too(self):
        # Current installs run the heartbeat as a user unit; an old one as
        # a system unit. Both are stopped.
        src = _read("linux/onionpress")
        fn = src[src.index("\nstop_heartbeat_client() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("systemctl --user stop onionpress-heartbeat", fn)
        self.assertIn("sudo systemctl stop onionpress-heartbeat", fn)

    def test_the_page_handler_runs_the_launchers_stop_and_restart(self):
        self.assertIn("stop", handle_action.ACTIONS)
        self.assertIn("restart", handle_action.ACTIONS)


class TestSettingsPageStopHint(unittest.TestCase):

    def setUp(self):
        self.php = _read("app/Resources/plugins/onionpress-settings.php")

    def _linux_hint(self):
        i = self.php.index("Stop OnionPress")
        m = re.search(r"<\?php if \( \$current_platform === 'linux' \) : \?>\n(.*?)<\?php endif; \?>",
                      self.php[i:], re.S)
        self.assertIsNotNone(m, "no Linux-only hint after the Stop button")
        return m.group(1)

    def test_hint_names_onionpress_restart(self):
        hint = self._linux_hint()
        self.assertIn("<code>onionpress restart</code>", hint)

    def test_hint_no_longer_names_a_system_unit_start(self):
        self.assertNotIn("sudo systemctl start onionpress", self.php)
        self.assertNotIn("systemctl start onionpress<", self.php)
        hint = self._linux_hint()
        # `onionpress start` and the user unit's start are named only as
        # what does not work after the page's Stop.
        for cmd in ("<code>onionpress start</code>", "<code>systemctl --user start onionpress</code>"):
            self.assertIn(cmd, hint)
            self.assertLess(hint.index("<code>onionpress restart</code>"), hint.index(cmd))
        self.assertIn("do nothing", hint)


if __name__ == "__main__":
    unittest.main()
