#!/usr/bin/env python3
"""
OnionCellar — failover system for OnionPress

When an OnionPress instance's .onion address goes offline, the cellar takes over
the address and serves 302 redirects to the Internet Archive Wayback Machine.

Two roles:
  1. Registration client (normal instances): sends keys to the cellar
  2. Cellar mode (cellar instance): monitors registered instances, takes over on failure
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

# The cellar's .onion address — placeholder until a real address is generated
CELLAR_ADDRESS = "ocellarg3xj7hpw25etw34glkjsels5q6knyxe6rmomsjplckwnexdqd.onion"

# Wayback Machine .onion address
WAYBACK_ONION = "archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# Paths inside containers
CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"
CELLAR_REGISTRY_FILE = f"{CELLAR_DATA_DIR}/registry.json"
CELLAR_KEYS_DIR = f"{CELLAR_DATA_DIR}/keys"

# Healthcheck intervals (seconds)
HEALTHY_INTERVAL = 300       # 5 minutes when healthy
FAST_POLL_INTERVAL = 15      # 15 seconds after recent failure/recovery
LONG_FAIL_INTERVAL = 1800    # 30 minutes after prolonged failure

# Thresholds
FAIL_THRESHOLD = 3           # consecutive failures before takeover
FAST_POLL_COUNT = 20         # how many fast polls before slowing down


def _docker_env(app):
    """Build environment dict for docker commands using app's config."""
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{app.colima_home}/default/docker.sock"
    env["DOCKER_CONFIG"] = os.path.join(app.app_support, "docker-config")
    return env


def _docker_bin(app):
    """Return path to docker binary."""
    return os.path.join(app.bin_dir, "docker")


def _run_docker(app, args, timeout=15):
    """Run a docker command and return (success, stdout)."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode == 0, result.stdout.strip()
    except Exception:
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


def _run_docker_rc(app, args, timeout=15):
    """Run a docker command and return (returncode, stdout).
    Unlike _run_docker, returns the actual return code rather than a bool."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return -1, ""


# ---------------------------------------------------------------------------
# Registration client (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _registration_status_file(app):
    """Path to the cellar registration status file."""
    return os.path.join(app.app_support, "cellar-registration.json")


def _load_registration_status(app):
    """Load persisted registration status."""
    path = _registration_status_file(app)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"registered": False, "last_attempt": None, "cellar_address": CELLAR_ADDRESS}


def _save_registration_status(app, status):
    """Persist registration status to disk."""
    path = _registration_status_file(app)
    try:
        with open(path, 'w') as f:
            json.dump(status, f, indent=2)
    except OSError:
        pass


def register_with_cellar(app):
    """
    Register this instance with the OnionCellar.
    Sends content address, healthcheck address, and secret key to the cellar.
    Called from a background thread; tries once per app startup.
    """
    # Check if registration is disabled
    if app.read_config_value("REGISTER_WITH_CELLAR", "yes").lower() == "no":
        app.log("OnionCellar registration disabled (REGISTER_WITH_CELLAR=no)")
        return

    # Don't register with ourselves
    if getattr(app, 'is_cellar', False):
        return

    content_addr = app.onion_address
    hc_addr = app.healthcheck_address

    if not content_addr or not content_addr.endswith('.onion'):
        app.log("OnionCellar: no content address available, skipping registration")
        return
    if not hc_addr or not hc_addr.endswith('.onion'):
        app.log("OnionCellar: no healthcheck address available, skipping registration")
        return

    app.log("Registering with OnionCellar...")

    # Extract the secret key (raw bytes)
    try:
        import key_manager
        secret_key_bytes = key_manager.extract_private_key()
    except Exception as e:
        app.log(f"OnionCellar: failed to extract key: {e}")
        return

    # Also get the public key
    ok, public_key_raw = _run_docker_raw(app, [
        "exec", "onionpress-tor", "cat",
        "/var/lib/tor/hidden_service/wordpress/hs_ed25519_public_key"
    ])
    if not ok:
        app.log("OnionCellar: failed to read public key")
        return

    # Build registration payload (keys as base64-encoded strings)
    import base64
    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "secret_key": base64.b64encode(secret_key_bytes).decode('ascii'),
        "public_key": base64.b64encode(public_key_raw).decode('ascii'),
        "version": getattr(app, 'version', 'unknown'),
    })

    # Send via wordpress container's curl through tor SOCKS proxy
    # (per CLAUDE.md: use docker exec for all Tor communication)
    ok, output = _run_docker(app, [
        "exec", "onionpress-wordpress",
        "curl", "-s", "-X", "POST",
        "--socks5-hostname", "onionpress-tor:9050",
        "-H", "Content-Type: application/json",
        "-d", payload,
        "--max-time", "30",
        f"http://{CELLAR_ADDRESS}/register"
    ], timeout=45)

    if ok and output:
        try:
            resp = json.loads(output)
            if resp.get("registered"):
                app.log("OnionCellar: registration successful")
                _save_registration_status(app, {
                    "registered": True,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "cellar_address": CELLAR_ADDRESS,
                    "content_address": content_addr,
                })
                return
            if resp.get("locked"):
                app.log("OnionCellar: cellar is locked, deferring registration (will retry on next startup)")
                _save_registration_status(app, {
                    "registered": False,
                    "locked": True,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "cellar_address": CELLAR_ADDRESS,
                })
                return
        except json.JSONDecodeError:
            pass

    app.log("OnionCellar: registration failed (will retry on next startup)")
    _save_registration_status(app, {
        "registered": False,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
        "cellar_address": CELLAR_ADDRESS,
    })


