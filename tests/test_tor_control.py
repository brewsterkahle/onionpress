#!/usr/bin/env python3
"""tor_control.py: the tor image's one Tor control-port client.

The image held five: tor-watchdog.py's, two classes in the queue manager,
onionheaven-tor-manager.sh (xxd + nc) and the inline sh and Python the
takeover worker and the farm's worker verbs ran. Now the watchdog, the
queue manager, the takeover worker and the farm's `bootstrap-phase` /
`onions-detached` verbs all go through tor_control.Controller, and every
takeover or release inside a worker goes through its queue manager.

The client is run against a fake control port on 127.0.0.1 (a thread that
speaks the reply grammar of control-spec.txt section 2.3). No Tor.
"""

import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, TOR_DIR)

import tor_control  # noqa: E402

COOKIE = bytes(range(32))
SID = "a" * 56


class FakeTor:
    """A control port: answers AUTHENTICATE (the cookie COOKIE) and a few
    commands; `events` lines are pushed after SETEVENTS."""

    def __init__(self, replies=None, events=()):
        self.replies = {
            "GETINFO status/bootstrap-phase":
                "250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY=\"Done\"\r\n250 OK\r\n",
            "GETINFO onions/detached":
                f"250+onions/detached=\r\n{SID}\r\n{'b' * 56}\r\n.\r\n250 OK\r\n",
            "GETINFO onions/current": "250-onions/current=\r\n250 OK\r\n",
            "GETINFO nothing": "552 Unrecognized key \"nothing\"\r\n",
            "DEL_ONION " + SID: "250 OK\r\n",
            "SETEVENTS HS_DESC": "250 OK\r\n",
        }
        self.replies.update(replies or {})
        self.events = list(events)
        self.received = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        with conn:
            f = conn.makefile("rb")
            for raw in f:
                line = raw.decode().rstrip("\r\n")
                self.received.append(line)
                if line.startswith("AUTHENTICATE "):
                    ok = line == "AUTHENTICATE " + COOKIE.hex()
                    conn.sendall(b"250 OK\r\n" if ok else b"515 Authentication failed\r\n")
                elif line.startswith("ADD_ONION "):
                    conn.sendall(f"250-ServiceID={SID}\r\n250 OK\r\n".encode())
                elif line == "QUIT":
                    return
                else:
                    conn.sendall(self.replies.get(line, "510 Unrecognized command\r\n").encode())
                    if line.startswith("SETEVENTS"):
                        for event in self.events:
                            conn.sendall(event.encode())

    def close(self):
        self.server.close()


