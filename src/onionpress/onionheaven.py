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
import sys
import threading
import time
from datetime import datetime, timezone

from onionpress import hub_record, key_manager
from onionpress.platform import docker_env, find_docker_bin, resolve_paths

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


# The ONIONHEAVEN_ADDRESS value configured_hub_address could not use, so
# the default hub stands in for it ("" when the config's value was fine).
# The registration logs it again right before the key goes out.
REJECTED_HUB_ADDRESS = ""


def hub_fallback_warning():
    """The log line for an ONIONHEAVEN_ADDRESS that is not an onion address
    (named by key only, never its value)."""
    return ("OnionHeaven: WARNING: ONIONHEAVEN_ADDRESS in the config is not a "
            "valid onion address; using the default hub "
            f"{DEFAULT_ONIONHEAVEN_ADDRESS} instead, which receives this "
            "site's onion service key. Fix the address, or set "
            "REGISTER_WITH_ONIONHEAVEN=no, then restart OnionPress.")


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
    global REJECTED_HUB_ADDRESS
    value = (value or "").strip()
    REJECTED_HUB_ADDRESS = ""
    if not value:
        return DEFAULT_ONIONHEAVEN_ADDRESS
    if key_manager.is_valid_onion_address(value):
        return value
    REJECTED_HUB_ADDRESS = value
    if log_func:
        log_func(hub_fallback_warning())
    return DEFAULT_ONIONHEAVEN_ADDRESS


def use_configured_hub(value, log_func=None):
    """Make the hub for ONIONHEAVEN_ADDRESS=*value* this process's
    (ONIONHEAVEN_ADDRESS, and REJECTED_HUB_ADDRESS through
    configured_hub_address) and return it. The one writer of the global
    outside tests: the daemon at start and on a settings change, the
    CLI's _hub_context (issue #476)."""
    global ONIONHEAVEN_ADDRESS
    ONIONHEAVEN_ADDRESS = configured_hub_address(value, log_func)
    return ONIONHEAVEN_ADDRESS


def _hub_url(path, hub=None):
    """http://<hub>:<port>/<path>, or None when the hub is not a well-formed
    onion address.

    *hub* defaults to ONIONHEAVEN_ADDRESS. Every request to a hub is built
    here. ONIONHEAVEN_ADDRESS is a module global (set by
    use_configured_hub, and by tests), so it is checked where it is used,
    not only where the config is read.
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
    socket_path = (getattr(app, "docker_socket", None)
                   or f"{app.colima_home}/default/docker.sock")
    return docker_env(socket_path,
                      os.path.join(app.app_support, "docker-config"))


def _docker_bin(app):
    """Return path to docker binary: the bundled one in app.bin_dir, or
    "docker" from PATH when there is no bundle (Linux). An app that names
    no docker_socket (the menubar's) always gets the bundled path."""
    bin_dir = getattr(app, "bin_dir", "") or ""
    if bin_dir and not getattr(app, "docker_socket", None):
        return os.path.join(bin_dir, "docker")
    return find_docker_bin(bin_dir)


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
_RECORD_CARRIED = hub_record.CARRIED


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
    status = hub_record.carry_over(
        status, _load_registration_status(app)
        if hub_record.missing_carried(status) else {})
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, 'w') as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)


_now_iso = hub_record.now_iso


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


_pair = hub_record.pair
_left_on_hubs = hub_record.left_on_hubs
_note_left_on_hub = hub_record.note_left_on_hub
_forget_left_on_hub = hub_record.forget_left_on_hub


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
    return hub_record.address_on_hold(_load_registration_status(app), addr, now)


def _key_address():
    """The address of the key in the tor container, or None if unreadable."""
    try:
        _private, public = key_manager.extract_keys()
        return key_manager.derive_onion_address(public)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The hub's answers, the heartbeat's local checks, what a release leaves
# ---------------------------------------------------------------------------

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 60

# The hub's answer to an /unregister for an address it has no row for
# (web-server.py _handle_unregister): nothing of ours is left there.
UNREGISTER_NOT_FOUND = "Entry not found"

# How often an opted-out install retries an /unregister the hub did not
# answer (hub unreachable, Tor trouble), and how often left_on_hubs
# entries for this install's address are retried. The hub takes an
# address over about 10 min after its last heartbeat while it still holds
# the key, and the /unregister that lands releases that takeover.
OPT_OUT_RETRY_SECONDS = 15 * 60

_WHY_TEXT = {
    "key-unreadable": "OnionPress could not read this site's onion service "
                      "key (is OnionPress running?)",
    "no-key": "this install no longer has the key of that address, so "
              "nothing can sign the request",
    "unanswered": "the hub did not confirm the request (unreachable or "
                  "refused)",
    "busy": "another OnionPress process was talking to the hub",
}


def _check_wordpress_healthy(app):
    """Quick local check if WordPress is responding (for a client whose host
    passes no wordpress_healthy hook)."""
    try:
        return app._health_checker.check_wordpress_local()
    except Exception:
        return False


# Endpoint label of the signed per-address status query (POST
# /status/<address>): web-server.py's STATUS_ENDPOINT, a test ties them.
STATUS_ENDPOINT = "status"


def _heartbeat_landed(app, content_addr, freshness_seconds=180):
    """Did an earlier POST /online actually land on the hub, even though
    we timed out waiting for the ACK?

    Posts a signed status query (POST /status/<content_addr>, signed by
    the site's own key under STATUS_ENDPOINT: since issue #469 the plain
    GET says only whether the address is registered, and the detail
    goes to the address's own key) and checks the most-recent
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
        from onionpress import onion_auth
        secret_key_bytes, public_key_raw = key_manager.extract_keys()
        if onion_auth.derive_onion_address(public_key_raw) != content_addr:
            return False  # the key is another address's (see _send_heartbeat_locked)
        timestamp = onion_auth.make_timestamp()
        payload = json.dumps({
            "content_address": content_addr,
            "healthcheck_address": "",
            "timestamp": timestamp,
            "signature": onion_auth.sign_payload(
                secret_key_bytes, public_key_raw,
                STATUS_ENDPOINT, content_addr, "", timestamp),
        })
        args = [
            "exec", "-i", "onionpress-tor",
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "127.0.0.1:9050",
            "-H", "Content-Type: application/json",
            "--data-binary", "@-",
            "--max-time", "30",
            url,
        ]
        rc, output = _run_docker_rc(app, args, timeout=45, input=payload)
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


class HubContext:
    """The app an OnionHeavenClient (below) talks through outside the Mac
    daemon: the CLI (onionpress.cli onionheaven-unregister, reset,
    restore) on either platform. Docker as resolve_paths() finds it: Colima's socket on a
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
# The client (runs on normal OnionPress instances): registration, the
# heartbeat, /online and /offline, the opt-out, the record's upkeep, release
# ---------------------------------------------------------------------------

