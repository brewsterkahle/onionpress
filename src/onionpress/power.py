"""Cross-platform sleep prevention, and Linux's sleep/wake events.

Mac:   CaffeineManager wraps `caffeinate -s | -i`.
Linux: SystemdInhibitor wraps `systemd-inhibit --what=idle:sleep`.

Both classes share the same interface (`start()` / `stop()` /
`is_running()`) so the daemon (onionpress.daemon, on either platform)
can swap them freely. Both
read the `PREVENT_SLEEP` key from `~/.onionpress/config`:

    normal      — no inhibit; system sleeps normally
    on-battery  — Mac: caffeinate -s (AC only); Linux: idle inhibit
                  (best-effort; Linux can't easily condition on AC)
    never       — Mac: caffeinate -i; Linux: idle+sleep inhibit

Both reap orphaned inhibitor processes from prior crashes via the
shared PID file in app_support / data_dir.

LogindPowerHandler (below) is Linux's side of the daemon's sleep and
wake: a systemd-logind delay inhibitor and the PrepareForSleep signal,
behind the interface of onionpress.mac_power.MacPowerHandler (issue
#407; until then linux/onionpress-heartbeat.py's SleepInhibitor).
"""

import os
import shutil
import subprocess
import threading
from typing import Callable, Optional


