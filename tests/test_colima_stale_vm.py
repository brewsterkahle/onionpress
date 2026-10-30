#!/usr/bin/env python3
"""onionpress.colima.stop_stale_colima: an orphaned VM (a crash or a
force-quit left it running) is stopped so the next start gets offset 0's
ports, and nothing else ever is.

What is held: a pid file naming a live process means the VM is someone's
(left alone, colima never asked); a stale or unreadable pid file, or none,
asks `colima list --json` with this install's COLIMA_HOME/LIMA_HOME (never
~/.colima), and only a VM reported Running is stopped; a colima that is
missing, fails or hangs is no stale VM; the progress lines go to the
caller's log, else to the "onionpress" logger.

subprocess.run is patched: no colima runs.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import colima  # noqa: E402
from onionpress import config as op_config  # noqa: E402


class _Run:
    """subprocess.run stand-in: answers `colima list` with *listing*,
    records every call; `stop` may raise."""

    def __init__(self, listing=(0, '{"name":"onionpress","status":"Running"}'),
                 list_raises=None, stop_raises=None):
        self.listing = listing
        self.list_raises = list_raises
        self.stop_raises = stop_raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[1:] == ["list", "--json"]:
            if self.list_raises:
                raise self.list_raises
            rc, out = self.listing
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if argv[1:] == ["stop"]:
            if self.stop_raises:
                raise self.stop_raises
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected command {argv}")

    def verbs(self):
        return [c[0][1] for c in self.calls]


class TestStopStaleColima(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-stale-colima-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "colima")
        self.pid_file = os.path.join(self.dir, "daemon.pid")
        self.lines = []

    def call(self, run, log=True):
        with mock.patch.object(colima.subprocess, "run", run):
            colima.stop_stale_colima("/b/colima", self.home, self.pid_file,
                                     log_func=self.lines.append if log else None)

    def write_pid(self, value):
        with open(self.pid_file, "w") as f:
            f.write(value)

    def test_a_live_owner_keeps_its_vm(self):
        self.write_pid(str(os.getpid()))
        run = _Run()
        self.call(run)
        self.assertEqual(run.calls, [])
        self.assertEqual(self.lines, [])

    def test_a_dead_owner_s_running_vm_is_stopped(self):
        # a pid no process has (the highest pid_t is far below this)
        self.write_pid("999999999")
        run = _Run()
        self.call(run)
        self.assertEqual(run.verbs(), ["list", "stop"])
        env = run.calls[0][1]["env"]
        self.assertEqual(env["COLIMA_HOME"], self.home)
        self.assertEqual(env["LIMA_HOME"], os.path.join(self.home, "_lima"))
        self.assertEqual(env["LIMA_INSTANCE"], "onionpress")
        self.assertIs(run.calls[1][1]["env"], env)
        self.assertEqual(run.calls[1][1]["timeout"], 60)
        self.assertTrue(any("orphaned Colima VM" in l for l in self.lines))
        self.assertTrue(any("stopped successfully" in l for l in self.lines))

    def test_no_pid_file_and_a_running_vm_is_stopped(self):
        run = _Run()
        self.call(run)
        self.assertEqual(run.verbs(), ["list", "stop"])

    def test_an_unreadable_pid_file_counts_as_stale(self):
        self.write_pid("not a pid\n")
        run = _Run()
        self.call(run)
        self.assertEqual(run.verbs(), ["list", "stop"])

    def test_a_stopped_vm_is_left_alone(self):
        run = _Run(listing=(0, '{"name":"onionpress","status":"Stopped"}'))
        self.call(run)
        self.assertEqual(run.verbs(), ["list"])
        self.assertEqual(self.lines, [])

    def test_a_failing_list_is_no_stale_vm(self):
        run = _Run(listing=(1, "Running"))
        self.call(run)
        self.assertEqual(run.verbs(), ["list"])

    def test_a_missing_or_hung_colima_is_no_stale_vm(self):
        for exc in (FileNotFoundError("colima"),
                    subprocess.TimeoutExpired(["colima"], 10),
                    PermissionError("denied")):
            with self.subTest(exc=type(exc).__name__):
                run = _Run(list_raises=exc)
                self.call(run)
                self.assertEqual(run.verbs(), ["list"])

    def test_a_stop_that_times_out_is_logged_not_raised(self):
        run = _Run(stop_raises=subprocess.TimeoutExpired(["colima", "stop"], 60))
        self.call(run)
        self.assertTrue(any("Failed to stop orphaned Colima VM" in l for l in self.lines))

    def test_without_a_log_func_the_lines_go_to_the_logger(self):
        run = _Run()
        with self.assertLogs("onionpress", level="WARNING") as cm:
            self.call(run, log=False)
        self.assertTrue(any("orphaned Colima VM" in m for m in cm.output))

    def test_config_re_exports_it(self):
        self.assertIs(op_config.stop_stale_colima, colima.stop_stale_colima)


if __name__ == "__main__":
    unittest.main()
