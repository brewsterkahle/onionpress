#!/usr/bin/env python3
"""
OnionHeaven — failover system for OnionPress

When an OnionPress instance's .onion address goes offline, OnionHeaven takes over
the address and serves 302 redirects to the Internet Archive Wayback Machine.

This module handles the registration client, periodic heartbeat (keeps OnionHeaven
informed that this instance is alive), and onionheaven mode detection for the
menubar UI.

The heartbeat monitor (takeover decisions, post-takeover auditing) runs inside the
onionheaven container as onionheaven-heartbeat.py — not in this module.
"""

import base64
import contextlib
import fcntl
import json
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone

from onionpress import key_manager

# The default hub (run by the Internet Archive), and what a malformed
# ONIONHEAVEN_ADDRESS falls back to.
DEFAULT_ONIONHEAVEN_ADDRESS = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"

# OnionHeaven hub address — updated from config at startup, through
# configured_hub_address()
ONIONHEAVEN_ADDRESS = DEFAULT_ONIONHEAVEN_ADDRESS

# Registration API port (served by web-server.py in the onionheaven container)
ONIONHEAVEN_API_PORT = 8083

# Wayback Machine .onion address
WAYBACK_ONION = "web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# Paths inside containers: the hub data volume (onionpress-onionheaven-data),
# which the WordPress container never mounts
ONIONHEAVEN_DATA_DIR = "/var/lib/onionheaven"


def configured_hub_address(value, log_func=None):
    """The hub to register with, for ONIONHEAVEN_ADDRESS=*value*.

    Empty means the default hub. Anything else must be a whole, well-formed
    v3 onion address (key_manager.is_valid_onion_address), not merely end
    in ".onion": the address goes into the URL every /online is POSTed to,
    and /online carries the site's onion private key, so a value such as
    "evil.example/" + 39 characters + ".onion" would send the key to a
    clearnet host through a Tor exit. A malformed value is logged by key
    name only and the default hub is used, so the key never goes
    anywhere the user did not agree to.
    """
    value = (value or "").strip()
    if not value:
        return DEFAULT_ONIONHEAVEN_ADDRESS
    if key_manager.is_valid_onion_address(value):
        return value
    if log_func:
        log_func("OnionHeaven: ONIONHEAVEN_ADDRESS in the config is not a valid "
                 "onion address; using the default hub")
    return DEFAULT_ONIONHEAVEN_ADDRESS


def _hub_url(path, hub=None):
    """http://<hub>:<port>/<path>, or None when the hub is not a well-formed
    onion address.

    *hub* defaults to ONIONHEAVEN_ADDRESS. Every request to a hub is built
    here. ONIONHEAVEN_ADDRESS is a module global that callers assign, so it
    is checked where it is used, not only where the config is read.
    """
    if hub is None:
        hub = ONIONHEAVEN_ADDRESS
    if not key_manager.is_valid_onion_address(hub):
        return None
    return f"http://{hub}:{ONIONHEAVEN_API_PORT}/{path}"


def _docker_env(app):
    """Build environment dict for docker commands using app's config.

    The menubar app talks to Colima's socket; an app object that names its
    own *docker_socket* (HubContext: the CLI on either platform, where
    Linux uses the system or rootless daemon) gets that one instead.
    """
    env = os.environ.copy()
    socket_path = (getattr(app, "docker_socket", None)
                   or f"{app.colima_home}/default/docker.sock")
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["DOCKER_CONFIG"] = os.path.join(app.app_support, "docker-config")
    return env


def _docker_bin(app):
    """Return path to docker binary: the bundled one in app.bin_dir, or
    "docker" from PATH when there is no bundle (Linux)."""
    bin_dir = getattr(app, "bin_dir", "") or ""
    if not bin_dir:
        return "docker"
    bundled = os.path.join(bin_dir, "docker")
    if getattr(app, "docker_socket", None) and not os.path.isfile(bundled):
        return "docker"
    return bundled


def _run_docker(app, args, timeout=15, input=None):
    """Run a docker command and return (success, stdout).
    *input*, if given, is written to the command's stdin."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args, input=input,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            env=_docker_env(app)
        )
        if result.returncode != 0 and result.stderr.strip():
            app.log(f"OnionHeaven: docker cmd failed (rc={result.returncode}): {result.stderr.strip()[:200]}")
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        app.log(f"OnionHeaven: docker cmd timed out after {timeout}s: {' '.join(str(a) for a in args[:4])}")
        return False, ""
    except Exception as e:
        app.log(f"OnionHeaven: docker cmd error: {e}")
        return False, ""


def _run_docker_raw(app, args, timeout=15):
    """Run a docker command returning raw bytes (for key extraction)."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode == 0, result.stdout
    except Exception:
        return False, b""


def _run_docker_rc(app, args, timeout=15, input=None):
    """Run a docker command and return (returncode, stdout).
    Unlike _run_docker, returns the actual return code rather than a bool.
    *input*, if given, is written to the command's stdin."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args, input=input,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        app.log(f"OnionHeaven: docker cmd timed out after {timeout}s: {' '.join(str(a) for a in args[:4])}")
        return -1, ""
    except Exception as e:
        app.log(f"OnionHeaven: docker cmd error: {e}")
        return -1, ""


# ---------------------------------------------------------------------------
# Registration choice (on by default)
# ---------------------------------------------------------------------------

# REGISTER_WITH_ONIONHEAVEN defaults to on:
#   "yes" or unset — register and heartbeat. Every new install takes part
#            unless the user turns it off; nothing asks first.
#   "no"   — the user opted out: never register, and if this install
#            registered before, ask the hub to delete its copy of the key
#            (a signed /unregister; see maybe_unregister_after_opt_out).
#   anything else (a typo) — neither: nothing is sent to the hub, not even
#            an /unregister, so a mistyped "no" never registers.
# Whatever the choice, an explicit end of the site's relationship with a
# hub takes the key back off it while the key is still here to sign the
# request: uninstall, reset, a key import, a restore to another address
# (release_registration) and a hub change (maintain_registration).
# Registering sends the site's onion private key to the hub. The choice is
# re-read before every request that carries the key, so turning it off
# stops the heartbeats at once, without a restart.


def registration_choice(value):
    """Normalize a REGISTER_WITH_ONIONHEAVEN config value.

    Returns "yes", "no", or None (unrecognized). A missing or empty value
    is "yes": OnionHeaven is on by default. Anything else that is not an
    explicit yes/no is None, so a typo neither registers nor unregisters.
    """
    if value is None:
        return "yes"
    v = str(value).strip().lower()
    if v == "":
        return "yes"
    if v in ("yes", "true", "1", "on"):
        return "yes"
    if v in ("no", "false", "0", "off"):
        return "no"
    return None


def get_registration_choice(app):
    """The user's current OnionHeaven choice from the config file."""
    return registration_choice(app.read_config_value("REGISTER_WITH_ONIONHEAVEN", ""))


def _choice_label(choice):
    """REGISTER_WITH_ONIONHEAVEN as a log line shows it."""
    return choice if choice in ("yes", "no") else "unrecognized"


# Every request that carries the key (/online), every /unregister and every
# change to the registration record holds this lock, and /online re-reads
# the choice inside it. A heartbeat already on its way when the user opts
# out therefore lands before the /unregister, never after it: an /online
# arriving after an /unregister would put the key back on the hub while
# this install's record says it is gone. _hub_lock() takes it together
# with an flock that orders the same requests between processes: the
# menubar's heartbeats and a reset, key import or restore run by the
# launcher (onionpress.cli).
_HUB_LOCK = threading.RLock()

# In ~/.onionpress: whichever OnionPress process is talking to a hub or
# changing the registration record holds an flock on it.
HUB_LOCK_FILE = "onionheaven-hub.lock"

