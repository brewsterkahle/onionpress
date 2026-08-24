#!/usr/bin/env python3
"""Behavioural tests for the launcher's ensure_menubar_running helper.

`quit` takes the MenubarApp down; `start` only ever brought containers back.
Any caller that scripts quit+start — moss's Restart recovery is exactly that
pair — therefore left the app off permanently, and the MenubarApp is the sole
writer of status.json and the sole sender of OnionHeaven's /online heartbeat.
The 2026-08-16 consequence: a frozen 19-hour-old reachability verdict and an
OnionHeaven takeover that could never be released.

These run the REAL helper, extracted from app/MacOS/onionpress by name and
sourced on its own. Extraction rather than running the whole launcher is what
makes this cross-platform: the launcher's top level is macOS-only (sysctl,
PlistBuddy, `stat -f`, `arch -arm64`) and has side effects — mkdir, a home
directory migration — that a unit test has no business triggering. The helper
itself uses only pgrep/nohup/rm, which behave the same on Linux, so the CI
that runs on ubuntu gets real coverage of the logic instead of a text match.

tests/test_install_invariants.py guards the call site and its ordering, which
is the half this file cannot see.
"""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER_SRC = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")

# The matcher the helper (and launcher.sh, and the quit arm) uses to decide
# whether a MenubarApp is already alive.
MENUBAR_MATCH = "MenubarApp/Contents/MacOS/OnionPress"


def _extract_function(name):
    """Return the source of shell function `name` from the launcher.

    Relies on the file's own layout convention: a function opens at column 0
    as `name() {` and closes at column 0 with `}`.
    """
    with open(LAUNCHER_SRC, "r", encoding="utf-8") as f:
        src = f.read()
    match = re.search(
        r"^%s\(\)\s*\{\n.*?^\}\n" % re.escape(name), src, re.M | re.S
    )
    if not match:
        raise AssertionError(f"{name}() not found in {LAUNCHER_SRC}")
    return match.group(0)


def _extract_start_trap():
    """Return the `trap … EXIT INT TERM HUP` line the `start` arm installs."""
    with open(LAUNCHER_SRC, "r", encoding="utf-8") as f:
        src = f.read()
    matches = re.findall(r"^\s*(trap .*EXIT INT TERM HUP)\s*$", src, re.M)
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one EXIT INT TERM HUP trap in %s, found %d"
            % (LAUNCHER_SRC, len(matches))
        )
    return matches[0]


