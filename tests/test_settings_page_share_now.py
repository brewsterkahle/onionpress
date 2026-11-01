#!/usr/bin/env python3
"""Share Now on Linux (issue #351).

The Settings page's Share Now touches `/var/lib/onionpress/.upload-analytics`
and answered "Upload queued" on both platforms, but only the Mac daemon
read the file. Now Linux's heartbeat client (linux/onionpress-heartbeat.py)
takes it every cycle, as www-data, and the host marker
`onionpress upload-analytics` leaves in the data directory, and wakes the
upload loop when sharing is on (with it off: logged, nothing sent). Both
launchers' `upload-analytics` is `analytics_sharing.request_upload`, which
refuses while sharing is off or nothing is running to take the request;
the page says "off" instead of "queued" when the host's config says so.
"""
import importlib.util
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

from onionpress import analytics_sharing, daemon, key_manager, onion_auth  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

HEARTBEAT_PATH = os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py")
SETTINGS_PHP = os.path.join(PROJECT_DIR, "app", "Resources", "plugins", "onionpress-settings.php")

# The daemon's HOME for the module: DATA_DIR comes from it at import, and
# a FileHandler on $DATA_DIR/onionpress.log is opened.
_HOME = tempfile.mkdtemp(prefix="op-share-now-")
_DATA_DIR = os.path.join(_HOME, ".onionpress")
os.makedirs(_DATA_DIR)


def _load_heartbeat():
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = _HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        spec = importlib.util.spec_from_file_location("onionpress_heartbeat_share_now", HEARTBEAT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved_home
    return mod


hb = _load_heartbeat()
REAL_DOCKER_EXEC = hb.docker_exec

VOLUME_PATH = analytics_sharing.UPLOAD_TRIGGER_VOLUME_PATH
VOLUME_EXEC = ("onionpress-wordpress",
               ["sh", "-c", f"test -f {VOLUME_PATH} && rm {VOLUME_PATH}"])


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel)) as fh:
        return fh.read()


class TestTriggerFilesAgree(unittest.TestCase):
    """One marker name and one volume path, in every copy."""

    def test_marker_and_path_are_the_same_everywhere(self):
        self.assertEqual(hb.UPLOAD_TRIGGER_MARKER, analytics_sharing.UPLOAD_TRIGGER_MARKER)
        self.assertEqual(daemon.UPLOAD_TRIGGER_MARKER, analytics_sharing.UPLOAD_TRIGGER_MARKER)
        self.assertEqual(hb.UPLOAD_TRIGGER_VOLUME_PATH, VOLUME_PATH)
        self.assertEqual(VOLUME_PATH, "/var/lib/onionpress/" + analytics_sharing.UPLOAD_TRIGGER_MARKER)
        php = _read("app/Resources/plugins/onionpress-settings.php")
        self.assertIn(f"$trigger = '{VOLUME_PATH}';", php)
        mac = _read("src/onionpress/daemon.py")
        self.assertIn(f'"test -f {VOLUME_PATH} && rm {VOLUME_PATH}"', mac)
        self.assertEqual(hb.WP_USER, "www-data")


