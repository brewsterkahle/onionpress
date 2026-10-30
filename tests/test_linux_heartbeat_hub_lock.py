#!/usr/bin/env python3
"""The Linux heartbeat holds the hub lock the Mac client does (issue #404).

`linux/onionpress-heartbeat.py` took no lock at all, while the CLI's
`onionheaven-unregister` (a reset, key import, restore or uninstall run by
the launcher) writes the same onionheaven-registration.json under
onionpress.onionheaven's `_hub_lock`; the release paths relied on
`stop_heartbeat_client` running first. Now the heartbeat takes the same
flock (HUB_LOCK_FILE next to the record): every /online and /unregister is
sent under it, /online re-reads the choice inside it, and every
read-modify-write of the record holds it (a writer that waits past
RELEASE_LOCK_TIMEOUT goes on without it, as before).

The census reads the script; the behavioural part holds the flock from
another process and runs the heartbeat's senders against it.
"""

import ast
import fcntl
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_onionheaven_opt_out as optout  # noqa: E402
from onionpress import onionheaven  # noqa: E402

HEARTBEAT = os.path.join(optout.PROJECT_DIR, "linux", "onionpress-heartbeat.py")
LOCKS = ("_hub_lock", "_record_lock")


def _tree():
    with open(HEARTBEAT, encoding="utf-8") as f:
        return ast.parse(f.read(), HEARTBEAT)


def _locked_lines(fn):
    """Line numbers inside a `with _hub_lock(...)` / `_record_lock()` in fn."""
    lines = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", None) in LOCKS for i in node.items):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _calls(fn, name):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == name]


def _functions():
    return [n for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)]


class TestCensus(unittest.TestCase):

    def test_the_lock_file_is_the_macs(self):
        hb = optout._load_linux_heartbeat()
        self.assertEqual(hb.HUB_LOCK_FILE, onionheaven.HUB_LOCK_FILE)
        self.assertEqual(hb.HEARTBEAT_LOCK_TIMEOUT, onionheaven.HEARTBEAT_LOCK_TIMEOUT)
        self.assertEqual(hb.RELEASE_LOCK_TIMEOUT, onionheaven.RELEASE_LOCK_TIMEOUT)

    def test_every_online_and_unregister_is_sent_under_the_lock(self):
        found = 0
        for fn in _functions():
            locked = _locked_lines(fn)
            for call in _calls(fn, "sign_and_post"):
                endpoint = call.args[0]
                if isinstance(endpoint, ast.Constant) and endpoint.value in ("online", "unregister"):
                    found += 1
                    self.assertIn(call.lineno, locked, f"{fn.name}: /{endpoint.value} unlocked")
        self.assertEqual(found, 3, "register, heartbeat, unregister")

    def test_online_re_reads_the_choice_inside_the_lock(self):
        for fn in _functions():
            if fn.name not in ("register", "heartbeat"):
                continue
            locked = _locked_lines(fn)
            reads = [c.lineno for c in _calls(fn, "_registration_choice")]
            self.assertTrue(reads and all(n in locked for n in reads), fn.name)

    def test_every_read_modify_write_of_the_record_is_locked(self):
        for fn in _functions():
            if fn.name in ("_save_state", "_load_state"):
                continue
            saves = _calls(fn, "_save_state")
            loads = _calls(fn, "_load_state")
            if not (saves and loads):
                continue
            decorated = any(getattr(d, "id", None) == "_under_record_lock" for d in fn.decorator_list)
            if decorated:
                continue
            locked = _locked_lines(fn)
            # A load is a read-modify-write's when the next record call
            # after it is a save (a read for a list of hubs is not).
            ordered = sorted([(c.lineno, "load") for c in loads] + [(c.lineno, "save") for c in saves])
            for (line, kind), (_next, after) in zip(ordered, ordered[1:]):
                if kind == "load" and after == "save":
                    self.assertIn(line, locked, f"{fn.name}:{line} unlocked")
            for call in saves:
                self.assertIn(call.lineno, locked, f"{fn.name}:{call.lineno} unlocked")

    def test_save_state_itself_holds_the_lock(self):
        fn = next(f for f in _functions() if f.name == "_save_state")
        locked = _locked_lines(fn)
        self.assertTrue(any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "replace"
                            and n.lineno in locked for n in ast.walk(fn)))


_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


class TestAgainstAHeldLock(optout._LinuxCase):

    def setUp(self):
        super().setUp()
        self.set_choice("yes")
        for name in ("HEARTBEAT_LOCK_TIMEOUT", "RELEASE_LOCK_TIMEOUT"):
            p = mock.patch.object(self.hb, name, 0.5)
            p.start()
            self.addCleanup(p.stop)
        self.posted = []
        p = mock.patch.object(self.hb, "sign_and_post",
                              lambda endpoint, *a, **k: (self.posted.append(endpoint) or
                                                         (True, {"online": True, "registered": True,
                                                                 "unregistered": True})))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.hb, "docker_exec", lambda *a, **k: (True, "200"))
        p.start()
        self.addCleanup(p.stop)

    def _hold(self):
        path = os.path.join(self.tmpdir, self.hb.HUB_LOCK_FILE)
        holder = subprocess.Popen([sys.executable, "-c", _HOLDER, path],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_the_lock_lies_next_to_the_record(self):
        self.assertTrue(self.hb.heartbeat(optout.HUB, optout.SITE, optout.HC,
                                          optout.EXPANDED, optout.PUB))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, self.hb.HUB_LOCK_FILE)))

    def test_a_heartbeat_waits_then_skips(self):
        self._hold()
        start = time.monotonic()
        self.assertFalse(self.hb.heartbeat(optout.HUB, optout.SITE, optout.HC,
                                           optout.EXPANDED, optout.PUB))
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.posted, [])

    def test_an_unregister_is_not_sent_while_another_process_holds_it(self):
        self._hold()
        self.assertFalse(self.hb.unregister(optout.HUB, optout.SITE, optout.HC,
                                            optout.EXPANDED, optout.PUB))
        self.assertEqual(self.posted, [])

    def test_a_record_write_goes_ahead_after_the_wait(self):
        self._hold()
        self.hb._save_state({"registered": False})
        self.assertEqual(self.hb._load_state().get("registered"), False)

    def test_the_heartbeat_drops_an_online_after_an_opt_out(self):
        self.set_choice("no")
        self.assertFalse(self.hb.heartbeat(optout.HUB, optout.SITE, optout.HC,
                                           optout.EXPANDED, optout.PUB))
        self.assertEqual(self.posted, [])

    def test_the_lock_is_released_and_re_entrant(self):
        with self.hb._hub_lock(1):
            with self.hb._hub_lock(1):
                self.hb._save_state({"registered": True})
        fd = os.open(os.path.join(self.tmpdir, self.hb.HUB_LOCK_FILE), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
