#!/usr/bin/env python3
"""Linux runs the #266 recovery loop (issue #310).

The dual probe, the RepublishTrigger, the wake reset and the request to
tor-watchdog lived in linux/onionpress-service.py, which no installer
shipped and no unit started. They now run in onionpress.reachability,
started by the heartbeat daemon every install already has. These tests
drive the loop with a stubbed HealthChecker, pin the daemon's wiring, and
keep any future Linux entry point from being left out of the installers.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import key_manager, onion_auth, reachability  # noqa: E402
from onionpress.health import HealthResult, RepublishTrigger  # noqa: E402

SITE = "op2pi7xqz4n3m2kbdj6f5sy7gqwz4txtvzpqfsnwsmfq3rhzpcojamyd.onion"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as fh:
        return fh.read()


def _wedged(code="000:rc=28"):
    return HealthResult(wp_healthy=True, tor_bootstrapped=True,
                        tor_internally_ready=True, onion_address=SITE,
                        bootstrap_pct=100, tor_externally_reachable=False,
                        external_http_code=code)


def _reachable():
    return HealthResult(wp_healthy=True, tor_bootstrapped=True,
                        tor_internally_ready=True, onion_address=SITE,
                        bootstrap_pct=100, tor_externally_reachable=True,
                        external_http_code="200")


class _Ok:
    ok = True


class LoopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-reachability-")
        self.status_path = os.path.join(self.tmp, reachability.STATUS_FILENAME)
        self.config = {}
        self.logs = []
        self.docker = mock.Mock()
        self.docker.exec.return_value = _Ok()
        self.online = True
        self.loop = reachability.ReachabilityLoop(
            self.docker, self.logs.append,
            read_config_value=lambda k, d: self.config.get(k, d),
            status_path=self.status_path,
            has_internet=lambda: self.online,
        )
        # Fire on the first wedge sample, so the wiring is what is tested
        # (the trigger's own policy has its tests in test_onionpress_health).
        self.loop.recovery.trigger = RepublishTrigger(
            failing_for=0, fast_for=0, cooldown=0)
        self.results = []
        self.loop.checker.full_check = mock.Mock(
            side_effect=lambda **kw: self.results.pop(0))

    def republish_requests(self):
        return [c for c in self.docker.exec.call_args_list
                if reachability.launcher_ops.WATCHDOG_REPUBLISH_REQUEST_PATH
                in c.args[1]]

    def status(self):
        with open(self.status_path) as fh:
            return json.load(fh)


class TestRepublishRequest(LoopTestCase):
    def test_wedged_probe_requests_republish_from_onionpress_tor(self):
        self.results = [_wedged()]
        self.loop.run_once()
        reqs = self.republish_requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].args[0], "onionpress-tor")
        self.assertTrue(any("requested descriptor republish" in m for m in self.logs))

    def test_log_only_when_disabled(self):
        self.config["REPUBLISH_ON_UNREACHABLE"] = "no"
        self.results = [_wedged()]
        self.loop.run_once()
        self.assertEqual(self.republish_requests(), [])
        self.assertTrue(any("log-only" in m for m in self.logs))

    def test_takeover_never_requests(self):
        self.results = [_wedged("takeover")]
        self.loop.run_once()
        self.assertEqual(self.republish_requests(), [])

    def test_reachable_writes_verdict(self):
        self.results = [_reachable()]
        self.loop.run_once()
        st = self.status()
        self.assertIs(st["reachable"], True)
        self.assertEqual(st["onion_address"], SITE)
        self.assertEqual(st["http_code"], "200")
        self.assertEqual(self.republish_requests(), [])

    def test_uses_the_learned_address_next_cycle(self):
        self.results = [_reachable(), _reachable()]
        self.loop.run_once()
        self.loop.run_once()
        self.assertEqual(
            self.loop.checker.full_check.call_args.kwargs["expected_address"], SITE)


class TestWindowResets(LoopTestCase):
    def setUp(self):
        super().setUp()
        # A real window this time: nothing fires within one cycle.
        self.loop.recovery.trigger = RepublishTrigger()

    def test_wake_resets_window_and_rechecks_wordpress(self):
        self.results = [_wedged()]
        self.loop.run_once()
        self.assertIsNotNone(self.loop.recovery.trigger.failing_since)
        self.assertTrue(self.loop.monitor.state.wordpress_confirmed)
        self.loop.note_wake()
        self.assertIsNone(self.loop.recovery.trigger.failing_since)
        self.assertFalse(self.loop.monitor.state.wordpress_confirmed)

    def test_no_probe_while_asleep(self):
        self.loop.note_sleep()
        self.assertIsNone(self.loop.run_once())
        self.loop.checker.full_check.assert_not_called()

    def test_probe_in_flight_across_wake_is_not_counted(self):
        def probe(**kw):
            self.loop.note_sleep()
            self.loop.note_wake()
            return _wedged()
        self.loop.checker.full_check = mock.Mock(side_effect=probe)
        self.assertIsNone(self.loop.run_once())
        self.assertIsNone(self.loop.recovery.trigger.failing_since)

    def test_offline_resets_window(self):
        self.results = [_wedged()]
        self.loop.run_once()
        self.online = False
        self.assertIsNone(self.loop.run_once())
        self.assertIsNone(self.loop.recovery.trigger.failing_since)
        self.assertIsNone(self.status()["reachable"])

    def test_clock_gap_counts_as_wake(self):
        # Wall clock jumped 30 min while the monotonic clock did not: the
        # machine was suspended and no PrepareForSleep reached us.
        self.results = [_wedged(), _wedged()]
        with mock.patch.object(reachability.time, "time", return_value=1000.0), \
             mock.patch.object(reachability.time, "monotonic", return_value=50.0):
            self.loop.run_once()
        self.assertIsNotNone(self.loop.recovery.trigger.failing_since)
        with mock.patch.object(reachability.time, "time", return_value=2830.0), \
             mock.patch.object(reachability.time, "monotonic", return_value=80.0):
            self.loop.run_once()
        self.assertTrue(any("suspend detected" in m for m in self.logs))
        # The window was reset before this cycle's sample: one sample, not two.
        self.assertEqual(len(self.loop.recovery.trigger.samples), 1)

    def test_no_clock_gap_keeps_window(self):
        self.results = [_wedged(), _wedged()]
        with mock.patch.object(reachability.time, "time", return_value=1000.0), \
             mock.patch.object(reachability.time, "monotonic", return_value=50.0):
            self.loop.run_once()
        with mock.patch.object(reachability.time, "time", return_value=1030.0), \
             mock.patch.object(reachability.time, "monotonic", return_value=80.0):
            self.loop.run_once()
        self.assertEqual(len(self.loop.recovery.trigger.samples), 2)


class TestOneRecoveryDriver(unittest.TestCase):
    """Issue #480: the #266 decision (trigger, request, wake and offline
    resets) is reachability.RecoveryWindow's on both platforms; neither
    driver builds or feeds a RepublishTrigger itself."""

    def test_the_linux_loop_runs_a_recovery_window(self):
        loop = reachability.ReachabilityLoop(
            mock.Mock(), lambda m: None, read_config_value=lambda k, d: d,
            status_path=os.path.join(tempfile.mkdtemp(), "r.json"))
        self.assertIsInstance(loop.recovery, reachability.RecoveryWindow)
        self.assertEqual(loop.recovery.container, "onionpress-tor")

    def test_neither_driver_touches_the_trigger(self):
        loop_src = _read("src/onionpress/reachability.py")
        loop_src = loop_src[loop_src.index("class ReachabilityLoop:"):]
        for name, src in (("daemon.py", _read("src/onionpress/daemon.py")),
                          ("ReachabilityLoop", loop_src)):
            with self.subTest(driver=name):
                self.assertNotIn("RepublishTrigger(", src)
                self.assertNotIn(".observe(", src)
                self.assertNotIn("request_watchdog_republish", src)
                self.assertNotIn('"REPUBLISH_ON_UNREACHABLE",', src)


class TestHeartbeatRunsTheLoop(unittest.TestCase):
    """linux/onionpress-heartbeat.py starts the loop and forwards sleep/wake."""

    def setUp(self):
        self.src = _read("linux/onionpress-heartbeat.py")

    def test_started_before_the_self_hub_check(self):
        main = self.src[self.src.index("def main():"):]
        start = main.index("_start_reachability_loop()")
        self.assertLess(start, main.index("if _content_addr == _current_hub:"),
                        "the hub needs the recovery loop too")
        self.assertLess(start, main.index("if enabled:"),
                        "the loop must not depend on the OnionHeaven opt-in")

    def test_sleep_handler_pauses_the_loop(self):
        body = self.src[self.src.index("    def _on_prepare_for_sleep("):
                        self.src.index("    def _signal_watchdogs(")]
        sleep, wake = body.split("        else:", 1)
        self.assertIn("_reachability.note_sleep()", sleep)
        self.assertIn("_reachability.note_wake()", wake)

    def test_loop_builds_under_the_daemon(self):
        home = tempfile.mkdtemp(prefix="op-linux-recovery-")
        os.makedirs(os.path.join(home, ".onionpress"))
        saved = os.environ.get("HOME")
        os.environ["HOME"] = home
        sys.modules.setdefault("key_manager", key_manager)
        sys.modules.setdefault("onion_auth", onion_auth)
        try:
            spec = importlib.util.spec_from_file_location(
                "onionpress_heartbeat_recovery_test",
                os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py"))
            hb = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(hb)
        finally:
            if saved is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = saved
        with mock.patch.object(reachability.ReachabilityLoop, "start") as start:
            loop = hb._start_reachability_loop()
            self.assertIs(hb._start_reachability_loop(), loop)  # idempotent
        start.assert_called_once()
        self.assertIsInstance(loop, reachability.ReachabilityLoop)
        self.assertEqual(loop.status_path,
                         os.path.join(home, ".onionpress", "reachability.json"))
        self.assertIs(loop.has_internet, hb._linux_has_default_route)


class TestLauncherUsesTheVerdict(unittest.TestCase):
    def test_write_status_reads_reachability_json(self):
        # The status is the writer's (onionpress.status_writer, #523):
        # the heartbeat's verdict is its one measure of reachability, and
        # it runs no probe of its own.
        from onionpress import status_writer
        writer = _read("src/onionpress/status_writer.py")
        self.assertIn("heartbeat_verdict(self.app_support, addr, self.now)", writer)
        self.assertIn("from .reachability import STATUS_FILENAME", writer)
        self.assertNotIn(".onionpress-health", writer)
        self.assertNotIn("curl", writer)
        self.assertNotIn("reachability.json", _read("linux/onionpress"))
        self.assertEqual(reachability.STATUS_FILENAME, "reachability.json")
        self.assertEqual(status_writer.REACHABILITY_FRESH_SECONDS, 180)


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
        self.assertIn("onionpress-heartbeat.py", self._entry_points())
        self.assertIn("onionpress-tray", self._entry_points())

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
