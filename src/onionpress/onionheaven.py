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
import json
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone

from onionpress import key_manager

# The hub the opt-in prompt names (run by the Internet Archive): the
# default, and what a malformed ONIONHEAVEN_ADDRESS falls back to.
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
    name only and the default hub is used, the one the opt-in prompt names,
    so the key never goes anywhere the user did not agree to.
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


def _hub_url(path):
    """http://<hub>:<port>/<path>, or None when ONIONHEAVEN_ADDRESS is not a
    well-formed onion address.

    Every request to the hub is built here. ONIONHEAVEN_ADDRESS is a module
    global that callers assign, so it is checked where it is used, not only
    where the config is read.
    """
    if not key_manager.is_valid_onion_address(ONIONHEAVEN_ADDRESS):
        return None
    return f"http://{ONIONHEAVEN_ADDRESS}:{ONIONHEAVEN_API_PORT}/{path}"


def _docker_env(app):
    """Build environment dict for docker commands using app's config."""
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{app.colima_home}/default/docker.sock"
    env["DOCKER_CONFIG"] = os.path.join(app.app_support, "docker-config")
    return env


def _docker_bin(app):
    """Return path to docker binary."""
    return os.path.join(app.bin_dir, "docker")


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
# Registration choice (opt-in)
# ---------------------------------------------------------------------------

# REGISTER_WITH_ONIONHEAVEN is tri-state:
#   "yes"  — the user opted in: register and heartbeat.
#   "no"   — the user opted out: never register, never ask again.
#   unset  — the user has not decided yet: do NOT register, and the UI
#            prompts them to enable it at startup once Tor is up.
# Registration is opt-in because enabling it sends the site's onion private
# key to the hub (see the prompt text below). Only an explicit "yes" turns
# it on — a missing or empty value must never silently register.

REGISTRATION_PROMPT_TITLE = "Keep your site reachable when your computer is off?"

REGISTRATION_PROMPT_MESSAGE = (
    "OnionHeaven keeps your .onion address working while your computer is "
    "off, asleep, or disconnected. When your heartbeats stop, the OnionHeaven "
    "hub takes over your address and sends visitors to the Wayback Machine's "
    "archived copy of your site. When you come back online, your live site "
    "takes over again automatically.\n\n"
    "Without it, visitors get a connection error whenever your computer is "
    "off.\n\n"
    "What is shared: a copy of your onion service private key is sent to the "
    "OnionHeaven hub, run by the Internet Archive, so it can publish your "
    "address on your behalf. The key is used only to keep your site reachable. "
    "If you later disable OnionHeaven, the hub stops using it.\n\n"
    "We strongly recommend enabling this. You can change it at any time under "
    "Settings."
)


def registration_choice(value):
    """Normalize a REGISTER_WITH_ONIONHEAVEN config value.

    Returns "yes", "no", or None (undecided). Anything other than an
    explicit yes/no — including an empty string or a missing key — is
    treated as undecided so a typo can never opt a user in.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("yes", "true", "1", "on"):
        return "yes"
    if v in ("no", "false", "0", "off"):
        return "no"
    return None


def get_registration_choice(app):
    """The user's current OnionHeaven choice from the config file."""
    return registration_choice(app.read_config_value("REGISTER_WITH_ONIONHEAVEN", ""))


# ---------------------------------------------------------------------------
# Registration client (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _registration_status_file(app):
    """Path to the onionheaven registration status file."""
    return os.path.join(app.app_support, "onionheaven-registration.json")


def _load_registration_status(app):
    """Load persisted registration status."""
    path = _registration_status_file(app)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"registered": False, "last_attempt": None, "onionheaven_address": ONIONHEAVEN_ADDRESS}


def _save_registration_status(app, status):
    """Persist registration status to disk."""
    path = _registration_status_file(app)
    try:
        with open(path, 'w') as f:
            json.dump(status, f, indent=2)
    except OSError:
        pass


