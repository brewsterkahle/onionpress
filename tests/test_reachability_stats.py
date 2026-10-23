#!/usr/bin/env python3
"""ReachabilityStats (src/onionpress/reachability_stats.py): the menubar's
in-memory probe counters and the one-line snapshot it logs at sleep, wake
and every 12 h (issue #238). Pure functions of the counters and the
clock; nothing here touches Docker or the host."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import reachability_stats as rs  # noqa: E402
from onionpress.reachability_stats import ReachabilityStats  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_codes(self):
        cases = {
            "": "unknown",
            "000:rc=28": "timeout",
            "000:rc=28:retry": "timeout",
            "000:rc=7": "curl_7",
            "000": "no_response",
            "takeover": "takeover",
            "302": "redirector",
            "degraded:hub-down": "degraded",
            "503": "http_503",
            "weird": "weird",
        }
        for code, mode in cases.items():
            with self.subTest(code=code):
                self.assertEqual(ReachabilityStats._classify(code), mode)


class TestCounters(unittest.TestCase):
    def test_probes_are_counted_by_mode(self):
        stats = ReachabilityStats(now=1000.0)
        stats.record_probe(True, "200", 120)
        stats.record_probe(False, "000:rc=28", 30000)
        stats.record_probe(False, "000:rc=28", 30000)
        stats.record_probe(False, "503", 400)
        self.assertEqual((stats.ok_count, stats.fail_count), (1, 3))
        self.assertEqual(stats.fail_modes, {"timeout": 2, "http_503": 1})

    def test_yellow_streaks(self):
        stats = ReachabilityStats(now=0.0)
        stats.enter_yellow(10.0)
        stats.enter_yellow(20.0)  # already yellow: the start is kept
        self.assertEqual(stats.current_yellow_start_ts, 10.0)
        stats.exit_yellow(40.0)
        stats.exit_yellow(50.0)  # not yellow: nothing counted
        self.assertIsNone(stats.current_yellow_start_ts)
        self.assertEqual(stats.total_yellow_seconds, 30.0)
        stats.enter_yellow(100.0)
        stats.exit_yellow(110.0)
        self.assertEqual(stats.total_yellow_seconds, 40.0)
        self.assertEqual(stats.longest_yellow_streak, 30.0)

    def test_reset_session_closes_an_open_streak_and_clears(self):
        stats = ReachabilityStats(now=0.0)
        stats.record_probe(False, "000", 1)
        stats.enter_yellow(5.0)
        stats.reset_session(25.0)
        self.assertEqual(stats.session_start_ts, 25.0)
        self.assertEqual((stats.ok_count, stats.fail_count), (0, 0))
        self.assertEqual(stats.fail_modes, {})
        self.assertEqual(stats.total_yellow_seconds, 0.0)
        self.assertIsNone(stats.current_yellow_start_ts)


class TestSnapshot(unittest.TestCase):
    def test_uptime_only(self):
        stats = ReachabilityStats(now=0.0)
        self.assertEqual(stats.format_snapshot(now=45.0), "snapshot: 45s up")

    def test_every_section(self):
        stats = ReachabilityStats(now=0.0)
        for _ in range(851):
            stats.record_probe(True, "200", 100)
        stats.record_probe(False, "000:rc=28", 30000)
        stats.enter_yellow(100.0)
        stats.exit_yellow(190.0)
        line = stats.format_snapshot(
            host={"ram_used_gb": 12.4, "ram_total_gb": 16.0, "load_1m": 1.2, "cpu_count": 8},
            containers={"onionpress_ram_mb": 1843.2, "total_cpu_percent": 3.1},
            now=14 * 3600 + 12 * 60)
        self.assertEqual(
            line,
            "snapshot: 14h12m up | reachability 851/852 ok (1 timeout, max yellow 90s)"
            " | host 12.4/16.0 GB ram, load 1.20, 8 cpu | onionpress 1.8 GB ram, 3.1% cpu")

    def test_an_open_streak_counts_toward_the_max(self):
        stats = ReachabilityStats(now=0.0)
        stats.record_probe(False, "503", 1)
        stats.enter_yellow(10.0)
        self.assertIn("max yellow 200s", stats.format_snapshot(now=210.0))

    def test_all_ok_has_no_mode_list(self):
        stats = ReachabilityStats(now=0.0)
        stats.record_probe(True, "200", 1)
        self.assertEqual(stats.format_snapshot(now=1.0), "snapshot: 1s up | reachability 1/1 ok")


class TestFormatters(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(rs._fmt_uptime(59), "59s")
        self.assertEqual(rs._fmt_uptime(60), "1m")
        self.assertEqual(rs._fmt_uptime(197), "3m17s")
        self.assertEqual(rs._fmt_uptime(3600), "1h00m")
        self.assertEqual(rs._fmt_uptime(51120), "14h12m")

    def test_host(self):
        self.assertEqual(rs._fmt_host(None), "")
        self.assertEqual(rs._fmt_host({}), "")
        self.assertEqual(rs._fmt_host({"ram_total_gb": 8.0}), "host 8.0 GB ram")
        self.assertEqual(rs._fmt_host({"load_1m": 0.5, "cpu_count": 4}), "host load 0.50, 4 cpu")
        self.assertEqual(rs._fmt_host({"ram_used_gb": None, "ram_total_gb": None}), "")

    def test_containers(self):
        self.assertEqual(rs._fmt_containers(None), "")
        self.assertEqual(rs._fmt_containers({"total_ram_mb": 512.0}), "onionpress 512 MB ram")
        self.assertEqual(rs._fmt_containers({"total_ram_mb": 2048.0, "onionpress_ram_mb": 3072.0}),
                         "onionpress 3.0 GB ram")
        self.assertEqual(rs._fmt_containers({"total_cpu_percent": 12.34}), "onionpress 12.3% cpu")
        self.assertEqual(rs._fmt_containers({"total_ram_mb": None}), "")


if __name__ == "__main__":
    unittest.main()
