#!/usr/bin/env python3
"""
OnionPress Heartbeat Client (Linux)

Registers this OnionPress instance with an OnionHeaven hub and sends
periodic heartbeats. Runs as a systemd service.

Reads config from ~/.onionpress/config (again every cycle):
  REGISTER_WITH_ONIONHEAVEN=yes|no  (default: unset = not enabled; opt-in)
    yes   - register and heartbeat (every /online carries the onion key)
    no    - send nothing; if this install registered before, send one
            signed /unregister so the hub deletes its copy of the key
    unset - send nothing at all (the tray and the CLI ask the user)
  Whatever the choice, a hub this site no longer uses (ONIONHEAVEN_ADDRESS
  changed) and the hubs of a key replaced while the daemon runs (a key
  import or restore) are asked to delete their copy of the key; the
  launcher's reset, import-key, restore and uninstall do the same through
  onionpress.cli onionheaven-unregister after stopping this daemon.
  ONIONHEAVEN_ADDRESS=<hub .onion>  (default: centralized OH)
  SHARE_ANALYTICS_WITH_ONIONHOME=yes  (opt-in; anything else = no upload)

Also hosts the opt-in analytics upload loop (onionpress.analytics_sharing)
so a Linux install ships its logs to OnionHome on the same daily cadence
as the Mac menubar app. The daemon is the natural home for it: it already
runs continuously under systemd and re-reads the config every cycle.
Before this, nothing on Linux ever ran the upload cycle (issue #207).

It also runs the onion service recovery loop (onionpress.reachability):
the dual probe and the RepublishTrigger that ask tor-watchdog for a
forced descriptor republish when the site has been unreachable through
the Tor network with Tor itself healthy (issue #266). On Linux that loop
used to live in onionpress-service.py, which nothing installed or ran
(issue #310).

Imports onion_auth and key_manager from /opt/onionpress/scripts/ and the
onionpress package from /opt/onionpress/lib/ (both laid down by
install.sh and the .deb).
"""

import base64
import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# D-Bus + GLib are needed for the systemd-logind delay-inhibitor pattern
# (the proper Linux equivalent of macOS's NSWorkspaceWillSleepNotification).
# Both are usually pre-installed on Debian/Ubuntu desktop images; if missing
# the daemon still runs, just without synchronous /offline-on-sleep.
try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False

# PySocks lets us POST /offline directly from the host through Tor's
# SOCKS port, skipping `docker exec → docker daemon → container exec
# → curl` startup. That savings (~200ms) is the difference between
# winning and losing the race against NetworkManager's WiFi teardown
# on suspend. If missing, the daemon falls back to the docker-exec
# path.
try:
    import socks
    HAVE_PYSOCKS = True
except ImportError:
    HAVE_PYSOCKS = False

# Add scripts directory to path for onion_auth and key_manager imports
SCRIPTS_DIR = "/opt/onionpress/scripts"
sys.path.insert(0, SCRIPTS_DIR)
# The shared package (src/onionpress/) is installed next to it under lib/;
# the launcher's op_py reaches it via PYTHONPATH, we add it here. Only the
# analytics upload needs it, and that import is deferred and guarded so an
# install without lib/ still heartbeats.
LIB_DIR = os.path.join(os.path.dirname(SCRIPTS_DIR), "lib")
if LIB_DIR not in sys.path:
    sys.path.append(LIB_DIR)

import key_manager
import onion_auth

# Defaults
DEFAULT_HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
API_PORT = 8083
HEARTBEAT_INTERVAL = 60
READY_TIMEOUT = 300  # 5 min

# State
DATA_DIR = os.path.expanduser("~/.onionpress")
CONFIG_PATH = os.path.join(DATA_DIR, "config")
STATE_PATH = os.path.join(DATA_DIR, "onionheaven-registration.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "onionpress.log")),
    ],
)
log = logging.getLogger("onionpress-heartbeat")

# Global for graceful shutdown
_current_hub = None
_content_addr = None
_hc_addr = None
_priv_key = None
_pub_key = None
_running = True
_registered = False  # Set True after first successful registration; read by SleepInhibitor.
_hub_conn = None     # Persistent HubConnection (set up after first /online).
_analytics_host = None  # AnalyticsHost once the upload loop has been started.
_reachability = None    # ReachabilityLoop once the recovery loop has been started.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_bad_hub_value_logged = None


def _hub_address(config):
    """The hub to talk to: ONIONHEAVEN_ADDRESS from *config*, or DEFAULT_HUB
    when it is unset or not a whole, well-formed onion address.

    As onionheaven.configured_hub_address on the Mac: /online carries the
    onion private key and the address goes into its URL, so "ends in
    .onion" is not enough ("evil.example/" + 39 characters + ".onion"
    passes that). A malformed value is logged by key name, once per value,
    and the default hub is used, the one the opt-in names.
    """
    global _bad_hub_value_logged
    value = (config.get("ONIONHEAVEN_ADDRESS") or "").strip()
    if not value:
        return DEFAULT_HUB
    if key_manager.is_valid_onion_address(value):
        return value
    if value != _bad_hub_value_logged:
        _bad_hub_value_logged = value
        log.warning("ONIONHEAVEN_ADDRESS in the config is not a valid onion "
                    "address; using the default hub")
    return DEFAULT_HUB


def read_config():
    """Parse KEY=VALUE config file. Returns dict."""
    config = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return config


def _registration_choice(config):
    """REGISTER_WITH_ONIONHEAVEN from *config*: "yes", "no" or None
    (undecided). Exactly what `onionpress onionheaven enable|disable`, the
    tray and the Settings page write, case aside; anything else is
    undecided, so a typo never registers and never unregisters."""
    value = (config.get("REGISTER_WITH_ONIONHEAVEN") or "").strip().lower()
    if value in ("yes", "no"):
        return value
    return None


# ---------------------------------------------------------------------------
# Docker exec helper
# ---------------------------------------------------------------------------

