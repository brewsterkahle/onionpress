"""Linux's onion service recovery loop (issues #266, #310).

The Mac menubar runs the dual probe and the RepublishTrigger inline in
check_status(). On Linux nothing ran them: the code lived in
linux/onionpress-service.py, which no installer shipped and no unit
started. The heartbeat daemon (linux/onionpress-heartbeat.py) is the
long-running per-user service every install already has, so it runs this
loop in a thread:

  - HealthChecker.full_check() every POLL_READY_SECONDS: WordPress, Tor
    bootstrap, hostname, Tor → WordPress, then the dual probe of
    /.onionpress-health through onionheaven's tor;
  - HealthMonitor.should_request_republish() (the shared RepublishTrigger);
  - launcher_ops.request_watchdog_republish() when it is due, or only a log
    line with REPUBLISH_ON_UNREACHABLE=no;
  - the window is reset on wake (note_wake, and a wall-clock vs monotonic
    gap for a suspend no D-Bus signal reported) and while offline, as on
    the Mac.

Each cycle's verdict goes to ~/.onionpress/reachability.json, which the
bash launcher's write_status reads instead of fetching the whole front
page through Tor itself.
"""

import json
import os
import threading
import time
from typing import Callable, Optional

from . import launcher_ops
from .health import (
    HealthChecker, HealthMonitor, HealthResult, POLL_READY_SECONDS,
)

STATUS_FILENAME = "reachability.json"

# CLOCK_MONOTONIC stops while the machine is suspended and the wall clock
# does not; a cycle whose two clocks drift apart by more than this slept
# in between, whether or not logind's PrepareForSleep reached us.
SUSPEND_GAP_SECONDS = 60


def _republish_enabled(read_config_value: Callable[[str, str], str]) -> bool:
    """REPUBLISH_ON_UNREACHABLE in ~/.onionpress/config; default on.
    "no" keeps the trigger in log-only mode — same knob as the Mac."""
    value = read_config_value("REPUBLISH_ON_UNREACHABLE", "yes")
    return str(value).strip().lower() not in ("no", "0", "false", "off")


class ReachabilityLoop:
    """The dual probe + republish trigger, one cycle per run_once()."""

    def __init__(
        self,
        docker,
        log_func: Callable[[str], None],
        read_config_value: Callable[[str, str], str],
        status_path: str,
        has_internet: Callable[[], bool] = lambda: True,
        interval: float = POLL_READY_SECONDS,
        container: str = "onionpress-tor",
    ):
        self.docker = docker
        self.log = log_func
        self.read_config_value = read_config_value
        self.status_path = status_path
        self.has_internet = has_internet
        self.interval = interval
        self.container = container
        self.checker = HealthChecker(docker, log_func=log_func)
        self.monitor = HealthMonitor(log_func=log_func)
        self.onion_address = ""
        self._lock = threading.Lock()
        self._sleeping = False
        # Bumped by note_sleep/note_wake: a probe that was in flight across
        # either is not counted (its outcome belongs to neither side).
        self._epoch = 0
        self._kick = threading.Event()
        self._last_wall: Optional[float] = None
        self._last_mono: Optional[float] = None

    # -- sleep / wake --------------------------------------------------------

    def note_sleep(self) -> None:
        """The system is about to suspend: stop probing until note_wake."""
        with self._lock:
            self._sleeping = True
            self._epoch += 1

    def note_wake(self) -> None:
        """Back from suspend (mirrors src/menubar.py handle_wake).

        Issue #266: forget any pre-sleep failure window so the first probe
        after wake can't look like 10+ min of unreachability and trigger
        DEL+ADD on top of the watchdog's own wake ADD.
        """
        with self._lock:
            self._sleeping = False
            self._epoch += 1
            self.monitor.republish.reset()
            self.monitor.state.wordpress_confirmed = False
        self._kick.set()

    def _slept_since_last_cycle(self) -> bool:
        wall, mono = time.time(), time.monotonic()
        last_wall, last_mono = self._last_wall, self._last_mono
        self._last_wall, self._last_mono = wall, mono
        if last_wall is None or last_mono is None:
            return False
        return (wall - last_wall) - (mono - last_mono) > SUSPEND_GAP_SECONDS

    # -- one cycle -----------------------------------------------------------

    def run_once(self) -> Optional[HealthResult]:
        """Probe once, feed the trigger, write the verdict. Returns the
        HealthResult, or None when no probe ran (asleep, offline)."""
        if self._slept_since_last_cycle():
            self.log("reachability: suspend detected (clock gap) — "
                     "resetting the republish window")
            self.note_wake()
            self._kick.clear()
        with self._lock:
            if self._sleeping:
                return None
            epoch = self._epoch
        if not self.has_internet():
            # No probe ran, so this cycle is evidence of nothing: don't let
            # an offline gap count towards a republish.
            with self._lock:
                self.monitor.republish.reset()
            self._write_status(None)
            return None

        hr = self.checker.full_check(
            expected_address=self.onion_address,
            wordpress_confirmed=self.monitor.state.wordpress_confirmed,
        )
        if hr.onion_address:
            self.onion_address = hr.onion_address

        due = False
        with self._lock:
            if self._sleeping or epoch != self._epoch:
                return None
            self.monitor.evaluate(hr, is_running=True)
            # Issue #266: unreachable through the Tor network for the
            # trigger's window with Tor internally healthy means the HSDirs
            # hold a descriptor whose intro points no longer answer for us.
            due = self.monitor.should_request_republish(hr)
            reason = self.monitor.republish.describe(time.time()) if due else ""
        if due:
            self._request_republish(reason)
        self._write_status(hr)
        return hr

    def _request_republish(self, reason: str) -> None:
        if not _republish_enabled(self.read_config_value):
            self.log(f"reachability: {reason} — would request descriptor "
                     f"republish (REPUBLISH_ON_UNREACHABLE=no, log-only)")
        else:
            status = launcher_ops.request_watchdog_republish(
                self.docker, self.container, reason)
            self.log(f"reachability: {reason} — "
                     f"{launcher_ops.describe_republish_request(status)}")

    def _write_status(self, hr: Optional[HealthResult]) -> None:
        """~/.onionpress/reachability.json, atomically. `reachable` is
        None whenever the dual probe did not run this cycle."""
        status = {
            "checked_at": int(time.time()),
            "onion_address": self.onion_address,
            "internally_ready": bool(hr and hr.tor_internally_ready),
            "reachable": hr.tor_externally_reachable if hr else None,
            "http_code": hr.external_http_code if hr else None,
            "wedge_class": hr.wedge_class if hr else None,
        }
        tmp = self.status_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(status, fh)
            os.replace(tmp, self.status_path)
        except OSError as e:
            self.log(f"reachability: could not write {self.status_path}: {e}")

    # -- the thread ----------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # never let one bad cycle end the loop
                self.log(f"reachability: cycle failed: {e}")
            self._kick.wait(timeout=self.interval)
            self._kick.clear()

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,),
                                  daemon=True, name="reachability")
        thread.start()
        return thread

    def stop(self) -> None:
        """Wake the loop so it sees its stop event promptly."""
        self._kick.set()
