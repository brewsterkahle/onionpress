"""The client side of `onionpress daemon` (issue #475): what a view reads
and the agent's controls (launchd on a Mac, the systemd user unit on
Linux, issue #407), without the daemon's loops.

The menubar (src/menubar.py) is a view of the daemon's state file
(CLAUDE.md, "The Mac Daemon"); it needs the state file's path and reader,
its freshness and display state, the daemon's pid and the agent's
launchctl state, and nothing of onionpress.daemon's loops. This module
imports none of them (health, docker, onion_proxy, thumbnails, power,
onionheaven, ...), so a process that imports it loads none either:
tests/test_mac_daemon.py's TestMenubarIsAView checks the closure.
onionpress.daemon re-exports every name here, so `daemon.read_state` and
the rest keep working for the CLI, the uninstaller and the tests.
"""

import hashlib
import json
import os
import plistlib
import pwd
import signal
import subprocess
import time

STATE_FILENAME = "daemon-state.json"
PID_FILENAME = "daemon.pid"
# The agent of the account's own ~/.onionpress; any other data dir's is
# derived from it by agent_label() (issue #505). e2e/harness/safety.py
# reads this literal.
AGENT_LABEL = "org.onionpress.daemon"
# Linux: the package's systemd user unit (linux/onionpress-daemon.service);
# systemd_unit() derives any other data dir's name the same way.
SYSTEMD_UNIT = "onionpress-daemon"
ONIONNAME_RETRY_MARKER = ".onionname-retry"
OFFLINE_ACK_MARKER = ".daemon-offline-ack"

# A state file older than this is a daemon that is gone or stuck: the
# menubar shows "Reconnecting" (never "Stopped") until a fresh one lands.
# The status tick is at most 30 s apart, and a slow tick (docker exec
# stalls under memory pressure) is reported by the daemon itself through
# last_check_complete.
STALE_AFTER_SECONDS = 90
# check_status has not finished a pass in this long: "stalled".
STALLED_AFTER_SECONDS = 180


# ---------------------------------------------------------------------------
# The state file (read by the menubar; the only coupling between the two)
# ---------------------------------------------------------------------------

def state_path(data_dir):
    return os.path.join(data_dir, STATE_FILENAME)


def pid_path(data_dir):
    return os.path.join(data_dir, PID_FILENAME)


