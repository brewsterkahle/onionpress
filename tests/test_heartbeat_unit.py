#!/usr/bin/env python3
"""The Linux heartbeat client unit around a restore (onionpress.heartbeat_unit,
issue #406: the one platform seam of the shared backup and restore).

The unit loads the onion service key at start and sends it with every
/online: before a restore replaces that key it is stopped (or its next
beat would put the old key back on the hub after the /unregister), and
started again after, with the key Tor serves now. cmd_restore runs inside
heartbeat_unit.paused; the Settings page's restore stops it in
before_key_change and starts it again whatever happens. On macOS nothing
is run: the daemon re-reads the key.

The launcher's own cases (stop, restart, reset, key import) keep bash
stop_heartbeat_client / start_heartbeat_client; a test here holds the
Python to the same steps.
"""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import heartbeat_unit  # noqa: E402

UNIT = "onionpress-heartbeat.service"


class _Systemctl:
    """A `run` standing in for systemctl: *active* and *enabled* name the
    scopes ("user", "system") whose unit is so; *masked* is the user
    unit's hold. Records every argv."""

    def __init__(self, active=(), enabled=(), masked=False, fail=()):
        self.active, self.enabled, self.masked, self.fail = set(active), set(enabled), masked, set(fail)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        scope = "user" if "--user" in argv else "system"
        verb = argv[-2]
        out, rc = "", 1
        if verb == "is-active":
            rc = 0 if scope in self.active else 3
        elif verb == "is-enabled":
            if scope == "user" and self.masked:
                out, rc = "masked-runtime\n", 1
            else:
                rc = 0 if scope in self.enabled else 1
        elif verb in ("stop", "start"):
            rc = 1 if verb in self.fail else 0
        return subprocess.CompletedProcess(argv, rc, out, "")


class TestStopAndStart(unittest.TestCase):

    def setUp(self):
        self.logs = []

    def test_stop_stops_the_user_unit_where_active(self):
        run = _Systemctl(active=("user",))
        heartbeat_unit.stop(self.logs.append, run)
        self.assertIn(["systemctl", "--user", "stop", UNIT], run.calls)
        self.assertNotIn(["sudo", "systemctl", "stop", UNIT], run.calls)
        self.assertEqual(self.logs, ["Stopping heartbeat client..."])

    def test_stop_stops_an_old_installs_system_unit_through_sudo(self):
        run = _Systemctl(active=("system",))
        heartbeat_unit.stop(self.logs.append, run)
        self.assertIn(["sudo", "systemctl", "stop", UNIT], run.calls)
        self.assertEqual(self.logs, ["Stopping heartbeat client (system unit)..."])

    def test_nothing_active_stops_nothing(self):
        run = _Systemctl()
        heartbeat_unit.stop(self.logs.append, run)
        self.assertEqual([c for c in run.calls if "stop" in c], [])
        self.assertEqual(self.logs, [])

    def test_start_starts_the_enabled_unit(self):
        for enabled, want in ((("user",), ["systemctl", "--user", "start", UNIT]),
                              (("system",), ["sudo", "systemctl", "start", UNIT])):
            with self.subTest(enabled=enabled):
                run = _Systemctl(enabled=enabled)
                heartbeat_unit.start(self.logs.append, run)
                self.assertIn(want, run.calls)
        run = _Systemctl()
        heartbeat_unit.start(self.logs.append, run)
        self.assertEqual([c for c in run.calls if "start" in c], [])

    def test_start_respects_the_install_hold(self):
        # Issue #464: masked until WordPress is installed, nothing registers
        # an address with the hub before the site exists.
        run = _Systemctl(enabled=("user",), masked=True)
        heartbeat_unit.start(self.logs.append, run)
        self.assertEqual([c for c in run.calls if "start" in c], [])
        self.assertEqual(self.logs, ["Heartbeat client stays held: WordPress is not installed yet"])
        self.assertTrue(heartbeat_unit.held(run))
        self.assertFalse(heartbeat_unit.held(_Systemctl(enabled=("user",))))

    def test_failures_are_logged_never_raised(self):
        run = _Systemctl(active=("user",), enabled=("user",), fail=("stop", "start"))
        heartbeat_unit.stop(self.logs.append, run)
        heartbeat_unit.start(self.logs.append, run)
        self.assertIn("WARNING: Failed to start heartbeat client", self.logs)

        def no_systemctl(argv, **kw):
            raise FileNotFoundError("systemctl")
        heartbeat_unit.stop(self.logs.append, no_systemctl)
        heartbeat_unit.start(self.logs.append, no_systemctl)
        self.assertFalse(heartbeat_unit.held(no_systemctl))


