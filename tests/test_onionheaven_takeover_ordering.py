#!/usr/bin/env python3
"""Tests for the write ordering of takeover_function in onionheaven_common.

Issue #232: takeover_function used to flip the row to status='taken-over',
bump last_taken_over and COMMIT before running any of its three guards,
and each guard early-returned without rolling back. The heartbeat loop
checks staleness, then calls takeover_function, which re-reads the row;
an /online heartbeat landing in between refreshes last_healthy, the
last_healthy guard correctly declines — and the row is left claiming a
takeover that never queued an ADD_ONION. The audit pipeline then locked
such orphans into confirmed_dead.

The contract pinned here: nothing is written to the registry until a
worker has accepted the address. Every test runs against an in-memory
sqlite registry with _exec_takeover, _ensure_capacity and is_farm_mode
stubbed, and subprocess.run patched to fail loudly, so no Docker, Tor or
control-port call can ever happen.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common as oc  # noqa: E402


CA = "op2w5bhnczf4ryaxldryql7guydj2r52xhbj6o6kjscji5pruzwqyxad.onion"
HA = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
HA_SIBLING = "op2hecfkpduw2iou5aiul3tbg57tnkxqgwgq4uht4vyedanvbcv2wwqd.onion"
WORKER = "onionheaven-takeover-0"
WORKER_PREFERRED = "onionheaven-takeover-1"

# Comfortably past PROPAGATION_DELAY however the env sets it.
STALE_AGO = oc.PROPAGATION_DELAY + 60


def _ts(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class TakeoverOrderingTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        oc.db_ensure_schema(self.conn)
        self._add_worker(WORKER)

        self.logs = []
        # Each stubbed _exec_takeover call records (worker, address, and the
        # registry status the worker would see at that instant).
        self.exec_calls = []
        self.exec_result = True
        self.during_exec = None  # callable run while the "docker exec" is in flight
        self.capacity_calls = 0

        patches = [
            mock.patch.object(oc, "log", self.logs.append),
            mock.patch.object(oc, "is_farm_mode", lambda: True),
            mock.patch.object(oc, "_ensure_capacity", self._fake_ensure_capacity),
            mock.patch.object(oc, "_exec_takeover", self._fake_exec_takeover),
            mock.patch.object(
                oc.subprocess, "run",
                side_effect=AssertionError(
                    "subprocess.run must not be called — these tests never touch Docker")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.conn.close)

    # -- fixtures ------------------------------------------------------------

    def _add_worker(self, name, bootstrapped=1, assigned=0):
        self.conn.execute(
            "INSERT INTO takeover_containers "
            "(container_name, bootstrapped, assigned_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, bootstrapped, assigned, _ts()))
        self.conn.commit()

    def _seed(self, ha=HA, status="online", last_healthy_ago=STALE_AGO,
              takeover_container=None, takeover_pending=None):
        self.conn.execute(
            "INSERT INTO registry (content_address, healthcheck_address, "
            "registered_at, status, last_healthy, takeover_container, "
            "takeover_pending) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (CA, ha, _ts(3600), status, _ts(last_healthy_ago),
             takeover_container, takeover_pending))
        self.conn.commit()

    def _row(self, ha=HA):
        return dict(self.conn.execute(
            "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
            (CA, ha)).fetchone())

    def _assigned(self, worker=WORKER):
        return self.conn.execute(
            "SELECT assigned_count FROM takeover_containers WHERE container_name = ?",
            (worker,)).fetchone()[0]

    def _fake_exec_takeover(self, worker, addr):
        status_seen = self.conn.execute(
            "SELECT status FROM registry WHERE content_address = ? AND healthcheck_address = ?",
            (addr, HA)).fetchone()[0]
        self.exec_calls.append((worker, addr, status_seen))
        if self.during_exec:
            self.during_exec()
        self.logs.append(f"STUB: queued {addr} on {worker}")
        return self.exec_result

    def _fake_ensure_capacity(self, conn):
        self.capacity_calls += 1

    def _heartbeat_loop_thinks_stale(self):
        """Replay the heartbeat main loop's own staleness check."""
        lh = datetime.fromisoformat(self._row()["last_healthy"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - lh).total_seconds() > oc.PROPAGATION_DELAY

    def _online_heartbeat_arrives(self):
        """What web-server.py's /online upsert does to an existing online row."""
        self.conn.execute(
            "UPDATE registry SET last_healthy = ? "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (_ts(0), CA, HA))
        self.conn.commit()

    def _assert_untouched(self, ha=HA, status="online"):
        row = self._row(ha)
        self.assertEqual(row["status"], status, f"status changed: {row} logs={self.logs}")
        self.assertIsNone(row["last_taken_over"], f"last_taken_over set: {row}")
        self.assertIsNone(row["takeover_container"], f"takeover_container set: {row}")
        self.assertEqual(self._assigned(), 0)
        self.assertFalse(any("Marked" in l for l in self.logs),
                         f"must not log a takeover that did not happen: {self.logs}")

    # -- the race from #232 --------------------------------------------------

    def test_online_heartbeat_between_check_and_call_leaves_row_untouched(self):
        self._seed(last_healthy_ago=STALE_AGO)
        self.assertTrue(self._heartbeat_loop_thinks_stale())

        # Race: the real instance's /online lands after the loop's check
        # and before takeover_function re-reads the row.
        self._online_heartbeat_arrives()
        oc.takeover_function(self.conn, CA, HA, force=False)

        self._assert_untouched()
        self.assertEqual(self.exec_calls, [], "no ADD_ONION may be queued")
        self.assertEqual(self.capacity_calls, 0)
        self.assertTrue(any("last_healthy not yet stale" in l for l in self.logs), self.logs)

    def test_other_guards_decline_without_writes(self):
        cases = {
            "sibling online": ("online", "other row(s) still online"),
            "sibling taken-over": ("taken-over", "another row already taken-over"),
        }
        for name, (sibling_status, expected_log) in cases.items():
            with self.subTest(name):
                self.conn.execute("DELETE FROM registry")
                self.conn.commit()
                self.logs.clear()
                self.exec_calls.clear()
                self._seed(last_healthy_ago=STALE_AGO)
                self._seed(ha=HA_SIBLING, status=sibling_status, last_healthy_ago=0)

                oc.takeover_function(self.conn, CA, HA, force=False)

                self._assert_untouched()
                self.assertEqual(self.exec_calls, [])
                self.assertTrue(any(expected_log in l for l in self.logs), self.logs)

    # -- failures after the guards -------------------------------------------

    def test_exec_takeover_failure_leaves_status_unchanged(self):
        self._seed(last_healthy_ago=STALE_AGO)
        self.exec_result = False

        oc.takeover_function(self.conn, CA, HA, force=False)

        self._assert_untouched()
        self.assertEqual(len(self.exec_calls), 1, "the queue was asked exactly once")
        self.assertEqual(self.capacity_calls, 1,
                         "capacity is still checked after a refused queue")

    def test_no_bootstrapped_worker_defers_without_writes(self):
        self.conn.execute("UPDATE takeover_containers SET bootstrapped = 0")
        self.conn.commit()
        self._seed(last_healthy_ago=STALE_AGO)

        oc.takeover_function(self.conn, CA, HA, force=False)

        self._assert_untouched()
        self.assertEqual(self.exec_calls, [])
        self.assertEqual(self.capacity_calls, 1, "workers are requested")
        self.assertTrue(any("Takeover deferred" in l for l in self.logs), self.logs)

    def test_row_changed_during_exec_is_not_overwritten(self):
        self._seed(last_healthy_ago=STALE_AGO)

        def unregister_lands():
            self.conn.execute(
                "UPDATE registry SET status = 'unregistered', unregistered_at = ? "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (_ts(0), CA, HA))
            self.conn.commit()
        self.during_exec = unregister_lands

        oc.takeover_function(self.conn, CA, HA, force=False)

        row = self._row()
        self.assertEqual(row["status"], "unregistered", row)
        self.assertIsNone(row["last_taken_over"], row)
        self.assertIsNone(row["takeover_container"], row)
        self.assertEqual(self._assigned(), 0)
        self.assertFalse(any("Marked" in l for l in self.logs), self.logs)
        self.assertTrue(any("not recorded" in l for l in self.logs), self.logs)

    # -- the happy path, with the ordering pinned ----------------------------

    def test_success_records_transition_only_after_queue_accepted(self):
        self._seed(last_healthy_ago=STALE_AGO, takeover_pending=_ts(30))

        oc.takeover_function(self.conn, CA, HA, force=False)

        # The worker was asked while the row was still 'online': no write
        # happened before the ADD_ONION was queued.
        self.assertEqual(self.exec_calls, [(WORKER, CA, "online")])

        row = self._row()
        self.assertEqual(row["status"], "taken-over", row)
        self.assertIsNotNone(row["last_taken_over"], row)
        self.assertEqual(row["takeover_container"], WORKER, row)
        self.assertIsNone(row["takeover_pending"], row)
        self.assertEqual(self._assigned(), 1)
        self.assertEqual(self.capacity_calls, 1)

        marked = [i for i, l in enumerate(self.logs)
                  if l.startswith(f"Marked {HA} as taken-over for {CA}")]
        queued = [i for i, l in enumerate(self.logs) if l.startswith("STUB: queued")]
        self.assertEqual(len(marked), 1, self.logs)
        self.assertEqual(len(queued), 1, self.logs)
        self.assertGreater(marked[0], queued[0],
                           f"'Marked' must be logged after the queue accepted: {self.logs}")

    def test_force_takes_over_fresh_row(self):
        # /offline calls with force=True: a fresh last_healthy is not a guard.
        self._seed(last_healthy_ago=0)

        oc.takeover_function(self.conn, CA, HA, force=True)

        row = self._row()
        self.assertEqual(row["status"], "taken-over", row)
        self.assertEqual(row["takeover_container"], WORKER, row)
        self.assertEqual(self.exec_calls, [(WORKER, CA, "online")])

    def test_previous_worker_is_preferred(self):
        self._add_worker(WORKER_PREFERRED, assigned=10)
        self._seed(last_healthy_ago=STALE_AGO, takeover_container=WORKER_PREFERRED)

        oc.takeover_function(self.conn, CA, HA, force=False)

        self.assertEqual(self.exec_calls, [(WORKER_PREFERRED, CA, "online")])
        row = self._row()
        self.assertEqual(row["status"], "taken-over", row)
        self.assertEqual(row["takeover_container"], WORKER_PREFERRED, row)
        self.assertEqual(self._assigned(WORKER_PREFERRED), 11)
        self.assertEqual(self._assigned(WORKER), 0)

    def test_already_taken_over_row_is_ignored_without_force(self):
        self._seed(status="taken-over", last_healthy_ago=STALE_AGO,
                   takeover_container=WORKER)
        self.conn.execute(
            "UPDATE registry SET last_taken_over = ? WHERE content_address = ?",
            ("2026-05-14T18:29:06Z", CA))
        self.conn.commit()

        oc.takeover_function(self.conn, CA, HA, force=False)

        self.assertEqual(self.exec_calls, [])
        self.assertEqual(self._row()["last_taken_over"], "2026-05-14T18:29:06Z")
        self.assertEqual(self.logs, [])


if __name__ == "__main__":
    unittest.main()
