#!/usr/bin/env python3
"""Tor control port watchdog — monitors Tor health and manages onion services.

Runs inside every Tor container. Connects to the local control port,
manages onion services via ADD_ONION/DEL_ONION, subscribes to events,
and recovers from failures (stale guards, bootstrap stalls, etc.).

Signal protocol (from host MenubarApp via docker exec kill):
  USR1 = sleep  → DEL_ONION all services (Tor stays running with circuits)
  USR2 = wake   → ADD_ONION all services (re-publish on existing circuits)

Republish request (from the host app via docker exec, issue #266):
  a file at REPUBLISH_REQUEST_PATH, written by
  onionpress.launcher_ops.request_watchdog_republish() once the app has
  watched the service be unreachable through the Tor network for 10+ min
  while Tor itself looked healthy. The watchdog answers with DEL_ONION +
  ADD_ONION (fresh intro points, immediate descriptor upload) — the same
  action the HS_DESC stall monitor has run thousands of times in the
  field — subject to its own cooldown. Level-triggered, so a request that
  lands during a cooldown or a frozen container is acted on later rather
  than lost or duplicated.

Supervision (issue #313): entrypoint.sh runs this under supervise_watchdog,
which restarts it with backoff whenever it exits and kills it when
ALIVE_PATH goes stale (a hung loop). The services survive a restart (they
are added with Flags=Detach), and so does what the host told us: the sleep
state is kept in SLEEPING_PATH and a pending republish request stays in
its file. The supervisor clears both once per container start, so neither
outlives the container.

Usage: Started by entrypoint.sh (supervise_watchdog) after Tor launches.
"""

import base64
import glob
import json
import os
import re
import signal
import stat
import socket
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9051
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"

# Rate limits (seconds)
DROPGUARDS_COOLDOWN = 300
DORMANT_COOLDOWN = 120       # 2 min after DROPGUARDS → try DORMANT/ACTIVE
HALT_COOLDOWN = 300  # 5 minutes — last resort

# Detection thresholds
FAILED_NODE_THRESHOLD = 5       # failures within window → DROPGUARDS
FAILED_NODE_WINDOW = 60         # seconds
BOOTSTRAP_STALL_TIMEOUT = 120   # no progress for 2 min → DROPGUARDS
HS_DESC_UPLOAD_TIMEOUT = 60     # no descriptor upload 60s after recovery → DEL+ADD

# Host-requested republish (issue #266). Path must match
# onionpress.launcher_ops.WATCHDOG_REPUBLISH_REQUEST_PATH (test-enforced).
REPUBLISH_REQUEST_PATH = "/run/tor-watchdog/republish-requested"
REPUBLISH_COOLDOWN = 900            # at most one forced republish per 15 min
REPUBLISH_MIN_AGE_AFTER_UPLOAD = 300  # never tear down a descriptor < 5 min old

# Liveness and the sleep state, beside the request file (issue #313).
# ALIVE_PATH is touched every pass of every loop (seconds apart; tens of
# seconds when control-port calls time out); entrypoint.sh's supervisor kills a watchdog whose file
# is 300 s old, and launcher_ops.request_watchdog_republish reports a
# request as unheard when it is 120 s old. Must match
# WATCHDOG_ALIVE_PATH in onionpress.launcher_ops and WATCHDOG_RUN_DIR in
# entrypoint.sh (test-enforced). SLEEPING_PATH exists between USR1 and
# USR2, so a watchdog restarted while the host sleeps does not ADD_ONION.
ALIVE_PATH = "/run/tor-watchdog/alive"
SLEEPING_PATH = "/run/tor-watchdog/sleeping"
# Reconnect delay when control port isn't available yet
CONNECT_RETRY_DELAY = 5

# Hidden service key paths
HS_BASE_DIR = "/var/lib/tor/hidden_service"
SERVICES_JSON_PATH = "/etc/tor/onion-services.json"  # written by entrypoint.sh
# The delivered key (the install's identity): <name>/<DELIVERED_KEY_NAME>
# in the onion-keys volume. entrypoint.sh derives C Tor's files from it.
KEYS_DIR = "/var/lib/onionpress-keys"
DELIVERED_KEY_NAME = "ks_hs_id.ed25519_expanded_private"
KEY_CONVERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "key-convert.py")

