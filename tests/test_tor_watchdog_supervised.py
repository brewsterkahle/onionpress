"""tor-watchdog.py is supervised (issue #313).

The watchdog does sleep/wake (DEL_ONION/ADD_ONION), stall recovery and the
app's republish requests, and nothing else in the tor container does. It
used to be started with a bare `python3 /tor-watchdog.py &`, so one
uncaught exception left the site to go dark at the next sleep. Now:

- every entrypoint mode starts it through `supervise_watchdog`, which logs
  each exit, restarts it with backoff and kills one whose alive file is
  stale (hung);
- the watchdog touches that alive file and keeps the host's sleep state in
  a marker, so a restart while the host sleeps does not ADD_ONION;
- the supervisor clears the run dir once per container start, so neither
  the marker nor a republish request outlives the container.

The supervisor is run here for real in a sandbox, with `python3` replaced
by a shell function.
"""

import importlib.util
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

_REPO = os.path.join(os.path.dirname(__file__), "..")
_TOR = os.path.join(_REPO, "app", "Resources", "docker", "tor")
_ENTRYPOINT = os.path.join(_TOR, "entrypoint.sh")
_WATCHDOG = os.path.join(_TOR, "tor-watchdog.py")


def _read(path):
    with open(path) as f:
        return f.read()


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("tor_watchdog_supervised", _WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The supervisor and the host's request script run in the tor image
# (Debian: GNU stat). BSD stat (a Mac running the tests) has no -c.
_GNU_STAT = subprocess.run(["stat", "-c", "%Y", "/"], capture_output=True).returncode == 0


def _supervisor_source():
    """The supervise_watchdog function, without the entrypoint's settings."""
    src = _read(_ENTRYPOINT)
    m = re.search(r"^supervise_watchdog\(\) \{\n.*?^\}\n", src, re.S | re.M)
    assert m, "supervise_watchdog() not found in entrypoint.sh"
    return m.group(0)


class TestEntrypointStartsOnlyTheSupervisor(unittest.TestCase):
    def test_the_watchdog_is_started_only_inside_the_supervisor(self):
        src = _read(_ENTRYPOINT)
        func = _supervisor_source()
        self.assertEqual(src.count("python3 /tor-watchdog.py"), 1)
        self.assertIn("python3 /tor-watchdog.py &", func)

    def test_every_mode_runs_the_supervisor(self):
        # Takeover worker, onionheaven, SOCKS-only and the onion service mode.
        src = _read(_ENTRYPOINT)
        self.assertEqual(len(re.findall(r"^\s*supervise_watchdog &$", src, re.M)), 4)

    def test_run_dir_matches_the_watchdog(self):
        src = _read(_ENTRYPOINT)
        run_dir = re.search(r'^WATCHDOG_RUN_DIR="([^"]+)"$', src, re.M).group(1)
        mod = _load_watchdog()
        for path in (mod.ALIVE_PATH, mod.SLEEPING_PATH, mod.REPUBLISH_REQUEST_PATH):
            self.assertEqual(os.path.dirname(path), run_dir)
        func = _supervisor_source()
        for path in (mod.ALIVE_PATH, mod.SLEEPING_PATH, mod.REPUBLISH_REQUEST_PATH):
            self.assertIn('"$WATCHDOG_RUN_DIR/%s"' % os.path.basename(path), func)

    def test_supervisor_cannot_be_signalled_as_the_watchdog(self):
        # The hosts signal `pgrep -f '[t]or-watchdog'`; a supervisor whose
        # command line matched would be killed by the USR1 meant for the
        # watchdog. It is a function in this script, so it carries the
        # script's command line; there must be no separate supervisor
        # script under a tor-watchdog name.
        self.assertFalse([n for n in os.listdir(_TOR)
                          if "tor-watchdog" in n and n != "tor-watchdog.py"])


class TestSupervisorRuns(unittest.TestCase):
    def _run(self, fake_python, seconds, stale_secs=300):
        tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", tmp])
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir)
        for name in ("sleeping", "republish-requested"):
            open(os.path.join(run_dir, name), "w").close()
        script = (
            f'WATCHDOG_RUN_DIR="{run_dir}"\n'
            f"WATCHDOG_STALE_SECS={stale_secs}\n"
            "WATCHDOG_POLL_SECS=1\n"
            "WATCHDOG_MAX_BACKOFF=60\n"
            "WATCHDOG_RESET_AFTER=600\n"
            f'STARTS="{tmp}/starts"\n'
            + fake_python + "\n"
            + _supervisor_source()
            + "supervise_watchdog\n"
        )
        proc = subprocess.Popen(["sh", "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
        starts = 0
        if os.path.exists(os.path.join(tmp, "starts")):
            starts = len(_read(os.path.join(tmp, "starts")).split())
        return out, starts, run_dir

    def test_an_exit_is_logged_and_restarted(self):
        # Peek at the run dir from inside the first start: the previous
        # container life's state must already be gone.
        fake = ('python3() { echo start >> "$STARTS"; '
                'ls "$WATCHDOG_RUN_DIR" >> "$STARTS.ls"; exit 7; }')
        out, starts, run_dir = self._run(fake, seconds=4)
        self.assertGreaterEqual(starts, 2, out)
        self.assertIn("exited with status 7", out)
        self.assertIn("restarting in 1s", out)
        self.assertIn("restarting in 2s", out)
        self.assertEqual(_read(os.path.join(os.path.dirname(run_dir), "starts.ls")), "")

    @unittest.skipUnless(_GNU_STAT, "needs GNU stat, as in the tor image")
    def test_a_hung_watchdog_is_killed_and_restarted(self):
        stamp = time.strftime("%Y%m%d%H%M.%S", time.localtime(time.time() - 1000))
        fake = ('python3() { echo start >> "$STARTS"; '
                f'touch -t {stamp} "$WATCHDOG_RUN_DIR/alive"; '
                'exec sleep 1000; }')
        out, starts, _ = self._run(fake, seconds=6, stale_secs=300)
        self.assertIn("has not touched its alive file", out)
        self.assertIn("exited with status", out)
        self.assertGreaterEqual(starts, 2, out)

    def test_a_fresh_alive_file_is_left_alone(self):
        fake = ('python3() { echo start >> "$STARTS"; '
                'touch "$WATCHDOG_RUN_DIR/alive"; exec sleep 1000; }')
        out, starts, _ = self._run(fake, seconds=3, stale_secs=300)
        self.assertNotIn("has not touched", out)
        self.assertEqual(starts, 1, out)


class TestWatchdogState(unittest.TestCase):
    def setUp(self):
        self.mod = _load_watchdog()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = os.path.join(tmp.name, "run", "tor-watchdog")
        self.mod.ALIVE_PATH = os.path.join(run, "alive")
        self.mod.SLEEPING_PATH = os.path.join(run, "sleeping")
        self.mod.REPUBLISH_REQUEST_PATH = os.path.join(run, "republish-requested")
        self.logged = []
        self.mod.log = self.logged.append

    def test_touch_alive_creates_and_refreshes(self):
        self.mod.touch_alive()
        self.assertTrue(os.path.exists(self.mod.ALIVE_PATH))
        os.utime(self.mod.ALIVE_PATH, (1, 1))
        self.mod.touch_alive()
        self.assertGreater(os.stat(self.mod.ALIVE_PATH).st_mtime, time.time() - 60)

    def test_sleep_marker_round_trip(self):
        self.assertFalse(self.mod.sleep_marker_present())
        self.mod.set_sleep_marker(True)
        self.assertTrue(self.mod.sleep_marker_present())
        self.mod.set_sleep_marker(False)
        self.assertFalse(self.mod.sleep_marker_present())
        self.mod.set_sleep_marker(False)  # idempotent

    def _start_state(self):
        """Run run() up to its first control-port connect; return its state."""
        class Stop(Exception):
            pass
        seen = []
        real = self.mod.WatchdogState

        def make_state():
            seen.append(real())
            return seen[-1]

        with mock.patch.object(self.mod, "WatchdogState", make_state), \
                mock.patch.object(self.mod, "discover_services", return_value=[]), \
                mock.patch.object(self.mod, "connect_and_auth", side_effect=Stop):
            with self.assertRaises(Stop):
                self.mod.run()
        return seen[0]

    def test_a_restart_while_asleep_stays_asleep(self):
        self.mod.set_sleep_marker(True)
        self.assertTrue(self._start_state().sleeping)
        self.assertTrue(os.path.exists(self.mod.ALIVE_PATH))

    def test_a_restart_while_awake_is_awake(self):
        self.assertFalse(self._start_state().sleeping)

    def test_a_restart_keeps_a_pending_request(self):
        os.makedirs(os.path.dirname(self.mod.REPUBLISH_REQUEST_PATH), exist_ok=True)
        with open(self.mod.REPUBLISH_REQUEST_PATH, "w") as f:
            f.write("unreachable\n")
        self._start_state()
        self.assertTrue(os.path.exists(self.mod.REPUBLISH_REQUEST_PATH))

    def test_sleep_and_wake_record_the_marker(self):
        src = _read(_WATCHDOG)
        self.assertRegex(src, r"state\.sleeping = True\n\s+set_sleep_marker\(True\)")
        self.assertRegex(src, r"state\.sleeping = False\n\s+set_sleep_marker\(False\)")

    def test_every_wait_loop_touches_alive(self):
        # The supervisor kills a watchdog whose alive file is 300 s old;
        # each loop that can run that long must keep touching it.
        src = _read(_WATCHDOG)
        cookie = src[src.index("def read_cookie"):src.index("def connect_and_auth")]
        self.assertIn("touch_alive()", cookie)
        run = src[src.index("def run():"):]
        self.assertGreaterEqual(run.count("touch_alive()"), 5)

    def test_a_fatal_error_logs_its_traceback(self):
        self.assertIn("traceback.format_exc()", _read(_WATCHDOG))


if __name__ == "__main__":
    unittest.main()