class TestEnsureMenubarRunning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-menubar-revival-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.resources = os.path.join(self.tmp, "Resources")
        self.data_dir = os.path.join(self.tmp, "data")
        self.log_file = os.path.join(self.tmp, "launcher.log")
        os.makedirs(self.data_dir)

        self.menubar_bin = os.path.join(
            self.resources, "MenubarApp", "Contents", "MacOS", "OnionPress"
        )
        self.marker = os.path.join(self.tmp, "launched")
        self.pidfile = os.path.join(self.data_dir, "menubar.pid")

        self.helper = "\n".join(
            _extract_function(name)
            for name in ("menubar_alive", "ensure_menubar_running")
        )

        # A real MenubarApp on a developer's Mac matches the same pgrep as
        # our stub would, so the "nothing running" cases are not decidable.
        if self._menubar_process_alive():
            self.skipTest("a real MenubarApp is running for this user")

    def _menubar_process_alive(self):
        # `ps`, not `pgrep`, for the reason the helper itself no longer uses
        # pgrep: on macOS it can fail to see a live MenubarApp, and a skip
        # guard that under-reports would let the "nothing running" cases run
        # against a developer's real app.
        procs = subprocess.run(
            ["ps", "-x", "-o", "args="], capture_output=True, text=True,
        ).stdout
        return MENUBAR_MATCH in procs

    def _install_stub(self, body):
        os.makedirs(os.path.dirname(self.menubar_bin), exist_ok=True)
        with open(self.menubar_bin, "w") as f:
            f.write(body)
        os.chmod(self.menubar_bin, 0o755)

    def _blind_pgrep_dir(self):
        """A directory holding a `pgrep` that never finds anything.

        Stands in for macOS's real behaviour — see the docstring on
        test_finds_a_live_app_that_pgrep_cannot_see.
        """
        d = os.path.join(self.tmp, "blind-bin")
        os.makedirs(d, exist_ok=True)
        stub = os.path.join(d, "pgrep")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)
        return d

    def _live_stub_pids(self):
        """PIDs of our stub MenubarApps that are still running.

        Scoped to this test's temp dir so a developer's real app — which
        setUp already skips on — could never be counted, and `ps` rather
        than `pgrep` for the reason the helper itself no longer uses pgrep.
        """
        procs = subprocess.run(
            ["ps", "-x", "-o", "pid=,args="], capture_output=True, text=True,
        ).stdout.splitlines()
        return [
            line.split(None, 1)[0]
            for line in procs
            if MENUBAR_MATCH in line and self.tmp in line
        ]

    def _run_helper(self, path_prefix=None, trap_and_exit=False):
        """Source the real helper with the launcher's globals defined.

        Run from a FILE, not `bash -c`: the matcher string would otherwise sit
        in the harness's own command line, where the helper's process scan
        would find it and no-op. Production runs `bash /…/onionpress start`,
        whose command line carries no such string, so a file keeps the test
        faithful as well as correct.

        With `trap_and_exit`, the script goes on to install the real trap the
        `start` arm installs and then finish, which is the rest of what a
        production `start` does after reviving the app.
        """
        env = dict(os.environ)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
        tail = ""
        if trap_and_exit:
            # The trap goes in AFTER the revival, as it does in the launcher,
            # and `start` then runs for a long time before exiting — ~80s in
            # the reported incident. One second is enough to let the stub get
            # going so that a dead stub afterwards means the trap killed it,
            # not that it never ran.
            tail = f"{_extract_start_trap()}\nsleep 1\nexit 0\n"
        script = textwrap.dedent(f"""\
            set -e
            RESOURCES_DIR={self.resources!r}
            DATA_DIR={self.data_dir!r}
            LOG_FILE={self.log_file!r}
            PIDFILE={os.path.join(self.data_dir, "onionpress.pid")!r}
            log() {{ echo "[log] $1" >> "$LOG_FILE"; }}

            {self.helper}

            ensure_menubar_running
            {tail}
        """)
        runner = os.path.join(self.tmp, "run-helper.sh")
        with open(runner, "w") as f:
            f.write(script)
        return subprocess.run(
            ["bash", runner], capture_output=True, text=True, timeout=30,
            env=env,
        )

    def _wait_for_marker(self, timeout=10.0):
        """The helper backgrounds the app, so the marker lands after it
        returns."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.marker):
                return True
            time.sleep(0.05)
        return False

    def _kill_stubs(self):
        subprocess.run(
            ["pkill", "-u", str(os.getuid()), "-f", MENUBAR_MATCH],
            capture_output=True,
        )

    def test_no_bundle_is_a_noop(self):
        """A source checkout or CI has no built MenubarApp — revival must be
        a no-op there, not an error that fails `start`."""
        proc = self._run_helper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_launches_the_app_when_it_is_not_running(self):
        """The whole point: containers may be fine, but a dead MenubarApp
        must be brought back."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\n")

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            self._wait_for_marker(),
            "ensure_menubar_running did not launch the MenubarApp",
        )

    def test_does_not_launch_a_second_copy(self):
        """The MenubarApp re-enters `onionpress start` on every launch
        (auto_start -> start_service), so an unguarded spawn would have the
        app start a second copy of itself."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\nsleep 30\n")
        self.addCleanup(self._kill_stubs)

        # A live process whose command line matches the helper's pgrep — the
        # stub's own path contains the matcher, so running it is enough.
        alive = subprocess.Popen(
            [self.menubar_bin],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        self.assertTrue(self._wait_for_marker(), "stub never started")
        os.remove(self.marker)

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            self._wait_for_marker(timeout=2.0),
            "a second MenubarApp was launched over a live one",
        )

    def test_finds_a_live_app_that_pgrep_cannot_see(self):
        """Detection must not rest on pgrep, because on macOS pgrep lies.

        Reproduced 2026-08-18 on macOS 26.5: with the MenubarApp running,
        `ps -x -o args=` prints its full path while `pgrep -f` for that same
        path, run at the same instant as the same uid, returns nothing. It
        misses the app specifically when the launcher runs as a child of that
        app — the one context this guard has to be right in, because the app
        re-enters `onionpress start` on every launch.

        What it cost: one moss recovery Start at 00:57:36 produced four
        MenubarApps in fifteen seconds. The guard reported "not running" each
        time, and — worse — went on to delete menubar.pid, which is what the
        app's OWN single-instance check reads (menubar.py __init__), so each
        new copy also sailed past that second line of defence. Three of the
        four lost the race for the onion proxy port with `[Errno 48] Address
        already in use`.

        The teardown 78 seconds later was blamed on that race for two days
        and did not belong to it — see
        test_the_app_survives_the_start_arm_finishing. Both bugs are real and
        this one is still worth the guard; only the teardown was misattributed.

        The stub pgrep here is blind on every platform, so this test asserts
        the guarantee rather than the macOS symptom: a live app is found, and
        its PID file survives, without pgrep contributing anything.
        """
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\nsleep 30\n")
        self.addCleanup(self._kill_stubs)

        alive = subprocess.Popen(
            [self.menubar_bin],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        self.assertTrue(self._wait_for_marker(), "stub never started")
        os.remove(self.marker)

        # The app writes this itself, very early in __init__.
        with open(self.pidfile, "w") as f:
            f.write(f"{alive.pid}\n")

        proc = self._run_helper(path_prefix=self._blind_pgrep_dir())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            self._wait_for_marker(timeout=2.0),
            "a second MenubarApp was launched over a live one",
        )
        self.assertTrue(
            os.path.exists(self.pidfile),
            "the live app's menubar.pid was deleted, which is what lets the "
            "next copy past menubar.py's own single-instance check",
        )

    def test_a_recycled_pid_is_not_mistaken_for_the_app(self):
        """menubar.pid alone is not identity — PIDs get recycled.

        A pid file left by a SIGKILLed app can name a PID the OS has since
        handed to something else entirely. Trusting `kill -0` on its own
        would then report the MenubarApp as alive forever and revival would
        never happen — the exact stranding this helper exists to prevent.
        """
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\n")
        # A live process that is emphatically not a MenubarApp.
        impostor = subprocess.Popen(["sleep", "30"])
        self.addCleanup(impostor.wait)
        self.addCleanup(impostor.kill)
        with open(self.pidfile, "w") as f:
            f.write(f"{impostor.pid}\n")

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            self._wait_for_marker(),
            "a recycled PID in menubar.pid suppressed a needed revival",
        )

    def test_clears_a_stale_pid_file_before_launching(self):
        """`quit` escalates to SIGKILL, which bypasses the app's own
        _remove_pid_file. The leftover menubar.pid is not inert — the
        launcher's upload-analytics arm reads it as liveness."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\n")
        with open(self.pidfile, "w") as f:
            f.write("2991\n")  # the dead PID from the 2026-08-16 incident

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._wait_for_marker())
        self.assertFalse(
            os.path.exists(self.pidfile),
            "a stale menubar.pid survived the relaunch",
        )

    def test_the_child_does_not_hold_the_callers_pipe_open(self):
        """moss runs the launcher as a subprocess and reads its output to
        EOF. A backgrounded child inheriting that pipe keeps it open, so the
        caller would block for as long as the MenubarApp lives — which is
        forever, by design. subprocess.run() below reads to EOF, so an
        inherited pipe shows up here as a timeout, not a wrong assertion.
        """
        self._install_stub(
            f"#!/bin/sh\ntouch {self.marker!r}\necho noise\nsleep 30\n"
        )
        self.addCleanup(self._kill_stubs)

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._wait_for_marker())
        self.assertNotIn(
            "noise", proc.stdout,
            "the MenubarApp's output reached the caller's pipe instead of "
            "the log file",
        )
        with open(self.log_file) as f:
            self.assertIn("noise", f.read(),
                          "the MenubarApp's output should land in the log")

    def test_the_app_survives_the_start_arm_finishing(self):
        """The `start` that revives the app must not then kill it.

        `nohup` makes the child ignore SIGHUP; it does NOT take the job out
        of the shell's job table. `start` installs

            trap 'rm -f "$PIDFILE"; kill $(jobs -p) …' EXIT INT TERM HUP

        *after* calling ensure_menubar_running, so `jobs -p` still listed the
        MenubarApp and the trap SIGTERMed it the moment `start` finished.
        That is the 2026-08-18 report: install completed green, and ~35s
        later OnionPress tore the whole stack down. Nothing logged it — the
        trap is silent — and moss, which never touched the app, was suspected
        for a day.

        It looked intermittent because it is not: it fires only on the
        `start` that actually spawns the app, since every later one returns
        early above and creates no job at all.

        The trap is extracted from the launcher rather than retyped here, so
        a change to it is a change to this test.
        """
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\nsleep 30\n")
        self.addCleanup(self._kill_stubs)

        proc = self._run_helper(trap_and_exit=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._wait_for_marker(), "stub never started")
        self.assertTrue(
            self._live_stub_pids(),
            "the MenubarApp was killed by `start`'s own EXIT trap — the "
            "launch must be disowned so the trap cannot reach it",
        )


if __name__ == "__main__":
    unittest.main()