# The content address in the shared volume. WordPress (www-data) can write
# that folder, so what is read there is untrusted: it goes into control-port
# commands (GETINFO, HSFETCH), where a CR/LF in a value that merely ends in
# ".onion" would start a command of the writer's choosing. Only one whole v3
# address passes, and a link at the name is not followed.
SHARED_ONION_ADDRESS_PATH = "/var/lib/onionpress/onion_address"
_ONION_ADDRESS_RE = re.compile(r"[a-z2-7]{56}\.onion")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg):
    ts = time.strftime("%b %d %H:%M:%S", time.gmtime())
    print(f"{ts} [tor-watchdog] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Liveness and sleep state (issue #313)
# ---------------------------------------------------------------------------
def touch_alive():
    """Mark this watchdog alive for the supervisor and the host (best effort)."""
    try:
        os.makedirs(os.path.dirname(ALIVE_PATH), exist_ok=True)
        with open(ALIVE_PATH, "a"):
            pass
        os.utime(ALIVE_PATH)
    except OSError:
        pass


def set_sleep_marker(sleeping):
    """Record the host's sleep state so it survives a watchdog restart."""
    try:
        if sleeping:
            os.makedirs(os.path.dirname(SLEEPING_PATH), exist_ok=True)
            with open(SLEEPING_PATH, "w") as f:
                f.write(f"{int(time.time())}\n")
        else:
            os.remove(SLEEPING_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"Warning: could not record sleep state: {e}")


def sleep_marker_present():
    return os.path.exists(SLEEPING_PATH)


# ---------------------------------------------------------------------------
# Control port connection
# ---------------------------------------------------------------------------
def read_cookie():
    """Read the Tor control auth cookie file."""
    for _ in range(60):  # retry for up to 5 minutes
        try:
            with open(COOKIE_PATH, "rb") as f:
                return f.read()
        except FileNotFoundError:
            touch_alive()
            time.sleep(CONNECT_RETRY_DELAY)
    return None


def connect_and_auth():
    """Connect to control port and authenticate. Returns socket or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((CONTROL_HOST, CONTROL_PORT))

        cookie = read_cookie()
        if cookie is None:
            log("Could not read auth cookie — giving up")
            s.close()
            return None

        s.sendall(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        resp = recv_response(s)
        if not resp.startswith("250"):
            log(f"Authentication failed: {resp.strip()}")
            s.close()
            return None

        return s
    except (ConnectionRefusedError, OSError) as e:
        return None


def recv_response(s):
    """Read a single control port response (may be multi-line)."""
    buf = b""
    while True:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        # Single-line response: "250 OK\r\n"
        # Multi-line: "250-first\r\n250 last\r\n"
        lines = buf.decode("utf-8", errors="replace").split("\r\n")
        for line in lines:
            if line and len(line) >= 4 and line[3] == " ":
                return buf.decode("utf-8", errors="replace")
    return buf.decode("utf-8", errors="replace")


def send_cmd(s, cmd):
    """Send a command and return the response."""
    try:
        s.sendall((cmd + "\r\n").encode())
        return recv_response(s)
    except (BrokenPipeError, OSError) as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Onion service management (ADD_ONION / DEL_ONION)
# ---------------------------------------------------------------------------

def _read_ed25519_key(secret_key_path):
    """Read a C Tor hs_ed25519_secret_key file and return base64 for ADD_ONION."""
    with open(secret_key_path, "rb") as f:
        data = f.read()
    if len(data) != 96:
        raise ValueError(f"Secret key wrong size: {len(data)} (expected 96)")
    # 32-byte header + 64-byte expanded key
    expanded_key = data[32:]
    return base64.b64encode(expanded_key).decode("ascii")


def _derive_onion_address(public_key_path):
    """Derive .onion address from hs_ed25519_public_key file.

    v3 onion address = base32(pubkey + checksum + version)
    checksum = SHA3-256(".onion checksum" + pubkey + version)[:2]
    """
    import hashlib
    with open(public_key_path, "rb") as f:
        data = f.read()
    if len(data) != 64:
        raise ValueError(f"Public key wrong size: {len(data)} (expected 64)")
    pubkey = data[32:]  # 32-byte header + 32-byte key
    version = b'\x03'
    checksum = hashlib.sha3_256(b".onion checksum" + pubkey + version).digest()[:2]
    addr_bytes = pubkey + checksum + version
    return base64.b32encode(addr_bytes).decode("ascii").lower() + ".onion"


def _read_hostname(path):
    """The one v3 address in the hostname file at path, or None (no file, a
    link, not a regular file, or anything but one whole address). A file
    that is there but not an address is logged."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read(128) if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else b""
    hostname = raw.decode("ascii", "replace").strip()
    if _ONION_ADDRESS_RE.fullmatch(hostname):
        return hostname
    log(f"{path} does not hold one onion address; using the public key's")
    return None


def discover_services():
    """Find onion services from /etc/tor/onion-services.json + keys on disk.

    The JSON file is written by entrypoint.sh with service names and port
    mappings. Keys and hostnames live at /var/lib/tor/hidden_service/<name>/.

    Returns list of dicts with 'service_id', 'service_name', 'key_b64', 'ports'.
    """
    # Read service definitions from JSON
    try:
        with open(SERVICES_JSON_PATH) as f:
            svc_defs = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"No onion-services.json found ({e}) — no services to manage")
        return []

    services = []
    for svc_def in svc_defs:
        name = svc_def.get("name", "")
        ports = svc_def.get("ports", [])
        if not name or not ports:
            continue

        service_dir = os.path.join(HS_BASE_DIR, name)

        # Read hostname for service_id (or derive from public key). The id
        # goes into DEL_ONION on the control port, and a restore can put
        # these files here from a backup: only one whole v3 address is
        # taken, anything else is replaced by the public key's address.
        hostname_file = os.path.join(service_dir, "hostname")
        public_key_file = os.path.join(service_dir, "hs_ed25519_public_key")
        service_id = None
        hostname = _read_hostname(hostname_file)
        if hostname is None:
            # No hostname file (fresh install), or not an address
            try:
                hostname = _derive_onion_address(public_key_file)
                # Write it so we don't have to derive again
                _write_atomic(hostname_file, (hostname + "\n").encode("ascii"))
                log(f"Derived hostname for {name}: {hostname}")
            except (OSError, ValueError):
                # No hostname or public key — will generate with NEW:BEST
                hostname = None
        if hostname:
            service_id = hostname[:-len(".onion")]

        secret_key_file = os.path.join(service_dir, "hs_ed25519_secret_key")
        key_b64 = None
        refuse_new = False
        try:
            key_b64 = _read_ed25519_key(secret_key_file)
        except (OSError, ValueError) as e:
            key_error = e
            pem_path = os.path.join(KEYS_DIR, name, DELIVERED_KEY_NAME)
            traces = _identity_traces(service_dir, pem_path)
            if not traces:
                # Nothing anywhere: a fresh install with no delivered key.
                log(f"No key for {name} — will generate with NEW:BEST")
            else:
                # Issue #311: this service has (or had) an address. A key
                # that cannot be read is a damaged file or a disk that is
                # not there yet, never permission to become another site.
                # The delivered key gives back the same address; failing
                # that, the service stays down rather than mint a new one.
                pem = _key_from_delivered_pem(pem_path) if "delivered key" in traces else None
                if pem:
                    key_b64, service_id = pem
                    log(f"WARNING: C Tor key for {name} is unusable ({key_error}); "
                        f"serving {service_id[:16]}... from the delivered key. "
                        f"The next container start re-derives C Tor's key files.")
                else:
                    refuse_new = True
                    log(f"ERROR: REFUSING to mint a new onion address for {name}: "
                        f"its key is unusable ({key_error}) but {', '.join(traces)} "
                        f"exist{'s' if len(traces) == 1 else ''}"
                        + (f" (address {service_id[:16]}...)" if service_id else "")
                        + ". The service stays down until its key is restored; "
                        "NEW:BEST would change the address.")

        services.append({
            "service_id": service_id if key_b64 else None,
            "service_name": name,
            "key_b64": key_b64,
            "refuse_new": refuse_new,
            "ports": ports,
            "service_dir": service_dir,
        })

    return services