def read_state(data_dir):
    """The daemon's last state, or None (no file, unreadable, not JSON)."""
    try:
        with open(state_path(data_dir), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def daemon_pid(data_dir):
    """The pid in daemon.pid when that process is alive, else None."""
    try:
        with open(pid_path(data_dir), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def state_is_fresh(state, now=None, max_age=STALE_AFTER_SECONDS):
    """A state the menubar may show: written recently by a live process."""
    if not state:
        return False
    now = time.time() if now is None else now
    try:
        age = now - float(state.get("updated_at") or 0)
    except (TypeError, ValueError):
        return False
    if age > max_age:
        return False
    return pid_alive(state.get("pid"))


def display_state_of(state, now=None):
    """One of 'stopped', 'available', 'offline', 'stalled', 'stuck',
    'starting', from a state document (the menubar's icon and title, and
    the daemon's own poll interval). 'reconnecting' is the menubar's own
    verdict for no fresh state at all, never this function's."""
    now = time.time() if now is None else now
    if not state or not state.get("is_running"):
        return "stopped"
    # A pass has not completed in 3+ minutes (a docker exec hung under
    # memory pressure): a stale "ready" is not to be trusted.
    try:
        if now - float(state.get("last_check_complete") or now) > STALLED_AFTER_SECONDS:
            return "stalled"
    except (TypeError, ValueError):
        pass
    if state.get("is_ready"):
        return "available"
    if not state.get("has_internet", True):
        return "offline"
    yellow_since = state.get("yellow_since")
    try:
        if yellow_since and now - float(yellow_since) > 300:
            return "stuck"
    except (TypeError, ValueError):
        pass
    return "starting"


# ---------------------------------------------------------------------------
# The launchd agent
# ---------------------------------------------------------------------------

def default_data_dir():
    """The account's own ~/.onionpress, from the passwd database: never
    $HOME, which a test install or a developer's shell overrides."""
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, ".onionpress")


def data_dir_label(base, data_dir=""):
    """A launchd label for *data_dir*'s install, under *base* (issue #505).

    launchd's gui domain is per user, not per install: a second OnionPress
    on the same macOS account (a test install with its own HOME, a dev
    build, a QA node) with the same label would take the real install's
    agent for its own, kickstart it or replace it. So the account's own
    ~/.onionpress (default_data_dir, the passwd home) keeps *base*, and any
    other data dir gets `<base>.<8 hex of sha256(its real path)>`: the
    same path always gives the same label, two paths never share one. ''
    means the default."""
    if not data_dir:
        return base
    real = os.path.realpath(data_dir)
    if real == os.path.realpath(default_data_dir()):
        return base
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{base}.{digest}"


def agent_label(data_dir=""):
    """The daemon's launchd label for *data_dir* (data_dir_label)."""
    return data_dir_label(AGENT_LABEL, data_dir)


def systemd_unit(data_dir=""):
    """The daemon's systemd user unit for *data_dir*'s install: the
    package's onionpress-daemon.service for the account's own
    ~/.onionpress, `onionpress-daemon.<8 hex>.service` for any other
    (data_dir_label, issue #505: the user manager is per user, not per
    install). No unit file ships for a derived name: `daemon start` says
    so, and a terminal's `onionpress daemon` serves such an install."""
    return data_dir_label(SYSTEMD_UNIT, data_dir) + ".service"


def _on_linux():
    """Whether this is the systemd side (os.uname: this module imports
    nothing of onionpress, see the docstring)."""
    try:
        return os.uname().sysname == "Linux"
    except (AttributeError, OSError):
        return False


def _systemctl(args, timeout=60):
    """`systemctl --user ARGS`; a CompletedProcess with rc 1 when it
    could not run."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 1, "", str(e))


def unit_loaded_pid(data_dir=""):
    """(loaded, pid) from `systemctl --user show` of the install's unit:
    loaded when the manager knows a unit file by that name, pid its
    MainPID when running (None between restarts, or not running)."""
    r = _systemctl(["show", "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID",
                    systemd_unit(data_dir)])
    if r.returncode != 0:
        return False, None
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    loaded = props.get("LoadState", "").strip() == "loaded"
    try:
        pid = int(props.get("MainPID", "0").strip() or 0)
    except ValueError:
        pid = 0
    return loaded, (pid if pid > 0 else None)


def agent_plist_path(data_dir=""):
    return os.path.expanduser(f"~/Library/LaunchAgents/{agent_label(data_dir)}.plist")


def agent_plist(launcher_script, logs_dir, port_offset_env=None, label=AGENT_LABEL):
    """The LaunchAgent: `<launcher> daemon`, kept alive across crashes
    (SuccessfulExit false: a clean exit 0, which `daemon stop`'s TERM and
    a `quit` produce, stays down; a crash or a kill -9 is restarted after
    ThrottleInterval), started at login (RunAtLoad: the login item opens
    the menubar, which would start it anyway), per user (gui domain;
    each user's own ~/.onionpress and port offset). AbandonProcessGroup:
    an `onionpress update` the daemon's own tick started stops the daemon
    before the bundle swap and must outlive it; so must a Settings-page
    restore in flight when the daemon is restarted."""
    plist = {
        "Label": label,
        "ProgramArguments": [launcher_script, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "AbandonProcessGroup": True,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": os.path.join(logs_dir, "daemon-stdout.log"),
        "StandardErrorPath": os.path.join(logs_dir, "daemon-stderr.log"),
        # launchd sets HOME itself; spelled out so the agent of a test
        # install (HOME under /tmp) never reaches the user's ~/.onionpress.
        "EnvironmentVariables": {"ONIONPRESS_DAEMON_LAUNCHD": "1",
                                 "HOME": os.path.expanduser("~")},
    }
    if port_offset_env is not None:
        plist["EnvironmentVariables"]["ONIONPRESS_PORT_OFFSET"] = str(port_offset_env)
    return plist


def _gui_domain():
    return f"gui/{os.getuid()}"


def _launchctl(args, timeout=20):
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(["launchctl", *args], 1, "", str(e))


def agent_loaded_pid(data_dir=""):
    """(loaded, pid) from `launchctl print gui/<uid>/<label>`: pid None
    when loaded but not running (between KeepAlive restarts). The label is
    *data_dir*'s (agent_label). On Linux: the unit's (unit_loaded_pid)."""
    if _on_linux():
        return unit_loaded_pid(data_dir)
    r = _launchctl(["print", f"{_gui_domain()}/{agent_label(data_dir)}"])
    if r.returncode != 0:
        return False, None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                return True, int(line.split("=", 1)[1].strip())
            except ValueError:
                return True, None
    return True, None


def install_agent(launcher_script, data_dir, log, path="", port_offset=None):
    """Write the plist (rewritten when its program or logs changed: an
    update moves nothing, since the bundle path stays, but a bundle run
    from somewhere else must point the agent at itself). Returns the
    path, or '' when there is no launcher to run."""
    path = path or agent_plist_path(data_dir)
    if not launcher_script or not os.path.isfile(launcher_script):
        log(f"daemon: LaunchAgent not installed: no launcher at {launcher_script or 'none'}")
        return ""
    logs_dir = os.path.join(data_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    wanted = agent_plist(launcher_script, logs_dir, port_offset, label=agent_label(data_dir))
    try:
        with open(path, "rb") as f:
            if plistlib.load(f) == wanted:
                return path
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        plistlib.dump(wanted, f)
    os.replace(tmp, path)
    log(f"daemon: LaunchAgent written ({path})")
    return path


def _start_unit(data_dir, log):
    """Linux: `systemctl --user start --no-block` of the install's unit
    (--no-block: the launcher's `start` runs inside onionpress.service,
    which the unit is ordered before). True when the daemon runs
    afterwards; a data dir without a unit file is said and False."""
    unit = systemd_unit(data_dir)
    loaded, pid = unit_loaded_pid(data_dir)
    if pid:
        return True
    if not loaded:
        log(f"daemon: no systemd user unit {unit} (another install's data dir, or no "
            "user manager): run `onionpress daemon` to serve it")
        return False
    r = _systemctl(["start", "--no-block", unit])
    if r.returncode != 0:
        log(f"daemon: systemctl --user start {unit} failed (rc {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()}")
        return False
    for _ in range(20):
        loaded, pid = unit_loaded_pid(data_dir)
        if pid:
            log(f"daemon: started under systemd ({unit}, pid {pid})")
            return True
        time.sleep(0.25)
    log(f"daemon: systemd accepted the start of {unit} but it is not running yet")
    return loaded


def start_agent(launcher_script, data_dir, log, path="", port_offset=None):
    """Install the plist, bootstrap it (or kickstart a loaded one that is
    not running). True when the agent is running afterwards. On Linux:
    the user unit (_start_unit)."""
    if _on_linux():
        return _start_unit(data_dir, log)
    path = install_agent(launcher_script, data_dir, log, path, port_offset)
    if not path:
        return False
    target = f"{_gui_domain()}/{agent_label(data_dir)}"
    loaded, pid = agent_loaded_pid(data_dir)
    if loaded and pid:
        return True
    if loaded:
        r = _launchctl(["kickstart", target])
    else:
        r = _launchctl(["bootstrap", _gui_domain(), path])
        if r.returncode != 0 and "already" in (r.stderr or "").lower():
            r = _launchctl(["kickstart", target])
    if r.returncode != 0:
        log(f"daemon: launchctl failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()}")
        return False
    for _ in range(20):
        loaded, pid = agent_loaded_pid(data_dir)
        if pid:
            log(f"daemon: started under launchd (pid {pid})")
            return True
        time.sleep(0.25)
    log("daemon: launchd accepted the agent but it is not running yet")
    return loaded


QUITTING_MARKER = ".daemon-quitting"


def stop_agent(data_dir, log, path="", wait=30, offline=True):
    """Boot the agent out (TERM: the daemon stops the proxy, the captures
    and caffeinate and exits 0) and wait for the process to be gone. With
    *offline* (a `quit`: the site is going down with the daemon) the
    daemon tells the hub /offline first; `update` passes False, since it
    comes right back. True when no daemon is left running. The plist
    stays: `start` bootstraps it again."""
    path = path or agent_plist_path(data_dir)
    pid = daemon_pid(data_dir)
    marker = os.path.join(data_dir, QUITTING_MARKER)
    try:
        if offline and pid:
            with open(marker, "w") as f:
                f.write(str(pid))
        elif os.path.exists(marker):
            os.remove(marker)
    except OSError:
        pass
    loaded, agent_pid = agent_loaded_pid(data_dir)
    if _on_linux() and agent_pid:
        # The unit's stop (TERM, TimeoutStopSec): a `systemctl stop` is
        # not restarted by Restart=; the pid is waited for below too.
        r = _systemctl(["stop", systemd_unit(data_dir)], timeout=wait + 30)
        if r.returncode != 0:
            log(f"daemon: systemctl --user stop failed (rc {r.returncode}): "
                f"{(r.stderr or r.stdout).strip()}")
    elif loaded and not _on_linux():
        r = _launchctl(["bootout", f"{_gui_domain()}/{agent_label(data_dir)}"])
        if r.returncode != 0:
            log(f"daemon: launchctl bootout failed (rc {r.returncode}): "
                f"{(r.stderr or r.stdout).strip()}")
    elif pid:
        # Running outside launchd (a terminal's `onionpress daemon`).
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + wait
    for p in (pid, agent_pid):
        while p and pid_alive(p) and time.monotonic() < deadline:
            time.sleep(0.25)
    left = [p for p in (pid, agent_pid) if p and pid_alive(p)]
    if left:
        log(f"daemon: still running after {wait}s (pid {left[0]})")
        return False
    log("daemon: stopped")
    return True


def remove_agent(log, path="", data_dir=""):
    """Boot the agent out and delete its plist (uninstall). On Linux the
    unit file is the package's: the unit is stopped, nothing deleted."""
    if _on_linux():
        _loaded, pid = unit_loaded_pid(data_dir)
        if pid:
            _systemctl(["stop", systemd_unit(data_dir)])
        return
    path = path or agent_plist_path(data_dir)
    loaded, _pid = agent_loaded_pid(data_dir)
    if loaded:
        _launchctl(["bootout", f"{_gui_domain()}/{agent_label(data_dir)}"])
    if os.path.exists(path):
        os.remove(path)
        log("daemon: LaunchAgent removed")


def agent_status(data_dir):
    """What `onionpress daemon status` prints."""
    loaded, pid = agent_loaded_pid(data_dir)
    state = read_state(data_dir)
    now = time.time()
    doc = {
        "loaded": loaded,
        "pid": pid or daemon_pid(data_dir),
        "state_file": state_path(data_dir),
        "state_fresh": state_is_fresh(state, now),
    }
    if _on_linux():
        doc["label"] = doc["unit"] = systemd_unit(data_dir)
    else:
        doc.update({
            "label": agent_label(data_dir),
            "plist": agent_plist_path(data_dir),
            "plist_installed": os.path.exists(agent_plist_path(data_dir)),
        })
    if state:
        doc["state_age_s"] = int(now - float(state.get("updated_at") or now))
        doc["display_state"] = display_state_of(state, now)
        doc["onion_address"] = state.get("onion_address") or ""
        doc["ports"] = state.get("ports") or {}
    return doc


def request_offline(data_dir, log, wait=10):
    """`onionpress daemon offline` (the launcher's `stop`): USR1 to the
    daemon, which tells the hub /offline while the tor container is still
    up, then wait for its acknowledgement. True when acknowledged; False
    when no daemon runs or it did not answer in time (the stop goes on)."""
    pid = daemon_pid(data_dir)
    if not pid:
        return False
    ack = os.path.join(data_dir, OFFLINE_ACK_MARKER)
    try:
        os.remove(ack)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as e:
        log(f"daemon: could not signal pid {pid}: {e}")
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if os.path.exists(ack):
            try:
                os.remove(ack)
            except OSError:
                pass
            return True
        time.sleep(0.2)
    log(f"daemon: no /offline acknowledgement from pid {pid} in {wait}s")
    return False