# How long a heartbeat waits for another process's /unregister before it
# skips its beat, and how long an /unregister waits for another process's
# heartbeat (one /online with its retry and landing check takes ~100 s).
HEARTBEAT_LOCK_TIMEOUT = 10
RELEASE_LOCK_TIMEOUT = 300


class HubBusy(Exception):
    """Another OnionPress process held the hub lock longer than we wait."""


_flock_state = {"depth": 0, "fd": None}  # guarded by _HUB_LOCK


def _acquire_hub_flock(app, timeout):
    """An exclusive flock on HUB_LOCK_FILE: its fd, None when the lock file
    cannot be used (going on without it, as before it existed), or HubBusy
    after *timeout* seconds."""
    path = os.path.join(app.app_support, HUB_LOCK_FILE)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        app.log(f"OnionHeaven: cannot open the hub lock ({e.strerror or e}); "
                "going on without it")
        return None
    deadline = time.monotonic() + timeout
    # Not time.sleep: tests stub it out, and this must really wait.
    waiter = threading.Event()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise HubBusy()
            waiter.wait(0.2)
        except OSError as e:
            os.close(fd)
            app.log(f"OnionHeaven: cannot lock the hub lock ({e.strerror or e}); "
                    "going on without it")
            return None


@contextlib.contextmanager
def _hub_lock(app, timeout=RELEASE_LOCK_TIMEOUT):
    """_HUB_LOCK plus the cross-process flock (taken by the outermost
    holder only: _HUB_LOCK is re-entrant). Raises HubBusy if another
    process keeps the flock for more than *timeout* seconds."""
    with _HUB_LOCK:
        outer = _flock_state["depth"] == 0
        if outer:
            _flock_state["fd"] = _acquire_hub_flock(app, timeout)
        _flock_state["depth"] += 1
        try:
            yield
        finally:
            _flock_state["depth"] -= 1
            if outer:
                fd, _flock_state["fd"] = _flock_state["fd"], None
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)


# ---------------------------------------------------------------------------
# The registration record
# ---------------------------------------------------------------------------
#
# ~/.onionpress/onionheaven-registration.json, read and written by the Mac
# app, the CLI on both platforms (onionpress.cli onionheaven-unregister,
# reset, restore) and the Linux heartbeat daemon:
#
#   "registered": true   the hub "onionheaven_address" may hold the key of
#                        "content_address": set by the first /online a hub
#                        takes, cleared only by an /unregister it answered.
#   "left_on_hubs"       registrations not taken back (yet), each
#                        {"content_address", "onionheaven_address", "since",
#                        "reason"}: an earlier hub after a hub change, or an
#                        /unregister the hub did not answer before a reset,
#                        key import, restore or uninstall went ahead. One for
#                        this install's current address is retried (its key
#                        is here to sign the request); one for an address
#                        whose key this install no longer has is reported:
#                        nothing can ask that hub to delete it any more.
#   "released"           {"content_address", "until"}: that address's key is
#                        being given up and its hubs answered the /unregister,
#                        so no /online may put it back before "until" (another
#                        OnionPress process could otherwise send one between
#                        the /unregister and the key's removal).

REGISTRATION_RECORD = "onionheaven-registration.json"
RELEASE_HOLD_SECONDS = 10 * 60
_RECORD_CARRIED = ("left_on_hubs", "released")


def _registration_status_file(app):
    """Path to the onionheaven registration status file (the record above)."""
    return os.path.join(app.app_support, REGISTRATION_RECORD)


def _load_registration_status(app):
    """Load persisted registration status."""
    path = _registration_status_file(app)
    try:
        with open(path) as f:
            status = json.load(f)
        if isinstance(status, dict):
            return status
    except (OSError, ValueError):
        pass
    return {"registered": False, "last_attempt": None, "onionheaven_address": ONIONHEAVEN_ADDRESS}


def _save_registration_status(app, status):
    """Persist registration status to disk (replacing the file whole).

    left_on_hubs and released are carried over from the record on disk
    unless *status* sets them (None drops one): a write that records a
    registration must not forget what other hubs may still hold.
    """
    path = _registration_status_file(app)
    status = dict(status)
    missing = [key for key in _RECORD_CARRIED if key not in status]
    if missing:
        previous = _load_registration_status(app)
        for key in missing:
            if key in previous:
                status[key] = previous[key]
    for key in _RECORD_CARRIED:
        if status.get(key) is None:
            status.pop(key, None)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, 'w') as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _main_registration(status, app=None):
    """(content_address, hub) of the registration the record names, or
    None. Records from before the address was stored fall back to the
    app's address; an unusable hub to the configured one."""
    if not status.get("registered"):
        return None
    addr = status.get("content_address")
    if not key_manager.is_valid_onion_address(addr):
        addr = getattr(app, "onion_address", None)
        if not key_manager.is_valid_onion_address(addr):
            return None
    hub = status.get("onionheaven_address")
    if not key_manager.is_valid_onion_address(hub):
        hub = ONIONHEAVEN_ADDRESS
    return addr, hub


def _pair(entry):
    return entry["content_address"], entry["onionheaven_address"]


def _left_on_hubs(status):
    """The well-formed left_on_hubs entries of *status*."""
    entries = status.get("left_on_hubs")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict)
            and key_manager.is_valid_onion_address(e.get("content_address"))
            and key_manager.is_valid_onion_address(e.get("onionheaven_address"))]


def _note_left_on_hub(status, addr, hub, reason):
    """Add (addr, hub) to *status*'s left_on_hubs, once."""
    entries = _left_on_hubs(status)
    if not any(_pair(e) == (addr, hub) for e in entries):
        entries.append({"content_address": addr, "onionheaven_address": hub,
                        "since": _now_iso(), "reason": reason})
    status["left_on_hubs"] = entries


def _forget_left_on_hub(status, addr, hub):
    """Drop (addr, hub) from *status*'s left_on_hubs; True if it was there."""
    entries = _left_on_hubs(status)
    kept = [e for e in entries if _pair(e) != (addr, hub)]
    status["left_on_hubs"] = kept
    return len(kept) != len(entries)


def _registrations(status, app=None):
    """Every (content_address, hub) the record says may hold one of this
    install's keys: its registration, then left_on_hubs."""
    pairs = []
    main = _main_registration(status, app)
    if main is not None:
        pairs.append(main)
    for entry in _left_on_hubs(status):
        if _pair(entry) not in pairs:
            pairs.append(_pair(entry))
    return pairs


def registrations_left(app):
    """What the record says hubs may still hold of this install's keys,
    as (content_address, hub) pairs. No network."""
    return _registrations(_load_registration_status(app), app)


def _address_on_hold(app, addr, now=None):
    """True while the record's "released" hold covers *addr*."""
    released = _load_registration_status(app).get("released")
    if not isinstance(released, dict) or released.get("content_address") != addr:
        return False
    until = released.get("until")
    now = time.time() if now is None else now
    return isinstance(until, (int, float)) and now < until


