"""Tests for src/onionpress/launcher_ops.py."""

import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.launcher_ops import (
    REPUBLISH_FAILED, REPUBLISH_NO_WATCHDOG, REPUBLISH_REQUESTED,
    WATCHDOG_ALIVE_MAX_AGE, WATCHDOG_ALIVE_PATH,
    WATCHDOG_REPUBLISH_REQUEST_PATH, describe_republish_request,
    get_running_wp_port, request_watchdog_republish,
)

_REPO = os.path.join(os.path.dirname(__file__), "..")
_WATCHDOG = os.path.join(_REPO, "app", "Resources", "docker", "tor", "tor-watchdog.py")


class TestGetRunningWpPort(unittest.TestCase):
    """Reads `docker port <container> 80` and returns the published host
    port, or None when there isn't one to trust."""

    def _run(self, stdout, returncode=0):
        return mock.patch(
            "onionpress.launcher_ops.subprocess.run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=""),
        )

    def test_parses_the_published_port(self):
        with self._run("0.0.0.0:18080\n"):
            self.assertEqual(get_running_wp_port(), 18080)

    def test_default_offset_port(self):
        with self._run("0.0.0.0:8080\n"):
            self.assertEqual(get_running_wp_port(), 8080)

    def test_a_port_below_our_allocation_range_is_not_ours(self):
        """Offsets are 8080 + k*10000, so nothing we allocate is ever below
        8080 — a published port under it belongs to some other mapping
        entirely and must not be read back as "our" WordPress port."""
        with self._run("0.0.0.0:80\n"):
            self.assertIsNone(get_running_wp_port())

    def test_container_not_running(self):
        with self._run("", returncode=1):
            self.assertIsNone(get_running_wp_port())


class _FakeDocker:
    """Records docker.exec() calls; `ok`/`returncode` control the result."""

    def __init__(self, ok=True, returncode=None, timed_out=False):
        self.ok = ok
        self.returncode = (0 if ok else 1) if returncode is None else returncode
        self.timed_out = timed_out
        self.calls = []

    def exec(self, container, argv, timeout=30, **kw):
        self.calls.append((container, list(argv), timeout))
        return mock.Mock(ok=self.ok, returncode=self.returncode,
                         timed_out=self.timed_out)