class ControllerTest(unittest.TestCase):

    def setUp(self):
        self.tor = FakeTor(events=["650 HS_DESC UPLOADED " + SID + " x y\r\n",
                                   "650 HS_DESC FAILED " + "b" * 56 + " x y\r\n"])
        self.addCleanup(self.tor.close)

    def connect(self, **kwargs):
        ctl = tor_control.Controller.connect(port=self.tor.port, cookie=COOKIE, **kwargs)
        self.addCleanup(ctl.close)
        return ctl

    def test_authenticates_with_the_cookie_in_hex(self):
        self.connect()
        self.assertIn("AUTHENTICATE " + COOKIE.hex(), self.tor.received)

    def test_a_wrong_cookie_is_refused(self):
        with self.assertRaises(ConnectionError):
            tor_control.Controller.connect(port=self.tor.port, cookie=b"x" * 32)

    def test_the_cookie_is_read_from_its_file(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(COOKIE)
            f.flush()
            tor_control.Controller.connect(port=self.tor.port, cookie_path=f.name).close()

    def test_nothing_listening_is_an_oserror(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()   # bound, never listening: refused
        with self.assertRaises(OSError):
            tor_control.Controller.connect(port=port, cookie=COOKIE, timeout=1)

    def test_a_data_reply_is_read_whole(self):
        ctl = self.connect()
        reply = ctl.command("GETINFO onions/detached")
        self.assertTrue(reply.endswith("250 OK\r\n"), reply)
        self.assertEqual(ctl.getinfo("onions/detached"), f"{SID}\n{'b' * 56}")
        self.assertEqual(ctl.getinfo("onions/current"), "")
        self.assertIsNone(ctl.getinfo("nothing"))
        self.assertEqual(ctl.onions(), {SID, "b" * 56})
        self.assertIn("PROGRESS=100", ctl.getinfo("status/bootstrap-phase"))

    def test_a_data_line_that_looks_like_an_end_line_does_not_end_it(self):
        # The old readers stopped at any line whose fourth character was a
        # space, inside a data block too.
        tor = FakeTor({"GETINFO config-text": "250+config-text=\r\nabc def\r\n.\r\n250 OK\r\n"})
        self.addCleanup(tor.close)
        with tor_control.Controller.connect(port=tor.port, cookie=COOKIE) as ctl:
            self.assertEqual(ctl.getinfo("config-text"), "abc def")
            self.assertTrue(ctl.command("GETINFO onions/current").startswith("250-onions/current="))

    def test_add_and_del_onion(self):
        ctl = self.connect()
        reply, sid = ctl.add_onion("ED25519-V3:AAAA", ["80,127.0.0.1:8082"])
        self.assertEqual(sid, SID)
        self.assertIn("ADD_ONION ED25519-V3:AAAA Flags=Detach Port=80,127.0.0.1:8082",
                      self.tor.received)
        self.assertEqual(ctl.del_onion(SID)[0], True)
        self.assertIn("DEL_ONION " + SID, self.tor.received)

    def test_a_command_is_one_line(self):
        ctl = self.connect()
        for bad in ("GETINFO x\r\nSIGNAL HALT", "GETINFO x\nSIGNAL HALT"):
            with self.assertRaises(ValueError):
                ctl.command(bad)
        self.assertNotIn("SIGNAL HALT", self.tor.received)

    def test_events_arrive_as_lines(self):
        ctl = self.connect(timeout=5)
        self.assertEqual(ctl.command("SETEVENTS HS_DESC"), "250 OK\r\n")
        lines = []
        while len(lines) < 2:
            lines += ctl.read_events()
        self.assertTrue(lines[0].startswith("650 HS_DESC UPLOADED " + SID))
        ctl.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            ctl.read_events()

    def test_a_closed_connection_is_control_closed(self):
        ctl = self.connect()
        ctl.send("QUIT")
        with self.assertRaises(tor_control.ControlClosed):
            while True:
                ctl.read_events()


class ScriptTest(unittest.TestCase):
    """The farm's worker verbs: `getinfo <key>` prints the reply as Tor sent
    it, `onions <key>` one <id>.onion per line (what onionheaven-heartbeat
    and check_worker_bootstrap read)."""

    def setUp(self):
        self.tor = FakeTor()
        self.addCleanup(self.tor.close)
        p = mock.patch.multiple(tor_control, CONTROL_PORT=self.tor.port)
        p.start()
        self.addCleanup(p.stop)
        self.cookie = tempfile.NamedTemporaryFile()
        self.addCleanup(self.cookie.close)
        self.cookie.write(COOKIE)
        self.cookie.flush()
        p = mock.patch.object(tor_control, "COOKIE_PATH", self.cookie.name)
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = tor_control.main(["tor_control.py", *args])
        return code, out.getvalue()

    def test_bootstrap_phase(self):
        code, out = self.run_main("getinfo", "status/bootstrap-phase")
        self.assertEqual(code, 0)
        self.assertIn("PROGRESS=100", out)

    def test_onions_detached(self):
        code, out = self.run_main("onions", "onions/detached")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), sorted([SID + ".onion", "b" * 56 + ".onion"]))

    def test_usage(self):
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.run_main("halt")[0], 2)

    def test_no_tor_is_exit_1(self):
        r = subprocess.run([sys.executable, os.path.join(TOR_DIR, "tor_control.py"),
                            "getinfo", "status/bootstrap-phase"],
                           capture_output=True, text=True, timeout=30,
                           cwd=tempfile.gettempdir())
        self.assertEqual(r.returncode, 1, r.stderr)