def _key_address():
    """The address of the key in the tor container, or None if unreadable."""
    try:
        _private, public = key_manager.extract_keys()
        return key_manager.derive_onion_address(public)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Registration client (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------


def register_with_onionheaven(app):
    """
    Register this instance with OnionHeaven by starting the heartbeat loop.

    Every /online carries the arti_key_pem (the hub refuses one without
    it), so the first heartbeat is also the registration. Called from a
    background thread; returns when the heartbeat loop ends, which it does
    as soon as REGISTER_WITH_ONIONHEAVEN is no longer "yes".
    Sets app._onionheaven_registration_succeeded on success;
    clears app._onionheaven_registration_in_flight when done.
    """
    # "yes" (or unset, which reads as yes) registers; "no" means the user
    # opted out, and an unrecognized value registers nothing.
    choice = get_registration_choice(app)
    if choice != "yes":
        app.log("OnionHeaven registration not enabled "
                f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
        app._onionheaven_registration_in_flight = False
        return

    # Don't register with ourselves
    if getattr(app, 'is_onionheaven', False):
        app._onionheaven_registration_in_flight = False
        return

    content_addr = app.onion_address
    hc_addr = app.healthcheck_address

    if not key_manager.is_valid_onion_address(content_addr):
        app.log("OnionHeaven: no content address available, skipping registration")
        app._onionheaven_registration_in_flight = False
        return
    if not key_manager.is_valid_onion_address(hc_addr):
        app.log("OnionHeaven: no healthcheck address available, skipping registration")
        app._onionheaven_registration_in_flight = False
        return

    app.log("Registering with OnionHeaven via heartbeat...")

    try:
        # Send initial heartbeat with key (acts as registration)
        wp_healthy = _check_wordpress_healthy(app)
        _send_heartbeat(app, wp_healthy)

        if getattr(app, '_onionheaven_registration_succeeded', False):
            # Start heartbeat loop (runs forever in this thread)
            _heartbeat_loop(app)
            return

        # First attempt failed — retry with backoff, then start heartbeat loop
        backoff = [10, 30]
        last_output = ""

        for attempt, wait in enumerate(backoff):
            app.log(f"OnionHeaven: registration attempt {attempt + 2} in {wait}s...")
            time.sleep(wait)
            choice = get_registration_choice(app)
            if choice != "yes":
                app.log("OnionHeaven: registration stopped "
                        f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
                return
            wp_healthy = _check_wordpress_healthy(app)
            _send_heartbeat(app, wp_healthy)
            if getattr(app, '_onionheaven_registration_succeeded', False):
                _heartbeat_loop(app)
                return

        # Even if registration hasn't succeeded yet, start heartbeat loop —
        # it will keep trying on each cycle
        app.log("OnionHeaven: initial registration attempts failed, continuing heartbeat loop")
        _heartbeat_loop(app)
        return

    except Exception as e:
        app.log(f"OnionHeaven: registration error: {e}")
    finally:
        app._onionheaven_registration_in_flight = False


def start_registration_thread(app):
    """Start the registration + heartbeat background thread.

    Registration happens via the first /online heartbeat. Every /online,
    the first and all later ones, carries the arti key: the hub refuses an
    /online without it.
    """
    thread = threading.Thread(target=register_with_onionheaven, args=(app,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Heartbeat loop (runs on normal OnionPress instances after registration)
# ---------------------------------------------------------------------------

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 60

def _heartbeat_loop(app):
    """Send periodic /online heartbeats to OnionHeaven every 60s.

    Runs in the registration thread after successful registration.
    Sends the same /online payload but adds wordpress_healthy status.
    Re-reads REGISTER_WITH_ONIONHEAVEN before every beat and ends the
    moment it is not "yes" (_send_heartbeat checks it again under the hub
    lock): unticking OnionHeaven in either Settings UI stops the key going
    to the hub without a restart. check_status starts a new loop when the
    choice is "yes" again. Otherwise it runs until the app quits (daemon
    thread) or a newer loop supersedes it (wake).

    Also emits a ~12h system-metrics snapshot line (issue #238) on
    platforms where the menubar isn't driving its own snapshots (i.e.
    Linux). The Mac menubar emits richer snapshots from check_status
    and sleep/wake, so the heartbeat skips emission when it detects
    _maybe_emit_snapshot on the app object.
    """
    # Prevent duplicate heartbeat loops (multiple registration threads
    # can race into this function after wake/restart)
    if getattr(app, '_heartbeat_loop_running', False):
        app.log("OnionHeaven: heartbeat loop already running, skipping duplicate")
        return
    app._heartbeat_loop_running = True
    my_generation = getattr(app, '_heartbeat_generation', 0)

    # Random jitter on first heartbeat to prevent thundering herd
    # after OnionHeaven restart
    jitter = random.uniform(0, 15)
    app.log(f"OnionHeaven: heartbeat loop starting (gen={my_generation}, first beat in {jitter:.0f}s)")
    time.sleep(jitter)

    # Issue #238: heartbeat-driven ~12h snapshot for Linux (no menubar).
    # 720 iterations × 60s ≈ 12h. The Mac menubar runs its own snapshot
    # path from check_status, so skip emission there.
    snapshot_every_n = max(1, int((12 * 3600) / HEARTBEAT_INTERVAL))
    iteration = 0
    drives_own_snapshot = hasattr(app, "_maybe_emit_snapshot")

    while True:
        # Exit if a newer heartbeat loop has been started (after wake)
        if getattr(app, '_heartbeat_generation', 0) != my_generation:
            app.log(f"OnionHeaven: heartbeat loop gen={my_generation} exiting (superseded)")
            return

        # Every beat: the user may have turned OnionHeaven off
        # (Mac Settings, the WordPress Settings page) since the last one.
        choice = get_registration_choice(app)
        if choice != "yes":
            _end_heartbeat_loop(app, my_generation, choice)
            return

        try:
            # Don't send /online while sleeping — let the hub detect us as
            # offline and take over. Power Nap keeps the app running but
            # doesn't trigger wake events, so _sleeping stays True.
            if getattr(app, '_sleeping', False):
                pass  # skip heartbeat while sleeping
            else:
                # Check WordPress health locally before sending heartbeat
                wp_healthy = _check_wordpress_healthy(app)
                _send_heartbeat(app, wp_healthy)
        except Exception as e:
            app.log(f"OnionHeaven: heartbeat error: {e}")

        iteration += 1
        if (not drives_own_snapshot
                and not getattr(app, '_sleeping', False)
                and iteration % snapshot_every_n == 0):
            _emit_heartbeat_snapshot(app)

        time.sleep(HEARTBEAT_INTERVAL)


def _end_heartbeat_loop(app, my_generation, choice):
    """The loop's exit when REGISTER_WITH_ONIONHEAVEN is no longer "yes".

    Clears the session's registration flags, so nothing sends /offline on
    sleep, stop or quit, and so check_status starts a fresh loop if the
    user turns OnionHeaven back on. The /unregister for an explicit "no"
    is maybe_unregister_after_opt_out's, not the loop's: it also covers
    an install that starts with "no", and an unrecognized value sends
    nothing.
    """
    if choice == "no":
        app.log("OnionHeaven: disabled (REGISTER_WITH_ONIONHEAVEN=no) — "
                "heartbeats stopped")
    else:
        app.log("OnionHeaven: REGISTER_WITH_ONIONHEAVEN is not yes or no — "
                "heartbeats stopped; nothing is sent to the hub until it is yes")
    app._onionheaven_registration_succeeded = False
    app._onionheaven_heartbeat_succeeded = False
    # Only this loop's own guard: after a wake a newer loop owns the flag.
    if getattr(app, '_heartbeat_generation', 0) == my_generation:
        app._heartbeat_loop_running = False


def _emit_heartbeat_snapshot(app):
    """System-metrics-only snapshot for the heartbeat path.

    Used on Linux where the menubar app isn't running. Importing the
    metric helpers lazily keeps this module's import surface unchanged.
    """
    try:
        from .system_metrics import host_metrics, container_metrics
        from .reachability_stats import ReachabilityStats
        host = host_metrics()
        docker = getattr(app, '_docker', None)
        ctn = container_metrics(docker) if docker is not None else None
        # Empty stats object: yields a snapshot with no reachability
        # section (heartbeat doesn't track per-probe outcomes).
        stats = ReachabilityStats()
        app.log(stats.format_snapshot(host, ctn))
    except Exception as e:
        app.log(f"OnionHeaven: heartbeat snapshot failed: {e}")


def _check_wordpress_healthy(app):
    """Quick local check if WordPress is responding."""
    try:
        return app._health_checker.check_wordpress_local()
    except Exception:
        return False


def _heartbeat_landed(app, content_addr, freshness_seconds=180):
    """Did an earlier POST /online actually land on the hub, even though
    we timed out waiting for the ACK?

    Calls GET /status/<content_addr> and checks the most-recent
    last_healthy across all registered entries. If that timestamp is
    within *freshness_seconds* of now, the server received and
    processed our heartbeat — our POST timeout was only on the return
    path. Used to distinguish "actually delivered" from "never got
    there" so the log doesn't scream FAILED for transient Tor ACK
    losses. See issue #208.
    """
    if not key_manager.is_valid_onion_address(content_addr):
        return False
    url = _hub_url("status/" + content_addr)
    if url is None:
        return False
    try:
        args = [
            "exec", "onionpress-tor",
            "curl", "-s", "--max-time", "30",
            "--socks5-hostname", "127.0.0.1:9050",
            url,
        ]
        rc, output = _run_docker_rc(app, args, timeout=45)
        if rc != 0 or not output:
            return False
        data = json.loads(output)
        entries = data.get("entries", [])
        if not entries:
            return False
        # We may have multiple entries across healthcheck_address rotations.
        # Take the most recent last_healthy — that's the one that matters.
        max_ts = None
        for e in entries:
            lh = e.get("last_healthy")
            if not lh:
                continue
            try:
                ts = datetime.fromisoformat(lh.replace("Z", "+00:00"))
            except ValueError:
                continue
            if max_ts is None or ts > max_ts:
                max_ts = ts
        if max_ts is None:
            return False
        age = (datetime.now(timezone.utc) - max_ts).total_seconds()
        return age < freshness_seconds
    except Exception:
        return False


def _send_heartbeat(app, wordpress_healthy=True):
    """Send a single /online heartbeat to OnionHeaven.

    Every call includes the arti_key_pem (the hub's /online refuses a
    request without it), so this is the one function that ships the key.
    It sends nothing unless REGISTER_WITH_ONIONHEAVEN is "yes", read under
    the hub lock, whoever calls it (registration, the loop, a wake
    reclaim), and nothing for an address whose key is being given up
    (uninstall here, or the record's "released" hold from a reset, key
    import or restore run by another process).
    """
    try:
        with _hub_lock(app, timeout=HEARTBEAT_LOCK_TIMEOUT):
            return _send_heartbeat_locked(app, wordpress_healthy)
    except HubBusy:
        app.log("OnionHeaven: not sending /online now: another OnionPress "
                "process is asking a hub to delete a key")
        return False


def _send_heartbeat_locked(app, wordpress_healthy):
    if getattr(app, 'is_onionheaven', False):
        return False
    choice = get_registration_choice(app)
    if choice != "yes":
        app.log("OnionHeaven: not sending /online "
                f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
        return False
    if getattr(app, '_onionheaven_ending', False):
        app.log("OnionHeaven: not sending /online (uninstalling)")
        return False

    content_addr = getattr(app, 'onion_address', None)
    hc_addr = getattr(app, 'healthcheck_address', None)

    if not key_manager.is_valid_onion_address(content_addr):
        return False
    if not key_manager.is_valid_onion_address(hc_addr):
        return False
    if _address_on_hold(app, content_addr):
        app.log(f"OnionHeaven: not sending /online for {content_addr}: its key "
                "is being given up and the hub was asked to delete its copy")
        return False
    online_url = _hub_url("online")
    if online_url is None:
        app.log("OnionHeaven: hub address is not a valid onion address, "
                "not sending /online")
        return False

    try:
        from onionpress import onion_auth
        secret_key_bytes, public_key_raw = key_manager.extract_keys()
        # Guard: the keystore key and app.onion_address must agree. They can
        # diverge during a restore — write_private_key updates the keystore
        # in the running container, but self.onion_address (loaded from the
        # ~/.onionpress/onion_address cache at startup) only refreshes when
        # update_status() next polls the container. A heartbeat fired in
        # that window would ship (content=OLD, key=NEW), and OnionHeaven's
        # /online clobbers KEYS_DIR/OLD/ with NEW's bytes — irrecoverable
        # data loss for the old address's takeover key.
        derived = onion_auth.derive_onion_address(public_key_raw)
        if derived != content_addr:
            app.log(f"OnionHeaven: ABORT heartbeat — onion_address stale "
                    f"(app={content_addr}, key_derives_to={derived}); "
                    f"skipping until update_status catches up")
            return False
        timestamp = onion_auth.make_timestamp()
        signature = onion_auth.sign_payload(
            secret_key_bytes, public_key_raw,
            "online", content_addr, hc_addr, timestamp
        )
    except Exception as e:
        app.log(f"OnionHeaven: heartbeat sign error: {e}")
        return False

    payload_dict = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "wordpress_healthy": wordpress_healthy,
        "version": getattr(app, 'version', 'unknown'),
        "is_onionheaven": getattr(app, 'is_onionheaven', False),
        "timestamp": timestamp,
        "signature": signature,
    }

    # Always include the key — every /online is both heartbeat and registration
    try:
        arti_pem = key_manager.build_openssh_key(secret_key_bytes, public_key_raw)
        payload_dict["arti_key_pem"] = base64.b64encode(arti_pem).decode('ascii')
    except Exception as e:
        app.log(f"OnionHeaven: failed to build arti key for heartbeat: {e}")

    payload = json.dumps(payload_dict)

    # The payload carries the onion private key, so it goes to curl on
    # stdin (docker exec -i, --data-binary @-), never in argv: `ps` shows
    # every user's command lines, on macOS and Linux alike.
    curl_args = [
        "exec", "-i", "onionpress-tor",
        "curl", "-s", "-S", "-X", "POST",
        "--socks5-hostname", "127.0.0.1:9050",
        "-H", "Content-Type: application/json",
        "--data-binary", "@-",
        "--max-time", "30",
        online_url
    ]

    had_success = getattr(app, '_onionheaven_heartbeat_succeeded', False)
    if not had_success:
        app.log("OnionHeaven: heartbeat sending /online...")
    rc, output = _run_docker_rc(app, curl_args, timeout=45, input=payload)
    ok = (rc == 0)
    if ok and output:
        try:
            json.loads(output)  # validate JSON
            if not had_success:
                hub = ONIONHEAVEN_ADDRESS[:12]
                app.log(f"OnionHeaven: heartbeat /online {hub}...onion OK")
        except (json.JSONDecodeError, KeyError):
            # Parseable rc=0 but body not JSON; queue retry. Quiet log —
            # we'll decide FAILED vs delivered-no-ack once retry is done.
            app.log(f"OnionHeaven: heartbeat /online bad JSON on first try, retrying")
            ok = False

    # Retry once after 3s on transient Tor HSDir failures. Soft-log the
    # first-attempt miss here (rather than screaming FAILED before we've
    # actually given up) so recovered runs don't leave scary breadcrumbs.
    if not (ok and output):
        if ok:  # we already logged "bad JSON" above
            pass
        else:
            reason = {6: "DNS error", 7: "connection refused", 28: "timed out",
                      52: "empty reply", 56: "connection reset"}.get(rc, "unknown")
            app.log(f"OnionHeaven: heartbeat /online {reason} rc={rc}, retrying in 3s")
        import time
        time.sleep(3)
        rc, output = _run_docker_rc(app, curl_args, timeout=45, input=payload)
        ok = (rc == 0)

    if ok and output:
        try:
            resp = json.loads(output)
            if resp.get("online"):
                _mark_registered(app, content_addr)
                app._onionheaven_heartbeat_succeeded = True
                return True  # success
            app.log(f"OnionHeaven: heartbeat rejected: {resp.get('error', 'unknown')}")
            return False
        except json.JSONDecodeError:
            app.log(f"OnionHeaven: heartbeat got non-JSON response: {output[:200]}")
            # Before declaring failure, check if the POST actually landed —
            # JSONDecodeError often means truncated body on a still-
            # successful server-side write.
            if _heartbeat_landed(app, content_addr):
                hub = ONIONHEAVEN_ADDRESS[:12]
                app.log(f"OnionHeaven: heartbeat /online {hub}...onion delivered (no-ack, response lost on return circuit)")
                _mark_registered(app, content_addr)
                app._onionheaven_heartbeat_succeeded = True
                return True
            return False
    else:
        # Both attempts failed to get a parseable response. Before we
        # log it as FAILED, ask the hub if our heartbeat actually landed
        # — Tor circuits lose ACK packets often enough that "client
        # timed out" regularly means "server received + processed,
        # response never came back." See issue #208.
        if _heartbeat_landed(app, content_addr):
            hub = ONIONHEAVEN_ADDRESS[:12]
            app.log(f"OnionHeaven: heartbeat /online {hub}...onion delivered (no-ack, response lost on return circuit)")
            _mark_registered(app, content_addr)
            app._onionheaven_heartbeat_succeeded = True
            return True
        app.log(f"OnionHeaven: heartbeat failed after retry: curl_rc={rc}, output={repr(output[:200]) if output else 'empty'}")
        return False


def _mark_registered(app, content_addr):
    """The hub took an /online: it holds the key now. Written whenever the
    record does not already say so for this address and hub (the first
    beat of a session, the first after an /unregister, the first after a
    hub or address change), also when only _heartbeat_landed saw the beat
    arrive, so an opt-out or a reset knows to unregister. A registration
    the record named before, for another hub or another address, is not
    dropped: it moves to left_on_hubs. Caller holds the hub lock."""
    hub = ONIONHEAVEN_ADDRESS
    status = _load_registration_status(app)
    main = _main_registration(status, app)
    entries = _left_on_hubs(status)
    if (getattr(app, '_onionheaven_registration_succeeded', False)
            and main == (content_addr, hub)
            and status.get("content_address") == content_addr
            and not any(_pair(e) == (content_addr, hub) for e in entries)):
        return
    app._onionheaven_registration_succeeded = True
    if main is not None and main != (content_addr, hub):
        _note_left_on_hub(status, main[0], main[1], "superseded")
        if main[0] == content_addr:
            app.log(f"OnionHeaven: registered with {hub} now; {main[1]} may "
                    "still hold this site's key and will be asked to delete it")
        else:
            app.log(f"OnionHeaven: WARNING — {main[1]} may still hold the key "
                    f"of {main[0]}, the address this install had before; "
                    "this install no longer has that key, so OnionPress "
                    "cannot ask the hub to delete it")
    _forget_left_on_hub(status, content_addr, hub)
    status.update({
        "registered": True,
        "last_attempt": _now_iso(),
        "onionheaven_address": hub,
        "content_address": content_addr,
    })
    status.pop("unregistered_at", None)
    _save_registration_status(app, status)
    app.log(f"OnionHeaven: registered via heartbeat ({content_addr})")


# The hub's answer to an /unregister for an address it has no row for
# (web-server.py _handle_unregister): nothing of ours is left there.
UNREGISTER_NOT_FOUND = "Entry not found"

# How often an opted-out install retries an /unregister the hub did not
# answer (hub unreachable, Tor trouble), and how often left_on_hubs
# entries for this install's address are retried. The hub takes an
# address over about 10 min after its last heartbeat while it still holds
# the key, and the /unregister that lands releases that takeover.
OPT_OUT_RETRY_SECONDS = 15 * 60


def unregister_from_onionheaven(app, content_address=None, while_choice=None, hub=None):
    """
    Ask a hub to forget this site: a signed /unregister for the whole
    content address (no healthcheck_address, so every registry row of the
    address goes, not just the current healthcheck's). The hub then
    releases any takeover of the address, marks its rows unregistered and
    deletes its copy of the onion service key.

    Without *hub*, the registration the record names (uninstall, and
    maybe_unregister_after_opt_out when the user turns OnionHeaven off:
    *while_choice*="no" drops the request if the choice changes before an
    attempt goes out). With *hub*, that hub's copy of *content_address*'s
    key, which the record must name (its registration or a left_on_hubs
    entry): release_registration and the left_on_hubs retries.

    Runs synchronously — caller should invoke from a background thread if needed.
    Best-effort: logs failures but does not raise. Returns True when the
    hub holds nothing for the address any more (unregistered, or it had no
    entry), False when it may still (retry later), None when there is
    nothing this install can do (never registered, the key changed, no
    usable hub address, the choice changed).
    """
    # Don't unregister OnionHeaven from itself
    if getattr(app, 'is_onionheaven', False):
        return None

    status = _load_registration_status(app)
    main = _main_registration(status, app)
    if hub is None:
        if main is None:
            app.log("OnionHeaven: no content address for unregister"
                    if status.get("registered") else
                    "OnionHeaven: not registered, skipping unregister")
            return None
        addr = content_address or main[0]
        hub_addr = main[1]
    else:
        addr = content_address or (main[0] if main else None)
        hub_addr = hub
        if (addr, hub_addr) not in _registrations(status, app):
            app.log("OnionHeaven: not registered, skipping unregister")
            return None

    if not key_manager.is_valid_onion_address(addr):
        app.log("OnionHeaven: no content address for unregister")
        return None
    unregister_url = _hub_url("unregister", hub_addr)
    if unregister_url is None:
        app.log("OnionHeaven: hub address is not a valid onion address, "
                "not sending /unregister")
        return None

    # Sign the unregister request with ed25519
    try:
        from onionpress import onion_auth
        secret_key_bytes, public_key_raw = key_manager.extract_keys()
    except Exception as e:
        app.log(f"OnionHeaven: failed to extract keys for unregister: {e}")
        return False

    # Same staleness guard as _send_heartbeat / _send_onionheaven_notification:
    # refuse to unregister an address that doesn't match the live key. addr
    # comes from the record (or app.onion_address), either of which can
    # lag a key change, and only the live key can sign the request.
    derived = onion_auth.derive_onion_address(public_key_raw)
    if derived != addr:
        app.log(f"OnionHeaven: ABORT unregister — content_address stale "
                f"(app={addr}, key_derives_to={derived})")
        return None

    # Retry with backoff — uninstall and opt-out are one-shot, reliability > speed
    backoff = [5, 15, 30]
    max_attempts = 4
    last_output = ""

    # The lock orders this after any heartbeat already on its way, in this
    # process or another (see _HUB_LOCK): an /online landing after the
    # /unregister would put the key back.
    try:
        with _hub_lock(app):
            app.log(f"Unregistering {addr} from OnionHeaven {hub_addr}...")
            for attempt in range(max_attempts):
                if while_choice is not None:
                    choice = get_registration_choice(app)
                    if choice != while_choice:
                        app.log("OnionHeaven: unregister dropped "
                                f"(REGISTER_WITH_ONIONHEAVEN is now {_choice_label(choice)})")
                        return None

                # Signed per attempt: the hub accepts a timestamp up to 5 min
                # old, and four attempts with backoff can take longer.
                timestamp = onion_auth.make_timestamp()
                signature = onion_auth.sign_payload(
                    secret_key_bytes, public_key_raw,
                    "unregister", addr, "", timestamp
                )
                payload = json.dumps({
                    "content_address": addr,
                    "timestamp": timestamp,
                    "signature": signature,
                })

                # Payload on stdin, like the heartbeat's: a signed request stays
                # out of argv.
                ok, output = _run_docker(app, [
                    "exec", "-i", "onionpress-tor",
                    "curl", "-s", "-X", "POST",
                    "--socks5-hostname", "127.0.0.1:9050",
                    "-H", "Content-Type: application/json",
                    "--data-binary", "@-",
                    "--max-time", "60",
                    unregister_url
                ], timeout=75, input=payload)
                last_output = output

                if ok and output:
                    try:
                        resp = json.loads(output)
                    except json.JSONDecodeError:
                        resp = None
                    if isinstance(resp, dict):
                        error_msg = resp.get("error")
                        if resp.get("unregistered") or error_msg == UNREGISTER_NOT_FOUND:
                            if resp.get("unregistered"):
                                app.log(f"OnionHeaven: unregistered successfully from {hub_addr}")
                            else:
                                app.log(f"OnionHeaven: {hub_addr} holds no entry for "
                                        f"{addr}; nothing to unregister")
                            _record_unregistered(app, addr, hub_addr)
                            return True
                        app.log(f"OnionHeaven: unregister rejected: {error_msg or 'unknown error'}")
                        # Don't retry on auth/validation errors — they won't fix themselves
                        if error_msg:
                            return False

                if attempt < max_attempts - 1:
                    delay = backoff[min(attempt, len(backoff) - 1)]
                    app.log(f"OnionHeaven: unregister attempt {attempt + 1} failed, retrying in {delay}s...")
                    time.sleep(delay)
    except HubBusy:
        app.log("OnionHeaven: another OnionPress process kept the hub lock; "
                "unregister not sent")
        return False

    app.log(f"OnionHeaven: unregister failed after {max_attempts} attempts (best-effort, continuing) — last response: {last_output!r}")
    return False


def _record_unregistered(app, addr, hub):
    """*hub* answered an /unregister for *addr*: drop that pair from the
    record. The registration slot is cleared only when it names that pair
    (it may name a newer hub the site registered with since). Caller holds
    the hub lock."""
    status = _load_registration_status(app)
    if _main_registration(status, app) == (addr, hub):
        status.update({
            "registered": False,
            "unregistered_at": _now_iso(),
            "onionheaven_address": hub,
            "content_address": addr,
        })
        # A later /online (OnionHeaven turned back on) must write the
        # record again.
        app._onionheaven_registration_succeeded = False
    _forget_left_on_hub(status, addr, hub)
    _save_registration_status(app, status)


# ---------------------------------------------------------------------------
# The end of a hub's hold on a key: reset, key import, restore, uninstall
# ---------------------------------------------------------------------------

_WHY_TEXT = {
    "key-unreadable": "OnionPress could not read this site's onion service "
                      "key (is OnionPress running?)",
    "no-key": "this install no longer has the key of that address, so "
              "nothing can sign the request",
    "unanswered": "the hub did not confirm the request (unreachable or "
                  "refused)",
    "busy": "another OnionPress process was talking to the hub",
}


def release_registration(app, reason, keep_address=None):
    """Before this install forgets an onion service key (reset, key
    import, a restore to another address, uninstall): ask every hub the
    record says may hold one of its keys to delete its copy, with a
    signed /unregister for the whole address, to the hub the record names.
    *keep_address* is a key that stays (a restore of the same address):
    its registrations are left alone.

    Only the key in the tor container can sign, so the container must be
    running. Whatever cannot be taken back (no key, the hub did not
    answer) stays in the record's left_on_hubs, so OnionPress can say what
    remains on which hub, and is logged loudly. When the current
    address's hubs all answered, the record holds it ("released") for
    RELEASE_HOLD_SECONDS so no heartbeat, from this process or another,
    puts the key back before it is gone.

    Goes out whatever REGISTER_WITH_ONIONHEAVEN says: the user is ending
    the site's relationship with the hub explicitly. Returns what may
    remain: a list of {"content_address", "onionheaven_address", "why"}
    (why: key-unreadable, no-key, unanswered, busy); [] when nothing does.
    """
    if getattr(app, 'is_onionheaven', False):
        return []
    try:
        with _hub_lock(app, timeout=RELEASE_LOCK_TIMEOUT):
            return _release_registration_locked(app, reason, keep_address)
    except HubBusy:
        remaining = [{"content_address": a, "onionheaven_address": h, "why": "busy"}
                     for a, h in registrations_left(app) if a != keep_address]
        _log_remaining(app, remaining)
        return remaining


def _release_registration_locked(app, reason, keep_address):
    status = _load_registration_status(app)
    pairs = [p for p in _registrations(status, app) if p[0] != keep_address]
    if not pairs:
        return []
    live = _key_address()
    remaining = []
    released_live = False
    for addr, hub in pairs:
        if addr != live:
            remaining.append({"content_address": addr, "onionheaven_address": hub,
                              "why": "key-unreadable" if live is None else "no-key"})
            continue
        result = unregister_from_onionheaven(app, content_address=addr, hub=hub)
        if result is True:
            released_live = True
        else:
            remaining.append({"content_address": addr, "onionheaven_address": hub,
                              "why": "unanswered" if result is False else "no-key"})
    # unregister_from_onionheaven rewrote the record: start from the file.
    status = _load_registration_status(app)
    main = _main_registration(status, app)
    for item in remaining:
        pair = (item["content_address"], item["onionheaven_address"])
        _note_left_on_hub(status, pair[0], pair[1], reason)
        if pair == main:
            status["registered"] = False
            app._onionheaven_registration_succeeded = False
    if released_live and not any(i["content_address"] == live for i in remaining):
        status["released"] = {"content_address": live,
                              "until": time.time() + RELEASE_HOLD_SECONDS}
    _save_registration_status(app, status)
    _log_remaining(app, remaining)
    return remaining


def _log_remaining(app, remaining):
    for item in remaining:
        app.log(f"OnionHeaven: WARNING — {item['onionheaven_address']} may still "
                f"hold the onion service key of {item['content_address']}: "
                f"{_WHY_TEXT.get(item['why'], item['why'])}")


def describe_remaining(remaining):
    """What release_registration could not take back, for the user: one
    paragraph per hub, or "" when nothing remains."""
    lines = []
    for item in remaining:
        lines.append(
            f"The OnionHeaven hub {item['onionheaven_address']} may still hold "
            f"a copy of the onion service key of {item['content_address']}: "
            f"{_WHY_TEXT.get(item['why'], item['why'])}.")
    return "\n".join(lines)


def end_hub_traffic(app):
    """Uninstall: nothing that carries the key goes to a hub from now on
    in this process, so no heartbeat can follow the /unregister."""
    app._onionheaven_ending = True


def maybe_unregister_after_opt_out(app, now=None):
    """Called every status cycle once Tor is up: if the user has explicitly
    turned OnionHeaven off (REGISTER_WITH_ONIONHEAVEN=no) and this install's
    record says the hub may hold its key, send one signed /unregister in a
    background thread, then forget the registration.

    Covers both a "yes" -> "no" change while running (Mac Settings, the
    WordPress Settings page) and an install that starts with "no" but
    registered in an earlier session. Unset (undecided) never sends
    anything: the hub keeps the key and takes the address over while the
    site is down, as before. A failed attempt is retried every
    OPT_OUT_RETRY_SECONDS; one that cannot work (key changed, no usable
    hub) is not retried until the next launch.

    Returns the thread started, or None.
    """
    if getattr(app, 'is_onionheaven', False):
        return None
    if get_registration_choice(app) != "no":
        return None
    if getattr(app, '_onionheaven_opt_out_in_flight', False):
        return None
    if getattr(app, '_onionheaven_opt_out_gave_up', False):
        return None
    now = time.time() if now is None else now
    last = getattr(app, '_onionheaven_opt_out_last_attempt', 0)
    if last and now - last < OPT_OUT_RETRY_SECONDS:
        return None
    if not _load_registration_status(app).get("registered"):
        return None

    app._onionheaven_opt_out_in_flight = True
    app._onionheaven_opt_out_last_attempt = now
    thread = threading.Thread(target=_unregister_after_opt_out, args=(app,), daemon=True)
    thread.start()
    return thread


def _unregister_after_opt_out(app):
    try:
        app.log("OnionHeaven: turned off (REGISTER_WITH_ONIONHEAVEN=no) — "
                "asking the hub to delete its copy of this site's key")
        result = unregister_from_onionheaven(app, while_choice="no")
        if get_registration_choice(app) != "no":
            # Turned back on meanwhile: a later "no" unregisters at once.
            app._onionheaven_opt_out_last_attempt = 0
        elif result is None:
            app._onionheaven_opt_out_gave_up = True
        elif result is False:
            app.log("OnionHeaven: will retry the /unregister in "
                    f"{OPT_OUT_RETRY_SECONDS // 60} min")
    except Exception as e:
        app.log(f"OnionHeaven: opt-out unregister error: {e}")
    finally:
        app._onionheaven_opt_out_in_flight = False


def maintain_registration(app, now=None):
    """Called every status cycle once Tor is up and the address is known
    (check_status, beside maybe_unregister_after_opt_out). Keeps the record
    true to what the hubs may hold, and takes back what it can:

    - a registration for an address whose key is no longer in the tor
      container (a key import or restore that did not take it back first)
      moves to left_on_hubs: nothing can sign its /unregister any more;
    - a registration with a hub other than ONIONHEAVEN_ADDRESS (the user
      changed the hub; it is read at startup) moves to left_on_hubs, to be
      taken back below;
    - left_on_hubs entries for this site's address get a signed
      /unregister, retried every OPT_OUT_RETRY_SECONDS, except the hub
      this install registers with while the choice is yes (the heartbeat
      puts the key there again, so the entry is dropped);
    - entries for other addresses are logged once per session.

    Each follows an explicit end (a hub change, a reset, a key import, a
    restore), so the /unregister goes out whatever REGISTER_WITH_ONIONHEAVEN
    says. Only reads the record here; the work runs in a background thread
    (the hub lock can be held for minutes). Returns the thread, or None.
    """
    if getattr(app, 'is_onionheaven', False):
        return None
    live = getattr(app, 'onion_address', None)
    if not key_manager.is_valid_onion_address(live):
        return None
    if getattr(app, '_onionheaven_left_in_flight', False):
        return None
    status = _load_registration_status(app)
    main = _main_registration(status, app)
    entries = _left_on_hubs(status)
    choice = get_registration_choice(app)

    reported = getattr(app, '_onionheaven_left_reported', None)
    if not isinstance(reported, set):
        reported = set()
        app._onionheaven_left_reported = reported
    for entry in entries:
        if entry["content_address"] != live and _pair(entry) not in reported:
            reported.add(_pair(entry))
            app.log(f"OnionHeaven: WARNING — {entry['onionheaven_address']} may "
                    f"still hold the onion service key of {entry['content_address']} "
                    f"(since {entry.get('since', '?')}, {entry.get('reason', '?')}). "
                    "This install no longer has that key, so OnionPress cannot "
                    "ask the hub to delete it; ask the hub's operator.")

    current = (live, ONIONHEAVEN_ADDRESS)
    fix = main is not None and (main[0] != live or (
        main[1] != ONIONHEAVEN_ADDRESS
        and key_manager.is_valid_onion_address(ONIONHEAVEN_ADDRESS)))
    drop = choice == "yes" and any(_pair(e) == current for e in entries)
    retry = any(e["content_address"] == live
                and not (choice == "yes" and _pair(e) == current)
                for e in entries)
    now = time.time() if now is None else now
    last = getattr(app, '_onionheaven_left_last_attempt', 0)
    if last and now - last < OPT_OUT_RETRY_SECONDS:
        # Asked (or looked) recently: only the local bookkeeping now.
        fix = retry = False
    if not (fix or drop or retry):
        return None
    app._onionheaven_left_in_flight = True
    if fix or retry:
        app._onionheaven_left_last_attempt = now
    thread = threading.Thread(target=_maintain_registration,
                              args=(app, live, fix or retry), daemon=True)
    thread.start()
    return thread


def _maintain_registration(app, live, network=True):
    """maintain_registration's work, under the hub lock. Without
    *network* (asked recently), only the record's bookkeeping."""
    try:
        with _hub_lock(app, timeout=HEARTBEAT_LOCK_TIMEOUT):
            choice = get_registration_choice(app)
            status = _load_registration_status(app)
            changed = False
            main = _main_registration(status, app)
            if main is not None and main[0] != live:
                key_addr = _key_address() if network else None
                if key_addr is not None and key_addr != main[0]:
                    _note_left_on_hub(status, main[0], main[1], "address changed")
                    status["registered"] = False
                    app._onionheaven_registration_succeeded = False
                    changed = True
                    app.log(f"OnionHeaven: WARNING — {main[1]} may still hold the "
                            f"key of {main[0]}, which this install no longer has; "
                            "OnionPress cannot ask the hub to delete it")
            elif (main is not None and main[1] != ONIONHEAVEN_ADDRESS
                    and key_manager.is_valid_onion_address(ONIONHEAVEN_ADDRESS)):
                _note_left_on_hub(status, main[0], main[1], "hub changed")
                status["registered"] = False
                app._onionheaven_registration_succeeded = False
                changed = True
                app.log(f"OnionHeaven: hub changed from {main[1]} to "
                        f"{ONIONHEAVEN_ADDRESS}; asking {main[1]} to delete its "
                        "copy of this site's key")
            if choice == "yes" and _forget_left_on_hub(status, live, ONIONHEAVEN_ADDRESS):
                changed = True
            if changed:
                _save_registration_status(app, status)
            pairs = [_pair(e) for e in _left_on_hubs(status)
                     if e["content_address"] == live
                     and not (choice == "yes"
                              and e["onionheaven_address"] == ONIONHEAVEN_ADDRESS)]
            for addr, hub in (pairs if network else []):
                result = unregister_from_onionheaven(app, content_address=addr, hub=hub)
                if result is False:
                    app.log(f"OnionHeaven: will ask {hub} again in "
                            f"{OPT_OUT_RETRY_SECONDS // 60} min")
    except HubBusy:
        pass
    except Exception as e:
        app.log(f"OnionHeaven: registration upkeep error: {e}")
    finally:
        app._onionheaven_left_in_flight = False


class HubContext:
    """The app object the functions above need, outside the menubar: the
    CLI (onionpress.cli onionheaven-unregister, reset, restore) on either
    platform. Docker as resolve_paths() finds it: Colima's socket on a
    Mac, the rootless or system daemon on Linux."""

    is_onionheaven = False
    healthcheck_address = None

    def __init__(self, paths, log_func, onion_address=None):
        from onionpress import __version__
        self.app_support = paths.data_dir
        self.config_file = paths.config_file
        self.bin_dir = paths.bin_dir
        self.colima_home = paths.colima_home
        self.docker_socket = paths.docker_socket
        self.onion_address = onion_address
        self.version = __version__
        self._log_func = log_func

    def read_config_value(self, key, default=""):
        from onionpress.config import read_value
        return read_value(self.config_file, key, default)

    def log(self, msg):
        self._log_func(msg)


# ---------------------------------------------------------------------------
# Online/offline notifications (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _send_onionheaven_notification(app, endpoint, log_label, max_attempts=1, max_time=10):
    """Send a lifecycle notification (/online or /offline) to OnionHeaven.

    Builds payload with content_address, healthcheck_address, and ed25519 signature.
    Uses docker exec curl through the Tor SOCKS proxy.

    Sends nothing unless REGISTER_WITH_ONIONHEAVEN is "yes": an /offline
    makes the hub take the address over at once, which an opted-out or
    undecided install must not ask for, and /online carries the key.
    /online also takes the hub lock, as _send_heartbeat does, and is not
    sent while uninstalling or for an address on the record's "released"
    hold; /offline takes no lock (it is sent inside the sleep deadline and
    carries no key).

    Returns True on success, False on failure.
    """
    if getattr(app, 'is_onionheaven', False):
        return False
    if get_registration_choice(app) != "yes":
        return False
    if endpoint == "online":
        try:
            with _hub_lock(app, timeout=HEARTBEAT_LOCK_TIMEOUT):
                if get_registration_choice(app) != "yes":
                    return False
                if getattr(app, '_onionheaven_ending', False):
                    return False
                if _address_on_hold(app, getattr(app, 'onion_address', None)):
                    return False
                return _send_onionheaven_notification_unlocked(
                    app, endpoint, log_label, max_attempts, max_time)
        except HubBusy:
            return False
    return _send_onionheaven_notification_unlocked(
        app, endpoint, log_label, max_attempts, max_time)


def _send_onionheaven_notification_unlocked(app, endpoint, log_label, max_attempts, max_time):
    # Check if we ever registered
    status = _load_registration_status(app)
    if not status.get("registered"):
        return False

    content_addr = getattr(app, 'onion_address', None)
    hc_addr = getattr(app, 'healthcheck_address', None)

    if not key_manager.is_valid_onion_address(content_addr):
        return False
    if not key_manager.is_valid_onion_address(hc_addr):
        return False
    url = _hub_url(endpoint)
    if url is None:
        app.log(f"OnionHeaven: hub address is not a valid onion address, "
                f"not sending /{endpoint}")
        return False

    # Sign with ed25519
    try:
        from onionpress import onion_auth
        secret_key_bytes, public_key_raw = key_manager.extract_keys()
        # See _send_heartbeat for context — same staleness guard applies to
        # lifecycle notifications (/online, /offline). Skip rather than ship
        # a mismatched (content_addr, arti_key_pem) pair.
        derived = onion_auth.derive_onion_address(public_key_raw)
        if derived != content_addr:
            app.log(f"OnionHeaven: ABORT /{endpoint} — onion_address stale "
                    f"(app={content_addr}, key_derives_to={derived})")
            return False
        timestamp = onion_auth.make_timestamp()
        signature = onion_auth.sign_payload(
            secret_key_bytes, public_key_raw,
            endpoint, content_addr, hc_addr, timestamp
        )
    except Exception as e:
        app.log(f"OnionHeaven: failed to sign /{endpoint} request: {e}")
        return False

    payload_dict = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "is_onionheaven": getattr(app, 'is_onionheaven', False),
        "timestamp": timestamp,
        "signature": signature,
    }

    # /online requires the arti key so OnionHeaven can do takeover if needed
    if endpoint == "online":
        try:
            arti_pem = key_manager.build_openssh_key(secret_key_bytes, public_key_raw)
            payload_dict["arti_key_pem"] = base64.b64encode(arti_pem).decode('ascii')
        except Exception as e:
            app.log(f"OnionHeaven: failed to build arti key for /{endpoint}: {e}")
            return False

    payload = json.dumps(payload_dict)

    backoff = [10, 30]
    last_output = ""

    for attempt in range(max_attempts):
        # Payload on stdin, like the heartbeat's: /online carries the
        # onion private key, which must stay out of argv.
        ok, output = _run_docker(app, [
            "exec", "-i", "onionpress-tor",
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "127.0.0.1:9050",
            "-H", "Content-Type: application/json",
            "--data-binary", "@-",
            "--max-time", str(max_time),
            url
        ], timeout=max_time + 15, input=payload)
        last_output = output

        if ok and output:
            try:
                resp = json.loads(output)
                if resp.get("online" if endpoint == "online" else "offline"):
                    app.log(f"OnionHeaven: /{endpoint} notification sent successfully")
                    return True
                error_msg = resp.get("error", "unknown error")
                app.log(f"OnionHeaven: /{endpoint} rejected: {error_msg}")
                return False
            except json.JSONDecodeError:
                app.log(f"OnionHeaven: /{endpoint} got non-JSON response: {output[:200]}")
        elif not ok:
            app.log(f"OnionHeaven: /{endpoint} curl failed (attempt {attempt + 1}/{max_attempts})")
        else:
            app.log(f"OnionHeaven: /{endpoint} got empty response (attempt {attempt + 1}/{max_attempts})")

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            app.log(f"OnionHeaven: /{endpoint} retrying in {delay}s...")
            time.sleep(delay)

    app.log(f"OnionHeaven: /{endpoint} failed after {max_attempts} attempts — last response: {last_output!r}")
    return False


def notify_onionheaven_offline(app, max_time=10):
    """Notify OnionHeaven that this instance is going offline (sleep/quit).

    Runs synchronously with a single attempt and short timeout —
    must complete before the system sleeps or the app quits.

    ``max_time`` is the curl --max-time bound (seconds). Callers driving
    this from inside an IOKit sleep delay-inhibitor (see
    :class:`onionpress.mac_power.MacPowerHandler`) should pass a tighter
    value (~5s) to leave headroom inside the 30s system suspend ceiling;
    the default (10s) is for non-time-critical callers like quit / stop.
    """
    if getattr(app, 'is_onionheaven', False):
        return False
    app.log("Notifying OnionHeaven: going offline")
    return _send_onionheaven_notification(app, "offline", "offline", max_attempts=1, max_time=max_time)


def notify_onionheaven_online(app):
    """Notify OnionHeaven that this instance is back online (wake/reconnect).

    Uses the same heartbeat code path that's proven to work — sends /online
    with arti_key_pem, retries once on failure. Sets
    app._onionheaven_reclaim_succeeded on success; clears
    app._onionheaven_reclaim_in_flight when done.
    """
    if getattr(app, 'is_onionheaven', False):
        app._onionheaven_reclaim_in_flight = False
        return False
    app.log("Notifying OnionHeaven: coming online (via heartbeat)")
    try:
        result = _send_heartbeat(app)
        if result:
            app._onionheaven_reclaim_succeeded = True
            app.log("OnionHeaven: reclaim succeeded")
        return bool(result)
    finally:
        app._onionheaven_reclaim_in_flight = False


def start_online_notification_thread(app):
    """Spawn a daemon thread to send /online notification."""
    thread = threading.Thread(target=notify_onionheaven_online, args=(app,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# OnionHeaven mode detection and UI helpers (heartbeat monitor runs in onionheaven container)
# ---------------------------------------------------------------------------

# The well-known OnionHeaven hub address — used to detect if this instance
# IS the hub (separate from which hub we register with).
_KNOWN_ONIONHEAVEN_ADDRESS = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"

# Addresses where the deployment owner wants PREVENT_SLEEP=never auto-set on
# first launch. Always includes the OnionHeaven hub (must stay awake to run
# takeover decisions). Other entries are deployment-pinned home instances
# where the 2026-04-26 sleep+wedge failure mode is unacceptable.
# The marker file in menubar.py ensures the auto-set fires only once per
# install; the user can flip PREVENT_SLEEP back via the menubar at any time.
_ALWAYS_AWAKE_ADDRESSES = frozenset([
    _KNOWN_ONIONHEAVEN_ADDRESS,
    "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion",
])


def is_onionheaven_instance(onion_address):
    """Check if this instance is the OnionHeaven hub.

    Compares against the well-known hub address, not the config field
    (which controls registration, not identity).
    """
    if not onion_address or not onion_address.endswith('.onion'):
        return False
    return onion_address.strip() == _KNOWN_ONIONHEAVEN_ADDRESS


def should_auto_prevent_sleep(onion_address):
    """Check if this instance should auto-set PREVENT_SLEEP=never on first launch."""
    if not onion_address or not onion_address.endswith('.onion'):
        return False
    return onion_address.strip() in _ALWAYS_AWAKE_ADDRESSES


