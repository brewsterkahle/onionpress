#!/usr/bin/env python3
"""Turning OnionHeaven off stops the key going to the hub at once, and an
explicit opt-out takes the key back off the hub.

REGISTER_WITH_ONIONHEAVEN is tri-state (tests/test_onionheaven_opt_in.py):

  yes    heartbeats, every one of which carries the onion service key;
  no     nothing that carries the key, and, when this install's record
         says it registered, one signed /unregister for the whole address
         (the hub releases any takeover, marks the rows unregistered and
         deletes its copy of the key), then the record is cleared;
  unset  nothing at all: no heartbeat, no /offline, no /unregister. The
         hub keeps the key and takes the address over while the site is
         down (decided 2026-10-11; see CLAUDE.md, "OnionHeaven Opt-In").

Mac (src/onionpress/onionheaven.py): the heartbeat loop re-reads the
choice every beat and ends when it is not yes, _send_heartbeat refuses
under the hub lock, and maybe_unregister_after_opt_out (from check_status)
sends the /unregister. Linux (linux/onionpress-heartbeat.py): the daemon
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

HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
OTHER_HUB = "hub" + "b" * 53 + ".onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
ELSEWHERE = "op2" + "c" * 53 + ".onion"
NOT_YES = ("no", "", "maybe", None)


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
    a = int.from_bytes(expanded[:32], "little")
    pub = onion_auth._encode_point(onion_auth._scalar_mult(a, onion_auth._B))
    return expanded, pub


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

        def fake_send(a, wp_healthy=True):
            beats.append(a.read_config_value("REGISTER_WITH_ONIONHEAVEN"))
            a._onionheaven_registration_succeeded = True
            if len(beats) == flip_after:
                a.set_choice(new_choice)
            return True

        sleeps = []

        def guarded_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 20:
                raise AssertionError("heartbeat loop did not stop")

        with mock.patch.object(onionheaven, "_send_heartbeat", fake_send), \
                mock.patch.object(onionheaven.time, "sleep", guarded_sleep), \
                mock.patch.object(onionheaven, "unregister_from_onionheaven") as unreg:
            onionheaven._heartbeat_loop(app)
        return beats, unreg

    def test_loop_ends_at_the_first_beat_after_turning_off(self):
        for new_choice in NOT_YES:
            with self.subTest(new_choice=new_choice):
                app = _App(self.tmpdir, "yes")
                beats, unreg = self._run_loop(app, flip_after=2, new_choice=new_choice)
                self.assertEqual(beats, ["yes", "yes"])
                self.assertFalse(app._heartbeat_loop_running)
                self.assertFalse(app._onionheaven_registration_succeeded)
                self.assertFalse(app._onionheaven_heartbeat_succeeded)
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
        app = _App(self.tmpdir, "no")
        app._heartbeat_generation = 3
        app._heartbeat_loop_running = True
        onionheaven._end_heartbeat_loop(app, 2, "no")
        self.assertTrue(app._heartbeat_loop_running)
        onionheaven._end_heartbeat_loop(app, 3, "no")
        self.assertFalse(app._heartbeat_loop_running)

    def test_registration_retries_stop_when_turned_off(self):
        app = _App(self.tmpdir, "yes")
        sends = []

        def failing_send(a, wp_healthy=True):
            sends.append(1)
            a.set_choice("no")
            return False

        with mock.patch.object(onionheaven, "_send_heartbeat", failing_send), \
                mock.patch.object(onionheaven, "_heartbeat_loop") as loop:
            app._onionheaven_registration_in_flight = True
            onionheaven.register_with_onionheaven(app)
        self.assertEqual(sends, [1])
        loop.assert_not_called()
        self.assertFalse(app._onionheaven_registration_in_flight)


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
        app._onionheaven_registration_succeeded = True
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
        self.assertFalse(app._onionheaven_registration_succeeded)
        self.assertFalse(app._onionheaven_opt_out_in_flight)
        # Done: the record is cleared, so nothing more is sent.
        thread, rec = self._opt_out(app, '{"unregistered": true}', now=10 ** 10)
        self.assertIsNone(thread)
        self.assertEqual(rec.calls, [])

    def test_undecided_and_yes_never_unregister(self):
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
        self.assertFalse(getattr(app, "_onionheaven_opt_out_gave_up", False))
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
            app._onionheaven_opt_out_last_attempt = 5
            onionheaven._unregister_after_opt_out(app)
        self.assertEqual(rec.calls, [])
        self.assertTrue(self.saved_record()["registered"])
        # A later "no" acts at once, and this session has not given up.
        self.assertEqual(app._onionheaven_opt_out_last_attempt, 0)
        self.assertFalse(getattr(app, "_onionheaven_opt_out_gave_up", False))

    def test_a_record_for_another_key_is_given_up_for_the_session(self):
        self.registered(address=ELSEWHERE)
        app = _App(self.tmpdir, "no")
        _, rec = self._opt_out(app, '{"unregistered": true}')
        self.assertEqual(rec.calls, [])
        self.assertTrue(app._onionheaven_opt_out_gave_up)
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
        app._onionheaven_registration_succeeded = True
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
        src = _read("src/menubar.py")
        self.assertIn("onionheaven.maybe_unregister_after_opt_out(self)", src)
        # The only place a heartbeat thread starts, and only on yes.
        self.assertEqual(src.count("onionheaven.start_registration_thread(self)"), 1)
        i = src.index("onionheaven.start_registration_thread(self)")
        self.assertIn('if oh_choice == "yes":', src[i - 200:i])
        self.assertNotIn("start_online_notification_thread", src)


# ─── Linux heartbeat daemon ────────────────────────────────────────────

_HB_HOME = tempfile.mkdtemp(prefix="op-hb-optout-test-")


def _load_linux_heartbeat():
    os.makedirs(os.path.join(_HB_HOME, ".onionpress"), exist_ok=True)
    saved = os.environ.get("HOME")
    os.environ["HOME"] = _HB_HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        spec = importlib.util.spec_from_file_location(
            "onionpress_heartbeat_optout_test",
            os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved


class _LinuxCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hb = _load_linux_heartbeat()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_HB_HOME, ignore_errors=True)

    def setUp(self):
        hb = self.hb
        self.tmpdir = tempfile.mkdtemp(prefix="op-hb-optout-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.config_path = os.path.join(self.tmpdir, "config")
        for name, value in (
                ("STATE_PATH", os.path.join(self.tmpdir, "onionheaven-registration.json")),
                ("CONFIG_PATH", self.config_path),
                ("_content_addr", SITE), ("_hc_addr", HC),
                ("_priv_key", EXPANDED), ("_pub_key", PUB),
                ("_current_hub", HUB), ("_hub_conn", None),
                ("_registered", False), ("_running", True),
                ("_pending_offline", False), ("_pending_online", False),
                ("_opt_out_last_attempt", 0.0), ("_opt_out_gave_up", False)):
            p = mock.patch.object(hb, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(hb.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(hb.log, "disabled", True)
        p.start()
        self.addCleanup(p.stop)

    def set_choice(self, choice):
        with open(self.config_path, "w") as f:
            if choice is not None:
                f.write(f"REGISTER_WITH_ONIONHEAVEN={choice}\n")

    def registered(self, hub=OTHER_HUB, address=SITE):
        self.hb._save_state({"registered": True, "content_address": address,
                             "onionheaven_address": hub})


class TestLinuxChoiceAndUnregister(_LinuxCase):

    def test_registration_choice(self):
        hb = self.hb
        self.assertEqual(hb._registration_choice({"REGISTER_WITH_ONIONHEAVEN": " Yes "}), "yes")
        self.assertEqual(hb._registration_choice({"REGISTER_WITH_ONIONHEAVEN": "NO"}), "no")
        for value in ("", "maybe", "y", None):
            cfg = {} if value is None else {"REGISTER_WITH_ONIONHEAVEN": value}
            self.assertIsNone(hb._registration_choice(cfg), value)

    def test_unregister_is_signed_for_the_whole_address(self):
        hb = self.hb
        self.registered()
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(hb.subprocess, "run", rec):
            self.assertTrue(hb.unregister(OTHER_HUB, SITE, HC, EXPANDED, PUB))
        argv, stdin = rec.calls[0]
        self.assertEqual(argv[-1], f"http://{OTHER_HUB}:8083/unregister")
        payload = json.loads(stdin)
        self.assertEqual(payload["healthcheck_address"], "")
        self.assertNotIn("arti_key_pem", payload)
        ok, err = onion_auth.verify_payload(
            SITE, "unregister", "", payload["timestamp"], payload["signature"])
        self.assertTrue(ok, err)
        self.assertFalse(hb._load_state()["registered"])

    def test_a_hub_without_an_entry_counts_as_done(self):
        hb = self.hb
        self.registered()
        rec = _Recorder(lambda argv: '{"error": "Entry not found"}')
        with mock.patch.object(hb.subprocess, "run", rec):
            self.assertTrue(hb.unregister(OTHER_HUB, SITE, HC, EXPANDED, PUB))
        self.assertEqual(len(rec.calls), 1)
        self.assertFalse(hb._load_state()["registered"])

    def test_unregister_is_dropped_when_turned_back_on(self):
        hb = self.hb
        self.set_choice("yes")
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(hb.subprocess, "run", rec):
            self.assertIsNone(hb.unregister(OTHER_HUB, SITE, HC, EXPANDED, PUB,
                                            while_choice="no"))
        self.assertEqual(rec.calls, [])

    def test_a_failed_registration_keeps_an_earlier_record(self):
        hb = self.hb
        self.set_choice("yes")
        self.registered(hub=HUB)
        with mock.patch.object(hb, "sign_and_post", return_value=(False, None)) as post, \
                mock.patch.object(hb, "_is_onionheaven_server", return_value=False):
            self.assertFalse(hb.register(HUB, SITE, HC, EXPANDED, PUB))
        self.assertEqual(post.call_count, 4)
        self.assertTrue(hb._load_state()["registered"])

    def test_registration_retries_stop_when_turned_off(self):
        hb = self.hb
        self.set_choice("yes")

        def refused_then_turned_off(*args, **kwargs):
            self.set_choice("no")
            return False, None

        with mock.patch.object(hb, "sign_and_post", side_effect=refused_then_turned_off) as post, \
                mock.patch.object(hb, "_is_onionheaven_server", return_value=False):
            self.assertFalse(hb.register(HUB, SITE, HC, EXPANDED, PUB))
        self.assertEqual(post.call_count, 1)

    def test_opt_out_only_on_an_explicit_no_with_a_record(self):
        hb = self.hb
        with mock.patch.object(hb, "unregister", return_value=True) as unreg:
            self.registered()
            for choice in ("yes", None):
                self.assertIsNone(hb._maybe_unregister_after_opt_out(choice))
            unreg.assert_not_called()
            hb._save_state({"registered": False, "content_address": SITE})
            self.assertIsNone(hb._maybe_unregister_after_opt_out("no"))
            unreg.assert_not_called()
            self.registered(hub=OTHER_HUB)
            self.assertTrue(hb._maybe_unregister_after_opt_out("no", now=100.0))
            unreg.assert_called_once_with(OTHER_HUB, SITE, HC, EXPANDED, PUB,
                                          while_choice="no")

    def test_opt_out_retries_after_the_interval(self):
        hb = self.hb
        self.registered()
        with mock.patch.object(hb, "unregister", return_value=False) as unreg:
            self.assertFalse(hb._maybe_unregister_after_opt_out("no", now=100.0))
            self.assertIsNone(hb._maybe_unregister_after_opt_out("no", now=160.0))
            self.assertFalse(hb._maybe_unregister_after_opt_out(
                "no", now=100.0 + hb.OPT_OUT_RETRY_SECONDS + 1))
        self.assertEqual(unreg.call_count, 2)

    def test_opt_out_gives_up_on_a_record_for_another_key(self):
        hb = self.hb
        self.registered(address=ELSEWHERE)
        with mock.patch.object(hb, "unregister") as unreg:
            self.assertIsNone(hb._maybe_unregister_after_opt_out("no", now=1.0))
        unreg.assert_not_called()
        self.assertTrue(hb._opt_out_gave_up)

    def test_disable_no_longer_sends_offline(self):
        src = _read("linux/onionpress-heartbeat.py")
        self.assertNotIn("Registration disabled — sending /offline", src)
        # Every read of the choice goes through the one normalizer.
        self.assertEqual(src.count('config.get("REGISTER_WITH_ONIONHEAVEN"'), 1)


class TestLinuxDaemonLoop(_LinuxCase):
    """main() driven cycle by cycle: config per cycle, senders mocked."""

    def _run(self, choices, signals_before_cycle=None, record=False):
        hb = self.hb
        signals_before_cycle = signals_before_cycle or {}
        if record:
            self.registered(hub=HUB)
        self.set_choice(choices[0])
        cycle = [0]

        def next_cycle(seconds):
            # time.sleep(HEARTBEAT_INTERVAL) at the top of each cycle.
            cycle[0] += 1
            if cycle[0] >= len(choices):
                hb._running = False
                return
            self.set_choice(choices[cycle[0]])
            for name in signals_before_cycle.get(cycle[0], ()):
                setattr(hb, name, True)

        def fake_register(hub, *args):
            hb._save_state({"registered": True, "content_address": SITE,
                            "onionheaven_address": hub})
            return True

        class _Inhibitor:
            def start(self):
                pass

        mocks = {}
        with mock.patch.object(hb.signal, "signal"), \
                mock.patch.object(hb.time, "sleep", next_cycle), \
                mock.patch.object(hb, "wait_for_ready", return_value=(SITE, HC, EXPANDED, PUB)), \
                mock.patch.object(hb, "_start_analytics_sharing"), \
                mock.patch.object(hb, "_poll_analytics_opt_in", lambda prev, cfg: prev), \
                mock.patch.object(hb, "SleepInhibitor", _Inhibitor), \
                mock.patch.object(hb, "HAVE_PYSOCKS", True), \
                mock.patch.object(hb, "HubConnection") as conn, \
                mock.patch.object(hb, "register", side_effect=fake_register) as reg, \
                mock.patch.object(hb, "heartbeat") as beat, \
                mock.patch.object(hb, "send_offline") as offline, \
                mock.patch.object(hb, "unregister", return_value=True) as unreg:
            hb.main()
            mocks.update(register=reg, heartbeat=beat, send_offline=offline,
                         unregister=unreg, connection=conn)
        return mocks

    def test_yes_then_no_stops_and_unregisters_without_offline(self):
        # A USR1/USR2 kick arrives in the cycle that sees "no": neither
        # /offline nor /online may go out for it.
        m = self._run(["yes", "yes", "no", "no"],
                      signals_before_cycle={2: ("_pending_offline", "_pending_online")})
        m["register"].assert_called_once()
        self.assertEqual(m["heartbeat"].call_count, 1)
        m["send_offline"].assert_not_called()
        m["unregister"].assert_called_once_with(HUB, SITE, HC, EXPANDED, PUB,
                                                while_choice="no")
        m["connection"].return_value.close.assert_called()

    def test_yes_then_unset_stops_silently(self):
        m = self._run(["yes", "yes", "", ""],
                      signals_before_cycle={2: ("_pending_offline", "_pending_online")})
        self.assertEqual(m["heartbeat"].call_count, 1)
        m["send_offline"].assert_not_called()
        m["unregister"].assert_not_called()

    def test_no_at_startup_with_a_record_unregisters_once(self):
        m = self._run(["no", "no", "no"], record=True)
        m["register"].assert_not_called()
        m["heartbeat"].assert_not_called()
        m["send_offline"].assert_not_called()
        m["unregister"].assert_called_once()
        # No connection to the hub is kept for an install that is not in.
        m["connection"].assert_not_called()

    def test_unset_at_startup_with_a_record_sends_nothing(self):
        m = self._run(["", "", ""], record=True)
        for name in ("register", "heartbeat", "send_offline", "unregister", "connection"):
            m[name].assert_not_called()

    def test_no_then_yes_registers_again(self):
        m = self._run(["no", "yes", "yes"])
        m["unregister"].assert_not_called()  # no record: never registered
        m["register"].assert_called_once()
        self.assertEqual(m["heartbeat"].call_count, 1)


class TestUserFacingText(unittest.TestCase):

    def test_linux_cli_disable_says_what_happens(self):
        src = _read("linux/onionpress")
        self.assertIn("asks the hub to delete its copy of your", src)
        self.assertNotIn(
            'echo "OnionHeaven disabled. Visitors will get an error whenever this machine is off."',
            src)

    def test_mac_texts(self):
        self.assertIn("delete its copy of the key", onionheaven.REGISTRATION_PROMPT_MESSAGE)
        settings = _read("src/onionpress/settings_ui.py")
        self.assertIn("delete its copy of your onion service key", settings)


if __name__ == "__main__":
    unittest.main()