class OnionHeavenClient:
    """This install's side of OnionHeaven (issue #479). It owns the
    session's state: whether a registration thread is starting, whether
    the hub took an /online, the heartbeat loop's generation, the
    opt-out's and the left_on_hubs upkeep's retry clocks, and whether the
    install is ending. It talks to the hub by docker exec into the tor
    container (_run_docker, _run_docker_rc) under the hub lock.

    *app* is what the client reads of its host, live: log(),
    read_config_value(), app_support and the docker settings (HubContext's
    attributes), onion_address, healthcheck_address, is_onionheaven and
    version. The Mac daemon holds one client for its whole run
    (Daemon.hub_client) and passes its hooks: *sleeping* (no /online while
    asleep), *wordpress_healthy* (the heartbeat's local check), *docker*
    (the heartbeat snapshot's container metrics) and *drives_own_snapshot*
    (the daemon emits its own). The CLI's HubContext gets one through
    client_of().

    _HUB_LOCK and its flock stay module-wide: every client in a process,
    and every OnionPress process, orders the requests that carry the key,
    the /unregisters and the record's changes through the one lock.
    """

    def __init__(self, app, sleeping=None, wordpress_healthy=None, docker=None,
                 drives_own_snapshot=False):
        self.app = app
        self._sleeping_hook = sleeping
        self._wordpress_healthy_hook = wordpress_healthy
        self._docker_hook = docker
        self.drives_own_snapshot = drives_own_snapshot
        # This session's registration: a registration thread is starting,
        # the hub took an /online (so an /offline is worth sending), the
        # last beat landed.
        self.registration_in_flight = False
        self.registration_succeeded = False
        self.heartbeat_succeeded = False
        # A wake's /online (notify_online).
        self.reclaim_succeeded = False
        self.reclaim_in_flight = False
        # The opt-out's /unregister (maybe_unregister_after_opt_out).
        self.opt_out_in_flight = False
        self.opt_out_last_attempt = 0
        self.opt_out_gave_up = False
        # The record's upkeep (maintain_registration); left_reported holds
        # the pairs already logged this session.
        self.left_in_flight = False
        self.left_last_attempt = 0
        self.left_reported = set()
        # Uninstalling (end_hub_traffic): nothing carrying the key goes out.
        self.ending = False
        # The heartbeat loop; a wake starts a newer generation.
        self.heartbeat_generation = 0
        self.heartbeat_loop_running = False

    # -- the host, the lock and the transport ------------------------------

    def log(self, msg):
        self.app.log(msg)

    def hub_lock(self, timeout=RELEASE_LOCK_TIMEOUT):
        """_hub_lock: the process-wide lock plus the cross-process flock in
        this install's data dir."""
        return _hub_lock(self.app, timeout)

    def _run_docker(self, args, timeout=15, input=None):
        return _run_docker(self.app, args, timeout=timeout, input=input)

    def _run_docker_rc(self, args, timeout=15, input=None):
        return _run_docker_rc(self.app, args, timeout=timeout, input=input)

    def _sleeping(self):
        return bool(self._sleeping_hook()) if self._sleeping_hook else False

    def _wordpress_healthy(self):
        if self._wordpress_healthy_hook is None:
            return _check_wordpress_healthy(self.app)
        try:
            return self._wordpress_healthy_hook()
        except Exception:
            return False

    def _docker(self):
        return self._docker_hook() if self._docker_hook else None

    # -- the daemon's status cycle, stop and wake ----------------------------

    def tick(self, tor_bootstrapped, address_known):
        """Every status cycle of the Mac daemon. Once Tor is bootstrapped and
        the address is known, start the registration + heartbeat thread
        when none runs and the choice is "yes" (unset reads as yes; re-read
        every cycle). Then the opt-out and the record's upkeep, whatever
        the choice: each decides for itself."""
        if not tor_bootstrapped or getattr(self.app, 'is_onionheaven', False):
            return
        if (address_known
                and not self.registration_succeeded
                and not self.registration_in_flight):
            # On by default: "yes" or unset registers; "no" never does
            # (the opt-out below asks the hub to forget an earlier
            # registration).
            if get_registration_choice(self.app) == "yes":
                self.registration_in_flight = True
                self.start_registration_thread()
        if key_manager.is_valid_onion_address(getattr(self.app, 'onion_address', None)):
            self.maybe_unregister_after_opt_out()
            self.maintain_registration()

    def reset_session(self):
        """The site stopped: the next start registers afresh, and no wake
        reclaim is pending."""
        self.registration_succeeded = False
        self.heartbeat_succeeded = False
        self.registration_in_flight = False
        self.reclaim_succeeded = False
        self.reclaim_in_flight = False

    def reset_reclaim(self):
        """The site is reachable again: a later wake may reclaim anew."""
        self.reclaim_succeeded = False
        self.reclaim_in_flight = False

    def wake(self):
        """Back from sleep: register afresh, and supersede the running
        heartbeat loop (it exits before its next beat)."""
        self.registration_succeeded = False
        self.heartbeat_succeeded = False
        self.registration_in_flight = False
        self.heartbeat_loop_running = False
        self.heartbeat_generation += 1

    # -- registration and the heartbeat ------------------------------------

    def register(self):
        """
        Register this instance with OnionHeaven by starting the heartbeat loop.

        Every /online carries the arti_key_pem (the hub refuses one without
        it), so the first heartbeat is also the registration. Called from a
        background thread; returns when the heartbeat loop ends, which it does
        as soon as REGISTER_WITH_ONIONHEAVEN is no longer "yes".
        Sets self.registration_succeeded on success;
        clears self.registration_in_flight when done.
        """
        # "yes" (or unset, which reads as yes) registers; "no" means the user
        # opted out, and an unrecognized value registers nothing.
        choice = get_registration_choice(self.app)
        if choice != "yes":
            self.log("OnionHeaven registration not enabled "
                     f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
            self.registration_in_flight = False
            return

        # Don't register with ourselves
        if getattr(self.app, 'is_onionheaven', False):
            self.registration_in_flight = False
            return

        content_addr = self.app.onion_address
        hc_addr = self.app.healthcheck_address

        if not key_manager.is_valid_onion_address(content_addr):
            self.log("OnionHeaven: no content address available, skipping registration")
            self.registration_in_flight = False
            return
        if not key_manager.is_valid_onion_address(hc_addr):
            self.log("OnionHeaven: no healthcheck address available, skipping registration")
            self.registration_in_flight = False
            return

        if REJECTED_HUB_ADDRESS and ONIONHEAVEN_ADDRESS == DEFAULT_ONIONHEAVEN_ADDRESS:
            self.log(hub_fallback_warning())
        self.log("Registering with OnionHeaven via heartbeat...")

        try:
            # Send initial heartbeat with key (acts as registration)
            wp_healthy = self._wordpress_healthy()
            self.send_heartbeat(wp_healthy)

            if self.registration_succeeded:
                # Start heartbeat loop (runs forever in this thread)
                self.heartbeat_loop()
                return

            # First attempt failed — retry with backoff, then start heartbeat loop
            backoff = [10, 30]
            last_output = ""

            for attempt, wait in enumerate(backoff):
                self.log(f"OnionHeaven: registration attempt {attempt + 2} in {wait}s...")
                time.sleep(wait)
                choice = get_registration_choice(self.app)
                if choice != "yes":
                    self.log("OnionHeaven: registration stopped "
                             f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
                    return
                wp_healthy = self._wordpress_healthy()
                self.send_heartbeat(wp_healthy)
                if self.registration_succeeded:
                    self.heartbeat_loop()
                    return

            # Even if registration hasn't succeeded yet, start heartbeat loop —
            # it will keep trying on each cycle
            self.log("OnionHeaven: initial registration attempts failed, continuing heartbeat loop")
            self.heartbeat_loop()
            return

        except Exception as e:
            self.log(f"OnionHeaven: registration error: {e}")
        finally:
            self.registration_in_flight = False

    def start_registration_thread(self):
        """Start the registration + heartbeat background thread.

        Registration happens via the first /online heartbeat. Every /online,
        the first and all later ones, carries the arti key: the hub refuses an
        /online without it.
        """
        thread = threading.Thread(target=self.register, daemon=True)
        thread.start()
        return thread

    def heartbeat_loop(self):
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
        and sleep/wake, so the heartbeat skips emission when the client
        was made with drives_own_snapshot.
        """
        # Prevent duplicate heartbeat loops (multiple registration threads
        # can race into this function after wake/restart)
        if self.heartbeat_loop_running:
            self.log("OnionHeaven: heartbeat loop already running, skipping duplicate")
            return
        self.heartbeat_loop_running = True
        my_generation = self.heartbeat_generation

        # Random jitter on first heartbeat to prevent thundering herd
        # after OnionHeaven restart
        jitter = random.uniform(0, 15)
        self.log(f"OnionHeaven: heartbeat loop starting (gen={my_generation}, first beat in {jitter:.0f}s)")
        time.sleep(jitter)

        # Issue #238: heartbeat-driven ~12h snapshot for Linux (no menubar).
        # 720 iterations × 60s ≈ 12h. The Mac menubar runs its own snapshot
        # path from check_status, so skip emission there.
        snapshot_every_n = max(1, int((12 * 3600) / HEARTBEAT_INTERVAL))
        iteration = 0
        drives_own_snapshot = self.drives_own_snapshot

        while True:
            # Exit if a newer heartbeat loop has been started (after wake)
            if self.heartbeat_generation != my_generation:
                self.log(f"OnionHeaven: heartbeat loop gen={my_generation} exiting (superseded)")
                return

            # Every beat: the user may have turned OnionHeaven off
            # (Mac Settings, the WordPress Settings page) since the last one.
            choice = get_registration_choice(self.app)
            if choice != "yes":
                self._end_heartbeat_loop(my_generation, choice)
                return

            try:
                # Don't send /online while sleeping — let the hub detect us as
                # offline and take over. Power Nap keeps the app running but
                # doesn't trigger wake events, so _sleeping stays True.
                if self._sleeping():
                    pass  # skip heartbeat while sleeping
                else:
                    # Check WordPress health locally before sending heartbeat
                    wp_healthy = self._wordpress_healthy()
                    self.send_heartbeat(wp_healthy)
            except Exception as e:
                self.log(f"OnionHeaven: heartbeat error: {e}")

            iteration += 1
            if (not drives_own_snapshot
                    and not self._sleeping()
                    and iteration % snapshot_every_n == 0):
                self._emit_heartbeat_snapshot()

            time.sleep(HEARTBEAT_INTERVAL)

    def _end_heartbeat_loop(self, my_generation, choice):
        """The loop's exit when REGISTER_WITH_ONIONHEAVEN is no longer "yes".

        Clears the session's registration flags, so nothing sends /offline on
        sleep, stop or quit, and so check_status starts a fresh loop if the
        user turns OnionHeaven back on. The /unregister for an explicit "no"
        is maybe_unregister_after_opt_out's, not the loop's: it also covers
        an install that starts with "no", and an unrecognized value sends
        nothing.
        """
        if choice == "no":
            self.log("OnionHeaven: disabled (REGISTER_WITH_ONIONHEAVEN=no) — "
                     "heartbeats stopped")
        else:
            self.log("OnionHeaven: REGISTER_WITH_ONIONHEAVEN is not yes or no — "
                     "heartbeats stopped; nothing is sent to the hub until it is yes")
        self.registration_succeeded = False
        self.heartbeat_succeeded = False
        # Only this loop's own guard: after a wake a newer loop owns the flag.
        if self.heartbeat_generation == my_generation:
            self.heartbeat_loop_running = False

    def _emit_heartbeat_snapshot(self):
        """System-metrics-only snapshot for the heartbeat path.

        Used on Linux where the menubar app isn't running. Importing the
        metric helpers lazily keeps this module's import surface unchanged.
        """
        try:
            from .system_metrics import host_metrics, container_metrics
            from .reachability_stats import ReachabilityStats
            host = host_metrics()
            docker = self._docker()
            ctn = container_metrics(docker) if docker is not None else None
            # Empty stats object: yields a snapshot with no reachability
            # section (heartbeat doesn't track per-probe outcomes).
            stats = ReachabilityStats()
            self.log(stats.format_snapshot(host, ctn))
        except Exception as e:
            self.log(f"OnionHeaven: heartbeat snapshot failed: {e}")

    def send_heartbeat(self, wordpress_healthy=True):
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
            with self.hub_lock(timeout=HEARTBEAT_LOCK_TIMEOUT):
                return self._send_heartbeat_locked(wordpress_healthy)
        except HubBusy:
            self.log("OnionHeaven: not sending /online now: another OnionPress "
                     "process is asking a hub to delete a key")
            return False

    def _send_heartbeat_locked(self, wordpress_healthy):
        if getattr(self.app, 'is_onionheaven', False):
            return False
        choice = get_registration_choice(self.app)
        if choice != "yes":
            self.log("OnionHeaven: not sending /online "
                     f"(REGISTER_WITH_ONIONHEAVEN={_choice_label(choice)})")
            return False
        if self.ending:
            self.log("OnionHeaven: not sending /online (uninstalling)")
            return False

        content_addr = getattr(self.app, 'onion_address', None)
        hc_addr = getattr(self.app, 'healthcheck_address', None)

        if not key_manager.is_valid_onion_address(content_addr):
            return False
        if not key_manager.is_valid_onion_address(hc_addr):
            return False
        if _address_on_hold(self.app, content_addr):
            self.log(f"OnionHeaven: not sending /online for {content_addr}: its key "
                     "is being given up and the hub was asked to delete its copy")
            return False
        online_url = _hub_url("online")
        if online_url is None:
            self.log("OnionHeaven: hub address is not a valid onion address, "
                     "not sending /online")
            return False

        try:
            from onionpress import onion_auth
            secret_key_bytes, public_key_raw = key_manager.extract_keys()
            # Guard: the keystore key and self.app.onion_address must agree. They can
            # diverge during a restore — write_private_key updates the keystore
            # in the running container, but self.onion_address (loaded from the
            # ~/.onionpress/onion_address cache at startup) only refreshes when
            # update_status() next polls the container. A heartbeat fired in
            # that window would ship (content=OLD, key=NEW), and OnionHeaven's
            # /online clobbers KEYS_DIR/OLD/ with NEW's bytes — irrecoverable
            # data loss for the old address's takeover key.
            derived = onion_auth.derive_onion_address(public_key_raw)
            if derived != content_addr:
                self.log(f"OnionHeaven: ABORT heartbeat — onion_address stale "
                         f"(app={content_addr}, key_derives_to={derived}); "
                         f"skipping until update_status catches up")
                return False
            timestamp = onion_auth.make_timestamp()
            signature = onion_auth.sign_payload(
                secret_key_bytes, public_key_raw,
                "online", content_addr, hc_addr, timestamp
            )
        except Exception as e:
            self.log(f"OnionHeaven: heartbeat sign error: {e}")
            return False

        payload_dict = {
            "content_address": content_addr,
            "healthcheck_address": hc_addr,
            "wordpress_healthy": wordpress_healthy,
            "version": getattr(self.app, 'version', 'unknown'),
            "is_onionheaven": getattr(self.app, 'is_onionheaven', False),
            "timestamp": timestamp,
            "signature": signature,
        }

        # Always include the key — every /online is both heartbeat and registration
        try:
            arti_pem = key_manager.build_openssh_key(secret_key_bytes, public_key_raw)
            payload_dict["arti_key_pem"] = base64.b64encode(arti_pem).decode('ascii')
        except Exception as e:
            self.log(f"OnionHeaven: failed to build arti key for heartbeat: {e}")

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

        had_success = self.heartbeat_succeeded
        if not had_success:
            self.log("OnionHeaven: heartbeat sending /online...")
        rc, output = self._run_docker_rc(curl_args, timeout=45, input=payload)
        ok = (rc == 0)
        if ok and output:
            try:
                json.loads(output)  # validate JSON
                if not had_success:
                    hub = ONIONHEAVEN_ADDRESS[:12]
                    self.log(f"OnionHeaven: heartbeat /online {hub}...onion OK")
            except (json.JSONDecodeError, KeyError):
                # Parseable rc=0 but body not JSON; queue retry. Quiet log —
                # we'll decide FAILED vs delivered-no-ack once retry is done.
                self.log(f"OnionHeaven: heartbeat /online bad JSON on first try, retrying")
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
                self.log(f"OnionHeaven: heartbeat /online {reason} rc={rc}, retrying in 3s")
            import time
            time.sleep(3)
            rc, output = self._run_docker_rc(curl_args, timeout=45, input=payload)
            ok = (rc == 0)

        if ok and output:
            try:
                resp = json.loads(output)
                if resp.get("online"):
                    self._mark_registered(content_addr)
                    self.heartbeat_succeeded = True
                    return True  # success
                self.log(f"OnionHeaven: heartbeat rejected: {resp.get('error', 'unknown')}")
                return False
            except json.JSONDecodeError:
                self.log(f"OnionHeaven: heartbeat got non-JSON response: {output[:200]}")
                # Before declaring failure, check if the POST actually landed —
                # JSONDecodeError often means truncated body on a still-
                # successful server-side write.
                if _heartbeat_landed(self.app, content_addr):
                    hub = ONIONHEAVEN_ADDRESS[:12]
                    self.log(f"OnionHeaven: heartbeat /online {hub}...onion delivered (no-ack, response lost on return circuit)")
                    self._mark_registered(content_addr)
                    self.heartbeat_succeeded = True
                    return True
                return False
        else:
            # Both attempts failed to get a parseable response. Before we
            # log it as FAILED, ask the hub if our heartbeat actually landed
            # — Tor circuits lose ACK packets often enough that "client
            # timed out" regularly means "server received + processed,
            # response never came back." See issue #208.
            if _heartbeat_landed(self.app, content_addr):
                hub = ONIONHEAVEN_ADDRESS[:12]
                self.log(f"OnionHeaven: heartbeat /online {hub}...onion delivered (no-ack, response lost on return circuit)")
                self._mark_registered(content_addr)
                self.heartbeat_succeeded = True
                return True
            self.log(f"OnionHeaven: heartbeat failed after retry: curl_rc={rc}, output={repr(output[:200]) if output else 'empty'}")
            return False

    def _mark_registered(self, content_addr):
        """The hub took an /online: it holds the key now. Written whenever the
        record does not already say so for this address and hub (the first
        beat of a session, the first after an /unregister, the first after a
        hub or address change), also when only _heartbeat_landed saw the beat
        arrive, so an opt-out or a reset knows to unregister. A registration
        the record named before, for another hub or another address, is not
        dropped: it moves to left_on_hubs. Caller holds the hub lock."""
        hub = ONIONHEAVEN_ADDRESS
        status = _load_registration_status(self.app)
        main = _main_registration(status, self.app)
        entries = _left_on_hubs(status)
        if (self.registration_succeeded
                and main == (content_addr, hub)
                and status.get("content_address") == content_addr
                and not any(_pair(e) == (content_addr, hub) for e in entries)):
            # Already recorded: only the beat's time, for the OnionHeaven
            # screen (client_overview).
            status["last_heartbeat_at"] = _now_iso()
            status.setdefault("registered_at", status.get("last_attempt") or status["last_heartbeat_at"])
            _save_registration_status(self.app, status)
            return
        self.registration_succeeded = True
        if main is not None and main != (content_addr, hub):
            _note_left_on_hub(status, main[0], main[1], "superseded")
            if main[0] == content_addr:
                self.log(f"OnionHeaven: registered with {hub} now; {main[1]} may "
                         "still hold this site's key and will be asked to delete it")
            else:
                self.log(f"OnionHeaven: WARNING — {main[1]} may still hold the key "
                         f"of {main[0]}, the address this install had before; "
                         "this install no longer has that key, so OnionPress "
                         "cannot ask the hub to delete it")
        _forget_left_on_hub(status, content_addr, hub)
        now = _now_iso()
        if main != (content_addr, hub) or not status.get("registered_at"):
            # A new registration (first beat, or a new hub or address): when
            # it began. Kept across the beats that follow.
            status["registered_at"] = now
        status.update({
            "registered": True,
            "last_attempt": now,
            "last_heartbeat_at": now,
            "onionheaven_address": hub,
            "content_address": content_addr,
        })
        status.pop("unregistered_at", None)
        _save_registration_status(self.app, status)
        self.log(f"OnionHeaven: registered via heartbeat ({content_addr})")

    # -- /unregister, and the end of a hub's hold on a key ------------------

    def unregister(self, content_address=None, while_choice=None, hub=None):
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
        if getattr(self.app, 'is_onionheaven', False):
            return None

        status = _load_registration_status(self.app)
        main = _main_registration(status, self.app)
        if hub is None:
            if main is None:
                self.log("OnionHeaven: no content address for unregister"
                         if status.get("registered") else
                         "OnionHeaven: not registered, skipping unregister")
                return None
            addr = content_address or main[0]
            hub_addr = main[1]
        else:
            addr = content_address or (main[0] if main else None)
            hub_addr = hub
            if (addr, hub_addr) not in _registrations(status, self.app):
                self.log("OnionHeaven: not registered, skipping unregister")
                return None

        if not key_manager.is_valid_onion_address(addr):
            self.log("OnionHeaven: no content address for unregister")
            return None
        unregister_url = _hub_url("unregister", hub_addr)
        if unregister_url is None:
            self.log("OnionHeaven: hub address is not a valid onion address, "
                     "not sending /unregister")
            return None

        # Sign the unregister request with ed25519
        try:
            from onionpress import onion_auth
            secret_key_bytes, public_key_raw = key_manager.extract_keys()
        except Exception as e:
            self.log(f"OnionHeaven: failed to extract keys for unregister: {e}")
            return False

        # Same staleness guard as _send_heartbeat / _send_onionheaven_notification:
        # refuse to unregister an address that doesn't match the live key. addr
        # comes from the record (or self.app.onion_address), either of which can
        # lag a key change, and only the live key can sign the request.
        derived = onion_auth.derive_onion_address(public_key_raw)
        if derived != addr:
            self.log(f"OnionHeaven: ABORT unregister — content_address stale "
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
            with self.hub_lock():
                self.log(f"Unregistering {addr} from OnionHeaven {hub_addr}...")
                for attempt in range(max_attempts):
                    if while_choice is not None:
                        choice = get_registration_choice(self.app)
                        if choice != while_choice:
                            self.log("OnionHeaven: unregister dropped "
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
                    ok, output = self._run_docker([
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
                                    self.log(f"OnionHeaven: unregistered successfully from {hub_addr}")
                                else:
                                    self.log(f"OnionHeaven: {hub_addr} holds no entry for "
                                             f"{addr}; nothing to unregister")
                                self._record_unregistered(addr, hub_addr)
                                return True
                            self.log(f"OnionHeaven: unregister rejected: {error_msg or 'unknown error'}")
                            # Don't retry on auth/validation errors — they won't fix themselves
                            if error_msg:
                                return False

                    if attempt < max_attempts - 1:
                        delay = backoff[min(attempt, len(backoff) - 1)]
                        self.log(f"OnionHeaven: unregister attempt {attempt + 1} failed, retrying in {delay}s...")
                        time.sleep(delay)
        except HubBusy:
            self.log("OnionHeaven: another OnionPress process kept the hub lock; "
                     "unregister not sent")
            return False

        self.log(f"OnionHeaven: unregister failed after {max_attempts} attempts (best-effort, continuing) — last response: {last_output!r}")
        return False

    def _record_unregistered(self, addr, hub):
        """*hub* answered an /unregister for *addr*: drop that pair from the
        record. The registration slot is cleared only when it names that pair
        (it may name a newer hub the site registered with since). Caller holds
        the hub lock."""
        status = _load_registration_status(self.app)
        if _main_registration(status, self.app) == (addr, hub):
            status.update({
                "registered": False,
                "unregistered_at": _now_iso(),
                "onionheaven_address": hub,
                "content_address": addr,
            })
            # A later /online (OnionHeaven turned back on) must write the
            # record again.
            self.registration_succeeded = False
        _forget_left_on_hub(status, addr, hub)
        _save_registration_status(self.app, status)

    def release(self, reason, keep_address=None):
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
        if getattr(self.app, 'is_onionheaven', False):
            return []
        try:
            with self.hub_lock(timeout=RELEASE_LOCK_TIMEOUT):
                return self._release_locked(reason, keep_address)
        except HubBusy:
            remaining = [{"content_address": a, "onionheaven_address": h, "why": "busy"}
                         for a, h in registrations_left(self.app) if a != keep_address]
            _log_remaining(self.app, remaining)
            return remaining

    def _release_locked(self, reason, keep_address):
        status = _load_registration_status(self.app)
        pairs = [p for p in _registrations(status, self.app) if p[0] != keep_address]
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
            result = self.unregister(content_address=addr, hub=hub)
            if result is True:
                released_live = True
            else:
                remaining.append({"content_address": addr, "onionheaven_address": hub,
                                  "why": "unanswered" if result is False else "no-key"})
        # unregister rewrote the record: start from the file.
        status = _load_registration_status(self.app)
        main = _main_registration(status, self.app)
        for item in remaining:
            pair = (item["content_address"], item["onionheaven_address"])
            _note_left_on_hub(status, pair[0], pair[1], reason)
            if pair == main:
                status["registered"] = False
                self.registration_succeeded = False
        if released_live and not any(i["content_address"] == live for i in remaining):
            status["released"] = {"content_address": live,
                                  "until": time.time() + RELEASE_HOLD_SECONDS}
        _save_registration_status(self.app, status)
        _log_remaining(self.app, remaining)
        return remaining

    def end_hub_traffic(self):
        """Uninstall: nothing that carries the key goes to a hub from now on
        in this process, so no heartbeat can follow the /unregister."""
        self.ending = True

    # -- the opt-out and the record's upkeep ------------------------------

    def maybe_unregister_after_opt_out(self, now=None):
        """Called every status cycle once Tor is up: if the user has explicitly
        turned OnionHeaven off (REGISTER_WITH_ONIONHEAVEN=no) and this install's
        record says the hub may hold its key, send one signed /unregister in a
        background thread, then forget the registration.

        Covers both a "yes" -> "no" change while running (Mac Settings, the
        WordPress Settings page) and an install that starts with "no" but
        registered in an earlier session. Unset (which reads as yes) and an
        unrecognized value never send anything: the hub keeps the key and
        takes the address over while the site is down, as before. A failed attempt is retried every
        OPT_OUT_RETRY_SECONDS; one that cannot work (key changed, no usable
        hub) is not retried until the next launch.

        Returns the thread started, or None.
        """
        if getattr(self.app, 'is_onionheaven', False):
            return None
        if get_registration_choice(self.app) != "no":
            return None
        if self.opt_out_in_flight:
            return None
        if self.opt_out_gave_up:
            return None
        now = time.time() if now is None else now
        last = self.opt_out_last_attempt
        if last and now - last < OPT_OUT_RETRY_SECONDS:
            return None
        if not _load_registration_status(self.app).get("registered"):
            return None

        self.opt_out_in_flight = True
        self.opt_out_last_attempt = now
        thread = threading.Thread(target=self._unregister_after_opt_out, daemon=True)
        thread.start()
        return thread

    def _unregister_after_opt_out(self):
        try:
            self.log("OnionHeaven: turned off (REGISTER_WITH_ONIONHEAVEN=no) — "
                     "asking the hub to delete its copy of this site's key")
            result = self.unregister(while_choice="no")
            if get_registration_choice(self.app) != "no":
                # Turned back on meanwhile: a later "no" unregisters at once.
                self.opt_out_last_attempt = 0
            elif result is None:
                self.opt_out_gave_up = True
            elif result is False:
                self.log("OnionHeaven: will retry the /unregister in "
                         f"{OPT_OUT_RETRY_SECONDS // 60} min")
        except Exception as e:
            self.log(f"OnionHeaven: opt-out unregister error: {e}")
        finally:
            self.opt_out_in_flight = False

    def maintain_registration(self, now=None):
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
        if getattr(self.app, 'is_onionheaven', False):
            return None
        live = getattr(self.app, 'onion_address', None)
        if not key_manager.is_valid_onion_address(live):
            return None
        if self.left_in_flight:
            return None
        status = _load_registration_status(self.app)
        main = _main_registration(status, self.app)
        entries = _left_on_hubs(status)
        choice = get_registration_choice(self.app)

        for entry in entries:
            if entry["content_address"] != live and _pair(entry) not in self.left_reported:
                self.left_reported.add(_pair(entry))
                self.log(f"OnionHeaven: WARNING — {entry['onionheaven_address']} may "
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
        last = self.left_last_attempt
        if last and now - last < OPT_OUT_RETRY_SECONDS:
            # Asked (or looked) recently: only the local bookkeeping now.
            fix = retry = False
        if not (fix or drop or retry):
            return None
        self.left_in_flight = True
        if fix or retry:
            self.left_last_attempt = now
        thread = threading.Thread(target=self._maintain_registration,
                                  args=(live, fix or retry), daemon=True)
        thread.start()
        return thread

    def _maintain_registration(self, live, network=True):
        """maintain_registration's work, under the hub lock. Without
        *network* (asked recently), only the record's bookkeeping."""
        try:
            with self.hub_lock(timeout=HEARTBEAT_LOCK_TIMEOUT):
                choice = get_registration_choice(self.app)
                status = _load_registration_status(self.app)
                changed = False
                main = _main_registration(status, self.app)
                if main is not None and main[0] != live:
                    key_addr = _key_address() if network else None
                    if key_addr is not None and key_addr != main[0]:
                        _note_left_on_hub(status, main[0], main[1], "address changed")
                        status["registered"] = False
                        self.registration_succeeded = False
                        changed = True
                        self.log(f"OnionHeaven: WARNING — {main[1]} may still hold the "
                                 f"key of {main[0]}, which this install no longer has; "
                                 "OnionPress cannot ask the hub to delete it")
                elif (main is not None and main[1] != ONIONHEAVEN_ADDRESS
                        and key_manager.is_valid_onion_address(ONIONHEAVEN_ADDRESS)):
                    _note_left_on_hub(status, main[0], main[1], "hub changed")
                    status["registered"] = False
                    self.registration_succeeded = False
                    changed = True
                    self.log(f"OnionHeaven: hub changed from {main[1]} to "
                             f"{ONIONHEAVEN_ADDRESS}; asking {main[1]} to delete its "
                             "copy of this site's key")
                if choice == "yes" and _forget_left_on_hub(status, live, ONIONHEAVEN_ADDRESS):
                    changed = True
                if changed:
                    _save_registration_status(self.app, status)
                pairs = [_pair(e) for e in _left_on_hubs(status)
                         if e["content_address"] == live
                         and not (choice == "yes"
                                  and e["onionheaven_address"] == ONIONHEAVEN_ADDRESS)]
                for addr, hub in (pairs if network else []):
                    result = self.unregister(content_address=addr, hub=hub)
                    if result is False:
                        self.log(f"OnionHeaven: will ask {hub} again in "
                                 f"{OPT_OUT_RETRY_SECONDS // 60} min")
        except HubBusy:
            pass
        except Exception as e:
            self.log(f"OnionHeaven: registration upkeep error: {e}")
        finally:
            self.left_in_flight = False

    # -- /online and /offline notifications ------------------------------

    def _send_notification(self, endpoint, log_label, max_attempts=1, max_time=10):
        """Send a lifecycle notification (/online or /offline) to OnionHeaven.

        Builds payload with content_address, healthcheck_address, and ed25519 signature.
        Uses docker exec curl through the Tor SOCKS proxy.

        Sends nothing unless REGISTER_WITH_ONIONHEAVEN is "yes": an /offline
        makes the hub take the address over at once, which an opted-out
        install, or one whose value is unrecognized, must not ask for, and /online carries the key.
        /online also takes the hub lock, as _send_heartbeat does, and is not
        sent while uninstalling or for an address on the record's "released"
        hold; /offline takes no lock (it is sent inside the sleep deadline and
        carries no key).

        Returns True on success, False on failure.
        """
        if getattr(self.app, 'is_onionheaven', False):
            return False
        if get_registration_choice(self.app) != "yes":
            return False
        if endpoint == "online":
            try:
                with self.hub_lock(timeout=HEARTBEAT_LOCK_TIMEOUT):
                    if get_registration_choice(self.app) != "yes":
                        return False
                    if self.ending:
                        return False
                    if _address_on_hold(self.app, getattr(self.app, 'onion_address', None)):
                        return False
                    return self._send_notification_unlocked(
                        endpoint, log_label, max_attempts, max_time)
            except HubBusy:
                return False
        return self._send_notification_unlocked(
            endpoint, log_label, max_attempts, max_time)

    def _send_notification_unlocked(self, endpoint, log_label, max_attempts, max_time):
        # Check if we ever registered
        status = _load_registration_status(self.app)
        if not status.get("registered"):
            return False

        content_addr = getattr(self.app, 'onion_address', None)
        hc_addr = getattr(self.app, 'healthcheck_address', None)

        if not key_manager.is_valid_onion_address(content_addr):
            return False
        if not key_manager.is_valid_onion_address(hc_addr):
            return False
        url = _hub_url(endpoint)
        if url is None:
            self.log(f"OnionHeaven: hub address is not a valid onion address, "
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
                self.log(f"OnionHeaven: ABORT /{endpoint} — onion_address stale "
                         f"(app={content_addr}, key_derives_to={derived})")
                return False
            timestamp = onion_auth.make_timestamp()
            signature = onion_auth.sign_payload(
                secret_key_bytes, public_key_raw,
                endpoint, content_addr, hc_addr, timestamp
            )
        except Exception as e:
            self.log(f"OnionHeaven: failed to sign /{endpoint} request: {e}")
            return False

        payload_dict = {
            "content_address": content_addr,
            "healthcheck_address": hc_addr,
            "is_onionheaven": getattr(self.app, 'is_onionheaven', False),
            "timestamp": timestamp,
            "signature": signature,
        }

        # /online requires the arti key so OnionHeaven can do takeover if needed
        if endpoint == "online":
            try:
                arti_pem = key_manager.build_openssh_key(secret_key_bytes, public_key_raw)
                payload_dict["arti_key_pem"] = base64.b64encode(arti_pem).decode('ascii')
            except Exception as e:
                self.log(f"OnionHeaven: failed to build arti key for /{endpoint}: {e}")
                return False

        payload = json.dumps(payload_dict)

        backoff = [10, 30]
        last_output = ""

        for attempt in range(max_attempts):
            # Payload on stdin, like the heartbeat's: /online carries the
            # onion private key, which must stay out of argv.
            ok, output = self._run_docker([
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
                        self.log(f"OnionHeaven: /{endpoint} notification sent successfully")
                        return True
                    error_msg = resp.get("error", "unknown error")
                    self.log(f"OnionHeaven: /{endpoint} rejected: {error_msg}")
                    return False
                except json.JSONDecodeError:
                    self.log(f"OnionHeaven: /{endpoint} got non-JSON response: {output[:200]}")
            elif not ok:
                self.log(f"OnionHeaven: /{endpoint} curl failed (attempt {attempt + 1}/{max_attempts})")
            else:
                self.log(f"OnionHeaven: /{endpoint} got empty response (attempt {attempt + 1}/{max_attempts})")

            if attempt < max_attempts - 1:
                delay = backoff[min(attempt, len(backoff) - 1)]
                self.log(f"OnionHeaven: /{endpoint} retrying in {delay}s...")
                time.sleep(delay)

        self.log(f"OnionHeaven: /{endpoint} failed after {max_attempts} attempts — last response: {last_output!r}")
        return False

    def notify_offline(self, max_time=10):
        """Notify OnionHeaven that this instance is going offline (sleep/quit).

        Runs synchronously with a single attempt and short timeout —
        must complete before the system sleeps or the app quits.

        ``max_time`` is the curl --max-time bound (seconds). Callers driving
        this from inside an IOKit sleep delay-inhibitor (see
        :class:`onionpress.mac_power.MacPowerHandler`) should pass a tighter
        value (~5s) to leave headroom inside the 30s system suspend ceiling;
        the default (10s) is for non-time-critical callers like quit / stop.
        """
        if getattr(self.app, 'is_onionheaven', False):
            return False
        self.log("Notifying OnionHeaven: going offline")
        return self._send_notification("offline", "offline", max_attempts=1, max_time=max_time)

    def notify_online(self):
        """Notify OnionHeaven that this instance is back online (wake/reconnect).

        Uses the same heartbeat code path that's proven to work — sends /online
        with arti_key_pem, retries once on failure. Sets
        self.reclaim_succeeded on success; clears
        self.reclaim_in_flight when done.
        """
        if getattr(self.app, 'is_onionheaven', False):
            self.reclaim_in_flight = False
            return False
        self.log("Notifying OnionHeaven: coming online (via heartbeat)")
        try:
            result = self.send_heartbeat()
            if result:
                self.reclaim_succeeded = True
                self.log("OnionHeaven: reclaim succeeded")
            return bool(result)
        finally:
            self.reclaim_in_flight = False

    def start_online_notification_thread(self):
        """Spawn a daemon thread to send /online notification."""
        thread = threading.Thread(target=self.notify_online, daemon=True)
        thread.start()
        return thread


# ---------------------------------------------------------------------------
# The client's functions of an app
# ---------------------------------------------------------------------------
#
# For callers that hold no client yet (the CLI's HubContext through
# release_onionheaven, the tests' app objects): each is the method of the
# app's client (client_of). They go as those callers move to a client
# (issue #479).

def client_of(app):
    """*app*'s OnionHeavenClient: the one it holds as hub_client (the Mac
    daemon's), else a new one, kept there so its state lasts."""
    client = getattr(app, "hub_client", None)
    if not isinstance(client, OnionHeavenClient):
        client = OnionHeavenClient(app)
        app.hub_client = client
    return client


def register_with_onionheaven(app):
    return client_of(app).register()


def start_registration_thread(app):
    return client_of(app).start_registration_thread()


def _heartbeat_loop(app):
    return client_of(app).heartbeat_loop()


def _end_heartbeat_loop(app, my_generation, choice):
    return client_of(app)._end_heartbeat_loop(my_generation, choice)


def _send_heartbeat(app, wordpress_healthy=True):
    return client_of(app).send_heartbeat(wordpress_healthy)


def _mark_registered(app, content_addr):
    return client_of(app)._mark_registered(content_addr)


def unregister_from_onionheaven(app, content_address=None, while_choice=None, hub=None):
    return client_of(app).unregister(content_address=content_address,
                                     while_choice=while_choice, hub=hub)


def release_registration(app, reason, keep_address=None):
    return client_of(app).release(reason, keep_address=keep_address)


def end_hub_traffic(app):
    return client_of(app).end_hub_traffic()


def maybe_unregister_after_opt_out(app, now=None):
    return client_of(app).maybe_unregister_after_opt_out(now=now)


def _unregister_after_opt_out(app):
    return client_of(app)._unregister_after_opt_out()


def maintain_registration(app, now=None):
    return client_of(app).maintain_registration(now=now)


def _send_onionheaven_notification(app, endpoint, log_label, max_attempts=1, max_time=10):
    return client_of(app)._send_notification(endpoint, log_label,
                                             max_attempts=max_attempts, max_time=max_time)


def notify_onionheaven_offline(app, max_time=10):
    return client_of(app).notify_offline(max_time=max_time)


def notify_onionheaven_online(app):
    return client_of(app).notify_online()


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




# ---------------------------------------------------------------------------
# `onionpress onionheaven enable|disable|status [--json]` (issue #365)
# ---------------------------------------------------------------------------
#
# One implementation for both platforms (the Linux launcher's bash
# onionheaven_cmd delegates here). enable/disable write
# REGISTER_WITH_ONIONHEAVEN through config's `set` path, so the change is
# logged, the running menubar is told (its marker) and the heartbeat, which
# re-reads the choice before every request that carries the key, follows
# within a minute; disable's /unregister is the running app's
# (maybe_unregister_after_opt_out on a Mac, the Linux daemon), at its next
# start when none runs. status reads the config and the registration
# record only: no network.

REGISTRATION_EXPLANATION = """\
  OnionHeaven keeps your .onion address reachable when this machine is
  off, asleep, or disconnected. While OnionPress runs it sends a heartbeat
  to the OnionHeaven hub. If the heartbeats stop, the hub takes over your
  address and redirects visitors to the Wayback Machine's archived copy of
  your site. When you come back online, your live site takes over again.

  What is shared: a copy of your onion service private key is sent to the
  hub (run by the Internet Archive) so it can publish your address while
  you are away. It is used only to keep your site reachable.

  It is on by default. To turn it back on:
      onionpress onionheaven enable"""

ENABLE_TEXT = """\
OnionHeaven enabled. Your site will register with the hub within a minute
and stay reachable at your .onion address when this machine is off."""

DISABLE_TEXT = """\
OnionHeaven disabled. The heartbeats stop within a minute, and if this
site was registered, OnionPress asks the hub to delete its copy of your
onion service key (when OnionPress is not running: at its next start).
Visitors will get a connection error whenever this machine is off.
Re-enable at any time with: onionpress onionheaven enable"""


def set_registration_choice(app, choice):
    """Write REGISTER_WITH_ONIONHEAVEN=yes|no through the config CLI's
    path (checked, logged, marker for the running menubar)."""
    from onionpress import config as _config
    assert choice in ("yes", "no")
    reason = _config.check_config_value("REGISTER_WITH_ONIONHEAVEN", choice)
    if reason:
        raise ValueError(reason)
    change = _config.set_config_value(app.config_file, "REGISTER_WITH_ONIONHEAVEN", choice)
    app.log(f"OnionHeaven {'enabled' if choice == 'yes' else 'disabled'} by user "
            f"(onionpress onionheaven {'enable' if choice == 'yes' else 'disable'}): "
            f"{change.describe()}")
    if change.changed:
        _config.note_config_changed(app.app_support, [change.key])
    return change


def registration_status(app):
    """What `onionheaven status --json` prints: the choice, the configured
    hub, and what the registration record says hubs may hold of this
    install's keys. No network."""
    value = app.read_config_value("REGISTER_WITH_ONIONHEAVEN", "")
    choice = registration_choice(value)
    record = _load_registration_status(app)
    main = _main_registration(record, app)
    return {
        "enabled": {"yes": True, "no": False}.get(choice),
        "choice": _choice_label(choice),
        "value": value,
        "hub": configured_hub_address(app.read_config_value("ONIONHEAVEN_ADDRESS", "")),
        "registered": bool(main),
        "content_address": main[0] if main else None,
        "registered_with": main[1] if main else None,
        "may_hold_key": [list(pair) for pair in _registrations(record, app)],
    }


def _file_log_func(log_file):
    """A log function that writes to the log file only (the terminal gets
    a summary instead)."""
    def log(msg):
        if not log_file:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_file, "a") as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass
    return log


def _hub_context(data_dir=None, log_func=None):
    """HubContext for this install, with the configured hub and a docker
    environment key_manager.extract_keys (plain `docker`, this process's
    environment) can use: the launchers export DOCKER_HOST, a direct run
    gets the socket resolve_paths finds."""
    paths = resolve_paths(data_dir=data_dir)
    log = log_func or _file_log_func(paths.log_file)
    ctx = HubContext(paths, log)
    use_configured_hub(ctx.read_config_value("ONIONHEAVEN_ADDRESS", ""), log)
    os.environ.setdefault("DOCKER_HOST", f"unix://{paths.docker_socket}")
    if paths.bin_dir and os.path.isdir(paths.bin_dir):
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if paths.bin_dir not in path_dirs:
            os.environ["PATH"] = os.pathsep.join([paths.bin_dir] + path_dirs)
    return ctx


# Remaining items the user can do something about: start OnionPress (the
# key could not be read) or try again later (the hub did not answer).
_RETRYABLE = ("key-unreadable", "unanswered", "busy")


def release_onionheaven(reason, keep_address=None, data_dir=None, log_func=None, out=None):
    """Before this install forgets an onion service key (reset, key
    import, restore, uninstall): ask every OnionHeaven hub that may hold
    one of its keys to delete its copy (release_registration: a signed
    /unregister, the hub the record names), and tell the user on *out*
    (stdout) what happened. Nothing is printed when no hub holds
    anything. Returns what may remain on a hub ([] = nothing).
    """
    out = out or sys.stdout
    ctx = _hub_context(data_dir, log_func)
    pairs = [p for p in registrations_left(ctx) if p[0] != keep_address]
    if not pairs:
        return []
    print("Asking the OnionHeaven hub to delete its copy of this site's "
          "onion service key (this can take a few minutes over Tor)...", file=out)
    remaining = release_registration(ctx, reason, keep_address=keep_address)
    left = {(i["content_address"], i["onionheaven_address"]) for i in remaining}
    for addr, hub in pairs:
        if (addr, hub) not in left:
            print(f"  Done: the hub {hub} no longer holds the key of {addr}.", file=out)
    if remaining:
        print("", file=out)
        print("WARNING: " + describe_remaining(remaining).replace(
            "\n", "\nWARNING: "), file=out)
        if any(i["why"] in _RETRYABLE for i in remaining):
            print("To have it deleted, make sure OnionPress is running and "
                  "try again before the key is gone.", file=out)
        print("OnionPress keeps a note of it in "
              f"{os.path.join(ctx.app_support, REGISTRATION_RECORD)}.",
              file=out)
    return remaining


def run_onionheaven_command(sub, app, json_output=False, out=None):
    """The CLI subcommand. Exit 0 done; 1 unrecognized subcommand; 2 the
    config refused the value."""
    import sys as _sys
    out = out or _sys.stdout
    if sub in ("enable", "disable"):
        choice = "yes" if sub == "enable" else "no"
        try:
            set_registration_choice(app, choice)
        except (ValueError, OSError) as e:
            print(f"ERROR: {e}", file=_sys.stderr)
            return 2
        print(ENABLE_TEXT if sub == "enable" else DISABLE_TEXT, file=out)
        return 0
    if sub == "overview":
        # Both roles, for the OnionHeaven screen (below): the record and
        # the config, plus the registry's rows when this install is a hub
        # (docker exec into onionheaven; no network).
        ov = overview(app)
        if json_output:
            print(json.dumps(ov, indent=2, sort_keys=True), file=out)
        else:
            print(describe_overview(ov), file=out)
        return 0
    if sub != "status":
        print("Usage: onionpress onionheaven {enable|disable|status|overview} [--json]",
              file=_sys.stderr)
        return 1
    status = registration_status(app)
    if json_output:
        print(json.dumps(status, indent=2, sort_keys=True), file=out)
        return 0
    if status["enabled"] is True:
        print("OnionHeaven: enabled", file=out)
    elif status["enabled"] is False:
        print("OnionHeaven: disabled (enable with: onionpress onionheaven enable)", file=out)
    else:
        print(f"OnionHeaven: not enabled (REGISTER_WITH_ONIONHEAVEN={status['value']} "
              "is not yes or no)", file=out)
        print("", file=out)
        print(REGISTRATION_EXPLANATION, file=out)
    print(f"Hub: {status['hub']}", file=out)
    if status["may_hold_key"]:
        # What the registration record says hubs may hold of this
        # install's keys (now or from before a reset, key import, restore
        # or hub change).
        print("", file=out)
        print("A copy of the onion service key of these addresses may be held by", file=out)
        print("these OnionHeaven hubs (address, hub):", file=out)
        for addr, hub in status["may_hold_key"]:
            print(f"  {addr} {hub}", file=out)
    return 0


# ---------------------------------------------------------------------------
# The OnionHeaven screen: one view of both roles this install can play
# ---------------------------------------------------------------------------
#
# overview(app) answers two questions the Settings dialog, the WordPress
# page and `onionpress onionheaven overview` all show: which hub keeps this
# site reachable (the config's choice and hub, the registration record,
# the last heartbeat) and whom this install keeps reachable (a hub: the
# registry's rows). The hosts the project runs are named (KNOWN_HOSTS), so
# the screen can say "the Internet Archive's OnionHeaven" instead of 56
# characters, and say so when a hub is nobody it knows. status_block() is
# the status.json `onionheaven` object: the flat keys the status page has
# always read, plus this nested view.
#
# The registry is data anything mounting the hub volume can write: every
# row is checked here (a v3 address, a status from a fixed set, short
# printable timestamps and versions) before it is shown anywhere.

DEFAULT_ONIONHOME_ADDRESS = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"

KNOWN_HOSTS = (
    {"address": DEFAULT_ONIONHEAVEN_ADDRESS,
     "name": "the Internet Archive's OnionHeaven",
     "kind": "onionheaven", "operator": "Internet Archive",
     "note": "the hub new OnionPress installs use"},
    {"address": DEFAULT_ONIONHOME_ADDRESS,
     "name": "OnionHome",
     "kind": "onionhome", "operator": "Internet Archive",
     "note": "the onion name registry, not an OnionHeaven hub"},
)

# The hub settings the screen edits (config.SETTINGS_PAGE_KEYS checks them;
# the auto-promote threshold went with #353).
HUB_SETTINGS = ("ONIONHEAVEN_PROPAGATION_DELAY", "ONIONHEAVEN_MAX_SERVICES")

HUB_SITES_LIMIT = 200
_SITE_STATUSES = frozenset({"online", "taken-over", "unregistered"})
_REGISTRY_DB = f"{ONIONHEAVEN_DATA_DIR}/registry.db"
_SITES_SQL = (
    "SELECT content_address, COALESCE(status,''), COALESCE(last_healthy,''), "
    "COALESCE(last_checked,''), COALESCE(version,''), COALESCE(wordpress_healthy,'') "
    "FROM registry WHERE unregistered_at IS NULL "
    "ORDER BY status, last_healthy DESC LIMIT %d")
_COUNTS_SQL = (
    "SELECT COUNT(*), SUM(CASE WHEN status='online' THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN status='taken-over' THEN 1 ELSE 0 END) "
    "FROM registry WHERE unregistered_at IS NULL")


def known_host(address):
    """The KNOWN_HOSTS entry for *address* (a copy), or None."""
    address = (address or "").strip().lower()
    for host in KNOWN_HOSTS:
        if host["address"] == address:
            return dict(host)
    return None


def _short_text(value, limit=40):
    """*value* if it is short printable ASCII, else ''."""
    value = str(value or "")
    if len(value) > limit or not value.isascii() or not value.isprintable():
        return ""
    return value


def _registry_query(app, sql):
    """Rows of *sql* run in the onionheaven container, tab-separated, or
    None when the container did not answer."""
    ok, out = _run_docker(
        app, ["exec", "onionheaven", "sqlite3", "-separator", "\t", _REGISTRY_DB, sql],
        timeout=10)
    return out if ok else None


def hub_sites(app, limit=HUB_SITES_LIMIT):
    """The sites this hub keeps reachable, from the registry's current
    rows, each checked before it is shown; None when the registry did
    not answer."""
    out = _registry_query(app, _SITES_SQL % int(limit))
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        addr, status, last_healthy, last_checked, version, wp_ok = parts
        if not key_manager.is_valid_onion_address(addr):
            continue
        status = status.strip().lower()
        rows.append({
            "content_address": addr,
            "status": status if status in _SITE_STATUSES else "unknown",
            "last_healthy": _short_text(last_healthy),
            "last_checked": _short_text(last_checked),
            "version": _short_text(version, 32),
            "wordpress_healthy": {"1": True, "0": False}.get(wp_ok.strip()),
        })
    return rows


def hub_counts(app):
    """{registered_count, online_count, taken_over_count} from the
    registry, or None when it did not answer."""
    out = _registry_query(app, _COUNTS_SQL)
    if out is None:
        return None
    try:
        total, online, taken = (out.strip().split("\t") + ["", "", ""])[:3]
        return {"registered_count": int(total or 0), "online_count": int(online or 0),
                "taken_over_count": int(taken or 0)}
    except ValueError:
        return None


def takeover_worker_count(app):
    ok, out = _run_docker(app, ["ps", "--format", "{{.Names}}",
                                "--filter", "name=onionheaven-takeover"], timeout=5)
    if not ok:
        return 0
    import re
    return len([n for n in out.splitlines() if re.fullmatch(r"onionheaven-takeover-\d+", n.strip())])


def _own_address(app):
    """This install's served address: the app's, else the cached one."""
    addr = getattr(app, "onion_address", None) or ""
    if not key_manager.is_valid_onion_address(addr):
        from onionpress.config import cached_onion_address
        addr = cached_onion_address(app.app_support) or ""
    return addr if key_manager.is_valid_onion_address(addr) else ""


def client_overview(app):
    """Which hub keeps this site reachable: the choice, the hub and what
    is known about it, the registration record, the heartbeat. No docker,
    no network."""
    st = registration_status(app)
    record = _load_registration_status(app)
    hub = st["hub"]
    host = known_host(hub)
    own = _own_address(app)
    content = st["content_address"] or own
    left = []
    for entry in _left_on_hubs(record):
        h = known_host(entry["onionheaven_address"])
        left.append({
            "content_address": entry["content_address"],
            "onionheaven_address": entry["onionheaven_address"],
            "hub_known_as": h["name"] if h else None,
            "since": entry.get("since"),
            "reason": entry.get("reason"),
            # Only the key this install still has can sign the request
            # that asks the hub to delete its copy.
            "key_here": bool(own) and entry["content_address"] == own,
        })
    return {
        "enabled": st["enabled"],
        "choice": st["choice"],
        "value": st["value"],
        "hub": hub,
        "hub_known_as": host["name"] if host else None,
        "hub_kind": host["kind"] if host else None,
        "hub_is_default": hub == DEFAULT_ONIONHEAVEN_ADDRESS,
        "registered": st["registered"],
        "registered_with": st["registered_with"],
        "content_address": content,
        "healthcheck_address": getattr(app, "healthcheck_address", None) or "",
        "registered_at": record.get("registered_at"),
        "last_heartbeat_at": record.get("last_heartbeat_at"),
        "last_attempt": record.get("last_attempt"),
        "unregistered_at": record.get("unregistered_at"),
        "left_on_hubs": left,
        "retry_minutes": OPT_OUT_RETRY_SECONDS // 60,
    }


def hub_overview(app, sites=True):
    """Whom this install keeps reachable. A hub is an install that accepts
    registrations (ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes, hand-edited) or
    serves the canonical hub address; only then is the registry asked
    (docker exec into onionheaven)."""
    own = _own_address(app)
    accept = str(app.read_config_value("ONIONHEAVEN_ACCEPT_REGISTRATIONS", "") or "").strip().lower() == "yes"
    canonical = bool(own) and own == DEFAULT_ONIONHEAVEN_ADDRESS
    active = accept or canonical or bool(getattr(app, "is_onionheaven", False))
    host = known_host(own)
    ov = {
        "active": active,
        "accepting_registrations": accept or canonical,
        "address": own,
        "known_as": host["name"] if host else None,
        "is_canonical": canonical,
        "registered_count": 0, "online_count": 0, "taken_over_count": 0,
        "takeover_containers": 0,
        "counts_unavailable": False,
        "sites": [], "sites_unavailable": False, "sites_truncated": False,
        "sites_limit": HUB_SITES_LIMIT,
        "settings": {},
    }
    if not active:
        return ov
    from onionpress import config as _cfg
    for key in HUB_SETTINGS:
        default = _cfg.DEFAULTS.get(key) or _cfg.CODE_DEFAULTS.get(key, "")
        ov["settings"][key] = str(app.read_config_value(key, default) or default)
    counts = hub_counts(app)
    if counts is None:
        ov["counts_unavailable"] = True
    else:
        ov.update(counts)
    ov["takeover_containers"] = takeover_worker_count(app)
    if sites:
        rows = hub_sites(app)
        if rows is None:
            ov["sites_unavailable"] = True
        else:
            ov["sites"] = rows
            ov["sites_truncated"] = len(rows) >= HUB_SITES_LIMIT
    return ov


def overview(app, hub=True, sites=True):
    """Both roles: {"client", "hub", "known_hosts", "generated_at"}. Without
    *hub* the registry is not asked (no docker at all: the Mac dialog's
    fallback while the daemon's state is stale)."""
    return {
        "client": client_overview(app),
        "hub": hub_overview(app, sites=sites) if hub else None,
        "known_hosts": [dict(h) for h in KNOWN_HOSTS],
        "generated_at": _now_iso(),
    }


def status_block(ov, server_active=None):
    """status.json's `onionheaven` object: the flat keys onionpress-status.php
    has always read (*server_active* as the platform defines it, else the
    hub view's), plus the nested view for the OnionHeaven page."""
    c = ov["client"]
    h = ov.get("hub") or {}
    return {
        "server_active": bool(h.get("active")) if server_active is None else bool(server_active),
        "client_registered": bool(c["registered"]),
        "client_enabled": c["enabled"] is True,
        "client_hub": c["hub"],
        "registered_count": int(h.get("registered_count", 0) or 0),
        "online_count": int(h.get("online_count", 0) or 0),
        "taken_over_count": int(h.get("taken_over_count", 0) or 0),
        "takeover_containers": int(h.get("takeover_containers", 0) or 0),
        "client": c,
        "hub": ov.get("hub"),
        "known_hosts": ov.get("known_hosts", []),
        "generated_at": ov.get("generated_at"),
    }


def describe_overview(ov):
    """`onionpress onionheaven overview` as text."""
    c = ov["client"]
    h = ov.get("hub") or {}
    lines = []
    if c["enabled"] is True:
        state = "registered" if c["registered"] else "enabled, not registered yet"
    elif c["enabled"] is False:
        state = "disabled"
    else:
        state = f"not enabled (REGISTER_WITH_ONIONHEAVEN={c['value']} is not yes or no)"
    lines.append(f"Kept reachable by a hub: {state}")
    who = f" ({c['hub_known_as']})" if c["hub_known_as"] else " (not a known host)"
    lines.append(f"  Hub: {c['hub']}{who}")
    if c["content_address"]:
        lines.append(f"  Site: {c['content_address']}")
    if c["last_heartbeat_at"]:
        lines.append(f"  Last heartbeat accepted: {c['last_heartbeat_at']}")
    for entry in c["left_on_hubs"]:
        lines.append(f"  A copy of the key of {entry['content_address']} may still be held by "
                     f"{entry['onionheaven_address']} ({entry.get('reason') or '?'})")
    if h.get("active"):
        lines.append(f"Serving as an OnionHeaven hub: {h['registered_count']} registered, "
                     f"{h['online_count']} online, {h['taken_over_count']} taken over, "
                     f"{h['takeover_containers']} takeover worker(s)")
        for site in h.get("sites", []):
            lines.append(f"  {site['content_address']} {site['status']} {site['last_healthy'] or '-'} "
                         f"{site['version'] or '-'}")
    else:
        lines.append("Serving as an OnionHeaven hub: no")
    return "\n".join(lines)