def start_registration_thread(app):
    """Start the registration background thread."""
    thread = threading.Thread(target=register_with_cellar, args=(app,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Cellar mode (runs on the OnionCellar instance)
# ---------------------------------------------------------------------------

def is_cellar_instance(onion_address):
    """Check if this instance's address matches the cellar address."""
    if not onion_address or not onion_address.endswith('.onion'):
        return False
    return onion_address.strip() == CELLAR_ADDRESS.strip()


def _is_cellar_unlocked(app):
    """Check if the cellar master key is currently unlocked."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-tor",
        "test", "-f", f"{CELLAR_DATA_DIR}/.master-key-unlocked"
    ])
    return ok


def _read_registry(app):
    """Read the cellar registry from the wordpress container."""
    ok, output = _run_docker(app, [
        "exec", "onionpress-wordpress",
        "cat", CELLAR_REGISTRY_FILE
    ])
    if ok and output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def _write_registry(app, registry):
    """Write the cellar registry to the wordpress container."""
    registry_json = json.dumps(registry, indent=2)
    # Write via sh -c with heredoc to avoid escaping issues
    _run_docker(app, [
        "exec", "onionpress-wordpress",
        "sh", "-c", f"mkdir -p {CELLAR_DATA_DIR} && cat > {CELLAR_REGISTRY_FILE} << 'REGEOF'\n{registry_json}\nREGEOF"
    ])


def _check_healthcheck(app, healthcheck_address):
    """Check if a healthcheck .onion address is reachable. Returns True if healthy."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-tor",
        "wget", "-q", "-O", "/dev/null", "--timeout=10",
        f"http://{healthcheck_address}/"
    ], timeout=20)
    return ok


def _check_content(app, content_address):
    """Check if a content .onion address is reachable. Returns True if reachable."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-tor",
        "wget", "-q", "-O", "/dev/null", "--timeout=10",
        f"http://{content_address}/"
    ], timeout=20)
    return ok


def _do_takeover(app, entry):
    """Take over a failed instance's .onion address.
    Returns: 'ok', 'locked', or 'failed'."""
    content_addr = entry["content_address"]
    app.log(f"OnionCellar: Taking over {content_addr}")

    # Use cellar-tor-manager.sh inside the tor container to add the address
    rc, output = _run_docker_rc(app, [
        "exec", "onionpress-tor",
        "/cellar-tor-manager.sh", "takeover", content_addr
    ], timeout=30)

    if rc == 0:
        app.log(f"OnionCellar: Takeover complete for {content_addr}")
        return "ok"
    elif rc == 2:
        app.log(f"OnionCellar: Cellar locked, deferring takeover for {content_addr}")
        return "locked"
    else:
        app.log(f"OnionCellar: Takeover failed for {content_addr}: {output}")
        return "failed"


def _do_release(app, entry):
    """Release a recovered instance's .onion address."""
    content_addr = entry["content_address"]
    app.log(f"OnionCellar: Releasing {content_addr} — original is back online")

    ok, output = _run_docker(app, [
        "exec", "onionpress-tor",
        "/cellar-tor-manager.sh", "release", content_addr
    ], timeout=30)

    if ok:
        app.log(f"OnionCellar: Released {content_addr}")
    else:
        app.log(f"OnionCellar: Release failed for {content_addr}: {output}")

    return ok