def _identity_traces(service_dir, pem_path):
    """What on disk says this service already has an address.

    Any of these means NEW:BEST would replace an existing identity: the
    delivered key, or C Tor's secret key, public key or hostname (even a
    damaged or dangling one).
    """
    traces = []
    if os.path.lexists(pem_path):
        traces.append("delivered key")
    for fname in ("hs_ed25519_secret_key", "hs_ed25519_public_key", "hostname"):
        if os.path.lexists(os.path.join(service_dir, fname)):
            traces.append(fname)
    return traces


def _key_from_delivered_pem(pem_path):
    """(key_b64, service_id) from the delivered PEM, or None if unusable."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("key_convert", KEY_CONVERT_PATH)
        key_convert = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(key_convert)
        expanded_key, public_key = key_convert.parse_arti_pem(pem_path)
        service_id = key_convert._onion_address(public_key)
    except Exception as e:  # any failure means "no usable delivered key"
        log(f"Delivered key {pem_path} is unusable too ({e})")
        return None
    return base64.b64encode(expanded_key).decode("ascii"), service_id


def _parse_generated_key(resp_lines):
    """(service_id, key_b64) from an ADD_ONION NEW:BEST reply, or Nones."""
    service_id = None
    key_b64 = None
    for line in resp_lines.splitlines():
        if line.startswith("250-ServiceID="):
            service_id = line.split("=", 1)[1]
        elif line.startswith("250-PrivateKey=ED25519-V3:"):
            key_b64 = line.split(":", 1)[1]
    return service_id, key_b64


def _write_new_file(path, data, mode):
    """Create path with data in one step; never replaces or follows anything
    there (FileExistsError). A temp file beside it (O_EXCL, O_NOFOLLOW) is
    synced, then hard-linked to path, which fails if path exists."""
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _save_generated_key(service_dir, service_id, key_b64):
    """Save a key minted by ADD_ONION NEW:BEST to disk.

    Only ever creates files: an existing secret key or hostname is an
    existing identity (issue #311), so it is left alone and the clash is
    logged. The secret key goes first, so a hostname never exists without it.
    """
    try:
        os.makedirs(service_dir, exist_ok=True)
        # Secret key first, in C Tor format (32-byte header + 64-byte key),
        # then the hostname, each in one step and never over an existing
        # file: a short key file left by a crash reads as no key, and an
        # existing one is an existing identity (issue #311).
        key_bytes = base64.b64decode(key_b64)
        header = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
        _write_new_file(os.path.join(service_dir, "hs_ed25519_secret_key"),
                        header + key_bytes, 0o600)
        _write_new_file(os.path.join(service_dir, "hostname"),
                        (service_id + ".onion\n").encode("ascii"), 0o644)
        log(f"Saved generated key for {service_id[:16]}... to {service_dir}")
        return True
    except FileExistsError as e:
        log(f"ERROR: not saving generated key for {service_id[:16]}...: "
            f"{e.filename} already exists and is not overwritten")
    except (OSError, ValueError) as e:
        log(f"Warning: failed to save generated key: {e}")
    return False


def _write_atomic(path, data):
    """Write path in one step, mode 0600: a new file beside it (O_EXCL,
    O_NOFOLLOW), synced, renamed over it, directory synced."""
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def add_all_services(cmd_sock, services):
    """ADD_ONION for all services. Returns (successes, collisions)."""
    count = 0
    collisions = 0
    for svc in services:
        port_args = " ".join(f"Port={p}" for p in svc["ports"])
        if svc.get("key_b64"):
            cmd = f"ADD_ONION ED25519-V3:{svc['key_b64']} Flags=Detach {port_args}"
        else:
            # NEW:BEST only for a service with no identity anywhere (issue
            # #311), checked again now: files may have appeared since
            # discovery, and a key that cannot be read is no licence.
            traces = _identity_traces(
                svc.get("service_dir", ""),
                os.path.join(KEYS_DIR, svc["service_name"], DELIVERED_KEY_NAME))
            if svc.get("refuse_new") or traces:
                if not svc.get("refusal_logged"):
                    log(f"ADD_ONION {svc['service_name']} — SKIPPED: no usable key, "
                        f"refusing to mint a replacement address")
                    svc["refusal_logged"] = True
                svc["refuse_new"] = True
                continue
            cmd = f"ADD_ONION NEW:BEST Flags=Detach {port_args}"
        resp = send_cmd(cmd_sock, cmd)
        if "250" in resp:
            if svc.get("key_b64"):
                if not svc.get("service_id"):
                    svc["service_id"] = _parse_generated_key(resp)[0]
                log(f"ADD_ONION {svc['service_name']} ({(svc['service_id'] or '?')[:16]}...) — ok")
            else:
                new_id, new_key = _parse_generated_key(resp)
                svc["service_id"] = new_id
                if new_id and new_key:
                    # Keep the key: the next DEL+ADD (sleep/wake, republish)
                    # must bring back this address, not mint another.
                    svc["key_b64"] = new_key
                    _save_generated_key(svc.get("service_dir", ""), new_id, new_key)
                log(f"ADD_ONION {svc['service_name']} (generated {new_id[:16] if new_id else '?'}...) — ok")
            count += 1
        elif "Onion address collision" in resp:
            log(f"ADD_ONION {svc['service_name']} — already active (collision)")
            collisions += 1
        else:
            log(f"ADD_ONION {svc['service_name']} — FAILED: {resp.strip()[:100]}")
    return count, collisions


def del_all_services(cmd_sock, services):
    """DEL_ONION for all services. Returns number of successes."""
    count = 0
    for svc in services:
        if not svc.get("service_id"):
            continue
        resp = send_cmd(cmd_sock, f"DEL_ONION {svc['service_id']}")
        if "250" in resp:
            log(f"DEL_ONION {svc['service_name']} ({svc['service_id'][:16]}...) — ok")
            count += 1
        else:
            log(f"DEL_ONION {svc['service_name']} — FAILED: {resp.strip()[:100]}")
    return count


# ---------------------------------------------------------------------------
# Signal handling (USR1=sleep, USR2=wake)
# ---------------------------------------------------------------------------
_signal_sleep = False
_signal_wake = False


def _handle_usr1(signum, frame):
    global _signal_sleep
    _signal_sleep = True
    # Immediate signal-receipt evidence — separate from the
    # "Received USR1 (sleep) — removing onion services" line in the
    # main loop, which only prints after the DEL_ONION work runs.
    # Without this, a system-sleep race where suspend lands before
    # the main loop wakes leaves no record that USR1 arrived at all.
    log("SIGUSR1 received (sleep)")


def _handle_usr2(signum, frame):
    global _signal_wake
    _signal_wake = True
    log("SIGUSR2 received (wake)")


# ---------------------------------------------------------------------------
# Watchdog state
# ---------------------------------------------------------------------------
class WatchdogState:
    def __init__(self):
        self.bootstrapped = False
        self.last_bootstrap_pct = 0
        self.last_bootstrap_change = time.time()
        self.last_dropguards = 0
        self.last_dormant = 0
        self.last_halt = 0
        self.failed_node_count = 0
        self.last_heartbeat_log = time.time()  # periodic "alive" log
        self.failed_node_window_start = time.time()
        self.last_recovery_time = 0  # when we last detected a wake
        self.last_recovery_trigger = ""  # e.g. "wake", "clock-skew", "circuits lost"
        self.hs_desc_uploaded_since_recovery = False
        self.hs_desc_upload_started_since_recovery = False
        self.hs_desc_upload_failed_since_recovery = False
        self.hs_desc_last_failed_reason = ""
        self.onion_addresses = []  # for HSFETCH
        self.services = []  # discovered onion services
        self.services_active = False  # True when ADD_ONION has been done
        self.sleeping = False  # True between USR1 (sleep) and USR2 (wake)
        # Host-requested republish bookkeeping
        self.last_forced_republish = 0   # when we last did DEL+ADD on request
        self.last_hs_desc_uploaded = 0   # wall-clock of the last HS_DESC UPLOADED
        self.republish_deferred_logged = False  # one "deferring" line per request


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _hsfetch_missing_descriptors(cmd_sock, state):
    """Check client descriptor cache and HSFETCH any missing addresses.

    Called right after bootstrap hits 100%. Queries the control port
    (read-only) for each address we care about, and only HSFETCHes
    the ones that aren't cached. Safe on cold start — no competing
    descriptors to poison the cache.
    """
    if not state.onion_addresses:
        state.onion_addresses = discover_onion_addresses()
    if not state.onion_addresses:
        return
    for addr in state.onion_addresses:
        resp = send_cmd(cmd_sock, f"GETINFO hs/client/desc/id/{addr}")
        if "hs-descriptor" in resp:
            continue  # already cached
        resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
        if "250" in resp:
            log(f"HSFETCH {addr[:16]}... — descriptor not cached, fetching")


def discover_onion_addresses():
    """Find onion addresses for HSFETCH — our own services + the content address."""
    addresses = set()
    for path in glob.glob(f"{HS_BASE_DIR}/*/hostname"):
        try:
            with open(path) as f:
                addr = f.read().strip()
        except OSError:
            continue
        if _ONION_ADDRESS_RE.fullmatch(addr):
            addresses.add(addr[:-len(".onion")])
    # Content address (shared volume) — for reachability checks. Read without
    # following a link, and only from a regular file.
    try:
        fd = os.open(SHARED_ONION_ADDRESS_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            regular = stat.S_ISREG(os.fstat(f.fileno()).st_mode)
            raw = f.read(128) if regular else b""
        addr = raw.decode("ascii", "replace").strip()
        if _ONION_ADDRESS_RE.fullmatch(addr):
            addresses.add(addr[:-len(".onion")])
    except OSError:
        pass
    return list(addresses)


def do_dropguards(cmd_sock, state, reason):
    """Send DROPGUARDS + NEWNYM with rate limiting."""
    now = time.time()
    if now - state.last_dropguards < DROPGUARDS_COOLDOWN:
        return

    log(f"Recovering: {reason}")

    resp = send_cmd(cmd_sock, "DROPGUARDS")
    if "250" in resp:
        log("Sent DROPGUARDS — fresh guard selection")
    else:
        log(f"DROPGUARDS failed: {resp.strip()}")

    resp = send_cmd(cmd_sock, "SIGNAL NEWNYM")
    if "250" in resp:
        log("Sent SIGNAL NEWNYM — new circuits")
    else:
        log(f"SIGNAL NEWNYM failed: {resp.strip()}")

    state.last_dropguards = now
    state.last_recovery_time = now
    state.last_recovery_trigger = reason
    state.hs_desc_uploaded_since_recovery = False
    state.hs_desc_upload_started_since_recovery = False
    state.hs_desc_upload_failed_since_recovery = False
    state.hs_desc_last_failed_reason = ""
    state.failed_node_count = 0


def do_dormant_cycle(cmd_sock, state, reason):
    """Mid-level recovery: DORMANT → ACTIVE forces clean re-bootstrap without restart."""
    now = time.time()
    if now - state.last_dormant < DORMANT_COOLDOWN:
        return

    log(f"Escalating: {reason}")

    resp = send_cmd(cmd_sock, "SIGNAL DORMANT")
    if "250" in resp:
        log("Sent SIGNAL DORMANT — Tor closing circuits and clearing state")
    else:
        log(f"SIGNAL DORMANT failed: {resp.strip()}")
        return

    time.sleep(3)

    resp = send_cmd(cmd_sock, "SIGNAL ACTIVE")
    if "250" in resp:
        log("Sent SIGNAL ACTIVE — Tor re-bootstrapping with fresh state")
    else:
        log(f"SIGNAL ACTIVE failed: {resp.strip()}")

    state.last_dormant = now
    state.bootstrapped = False


def do_halt(cmd_sock, state, reason):
    """Last resort: tell Tor to shut down. Docker restart policy brings it back."""
    now = time.time()
    if now - state.last_halt < HALT_COOLDOWN:
        return

    log(f"LAST RESORT: {reason} — sending SIGNAL HALT")
    send_cmd(cmd_sock, "SIGNAL HALT")
    state.last_halt = now


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------
def process_event(line, cmd_sock, state):
    """Process a single event line from the control port."""

    # --- Clock jump ---
    # Don't DROPGUARDS — Tor recovers naturally after clock skew.
    # USR1/USR2 signals handle sleep/wake.
    if "CLOCK_SKEW" in line or "clock just jumped" in line:
        log("Clock skew detected — letting Tor recover naturally")
        state.last_recovery_time = time.time()
        state.last_recovery_trigger = "clock-skew"
        state.hs_desc_uploaded_since_recovery = False
        state.hs_desc_upload_started_since_recovery = False
        state.hs_desc_upload_failed_since_recovery = False
        state.hs_desc_last_failed_reason = ""
        return

    # --- Failed to find node for hop #1 ---
    if "Failed to find node" in line:
        now = time.time()
        if now - state.failed_node_window_start > FAILED_NODE_WINDOW:
            state.failed_node_count = 0
            state.failed_node_window_start = now
        state.failed_node_count += 1

        if state.failed_node_count >= FAILED_NODE_THRESHOLD:
            do_dropguards(cmd_sock, state,
                          f"{state.failed_node_count} guard failures in {FAILED_NODE_WINDOW}s")
        return

    # --- Guard exhaustion ---
    if "No usable guards" in line or "All current guards excluded" in line:
        do_dropguards(cmd_sock, state, "guard exhaustion")
        return

    # --- Network recovery ---
    # Don't DROPGUARDS here — Tor recovers naturally and USR2 handles wake.
    # DROPGUARDS throws away guards right when ADD_ONION needs them to publish.
    if "Tor now sees network activity" in line:
        log("Network came back — letting Tor recover naturally")
        return

    # --- Bootstrap progress ---
    if "BOOTSTRAP" in line or "Bootstrapped" in line:
        pct = _extract_bootstrap_pct(line)
        if pct is not None:
            if pct != state.last_bootstrap_pct:
                state.last_bootstrap_pct = pct
                state.last_bootstrap_change = time.time()
            if pct >= 100:
                if not state.bootstrapped:
                    log("Tor bootstrapped to 100%")
                state.bootstrapped = True
                state.failed_node_count = 0
            else:
                state.bootstrapped = False
        return

    # --- Descriptor publication events (onion service containers) ---
    # Event format: 650 HS_DESC <ACTION> <HSAddress> <AuthType> <HsDir> ...
    # Match with trailing space so UPLOAD doesn't match UPLOADED.
    if "HS_DESC UPLOADED " in line:
        # Log the first UPLOADED after each recovery arming, with elapsed
        # time and the trigger that armed the monitor. This is the positive
        # robustness signal: it confirms Tor actually republished after our
        # ADD path armed the stall monitor, and the elapsed time lets us
        # spot regressions in publication latency per trigger type.
        if (state.last_recovery_time > 0
                and not state.hs_desc_uploaded_since_recovery):
            elapsed = time.time() - state.last_recovery_time
            log(f"HS_DESC UPLOADED (trigger={state.last_recovery_trigger} "
                f"elapsed={elapsed:.1f}s)")
        state.hs_desc_uploaded_since_recovery = True
        state.last_hs_desc_uploaded = time.time()
        return
    if "HS_DESC UPLOAD " in line:
        state.hs_desc_upload_started_since_recovery = True
        return
    if "HS_DESC FAILED " in line:
        state.hs_desc_upload_failed_since_recovery = True
        if "REASON=" in line:
            state.hs_desc_last_failed_reason = line.split("REASON=", 1)[1].split()[0]
        return


def _extract_bootstrap_pct(line):
    """Extract bootstrap percentage from a log or event line."""
    if "PROGRESS=" in line:
        for part in line.split():
            if part.startswith("PROGRESS="):
                try:
                    return int(part.split("=")[1])
                except ValueError:
                    pass
    if "Bootstrapped" in line:
        for part in line.split():
            if part.endswith("%"):
                try:
                    return int(part.rstrip("%"))
                except ValueError:
                    pass
    return None


def read_republish_request():
    """Return the reason text of a pending host republish request, or None."""
    try:
        with open(REPUBLISH_REQUEST_PATH) as f:
            return " ".join(f.read().split())[:200] or "unspecified"
    except OSError:
        return None


def clear_republish_request():
    """Remove a pending request file (idempotent)."""
    try:
        os.remove(REPUBLISH_REQUEST_PATH)
    except OSError:
        pass


def force_republish(cmd_sock, state, reason):
    """DEL_ONION + ADD_ONION every managed service and arm the stall monitor.

    Fresh intro points, immediate descriptor upload. Shared by the host
    republish request and (via the same DEL+ADD) the HS_DESC stall path.
    """
    now = time.time()
    log(f"Republish requested by host ({reason}) — DEL+ADD to force fresh descriptors")
    del_all_services(cmd_sock, state.services)
    added, collisions = add_all_services(cmd_sock, state.services)
    state.services_active = (added + collisions) > 0
    state.last_forced_republish = now
    # Arm the HS_DESC stall monitor so a republish that never uploads gets
    # the existing DEL+ADD retry after HS_DESC_UPLOAD_TIMEOUT.
    state.last_recovery_time = now
    state.last_recovery_trigger = "republish-request"
    state.hs_desc_uploaded_since_recovery = False
    state.hs_desc_upload_started_since_recovery = False
    state.hs_desc_upload_failed_since_recovery = False
    state.hs_desc_last_failed_reason = ""
    return added


def check_republish_request(cmd_sock, state):
    """Act on a host republish request if one is pending and it is safe to.

    Called from check_stalls() (every recv timeout, ~5s). The file is
    left in place while we can't act — not bootstrapped, inside the
    cooldown, or the current descriptor is younger than
    REPUBLISH_MIN_AGE_AFTER_UPLOAD — so the request is honoured as soon
    as the guard clears, and repeated requests from the host coalesce
    into one action. It is discarded when it can't apply at all (asleep,
    no services).
    """
    reason = read_republish_request()
    if reason is None:
        state.republish_deferred_logged = False
        return False

    if state.sleeping or not state.services:
        why = "asleep" if state.sleeping else "no services configured"
        log(f"Republish request ({reason}) ignored — {why}")
        clear_republish_request()
        state.republish_deferred_logged = False
        return False

    now = time.time()
    hold = None
    if not state.bootstrapped:
        hold = "Tor not bootstrapped"
    elif now - state.last_forced_republish < REPUBLISH_COOLDOWN:
        hold = (f"cooldown, last forced republish "
                f"{int(now - state.last_forced_republish)}s ago")
    elif (state.last_hs_desc_uploaded
          and now - state.last_hs_desc_uploaded < REPUBLISH_MIN_AGE_AFTER_UPLOAD):
        hold = (f"descriptor uploaded only "
                f"{int(now - state.last_hs_desc_uploaded)}s ago")
    if hold:
        if not state.republish_deferred_logged:
            log(f"Republish request ({reason}) deferred — {hold}")
            state.republish_deferred_logged = True
        return False

    clear_republish_request()
    state.republish_deferred_logged = False
    force_republish(cmd_sock, state, reason)
    return True


def check_stalls(cmd_sock, state):
    """Periodic check for stalls that events alone can't catch."""
    now = time.time()

    # Host-requested republish (issue #266). Checked first so a request
    # isn't shadowed by the heartbeat/stall logic below.
    check_republish_request(cmd_sock, state)

    # Periodic heartbeat log (every 5 minutes)
    if now - state.last_heartbeat_log > 300:
        ce = "?"
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=" in resp:
            ce = resp.split("circuit-established=")[1].split()[0].strip()
        log(f"alive — bootstrapped={state.bootstrapped}, "
            f"circuit-established={ce}, services_active={state.services_active}")
        state.last_heartbeat_log = now

    # Active circuit health check — if Tor reports no circuits, recover
    if state.bootstrapped:
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=0" in resp:
            do_dropguards(cmd_sock, state, "circuit-established=0 (circuits lost)")

    # Bootstrap stall
    if (not state.bootstrapped
            and state.last_bootstrap_pct > 0
            and now - state.last_bootstrap_change > BOOTSTRAP_STALL_TIMEOUT
            and now - state.last_dropguards > DROPGUARDS_COOLDOWN):
        do_dropguards(cmd_sock, state,
                      f"bootstrap stalled at {state.last_bootstrap_pct}% for {BOOTSTRAP_STALL_TIMEOUT}s")

    # Descriptor upload stall — decide between DEL+ADD vs leave-alone based
    # on which HS_DESC events fired since recovery. The structured log line
    # lets analytics count branch frequencies across users.
    if (state.last_recovery_time > 0
            and not state.hs_desc_uploaded_since_recovery
            and now - state.last_recovery_time > HS_DESC_UPLOAD_TIMEOUT
            and state.bootstrapped):
        elapsed = int(now - state.last_recovery_time)
        started = state.hs_desc_upload_started_since_recovery
        failed = state.hs_desc_upload_failed_since_recovery
        trigger = state.last_recovery_trigger or "unknown"
        failed_reason = state.hs_desc_last_failed_reason or ""

        fields = (f"trigger={trigger} elapsed={elapsed}s "
                  f"upload_started={'yes' if started else 'no'} "
                  f"uploaded=no "
                  f"failed={'yes' if failed else 'no'}")
        if failed and failed_reason:
            fields += f" failed_reason={failed_reason}"

        if started and not failed:
            log(f"HS_DESC stall after recovery ({fields}) — UPLOAD in flight, leaving alone")
        elif state.services:
            log(f"HS_DESC stall after recovery ({fields}) — DEL+ADD to force republish")
            del_all_services(cmd_sock, state.services)
            added, _ = add_all_services(cmd_sock, state.services)
            if added > 0:
                state.services_active = True
        else:
            # SOCKS-only container (no services). Fall back to HSFETCH to
            # refresh the client-side descriptor cache for known addresses.
            if not state.onion_addresses:
                state.onion_addresses = discover_onion_addresses()
            if state.onion_addresses:
                log(f"HS_DESC stall after recovery ({fields}) — no services configured; HSFETCH only")
                send_cmd(cmd_sock, "SIGNAL NEWNYM")
                for addr in state.onion_addresses:
                    resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
                    if "250" in resp:
                        log(f"HSFETCH {addr[:16]}... — refreshing descriptor")
            else:
                log(f"HS_DESC stall after recovery ({fields}) — no services or addresses known; no action")
        state.last_recovery_time = 0

    # Escalation: DORMANT/ACTIVE if DROPGUARDS didn't work after 2 minutes.
    # Only safe for SOCKS-only containers — DORMANT kills onion services permanently.
    if (os.environ.get("NO_ONION_SERVICE") == "1"
            and state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > DORMANT_COOLDOWN
            and now - state.last_dormant > DORMANT_COOLDOWN):
        do_dormant_cycle(cmd_sock, state,
                         f"DROPGUARDS didn't recover after {DORMANT_COOLDOWN}s — trying DORMANT/ACTIVE")

    # Last resort: HALT
    if (state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > HALT_COOLDOWN
            and now - state.last_halt > HALT_COOLDOWN):
        do_halt(cmd_sock, state,
                f"still not bootstrapped {HALT_COOLDOWN}s after recovery attempts")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run():
    global _signal_sleep, _signal_wake

    log("Starting tor-watchdog")
    touch_alive()
    state = WatchdogState()
    # A request left over from a previous life of this container is moot
    # (the first ADD_ONION is a fresh publish); entrypoint.sh's supervisor
    # removes it once per container start. One that is here now arrived
    # while an earlier watchdog in this container had died: honour it.
    # Likewise a sleep marker means USR1 came and USR2 has not: the
    # services were removed on purpose and stay removed until wake.
    if sleep_marker_present():
        state.sleeping = True
        log("Restarted while the host sleeps — onion services stay removed until USR2")

    # Discover onion services from disk (keys + torrc port mappings)
    state.services = discover_services()
    if state.services:
        log(f"Discovered {len(state.services)} onion service(s): "
            + ", ".join(s["service_name"] for s in state.services))
    else:
        log("No onion services found on disk (SOCKS-only container?)")

    while True:
        # Connect event socket
        log("Connecting to control port...")
        event_sock = None
        while event_sock is None:
            event_sock = connect_and_auth()
            if event_sock is None:
                touch_alive()
                time.sleep(CONNECT_RETRY_DELAY)

        # Connect command socket
        cmd_sock = None
        while cmd_sock is None:
            cmd_sock = connect_and_auth()
            if cmd_sock is None:
                touch_alive()
                time.sleep(CONNECT_RETRY_DELAY)

        # Subscribe to events
        resp = send_cmd(event_sock, "SETEVENTS STATUS_CLIENT STATUS_GENERAL NOTICE WARN HS_DESC")
        if "250" not in resp:
            log(f"Failed to subscribe to events: {resp.strip()}")
            event_sock.close()
            cmd_sock.close()
            time.sleep(CONNECT_RETRY_DELAY)
            continue

        # Check current bootstrap status
        resp = send_cmd(cmd_sock, "GETINFO status/bootstrap-phase")
        if "PROGRESS=100" in resp:
            state.bootstrapped = True
            log("Tor already bootstrapped to 100%")
        else:
            pct = _extract_bootstrap_pct(resp)
            if pct is not None:
                state.last_bootstrap_pct = pct
                log(f"Tor bootstrap at {pct}%")

        # ADD_ONION for all services — do it before bootstrap so Tor publishes
        # descriptors as soon as it has circuits (no delay after bootstrap).
        if state.services and not state.services_active and not state.sleeping:
            n, _c = add_all_services(cmd_sock, state.services)
            state.services_active = (n + _c) > 0
            if state.services_active:
                # Arm the HS_DESC stall monitor for the post-ADD publication
                # window. Without this, the stall handler doesn't engage until
                # the first USR2 fires. The menubar app used to send a
                # spurious USR2 here just to set last_recovery_time, which
                # re-entered the wake handler and collided with the ADD we
                # just did — forcing a wasteful DEL+ADD on every cold start.
                state.last_recovery_time = time.time()
                state.last_recovery_trigger = "startup"
                state.hs_desc_uploaded_since_recovery = False
                state.hs_desc_upload_started_since_recovery = False
                state.hs_desc_upload_failed_since_recovery = False
                state.hs_desc_last_failed_reason = ""

        log("Connected — monitoring Tor health")
        event_sock.settimeout(5)  # wake up frequently to check signals + stalls
        buf = ""

        while True:
            touch_alive()

            # Check for USR1 (sleep) signal
            if _signal_sleep:
                _signal_sleep = False
                log("Received USR1 (sleep) — removing onion services")
                state.sleeping = True
                set_sleep_marker(True)
                if state.services and state.services_active:
                    del_all_services(cmd_sock, state.services)
                    state.services_active = False

            # Check for USR2 (wake) signal
            if _signal_wake:
                _signal_wake = False
                state.sleeping = False
                set_sleep_marker(False)
                state.last_recovery_time = time.time()
                state.last_recovery_trigger = "wake"
                state.hs_desc_uploaded_since_recovery = False
                state.hs_desc_upload_started_since_recovery = False
                state.hs_desc_upload_failed_since_recovery = False
                state.hs_desc_last_failed_reason = ""

                # Wait for Tor to have live circuits before ADD_ONION.
                # After sleep, Tor may report bootstrapped=True (stale) but
                # circuit-established=0 if the network isn't up yet.  ADD_ONION
                # without circuits silently fails to upload descriptors.
                log("Received USR2 (wake) — waiting for circuits before ADD_ONION...")
                waited = 0
                while waited < 120:
                    resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
                    if "circuit-established=1" in resp:
                        log(f"Circuits established after {waited}s — proceeding with ADD_ONION")
                        break
                    touch_alive()
                    time.sleep(5)
                    waited += 5
                else:
                    log(f"WARNING: No circuits after {waited}s — attempting ADD_ONION anyway")

                if state.services:
                    n, collisions = add_all_services(cmd_sock, state.services)
                    if collisions > 0:
                        # Services were re-added during sleep (race condition).
                        # DEL then ADD to force fresh descriptor publication.
                        log(f"Collision on wake — DEL+ADD to force fresh descriptors")
                        del_all_services(cmd_sock, state.services)
                        n, collisions = add_all_services(cmd_sock, state.services)
                    state.services_active = (n + collisions) > 0

            # Read events
            try:
                data = event_sock.recv(4096)
                if not data:
                    log("Control port connection closed — reconnecting")
                    state.services_active = False
                    break
                buf += data.decode("utf-8", errors="replace")
            except socket.timeout:
                # No events — check for stalls
                check_stalls(cmd_sock, state)

                # If services not yet added (e.g. after reconnect), add them now
                # But NOT while sleeping — DEL_ONION was intentional
                # And only once circuits are established — ADD_ONION without
                # circuits silently fails to upload descriptors.
                if state.services and not state.services_active and not state.sleeping:
                    resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
                    if "circuit-established=1" in resp:
                        n, _c = add_all_services(cmd_sock, state.services)
                        state.services_active = (n + _c) > 0
                        if state.services_active:
                            # Arm HS_DESC stall monitor for the post-reconnect
                            # publication window — same reason as the startup
                            # ADD path above.
                            state.last_recovery_time = time.time()
                            state.last_recovery_trigger = "reconnect"
                            state.hs_desc_uploaded_since_recovery = False
                            state.hs_desc_upload_started_since_recovery = False
                            state.hs_desc_upload_failed_since_recovery = False
                            state.hs_desc_last_failed_reason = ""

                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                log("Control port connection lost — reconnecting")
                state.services_active = False
                break

            # Process complete lines
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                if not line:
                    continue
                if line.startswith("650"):
                    process_event(line, cmd_sock, state)

            # Also check stalls on every recv cycle
            check_stalls(cmd_sock, state)

        # Clean up and reconnect
        try:
            event_sock.close()
        except OSError:
            pass
        try:
            cmd_sock.close()
        except OSError:
            pass
        time.sleep(CONNECT_RETRY_DELAY)


if __name__ == "__main__":
    # Only run for C Tor

    # Install signal handlers
    signal.signal(signal.SIGUSR1, _handle_usr1)
    signal.signal(signal.SIGUSR2, _handle_usr2)

    try:
        run()
    except KeyboardInterrupt:
        log("Shutting down")
    except Exception as e:
        # entrypoint.sh's supervisor restarts us; the traceback is what
        # tells the next reader why it had to.
        log(f"Fatal error: {e!r}")
        for line in traceback.format_exc().rstrip().splitlines():
            log(f"  {line}")
        sys.exit(1)
