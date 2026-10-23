#!/usr/bin/env python3
"""system_metrics (src/onionpress/system_metrics.py): the host and container
resource numbers in the reachability snapshot (menubar sleep/wake/12 h
timer, the Linux heartbeat's 12 h timer). Every collector is best effort:
a failure is a None, never an exception. subprocess, /proc and the OS
detection are faked here."""

import io
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import system_metrics as sm  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import OS  # noqa: E402


def _completed(stdout, rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class TestParsers(unittest.TestCase):
    def test_mem(self):
        self.assertIsNone(sm._parse_mem_to_mb(""))
        self.assertIsNone(sm._parse_mem_to_mb("12MB"))
        self.assertAlmostEqual(sm._parse_mem_to_mb("512KiB"), 0.5)
        self.assertEqual(sm._parse_mem_to_mb("123MiB"), 123.0)
        self.assertEqual(sm._parse_mem_to_mb("1.5GiB"), 1536.0)
        self.assertEqual(sm._parse_mem_to_mb("1TiB"), 1024 * 1024)

    def test_percent(self):
        self.assertEqual(sm._parse_percent("3.10%"), 3.1)
        self.assertEqual(sm._parse_percent("0%"), 0.0)
        self.assertIsNone(sm._parse_percent("--"))


class FakeDocker:
    def __init__(self, result, colima_home=None):
        self.result = result
        self.calls = []
        self.paths = mock.Mock(colima_home=colima_home)

    def run(self, args, timeout=30, quiet=False, **kwargs):
        self.calls.append(args)
        return self.result


class TestContainerMetrics(unittest.TestCase):
    def test_sums_docker_stats_on_linux(self):
        docker = FakeDocker(DockerResult(0, "100MiB / 2GiB\t1.5%\n1.5GiB / 2GiB\t2.5%\n\nbad line\n", ""))
        with mock.patch.object(sm, "detect_os", return_value=OS.LINUX):
            out = sm.container_metrics(docker)
        self.assertEqual(out, {"total_ram_mb": 1636.0, "total_cpu_percent": 4.0,
                               "onionpress_ram_mb": 1636.0})
        self.assertEqual(docker.calls[0][:2], ["stats", "--no-stream"])

    def test_a_failed_stats_call_is_all_none(self):
        docker = FakeDocker(DockerResult(1, "", "Cannot connect"))
        with mock.patch.object(sm, "detect_os", return_value=OS.LINUX):
            out = sm.container_metrics(docker)
        self.assertEqual(out, {"total_ram_mb": None, "total_cpu_percent": None,
                               "onionpress_ram_mb": None})

    def test_mac_footprint_is_the_host_side_rss(self):
        docker = FakeDocker(DockerResult(0, "100MiB / 2GiB\t1.0%\n", ""), colima_home="/c")
        with mock.patch.object(sm, "detect_os", return_value=OS.MACOS), \
                mock.patch.object(sm, "_macos_onionpress_rss_kb", return_value=2048) as rss:
            out = sm.container_metrics(docker)
        rss.assert_called_once_with("/c")
        self.assertEqual(out["onionpress_ram_mb"], 2.0)
        self.assertEqual(out["total_ram_mb"], 100.0)

    def test_an_exception_in_docker_is_swallowed(self):
        docker = mock.Mock()
        docker.run.side_effect = OSError("no docker")
        with mock.patch.object(sm, "detect_os", return_value=OS.LINUX):
            out = sm.container_metrics(docker)
        self.assertIsNone(out["total_ram_mb"])


class TestHostMetrics(unittest.TestCase):
    def test_linux(self):
        meminfo = "MemTotal:       16000000 kB\nMemFree:  1 kB\nMemAvailable:   4000000 kB\n"
        with mock.patch.object(sm, "detect_os", return_value=OS.LINUX), \
                mock.patch("builtins.open", return_value=io.StringIO(meminfo)), \
                mock.patch.object(os, "getloadavg", return_value=(1.25, 1.0, 1.0)), \
                mock.patch.object(os, "cpu_count", return_value=8):
            out = sm.host_metrics()
        self.assertEqual(out["load_1m"], 1.25)
        self.assertEqual(out["cpu_count"], 8)
        self.assertAlmostEqual(out["ram_total_gb"], 16000000 * 1024 / 1024 ** 3)
        self.assertAlmostEqual(out["ram_used_gb"], 12000000 * 1024 / 1024 ** 3)

    def test_linux_without_proc_is_none(self):
        with mock.patch.object(sm, "detect_os", return_value=OS.LINUX), \
                mock.patch("builtins.open", side_effect=FileNotFoundError):
            out = sm.host_metrics()
        self.assertIsNone(out["ram_total_gb"])
        self.assertIsNone(out["ram_used_gb"])

    def test_macos(self):
        vm_stat = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                   "Pages free:                   1000.\n"
                   "Pages active:                 2000.\n"
                   "Pages inactive:               9999.\n"
                   "Pages wired down:             3000.\n"
                   "Pages occupied by compressor: 1000.\n")

        def run(argv, **kw):
            if argv == ["sysctl", "-n", "hw.memsize"]:
                return _completed("17179869184\n")
            if argv == ["vm_stat"]:
                return _completed(vm_stat)
            raise AssertionError(argv)

        with mock.patch.object(sm, "detect_os", return_value=OS.MACOS), \
                mock.patch.object(subprocess, "run", side_effect=run), \
                mock.patch.object(os, "getloadavg", side_effect=OSError):
            out = sm.host_metrics()
        self.assertEqual(out["ram_total_gb"], 16.0)
        # active + wired + compressor, never the inactive cache pages
        self.assertAlmostEqual(out["ram_used_gb"], 6000 * 16384 / 1024 ** 3)
        self.assertIsNone(out["load_1m"])

    def test_macos_tool_failures_are_none(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("", rc=1)):
            self.assertIsNone(sm._macos_total_ram_bytes())
            self.assertIsNone(sm._macos_used_ram_bytes())
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(sm._macos_total_ram_bytes())
            self.assertIsNone(sm._macos_used_ram_bytes())


class TestMacRss(unittest.TestCase):
    def test_sums_the_bundle_processes_and_the_vm(self):
        ps_all = ("  1 /sbin/launchd\n"
                  " 20 /Applications/OnionPress.app/Contents/MacOS/OnionPress\n"
                  " 21 /Applications/OnionPress.app/Contents/MacOS/bin/colima start\n"
                  " 30 /Applications/Other.app/Contents/MacOS/Other\n"
                  "bad\n")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:3] == ["ps", "-A", "-o"]:
                return _completed(ps_all)
            if argv[:3] == ["lsof", "-t", "+D"]:
                return _completed("21\n40\n", rc=1)  # lsof's rc=1 with PIDs is normal
            if argv[:3] == ["ps", "-o", "rss="]:
                return _completed("1000\n2000\n3000\n")
            raise AssertionError(argv)

        with mock.patch.object(subprocess, "run", side_effect=run), \
                mock.patch.object(os.path, "isdir", return_value=True):
            self.assertEqual(sm._macos_onionpress_rss_kb("/c"), 6000)
        pids = set(calls[-1][-1].split(","))
        self.assertEqual(pids, {"20", "21", "40"})
        self.assertEqual(calls[1], ["lsof", "-t", "+D", os.path.join("/c", "_lima", "colima")])

    def test_no_processes_is_none(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("")):
            self.assertIsNone(sm._macos_onionpress_rss_kb(None))


if __name__ == "__main__":
    unittest.main()
