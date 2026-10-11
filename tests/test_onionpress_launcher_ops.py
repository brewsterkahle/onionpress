"""Tests for src/onionpress/launcher_ops.py."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.launcher_ops import (
    WATCHDOG_REPUBLISH_REQUEST_PATH, get_running_wp_port,
    request_watchdog_republish,
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
    """Records docker.exec() calls; `ok` controls the result."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def exec(self, container, argv, timeout=30, **kw):
        self.calls.append((container, list(argv), timeout))
        return mock.Mock(ok=self.ok)


class TestRequestWatchdogRepublish(unittest.TestCase):
    """The host side of the issue #266 republish request."""

    def test_writes_the_request_file_inside_the_tor_container(self):
        docker = _FakeDocker()
        self.assertTrue(request_watchdog_republish(
            docker, "onionpress-tor", "unreachable 612s http=000:rc=97"))
        (container, argv, timeout), = docker.calls
        self.assertEqual(container, "onionpress-tor")
        self.assertEqual(argv[:2], ["sh", "-c"])
        # Reason and path travel as positional args, never interpolated
        # into the script — a reason can't become shell.
        self.assertEqual(argv[-2:], ["unreachable 612s http=000:rc=97",
                                     WATCHDOG_REPUBLISH_REQUEST_PATH])
        self.assertIn('"$2"', argv[2])
        self.assertIn('"$1"', argv[2])
        self.assertIn("mkdir -p", argv[2])
        self.assertLessEqual(timeout, 10)

    def test_reason_is_flattened_and_bounded(self):
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "a\nb\t" + "x" * 500)
        reason = docker.calls[0][1][-2]
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), 200)
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "")
        self.assertEqual(docker.calls[0][1][-2], "unspecified")

    def test_returns_false_when_docker_exec_fails(self):
        self.assertFalse(request_watchdog_republish(
            _FakeDocker(ok=False), "onionpress-tor", "x"))

    def test_the_shell_snippet_really_writes_the_file(self):
        """Run the exact argv through a real sh against a temp path."""
        import subprocess
        import tempfile
        docker = _FakeDocker()
        request_watchdog_republish(docker, "onionpress-tor", "why not")
        argv = docker.calls[0][1]
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "run", "tor-watchdog", "republish-requested")
            argv[-1] = target
            subprocess.run(argv, check=True, timeout=10)
            with open(target) as f:
                self.assertEqual(f.read(), "why not\n")

    def test_path_matches_the_watchdog(self):
        """launcher_ops and tor-watchdog.py must agree on the request path;
        the app writes it, the watchdog polls it."""
        with open(_WATCHDOG) as f:
            src = f.read()
        self.assertIn(
            f'REPUBLISH_REQUEST_PATH = "{WATCHDOG_REPUBLISH_REQUEST_PATH}"', src)


if __name__ == "__main__":
    unittest.main()