class CaffeineManager:
    def __init__(
        self,
        app_support: str,
        log_func: Callable[[str], None],
        read_config: Callable[[str, str], str],
    ):
        self.app_support = app_support
        self._log = log_func
        self._read_config = read_config
        self._process: Optional[subprocess.Popen] = None

    @property
    def pid_file(self) -> str:
        return os.path.join(self.app_support, "caffeinate.pid")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _cleanup_stale(self) -> None:
        if not os.path.exists(self.pid_file):
            return
        try:
            with open(self.pid_file) as f:
                old_pid = int(f.read().strip())
            # Confirm it's actually caffeinate before killing — PIDs recycle.
            result = subprocess.run(
                ["ps", "-p", str(old_pid), "-o", "comm="],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0 and "caffeinate" in result.stdout:
                os.kill(old_pid, 15)  # SIGTERM
                self._log(f"Cleaned up orphaned caffeinate (PID {old_pid}) from previous run")
            os.remove(self.pid_file)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            try:
                os.remove(self.pid_file)
            except OSError:
                pass

    def start(self) -> None:
        """Start caffeinate per configured mode. No-op if already running or mode=normal."""
        if self.is_running():
            return

        self._cleanup_stale()

        mode = self._read_config("PREVENT_SLEEP", "normal").lower()
        # Backward compat for older configs.
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            caff_args = ["caffeinate", "-s"]
            caff_msg = "staying awake on AC power"
        elif mode == "never":
            caff_args = ["caffeinate", "-i"]
            caff_msg = "never sleeping while OnionPress runs"
        else:
            return  # "normal" or unknown — Mac sleeps normally

        try:
            self._process = subprocess.Popen(
                caff_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                with open(self.pid_file, "w") as f:
                    f.write(str(self._process.pid))
            except OSError:
                pass
            self._log(f"Started caffeinate (PID {self._process.pid}) - {caff_msg}")
        except Exception as e:
            self._log(f"Failed to start caffeinate: {e}")

    def stop(self) -> None:
        """Terminate caffeinate and remove the PID file."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
            self._log("Stopped caffeinate - Mac can sleep normally")
        except Exception:
            try:
                self._process.kill()
                self._log("Force killed caffeinate process")
            except Exception:
                pass
        finally:
            self._process = None
            try:
                os.remove(self.pid_file)
            except OSError:
                pass


class SystemdInhibitor:
    """Linux equivalent of CaffeineManager — wraps systemd-inhibit.

    `systemd-inhibit --what=<locks> sleep infinity` holds an inhibitor
    lock as long as its child sleep process is alive. We Popen() that
    command and store the PID; on stop() we terminate it and the lock
    releases.

    `what` modes:
      idle       — prevents idle suspend (screensaver / auto-suspend)
      sleep      — prevents systemd-initiated suspend / hibernate
      idle:sleep — both

    Manual sleep (lid close, `systemctl suspend`) is governed by
    different locks (`handle-lid-switch`, etc.) which we deliberately
    do NOT block — the user must always be able to put the machine
    to sleep deliberately.
    """

    def __init__(
        self,
        data_dir: str,
        log_func: Callable[[str], None],
        read_config: Callable[[str, str], str],
    ):
        self.data_dir = data_dir
        self._log = log_func
        self._read_config = read_config
        self._process: Optional[subprocess.Popen] = None

    @property
    def pid_file(self) -> str:
        return os.path.join(self.data_dir, "systemd-inhibit.pid")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _cleanup_stale(self) -> None:
        if not os.path.exists(self.pid_file):
            return
        try:
            with open(self.pid_file) as f:
                old_pid = int(f.read().strip())
            # Confirm it's actually systemd-inhibit before killing — PIDs recycle.
            try:
                with open(f"/proc/{old_pid}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                comm = ""
            if "systemd-inhibit" in comm or comm == "systemd-inhibi":
                os.kill(old_pid, 15)
                self._log(f"Cleaned up orphaned systemd-inhibit (PID {old_pid})")
            os.remove(self.pid_file)
        except (ValueError, OSError):
            try:
                os.remove(self.pid_file)
            except OSError:
                pass

    def start(self) -> None:
        """Start systemd-inhibit per configured mode. No-op if already running."""
        if self.is_running():
            return

        if not shutil.which("systemd-inhibit"):
            return  # No systemd on this host — silently skip.

        self._cleanup_stale()

        mode = self._read_config("PREVENT_SLEEP", "normal").lower()
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            what = "idle"
            msg = "blocking idle suspend while OnionPress runs"
        elif mode == "never":
            what = "idle:sleep"
            msg = "blocking idle and auto-suspend while OnionPress runs"
        else:
            return  # "normal" — Linux sleeps normally

        try:
            self._process = subprocess.Popen(
                ["systemd-inhibit",
                 f"--what={what}",
                 "--who=OnionPress",
                 "--why=Serving onion site",
                 "--mode=block",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            try:
                with open(self.pid_file, "w") as f:
                    f.write(str(self._process.pid))
            except OSError:
                pass
            self._log(f"Started systemd-inhibit (PID {self._process.pid}) - {msg}")
        except Exception as e:
            self._log(f"Failed to start systemd-inhibit: {e}")

    def stop(self) -> None:
        """Terminate systemd-inhibit and remove the PID file."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
            self._log("Stopped systemd-inhibit - system can sleep normally")
        except Exception:
            try:
                self._process.kill()
                self._log("Force killed systemd-inhibit process")
            except Exception:
                pass
        finally:
            self._process = None
            try:
                os.remove(self.pid_file)
            except OSError:
                pass


class LogindPowerHandler:
    """Linux's sleep and wake for the daemon: a systemd-logind "delay"
    inhibitor on the system bus and the PrepareForSleep signal.

    The interface is onionpress.mac_power.MacPowerHandler's, so the
    daemon registers one or the other by platform and keeps one sleep
    and wake policy (Daemon.handle_sleep / handle_wake):

        handler = LogindPowerHandler(on_will_sleep=..., on_has_powered_on=...,
                                     log=...)
        handler.register()        -> bool (False: no D-Bus, no logind)
        handler.run_loop_once(s)  -> False (the GLib loop runs in its own
                                     thread; the caller sleeps itself)
        handler.stop()            -> release the inhibitor, end the loop

    PrepareForSleep(true) is broadcast before the kernel freezes
    processes, and the delay inhibitor keeps the suspend from completing
    until the fd is closed, for at most logind's InhibitDelayMaxSec
    (5 s by default): `on_will_sleep` runs synchronously inside that
    window, then the fd is released whatever happened. OFFLINE_MAX_TIME
    is the share of it a /offline through `docker exec curl` may take
    (a cold SOCKS5 + Tor rendezvous takes 1-2 s; the watchdog signals
    before it ~0.1 s each). NetworkManager tears the WiFi down on the
    same signal: the race is inherent to the Linux desktop sleep flow,
    and the existing circuits to the hub usually survive long enough.
    PrepareForSleep(false) re-acquires the inhibitor for the next cycle
    and runs `on_has_powered_on`.

    Without python3-dbus or python3-gi, or off a logind system, register()
    returns False and the daemon notices a suspend by its clock gap only.
    """

    OFFLINE_MAX_TIME = 3

    def __init__(self, on_will_sleep, on_has_powered_on, log=print):
        self._on_will_sleep = on_will_sleep
        self._on_has_powered_on = on_has_powered_on
        self._log = log
        self._inhibitor_fd = None
        self._bus = None
        self._login_obj = None
        self._loop = None
        self._thread = None
        self._dbus = None
        self._glib = None

    def register(self) -> bool:
        """Take the inhibitor, subscribe to PrepareForSleep and run a GLib
        main loop in a thread. True when the signal will be heard."""
        try:
            import dbus
            import dbus.mainloop.glib
            from gi.repository import GLib
        except ImportError as e:
            self._log(f"LogindPowerHandler: dbus/glib not importable ({e}); "
                      "sleep is noticed by the clock gap only")
            return False
        self._dbus, self._glib = dbus, GLib
        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
            self._login_obj = self._bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1")
            if not self._acquire():
                return False
            self._bus.add_signal_receiver(
                self._on_prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
                path="/org/freedesktop/login1")
            self._loop = GLib.MainLoop()
            self._thread = threading.Thread(target=self._loop.run, daemon=True,
                                            name="logind-power")
            self._thread.start()
            self._log("LogindPowerHandler: watching logind PrepareForSleep (delay-inhibitor active)")
            return True
        except Exception as e:
            self._log(f"LogindPowerHandler: register failed: {e}")
            return False

    def run_loop_once(self, seconds: float) -> bool:
        """The GLib loop runs in its own thread: nothing to turn here."""
        return False

    def stop(self) -> None:
        """Release the inhibitor and end the loop (the daemon's shutdown)."""
        self._release()
        loop, self._loop = self._loop, None
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass

    def _acquire(self) -> bool:
        if self._inhibitor_fd is not None:
            return True
        try:
            manager = self._dbus.Interface(self._login_obj, "org.freedesktop.login1.Manager")
            fd_obj = manager.Inhibit("sleep", "OnionPress",
                                     "Notify the OnionHeaven hub before suspend", "delay")
            self._inhibitor_fd = fd_obj.take()
            return True
        except Exception as e:
            self._log(f"LogindPowerHandler: Inhibit() failed: {e}")
            self._inhibitor_fd = None
            return False

    def _release(self) -> None:
        fd, self._inhibitor_fd = self._inhibitor_fd, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _on_prepare_for_sleep(self, going_to_sleep) -> None:
        """The signal, on the GLib thread. Going to sleep: the callback
        inside the delay window, then the fd is released, always. Waking:
        the inhibitor is taken again first, so the next sleep is held too."""
        if bool(going_to_sleep):
            self._log("LogindPowerHandler: PrepareForSleep(true)")
            try:
                self._on_will_sleep()
            except Exception as e:
                self._log(f"LogindPowerHandler: on_will_sleep raised: {e}")
            finally:
                self._release()
                self._log("LogindPowerHandler: delay lock released, suspend can proceed")
            return
        self._log("LogindPowerHandler: PrepareForSleep(false)")
        self._acquire()
        try:
            self._on_has_powered_on()
        except Exception as e:
            self._log(f"LogindPowerHandler: on_has_powered_on raised: {e}")
