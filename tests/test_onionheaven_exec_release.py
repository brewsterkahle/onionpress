#!/usr/bin/env python3
"""Tests for _exec_release / _has_onion_on_worker in onionheaven_common.

These guard the JSON-aware, verify-after-release contract that replaced
the returncode-only check. The previous contract silently passed
release_warning (queue-manager explicitly reported DEL_ONION failed)
and not_found-with-stale-in-memory-state through as success, leaving
orphaned takeover services serving redirects after release. The matrix
below pins down every branch of the new gate.
"""

import json
import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common  # noqa: E402


CONTAINER = "onionheaven-takeover-7"  # a worker's name: anything else is refused
ADDR = "op2ijk3cvd7kswainvwlg7uqxuoghaxzns6quht2csz3cdp5sgr2lnqd.onion"


def _result(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess-shaped mock with .returncode/.stdout/.stderr."""
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _make_side_effect(release=None, has=None):
    """Returns a farm_exec side_effect that dispatches on the verb.

    Each arg is either a result built by _result(), an Exception class to
    raise, or None (which makes the call fail the test if it happens).
    """
    def side_effect(container, verb, address=None):
        cmd = (container, verb, address)
        if verb == "release":
            target = release
            label = "release"
        elif verb == "has":
            target = has
            label = "has"
        else:
            raise AssertionError(f"Unexpected farm_exec call: {cmd}")
        if target is None:
            raise AssertionError(f"Unexpected {label} farm_exec call: {cmd}")
        if isinstance(target, type) and issubclass(target, BaseException):
            raise target(f"simulated {label} failure")
        return target
    return side_effect


class HasOnionOnWorkerTest(unittest.TestCase):
    """_has_onion_on_worker — independent probe of Tor's onions/detached."""

    def setUp(self):
        self.log_lines = []
        self._orig_log = onionheaven_common.log
        onionheaven_common.log = self.log_lines.append

    def tearDown(self):
        onionheaven_common.log = self._orig_log

    def _call(self, **kwargs):
        side = _make_side_effect(has=kwargs.get("has"))
        with mock.patch.object(onionheaven_common, "farm_exec",
                               side_effect=side):
            return onionheaven_common._has_onion_on_worker(CONTAINER, ADDR)

    def test_subprocess_exception_returns_none(self):
        self.assertIsNone(self._call(has=RuntimeError))

    def test_returncode_nonzero_returns_none(self):
        self.assertIsNone(self._call(has=_result(returncode=1)))

    def test_has_onion_true(self):
        self.assertTrue(self._call(
            has=_result(stdout=json.dumps({"address": ADDR, "has_onion": True}))))

    def test_has_onion_false(self):
        self.assertFalse(self._call(
            has=_result(stdout=json.dumps({"address": ADDR, "has_onion": False}))))

    def test_error_response_returns_none(self):
        # Older worker without the `has` command — daemon responds with error.
        self.assertIsNone(self._call(
            has=_result(stdout=json.dumps({"error": "unknown command: has ..."}))))

    def test_missing_has_onion_key_returns_none(self):
        self.assertIsNone(self._call(
            has=_result(stdout=json.dumps({"address": ADDR}))))

    def test_non_json_returns_none(self):
        self.assertIsNone(self._call(has=_result(stdout="not json")))

    def test_empty_stdout_returns_none(self):
        self.assertIsNone(self._call(has=_result(stdout="")))


class ExecReleaseTest(unittest.TestCase):
    """_exec_release — verify-after-release as source of truth."""

    def setUp(self):
        self.log_lines = []
        self.addr_log_lines = []
        self._orig_log = onionheaven_common.log
        self._orig_addr_log = onionheaven_common.addr_log
        onionheaven_common.log = self.log_lines.append
        onionheaven_common.addr_log = lambda addr, msg: self.addr_log_lines.append(
            (addr, msg))

    def tearDown(self):
        onionheaven_common.log = self._orig_log
        onionheaven_common.addr_log = self._orig_addr_log

    def _call(self, release=None, has=None):
        side = _make_side_effect(release=release, has=has)
        with mock.patch.object(onionheaven_common, "farm_exec",
                               side_effect=side):
            return onionheaven_common._exec_release(CONTAINER, ADDR)

    # --- controller-level failures (no has probe) ---

    def test_release_subprocess_exception(self):
        # No has probe because the release call itself blew up.
        self.assertFalse(self._call(release=OSError))
        self.assertTrue(any("Release error" in l for l in self.log_lines),
                        f"logs={self.log_lines}")

    def test_release_subprocess_nonzero_returncode(self):
        self.assertFalse(self._call(
            release=_result(returncode=2, stderr="docker exec failed")))
        self.assertTrue(any("Release subprocess failed" in l for l in self.log_lines),
                        f"logs={self.log_lines}")

    # --- verify probe wins over reported status ---

    def test_released_and_verified_gone(self):
        # The normal happy path: queue-manager says released, Tor confirms gone.
        self.assertTrue(self._call(
            release=_result(stdout=json.dumps({"status": "released"})),
            has=_result(stdout=json.dumps({"has_onion": False}))))
        self.assertTrue(any("Release complete" in m for _, m in self.addr_log_lines),
                        f"addr_log={self.addr_log_lines}")

    def test_released_but_verify_still_has(self):
        # Queue-manager claimed success but Tor still has the service.
        # This is the silent-failure case the old returncode check missed.
        self.assertFalse(self._call(
            release=_result(stdout=json.dumps({"status": "released"})),
            has=_result(stdout=json.dumps({"has_onion": True}))))
        self.assertTrue(any("FAILED-VERIFY" in l for l in self.log_lines),
                        f"logs={self.log_lines}")

    def test_not_found_and_verified_gone(self):
        # Normal sweep case: a non-tracked worker correctly reports
        # not_found, and verify confirms it really doesn't have the onion.
        self.assertTrue(self._call(
            release=_result(stdout=json.dumps({"status": "not_found"})),
            has=_result(stdout=json.dumps({"has_onion": False}))))

    def test_not_found_but_verify_still_has(self):
        # The OnionHeaven-takeover-1 case: queue-manager's in-memory state
        # was empty (post daemon restart) and has_onion lied — but Tor
        # actually has the orphaned service. Verify catches it.
        self.assertFalse(self._call(
            release=_result(stdout=json.dumps({"status": "not_found"})),
            has=_result(stdout=json.dumps({"has_onion": True}))))
        self.assertTrue(any("FAILED-VERIFY" in l for l in self.log_lines),
                        f"logs={self.log_lines}")

    def test_release_warning_and_verified_gone(self):
        # cmd.del_onion returned a non-250 (e.g. 552 because the service
        # was already gone — race with another release). Tor confirms
        # it's gone; we accept this as success.
        self.assertTrue(self._call(
            release=_result(stdout=json.dumps(
                {"status": "release_warning", "detail": "552 ..."})),
            has=_result(stdout=json.dumps({"has_onion": False}))))

    def test_release_warning_and_still_has(self):
        # DEL_ONION explicitly failed and the service is still there.
        self.assertFalse(self._call(
            release=_result(stdout=json.dumps({"status": "release_warning"})),
            has=_result(stdout=json.dumps({"has_onion": True}))))

    def test_non_json_release_response_verify_gone(self):
        # Garbled or truncated response from queue-manager. Verify is the
        # source of truth, so a clean Tor state still passes.
        self.assertTrue(self._call(
            release=_result(stdout="not-valid-json"),
            has=_result(stdout=json.dumps({"has_onion": False}))))

    def test_release_reports_status_appears_in_addr_log(self):
        # The diagnostic log line must include the queue-manager's
        # reported status so silent-warning patterns become greppable
        # across users.
        self._call(
            release=_result(stdout=json.dumps({"status": "release_warning"})),
            has=_result(stdout=json.dumps({"has_onion": False})))
        joined = " | ".join(m for _, m in self.addr_log_lines)
        self.assertIn("release_warning", joined,
                      f"reported= field missing from addr_log: {joined}")

    # --- verify probe unavailable: fall back to reported status ---

    def test_verify_unavailable_with_released_status(self):
        # Older worker (no `has` command) → has probe returns None → fall
        # back to JSON. `released` becomes True (same as old behavior).
        self.assertTrue(self._call(
            release=_result(stdout=json.dumps({"status": "released"})),
            has=_result(returncode=1)))

    def test_verify_unavailable_with_not_found_status(self):
        # `not_found` from a non-tracked worker — benign, return True.
        self.assertTrue(self._call(
            release=_result(stdout=json.dumps({"status": "not_found"})),
            has=_result(returncode=1)))

    def test_verify_unavailable_with_release_warning(self):
        # `release_warning` is the explicit-failure case. The whole point
        # of the new contract: stop treating it as success.
        self.assertFalse(self._call(
            release=_result(stdout=json.dumps({"status": "release_warning"})),
            has=_result(returncode=1)))

    def test_verify_unavailable_with_missing_status(self):
        # Status field absent (some unexpected response shape) — fail
        # closed when we can't verify and the JSON didn't say a known-good
        # status.
        self.assertFalse(self._call(
            release=_result(stdout=json.dumps({"address": ADDR})),
            has=_result(returncode=1)))


if __name__ == "__main__":
    unittest.main()