class TestPaused(unittest.TestCase):

    def test_linux_stops_before_and_starts_after_even_on_failure(self):
        run = _Systemctl(active=("user",), enabled=("user",))
        logs = []
        with self.assertRaises(RuntimeError):
            with heartbeat_unit.paused("linux", logs.append, run):
                self.assertEqual([c[-2] for c in run.calls if c[-2] in ("stop", "start")], ["stop"])
                raise RuntimeError("the restore failed")
        self.assertEqual([c[-2] for c in run.calls if c[-2] in ("stop", "start")], ["stop", "start"])

    def test_macos_runs_nothing(self):
        run = _Systemctl(active=("user",), enabled=("user",))
        with heartbeat_unit.paused("macos", lambda m: None, run):
            pass
        self.assertEqual(run.calls, [])


class TestTheRestoresUseIt(unittest.TestCase):

    def test_cmd_restore_runs_inside_paused(self):
        from onionpress import cli
        seen = []

        class Paused:
            def __init__(self, os_name, log, run=None):
                seen.append(("enter", os_name))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                seen.append("exit")
                return False
        obj = cli.OnionPressCLI.__new__(cli.OnionPressCLI)
        obj.log = lambda m: None
        with mock.patch.object(cli, "detect_os", return_value=cli.OS.LINUX), \
                mock.patch("onionpress.heartbeat_unit.paused", Paused), \
                mock.patch.object(cli.OnionPressCLI, "_restore",
                                  side_effect=lambda *a, **k: seen.append("restore") or 7) as inner:
            self.assertEqual(obj.cmd_restore("pw", "/b.zip", "", confirmed=frozenset({"x"}),
                                             json_output=True), 7)
        self.assertEqual(seen, [("enter", "linux"), "restore", "exit"])
        inner.assert_called_once_with("pw", "/b.zip", "", confirmed=frozenset({"x"}), json_output=True)

    def test_the_bash_cases_keep_the_same_steps(self):
        """The launcher's own stop/start_heartbeat_client: the same unit,
        the same scopes, the same hold."""
        with open(os.path.join(PROJECT_ROOT, "linux", "onionpress"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(re.search(r"\nHEARTBEAT_UNIT=(\S+)", text).group(1), heartbeat_unit.UNIT)
        stop = text[text.index("\nstop_heartbeat_client() {"):]
        stop = stop[:stop.index("\n}\n")]
        self.assertIn("systemctl --user stop onionpress-heartbeat", stop)
        self.assertIn("sudo systemctl stop onionpress-heartbeat", stop)
        start = text[text.index("\nstart_heartbeat_client() {"):]
        start = start[:start.index("\n}\n")]
        self.assertIn("heartbeat_client_held", start)
        self.assertIn("systemctl --user start onionpress-heartbeat", start)
        self.assertIn("sudo systemctl start onionpress-heartbeat", start)
        # And the restore cases no longer call them: the CLI does.
        for case in ("restore", "settings-page-restore", "settings-page-backup", "backup"):
            start_i = text.index(f"\n        {case})\n")
            body = text[start_i:text.index("\n            ;;\n", start_i)]
            self.assertNotIn("heartbeat_client", body, case)


if __name__ == "__main__":
    unittest.main()