def _read(name):
    with open(os.path.join(TOR_DIR, name), encoding="utf-8") as f:
        return f.read()


class OneClientTest(unittest.TestCase):

    def test_nothing_else_in_the_image_speaks_to_the_control_port(self):
        for name in sorted(os.listdir(TOR_DIR)):
            if name == "tor_control.py" or not name.endswith((".py", ".sh")):
                continue
            src = _read(name)
            with self.subTest(script=name):
                self.assertNotIn("9051", re.sub(r"(?m)^\s*#.*$", "", src)
                                 .replace("ControlPort 127.0.0.1:9051", ""))
                self.assertNotIn("control_auth_cookie", src)
                self.assertNotIn("AUTHENTICATE", src)
                self.assertIsNone(re.search(r"\bxxd\b|\bnc -", src))

    def test_the_users_go_through_it(self):
        for name in ("tor-watchdog.py", "onionheaven-queue-manager.py",
                     "onionheaven-takeover-worker.py"):
            with self.subTest(script=name):
                self.assertIn("import tor_control", _read(name))
        farm = _read("onionheaven-farm.py")
        self.assertIn('TOR_CONTROL = "/tor_control.py"', farm)
        self.assertIn('"bootstrap-phase": (["python3", TOR_CONTROL, "getinfo", "status/bootstrap-phase"]', farm)
        self.assertIn('"onions-detached": (["python3", TOR_CONTROL, "onions", "onions/detached"]', farm)
        self.assertIn("COPY tor_control.py /tor_control.py", _read("Dockerfile"))

    def test_no_tor_manager(self):
        self.assertFalse(os.path.exists(os.path.join(TOR_DIR, "onionheaven-tor-manager.sh")))
        for name in sorted(os.listdir(TOR_DIR)):
            if name.endswith((".py", ".sh")) or name == "Dockerfile":
                with self.subTest(file=name):
                    self.assertNotIn("tor-manager", _read(name))
        common = _read("onionheaven_common.py")
        for fn in ("_takeover_local", "_release_local"):
            body = common[common.index(f"def {fn}("):]
            body = body[:body.index("\ndef ")]
            self.assertIn("_queue_manager_local(", body)


class LocalQueueManagerTest(unittest.TestCase):
    """_takeover_local / _release_local ask the worker's own queue manager."""

    def setUp(self):
        import onionheaven_common as oc
        self.oc = oc
        p = mock.patch.object(oc, "log")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, stdout, rc=0):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        return mock.patch.object(self.oc.subprocess, "run", return_value=done)

    def test_takeover(self):
        addr = SID + ".onion"
        for status, ok in (("queued", True), ("already_active", True), ("in_flight", True),
                           ("already_queued", True), ("failed", False)):
            with self.subTest(status=status), \
                    self._run(f'{{"status": "{status}", "address": "{addr}"}}') as run:
                self.assertIs(self.oc._takeover_local(addr), ok)
                self.assertEqual(run.call_args.args[0],
                                 ["python3", "/onionheaven-queue-manager.py", "takeover", addr])
        with self._run('{"error": "queue manager not running"}', rc=1):
            self.assertFalse(self.oc._takeover_local(addr))
        with self._run("") as run:
            self.assertFalse(self.oc._takeover_local("not an address"))
            run.assert_not_called()

    def test_release(self):
        addr = SID + ".onion"
        for status in ("released", "not_found", "removed_from_queue", "release_warning"):
            with self.subTest(status=status), self._run(f'{{"status": "{status}"}}'):
                self.assertTrue(self.oc._release_local(addr))
        with self._run('{"error": "queue manager timed out"}', rc=1):
            self.assertFalse(self.oc._release_local(addr))


if __name__ == "__main__":
    unittest.main()
