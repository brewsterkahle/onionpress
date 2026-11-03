#!/usr/bin/env python3
"""Share Now (issue #351, #407).

Since issue #528 the Settings page's Share Now is a handle-action
request (`upload-analytics`), handled by `analytics_sharing.request_upload`
as `onionpress upload-analytics` is: it refuses while sharing is off or
no daemon is running to take the request (the pid file, on both
platforms), else leaves the data dir's marker the daemon takes within a
cycle (tests/test_mac_daemon.py drives the take). Nothing is touched in
the volume, and the daemon runs no `docker exec` per cycle to look.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import analytics_sharing, daemon  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

SETTINGS_PHP = os.path.join(PROJECT_DIR, "app", "Resources", "plugins", "onionpress-settings.php")


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel)) as fh:
        return fh.read()


class TestOneTrigger(unittest.TestCase):
    """One marker, in the data dir; none in the volume."""

    def test_the_marker_is_the_data_dirs_only(self):
        self.assertEqual(daemon.UPLOAD_TRIGGER_MARKER, analytics_sharing.UPLOAD_TRIGGER_MARKER)
        self.assertFalse(hasattr(analytics_sharing, "UPLOAD_TRIGGER_VOLUME_PATH"))
        src = _read("src/onionpress/daemon.py")
        self.assertIn("if self._take_marker(UPLOAD_TRIGGER_MARKER):", src)
        self.assertNotIn("/var/lib/onionpress/.upload-analytics", src)
        self.assertNotIn(".upload-analytics", _read("app/Resources/plugins/onionpress-settings.php"))


class TestRequestUpload(unittest.TestCase):
    """analytics_sharing.request_upload: `onionpress upload-analytics`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-request-upload-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = resolve_paths(data_dir=self.dir)
        self.marker = os.path.join(self.dir, analytics_sharing.UPLOAD_TRIGGER_MARKER)

    def _config(self, text):
        with open(self.paths.config_file, "w") as fh:
            fh.write(text)

    def _run(self, os_name):
        out = io.StringIO()
        rc = analytics_sharing.request_upload(self.paths, os_name, out=out)
        return rc, out.getvalue()

    def test_sharing_off_refuses_before_anything_else(self):
        for text in ("", "SHARE_ANALYTICS_WITH_ONIONHOME=no\n", "SHARE_ANALYTICS_WITH_ONIONHOME=maybe\n"):
            with self.subTest(config=text):
                self._config(text)
                with mock.patch.object(analytics_sharing, "daemon_running") as running:
                    rc, out = self._run("linux")
                self.assertEqual(rc, 1)
                self.assertEqual(out.strip(), analytics_sharing.SHARING_OFF_MSG)
                self.assertIn("onionpress config set SHARE_ANALYTICS_WITH_ONIONHOME yes", out)
                running.assert_not_called()
                self.assertFalse(os.path.exists(self.marker))

    def test_nothing_running_refuses_without_a_marker(self):
        self._config("SHARE_ANALYTICS_WITH_ONIONHOME=yes\n")
        with mock.patch.object(analytics_sharing, "daemon_running", return_value=False):
            rc, out = self._run("linux")
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), analytics_sharing.NOT_RUNNING_MSG)
        self.assertFalse(os.path.exists(self.marker))

    def test_on_leaves_the_marker_the_daemons_take(self):
        self._config("SHARE_ANALYTICS_WITH_ONIONHOME=Yes \n")
        with mock.patch.object(analytics_sharing, "daemon_running", return_value=True) as running:
            rc, out = self._run("macos")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), analytics_sharing.REQUESTED_MSG)
        self.assertTrue(os.path.isfile(self.marker))
        running.assert_called_once_with(self.dir, "macos")
        # The Mac daemon takes exactly this file.
        self.assertEqual(os.path.basename(self.marker), daemon.UPLOAD_TRIGGER_MARKER)

    def test_mac_daemon_running_is_a_live_daemon_pid(self):
        pid_file = os.path.join(self.dir, daemon.PID_FILENAME)
        self.assertFalse(analytics_sharing.daemon_running(self.dir, "macos"))
        with open(pid_file, "w") as fh:
            fh.write("not a pid\n")
        self.assertFalse(analytics_sharing.daemon_running(self.dir, "macos"))
        with open(pid_file, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        self.assertTrue(analytics_sharing.daemon_running(self.dir, "macos"))
        with mock.patch.object(analytics_sharing.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(analytics_sharing.daemon_running(self.dir, "macos"))

    def test_linux_daemon_running_is_the_pid_file_too(self):
        # One rule on both platforms (#407): the daemon's pid file names a
        # live process; nothing asks systemd.
        pid_file = os.path.join(self.dir, daemon.PID_FILENAME)
        with mock.patch.object(analytics_sharing.subprocess, "run",
                               side_effect=AssertionError("systemctl asked")):
            self.assertFalse(analytics_sharing.daemon_running(self.dir, "linux"))
            with open(pid_file, "w") as fh:
                fh.write(f"{os.getpid()}\n")
            self.assertTrue(analytics_sharing.daemon_running(self.dir, "linux"))


class TestLaunchersDelegate(unittest.TestCase):

    @staticmethod
    def _case(src, name):
        main = src[src.index("\nmain() {"):]
        m = re.search(rf"\n        {re.escape(name)}\)\n(.*?)\n            ;;", main, re.S)
        if m is None:
            raise AssertionError(f"no {name}) case")
        return m.group(1)

    def test_linux_launcher_delegates_and_lists_the_command(self):
        src = _read("linux/onionpress")
        body = self._case(src, "upload-analytics")
        self.assertIn('op_py --data-dir "$DATA_DIR" upload-analytics', body)
        self.assertNotIn("touch", body)
        from onionpress import cli
        self.assertIn("upload-analytics", cli.launcher_help("linux")[cli.launcher_help("linux").index("Advanced / internal"):])

    def test_mac_launcher_delegates(self):
        src = _read("app/MacOS/onionpress")
        body = self._case(src, "upload-analytics")
        self.assertIn('bundled_python -m onionpress.cli --data-dir "$DATA_DIR" upload-analytics', body)
        self.assertNotIn("touch", body)
        from onionpress import cli
        self.assertIn("upload-analytics", cli.launcher_help("macos")[cli.launcher_help("macos").index("Advanced / internal"):])


class TestSettingsPageAsksTheHost(unittest.TestCase):

    def setUp(self):
        self.php = _read("app/Resources/plugins/onionpress-settings.php")

    def test_the_button_posts_the_action(self):
        self.assertNotIn("register_rest_route( 'onionpress/v1', '/upload-analytics'", self.php)
        self.assertIn("['onionpress_action', 'upload-analytics']", self.php)
        self.assertIn("'upload-analytics' => 'upload-analytics'", self.php)

    def test_php_syntax(self):
        if shutil.which("php") is None:
            self.skipTest("php is not installed")
        r = subprocess.run(["php", "-l", SETTINGS_PHP], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
