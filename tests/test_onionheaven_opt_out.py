#!/usr/bin/env python3
"""Turning OnionHeaven off stops the key going to the hub at once, and an
explicit opt-out takes the key back off the hub.

REGISTER_WITH_ONIONHEAVEN is on by default (tests/test_onionheaven_opt_in.py):

  yes    (or unset) heartbeats, every one of which carries the onion
         service key;
  no     nothing that carries the key, and, when this install's record
         says it registered, one signed /unregister for the whole address
         (the hub releases any takeover, marks the rows unregistered and
         deletes its copy of the key), then the record is cleared;
  other  (a typo) nothing at all: no heartbeat, no /offline, no
         /unregister. The hub keeps the key and takes the address over
         while the site is down.

Mac (src/onionpress/onionheaven.py): the heartbeat loop re-reads the
choice every beat and ends when it is not yes, _send_heartbeat refuses
under the hub lock, and maybe_unregister_after_opt_out (from check_status)
sends the /unregister. Linux runs the same client in the same daemon
since #407 (until then linux/onionpress-heartbeat.py: the daemon
re-reads the choice before anything else each cycle and unregisters on
"no" where it used to send /offline (which made the hub take over a site
that was still running, and kept the key).

Nothing here talks to Docker or Tor: subprocess and the senders are mocked.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import key_manager, onion_auth, onionheaven  # noqa: E402

Client = onionheaven.OnionHeavenClient

HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
OTHER_HUB = "hub" + "b" * 53 + ".onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
ELSEWHERE = "op2" + "c" * 53 + ".onion"
NOT_YES = ("no", "maybe", "y")


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _keypair():
    """A real ed25519 key (RFC 8032 test vector 1): the hub verifies the
    /unregister signature, so these tests do too."""
    seed = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    expanded = bytes(h)
    return expanded, onion_auth.derive_public_key(expanded)


EXPANDED, PUB = _keypair()
SITE = onion_auth.derive_onion_address(PUB)


class _Recorder:
    """Stands in for subprocess.run: records argv and stdin, answers with
    *reply(argv)* on stdout, returncode 0."""

    def __init__(self, reply=lambda argv: ""):
        self.calls = []
        self._reply = reply

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        out = self._reply(argv)
        if not kwargs.get("text"):
            out = out.encode()
        return subprocess.CompletedProcess(
            argv, 0, stdout=out, stderr="" if kwargs.get("text") else b"")

    def urls(self):
        return [argv[-1] for argv, _ in self.calls]


class _App:
    def __init__(self, tmpdir, choice="yes"):
        self.config = {}
        self.set_choice(choice)
        self.onion_address = SITE
        self.healthcheck_address = HC
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = tmpdir
        self.colima_home = os.path.join(tmpdir, "colima")
        self.bin_dir = os.path.join(tmpdir, "bin")
        self.logs = []

    def set_choice(self, choice):
        if choice is None:
            self.config.pop("REGISTER_WITH_ONIONHEAVEN", None)
        else:
            self.config["REGISTER_WITH_ONIONHEAVEN"] = choice

    def read_config_value(self, key, default=""):
        return self.config.get(key, default)

    def log(self, msg):
        self.logs.append(msg)


class _MacCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oh-optout-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        for name, value in (("extract_keys", (EXPANDED, PUB)),):
            p = mock.patch.object(key_manager, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        # Retries back off with time.sleep; never wait in a test. (This is
        # the time module itself, so the tests wait on Events instead.)
        p = mock.patch.object(onionheaven.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", HUB)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(onionheaven, "_check_wordpress_healthy",
                              lambda app: True)
        p.start()
        self.addCleanup(p.stop)

    def record(self, **status):
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json"), "w") as f:
            json.dump(status, f)

    def saved_record(self):
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json")) as f:
            return json.load(f)

    def registered(self, hub=OTHER_HUB, address=SITE):
        self.record(registered=True, content_address=address,
                    onionheaven_address=hub)


# ─── Mac: the heartbeat stops when the choice is not yes ───────────────


class TestMacHeartbeatLoopFollowsTheChoice(_MacCase):

    def _run_loop(self, app, flip_after, new_choice):
        beats = []

        def fake_send(client, wp_healthy=True):
            beats.append(client.app.read_config_value("REGISTER_WITH_ONIONHEAVEN"))
            client.registration_succeeded = True
            if len(beats) == flip_after:
                client.app.set_choice(new_choice)
            return True

        sleeps = []

        def guarded_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 20:
                raise AssertionError("heartbeat loop did not stop")

        with mock.patch.object(Client, "send_heartbeat", fake_send), \
                mock.patch.object(onionheaven.time, "sleep", guarded_sleep), \
                mock.patch.object(Client, "unregister") as unreg:
            onionheaven.client_of(app).heartbeat_loop()
        return beats, unreg

    def test_loop_ends_at_the_first_beat_after_turning_off(self):
        for new_choice in NOT_YES:
            with self.subTest(new_choice=new_choice):
                app = _App(self.tmpdir, "yes")
                beats, unreg = self._run_loop(app, flip_after=2, new_choice=new_choice)
                self.assertEqual(beats, ["yes", "yes"])
                client = onionheaven.client_of(app)
                self.assertFalse(client.heartbeat_loop_running)
                self.assertFalse(client.registration_succeeded)
                self.assertFalse(client.heartbeat_succeeded)
                self.assertTrue(any("heartbeats stopped" in l for l in app.logs), app.logs)
                # The loop never unregisters itself: an explicit no goes
                # through maybe_unregister_after_opt_out, unset never does.
                unreg.assert_not_called()

    def test_loop_sends_nothing_unless_yes_from_the_start(self):
        for choice in NOT_YES:
            with self.subTest(choice=choice):
                app = _App(self.tmpdir, choice)
                beats, _ = self._run_loop(app, flip_after=99, new_choice=choice)
                self.assertEqual(beats, [])

    def test_a_superseded_loop_leaves_the_new_loops_guard(self):
        client = onionheaven.client_of(_App(self.tmpdir, "no"))
        client.heartbeat_generation = 3
        client.heartbeat_loop_running = True
        client._end_heartbeat_loop(2, "no")
        self.assertTrue(client.heartbeat_loop_running)
        client._end_heartbeat_loop(3, "no")
        self.assertFalse(client.heartbeat_loop_running)

    def test_registration_retries_stop_when_turned_off(self):
        app = _App(self.tmpdir, "yes")
        sends = []

        def failing_send(client, wp_healthy=True):
            sends.append(1)
            client.app.set_choice("no")
            return False

        client = onionheaven.client_of(app)
        with mock.patch.object(Client, "send_heartbeat", failing_send), \
                mock.patch.object(Client, "heartbeat_loop") as loop:
            client.registration_in_flight = True
            client.register()
        self.assertEqual(sends, [1])
        loop.assert_not_called()
        self.assertFalse(client.registration_in_flight)


class TestMacNothingGoesToTheHubUnlessYes(_MacCase):
    """_send_heartbeat is the one sender of the key; /offline makes the hub
    take the address over. Neither goes out for no or unset, whoever calls
    (registration, the loop, the wake reclaim, sleep/stop/quit)."""

    def test_heartbeat_and_notifications(self):
        self.registered(hub=HUB)
        for choice in NOT_YES:
            with self.subTest(choice=choice):
                app = _App(self.tmpdir, choice)
                rec = _Recorder(lambda argv: '{"online": true, "offline": true}')
                with mock.patch.object(onionheaven.subprocess, "run", rec):
                    self.assertFalse(onionheaven._send_heartbeat(app))
                    self.assertFalse(onionheaven.notify_onionheaven_online(app))
                    self.assertFalse(onionheaven.notify_onionheaven_offline(app))
                    self.assertFalse(onionheaven._send_onionheaven_notification(
                        app, "online", "online"))
                    onionheaven.register_with_onionheaven(app)
                self.assertEqual(rec.calls, [])

    def test_yes_still_sends(self):
        app = _App(self.tmpdir, "yes")
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven._send_heartbeat(app))
        self.assertEqual(rec.urls(), [f"http://{HUB}:8083/online"])
        self.assertTrue(self.saved_record()["registered"])

    def test_a_heartbeat_the_hub_got_without_an_ack_is_recorded(self):
        # The opt-out keys off the record, so a beat that only
        # _heartbeat_landed saw arrive must write it too.
        app = _App(self.tmpdir, "yes")
        rec = _Recorder(lambda argv: "")
        with mock.patch.object(onionheaven.subprocess, "run", rec), \
                mock.patch.object(onionheaven, "_heartbeat_landed", return_value=True):
            self.assertTrue(onionheaven._send_heartbeat(app))
        status = self.saved_record()
        self.assertTrue(status["registered"])
        self.assertEqual(status["content_address"], SITE)


# ─── Mac: an explicit no unregisters ───────────────────────────────────


class TestMacOptOutUnregisters(_MacCase):

    def _opt_out(self, app, reply, now=None):
        rec = _Recorder(lambda argv: reply)
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            thread = onionheaven.maybe_unregister_after_opt_out(app, now=now)
            if thread is not None:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        return thread, rec

    def test_explicit_no_sends_one_signed_unregister_for_the_whole_address(self):
        self.registered(hub=OTHER_HUB)
        app = _App(self.tmpdir, "no")
        client = onionheaven.client_of(app)
        client.registration_succeeded = True
        thread, rec = self._opt_out(app, '{"unregistered": true}')
        self.assertIsNotNone(thread)
        # To the hub this install registered with, not the configured one.
        self.assertEqual(rec.urls(), [f"http://{OTHER_HUB}:8083/unregister"])
        argv, stdin = rec.calls[0]
        self.assertEqual(argv[1:4], ["exec", "-i", "onionpress-tor"])
        payload = json.loads(stdin)
        self.assertEqual(payload["content_address"], SITE)
        # No healthcheck_address: every registry row of the address goes,
        # and with them the key (the hub keeps it while any row is active).
        self.assertNotIn("healthcheck_address", payload)
        self.assertNotIn("arti_key_pem", payload)
        ok, err = onion_auth.verify_payload(
            SITE, "unregister", "", payload["timestamp"], payload["signature"])
        self.assertTrue(ok, err)
        status = self.saved_record()
        self.assertFalse(status["registered"])
        self.assertEqual(status["onionheaven_address"], OTHER_HUB)
        self.assertFalse(client.registration_succeeded)
        self.assertFalse(client.opt_out_in_flight)
        # Done: the record is cleared, so nothing more is sent.
        thread, rec = self._opt_out(app, '{"unregistered": true}', now=10 ** 10)
        self.assertIsNone(thread)
        self.assertEqual(rec.calls, [])

    def test_unrecognized_and_yes_never_unregister(self):
        self.registered()
        for choice in ("yes", "", "maybe", None):
            with self.subTest(choice=choice):
                app = _App(self.tmpdir, choice)
                thread, rec = self._opt_out(app, '{"unregistered": true}')
                self.assertIsNone(thread)
                self.assertEqual(rec.calls, [])
                self.assertTrue(self.saved_record()["registered"])

    def test_no_without_a_registration_sends_nothing(self):
        # Never registered: the hub must not even learn the address.
        app = _App(self.tmpdir, "no")
        for record in (None, {"registered": False, "content_address": SITE}, ["junk"]):
            with self.subTest(record=record):
                path = os.path.join(self.tmpdir, "onionheaven-registration.json")
                if record is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, "w") as f:
                        json.dump(record, f)
                thread, rec = self._opt_out(app, '{"unregistered": true}')
                self.assertIsNone(thread)
                self.assertEqual(rec.calls, [])

    def test_a_hub_without_an_entry_counts_as_done(self):
        self.registered()
        app = _App(self.tmpdir, "no")
        _, rec = self._opt_out(app, '{"error": "Entry not found"}')
        self.assertEqual(len(rec.calls), 1)
        self.assertFalse(self.saved_record()["registered"])

    def test_a_failed_unregister_is_retried_after_the_interval(self):
        self.registered()
        app = _App(self.tmpdir, "no")
        _, rec = self._opt_out(app, "", now=1000.0)
        self.assertEqual(len(rec.calls), 4)  # four attempts, then give up for now
        self.assertTrue(self.saved_record()["registered"])
        self.assertFalse(onionheaven.client_of(app).opt_out_gave_up)
        thread, rec = self._opt_out(app, "", now=1000.0 + 60)
        self.assertIsNone(thread)
        thread, rec = self._opt_out(
            app, '{"unregistered": true}',
            now=1000.0 + onionheaven.OPT_OUT_RETRY_SECONDS + 1)
        self.assertIsNotNone(thread)
        self.assertFalse(self.saved_record()["registered"])

    def test_a_rejection_is_not_retried_at_once(self):
        self.registered()
        app = _App(self.tmpdir, "no")
        _, rec = self._opt_out(app, '{"error": "Invalid signature"}')
        self.assertEqual(len(rec.calls), 1)
        self.assertTrue(self.saved_record()["registered"])

    def test_turning_it_back_on_drops_the_unregister(self):
        self.registered()
        app = _App(self.tmpdir, "yes")
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertIsNone(onionheaven.unregister_from_onionheaven(
                app, while_choice="no"))
            client = onionheaven.client_of(app)
            client.opt_out_last_attempt = 5
            client._unregister_after_opt_out()
        self.assertEqual(rec.calls, [])
        self.assertTrue(self.saved_record()["registered"])
        # A later "no" acts at once, and this session has not given up.
        self.assertEqual(client.opt_out_last_attempt, 0)
        self.assertFalse(client.opt_out_gave_up)

    def test_a_record_for_another_key_is_given_up_for_the_session(self):
        self.registered(address=ELSEWHERE)
        app = _App(self.tmpdir, "no")
        _, rec = self._opt_out(app, '{"unregistered": true}')
        self.assertEqual(rec.calls, [])
        self.assertTrue(onionheaven.client_of(app).opt_out_gave_up)
        thread, _ = self._opt_out(app, '{"unregistered": true}', now=10 ** 10)
        self.assertIsNone(thread)

    def test_a_heartbeat_in_flight_lands_before_the_unregister(self):
        self.registered()
        app = _App(self.tmpdir, "no")
        held, release = threading.Event(), threading.Event()

        def heartbeat_in_flight():
            with onionheaven._HUB_LOCK:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=heartbeat_in_flight)
        holder.start()
        self.assertTrue(held.wait(10))
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            thread = onionheaven.maybe_unregister_after_opt_out(app)
            threading.Event().wait(0.3)
            self.assertEqual(rec.calls, [], "unregister went out under a heartbeat")
            release.set()
            holder.join(10)
            thread.join(10)
        self.assertEqual(len(rec.calls), 1)

    def test_turning_it_back_on_registers_again(self):
        self.registered(hub=HUB)
        app = _App(self.tmpdir, "no")
        onionheaven.client_of(app).registration_succeeded = True
        self._opt_out(app, '{"unregistered": true}')
        self.assertFalse(self.saved_record()["registered"])
        app.set_choice("yes")
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven._send_heartbeat(app))
        # The record says the hub holds the key again, so a later "no"
        # unregisters again.
        self.assertTrue(self.saved_record()["registered"])

    def test_uninstall_unregisters_the_whole_address_too(self):
        self.registered(hub=HUB)
        app = _App(self.tmpdir, "yes")
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven.unregister_from_onionheaven(app))
        payload = json.loads(rec.calls[0][1])
        self.assertNotIn("healthcheck_address", payload)

    def test_menubar_wiring(self):
        # The status pass is the daemon's (onionpress.daemon, issue #367),
        # through its client's tick (issue #479).
        src = _read("src/onionpress/daemon.py")
        self.assertIn("self.hub_client.tick(tor_bootstrapped and self.hub_registration_allowed(),\n"
                      "                             self._address_known())", src)
        self.assertNotIn("maybe_unregister_after_opt_out(", _read("src/menubar.py"))
        oh = _read("src/onionpress/onionheaven.py")
        tick = oh[oh.index("    def tick("):]
        tick = tick[:tick.index("\n    def ", 1)]
        self.assertIn("self.maybe_unregister_after_opt_out()", tick)
        # The only place a heartbeat thread starts, and only on yes.
        self.assertNotIn("start_registration_thread", src)
        self.assertEqual(oh.count("self.start_registration_thread()"), 1)
        i = tick.index("self.start_registration_thread()")
        self.assertIn('if get_registration_choice(self.app) == "yes":',
                      tick[i - 200:i])
        self.assertNotIn("start_online_notification_thread", src)
        self.assertNotIn("notify_online", src)


if __name__ == "__main__":
    unittest.main()