class TestHeartbeatTakesTheTrigger(unittest.TestCase):

    def setUp(self):
        self.marker = os.path.join(hb.DATA_DIR, hb.UPLOAD_TRIGGER_MARKER)
        self.execs = []
        self.exec_answer = (False, "")
        self.triggers = []
        self.logged = []

        def fake_exec(container, args, timeout=30, input=None, user=None):
            self.execs.append((container, args, timeout, user))
            return self.exec_answer

        stack = [
            mock.patch.object(hb, "docker_exec", fake_exec),
            mock.patch.object(hb, "_analytics_host", object()),
            mock.patch.object(analytics_sharing, "trigger_upload",
                              lambda: self.triggers.append(1)),
            mock.patch.object(hb.log, "info", lambda msg, *a: self.logged.append(msg % a)),
            mock.patch.object(hb.log, "warning", lambda msg, *a: self.logged.append(msg % a)),
        ]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._remove_marker)

    def _remove_marker(self):
        if os.path.isdir(self.marker):
            os.rmdir(self.marker)
        elif os.path.lexists(self.marker):
            os.remove(self.marker)

    def _touch(self):
        with open(self.marker, "w"):
            pass

    YES = {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes"}

    def test_nothing_waiting_asks_the_container_once_and_does_nothing(self):
        self.assertFalse(hb._poll_upload_trigger(self.YES))
        self.assertEqual(self.execs, [(*VOLUME_EXEC, 5, "www-data")])
        self.assertEqual(self.triggers, [])
        self.assertEqual(self.logged, [])

    def test_the_host_marker_is_taken_once_and_wakes_the_loop(self):
        self._touch()
        self.assertTrue(hb._poll_upload_trigger(self.YES))
        self.assertFalse(os.path.lexists(self.marker), "the marker must be removed")
        self.assertEqual(self.triggers, [1])
        self.assertTrue(any("uploading" in m for m in self.logged), self.logged)
        # Taken: the next cycle finds nothing.
        self.assertFalse(hb._poll_upload_trigger(self.YES))
        self.assertEqual(self.triggers, [1])

    def test_the_pages_file_is_taken_as_www_data_and_wakes_the_loop(self):
        self.exec_answer = (True, "")
        self.assertTrue(hb._poll_upload_trigger(self.YES))
        self.assertEqual(self.execs[-1][3], "www-data")
        self.assertEqual(self.triggers, [1])

    def test_sharing_off_takes_the_request_logs_and_sends_nothing(self):
        for config in ({}, {"SHARE_ANALYTICS_WITH_ONIONHOME": "no"},
                       {"SHARE_ANALYTICS_WITH_ONIONHOME": "sometimes"}):
            with self.subTest(config=config):
                self.logged.clear()
                self._touch()
                self.assertFalse(hb._poll_upload_trigger(config))
                self.assertFalse(os.path.lexists(self.marker), "taken even when off")
                self.assertEqual(self.triggers, [])
                self.assertTrue(any("ignored" in m and "SHARE_ANALYTICS_WITH_ONIONHOME" in m
                                    for m in self.logged), self.logged)

    def test_without_the_upload_loop_nothing_is_woken(self):
        with mock.patch.object(hb, "_analytics_host", None):
            self._touch()
            self.assertFalse(hb._poll_upload_trigger(self.YES))
        self.assertEqual(self.triggers, [])
        self.assertTrue(any("not" in m and "running" in m for m in self.logged), self.logged)

    def test_a_marker_that_cannot_be_removed_does_not_count(self):
        # A folder at the marker's name: os.remove fails, and counting it
        # would upload every cycle for as long as it stays.
        os.mkdir(self.marker)
        self.assertFalse(hb._poll_upload_trigger(self.YES))
        self.assertEqual(self.triggers, [])
        self.assertTrue(any("could not remove" in m for m in self.logged), self.logged)

    def test_docker_exec_runs_as_the_user_given(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(hb.subprocess, "run", run):
            REAL_DOCKER_EXEC("c", ["true"], user="www-data")
            REAL_DOCKER_EXEC("c", ["true"])
        self.assertEqual(calls, [["docker", "exec", "-u", "www-data", "c", "true"],
                                 ["docker", "exec", "c", "true"]])


class TestHeartbeatLoopsPoll(unittest.TestCase):
    """Both of main()'s loops (the hub's and the client's) poll the trigger
    right after the opt-in, with the cycle's config."""

    def test_both_loops_poll_after_the_opt_in(self):
        src = _read("linux/onionpress-heartbeat.py")
        main_body = src[src.index("\ndef main():"):src.index('\nif __name__ == "__main__":')]
        polls = [m.start() for m in re.finditer(r"_poll_upload_trigger\(config\)", main_body)]
        opt_ins = [m.start() for m in re.finditer(
            r"analytics_opt_in = _poll_analytics_opt_in\(analytics_opt_in, config\)", main_body)]
        self.assertEqual(len(polls), 2, "one poll per loop")
        self.assertEqual(len(opt_ins), 2)
        for opt_in, poll in zip(opt_ins, polls):
            self.assertLess(opt_in, poll)
            self.assertLess(poll - opt_in, 200, "the poll follows the opt-in directly")
        hub_loop = main_body[main_body.index("This node IS the OnionHeaven hub"):]
        self.assertLess(hub_loop.index("config = read_config()"),
                        hub_loop.index("_poll_upload_trigger(config)"))


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

    def test_linux_daemon_running_is_the_heartbeat_unit(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0 if argv == active else 3)
        active = ["systemctl", "--user", "is-active", "onionpress-heartbeat.service"]
        with mock.patch.object(analytics_sharing.subprocess, "run", run):
            self.assertTrue(analytics_sharing.daemon_running(self.dir, "linux"))
            self.assertEqual(calls, [active])
            active = ["systemctl", "is-active", "onionpress-heartbeat.service"]
            calls.clear()
            self.assertTrue(analytics_sharing.daemon_running(self.dir, "linux"))
            self.assertEqual(len(calls), 2, "the old install's system unit is asked too")
            active = None
            self.assertFalse(analytics_sharing.daemon_running(self.dir, "linux"))
        with mock.patch.object(analytics_sharing.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(analytics_sharing.daemon_running(self.dir, "linux"))


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
        usage = re.search(r'echo "Usage: \$0 \{(.*?)\}"', src).group(1)
        self.assertIn("upload-analytics", usage.split("|"))

    def test_mac_launcher_delegates(self):
        src = _read("app/MacOS/onionpress")
        body = self._case(src, "upload-analytics")
        self.assertIn('bundled_python -m onionpress.cli --data-dir "$DATA_DIR" upload-analytics', body)
        self.assertNotIn("touch", body)
        self.assertIn("upload-analytics", src[src.index("Advanced / internal"):])


class TestSettingsPageSaysOff(unittest.TestCase):

    def setUp(self):
        self.php = _read("app/Resources/plugins/onionpress-settings.php")
        start = self.php.index("register_rest_route( 'onionpress/v1', '/upload-analytics'")
        self.callback = self.php[start:self.php.index("\n} );", start)]

    def test_off_is_said_before_the_file_is_touched(self):
        off = self.callback.index("Log sharing is off")
        self.assertLess(self.callback.index("config-current.json"), off)
        self.assertLess(off, self.callback.index("touch( $trigger )"))
        self.assertIn("SHARE_ANALYTICS_WITH_ONIONHOME", self.callback)
        self.assertIn("=== 'yes' ? 'yes' : 'no'", self.callback)
        self.assertIn("if ( $sharing === 'no' )", self.callback)
        self.assertIn("Upload queued", self.callback)

    def test_php_syntax(self):
        if shutil.which("php") is None:
            self.skipTest("php is not installed")
        r = subprocess.run(["php", "-l", SETTINGS_PHP], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
