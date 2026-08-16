#!/usr/bin/env python3
"""Behavioural tests for `onionpress start` idempotence (app/MacOS/onionpress).

Starting an already-running stack must be a no-op, not a conflict: a managing
publisher app owns the stack's lifecycle (install + start) while the menu-bar
app is the
power-user surface, and the menu bar re-enters `start` on every launch
(auto_start -> start_service). Before the fix that meant re-running the
up-to-120s wait_for_services and a blocking GUI dialog over a healthy site.

These run the REAL bash launcher, but inside a throwaway .app layout, a
throwaway $HOME and a sanitised $PATH, so they never touch the developer's
containers, Colima VM or ~/.onionpress. The stack is stubbed by a local HTTP
server answering the receiver's /status route — the same signal publisher
clients and ./test-receiver.sh use (docs/static-publish-protocol.md).

macOS-only: this is the macOS launcher (PlistBuddy, `stat -f`, `arch -arm64`,
osascript). The ubuntu CI job covers the ordering invariant statically, in
tests/test_install_invariants.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER_SRC = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")

# Same ladder as the launcher, publisher clients and test-receiver.sh:
# OnionPress offsets each additional macOS user by +10000.
CANDIDATE_PORTS = (8080, 18080, 28080, 38080, 48080)
STATUS_PATH = "/wp-json/onionpress/v1/status"


def _receiver_answering(port, timeout=1.0):
    """True if something on `port` answers /status like the receiver does."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{STATUS_PATH}", timeout=timeout
        ) as resp:
            return b"receiver_version" in resp.read()
    except urllib.error.HTTPError as err:
        err.close()  # HTTPError is itself a response; closing it keeps the
        return False  # test output free of ResourceWarnings
    except Exception:
        return False


class _StubReceiver(BaseHTTPRequestHandler):
    """Minimal stand-in for the onionpress-static-receiver mu-plugin."""

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != STATUS_PATH:
            self.send_error(404)
            return
        body = json.dumps({
            "onion_address": "stubstubstub.onion",
            "current_generation": None,
            "receiver_version": "1",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # keep test output clean


@unittest.skipUnless(sys.platform == "darwin", "macOS launcher")
class TestLauncherStartIdempotent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-launcher-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Throwaway .app layout: the launcher derives APP_DIR/RESOURCES_DIR/
        # BIN_DIR from its own path, so a copy inside tmp keeps every lookup
        # (bundled colima, docker compose files, ...) pointed at empty dirs.
        contents = os.path.join(self.tmp, "OnionPress.app", "Contents")
        os.makedirs(os.path.join(contents, "MacOS"))
        os.makedirs(os.path.join(contents, "Resources", "bin"))
        self.launcher = os.path.join(contents, "MacOS", "onionpress")
        shutil.copy2(LAUNCHER_SRC, self.launcher)
        os.chmod(self.launcher, 0o755)
        shutil.copy2(os.path.join(PROJECT_ROOT, "app", "Info.plist"),
                     os.path.join(contents, "Info.plist"))

        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.data_dir = os.path.join(self.home, ".onionpress")
        self.pidfile = os.path.join(self.data_dir, "onionpress.pid")

        # Stub osascript so the PID-lock dialog can never pop a real modal
        # (or block) during a test run.
        stub_bin = os.path.join(self.tmp, "stub-bin")
        os.makedirs(stub_bin)
        osascript = os.path.join(stub_bin, "osascript")
        with open(osascript, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(osascript, 0o755)

        self.env = {
            "HOME": self.home,
            # No colima/docker on PATH: if a regression let `start` fall
            # through, it fails fast on the missing runtime instead of
            # touching the developer's real VM.
            "PATH": stub_bin + ":/usr/bin:/bin:/usr/sbin:/sbin",
            # Skip detect_port_offset, which needs the py2app-bundled python
            # (a build artifact that does not exist in a source checkout).
            "ONIONPRESS_PORT_OFFSET": "0",
        }

    def _serve_stub_receiver(self):
        """Bind the first free candidate port; skip if all five are taken."""
        for port in CANDIDATE_PORTS:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), _StubReceiver)
            except OSError:
                continue
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            return port
        self.skipTest(f"no free port in {CANDIDATE_PORTS} to host a stub receiver")

    def _write_pidfile(self, pid):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.pidfile, "w") as f:
            f.write(f"{pid}\n")

    def _run_start(self):
        return subprocess.run(
            ["bash", self.launcher, "start"],
            env=self.env, capture_output=True, text=True, timeout=120,
        )

    def test_start_is_a_noop_when_the_receiver_answers(self):
        """The whole point: a second `start` over a healthy stack exits 0."""
        port = self._serve_stub_receiver()
        proc = self._run_start()
        self.assertEqual(
            proc.returncode, 0,
            f"`start` must be a no-op while the receiver answers on {port}.\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("already running", proc.stdout)
        self.assertIn(str(port), proc.stdout)

    def test_noop_start_leaves_no_pid_lock_behind(self):
        """No PID file existed, so none may be left — the early exit must
        happen before `echo $$ > $PIDFILE`, not after."""
        self._serve_stub_receiver()
        proc = self._run_start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            os.path.exists(self.pidfile),
            "the no-op path wrote a PID lock it never needed",
        )

    def test_noop_start_does_not_delete_another_invocations_pid_file(self):
        """Trap safety. The EXIT trap removes $PIDFILE; if the early exit ran
        after the trap was installed, a `start` that no-ops would strip the
        lock belonging to the live invocation that owns it."""
        owner_pid = os.getpid()  # a process that is definitely alive
        self._write_pidfile(owner_pid)
        self._serve_stub_receiver()

        proc = self._run_start()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            os.path.exists(self.pidfile),
            "the no-op path deleted a PID file belonging to another invocation",
        )
        with open(self.pidfile) as f:
            self.assertEqual(f.read().strip(), str(owner_pid))

    def test_pid_lock_still_rejects_a_start_while_one_is_booting(self):
        """Fall-through is unchanged: no receiver answering + a live PID file
        is still a genuine collision (a `start` mid-boot), still exit 1."""
        answering = [p for p in CANDIDATE_PORTS if _receiver_answering(p)]
        if answering:
            self.skipTest(f"a real OnionPress receiver is answering on {answering}")

        owner_pid = os.getpid()
        self._write_pidfile(owner_pid)

        proc = self._run_start()

        self.assertEqual(
            proc.returncode, 1,
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn(str(owner_pid), proc.stderr)
        self.assertTrue(os.path.exists(self.pidfile), "PID lock was released")


if __name__ == "__main__":
    unittest.main()
