"""Issue #390: a hub-side outage is not a takeover storm.

The monitor took a site over when its own /online stopped landing for
PROPAGATION_DELAY, with no check of the form "no site has heartbeated,
so the problem is ours". Now fleet_brake() looks at the pass as a whole:
when at least BRAKE_MIN_SITES sites are online and BRAKE_FRACTION of
them or more are stale at once, no site is taken over for a missed
heartbeat (a signed /offline still is), once said loudly; when the share
drops under the line, every online site gets a fresh grace period. And
at most STALE_TAKEOVERS_PER_PASS stale sites are taken over per pass.
"""

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
import onionheaven_common as oc  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hbm = _load("onionheaven_heartbeat_fleet_brake", "onionheaven-heartbeat.py")

STALE = "2026-01-01T00:00:00Z"
FRESH = "2999-01-01T00:00:00Z"


class _Stop(BaseException):
    pass


def _addr(i):
    return (f"{i:056d}".replace("0", "a").replace("1", "b").replace("2", "c")
            .replace("3", "d").replace("4", "e").replace("5", "f").replace("6", "g")
            .replace("7", "h").replace("8", "i").replace("9", "j") + ".onion")


class FleetBrakeTest(unittest.TestCase):
    """One pass of the monitor's main loop on a temp registry."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-fleet-brake-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.taken = []
        self.logged = []
        noop = lambda *a, **k: None  # noqa: E731
        for target, name, value in (
                (oc, "ONIONHEAVEN_DATA_DIR", self.base),
                (oc, "DB_PATH", os.path.join(self.base, "registry.db")),
                (oc, "log", self.logged.append),
                (hbm, "ONIONHEAVEN_DATA_DIR", self.base),
                (hbm, "log", self.logged.append),
                (hbm, "wait_for_hub_admission", noop),
                (hbm, "startup_reconciliation", noop),
                (hbm, "check_worker_bootstrap", noop),
                (hbm, "cleanup_dead_workers", noop),
                (hbm, "takeover_function",
                 lambda conn, ca, ha, force=False: self.taken.append((ca, force))),
                (hbm, "wall_sleep", self._stop),
                (hbm, "_verification_was_unavailable", False),
                (hbm, "_brake_engaged", False)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        conn = oc.db_connect()
        oc.db_ensure_schema(conn)
        conn.close()

    @staticmethod
    def _stop(_seconds):
        raise _Stop()

    def _rows(self, stale, fresh):
        conn = oc.db_connect()
        for i in range(stale + fresh):
            conn.execute(
                "INSERT INTO registry (content_address, healthcheck_address, registered_at, "
                "last_healthy, status) VALUES (?, ?, '2026-01-01T00:00:00Z', ?, 'online')",
                (_addr(i), _addr(1000 + i), STALE if i < stale else FRESH))
        conn.commit()
        conn.close()

    def _pass(self):
        with self.assertRaises(_Stop):
            hbm.main()

    def _last_healthy(self):
        conn = sqlite3.connect(oc.DB_PATH)
        try:
            return [r[0] for r in conn.execute("SELECT last_healthy FROM registry ORDER BY content_address")]
        finally:
            conn.close()

    def test_defaults(self):
        self.assertEqual(hbm.BRAKE_MIN_SITES, 5)
        self.assertEqual(hbm.BRAKE_FRACTION, 0.5)
        self.assertEqual(hbm.STALE_TAKEOVERS_PER_PASS, 25)

    def test_most_of_the_fleet_stale_at_once_takes_over_nothing(self):
        self._rows(stale=8, fresh=2)
        self._pass()
        self._pass()
        self.assertEqual(self.taken, [])
        engaged = [m for m in self.logged if m.startswith("FLEET BRAKE:")]
        self.assertEqual(len(engaged), 1, self.logged)
        self.assertIn("8 of 10 online sites", engaged[0])
        self.assertTrue(any("8 stale heartbeats not acted on (fleet brake: 8 of 10" in m
                            for m in self.logged), self.logged)
        self.assertEqual(self._last_healthy().count(STALE), 8, "nothing was touched")

    def test_a_few_stale_sites_are_taken_over(self):
        self._rows(stale=3, fresh=7)
        self._pass()
        self.assertEqual(sorted(ca for ca, _ in self.taken), [_addr(0), _addr(1), _addr(2)])
        self.assertFalse(any("FLEET BRAKE" in m for m in self.logged))

    def test_exactly_the_fraction_engages(self):
        self._rows(stale=5, fresh=5)
        self._pass()
        self.assertEqual(self.taken, [])
        self.assertTrue(any(m.startswith("FLEET BRAKE:") for m in self.logged))

    def test_a_small_fleet_is_below_the_brake(self):
        self._rows(stale=4, fresh=0)       # under BRAKE_MIN_SITES
        self._pass()
        self.assertEqual(len(self.taken), 4)
        self.assertFalse(any("FLEET BRAKE" in m for m in self.logged))

    def test_release_gives_every_site_a_fresh_grace_period(self):
        self._rows(stale=8, fresh=2)
        self._pass()
        self.assertEqual(self.taken, [])
        # The hub recovers: seven sites heartbeat again, one is really down.
        conn = sqlite3.connect(oc.DB_PATH)
        conn.execute("UPDATE registry SET last_healthy = ? WHERE content_address != ?",
                     (FRESH, _addr(0)))
        conn.commit()
        conn.close()
        self._pass()
        self.assertEqual(self.taken, [], "the released brake grants a grace period first")
        self.assertTrue(any(m.startswith("FLEET BRAKE released") for m in self.logged))
        self.assertNotIn(STALE, self._last_healthy())
        self.assertFalse(hbm._brake_engaged)

    def test_a_signed_offline_is_honoured_under_the_brake(self):
        self._rows(stale=8, fresh=2)
        conn = sqlite3.connect(oc.DB_PATH)
        conn.execute("UPDATE registry SET last_healthy = ?, takeover_requested = ? "
                     "WHERE content_address = ?", (FRESH, "2999-01-01T00:00:01Z", _addr(9)))
        conn.commit()
        conn.close()
        self._pass()
        self.assertEqual(self.taken, [(_addr(9), True)])

    def test_at_most_the_per_pass_cap_then_the_rest_next_pass(self):
        with mock.patch.object(hbm, "STALE_TAKEOVERS_PER_PASS", 3):
            self._rows(stale=4, fresh=10)       # 4 of 14: under the fraction
            self._pass()
            self.assertEqual(len(self.taken), 3)
            self.assertTrue(any("1 stale heartbeats deferred to the next pass" in m
                                for m in self.logged), self.logged)
            # The three taken over are no longer 'online' in a real run
            # (takeover_function changes status); the stub does not, so
            # mark them to see the fourth go next pass.
            conn = sqlite3.connect(oc.DB_PATH)
            for ca, _ in self.taken:
                conn.execute("UPDATE registry SET status = 'taken-over' WHERE content_address = ?", (ca,))
            conn.commit()
            conn.close()
            self.taken.clear()
            self._pass()
            self.assertEqual(len(self.taken), 1)

    def test_compose_passes_the_knobs(self):
        with open(os.path.join(PROJECT_ROOT, "app/Resources/docker/docker-compose.yml")) as f:
            compose = f.read()
        for var, default in (("ONIONHEAVEN_BRAKE_MIN_SITES", "5"),
                             ("ONIONHEAVEN_BRAKE_FRACTION", "0.5"),
                             ("ONIONHEAVEN_STALE_TAKEOVERS_PER_PASS", "25")):
            with self.subTest(var=var):
                self.assertIn(f"- {var}=${{{var}:-{default}}}", compose)


if __name__ == "__main__":
    unittest.main()