class TestRequestWatchdogRepublish(unittest.TestCase):
    """The host side of the issue #266 republish request."""

    def test_writes_the_request_file_inside_the_tor_container(self):
        docker = _FakeDocker()
        self.assertEqual(request_watchdog_republish(
            docker, "onionpress-tor", "unreachable 612s http=000:rc=97"),
            REPUBLISH_REQUESTED)
        (container, argv, timeout), = docker.calls
        self.assertEqual(container, "onionpress-tor")
        self.assertEqual(argv[:2], ["sh", "-c"])
        # Reason and paths travel as positional args, never interpolated
        # into the script — a reason can't become shell.
        self.assertEqual(argv[4:], ["unreachable 612s http=000:rc=97",
                                    WATCHDOG_REPUBLISH_REQUEST_PATH,
                                    WATCHDOG_ALIVE_PATH,
                                    str(WATCHDOG_ALIVE_MAX_AGE)])
        self.assertIn('"$2"', argv[2])
        self.assertIn('"$1"', argv[2])
        self.assertIn("mkdir -p", argv[2])
        self.assertLessEqual(timeout, 10)

    def test_reason_is_flattened_and_bounded(self):
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "a\nb\t" + "x" * 500)
        reason = docker.calls[0][1][4]
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), 200)
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "")
        self.assertEqual(docker.calls[0][1][4], "unspecified")

    def test_reports_failure_when_docker_exec_fails(self):
        self.assertEqual(request_watchdog_republish(
            _FakeDocker(ok=False), "onionpress-tor", "x"), REPUBLISH_FAILED)
        # A timed-out exec is a failure whatever its code says.
        self.assertEqual(request_watchdog_republish(
            _FakeDocker(ok=False, returncode=3, timed_out=True),
            "onionpress-tor", "x"), REPUBLISH_FAILED)

    def test_reports_a_missing_watchdog(self):
        """Issue #313: the file is written but nothing is reading it."""
        self.assertEqual(request_watchdog_republish(
            _FakeDocker(ok=False, returncode=3), "onionpress-tor", "x"),
            REPUBLISH_NO_WATCHDOG)

    def test_every_result_has_its_own_log_text(self):
        texts = {describe_republish_request(s) for s in
                 (REPUBLISH_REQUESTED, REPUBLISH_NO_WATCHDOG, REPUBLISH_FAILED)}
        self.assertEqual(len(texts), 3)
        self.assertIn("not running", describe_republish_request(
            REPUBLISH_NO_WATCHDOG))

    # -- the exact script, run through a real sh ---------------------------

    def _run_script(self, tmp, watchdog_running=True, alive_age=None):
        """Run the request argv against temp paths with a fake pgrep.

        Returns (exit status, request file contents or None, pgrep's argv).
        """
        import subprocess
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "why not")
        argv = docker.calls[0][1]
        target = os.path.join(tmp, "run", "tor-watchdog", "republish-requested")
        alive = os.path.join(tmp, "run", "tor-watchdog", "alive")
        argv[5], argv[6] = target, alive
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        pgrep_log = os.path.join(tmp, "pgrep-args")
        with open(os.path.join(bindir, "pgrep"), "w") as f:
            f.write("#!/bin/sh\n"
                    f'printf "%s\\n" "$@" > "{pgrep_log}"\n'
                    f"exit {0 if watchdog_running else 1}\n")
        os.chmod(os.path.join(bindir, "pgrep"), 0o755)
        if alive_age is not None:
            os.makedirs(os.path.dirname(alive), exist_ok=True)
            open(alive, "w").close()
            then = time.time() - alive_age
            os.utime(alive, (then, then))
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        rc = subprocess.run(argv, env=env, timeout=10).returncode
        content = None
        if os.path.exists(target):
            with open(target) as f:
                content = f.read()
        with open(pgrep_log) as f:
            pgrep_args = f.read().split("\n")
        return rc, content, pgrep_args

    def test_the_shell_snippet_really_writes_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, content, pgrep_args = self._run_script(tmp, alive_age=5)
        self.assertEqual(rc, 0)
        self.assertEqual(content, "why not\n")
        # The pattern must not match this sh, whose argv holds the request
        # path (".../tor-watchdog/..."): it names the interpreter and script.
        self.assertEqual(pgrep_args[:2], ["-f", "python3 /[t]or-watchdog.py"])

    def test_no_watchdog_process_still_leaves_the_request(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, content, _ = self._run_script(tmp, watchdog_running=False)
        self.assertEqual(rc, 3)
        self.assertEqual(content, "why not\n")

    @unittest.skipUnless(
        subprocess.run(["stat", "-c", "%Y", "/"], capture_output=True).returncode == 0,
        "needs GNU stat, as in the tor image")
    def test_stale_alive_file_means_a_hung_watchdog(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, content, _ = self._run_script(
                tmp, alive_age=WATCHDOG_ALIVE_MAX_AGE + 60)
        self.assertEqual(rc, 3)
        self.assertEqual(content, "why not\n")

    def test_image_without_alive_file_trusts_the_process(self):
        """The app may ship before the image: no alive file, a running
        watchdog process, is a request that will be read."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, _ = self._run_script(tmp, alive_age=None)
        self.assertEqual(rc, 0)

    def test_path_matches_the_watchdog(self):
        """launcher_ops and tor-watchdog.py must agree on the request path;
        the app writes it, the watchdog polls it."""
        with open(_WATCHDOG) as f:
            src = f.read()
        self.assertIn(
            f'REPUBLISH_REQUEST_PATH = "{WATCHDOG_REPUBLISH_REQUEST_PATH}"', src)

    def test_alive_path_matches_the_watchdog(self):
        with open(_WATCHDOG) as f:
            src = f.read()
        self.assertIn(f'ALIVE_PATH = "{WATCHDOG_ALIVE_PATH}"', src)


if __name__ == "__main__":
    unittest.main()
