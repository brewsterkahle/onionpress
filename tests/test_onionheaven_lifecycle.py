#!/usr/bin/env python3
"""Every path that ends this install's relationship with an OnionHeaven
hub takes the onion service key back off that hub.

A registered site's hub holds a copy of its onion service private key
(every /online carries it). A signed /unregister for the whole address
makes the hub delete it, and only the key itself can sign one, so it has
to go out before the key does:

  uninstall, reset (and scrub's)   onionheaven.release_registration, from
  key import, restore, setup        the menubar or through onionpress.cli
  --from-backup                     onionheaven-unregister (both launchers);
                                    Linux stops its heartbeat client first;
  a hub change                      maintain_registration (Mac, after the
                                    restart ONIONHEAVEN_ADDRESS needs) and the
                                    Linux daemon;
  a key replaced under the daemon   the daemon, with the copy in its memory.

What cannot be taken back (OnionPress not running, the hub not answering,
the key already gone) stays in the registration record's left_on_hubs and
is told to the user. A "released" hold and a cross-process lock keep a
heartbeat from another OnionPress process from putting the key back
between the /unregister and the key's removal. The opt-out's own
/unregister is tests/test_onionheaven_opt_out.py's.

Nothing here talks to Docker or Tor: subprocess and the senders are
mocked, and the launchers' functions run against stubs.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
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

from onionpress import cli, key_manager, onion_auth, onionheaven  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
OTHER_HUB = "hub" + "b" * 53 + ".onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
ELSEWHERE = "op2" + "c" * 53 + ".onion"
NEW_SITE = "op2" + "d" * 53 + ".onion"
RECORD = "onionheaven-registration.json"


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


def _replies(*answers):
    """A reply function answering each /unregister in turn, by hub."""
    answers = dict(answers)

    def reply(argv):
        for hub, answer in answers.items():
            if hub in argv[-1]:
                return answer
        return ""
    return reply


class _App:
    def __init__(self, tmpdir, choice="yes"):
        self.config = {}
        if choice is not None:
            self.config["REGISTER_WITH_ONIONHEAVEN"] = choice
        self.onion_address = SITE
        self.healthcheck_address = HC
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = tmpdir
        self.colima_home = os.path.join(tmpdir, "colima")
        self.bin_dir = os.path.join(tmpdir, "bin")
        self.logs = []

    def read_config_value(self, key, default=""):
        return self.config.get(key, default)

    def log(self, msg):
        self.logs.append(msg)


class _MacCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oh-lifecycle-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.keys = mock.patch.object(key_manager, "extract_keys",
                                      return_value=(EXPANDED, PUB))
        self.keys.start()
        self.addCleanup(self.keys.stop)
        for target, name, value in (
                (onionheaven.time, "sleep", mock.DEFAULT),
                (onionheaven, "ONIONHEAVEN_ADDRESS", HUB),
                (onionheaven, "_check_wordpress_healthy", lambda app: True)):
            p = (mock.patch.object(target, name) if value is mock.DEFAULT
                 else mock.patch.object(target, name, value))
            p.start()
            self.addCleanup(p.stop)

    def record(self, **status):
        with open(os.path.join(self.tmpdir, RECORD), "w") as f:
            json.dump(status, f)

    def saved(self):
        with open(os.path.join(self.tmpdir, RECORD)) as f:
            return json.load(f)

    def left(self):
        return [(e["content_address"], e["onionheaven_address"], e["reason"])
                for e in self.saved().get("left_on_hubs", [])]

    def registered(self, hub=OTHER_HUB, address=SITE, **extra):
        self.record(registered=True, content_address=address,
                    onionheaven_address=hub, **extra)

    def assert_signed_unregister(self, call, address=SITE):
        argv, stdin = call
        payload = json.loads(stdin)
        self.assertEqual(payload["content_address"], address)
        self.assertNotIn("healthcheck_address", payload)
        self.assertNotIn("arti_key_pem", payload)
        ok, err = onion_auth.verify_payload(
            address, "unregister", "", payload["timestamp"], payload["signature"])
        self.assertTrue(ok, err)


# ─── release_registration: reset, key import, restore, uninstall ───────


class TestRelease(_MacCase):

    def release(self, app, reply, **kwargs):
        rec = _Recorder(reply if callable(reply) else (lambda argv: reply))
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            remaining = onionheaven.release_registration(app, "reset", **kwargs)
        return remaining, rec

    def test_nothing_registered_sends_nothing(self):
        for record in (None, {"registered": False, "content_address": SITE}):
            with self.subTest(record=record):
                if record is not None:
                    self.record(**record)
                remaining, rec = self.release(_App(self.tmpdir), '{"unregistered": true}')
                self.assertEqual(remaining, [])
                self.assertEqual(rec.calls, [])

    def test_whatever_the_choice_the_hub_the_record_names_is_asked(self):
        # An explicit end: unset and no unregister too, not only yes.
        for choice in ("yes", "no", "", None):
            with self.subTest(choice=choice):
                self.registered(hub=OTHER_HUB)
                app = _App(self.tmpdir, choice)
                remaining, rec = self.release(app, '{"unregistered": true}')
                self.assertEqual(remaining, [])
                self.assertEqual(rec.urls(), [f"http://{OTHER_HUB}:8083/unregister"])
                self.assert_signed_unregister(rec.calls[0])
                status = self.saved()
                self.assertFalse(status["registered"])
                self.assertEqual(status["onionheaven_address"], OTHER_HUB)
                self.assertEqual(status.get("left_on_hubs", []), [])
                # The released address is held: no /online for it.
                self.assertEqual(status["released"]["content_address"], SITE)

    def test_a_restore_of_the_same_address_keeps_its_registration(self):
        self.registered(hub=HUB)
        remaining, rec = self.release(_App(self.tmpdir), '{"unregistered": true}',
                                      keep_address=SITE)
        self.assertEqual((remaining, rec.calls), ([], []))
        self.assertTrue(self.saved()["registered"])

    def test_without_the_key_nothing_is_sent_and_the_record_says_so(self):
        self.registered(hub=OTHER_HUB)
        app = _App(self.tmpdir)
        with mock.patch.object(key_manager, "extract_keys",
                               side_effect=Exception("tor container not running")):
            remaining, rec = self.release(app, '{"unregistered": true}')
        self.assertEqual(rec.calls, [])
        self.assertEqual(remaining, [{"content_address": SITE,
                                      "onionheaven_address": OTHER_HUB,
                                      "why": "key-unreadable"}])
        status = self.saved()
        self.assertFalse(status["registered"])
        self.assertEqual(self.left(), [(SITE, OTHER_HUB, "reset")])
        self.assertNotIn("released", status)
        self.assertTrue(any("may still hold" in line for line in app.logs), app.logs)
        text = onionheaven.describe_remaining(remaining)
        self.assertIn(OTHER_HUB, text)
        self.assertIn("is OnionPress running?", text)

    def test_a_hub_that_does_not_answer_stays_on_record(self):
        self.registered(hub=OTHER_HUB)
        remaining, rec = self.release(_App(self.tmpdir), "")
        self.assertEqual(len(rec.calls), 4)  # four attempts
        self.assertEqual([i["why"] for i in remaining], ["unanswered"])
        self.assertEqual(self.left(), [(SITE, OTHER_HUB, "reset")])
        self.assertFalse(self.saved()["registered"])

    def test_earlier_hubs_are_asked_too_and_lost_keys_reported(self):
        # Main registration with HUB, an earlier hub that never answered
        # for this address, and a hub holding a key this install lost.
        self.registered(hub=HUB, left_on_hubs=[
            {"content_address": SITE, "onionheaven_address": OTHER_HUB,
             "since": "x", "reason": "hub changed"},
            {"content_address": ELSEWHERE, "onionheaven_address": HUB,
             "since": "x", "reason": "address changed"}])
        remaining, rec = self.release(
            _App(self.tmpdir),
            _replies((HUB, '{"unregistered": true}'),
                     (OTHER_HUB, '{"error": "Entry not found"}')))
        self.assertEqual(sorted(rec.urls()), sorted([
            f"http://{HUB}:8083/unregister", f"http://{OTHER_HUB}:8083/unregister"]))
        for call in rec.calls:
            self.assert_signed_unregister(call)
        self.assertEqual(remaining, [{"content_address": ELSEWHERE,
                                      "onionheaven_address": HUB, "why": "no-key"}])
        self.assertEqual(self.left(), [(ELSEWHERE, HUB, "address changed")])

    def test_a_heartbeat_does_not_put_the_released_key_back(self):
        self.registered(hub=HUB)
        app = _App(self.tmpdir, "yes")
        self.release(app, '{"unregistered": true}')
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertFalse(onionheaven._send_heartbeat(app))
            self.assertFalse(onionheaven._send_onionheaven_notification(
                app, "online", "online"))
        self.assertEqual(rec.calls, [])
        # The hold ends (a cancelled reset): the site registers again.
        status = self.saved()
        status["released"]["until"] = 1
        self.record(**status)
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven._send_heartbeat(app))
        self.assertTrue(self.saved()["registered"])

    def test_uninstall_stops_the_heartbeats_in_this_process(self):
        app = _App(self.tmpdir, "yes")
        onionheaven.end_hub_traffic(app)
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertFalse(onionheaven._send_heartbeat(app))
        self.assertEqual(rec.calls, [])


class TestTheHubLockSpansProcesses(_MacCase):
    """The menubar heartbeats while `onionpress reset` (another process)
    releases the key: the flock orders them."""

    def _hold_lock(self):
        path = os.path.join(self.tmpdir, onionheaven.HUB_LOCK_FILE)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('locked', flush=True)\n"
             "sys.stdin.readline()\n", path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def stop():
            holder.kill()
            holder.communicate(timeout=10)
        self.addCleanup(stop)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_a_heartbeat_skips_while_another_process_releases(self):
        self.registered(hub=HUB)
        app = _App(self.tmpdir, "yes")
        holder = self._hold_lock()
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(onionheaven, "HEARTBEAT_LOCK_TIMEOUT", 0.3), \
                mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertFalse(onionheaven._send_heartbeat(app))
        self.assertEqual(rec.calls, [])
        self.assertTrue(any("another OnionPress process" in line for line in app.logs))
        holder.stdin.write("\n")
        holder.stdin.flush()

    def test_a_release_waits_for_the_other_process(self):
        self.registered(hub=HUB)
        app = _App(self.tmpdir, "no")
        holder = self._hold_lock()
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        result = {}
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            worker = threading.Thread(target=lambda: result.setdefault(
                "remaining", onionheaven.release_registration(app, "reset")))
            worker.start()
            worker.join(0.6)
            self.assertTrue(worker.is_alive())
            self.assertEqual(rec.calls, [], "unregister went out under another process's lock")
            holder.stdin.write("\n")
            holder.stdin.flush()
            worker.join(10)
        self.assertEqual(result["remaining"], [])
        self.assertEqual(len(rec.calls), 1)


# ─── The record: what each hub may hold ────────────────────────────────


class TestRecord(_MacCase):

    def test_a_new_hub_or_address_does_not_forget_the_old_registration(self):
        app = _App(self.tmpdir, "yes")
        rec = _Recorder(lambda argv: '{"online": true}')
        for previous, reason_address in ((OTHER_HUB, SITE), (HUB, ELSEWHERE)):
            with self.subTest(previous=previous):
                self.registered(hub=previous, address=reason_address)
                onionheaven.client_of(app).registration_succeeded = True
                with mock.patch.object(onionheaven.subprocess, "run", rec):
                    self.assertTrue(onionheaven._send_heartbeat(app))
                status = self.saved()
                self.assertEqual((status["content_address"], status["onionheaven_address"]),
                                 (SITE, HUB))
                self.assertTrue(status["registered"])
                self.assertEqual(self.left(), [(reason_address, previous, "superseded")])

    def test_writes_keep_what_other_hubs_hold(self):
        app = _App(self.tmpdir)
        self.registered(hub=HUB, left_on_hubs=[
            {"content_address": ELSEWHERE, "onionheaven_address": OTHER_HUB,
             "since": "x", "reason": "reset"}])
        onionheaven._save_registration_status(app, {"registered": False})
        self.assertEqual(self.left(), [(ELSEWHERE, OTHER_HUB, "reset")])
        self.assertEqual(onionheaven.registrations_left(app), [(ELSEWHERE, OTHER_HUB)])

    def test_unregistering_an_earlier_hub_leaves_the_current_registration(self):
        self.registered(hub=HUB, left_on_hubs=[
            {"content_address": SITE, "onionheaven_address": OTHER_HUB,
             "since": "x", "reason": "hub changed"}])
        app = _App(self.tmpdir)
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven.unregister_from_onionheaven(
                app, content_address=SITE, hub=OTHER_HUB))
            # A pair the record does not name is never sent.
            self.assertIsNone(onionheaven.unregister_from_onionheaven(
                app, content_address=SITE, hub=OTHER_HUB))
        self.assertEqual(rec.urls(), [f"http://{OTHER_HUB}:8083/unregister"])
        status = self.saved()
        self.assertTrue(status["registered"])
        self.assertEqual(status["onionheaven_address"], HUB)
        self.assertEqual(self.left(), [])


# ─── Hub changes and lost keys, every status cycle (Mac) ───────────────


class TestMaintain(_MacCase):

    def maintain(self, app, reply='{"unregistered": true}', now=None):
        rec = _Recorder(reply if callable(reply) else (lambda argv: reply))
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            thread = onionheaven.maintain_registration(app, now=now)
            if thread is not None:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        return thread, rec

    def test_a_hub_change_takes_the_key_back_off_the_old_hub(self):
        # ONIONHEAVEN_ADDRESS (HUB) is what the config says since the
        # restart; the record still names OTHER_HUB.
        for choice in ("yes", "no", None):
            with self.subTest(choice=choice):
                self.registered(hub=OTHER_HUB)
                app = _App(self.tmpdir, choice)
                thread, rec = self.maintain(app)
                self.assertIsNotNone(thread)
                self.assertEqual(rec.urls(), [f"http://{OTHER_HUB}:8083/unregister"])
                self.assert_signed_unregister(rec.calls[0])
                self.assertFalse(self.saved()["registered"])
                self.assertEqual(self.left(), [])
                self.assertTrue(any("hub changed" in line for line in app.logs))

    def test_an_old_hub_that_does_not_answer_is_asked_again_later(self):
        self.registered(hub=OTHER_HUB)
        app = _App(self.tmpdir, "yes")
        _, rec = self.maintain(app, "", now=1000.0)
        self.assertEqual(len(rec.calls), 4)
        self.assertEqual(self.left(), [(SITE, OTHER_HUB, "hub changed")])
        thread, _ = self.maintain(app, "", now=1060.0)
        self.assertIsNone(thread)
        _, rec = self.maintain(app, '{"unregistered": true}',
                               now=1000.0 + onionheaven.OPT_OUT_RETRY_SECONDS + 1)
        self.assertEqual(rec.urls(), [f"http://{OTHER_HUB}:8083/unregister"])
        self.assertEqual(self.left(), [])

    def test_the_hub_this_site_registers_with_is_not_unregistered(self):
        # Back to the earlier hub with OnionHeaven on: the heartbeat puts
        # the key there again, so the entry goes and nothing is sent.
        self.record(registered=False, left_on_hubs=[
            {"content_address": SITE, "onionheaven_address": HUB,
             "since": "x", "reason": "hub changed"}])
        thread, rec = self.maintain(_App(self.tmpdir, "yes"))
        self.assertEqual(rec.calls, [])
        self.assertEqual(self.left(), [])

    def test_a_registration_whose_key_is_gone_is_recorded_not_sent(self):
        # A key import or restore swapped the key without a release.
        self.registered(hub=HUB, address=ELSEWHERE)
        app = _App(self.tmpdir, "yes")
        _, rec = self.maintain(app)
        self.assertEqual(rec.calls, [])
        self.assertFalse(self.saved()["registered"])
        self.assertEqual(self.left(), [(ELSEWHERE, HUB, "address changed")])
        # Reported, once per session.
        app.logs.clear()
        for _ in range(2):
            self.maintain(app)
        self.assertEqual(sum("cannot ask the hub" in line for line in app.logs), 1)

    def test_nothing_to_do_starts_nothing(self):
        self.registered(hub=HUB)
        thread, rec = self.maintain(_App(self.tmpdir, "yes"))
        self.assertIsNone(thread)
        self.assertEqual(rec.calls, [])

    def test_the_hub_address_needs_a_restart(self):
        # The Settings dialog's restart set is config.NEEDS_RESTART, shared
        # with `onionpress config set` (#365).
        from onionpress import config as op_config
        self.assertIn("_NEEDS_RESTART = op_config.NEEDS_RESTART",
                      _read("src/onionpress/settings_ui.py"))
        self.assertIn("ONIONHEAVEN_ADDRESS", op_config.NEEDS_RESTART)

    def test_menubar_wiring(self):
        # The loops are the daemon's (onionpress.daemon, issue #367): the
        # opt-out and the upkeep run from its status pass, in its
        # OnionHeavenClient's tick (issue #479).
        self.assertIn("self.hub_client.tick(", _read("src/onionpress/daemon.py"))
        src = _read("src/onionpress/onionheaven.py")
        i = src.index("            self.maybe_unregister_after_opt_out()")
        self.assertIn("self.maintain_registration()", src[i:i + 200])
        menubar = _read("src/menubar.py")
        for name in ("maybe_unregister_after_opt_out", "maintain_registration",
                     "start_registration_thread", "end_hub_traffic",
                     "unregister_from_onionheaven", "release_registration"):
            self.assertNotIn(f"onionheaven.{name}(", menubar, name)
        # Uninstall: `onionpress uninstall` stops the daemon (its heartbeat
        # holds the key), then the release, then the stop (onionpress.uninstall).
        uninstall = _read("src/onionpress/uninstall.py")
        self.assertLess(uninstall.index("op_daemon.stop_agent("),
                        uninstall.index('release_onionheaven("uninstall"'))
        self.assertLess(uninstall.index('release_onionheaven("uninstall"'),
                        uninstall.index('[launcher, "stop"]'))
        # Not running: the CLI's check asks "hub-holds-key" before "uninstall"
        # (the DELETE confirmation); the menubar asks in that order.
        self.assertLess(uninstall.index('"hub-holds-key", confirmed'),
                        uninstall.index('"uninstall", confirmed'))
        proceed = menubar[menubar.index("def _proceed_with_uninstall(self):"):]
        self.assertLess(proceed.index('["uninstall", "--check", "--json", *flags]'),
                        proceed.index("def do_uninstall():"))
        # The Settings page's restore releases before the key is replaced
        # (the CLI's settings-page-restore).
        cli_src = _read("src/onionpress/cli.py")
        self.assertIn("before_key_change=before_key_change", cli_src)
        self.assertIn('release_onionheaven("restore", keep_address=restored_address', cli_src)
        backup = _read("src/onionpress/backup.py")
        self.assertLess(backup.index("before_key_change(derived_address)"),
                        backup.index("key_manager.write_private_key(priv, pub)"))


# ─── onionpress.cli: the launchers' way in ─────────────────────────────


class _CliCase(_MacCase):

    def setUp(self):
        super().setUp()
        real = resolve_paths
        for p in (mock.patch.object(
                    cli, "resolve_paths",
                    lambda data_dir=None, **kw: real(data_dir=data_dir, app_bundle="")),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *args, reply='{"unregistered": true}'):
        rec = _Recorder(reply if callable(reply) else (lambda argv: reply))
        out = io.StringIO()
        with mock.patch.object(onionheaven.subprocess, "run", rec), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--data-dir", self.tmpdir, *args])
        return rc, out.getvalue(), rec


class TestCliUnregister(_CliCase):

    def test_exit_codes_and_what_the_user_is_told(self):
        cases = (
            # record, reply, exit code, words in the output
            (None, '{"unregistered": true}', 0, None),
            (dict(registered=True, content_address=SITE, onionheaven_address=OTHER_HUB),
             '{"unregistered": true}', 0, "no longer holds the key"),
            (dict(registered=True, content_address=SITE, onionheaven_address=OTHER_HUB),
             "", 1, "make sure OnionPress is running"),
            (dict(registered=True, content_address=ELSEWHERE, onionheaven_address=HUB),
             '{"unregistered": true}', 3, "no longer has the key"),
        )
        for record, reply, code, words in cases:
            with self.subTest(record=record, reply=reply):
                path = os.path.join(self.tmpdir, RECORD)
                if os.path.exists(path):
                    os.remove(path)
                if record is not None:
                    self.record(**record)
                rc, out, rec = self.run_cli("onionheaven-unregister", "--reason", "reset",
                                            reply=reply)
                self.assertEqual(rc, code, out)
                if words is None:
                    self.assertEqual(out, "")
                    self.assertEqual(rec.calls, [])
                else:
                    self.assertIn(words, out)

    def test_keep_address(self):
        self.registered(hub=HUB)
        rc, out, rec = self.run_cli("onionheaven-unregister", "--reason", "restore",
                                    "--keep-address", SITE)
        self.assertEqual((rc, out, rec.calls), (0, "", []))
        rc, _, _ = self.run_cli("onionheaven-unregister", "--keep-address", "x.onion")
        self.assertEqual(rc, 2)

    def test_registrations_listing(self):
        self.registered(hub=HUB, left_on_hubs=[
            {"content_address": ELSEWHERE, "onionheaven_address": OTHER_HUB,
             "since": "x", "reason": "reset"}])
        rc, out, rec = self.run_cli("onionheaven-registrations")
        self.assertEqual(rc, 0)
        self.assertEqual(out.split("\n"), [f"{SITE} {HUB}", f"{ELSEWHERE} {OTHER_HUB}", ""])
        self.assertEqual(rec.calls, [])


class TestCliResetAndRestore(_CliCase):

    def _cli(self):
        obj = cli.OnionPressCLI.__new__(cli.OnionPressCLI)
        obj.paths = resolve_paths(data_dir=self.tmpdir, app_bundle="")
        obj.log = lambda msg: None
        obj.order = []
        obj.containers = mock.Mock()
        obj.containers.stop.side_effect = lambda: obj.order.append("stop")
        obj.docker = mock.Mock()
        obj.docker.run.return_value = mock.Mock(ok=True, output="")
        return obj

    def test_reset_releases_first_and_keeps_the_record_if_a_hub_may_hold_a_key(self):
        for remaining, kept in (([], False),
                                ([{"content_address": SITE, "onionheaven_address": HUB,
                                   "why": "unanswered"}], True)):
            with self.subTest(remaining=remaining):
                self.registered(hub=HUB)
                obj = self._cli()

                def release(reason, **kwargs):
                    obj.order.append(("release", reason))
                    return remaining

                with mock.patch.object(cli, "release_onionheaven", release), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(obj.cmd_reset(yes=True), 0)
                self.assertEqual(obj.order, [("release", "reset"), "stop"])
                self.assertEqual(os.path.exists(os.path.join(self.tmpdir, RECORD)), kept)

    def test_reset_asks_before_going_on_without_taking_the_key_back(self):
        self.registered(hub=HUB)
        obj = self._cli()
        left = [{"content_address": SITE, "onionheaven_address": HUB,
                 "why": "key-unreadable"}]
        with mock.patch.object(cli, "release_onionheaven", return_value=left), \
                mock.patch("builtins.input", side_effect=["", "n"]), \
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(obj.cmd_reset(yes=False), 1)
        self.assertEqual(obj.order, [])

    def test_reset_refuses_without_the_confirmation_off_a_terminal(self):
        """Not at a terminal (a UI, a script), `reset` needs --confirmed
        reset, and --confirmed hub-holds-key when the hub keeps the key
        (onionpress.confirm); --check says so and changes nothing."""
        self.registered(hub=HUB)
        obj = self._cli()
        obj.docker.container_running.return_value = False
        left = [{"content_address": SITE, "onionheaven_address": HUB,
                 "why": "key-unreadable"}]
        out = io.StringIO()
        with mock.patch.object(cli, "release_onionheaven", return_value=left) as release, \
                mock.patch.object(sys, "stdin", io.StringIO()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(obj.cmd_reset(check=True, json_output=True), 4)
            first = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual(obj.cmd_reset(check=True, json_output=True,
                                           confirmed=frozenset({"hub-holds-key"})), 4)
            second = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual(obj.cmd_reset(check=True, json_output=True,
                                           confirmed=frozenset({"hub-holds-key", "reset"})), 0)
            release.assert_not_called()
            self.assertEqual(obj.cmd_reset(json_output=True), 3)
            release.assert_not_called()
            self.assertEqual(obj.cmd_reset(json_output=True, confirmed=frozenset({"reset"})), 3)
            release.assert_called_once()
            self.assertEqual(obj.order, [])
            self.assertEqual(obj.cmd_reset(json_output=True,
                                           confirmed=frozenset({"reset", "hub-holds-key"})), 0)
        self.assertEqual(first["confirm"], "hub-holds-key")
        self.assertEqual(first["hubs"], [{"content_address": SITE, "onionheaven_address": HUB}])
        self.assertEqual(second["confirm"], "reset")
        self.assertEqual(obj.order, ["stop"])

    def test_restore_releases_the_old_key_but_not_the_restored_one(self):
        obj = self._cli()
        obj.containers.stop.side_effect = RuntimeError("stop here")
        calls = []
        with mock.patch("onionpress.backup.read_backup_metadata", return_value={}), \
                mock.patch("onionpress.backup.prepare_install_from_backup",
                           return_value=(self.tmpdir, {"onion_address": NEW_SITE})), \
                mock.patch("onionpress.backup.staged_onion_key_address",
                           return_value=NEW_SITE), \
                mock.patch.object(cli, "release_onionheaven",
                                  lambda reason, **kw: calls.append((reason, kw)) or []):
            self.assertEqual(obj.cmd_restore("pw", "/nonexistent.zip"), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "restore")
        self.assertEqual(calls[0][1]["keep_address"], NEW_SITE)
        obj.containers.stop.assert_called_once()

    def test_a_restore_without_a_key_releases_nothing(self):
        """A backup without an onion service key (the Mac Settings page's)
        keeps this install's key: no hub is asked to let go of it."""
        obj = self._cli()
        obj.containers.stop.side_effect = RuntimeError("stop here")
        calls = []
        with mock.patch("onionpress.backup.read_backup_metadata", return_value={}), \
                mock.patch("onionpress.backup.prepare_install_from_backup",
                           return_value=(self.tmpdir, {"onion_address": SITE})), \
                mock.patch("onionpress.backup.staged_onion_key_address",
                           return_value=""), \
                mock.patch.object(cli, "release_onionheaven",
                                  lambda reason, **kw: calls.append((reason, kw)) or []):
            self.assertEqual(obj.cmd_restore("pw", "/nonexistent.zip"), 1)
        self.assertEqual(calls, [])
        obj.containers.stop.assert_called_once()


# ─── The launchers ─────────────────────────────────────────────────────


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


def _case_branch(text, label):
    """The text of `<label>)` in the launcher's main case, up to its ;;."""
    start = text.index(f"\n        {label})\n")
    return text[start:text.index("\n            ;;\n", start)]


class TestReleaseFunctionInTheLaunchers(unittest.TestCase):
    """onionheaven_release, run against stubs: the heartbeat client stops
    before anything is sent (Linux), the CLI's answer decides whether the
    user is asked, and a "no" stops the caller."""

    def _run(self, rel, rc, answer="", yes=False, keep=""):
        text = _read(rel)
        tmp = tempfile.mkdtemp(prefix="oh-release-sh-")
        self.addCleanup(shutil.rmtree, tmp, True)
        calls = os.path.join(tmp, "calls")
        funcs = [_shell_function(text, "onionheaven_release")]
        if "linux" in rel:
            funcs[:0] = [_shell_function(text, "stop_heartbeat_client"),
                         _shell_function(text, "start_heartbeat_client")]
        script = (
            "set -e\n"
            "log() { :; }\n"
            'systemctl() { echo "systemctl $*" >> "$CALLS"; }\n'
            'sudo() { echo "sudo" >> "$CALLS"; "$@"; }\n'
            'op_py() { echo "cli $*" >> "$CALLS"; return "$RC"; }\n'
            'bundled_python() { echo "cli $*" >> "$CALLS"; return "$RC"; }\n'
            + "\n".join(funcs) + "\n"
            'if onionheaven_release reset "$KEEP"; then echo GO; else echo STOP; fi\n'
            'echo "rc=$ONIONHEAVEN_RELEASE_RC"\n')
        env = {**os.environ, "CALLS": calls, "RC": str(rc), "KEEP": keep,
               "LOG_FILE": os.path.join(tmp, "log"),
               "ONIONPRESS_YES": "true" if yes else ""}
        out = subprocess.run(["bash", "-c", script], input=answer + "\n",
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(calls) as f:
            return out.stdout, f.read().splitlines()

    def test_both_launchers(self):
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            with self.subTest(rel=rel):
                out, calls = self._run(rel, 0)
                self.assertIn("GO", out)
                self.assertIn("rc=0", out)
                cli_calls = [c for c in calls if c.startswith("cli ")]
                self.assertEqual(cli_calls, [
                    "cli -m onionpress.cli onionheaven-unregister --reason reset"
                    if "MacOS" in rel else "cli onionheaven-unregister --reason reset"])
                out, calls = self._run(rel, 0, keep=SITE)
                self.assertTrue(calls[-1].endswith(f"--keep-address {SITE}"), calls)
                # Nothing left the user can do something about: no question.
                out, _ = self._run(rel, 3)
                self.assertIn("GO", out)
                self.assertNotIn("Continue anyway", out)
                # Something left: asked, and "no" stops the caller.
                out, calls = self._run(rel, 1, answer="n")
                self.assertIn("Continue anyway", out)
                self.assertIn("STOP", out)
                out, _ = self._run(rel, 1, answer="y")
                self.assertIn("GO", out)
                out, _ = self._run(rel, 1, yes=True)
                self.assertIn("GO", out)
                self.assertNotIn("Continue anyway", out)
                # The command itself failed (an older lib/ without it, a
                # crash): a key may be left, so the user is asked too.
                out, _ = self._run(rel, 2, answer="n")
                self.assertIn("STOP", out)

    def test_linux_stops_the_heartbeat_client_before_the_request(self):
        _, calls = self._run("linux/onionpress", 0)
        first_cli = next(i for i, c in enumerate(calls) if c.startswith("cli "))
        stops = [i for i, c in enumerate(calls) if " stop onionpress-heartbeat" in c]
        # The user unit and the legacy system unit.
        self.assertIn("systemctl --user stop onionpress-heartbeat", calls)
        self.assertIn("systemctl stop onionpress-heartbeat", calls)
        self.assertTrue(stops and max(stops) < first_cli, calls)
        # A "no" brings it back.
        _, calls = self._run("linux/onionpress", 1, answer="n")
        self.assertIn("systemctl --user start onionpress-heartbeat", calls)


class TestLauncherPathsReleaseFirst(unittest.TestCase):
    """Each path asks the hubs before the key goes."""

    def assert_before(self, text, first, then, where):
        self.assertIn(first, text, where)
        self.assertIn(then, text, where)
        self.assertLess(text.index(first), text.index(then), where)

    def test_linux_uninstall_sends_the_real_unregister(self):
        text = _read("linux/onionpress")
        # The old one: unsigned, to a port nothing listens on in the tor
        # container, before the heartbeat client was stopped.
        self.assertNotIn("127.0.0.1:9077/unregister", text)
        branch = _case_branch(text, "uninstall")
        self.assert_before(branch, "onionheaven_release uninstall",
                           "Stopping user services", "uninstall")
        self.assert_before(branch, "onionheaven_release uninstall",
                           'docker volume rm "$vol"', "uninstall")
        self.assert_before(branch, "create_backup", "onionheaven_release uninstall",
                           "uninstall: the backup first")
        self.assertIn("ONIONHEAVEN_RELEASE_RC", branch[branch.index("has been uninstalled"):])

    def test_resets_keep_the_record_when_a_hub_may_hold_a_key(self):
        # The Mac launcher's reset is the CLI's (cmd_reset, below).
        mac = _read("app/MacOS/onionpress")
        self.assertRegex(_case_branch(mac, "reset"),
                         r'bundled_python -m onionpress\.cli --data-dir "\$DATA_DIR" reset ')
        for rel, label in (("linux/onionpress", "reset"),
                           ("app/MacOS/onionpress", "scrub")):
            with self.subTest(rel=rel, label=label):
                branch = _case_branch(_read(rel), label)
                self.assert_before(branch, f"onionheaven_release {label}",
                                   "stop_containers", label)
                self.assert_before(branch, f"onionheaven_release {label}",
                                   'docker volume rm "$vol"', label)
                self.assertIn('if [ "$ONIONHEAVEN_RELEASE_RC" -eq 0 ]; then\n'
                              '                rm -f "$DATA_DIR/onionheaven-registration.json"',
                              branch)
                self.assertEqual(branch.count("onionheaven-registration.json"), 1)

    def test_cli_reset_keeps_the_record_too(self):
        src = _read("src/onionpress/cli.py")
        body = src[src.index("    def cmd_reset("):src.index("    # -- PID lock --")]
        self.assertNotIn('"onionheaven-registration.json"', body)
        self.assertIn("*([] if remaining else [REGISTRATION_RECORD])", body)

    def test_key_imports(self):
        for rel in ("linux/onionpress", "app/MacOS/onionpress"):
            with self.subTest(rel=rel):
                text = _read(rel)
                # Every import replaces the key in replace_onion_key: the
                # hubs are asked once, after the current key was kept and
                # before anything stops or is replaced.
                body = _shell_function(text, "replace_onion_key")
                self.assertEqual(body.count("onionheaven_release key-import"), 1)
                self.assert_before(body, "keep-current",
                                   "onionheaven_release key-import", rel)
                self.assert_before(body, "onionheaven_release key-import",
                                   "stop_containers", rel)
                self.assert_before(body, "onionheaven_release key-import",
                                   "touch \"$DATA_DIR/.import-key-pending\"", rel)
                body = _shell_function(text, "import_key")
                self.assertNotIn("onionheaven_release", body)
                self.assert_before(body, 'read -r confirm', "replace_onion_key", rel)

    def test_linux_settings_page_key_changes(self):
        text = _read("linux/onionpress")
        # The page's key import is refused on Linux by the shared handler,
        # and generate-vanity is gone (tests/test_settings_page_onion_key.py):
        # no key change from there reaches the hubs.
        handler = _read("src/onionpress/handle_action.py")
        branch = handler[handler.index("def _import_key_file("):handler.index("def _import_key(")]
        self.assertIn('if host.os_name != "macos":', branch)
        self.assertIn("IMPORT_REFUSED_LINUX_MSG", branch)
        self.assertNotIn("generate-vanity", handler)
        self.assertNotIn("                generate-vanity)\n", text)
        body = _shell_function(text, "restore_backup")
        self.assert_before(body, 'onionheaven_release restore "$onion_address"',
                           'touch "$DATA_DIR/.import-key-pending"', "restore_backup")
        self.assert_before(body, "op_py backup-address",
                           'onionheaven_release restore "$onion_address"', "restore_backup")

    def test_restores_and_setup_from_backup(self):
        linux = _read("linux/onionpress")
        branch = _case_branch(linux, "restore")
        # `restore --check` runs the CLI before anything else (it changes
        # nothing, onionpress.confirm); the real run stops the heartbeat
        # client first (it holds the key) and starts it again after.
        check = branch.index('if [ "$restore_check" = yes ]')
        real = branch[branch.index("exit $?", check):]
        self.assertNotIn("heartbeat_client", branch[:check])
        self.assert_before(real, "stop_heartbeat_client", "op_py restore", "restore")
        self.assert_before(real, "op_py restore", "start_heartbeat_client", "restore")
        for rel in ("linux/onionpress", "app/MacOS/onionpress"):
            with self.subTest(rel=rel):
                branch = _case_branch(_read(rel), "setup")
                self.assert_before(branch, "onionheaven_release restore",
                                   'touch "$DATA_DIR/.import-key-pending"', rel)
                self.assertIn('$(basename "$restored_key_dir")', branch)


# ─── Linux heartbeat daemon ────────────────────────────────────────────

_HB_HOME = tempfile.mkdtemp(prefix="op-hb-lifecycle-test-")


def _load_linux_heartbeat():
    os.makedirs(os.path.join(_HB_HOME, ".onionpress"), exist_ok=True)
    saved = os.environ.get("HOME")
    os.environ["HOME"] = _HB_HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        spec = importlib.util.spec_from_file_location(
            "onionpress_heartbeat_lifecycle_test",
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
        self.tmpdir = tempfile.mkdtemp(prefix="op-hb-lifecycle-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.config_path = os.path.join(self.tmpdir, "config")
        for name, value in (
                ("STATE_PATH", os.path.join(self.tmpdir, RECORD)),
                ("CONFIG_PATH", self.config_path),
                ("_content_addr", SITE), ("_hc_addr", HC),
                ("_priv_key", EXPANDED), ("_pub_key", PUB),
                ("_current_hub", HUB), ("_hub_conn", None),
                ("_registered", False), ("_running", True),
                ("_pending_offline", False), ("_pending_online", False),
                ("_opt_out_last_attempt", 0.0), ("_opt_out_gave_up", False),
                ("_left_last_attempt", 0.0), ("_left_reported", set())):
            p = mock.patch.object(hb, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("sleep",):
            p = mock.patch.object(hb.time, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(hb.log, "disabled", True)
        p.start()
        self.addCleanup(p.stop)

    def write_config(self, **values):
        with open(self.config_path, "w") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")

    def state(self, **values):
        with open(self.hb.STATE_PATH, "w") as f:
            json.dump(values, f)

    def left(self):
        return [(e["content_address"], e["onionheaven_address"], e["reason"])
                for e in self.hb._load_state().get("left_on_hubs", [])]


class TestLinuxRecord(_LinuxCase):

    def test_registration_keeps_what_earlier_hubs_hold(self):
        hb = self.hb
        self.write_config(REGISTER_WITH_ONIONHEAVEN="yes")
        for previous, address in ((OTHER_HUB, SITE), (HUB, ELSEWHERE)):
            with self.subTest(previous=previous):
                self.state(registered=True, content_address=address,
                           onionheaven_address=previous)
                with mock.patch.object(hb, "sign_and_post",
                                       return_value=(True, {"registered": True})), \
                        mock.patch.object(hb, "_is_onionheaven_server", return_value=False):
                    self.assertTrue(hb.register(HUB, SITE, HC, EXPANDED, PUB))
                state = hb._load_state()
                self.assertEqual((state["content_address"], state["onionheaven_address"]),
                                 (SITE, HUB))
                self.assertEqual(self.left(), [(address, previous, "superseded")])

    def test_unregistering_an_earlier_hub_keeps_the_registration(self):
        hb = self.hb
        self.state(registered=True, content_address=SITE, onionheaven_address=HUB,
                   left_on_hubs=[{"content_address": SITE, "onionheaven_address": OTHER_HUB,
                                  "since": "x", "reason": "hub changed"}])
        rec = _Recorder(lambda argv: '{"unregistered": true}')
        with mock.patch.object(hb.subprocess, "run", rec):
            self.assertTrue(hb.unregister(OTHER_HUB, SITE, HC, EXPANDED, PUB))
        state = hb._load_state()
        self.assertTrue(state["registered"])
        self.assertEqual(self.left(), [])

    def test_a_released_key_is_not_sent_again(self):
        hb = self.hb
        self.write_config(REGISTER_WITH_ONIONHEAVEN="yes")
        self.state(registered=False, released={"content_address": SITE,
                                               "until": hb.time.time() + 600})
        with mock.patch.object(hb, "sign_and_post") as post, \
                mock.patch.object(hb, "docker_exec") as dexec, \
                mock.patch.object(hb, "_is_onionheaven_server", return_value=False):
            self.assertFalse(hb.heartbeat(HUB, SITE, HC, EXPANDED, PUB))
            self.assertFalse(hb.register(HUB, SITE, HC, EXPANDED, PUB))
        post.assert_not_called()
        dexec.assert_not_called()

    def test_a_registration_for_another_address_is_recorded_as_lost(self):
        hb = self.hb
        self.state(registered=True, content_address=ELSEWHERE, onionheaven_address=HUB)
        hb._reconcile_record()
        self.assertFalse(hb._load_state()["registered"])
        self.assertEqual(self.left(), [(ELSEWHERE, HUB, "address changed")])

    def test_earlier_hubs_are_asked_again_whatever_the_choice(self):
        hb = self.hb
        self.state(registered=True, content_address=SITE, onionheaven_address=HUB,
                   left_on_hubs=[
                       {"content_address": SITE, "onionheaven_address": OTHER_HUB,
                        "since": "x", "reason": "hub changed"},
                       {"content_address": ELSEWHERE, "onionheaven_address": OTHER_HUB,
                        "since": "x", "reason": "reset"}])
        with mock.patch.object(hb, "unregister", return_value=False) as unreg:
            self.assertEqual(hb._maybe_release_left_hubs(None, now=100.0), [False])
            self.assertIsNone(hb._maybe_release_left_hubs(None, now=160.0))
            hb._maybe_release_left_hubs("no", now=100.0 + hb.OPT_OUT_RETRY_SECONDS + 1)
        self.assertEqual(unreg.call_args_list, [
            mock.call(OTHER_HUB, SITE, HC, EXPANDED, PUB)] * 2)

    def test_the_hub_this_site_registers_with_is_dropped_not_unregistered(self):
        hb = self.hb
        self.state(registered=False, left_on_hubs=[
            {"content_address": SITE, "onionheaven_address": HUB,
             "since": "x", "reason": "hub changed"}])
        with mock.patch.object(hb, "unregister") as unreg:
            self.assertIsNone(hb._maybe_release_left_hubs("yes", now=1.0))
        unreg.assert_not_called()
        self.assertEqual(self.left(), [])


class TestLinuxDaemonLoop(_LinuxCase):
    """main() driven cycle by cycle: config per cycle, senders mocked."""

    def _run(self, configs, served=None):
        hb = self.hb
        self.write_config(**configs[0])
        cycle = [0]

        def next_cycle(seconds):
            cycle[0] += 1
            if cycle[0] >= len(configs):
                hb._running = False
                return
            self.write_config(**configs[cycle[0]])

        def fake_register(hub, *args):
            hb._record_registered(hub, SITE)
            return True

        def key_replaced():
            return (served or {}).get(cycle[0])

        class _Inhibitor:
            def start(self):
                pass

        mocks = {}
        with mock.patch.object(hb.signal, "signal"), \
                mock.patch.object(hb.time, "sleep", next_cycle), \
                mock.patch.object(hb, "wait_for_ready", return_value=(SITE, HC, EXPANDED, PUB)), \
                mock.patch.object(hb, "_start_analytics_sharing"), \
                mock.patch.object(hb, "_start_reachability_loop"), \
                mock.patch.object(hb, "_poll_analytics_opt_in", lambda prev, cfg: prev), \
                mock.patch.object(hb, "_poll_upload_trigger", lambda cfg: False), \
                mock.patch.object(hb, "SleepInhibitor", _Inhibitor), \
                mock.patch.object(hb, "HAVE_PYSOCKS", False), \
                mock.patch.object(hb, "_key_replaced", side_effect=key_replaced) as kr, \
                mock.patch.object(hb, "register", side_effect=fake_register) as reg, \
                mock.patch.object(hb, "heartbeat") as beat, \
                mock.patch.object(hb, "send_offline") as offline, \
                mock.patch.object(hb, "unregister", return_value=False) as unreg:
            hb.main()
            mocks.update(register=reg, heartbeat=beat, send_offline=offline,
                         unregister=unreg, key_replaced=kr, cycles=cycle[0])
        return mocks

    def test_a_hub_change_that_is_not_answered_stays_on_record(self):
        m = self._run([
            dict(REGISTER_WITH_ONIONHEAVEN="yes", ONIONHEAVEN_ADDRESS=HUB),
            dict(REGISTER_WITH_ONIONHEAVEN="yes", ONIONHEAVEN_ADDRESS=OTHER_HUB),
            dict(REGISTER_WITH_ONIONHEAVEN="yes", ONIONHEAVEN_ADDRESS=OTHER_HUB)])
        m["unregister"].assert_any_call(HUB, SITE, HC, EXPANDED, PUB)
        self.assertEqual(self.left(), [(SITE, HUB, "hub changed")])
        state = self.hb._load_state()
        self.assertEqual((state["onionheaven_address"], state["registered"]),
                         (OTHER_HUB, True))

    def test_a_key_replaced_under_the_daemon_is_taken_back_with_the_old_copy(self):
        # Cycle 2: Tor serves a new address (a key import or restore while
        # the daemon ran). The daemon asks the hub to delete the old key,
        # signed with the copy in its memory, and exits for systemd to
        # start it again with the new one: no more beats, no /offline.
        m = self._run([dict(REGISTER_WITH_ONIONHEAVEN="yes")] * 5,
                      served={2: NEW_SITE})
        m["unregister"].assert_called_once_with(HUB, SITE, HC, EXPANDED, PUB)
        self.assertEqual(m["heartbeat"].call_count, 1)
        m["send_offline"].assert_not_called()
        self.assertEqual(m["cycles"], 2)
        # Not answered: recorded, the key is gone after the exit.
        self.assertFalse(self.hb._load_state()["registered"])
        self.assertEqual(self.left(), [(SITE, HUB, "key replaced")])

    def test_nothing_registered_reads_nothing_from_docker(self):
        m = self._run([dict(REGISTER_WITH_ONIONHEAVEN="no")] * 3)
        m["key_replaced"].assert_not_called()
        m["unregister"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
