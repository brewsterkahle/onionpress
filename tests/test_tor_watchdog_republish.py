"""Tests for the host republish request in app/Resources/docker/tor/tor-watchdog.py.

The watchdog is a standalone script (no package), so it is loaded with
importlib. Nothing here touches a control port or Docker: send_cmd is
replaced with a recorder, and the request path is pointed at a temp dir.
"""

import importlib.util
import os
import tempfile
import time
import unittest
from unittest import mock

_WATCHDOG = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor",
    "tor-watchdog.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("tor_watchdog_under_test", _WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Stands in for send_cmd: records commands, answers 250 to everything."""

    def __init__(self):
        self.commands = []

    def __call__(self, sock, cmd):
        self.commands.append(cmd)
        return "250 OK\r\n"


class TestRepublishRequest(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run", "republish-requested")
        self.mod.REPUBLISH_REQUEST_PATH = self.path
        self.mod.log = lambda msg: self.logged.append(msg)
        self.logged = []
        self.sent = _Recorder()
        self.mod.send_cmd = self.sent
        self.state = self.mod.WatchdogState()
        self.state.bootstrapped = True
        self.state.services_active = True
        self.state.services = [
            {"service_id": "op2" + "a" * 53, "service_name": "wordpress",
             "key_b64": "KEY1", "ports": ["80,wordpress:80"]},
            {"service_id": "xod" + "b" * 53, "service_name": "healthcheck",
             "key_b64": "KEY2", "ports": ["80,127.0.0.1:8081"]},
        ]

    def _request(self, reason="unreachable 612s http=000:rc=97"):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(reason + "\n")

    # -- the happy path -------------------------------------------------

    def test_request_triggers_del_then_add_for_every_service(self):
        self._request()
        self.assertTrue(self.mod.check_republish_request(None, self.state))
        cmds = self.sent.commands
        self.assertEqual(
            [c.split()[0] for c in cmds],
            ["DEL_ONION", "DEL_ONION", "ADD_ONION", "ADD_ONION"],
        )
        self.assertIn(self.state.services[0]["service_id"], cmds[0])
        self.assertIn("ED25519-V3:KEY1", cmds[2])
        self.assertIn("Flags=Detach", cmds[2])
        self.assertFalse(os.path.exists(self.path), "request file consumed")
        self.assertTrue(self.state.services_active)
        self.assertTrue(any("Republish requested by host (unreachable 612s http=000:rc=97)" in m
                            for m in self.logged), self.logged)

    def test_republish_arms_the_hs_desc_stall_monitor(self):
        # A forced republish that never uploads must get the existing
        # DEL+ADD retry from check_stalls, so the monitor is re-armed with
        # its own trigger name.
        self.state.hs_desc_uploaded_since_recovery = True
        self.state.last_recovery_time = 0
        self._request()
        before = time.time()
        self.mod.check_republish_request(None, self.state)
        self.assertGreaterEqual(self.state.last_recovery_time, before)
        self.assertEqual(self.state.last_recovery_trigger, "republish-request")
        self.assertFalse(self.state.hs_desc_uploaded_since_recovery)
        self.assertGreaterEqual(self.state.last_forced_republish, before)

    def test_no_request_is_a_no_op(self):
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])

    # -- guards: file stays, request honoured later -----------------------

    def test_cooldown_defers_and_keeps_the_file(self):
        self.state.last_forced_republish = time.time() - 60
        self._request()
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])
        self.assertTrue(os.path.exists(self.path), "request kept for later")
        # One "deferred" line, not one per 5s poll.
        self.mod.check_republish_request(None, self.state)
        self.mod.check_republish_request(None, self.state)
        deferred = [m for m in self.logged if "deferred" in m]
        self.assertEqual(len(deferred), 1, self.logged)
        self.assertIn("cooldown", deferred[0])
        # Cooldown over → acted on without a new request from the host.
        self.state.last_forced_republish = time.time() - self.mod.REPUBLISH_COOLDOWN - 1
        self.assertTrue(self.mod.check_republish_request(None, self.state))
        self.assertFalse(os.path.exists(self.path))

    def test_fresh_descriptor_is_not_torn_down(self):
        # The HSDirs may simply not have the new descriptor yet; give the
        # upload REPUBLISH_MIN_AGE_AFTER_UPLOAD before replacing it.
        self.state.last_hs_desc_uploaded = time.time() - 30
        self._request()
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(any("uploaded only" in m for m in self.logged), self.logged)
        self.state.last_hs_desc_uploaded = time.time() - self.mod.REPUBLISH_MIN_AGE_AFTER_UPLOAD - 1
        self.assertTrue(self.mod.check_republish_request(None, self.state))

    def test_not_bootstrapped_defers(self):
        self.state.bootstrapped = False
        self._request()
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])
        self.assertTrue(os.path.exists(self.path))
        self.state.bootstrapped = True
        self.assertTrue(self.mod.check_republish_request(None, self.state))

    # -- guards: request discarded ---------------------------------------

    def test_asleep_discards_the_request(self):
        # DEL_ONION at sleep was deliberate; the wake USR2 republishes.
        self.state.sleeping = True
        self._request()
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(any("ignored — asleep" in m for m in self.logged), self.logged)

    def test_no_services_discards_the_request(self):
        self.state.services = []
        self._request()
        self.assertFalse(self.mod.check_republish_request(None, self.state))
        self.assertEqual(self.sent.commands, [])
        self.assertFalse(os.path.exists(self.path))

    # -- plumbing --------------------------------------------------------

    def test_hs_desc_uploaded_event_records_the_time(self):
        before = time.time()
        self.mod.process_event(
            "650 HS_DESC UPLOADED op2abc NO_AUTH $ABC~relay", None, self.state)
        self.assertGreaterEqual(self.state.last_hs_desc_uploaded, before)
        # "UPLOAD " (in flight) must not count as uploaded.
        self.state.last_hs_desc_uploaded = 0
        self.mod.process_event(
            "650 HS_DESC UPLOAD op2abc NO_AUTH $ABC~relay", None, self.state)
        self.assertEqual(self.state.last_hs_desc_uploaded, 0)

    def test_check_stalls_polls_the_request(self):
        # check_stalls is what the main loop calls every recv timeout.
        self._request()
        with mock.patch.object(self.mod, "check_republish_request",
                               return_value=True) as crr:
            self.mod.check_stalls(None, self.state)
        crr.assert_called_once()

    def test_startup_discards_a_stale_request(self):
        # run() clears any file left from the container's previous life
        # before its startup ADD_ONION (itself a fresh publish).
        self._request()
        self.mod.clear_republish_request()
        self.assertFalse(os.path.exists(self.path))
        self.mod.clear_republish_request()  # idempotent
        with open(_WATCHDOG) as f:
            src = f.read()
        start = src.index("def run():")
        self.assertIn("clear_republish_request()", src[start:start + 1500])

    def test_reason_is_flattened_and_bounded(self):
        self._request("a\nb\t" + "x" * 500)
        reason = self.mod.read_republish_request()
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), 200)
        self._request("")
        self.assertEqual(self.mod.read_republish_request(), "unspecified")


if __name__ == "__main__":
    unittest.main()