def docker_exec(container, args, timeout=30, input=None):
    """Run a command inside a Docker container. Returns (success, stdout).
    *input*, if given, is written to the command's stdin (docker exec -i)."""
    cmd = ["docker", "exec"] + (["-i"] if input is not None else []) + [container] + args
    try:
        result = subprocess.run(
            cmd, input=input, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        if result.returncode != 0 and result.stderr.strip():
            log.debug("docker exec %s failed: %s", container, result.stderr.strip()[:200])
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("docker exec %s timed out after %ds", container, timeout)
        return False, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Wait for readiness
# ---------------------------------------------------------------------------

def wait_for_ready():
    """Wait for content address, healthcheck address, and keys.
    Returns (content_addr, hc_addr, priv_key, pub_key) or None on timeout.
    """
    deadline = time.time() + READY_TIMEOUT
    backoff = 5

    while time.time() < deadline and _running:
        # Content address
        ok, content = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        if not ok or not key_manager.is_valid_onion_address(content):
            log.info("Waiting for content address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Healthcheck address
        ok, hc = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
            timeout=10,
        )
        if not ok or not key_manager.is_valid_onion_address(hc):
            log.info("Waiting for healthcheck address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Keys
        try:
            priv_key, pub_key = key_manager.extract_keys()
        except Exception as e:
            log.info("Waiting for keys: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        return content.strip(), hc.strip(), priv_key, pub_key

    return None


# ---------------------------------------------------------------------------
# Signing + POST via tor-client
# ---------------------------------------------------------------------------

def _detect_host_socks_port():
    """Find the host port that maps to onionpress-tor's 9050. The launcher
    publishes Tor's SOCKS at 9050 + ONIONPRESS_PORT_OFFSET — we ask docker
    rather than guessing so we work under both the default (9050) and
    offset-by-10000 case (system tor squatting on 9050)."""
    try:
        result = subprocess.run(
            ["docker", "port", "onionpress-tor", "9050/tcp"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if ":" in line:
                    _, port = line.rsplit(":", 1)
                    return int(port.strip())
    except Exception as e:
        log.debug("Could not detect host SOCKS port: %s", e)
    return 9050


class HubConnection:
    """Persistent SOCKS5+HTTP/1.1 socket to the OnionHeaven hub.

    A single socket reused across every /online + /offline POST.
    Heartbeat traffic (~once per minute) doubles as the keep-alive
    that holds the socket open — no separate keepalive logic. On
    broken pipe / closed connection we reconnect once and retry,
    so transient Tor circuit churn or hub restarts are transparent.

    Why it exists: the suspend /offline POST must land within ~30ms
    of PrepareForSleep, before NetworkManager finishes tearing down
    WiFi. A fresh SOCKS5+CONNECT+Tor-rendezvous takes 1-2 seconds —
    can't fit. A pre-established socket lets the inhibitor do nothing
    more than sock.sendall(POST_bytes) at suspend time, which is
    single-digit milliseconds.

    Thread-safe: a single lock serialises all socket access. The
    heartbeat main loop and the SleepInhibitor DBus thread both call
    .post().
    """

    def __init__(self, hub_addr, socks_port=None):
        self._hub_addr = hub_addr
        self._socks_port = socks_port
        self._sock = None
        self._lock = threading.Lock()

    def _open_locked(self):
        """Caller holds self._lock."""
        if self._socks_port is None:
            self._socks_port = _detect_host_socks_port()
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, "127.0.0.1", self._socks_port, rdns=True)
        s.settimeout(15)  # generous for cold Tor rendezvous setup
        s.connect((self._hub_addr, API_PORT))
        # OS-level keepalive on the underlying TCP: detects dead peers
        # if the hub vanishes without sending FIN.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        self._sock = s

    def _close_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def hub_addr(self):
        return self._hub_addr

    def is_open(self):
        with self._lock:
            return self._sock is not None

    def connect(self):
        """Open the persistent socket eagerly so it's ready before the first
        sleep can race us. Idempotent. Returns True on success."""
        with self._lock:
            if self._sock is not None:
                return True
            try:
                self._open_locked()
                return True
            except Exception as e:
                log.warning("HubConnection.connect failed: %s", e)
                self._close_locked()
                return False

    def close(self):
        with self._lock:
            self._close_locked()

    def _send_request(self, endpoint, body):
        """Caller holds self._lock and self._sock is not None."""
        request = (
            f"POST /{endpoint} HTTP/1.1\r\n"
            f"Host: {self._hub_addr}:{API_PORT}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: keep-alive\r\n"
            f"\r\n"
        ).encode() + body
        self._sock.sendall(request)

    def _read_response(self):
        """Caller holds self._lock and self._sock is not None.
        Returns (status_code, body_dict_or_None, should_close).

        `should_close` is True when the hub indicates the connection
        won't be kept open (HTTP/1.0 reply, or `Connection: close`).
        Caller must close the socket in that case; we drained the
        body so the FIN/EOF arrives cleanly."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionResetError("hub closed before headers complete")
            buf += chunk
            if len(buf) > 65536:
                raise OSError("response headers oversized")
        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line, _, headers_blob = head.partition(b"\r\n")
        parts = status_line.split(b" ", 2)
        if len(parts) < 3 or not parts[0].startswith(b"HTTP/"):
            raise OSError(f"bad status line: {status_line!r}")
        status = int(parts[1])
        http_version = parts[0]  # e.g. b"HTTP/1.0" or b"HTTP/1.1"

        content_length = None
        connection_close = False
        for line in headers_blob.split(b"\r\n"):
            if b":" not in line:
                continue
            k, _, v = line.partition(b":")
            name = k.strip().lower()
            val = v.strip()
            if name == b"content-length":
                try:
                    content_length = int(val)
                except ValueError:
                    pass
            elif name == b"connection":
                if val.lower() == b"close":
                    connection_close = True

        # HTTP/1.0 defaults to close-after-response unless explicit
        # keep-alive is set. The old hub uses 1.0 with no Content-Length
        # — we have to read until the hub closes the socket and then
        # not reuse it for further requests.
        if http_version == b"HTTP/1.0" or connection_close:
            should_close = True
        else:
            should_close = False

        body = rest
        if content_length is not None:
            while len(body) < content_length:
                chunk = self._sock.recv(content_length - len(body))
                if not chunk:
                    raise ConnectionResetError("hub closed during body read")
                body += chunk
        elif should_close:
            # No Content-Length, connection will close — read until EOF.
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                body += chunk

        try:
            parsed = json.loads(body) if body else None
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return status, parsed, should_close

    def post(self, endpoint, payload, timeout=15):
        """Send POST, read response, return (ok, body_dict).
        Reconnects once on broken socket; second failure returns (False, None).

        If the hub indicates the connection won't be kept open
        (HTTP/1.0 reply, or `Connection: close` header), we close
        the socket cleanly so the next call opens a fresh one. Lets
        the daemon talk to an old hub that hasn't been updated to
        HTTP/1.1 yet — works, just slower."""
        body = json.dumps(payload).encode("utf-8")
        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._open_locked()
                    self._sock.settimeout(timeout)
                    self._send_request(endpoint, body)
                    status, resp, should_close = self._read_response()
                    if should_close:
                        self._close_locked()
                    return status == 200, resp
                except (BrokenPipeError, ConnectionResetError, OSError,
                        socket.timeout, Exception) as e:
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    log.debug("HubConnection.post(%s): %s: %s — reconnecting",
                              endpoint, type(e).__name__, e)
                    self._close_locked()
            return False, None

    def post_fast_no_response(self, endpoint, payload):
        """Send POST, do NOT wait for the response. For the suspend race:
        we need the bytes on the wire before NetworkManager finishes
        WiFi teardown, and waiting for the hub's response over a
        going-down WiFi just burns the budget. Returns True if sendall
        completed locally; the hub still processes the POST.

        Closes the socket after the send because the response body is
        now sitting in the receive buffer unread — leaving it would
        corrupt the next request/response framing. Next /online from
        the main loop reconnects."""
        body = json.dumps(payload).encode("utf-8")
        with self._lock:
            if self._sock is None:
                # Not pre-established — abandoning this path; caller
                # should fall back to the slow path.
                return False
            try:
                self._sock.settimeout(0.5)
                self._send_request(endpoint, body)
                return True
            except (BrokenPipeError, ConnectionResetError, OSError,
                    socket.timeout) as e:
                log.warning("HubConnection.post_fast_no_response: %s: %s",
                            type(e).__name__, e)
                return False
            finally:
                # Always close — either we sent and the response is
                # unread, or we failed and the socket may be half-dead.
                self._close_locked()


# Endpoint label the hub's web-server.py requires on the signature backing an
# is_onionheaven claim (PEER_CLAIM_ENDPOINT there; a test keeps them equal).
PEER_CLAIM_ENDPOINT = "onionheaven-peer"


def _build_signed_payload(endpoint, content_addr, hc_addr, priv_key, pub_key, extra=None):
    timestamp = onion_auth.make_timestamp()
    signature = onion_auth.sign_payload(
        priv_key, pub_key,
        endpoint, content_addr, hc_addr, timestamp
    )
    payload = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "timestamp": timestamp,
        "signature": signature,
    }
    if extra:
        payload.update(extra)
    # The hub ignores a bare is_onionheaven (it sits outside the signed
    # string); the claim counts only with its own signature.
    if payload.get("is_onionheaven"):
        payload["onionheaven_peer_signature"] = onion_auth.sign_payload(
            priv_key, pub_key,
            PEER_CLAIM_ENDPOINT, content_addr, hc_addr, timestamp
        )
    return payload


def sign_and_post(endpoint, hub_addr, content_addr, hc_addr, priv_key, pub_key,
                  extra=None, max_time=60, docker_timeout=75):
    """Sign + POST to the hub. Returns (success, response_dict_or_None).

    Routes through the persistent HubConnection when one is available
    (set up after first successful registration). Falls back to
    docker exec curl on first-use, after broken-pipe retry exhaustion,
    or when PySocks isn't importable on this box.

    max_time / docker_timeout only apply to the docker-exec fallback —
    the persistent path uses its own (tighter) socket timeout.
    """
    if not key_manager.is_valid_onion_address(hub_addr):
        log.warning("POST /%s: hub address is not a valid onion address, not sent",
                    endpoint)
        return False, None
    payload = _build_signed_payload(endpoint, content_addr, hc_addr,
                                    priv_key, pub_key, extra)

    # The persistent socket goes to one hub; a request for another (an
    # /unregister to the hub this install registered with) takes the
    # docker-exec path.
    if _hub_conn is not None and _hub_conn.hub_addr == hub_addr:
        ok, resp = _hub_conn.post(endpoint, payload)
        if ok:
            return True, resp
        log.debug("POST /%s: persistent path failed, falling back to docker exec", endpoint)

    # Fallback: docker exec curl. Used at first-use (no _hub_conn yet)
    # and when the persistent connection refuses to recover. The payload
    # goes on stdin, never in argv: /online carries the onion private key,
    # and /proc/<pid>/cmdline is readable by every user.
    ok, output = docker_exec(
        "onionpress-tor",
        [
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "127.0.0.1:9050",
            "-H", "Content-Type: application/json",
            "--data-binary", "@-",
            "--max-time", str(max_time),
            f"http://{hub_addr}:{API_PORT}/{endpoint}",
        ],
        timeout=docker_timeout,
        input=json.dumps(payload),
    )

    if ok and output:
        try:
            return True, json.loads(output)
        except json.JSONDecodeError:
            log.warning("POST /%s: non-JSON response: %s", endpoint, output[:200])
            return False, None
    log.debug("POST /%s: ok=%s output=%r", endpoint, ok, output[:200] if output else "")
    return False, None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _is_onionheaven_server():
    """Check if the operator runs this node as an OnionHeaven hub too.

    Read from ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes, the switch that makes
    the tor container accept registrations — not from the activation
    marker, which is only a launcher hint. The claim earns this node the
    hub's 30-min peer grace before a takeover.
    """
    return read_config().get(
        "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "").strip().lower() == "yes"


def register(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Register with hub. Retries 4x with backoff. Returns True on success."""
    arti_pem = key_manager.build_openssh_key(priv_key, pub_key)
    extra = {
        "arti_key_pem": base64.b64encode(arti_pem).decode("ascii"),
        "version": _read_version(),
    }
    if _is_onionheaven_server():
        extra["is_onionheaven"] = True

    backoff_delays = [10, 30, 30]
    for attempt in range(4):
        # Every attempt carries the key: stop the moment the user turns
        # OnionHeaven off, not after the retries.
        if _registration_choice(read_config()) != "yes":
            log.info("Registration stopped: REGISTER_WITH_ONIONHEAVEN is no longer yes")
            return False
        if _address_on_hold(content_addr):
            log.info("Registration skipped: the key of %s is being given up "
                     "and its hubs were asked to delete it", content_addr)
            return False
        log.info("Registration attempt %d/4 with %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("online", hub_addr, content_addr, hc_addr, priv_key, pub_key, extra)

        if ok and resp:
            if resp.get("registered"):
                log.info("Registration successful: %s", resp)
                _record_registered(hub_addr, content_addr)
                return True
            error = resp.get("error", "unknown")
            log.error("Registration rejected: %s", error)
            return False

        if attempt < 3:
            delay = backoff_delays[attempt]
            log.info("Registration failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Registration failed after 4 attempts")
    # The record says whether the hub may hold the key (the opt-out's
    # /unregister keys off it), so a failed attempt must not clear one an
    # earlier session wrote.
    previous = _load_state()
    if previous.get("registered"):
        previous["last_attempt"] = datetime.now(timezone.utc).isoformat()
        _save_state(previous)
    else:
        _save_state({
            "registered": False,
            "last_attempt": datetime.now(timezone.utc).isoformat(),
            "onionheaven_address": hub_addr,
        })
    return False


# ---------------------------------------------------------------------------
# Unregister
# ---------------------------------------------------------------------------

# The hub's answer to an /unregister for an address it has no row for
# (web-server.py _handle_unregister): nothing of ours is left there.
UNREGISTER_NOT_FOUND = "Entry not found"

# How often an opted-out install retries an /unregister the hub did not
# answer. The hub takes an address over ~10 min after its last heartbeat
# while it still holds the key; the /unregister that lands releases it.
OPT_OUT_RETRY_SECONDS = 15 * 60


def unregister(hub_addr, content_addr, hc_addr, priv_key, pub_key, while_choice=None):
    """Unregister from hub. Retries 4x — critical to avoid false takeover.

    For the whole content address: the request carries no
    healthcheck_address (*hc_addr* is not sent), so the hub unregisters
    every row of the address, releases any takeover and deletes its copy
    of the key. With *while_choice*, an attempt goes out only while
    REGISTER_WITH_ONIONHEAVEN still has that value.

    Returns True when the hub holds nothing for the address any more
    (unregistered, or no entry), False when it may still, None when the
    choice changed before an attempt.
    """
    backoff_delays = [5, 15, 30]
    for attempt in range(4):
        if while_choice is not None:
            choice = _registration_choice(read_config())
            if choice != while_choice:
                log.info("Unregister dropped: REGISTER_WITH_ONIONHEAVEN is now %s",
                         choice or "unset")
                return None
        log.info("Unregister attempt %d/4 from %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("unregister", hub_addr, content_addr, "", priv_key, pub_key)

        if ok and resp:
            error = resp.get("error")
            if resp.get("unregistered") or error == UNREGISTER_NOT_FOUND:
                if resp.get("unregistered"):
                    log.info("Unregistered successfully from %s", hub_addr)
                else:
                    log.info("Hub %s holds no entry for %s; nothing to unregister",
                             hub_addr, content_addr)
                _record_unregistered(hub_addr, content_addr)
                return True
            log.error("Unregister rejected: %s", error or "unknown")
            if error:
                return False

        if attempt < 3:
            delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
            log.info("Unregister failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Unregister failed after 4 attempts (best-effort)")
    return False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Send a single /online heartbeat. Returns True on success."""
    if _address_on_hold(content_addr):
        log.info("Heartbeat skipped: the key of %s is being given up and its "
                 "hubs were asked to delete it", content_addr)
        return False
    # Check WordPress health
    wp_healthy = False
    ok, output = docker_exec(
        "onionpress-wordpress",
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "http://localhost:80/"],
        timeout=10,
    )
    if ok and output.strip() in ("200", "301", "302"):
        wp_healthy = True

    log.debug("WP health check: ok=%s output=%r healthy=%s", ok, output, wp_healthy)

    # Always include key — every /online is both heartbeat and registration
    arti_pem = key_manager.build_openssh_key(priv_key, pub_key)
    extra = {
        "wordpress_healthy": wp_healthy,
        "arti_key_pem": base64.b64encode(arti_pem).decode("ascii"),
    }
    if _is_onionheaven_server():
        extra["is_onionheaven"] = True

    ok, resp = sign_and_post(
        "online", hub_addr, content_addr, hc_addr, priv_key, pub_key,
        extra=extra,
    )

    if ok and resp and resp.get("online"):
        log.info("Heartbeat OK (wp_healthy=%s)", wp_healthy)
        return True
    if ok and resp:
        log.warning("Heartbeat rejected: %s", resp.get("error", "unknown"))
    else:
        log.warning("Heartbeat failed (will retry next cycle)")
    return False


# ---------------------------------------------------------------------------
# Offline notification
# ---------------------------------------------------------------------------

def send_offline(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Best-effort /offline notification."""
    try:
        ok, resp = sign_and_post("offline", hub_addr, content_addr, hc_addr, priv_key, pub_key)
        if ok and resp and resp.get("offline"):
            log.info("Sent /offline to %s", hub_addr)
        else:
            log.warning("Failed to send /offline to %s", hub_addr)
    except Exception as e:
        log.warning("Failed to send /offline: %s", e)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

# The registration record is shared with onionpress.onionheaven (the CLI's
# onionheaven-unregister, run by the launcher before a reset, key import,
# restore or uninstall); its comments describe the fields. Besides
# "registered" this daemon keeps "left_on_hubs" (registrations not taken
# back yet) and honours "released" (an address whose key is being given up:
# no /online for it until "until").
_STATE_CARRIED = ("left_on_hubs", "released")


def _save_state(data):
    """Write the record whole. left_on_hubs and released are carried over
    from the file unless *data* sets them (None drops one)."""
    data = dict(data)
    missing = [k for k in _STATE_CARRIED if k not in data]
    if missing:
        previous = _load_state()
        for key in missing:
            if key in previous:
                data[key] = previous[key]
    for key in _STATE_CARRIED:
        if data.get(key) is None:
            data.pop(key, None)
    tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log.warning("Failed to save state: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _load_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_registration(state):
    """(content_address, hub) the record says is registered, or None."""
    if not state.get("registered"):
        return None
    addr = state.get("content_address")
    hub = state.get("onionheaven_address")
    if not key_manager.is_valid_onion_address(addr):
        return None
    if not key_manager.is_valid_onion_address(hub):
        hub = _current_hub
    return (addr, hub) if key_manager.is_valid_onion_address(hub) else None


def _left_on_hubs(state):
    entries = state.get("left_on_hubs")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict)
            and key_manager.is_valid_onion_address(e.get("content_address"))
            and key_manager.is_valid_onion_address(e.get("onionheaven_address"))]


def _entry_pair(entry):
    return entry["content_address"], entry["onionheaven_address"]


def _note_left_on_hub(state, addr, hub, reason):
    entries = _left_on_hubs(state)
    if not any(_entry_pair(e) == (addr, hub) for e in entries):
        entries.append({"content_address": addr, "onionheaven_address": hub,
                        "since": datetime.now(timezone.utc).isoformat(),
                        "reason": reason})
    state["left_on_hubs"] = entries


def _forget_left_on_hub(state, addr, hub):
    entries = _left_on_hubs(state)
    kept = [e for e in entries if _entry_pair(e) != (addr, hub)]
    state["left_on_hubs"] = kept
    return len(kept) != len(entries)


def _record_registered(hub_addr, content_addr):
    """A hub took an /online: the record says it holds the key. A
    registration the record named before, for another hub or address,
    moves to left_on_hubs instead of being forgotten."""
    state = _load_state()
    previous = _state_registration(state)
    if previous is not None and previous != (content_addr, hub_addr):
        _note_left_on_hub(state, previous[0], previous[1], "superseded")
        if previous[0] != content_addr:
            log.warning("OnionHeaven: %s may still hold the key of %s, the "
                        "address this install had before; this install no "
                        "longer has that key", previous[1], previous[0])
    _forget_left_on_hub(state, content_addr, hub_addr)
    state.update({
        "registered": True,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
        "onionheaven_address": hub_addr,
        "content_address": content_addr,
    })
    state.pop("unregistered_at", None)
    _save_state(state)


def _record_unregistered(hub_addr, content_addr):
    """*hub_addr* answered an /unregister for *content_addr*: drop that
    pair. The registration is cleared only when it names that pair."""
    state = _load_state()
    if _state_registration(state) == (content_addr, hub_addr):
        state.update({
            "registered": False,
            "unregistered_at": datetime.now(timezone.utc).isoformat(),
            "onionheaven_address": hub_addr,
            "content_address": content_addr,
        })
    _forget_left_on_hub(state, content_addr, hub_addr)
    _save_state(state)


def _address_on_hold(content_addr, now=None):
    """True while the record's "released" hold covers *content_addr*: the
    launcher had its hubs delete that key, which is about to go."""
    released = _load_state().get("released")
    if not isinstance(released, dict) or released.get("content_address") != content_addr:
        return False
    until = released.get("until")
    now = time.time() if now is None else now
    return isinstance(until, (int, float)) and now < until


def _read_version():
    try:
        with open("/opt/onionpress/VERSION") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Analytics upload (opt-in) — onionpress.analytics_sharing on Linux
# ---------------------------------------------------------------------------

ANALYTICS_CONFIG_KEY = "SHARE_ANALYTICS_WITH_ONIONHOME"
_FIRST_LINE_DATE_RE = re.compile(rb"^\[(\d{4}-\d{2}-\d{2})")


def _advertised_log_name(log_type, path):
    """Name under which OnionHome stores *path*, stable for the file's life.

    The Linux logs are plain files rotated by suffix (``onionpress.log``
    → ``onionpress.log.1``), so the on-disk name is not a stable identity:
    after a rotation ``onionpress.log`` is a brand-new, smaller file, and
    OnionHome (which re-requests a name only when the offered size grows)
    would not ask for it again until it outgrew the old snapshot. Naming
    the file by its first line instead — ``<type>-<date>-<8 hex>.log``,
    date taken from a ``[YYYY-MM-DD`` prefix when there is one — gives a
    name that survives rotation (the first line never changes) and that
    the hub already holds a smaller snapshot of, so it keeps re-requesting
    the growing file. Returns None for a file with no complete first line
    yet (nothing worth shipping).
    """
    try:
        with open(path, "rb") as fh:
            first = fh.readline(4096)
    except OSError:
        return None
    if not first.endswith(b"\n"):
        return None
    tag = hashlib.sha256(first).hexdigest()[:8]
    m = _FIRST_LINE_DATE_RE.match(first)
    if m:
        return f"{log_type}-{m.group(1).decode('ascii')}-{tag}.log"
    return f"{log_type}-{tag}.log"


class _RotatedPlainLog:
    """Duck-typed stand-in for onionpress.log_rotation.RotatingLog.

    The Mac app hands analytics_sharing RotatingLog objects; the Linux
    launcher writes plain files and rotates them to ``.1``, ``.2``, ``.3``
    (``rotate_log`` in linux/onionpress, and the container capture loop).
    This exposes those the way ``_do_upload_cycle`` expects: the active
    file via ``current_path()`` and the rotated ones via
    ``completed_files()``, each under its stable advertised name.
    """

    def __init__(self, path, log_type):
        self._path = path
        self._log_type = log_type

    def current_path(self):
        return self._path

    def current_name(self):
        """Advertised name of the active file (see _advertised_log_name);
        the plain basename only while the file has no complete line."""
        return (_advertised_log_name(self._log_type, self._path)
                or os.path.basename(self._path))

    def completed_files(self):
        result = []
        for n in range(1, 10):
            p = f"{self._path}.{n}"
            if not os.path.isfile(p):
                break
            name = _advertised_log_name(self._log_type, p)
            if name is None:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > 0:
                result.append({"name": name, "size": size, "path": p})
        return result


def _linux_has_default_route():
    """True if the kernel has a default route (v4 or v6); True on any
    read error so a quirky /proc never blocks the upload — the manifest
    POST reports its own failure and the loop backs off from that."""
    try:
        with open("/proc/net/route") as fh:
            next(fh, None)  # header
            for line in fh:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    return True
    except OSError:
        return True
    try:
        with open("/proc/net/ipv6_route") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 1 and parts[0] == "0" * 32 and parts[1] == "00":
                    return True
    except OSError:
        return True
    return False


class AnalyticsHost:
    """The ``app`` object onionpress.analytics_sharing drives on Linux.

    Mirrors the attributes the Mac menubar app exposes to the sharing
    loop: config access, logging, onion addresses, version, the log files
    to offer, the latest status snapshot, and how to reach docker.
    """

    _sleeping = False  # the SleepInhibitor flushes /offline; uploads just retry

    def __init__(self, content_addr, hc_addr, data_dir=None):
        self.app_support = data_dir or DATA_DIR
        self.onion_address = content_addr
        self.healthcheck_address = hc_addr
        self.version = _read_version()
        logs_dir = os.path.join(self.app_support, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        # Same attribute names analytics_sharing looks for on the Mac app.
        # The active files are also found by _do_upload_cycle's own
        # container-*.log scan; it dedupes by path, so they ship once,
        # under the name given here.
        self._onionpress_log = _RotatedPlainLog(
            os.path.join(self.app_support, "onionpress.log"), "onionpress")
        self._tor_log = _RotatedPlainLog(
            os.path.join(logs_dir, "container-tor.log"), "container-tor")
        self._onionheaven_log = _RotatedPlainLog(
            os.path.join(logs_dir, "container-onionheaven.log"),
            "container-onionheaven")

    # -- what analytics_sharing calls --------------------------------------

    def read_config_value(self, key, default=""):
        return read_config().get(key, default)

    def log(self, msg):
        log.info("%s", msg)

    def check_internet_connectivity(self):
        return _linux_has_default_route()

    def docker_command(self):
        """``docker`` from PATH with the daemon's own environment — the
        rootless socket is found the same way every other docker call in
        this file finds it."""
        return "docker", None

    @property
    def _last_status_payload(self):
        """The launcher's status.json (machine state, no secrets), offered
        as the dated status snapshot the Mac app also ships."""
        try:
            with open(os.path.join(self.app_support, "status.json")) as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


def _import_analytics_sharing():
    try:
        from onionpress import analytics_sharing
    except ImportError as e:
        log.warning("Analytics sharing: unavailable on this install "
                    "(cannot import onionpress.analytics_sharing from %s: %s)",
                    LIB_DIR, e)
        return None
    return analytics_sharing


def _start_analytics_sharing(content_addr, hc_addr):
    """Start the daily upload loop (a daemon thread). Idempotent.

    The loop itself re-reads SHARE_ANALYTICS_WITH_ONIONHOME every time it
    wakes and uploads only on an explicit ``yes``, exactly like the Mac.
    """
    global _analytics_host
    if _analytics_host is not None:
        return _analytics_host
    analytics_sharing = _import_analytics_sharing()
    if analytics_sharing is None:
        return None
    _analytics_host = AnalyticsHost(content_addr, hc_addr)
    analytics_sharing.start_analytics_sharing(_analytics_host)
    enabled = _analytics_host.read_config_value(ANALYTICS_CONFIG_KEY, "no").lower()
    log.info("Analytics sharing: loop started (%s=%s)",
             ANALYTICS_CONFIG_KEY, enabled or "unset")
    return _analytics_host


def _start_reachability_loop():
    """Start the onion service recovery loop (a daemon thread). Idempotent.

    Runs on every node, the hub included, whatever the OnionHeaven opt-in
    says: it probes our own address and talks only to our own containers.
    Logs and returns None when the onionpress package is not importable,
    so an install without lib/ still heartbeats.
    """
    global _reachability
    if _reachability is not None:
        return _reachability
    try:
        from onionpress.docker import Docker
        from onionpress.platform import resolve_paths
        from onionpress.reachability import ReachabilityLoop, STATUS_FILENAME
    except ImportError as e:
        log.warning("Recovery loop: unavailable on this install "
                    "(cannot import onionpress.reachability from %s: %s)",
                    LIB_DIR, e)
        return None

    def _log(msg):
        log.info("%s", msg)

    docker = Docker(resolve_paths(data_dir=DATA_DIR, app_bundle=""), log_func=_log)
    _reachability = ReachabilityLoop(
        docker, _log,
        read_config_value=lambda key, default: read_config().get(key, default),
        status_path=os.path.join(DATA_DIR, STATUS_FILENAME),
        has_internet=_linux_has_default_route,
    )
    _reachability.start(threading.Event())
    log.info("Recovery loop: started (dual probe every %ds)",
             _reachability.interval)
    return _reachability


def _poll_analytics_opt_in(previous, config):
    """Per-cycle check of the opt-in flag. Returns its current value.

    When it flips to ``yes`` the loop is woken so the first upload happens
    now rather than at the next daily window — the Mac does the same when
    the setting is saved. A flip away from ``yes`` needs nothing: the loop
    checks the flag before every upload.
    """
    current = config.get(ANALYTICS_CONFIG_KEY, "no").strip().lower()
    if current == "yes" and previous != "yes" and _analytics_host is not None:
        analytics_sharing = _import_analytics_sharing()
        if analytics_sharing is not None:
            log.info("Analytics sharing: enabled in config, uploading now")
            analytics_sharing.trigger_upload()
    return current


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _signal_handler(signum, frame):
    global _running
    log.info("Received signal %d, shutting down...", signum)
    _running = False


# Pre-sleep (USR1) and post-wake (USR2) async kicks. The system-sleep hook
# (/usr/lib/systemd/system-sleep/onionpress) sends USR1 to flush /offline
# before suspend and USR2 after resume to send an immediate /online instead
# of waiting up to HEARTBEAT_INTERVAL. Signal handlers must not do network
# I/O directly (signal-safety), so they just flip flags and the main loop
# acts on them — time.sleep() is interrupted by the signal on Linux, so
# the loop wakes up immediately.
_pending_offline = False
_pending_online = False


def _usr1_handler(signum, frame):
    global _pending_offline
    log.info("Received SIGUSR1 — will flush /offline this cycle")
    _pending_offline = True


def _usr2_handler(signum, frame):
    global _pending_online
    log.info("Received SIGUSR2 — will send /online immediately")
    _pending_online = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class SleepInhibitor:
    """Hold a systemd-logind sleep delay inhibitor and use the
    PrepareForSleep signal to do a synchronous /offline POST before
    the kernel actually suspends.

    The Linux equivalent of macOS's NSWorkspaceWillSleepNotification:
    PrepareForSleep(true) is broadcast by logind before the kernel
    freezes processes, and the delay-type inhibitor keeps the suspend
    operation from completing until we release it (up to
    InhibitDelayMaxSec, default 5s, often configured higher).

    NetworkManager handles the same signal independently and starts
    tearing down the WiFi immediately; that race is inherent to the
    Linux desktop sleep flow. The inhibitor gives us a real window
    (vs. the system-sleep hook, which fires *after* NM teardown
    completes), and the existing Tor circuits to the hub often
    survive long enough for the POST to land.

    Falls back gracefully — if logind isn't reachable or the import
    failed, the daemon still runs, just without sync-on-sleep
    (heartbeat-timeout fallback path picks up after a few minutes).
    """

    def __init__(self):
        self._inhibitor_fd = None
        self._bus = None
        self._login_obj = None
        self._loop = None
        self._thread = None

    def start(self):
        if not HAVE_DBUS:
            log.warning("sleep-inhibitor: dbus/glib not importable, skipping")
            return False
        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
            self._login_obj = self._bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
            if not self._acquire():
                return False
            self._bus.add_signal_receiver(
                self._on_prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
                path="/org/freedesktop/login1",
            )
            self._loop = GLib.MainLoop()
            self._thread = threading.Thread(
                target=self._loop.run, daemon=True, name="sleep-inhibitor"
            )
            self._thread.start()
            log.info("sleep-inhibitor: watching logind PrepareForSleep")
            return True
        except Exception as e:
            log.warning("sleep-inhibitor: start failed: %s", e)
            return False

    def _acquire(self):
        if self._inhibitor_fd is not None:
            return True
        try:
            manager = dbus.Interface(
                self._login_obj, "org.freedesktop.login1.Manager"
            )
            fd_obj = manager.Inhibit(
                "sleep",
                "OnionPress",
                "Notify OnionHeaven hub before suspend",
                "delay",
            )
            self._inhibitor_fd = fd_obj.take()
            return True
        except Exception as e:
            log.warning("sleep-inhibitor: Inhibit() failed: %s", e)
            self._inhibitor_fd = None
            return False

    def _release(self):
        if self._inhibitor_fd is None:
            return
        try:
            os.close(self._inhibitor_fd)
        except OSError:
            pass
        self._inhibitor_fd = None

    def _on_prepare_for_sleep(self, going_to_sleep):
        if bool(going_to_sleep):
            log.info("sleep-inhibitor: PrepareForSleep(true) — sending /offline + DEL_ONION")
            if _reachability is not None:
                _reachability.note_sleep()
            try:
                self._send_offline_now()
            except Exception as e:
                log.warning("sleep-inhibitor: send_offline raised: %s", e)
            # Signal tor-watchdog inside both Tor-carrying containers to
            # DEL_ONION their services. Done here, not in the system-sleep
            # hook, because we still hold the delay inhibitor — docker is
            # still responsive. Once the hook fires it's after WiFi
            # teardown and docker exec gets throttled to >2s.
            self._signal_watchdogs("USR1")
            self._release()
            log.info("sleep-inhibitor: delay lock released, suspend can proceed")
        else:
            log.info("sleep-inhibitor: PrepareForSleep(false) — woke up, sending ADD_ONION")
            # Re-acquire the inhibitor for the next sleep cycle.
            self._acquire()
            # Signal tor-watchdog to ADD_ONION services back. The watchdog
            # waits for Tor circuits internally before doing the ADD, so
            # there's no race with WiFi reconnecting.
            self._signal_watchdogs("USR2")
            # Issue #266: a pre-sleep failure window must not fire DEL+ADD
            # on the first probe after wake (mirrors src/menubar.py).
            if _reachability is not None:
                _reachability.note_wake()
            # Kick the main heartbeat loop awake so it sends /online
            # immediately instead of waiting out the remainder of its
            # time.sleep(HEARTBEAT_INTERVAL). Sending SIGUSR2 to our own
            # PID runs _usr2_handler (which sets _pending_online) AND
            # interrupts time.sleep via EINTR.
            try:
                os.kill(os.getpid(), signal.SIGUSR2)
            except OSError as e:
                log.warning("sleep-inhibitor: self-SIGUSR2 failed: %s", e)
                # Fallback: at least set the flag directly so the next
                # natural loop iteration picks it up.
                global _pending_online
                _pending_online = True

    def _signal_watchdogs(self, sig):
        """Send a Unix signal to tor-watchdog inside each Tor-carrying
        container, in parallel threads so neither one's latency stacks
        onto the other. Each call is best-effort: failure is logged but
        doesn't block the rest of the suspend flow."""
        threads = []
        for c in ("onionpress-tor", "onionheaven"):
            t = threading.Thread(
                target=self._signal_watchdog_one, args=(c, sig),
                daemon=True, name=f"signal-{c}",
            )
            t.start()
            threads.append(t)
        # Bounded join: don't block the inhibitor lock release for longer
        # than necessary. 3s is plenty when docker is still responsive
        # (typical call is ~100ms); a slow run still logs even after the
        # join times out.
        for t in threads:
            t.join(timeout=3)

    def _signal_watchdog_one(self, container, sig):
        try:
            result = subprocess.run(
                ["docker", "exec", container, "sh", "-c",
                 f"kill -{sig} $(pgrep -f '[t]or-watchdog')"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                log.info("sleep-inhibitor: SIG%s → %s tor-watchdog", sig, container)
            else:
                log.warning("sleep-inhibitor: %s tor-watchdog signal failed (rc=%d): %s",
                            container, result.returncode, result.stderr.strip()[:120])
        except subprocess.TimeoutExpired:
            log.warning("sleep-inhibitor: %s tor-watchdog signal timed out", container)
        except Exception as e:
            log.warning("sleep-inhibitor: %s tor-watchdog signal errored: %s", container, e)

    def _send_offline_now(self):
        """Fast-path /offline through the persistent HubConnection — the
        only path that wins the race against NetworkManager's WiFi
        teardown on Linux suspend. Falls back to sign_and_post (which
        will itself fall back to docker exec) only if the persistent
        connection isn't established yet."""
        if not _registered:
            log.info("sleep-inhibitor: not registered yet, skipping /offline")
            return
        if _content_addr is None or _content_addr == _current_hub:
            return
        if _hc_addr is None or _priv_key is None or _pub_key is None:
            log.info("sleep-inhibitor: state incomplete, skipping /offline")
            return

        payload = _build_signed_payload(
            "offline", _content_addr, _hc_addr, _priv_key, _pub_key,
        )

        if _hub_conn is not None and _hub_conn.is_open():
            t0 = time.monotonic()
            sent = _hub_conn.post_fast_no_response("offline", payload)
            dt_ms = (time.monotonic() - t0) * 1000
            if sent:
                log.info(
                    "sleep-inhibitor: /offline POSTed on persistent socket in %.1fms",
                    dt_ms,
                )
                return
            log.warning("sleep-inhibitor: persistent socket send failed — falling back")

        # Persistent socket isn't open (first ever sleep, post-restart),
        # OR send failed. Fall through to the slow path; it may or may
        # not land before NM kills the network.
        ok, resp = sign_and_post(
            "offline", _current_hub, _content_addr, _hc_addr,
            _priv_key, _pub_key,
            max_time=4, docker_timeout=6,
        )
        if ok and resp and resp.get("offline"):
            log.info("sleep-inhibitor: /offline acknowledged by hub (slow path)")
        else:
            log.warning("sleep-inhibitor: /offline POST failed (ok=%s)", ok)


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------

_opt_out_last_attempt = 0.0   # time.monotonic() of the last opt-out /unregister
_opt_out_gave_up = False      # the record names an address whose key we lack


def _stop_heartbeats(choice):
    if choice == "no":
        log.info("OnionHeaven disabled (REGISTER_WITH_ONIONHEAVEN=no) — heartbeats stopped")
    else:
        log.info("REGISTER_WITH_ONIONHEAVEN is unset — heartbeats stopped; "
                 "nothing is sent to the hub until it is yes")


def _maybe_unregister_after_opt_out(choice, now=None):
    """REGISTER_WITH_ONIONHEAVEN=no and a record that this install
    registered: one signed /unregister to the hub the record names, so it
    deletes its copy of the key and does not take over a site that is
    still running, then the record is cleared (unregister() does that).

    Covers a "yes" -> "no" change while running (CLI, tray, Settings page)
    and a daemon that starts with "no" after an earlier registration.
    Undecided (unset) sends nothing. A failed attempt is retried every
    OPT_OUT_RETRY_SECONDS. Returns unregister()'s result, or None when
    nothing was sent.
    """
    global _opt_out_last_attempt, _opt_out_gave_up
    if choice != "no":
        # A later "no" acts at once.
        _opt_out_last_attempt = 0.0
        return None
    state = _load_state()
    if not state.get("registered") or _opt_out_gave_up:
        return None
    now = time.monotonic() if now is None else now
    if _opt_out_last_attempt and now - _opt_out_last_attempt < OPT_OUT_RETRY_SECONDS:
        return None
    if _content_addr is None or _priv_key is None or _pub_key is None:
        return None
    recorded = state.get("content_address") or _content_addr
    if recorded != _content_addr:
        # Only the key of the recorded address can sign its /unregister.
        log.warning("OnionHeaven: the registration record names %s, but this "
                    "install's key is %s's; cannot unregister it", recorded,
                    _content_addr)
        _opt_out_gave_up = True
        return None
    hub = state.get("onionheaven_address")
    if not key_manager.is_valid_onion_address(hub):
        hub = _current_hub
    if hub == _content_addr:
        return None
    _opt_out_last_attempt = now
    log.info("OnionHeaven turned off (REGISTER_WITH_ONIONHEAVEN=no) — asking %s "
             "to delete its copy of this site's key", hub)
    result = unregister(hub, _content_addr, _hc_addr, _priv_key, _pub_key,
                        while_choice="no")
    if result is None:
        _opt_out_last_attempt = 0.0  # turned back on meanwhile
    elif result is False:
        log.info("OnionHeaven: will retry the /unregister in %d min",
                 OPT_OUT_RETRY_SECONDS // 60)
    return result


# ---------------------------------------------------------------------------
# What the hubs may still hold (record upkeep)
# ---------------------------------------------------------------------------
#
# These follow explicit ends of a hub's hold on a key (a hub change, a key
# import or restore, a reset or uninstall the launcher could not finish),
# so they go out whatever REGISTER_WITH_ONIONHEAVEN says.

_left_last_attempt = 0.0   # time.monotonic() of the last left_on_hubs retry
_left_reported = set()     # entries for a lost key, logged once per run

HOSTNAME_PATH = "/var/lib/tor/hidden_service/wordpress/hostname"


def _reconcile_record():
    """At start, with this daemon's key loaded. A registration the record
    names for another address is a key this install no longer has (an
    import or restore while the daemon was stopped, nothing sent first):
    it moves to left_on_hubs. Entries for a lost key are logged once."""
    state = _load_state()
    reg = _state_registration(state)
    if reg is not None and reg[0] != _content_addr:
        _note_left_on_hub(state, reg[0], reg[1], "address changed")
        state["registered"] = False
        _save_state(state)
    for entry in _left_on_hubs(state):
        pair = _entry_pair(entry)
        if pair[0] != _content_addr and pair not in _left_reported:
            _left_reported.add(pair)
            log.warning("OnionHeaven: %s may still hold the onion service key of "
                        "%s (since %s, %s). This install no longer has that key, "
                        "so OnionPress cannot ask the hub to delete it; ask the "
                        "hub's operator.", pair[1], pair[0],
                        entry.get("since", "?"), entry.get("reason", "?"))


def _maybe_release_left_hubs(choice, now=None):
    """left_on_hubs entries for this daemon's address (its key is in
    memory) get a signed /unregister, every OPT_OUT_RETRY_SECONDS. Not the
    hub this install registers with while the choice is yes: the
    heartbeat puts the key there again, so that entry is dropped.
    Returns the unregister() results, or None when nothing was sent."""
    global _left_last_attempt
    state = _load_state()
    current = (_content_addr, _current_hub)
    if choice == "yes" and _forget_left_on_hub(state, *current):
        _save_state(state)
    pairs = [_entry_pair(e) for e in _left_on_hubs(state)
             if e["content_address"] == _content_addr
             and e["onionheaven_address"] != _content_addr
             and not (choice == "yes" and _entry_pair(e) == current)]
    if not pairs:
        return None
    now = time.monotonic() if now is None else now
    if _left_last_attempt and now - _left_last_attempt < OPT_OUT_RETRY_SECONDS:
        return None
    _left_last_attempt = now
    results = []
    for _addr, hub in pairs:
        log.info("OnionHeaven: asking %s to delete its copy of this site's key", hub)
        results.append(unregister(hub, _content_addr, _hc_addr, _priv_key, _pub_key))
    return results


def _holding_key_on_hubs():
    """Does the record say a hub may hold the key this daemon loaded?"""
    state = _load_state()
    reg = _state_registration(state)
    return ((reg is not None and reg[0] == _content_addr)
            or any(e["content_address"] == _content_addr
                   for e in _left_on_hubs(state)))


def _key_replaced():
    """The address the tor container serves, when it is not the one whose
    key this daemon loaded (a key import or restore while it ran), else
    None (also when it cannot be read: the container may be restarting)."""
    ok, served = docker_exec("onionpress-tor", ["cat", HOSTNAME_PATH], timeout=10)
    served = served.strip() if ok else ""
    if key_manager.is_valid_onion_address(served) and served != _content_addr:
        return served
    return None


def _release_replaced_key(served):
    """This daemon holds a key that is no longer the site's. Before it exits
    (systemd starts it again with the new key), every hub the record says
    may hold the old key is asked to delete it: the key is still in
    memory to sign the request, and nothing else can once it exits. What
    does not answer stays in left_on_hubs."""
    log.warning("OnionHeaven: the onion service key changed (Tor serves %s now; "
                "this daemon holds the key of %s). Asking the hubs to delete "
                "their copy of the old key, then restarting.", served, _content_addr)
    state = _load_state()
    pairs = []
    reg = _state_registration(state)
    if reg is not None and reg[0] == _content_addr:
        pairs.append(reg)
    pairs += [_entry_pair(e) for e in _left_on_hubs(state)
              if e["content_address"] == _content_addr and _entry_pair(e) not in pairs]
    for _addr, hub in pairs:
        if hub == _content_addr:
            continue
        if unregister(hub, _content_addr, _hc_addr, _priv_key, _pub_key) is not True:
            state = _load_state()
            _note_left_on_hub(state, _content_addr, hub, "key replaced")
            if _state_registration(state) == (_content_addr, hub):
                state["registered"] = False
            _save_state(state)
            log.warning("OnionHeaven: %s may still hold the key of %s", hub, _content_addr)


def main():
    global _current_hub, _content_addr, _hc_addr, _priv_key, _pub_key, _running
    global _pending_offline, _pending_online

    os.makedirs(DATA_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGUSR1, _usr1_handler)
    signal.signal(signal.SIGUSR2, _usr2_handler)

    log.info("heartbeat client starting...")
    # Signing goes on without libsodium (a heartbeat that cannot be signed
    # lets the hub take the site over), but on variable-time pure Python.
    if not onion_auth.signing_uses_libsodium():
        log.warning("Requests are signed without libsodium (pure-Python "
                    "ed25519, not constant-time): install python3-nacl "
                    "(sudo apt install python3-nacl)")

    # Wait for addresses and keys
    result = wait_for_ready()
    if result is None:
        log.error("Timed out waiting for addresses/keys after %ds", READY_TIMEOUT)
        sys.exit(1)

    _content_addr, _hc_addr, _priv_key, _pub_key = result
    log.info("Content address: %s", _content_addr)
    log.info("Healthcheck address: %s", _hc_addr)

    # Read initial config
    config = read_config()
    # Opt-in: only an explicit "yes" registers. Unset means the user has
    # not decided yet (the tray / `onionpress onionheaven` ask them).
    choice = _registration_choice(config)
    enabled = choice == "yes"
    _current_hub = _hub_address(config)

    # Opt-in analytics upload to OnionHome. Started for every node, hub
    # included (OnionHome is a different hub than OnionHeaven), before the
    # self-hub check below. Uploads only ever happen on an explicit
    # SHARE_ANALYTICS_WITH_ONIONHOME=yes, re-read at every wake.
    _start_analytics_sharing(_content_addr, _hc_addr)
    analytics_opt_in = _poll_analytics_opt_in(None, config)

    # Onion service recovery (issue #266), also for the hub, so before the
    # self-hub check below.
    _start_reachability_loop()

    # Self-registration check: if our address IS the hub, skip all client activity
    if _content_addr == _current_hub:
        log.info("This node IS the OnionHeaven hub (%s) — skipping client registration", _current_hub)
        # Sleep forever (or until signal) — the server container handles everything
        while _running:
            time.sleep(60)
            analytics_opt_in = _poll_analytics_opt_in(analytics_opt_in, read_config())
        return

    global _registered, _hub_conn
    _registered = False

    # A registration recorded for an address whose key this install no
    # longer has, and what earlier hubs may still hold.
    _reconcile_record()

    if enabled:
        _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
    elif choice == "no":
        log.info("OnionHeaven disabled (REGISTER_WITH_ONIONHEAVEN=no; "
                 "enable with: onionpress onionheaven enable)")
        # Registered in an earlier session? Ask the hub to forget us.
        _maybe_unregister_after_opt_out(choice)
    else:
        log.info("Registration not enabled (REGISTER_WITH_ONIONHEAVEN is not 'yes'; "
                 "enable with: onionpress onionheaven enable)")

    # Establish the persistent HubConnection now that registration has
    # primed Tor's circuit to the hub. Subsequent /online heartbeats go
    # through this socket, keeping it warm; the SleepInhibitor reuses
    # the same socket on PrepareForSleep so /offline lands in ~5ms
    # instead of the ~1.7s a fresh SOCKS5+Tor rendezvous takes.
    #
    # Open eagerly (rather than lazy on first /online) so a fast lid-close
    # right after daemon restart still has a hot socket — without this,
    # the first 60s of daemon uptime had the SleepInhibitor falling
    # through to the slow path with no socket to use. Only once
    # registered: an install that has not opted in keeps no connection
    # to the hub.
    if HAVE_PYSOCKS and _registered and _content_addr != _current_hub:
        _hub_conn = HubConnection(_current_hub)
        if _hub_conn.connect():
            log.info("hub-connection: persistent SOCKS5 socket open")
        else:
            log.warning("hub-connection: initial connect failed, will retry on first heartbeat")

    # Start the logind sleep-inhibitor now that we have keys + a hub.
    # The inhibitor sends /offline synchronously on PrepareForSleep(true)
    # — the only Linux mechanism that beats NetworkManager's sleep
    # teardown reliably. See SleepInhibitor docstring above.
    sleep_inhibitor = SleepInhibitor()
    sleep_inhibitor.start()

    # Heartbeat loop. Uses the _registered global so the sleep-inhibitor
    # thread sees the current registration state without needing locks
    # (single-writer/single-reader, value semantics on a bool).
    while _running:
        time.sleep(HEARTBEAT_INTERVAL)
        if not _running:
            break

        # Re-read config each cycle, before anything is sent: the choice
        # gates every request below, the sleep/wake kicks included.
        config = read_config()
        choice = _registration_choice(config)
        new_enabled = choice == "yes"
        new_hub = _hub_address(config)
        analytics_opt_in = _poll_analytics_opt_in(analytics_opt_in, config)

        # The key this daemon loaded at start may have been replaced since
        # (a key import or restore while it ran): it must not go on sending
        # it. While a hub may hold it, check what Tor serves; if that
        # changed, have the hubs delete the old key (signed with the copy
        # in memory, the last one this install has) and exit, so systemd
        # starts the daemon again with the new key.
        if _registered or _holding_key_on_hubs():
            served = _key_replaced()
            if served:
                _registered = False
                _release_replaced_key(served)
                break

        # Turned off ("no", or back to unset): stop at once. Nothing that
        # carries the key goes out from here on, and no /offline either:
        # it would make the hub take over a site that is still running.
        if not new_enabled and _registered:
            _stop_heartbeats(choice)
            _registered = False
            enabled = False
            if _hub_conn is not None:
                _hub_conn.close()
                _hub_conn = None

        # Async sleep/wake kicks from the system-sleep hook. USR1 means the
        # laptop is about to suspend — flush /offline so the hub takes over
        # without waiting for missed heartbeats to time out. USR2 means we
        # just woke — send /online right now instead of waiting up to a
        # full HEARTBEAT_INTERVAL for the regular cycle to fire.
        if _pending_offline and _registered and _content_addr != _current_hub:
            _pending_offline = False
            log.info("USR1: sending /offline before suspend...")
            send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
        elif _pending_offline:
            _pending_offline = False  # nothing to do, swallow the flag
        if _pending_online:
            _pending_online = False
            if _registered and _content_addr != _current_hub:
                log.info("USR2: sending immediate /online after wake")
                heartbeat(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

        # Explicit opt-out: if this install registered, one signed
        # /unregister (retried while the hub does not answer), so the hub
        # deletes its copy of the key. Unset sends nothing.
        _maybe_unregister_after_opt_out(choice)
        # Earlier hubs (a hub change, a release the launcher could not
        # finish) that may hold this site's key: asked again, whatever the
        # choice.
        _maybe_release_left_hubs(choice)
        if choice == "no":
            _current_hub = new_hub
            enabled = False
            continue

        # Handle hub address change
        if new_hub != _current_hub and _registered:
            log.info("Hub address changed from %s to %s", _current_hub, new_hub)
            # Unregister from old hub to prevent false takeover (and so it
            # deletes its copy of the key). If it does not answer, the record
            # keeps it in left_on_hubs and _maybe_release_left_hubs asks again.
            if unregister(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key) is not True:
                state = _load_state()
                _note_left_on_hub(state, _content_addr, _current_hub, "hub changed")
                if _state_registration(state) == (_content_addr, _current_hub):
                    state["registered"] = False
                _save_state(state)
            _registered = False
            # Persistent socket points at the old hub — drop it so the
            # next /online opens a fresh one to the new hub.
            if _hub_conn is not None:
                _hub_conn.close()
                _hub_conn = None
            _current_hub = new_hub
            if new_enabled:
                # Self-check for new hub
                if _content_addr == _current_hub:
                    log.info("New hub is this node — stopping client activity")
                    while _running:
                        time.sleep(60)
                    break
                _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
                if _registered and HAVE_PYSOCKS:
                    _hub_conn = HubConnection(_current_hub)
            continue
        _current_hub = new_hub

        if new_enabled and not enabled and not _registered:
            log.info("Registration re-enabled — registering")
            if _content_addr == _current_hub:
                log.info("This node IS the hub — skipping")
                enabled = True
                continue
            _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            if _registered and HAVE_PYSOCKS and _hub_conn is None:
                _hub_conn = HubConnection(_current_hub)
            enabled = True
            continue

        enabled = new_enabled

        # Retry registration if not yet registered
        if enabled and not _registered:
            if _content_addr != _current_hub:
                log.info("Retrying registration with %s...", _current_hub)
                _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
                if _registered and HAVE_PYSOCKS and _hub_conn is None:
                    _hub_conn = HubConnection(_current_hub)
            continue

        # Send heartbeat if registered
        if _registered:
            heartbeat(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    # Graceful shutdown: send /offline
    if _registered and _current_hub:
        log.info("Sending /offline before shutdown...")
        send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    if _hub_conn is not None:
        _hub_conn.close()

    log.info("heartbeat client stopped")


def _cached_content_address():
    """Read content address from the registration state file (instant)."""
    try:
        with open(STATE_PATH) as f:
            return (json.load(f).get("content_address") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _cached_hc_address():
    """Read healthcheck address from ~/.onionpress/healthcheck-address (instant)."""
    try:
        with open(os.path.join(DATA_DIR, "healthcheck-address")) as f:
            return f.read().strip()
    except OSError:
        return ""


def offline_once():
    """One-shot synchronous /offline POST for the system-sleep hook.

    The long-running daemon receives SIGUSR1 from the hook and sets a
    flag, but the actual POST happens later in the main loop — by which
    time systemd-logind has often already proceeded with the suspend
    (it only waits for the hook to return, not for the heartbeat process
    to finish its async work). This entry point runs in the hook's own
    process so the hook can't return until the /offline POST has been
    attempted.

    Reads cached addresses from disk to avoid extra docker exec calls
    on the hot path — the daemon writes the registration state file
    and the healthcheck-address file during normal operation, so by
    the time the user can suspend the box those files are populated.
    Only the key extraction needs a docker exec, and the curl POST
    itself.
    """
    # Drop the StreamHandler the daemon installs — when invoked by the
    # launcher's sleep-pre our stderr is already redirected into the
    # same onionpress.log file the FileHandler writes to, so each line
    # would land twice without this.
    root = logging.getLogger()
    for h in list(root.handlers):
        if type(h) is logging.StreamHandler:
            root.removeHandler(h)

    config = read_config()
    if _registration_choice(config) != "yes":
        return 0
    hub = _hub_address(config)

    content = _cached_content_address()
    if not key_manager.is_valid_onion_address(content):
        # Fall back to container hostname file with a generous timeout —
        # docker exec on rootless docker can take a few seconds when the
        # box is preparing to suspend.
        ok, content = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=5,
        )
        content = content.strip() if ok else ""
        if not key_manager.is_valid_onion_address(content):
            log.warning("offline-once: content address unavailable, skipping /offline")
            return 1
    if content == hub:
        return 0  # We ARE the hub — nothing to notify.

    hc = _cached_hc_address()
    if not key_manager.is_valid_onion_address(hc):
        ok, hc = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
            timeout=5,
        )
        hc = hc.strip() if ok else ""
        if not key_manager.is_valid_onion_address(hc):
            log.warning("offline-once: healthcheck address unavailable, skipping /offline")
            return 1

    try:
        priv_key, pub_key = key_manager.extract_keys()
    except Exception as e:
        log.warning("offline-once: keys unavailable: %s", e)
        return 1

    log.info("offline-once: posting /offline to %s", hub)
    ok, resp = sign_and_post(
        "offline", hub, content, hc, priv_key, pub_key,
        max_time=4, docker_timeout=6,
    )
    if ok and resp and resp.get("offline"):
        log.info("offline-once: /offline acknowledged by hub")
        return 0
    log.warning("offline-once: /offline POST failed (ok=%s resp=%s)", ok, resp)
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--offline-once":
        sys.exit(offline_once())
    main()