def cellar_poller(app):
    """
    Main cellar polling loop. Monitors registered instances and manages takeover/release.
    Runs as a background thread on the cellar instance.
    """
    app.log("OnionCellar: healthcheck poller started")

    # Wait for services to be ready
    while not app.is_ready:
        time.sleep(10)

    while True:
        try:
            registry = _read_registry(app)
            if not registry:
                time.sleep(HEALTHY_INTERVAL)
                continue

            modified = False
            min_sleep = HEALTHY_INTERVAL

            for entry in registry:
                content_addr = entry.get("content_address", "")
                hc_addr = entry.get("healthcheck_address", "")
                fail_count = entry.get("fail_count", 0)
                takeover_active = entry.get("takeover_active", False)
                fast_poll_remaining = entry.get("_fast_poll_remaining", 0)

                if not content_addr or not hc_addr:
                    continue

                # Check healthcheck
                hc_ok = _check_healthcheck(app, hc_addr)

                if hc_ok:
                    if takeover_active:
                        # Instance recovered — the healthcheck address is independent
                        # (not taken over), so it being reachable means the original
                        # instance is back online. Release the content address.
                        _do_release(app, entry)
                        entry["takeover_active"] = False
                        entry["status"] = "healthy"
                        entry["fail_count"] = 0
                        entry["_fast_poll_remaining"] = FAST_POLL_COUNT
                        modified = True
                    elif fail_count > 0:
                        # Was failing, now recovering
                        entry["fail_count"] = 0
                        entry["status"] = "healthy"
                        entry["_fast_poll_remaining"] = FAST_POLL_COUNT
                        modified = True
                    else:
                        entry["status"] = "healthy"
                else:
                    # Healthcheck failed
                    new_fail_count = fail_count + 1
                    entry["fail_count"] = new_fail_count
                    entry["status"] = "failing"
                    entry["_fast_poll_remaining"] = FAST_POLL_COUNT
                    modified = True

                    if new_fail_count >= FAIL_THRESHOLD and not takeover_active:
                        # Check if cellar is unlocked before attempting takeover
                        if not _is_cellar_unlocked(app):
                            entry["status"] = "takeover_deferred_locked"
                            modified = True
                        else:
                            # Double-check: also test the content address
                            content_ok = _check_content(app, content_addr)
                            if not content_ok:
                                result = _do_takeover(app, entry)
                                if result == "ok":
                                    entry["takeover_active"] = True
                                    entry["status"] = "taken_over"
                                elif result == "locked":
                                    entry["status"] = "takeover_deferred_locked"
                                else:
                                    entry["status"] = "takeover_failed"
                                modified = True

                # Update timestamp
                entry["last_healthcheck"] = datetime.now(timezone.utc).isoformat()

                # Determine sleep interval for this entry
                if fast_poll_remaining > 0:
                    entry["_fast_poll_remaining"] = fast_poll_remaining - 1
                    min_sleep = min(min_sleep, FAST_POLL_INTERVAL)
                    modified = True
                elif takeover_active:
                    min_sleep = min(min_sleep, LONG_FAIL_INTERVAL)
                else:
                    min_sleep = min(min_sleep, HEALTHY_INTERVAL)

            if modified:
                _write_registry(app, registry)

            time.sleep(min_sleep)

        except Exception as e:
            app.log(f"OnionCellar: poller error: {e}")
            time.sleep(60)


def start_cellar_poller(app):
    """Start the cellar healthcheck polling thread."""
    thread = threading.Thread(target=cellar_poller, args=(app,), daemon=True)
    thread.start()
    return thread
