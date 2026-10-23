"""Unit tests for the stress test orchestrator's configuration.

tests/stress/orchestrator/config.py needs no Docker: StressConfig only
derives counts, names and timeouts from its arguments.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stress.orchestrator.config import StressConfig  # noqa: E402


class TestStressConfig(unittest.TestCase):
    """Tests for StressConfig."""

    def test_defaults(self):
        cfg = StressConfig()
        self.assertEqual(cfg.total, 5)
        self.assertEqual(cfg.healthy, 3)
        self.assertEqual(cfg.failing, 2)
        self.assertEqual(cfg.per_ctr, 20)
        self.assertEqual(cfg.num_containers, 1)
        self.assertEqual(cfg.num_poll_clients, 1)
        self.assertEqual(cfg.mode, "worker")
        self.assertIs(cfg.no_healthcheck, False)
        self.assertEqual(cfg.base_port, 9100)

    def test_auto_split_even(self):
        cfg = StressConfig(total=10)
        self.assertEqual(cfg.healthy, 5)
        self.assertEqual(cfg.failing, 5)

    def test_auto_split_odd(self):
        cfg = StressConfig(total=7)
        self.assertEqual(cfg.failing, 3)
        self.assertEqual(cfg.healthy, 4)

    # The constructor's __post_init__ splits total before the explicit
    # flags can be set, so an explicit count goes the way from_args sets
    # it: the value and its flag, then __post_init__ again.
    def test_explicit_failing(self):
        cfg = StressConfig(total=10)
        cfg.failing = 3
        cfg._failing_set = True
        cfg.__post_init__()
        self.assertEqual(cfg.failing, 3)
        self.assertEqual(cfg.healthy, 7)
        cfg = StressConfig.from_args(["--total", "10", "--failing", "3"])
        self.assertEqual((cfg.healthy, cfg.failing), (7, 3))

    def test_explicit_healthy(self):
        cfg = StressConfig(total=10)
        cfg.healthy = 8
        cfg._healthy_set = True
        cfg.__post_init__()
        self.assertEqual(cfg.healthy, 8)
        self.assertEqual(cfg.failing, 2)
        cfg = StressConfig.from_args(["--total", "10", "--healthy", "8"])
        self.assertEqual((cfg.healthy, cfg.failing), (8, 2))

    def test_explicit_both(self):
        cfg = StressConfig.from_args(["--healthy", "6", "--failing", "4"])
        self.assertEqual((cfg.total, cfg.healthy, cfg.failing), (10, 6, 4))

    def test_num_containers_exact(self):
        cfg = StressConfig(total=20, per_ctr=20)
        self.assertEqual(cfg.num_containers, 1)

    def test_num_containers_partial(self):
        cfg = StressConfig(total=21, per_ctr=20)
        self.assertEqual(cfg.num_containers, 2)

    def test_num_containers_large(self):
        cfg = StressConfig(total=100, per_ctr=50)
        self.assertEqual(cfg.num_containers, 2)

    def test_poll_clients_clamped_min(self):
        cfg = StressConfig(total=2)
        self.assertEqual(cfg.num_poll_clients, 1)  # minimum

    def test_poll_clients_clamped_max(self):
        cfg = StressConfig(total=1000)
        self.assertEqual(cfg.num_poll_clients, 20)  # maximum

    def test_poll_clients_scaling(self):
        cfg = StressConfig(total=100)
        # (100/2 + 2) / 3 = 17
        self.assertEqual(cfg.num_poll_clients, 17)

    def test_timeouts_default(self):
        cfg = StressConfig()
        self.assertEqual(cfg.bootstrap_timeout, 900)
        self.assertEqual(cfg.takeover_timeout, 600)

    def test_timeouts_no_timeout(self):
        cfg = StressConfig(no_timeout=True)
        self.assertEqual(cfg.bootstrap_timeout, 86400)
        self.assertEqual(cfg.takeover_timeout, 86400)

    def test_workers_in_container_last(self):
        cfg = StressConfig(total=25, per_ctr=20)
        self.assertEqual(cfg.workers_in_container(0), 20)
        self.assertEqual(cfg.workers_in_container(1), 5)

    def test_workers_in_container_exact(self):
        cfg = StressConfig(total=20, per_ctr=20)
        self.assertEqual(cfg.workers_in_container(0), 20)

    def test_container_name(self):
        cfg = StressConfig()
        self.assertEqual(cfg.container_name(0), "stress-worker-0")
        self.assertEqual(cfg.container_name(5), "stress-worker-5")

    def test_poll_client_name(self):
        cfg = StressConfig()
        self.assertEqual(cfg.poll_client_name(0), "stress-poll-client-0")

    def test_fail_start(self):
        cfg = StressConfig(total=10)
        self.assertEqual(cfg.fail_start, 5)  # total - failing

    def test_stress_version_format(self):
        cfg = StressConfig()
        self.assertTrue(cfg.stress_version.startswith("stress-test-"))
        self.assertIn(str(os.getpid()), cfg.stress_version)

    def test_from_args_basic(self):
        cfg = StressConfig.from_args(["--total", "20", "--per-ctr", "10"])
        self.assertEqual(cfg.total, 20)
        self.assertEqual(cfg.per_ctr, 10)
        self.assertEqual(cfg.num_containers, 2)

    def test_from_args_cleanup(self):
        cfg = StressConfig.from_args(["--cleanup"])
        self.assertIs(cfg.cleanup, True)

    def test_from_args_no_timeout(self):
        cfg = StressConfig.from_args(["--no-timeout"])
        self.assertIs(cfg.no_timeout, True)
        self.assertEqual(cfg.bootstrap_timeout, 86400)

    def test_from_args_mode(self):
        cfg = StressConfig.from_args(["--mode", "worker"])
        self.assertEqual(cfg.mode, "worker")


if __name__ == "__main__":
    unittest.main()