def register_with_onionheaven(app):
    """
    Register this instance with OnionHeaven by starting the heartbeat loop.

    The first heartbeat includes the arti_key_pem, effectively combining
    registration and heartbeat into a single /online call. Called from a
    background thread.
    Sets app._onionheaven_registration_succeeded on success;
    clears app._onionheaven_registration_in_flight when done.
    """
    # Opt-in: only an explicit "yes" registers. Unset means the user has
    # not decided yet (the menubar prompts them); "no" means they opted out.
    choice = get_registration_choice(app)
    if choice != "yes":
        app.log("OnionHeaven registration not enabled "
                f"(REGISTER_WITH_ONIONHEAVEN={'no' if choice == 'no' else 'unset'})")
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

    Registration happens via the first /online heartbeat (which includes
    the arti key). Subsequent heartbeats omit the key.
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
    Keeps running until the app quits (daemon thread).

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

    On the first successful call (or if key hasn't been sent yet), includes
    the arti_key_pem so OnionHeaven can do takeover if needed. Subsequent
    calls omit the key to save bandwidth.
    """
    if getattr(app, 'is_onionheaven', False):
        return False

    content_addr = getattr(app, 'onion_address', None)
    hc_addr = getattr(app, 'healthcheck_address', None)

    if not key_manager.is_valid_onion_address(content_addr):
        return False
    if not key_manager.is_valid_onion_address(hc_addr):
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
                # Save registration status on first success
                if not getattr(app, '_onionheaven_registration_succeeded', False):
                    app._onionheaven_registration_succeeded = True
                    _save_registration_status(app, {
                        "registered": True,
                        "last_attempt": datetime.now(timezone.utc).isoformat(),
                        "onionheaven_address": ONIONHEAVEN_ADDRESS,
                        "content_address": content_addr,
                    })
                    app.log(f"OnionHeaven: registered via heartbeat ({content_addr})")
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
            app._onionheaven_heartbeat_succeeded = True
            return True
        app.log(f"OnionHeaven: heartbeat failed after retry: curl_rc={rc}, output={repr(output[:200]) if output else 'empty'}")
        return False


def unregister_from_onionheaven(app, content_address=None):
    """
    Unregister this instance from OnionHeaven.
    Called during uninstall or address prefix change so OnionHeaven stops
    monitoring an address that will never come back.

    Runs synchronously — caller should invoke from a background thread if needed.
    Best-effort: logs failures but does not raise.
    """
    # Don't unregister OnionHeaven from itself
    if getattr(app, 'is_onionheaven', False):
        return

    # Check if we ever registered
    status = _load_registration_status(app)
    if not status.get("registered"):
        app.log("OnionHeaven: not registered, skipping unregister")
        return

    addr = content_address or status.get("content_address") or getattr(app, 'onion_address', None)
    if not key_manager.is_valid_onion_address(addr):
        app.log("OnionHeaven: no content address for unregister")
        return
    unregister_url = _hub_url("unregister")
    if unregister_url is None:
        app.log("OnionHeaven: hub address is not a valid onion address, "
                "not sending /unregister")
        return

    app.log(f"Unregistering {addr} from OnionHeaven...")

    # Sign the unregister request with ed25519
    try:
        from onionpress import onion_auth
        secret_key_bytes, public_key_raw = key_manager.extract_keys()
    except Exception as e:
        app.log(f"OnionHeaven: failed to extract keys for unregister: {e}")
        return

    # Same staleness guard as _send_heartbeat / _send_onionheaven_notification:
    # refuse to unregister an address that doesn't match the live key. addr
    # comes from a caller-supplied parameter, a registration-status cache,
    # or self.onion_address — all three can lag the container's actual key
    # after a vanity rotation or restore. Without this check we'd sign with
    # the new key but ship the old address, which the server-side guard
    # (542cb61b) rejects — but we'd still waste the round-trip and leak the
    # mismatch into logs without a clear KEY-MISMATCH marker.
    derived = onion_auth.derive_onion_address(public_key_raw)
    if derived != addr:
        app.log(f"OnionHeaven: ABORT unregister — content_address stale "
                f"(app={addr}, key_derives_to={derived})")
        return

    hc_addr = getattr(app, 'healthcheck_address', None)
    hc_addr_val = hc_addr if key_manager.is_valid_onion_address(hc_addr) else ""

    timestamp = onion_auth.make_timestamp()
    signature = onion_auth.sign_payload(
        secret_key_bytes, public_key_raw,
        "unregister", addr, hc_addr_val, timestamp
    )

    payload_dict = {
        "content_address": addr,
        "timestamp": timestamp,
        "signature": signature,
    }
    if hc_addr_val:
        payload_dict["healthcheck_address"] = hc_addr_val
    payload = json.dumps(payload_dict)

    # Retry with backoff — uninstall/address-change are one-shot, reliability > speed
    backoff = [5, 15, 30]
    max_attempts = 4
    last_output = ""

    for attempt in range(max_attempts):
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
                if resp.get("unregistered"):
                    app.log("OnionHeaven: unregistered successfully")
                    _save_registration_status(app, {
                        "registered": False,
                        "unregistered_at": datetime.now(timezone.utc).isoformat(),
                        "onionheaven_address": ONIONHEAVEN_ADDRESS,
                        "content_address": addr,
                    })
                    return
                error_msg = resp.get("error", "unknown error")
                app.log(f"OnionHeaven: unregister rejected: {error_msg}")
                # Don't retry on auth/validation errors — they won't fix themselves
                if resp.get("error"):
                    return
            except json.JSONDecodeError:
                pass

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            app.log(f"OnionHeaven: unregister attempt {attempt + 1} failed, retrying in {delay}s...")
            time.sleep(delay)

    app.log(f"OnionHeaven: unregister failed after {max_attempts} attempts (best-effort, continuing) — last response: {last_output!r}")


# ---------------------------------------------------------------------------
# Online/offline notifications (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _send_onionheaven_notification(app, endpoint, log_label, max_attempts=1, max_time=10):
    """Send a lifecycle notification (/online or /offline) to OnionHeaven.

    Builds payload with content_address, healthcheck_address, and ed25519 signature.
    Uses docker exec curl through the Tor SOCKS proxy.

    Returns True on success, False on failure.
    """
    if getattr(app, 'is_onionheaven', False):
        return False

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


