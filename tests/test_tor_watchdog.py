"""Tests for app/Resources/docker/tor/tor-watchdog.py — the escalation ladder.

The failure these exist for: after a Mac sleep on 2026-08-08 the onion went
dark and stayed dark for ~20 minutes. Tor reported bootstrapped=100% the whole
time (stale), so every rung above DROPGUARDS — all of them gated on
`not bootstrapped` — was unreachable, and the wedged snowflake-client was
never restarted. The ladder now hangs off SERVING instead.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

_WATCHDOG = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor", "tor-watchdog.py"
)
_spec = importlib.util.spec_from_file_location("tor_watchdog", _WATCHDOG)
tw = importlib.util.module_from_spec(_spec)
sys.modules["tor_watchdog"] = tw
_spec.loader.exec_module(tw)


class FakeSock:
    """Records commands; answers GETINFO from a canned map."""

    def __init__(self, answers=None):
        self.sent = []
        self.answers = answers or {}

    def sendall(self, data):
        self.sent.append(data.decode().strip())

    def recv(self, _n):
        cmd = self.sent[-1] if self.sent else ""
        return (self.answers.get(cmd, "250 OK") + "\r\n").encode()


def _serving_state():
    """A healthy onion-service watchdog: bootstrapped, attached, published."""
    s = tw.WatchdogState()
    s.bootstrapped = True
    s.services = [{"service_name": "wordpress", "service_id": "abc", "key_b64": "k", "ports": ["80,127.0.0.1:8080"]}]
    s.services_active = True
    s.last_recovery_time = 0
    return s


class TestIsServing(unittest.TestCase):
    def test_healthy_stack_is_serving(self):
        self.assertTrue(tw.is_serving(_serving_state(), circuit_established=True))

    def test_bootstrapped_but_no_circuits_is_not_serving(self):
        # The exact post-sleep shape: Tor still says 100%, nobody can reach us.
        self.assertFalse(tw.is_serving(_serving_state(), circuit_established=False))

    def test_circuits_but_undelivered_descriptor_is_not_serving(self):
        s = _serving_state()
        s.last_recovery_time = 1000
        s.hs_desc_uploaded_since_recovery = False
        self.assertFalse(tw.is_serving(s, circuit_established=True))

    def test_descriptor_landing_restores_serving(self):
        s = _serving_state()
        s.last_recovery_time = 1000
        s.hs_desc_uploaded_since_recovery = True
        self.assertTrue(tw.is_serving(s, circuit_established=True))

    def test_socks_only_container_serves_on_circuits_alone(self):
        s = tw.WatchdogState()
        s.bootstrapped = True
        s.services = []
        self.assertTrue(tw.is_serving(s, circuit_established=True))


class TestLadder(unittest.TestCase):
    """`next_escalation` is the whole ladder, and it is pure."""

    def _down_for(self, seconds, **kw):
        s = _serving_state()
        s.not_serving_since = 1_000_000
        for k, v in kw.items():
            setattr(s, k, v)
        return s, 1_000_000 + seconds

    def test_serving_never_escalates(self):
        s = _serving_state()
        self.assertIsNone(tw.next_escalation(s, 1_000_000, has_transport=True))

    def test_nothing_happens_before_the_transport_rung(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER - 1)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_transport_restart_is_due_at_its_threshold(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-pt")

    def test_no_transport_configured_skips_that_rung(self):
        # A direct-connection Tor has no managed proxy to restart; going
        # straight to a process restart at 180s would be gratuitous.
        s, now = self._down_for(tw.PT_RESTART_AFTER)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=False))

    def test_transport_restart_is_rate_limited(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER + 10,
                                last_pt_restart=1_000_000 + tw.PT_RESTART_AFTER)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_tor_restart_takes_over_when_the_transport_restart_did_not_help(self):
        s, now = self._down_for(tw.TOR_RESTART_AFTER,
                                last_pt_restart=1_000_000 + tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-tor")

    def test_restarts_that_change_nothing_end_in_degraded(self):
        base = 1_000_000
        s, now = self._down_for(2_000, tor_restarts=[base + 1, base + 2, base + 3])
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "degraded")

    def test_degraded_stops_climbing(self):
        s, now = self._down_for(99_999, degraded=True)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_restarts_outside_the_window_do_not_count_toward_degraded(self):
        base = 1_000_000
        s, now = self._down_for(tw.TOR_RESTART_AFTER,
                                tor_restarts=[base - tw.DEGRADED_WINDOW - 10] * 3)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-tor")

    def test_the_ladder_never_climbs_back_down_to_the_transport_rung(self):
        # Restarting Tor already re-execs the transport, so offering the
        # smaller action again while the bigger one cools off would just make
        # the loop busier than the rung it replaced.
        base = 1_000_000
        s, now = self._down_for(tw.TOR_RESTART_AFTER + 60,
                                tor_restarts=[base + tw.TOR_RESTART_AFTER])
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))


class TestRestartTransport(unittest.TestCase):
    def test_kills_every_transport_process_then_reloads(self):
        sock = FakeSock()
        state = tw.WatchdogState()
        killed = []
        tw.do_restart_pt(sock, state, "test", kill=lambda p, s: killed.append(p), pids=[41, 42])

        self.assertEqual(killed, [41, 42])
        self.assertIn("SIGNAL RELOAD", sock.sent)

    def test_sends_no_add_onion(self):
        # The address-safety property for this rung: it cannot change the
        # address because it never touches a key or an onion service.
        sock = FakeSock()
        tw.do_restart_pt(sock, tw.WatchdogState(), "test", kill=lambda p, s: None, pids=[1])
        self.assertFalse(any("ONION" in c for c in sock.sent))

    def test_missing_process_still_records_the_attempt(self):
        # Otherwise a container with no transport retries every pass.
        state = tw.WatchdogState()
        self.assertFalse(tw.do_restart_pt(FakeSock(), state, "test", pids=[]))
        self.assertGreater(state.last_pt_restart, 0)

    def test_finds_transport_pids_from_proc(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, cmd in (("7", "/usr/bin/snowflake-client\0-url\0x"),
                             ("9", "/usr/sbin/apache2\0-D\0FOREGROUND")):
                os.mkdir(os.path.join(root, pid))
                with open(os.path.join(root, pid, "cmdline"), "w") as f:
                    f.write(cmd)
            os.mkdir(os.path.join(root, "self"))  # non-numeric entries ignored
            self.assertEqual(tw._pt_pids(proc_root=root), [7])


class TestVanityAddressSurvives(unittest.TestCase):
    """The catastrophic regression: recovery that mints a NEW address.

    The user has published theirs. Every rung must either reuse the on-disk
    key or do nothing.
    """

    def _service_dir(self, root, name, key=None, pubkey=None, hostname=None):
        d = os.path.join(root, name)
        os.makedirs(d)
        if key is not None:
            with open(os.path.join(d, "hs_ed25519_secret_key"), "wb") as f:
                f.write(key)
        if pubkey is not None:
            with open(os.path.join(d, "hs_ed25519_public_key"), "wb") as f:
                f.write(pubkey)
        if hostname is not None:
            with open(os.path.join(d, "hostname"), "w") as f:
                f.write(hostname + "\n")
        return d

    def _discover(self, root, defs):
        cfg = os.path.join(root, "services.json")
        with open(cfg, "w") as f:
            json.dump(defs, f)
        real_base, real_open = tw.HS_BASE_DIR, None
        tw.HS_BASE_DIR = root
        try:
            import builtins
            real_open = builtins.open

            def fake_open(path, *a, **kw):
                if path == "/etc/tor/onion-services.json":
                    return real_open(cfg, *a, **kw)
                return real_open(path, *a, **kw)

            builtins.open = fake_open
            return tw.discover_services()
        finally:
            if real_open:
                import builtins
                builtins.open = real_open
            tw.HS_BASE_DIR = real_base

    def test_unreadable_key_for_a_published_address_refuses_new_best(self):
        with tempfile.TemporaryDirectory() as root:
            self._service_dir(root, "wordpress", key=b"too-short",
                              hostname="vanityaddress.onion")
            svcs = self._discover(root, [{"name": "wordpress", "ports": ["80,127.0.0.1:8080"]}])

            self.assertEqual(len(svcs), 1)
            self.assertTrue(svcs[0]["key_unreadable"])
            # It keeps the address it already publishes...
            self.assertEqual(svcs[0]["service_id"], "vanityaddress")
            # ...and carries no key, so nothing can ADD it under a new one.
            self.assertIsNone(svcs[0]["key_b64"])

    def test_add_onion_skips_a_service_whose_key_is_unreadable(self):
        sock = FakeSock()
        added, collisions = tw.add_all_services(sock, [{
            "service_name": "wordpress", "service_id": "vanityaddress",
            "key_b64": None, "key_unreadable": True, "ports": ["80,127.0.0.1:8080"],
        }])

        self.assertEqual((added, collisions), (0, 0))
        self.assertFalse(any("NEW:BEST" in c for c in sock.sent),
                         "NEW:BEST here would replace the user's published address")

    def test_a_healthy_service_is_added_with_its_own_key(self):
        sock = FakeSock()
        tw.add_all_services(sock, [{
            "service_name": "wordpress", "service_id": "vanityaddress",
            "key_b64": "KEYBYTES", "ports": ["80,127.0.0.1:8080"],
        }])
        self.assertTrue(any("ED25519-V3:KEYBYTES" in c for c in sock.sent))
        self.assertFalse(any("NEW:BEST" in c for c in sock.sent))


class TestRestartHistorySurvivesTheRestart(unittest.TestCase):
    """Rung 3 ends the container, so the counter cannot live in memory.

    Without this, rung 4 is unreachable: every restart resets the count and
    the ladder restarts Tor every 15 minutes forever against a dead network.
    """

    def test_stamps_round_trip_through_the_state_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            before = _serving_state()
            before.tor_restarts = [990, 995]
            tw.write_state_file(before, False, path=path, now=1000)

            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000)
            self.assertEqual(after.tor_restarts, [990, 995])

    def test_stale_stamps_are_dropped_on_the_way_back_in(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            before = _serving_state()
            before.tor_restarts = [1000]
            tw.write_state_file(before, False, path=path, now=1000)

            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000 + tw.DEGRADED_WINDOW + 1)
            self.assertEqual(after.tor_restarts, [])

    def test_no_state_file_is_a_clean_start(self):
        after = tw.WatchdogState()
        tw.load_restart_history(after, path="/nonexistent/state.json")
        self.assertEqual(after.tor_restarts, [])


class TestStateFile(unittest.TestCase):
    def test_publishes_a_serving_verdict_for_external_consumers(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "nested", "watchdog-state.json")
            tw.write_state_file(_serving_state(), circuit_established=True, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)

            self.assertTrue(payload["serving"])
            self.assertFalse(payload["degraded"])
            self.assertEqual(payload["updated_at"], 1234)

    def test_a_degraded_stack_says_so_rather_than_going_quiet(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            s = _serving_state()
            s.degraded = True
            s.degraded_reason = "network unreachable"
            s.not_serving_since = 900
            tw.write_state_file(s, circuit_established=False, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)

            self.assertFalse(payload["serving"])
            self.assertTrue(payload["degraded"])
            self.assertEqual(payload["degraded_reason"], "network unreachable")

    def test_an_unwritable_path_is_never_fatal(self):
        tw.write_state_file(_serving_state(), True, path="/proc/nope/state.json")


class TestConfiguredTransports(unittest.TestCase):
    def test_reads_the_transport_tor_was_told_to_launch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".torrc", delete=False) as f:
            f.write("UseBridges 1\n"
                    "ClientTransportPlugin snowflake exec /usr/bin/snowflake-client\n"
                    "Bridge snowflake 192.0.2.3:1\n")
            path = f.name
        try:
            self.assertEqual(tw.configured_transports(path), ["snowflake"])
        finally:
            os.unlink(path)

    def test_no_torrc_is_not_an_error(self):
        self.assertEqual(tw.configured_transports("/nonexistent/torrc"), [])


if __name__ == "__main__":
    unittest.main()
