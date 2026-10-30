#!/usr/bin/env python3
"""
OnionPress Web Server

Lightweight HTTP server (Python stdlib only) that handles:
- OnionHeaven registration, unregistration, and lifecycle notifications
- OnionHome analytics log collection (auto-detected, no config needed)

Runs inside the tor container on port 8083, exposed through the onion service.

Access rules. Port 8083 is published on every node's wordpress onion
service, so everything below is reachable by anyone over Tor. Tor hands
onion-service connections to this process from 127.0.0.1, which is why a
loopback source address proves nothing and is never trusted on its own.
The only source-address trust is the Docker bridge range (172.x / 10.x):
those requests can only come from a container on the compose network.

/online, /offline, /unregister and /reset-onionheaven are hub-only: every
other node answers them 403 without parsing the body (see "Hub admission"
in onionheaven_common).

Endpoints:
  POST /online       — Heartbeat / register (upserts registry entry, stores arti key).
                       Requires a valid ed25519 signature from the content
                       address's own key, from every source address.
                       Hub only.
  POST /unregister   — Release takeover (DEL_ONION + decrement worker), mark
                       row status='unregistered', delete arti key file.
                       Row is NOT deleted — a subsequent /online from the same
                       (content_address, healthcheck_address) transitions
                       status back to 'online' (re-registration).
                       Signature required, from every source address. Hub only.
  POST /offline      — Notify OnionHeaven that instance is going offline.
                       Signature required, from every source address. Hub only.
  POST /reset-onionheaven — Clean stress tests + refresh workers with current
                       code. Docker-bridge sources only; Tor-origin callers
                       get 403. One run at a time — a concurrent call gets
                       409. Hub only. Needs the farm controller, which this
                       container cannot reach: 503, and the `onionpress
                       reset-onionheaven` CLI runs the reset in the
                       onionheaven container instead.
  POST /logs/manifest — (OnionHome only) Accept log file manifests for analytics sharing
  POST /logs/upload   — (OnionHome only) Accept log file uploads for analytics sharing
  POST /api/name/register, /release — (OnionHome only) signature required
  POST /api/name/register-local, /release-local — Docker-bridge sources only
  GET  /status       — Public status summary (no auth); accepting_registrations
                       says whether this node is a hub
  GET  /status/<addr> — Coarse: whether the content address is registered
                       and its status word (no auth)
  POST /status/<addr> — Per-address detail, signed by the address's own key
                       (endpoint "status")
"""

MAX_REQUEST_BODY = 1_048_576  # 1 MB — reject larger POST bodies to prevent memory exhaustion
MAX_LOG_UPLOAD_BODY = 10_485_760  # 10 MB, /logs/upload only

# Bounds on what an unauthenticated client can hold (#461). Every node keeps
# one keep-alive connection to its hub, so the connection cap sits well
# above the node count; bodies are read under a deadline, never just an
# idle timeout, and only a few large ones at a time. The deadline is
# generous: onion circuits stall for minutes, so a body gets at least the
# handler's idle timeout (300 s) and a slow circuit's rate; memory is
# bounded by the two caps, not by it.
MAX_CONNECTIONS = 1024
MAX_LARGE_BODIES = 4  # concurrent bodies above MAX_REQUEST_BODY
BODY_DEADLINE_FLOOR = 300  # seconds, plus one per BODY_MIN_RATE bytes
BODY_MIN_RATE = 4096

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from onion_auth import verify_payload, verify_name_payload, VerificationUnavailable
from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    note_signed_request, short_text, ONIONHOME_ADDRESS,
    takeover_function, release_function,
    is_registration_hub, registrations_opted_in, write_key_file,
    KEYS_DIR, PROPAGATION_DELAY, ONIONHEAVEN_DATA_DIR, OWN_HOSTNAME_PATH,
)
import onionnames

# What a free registration may take (issue #391). Minting a key is
# instant, so without these a Tor client could hold thousands of registry
# rows and key dirs, fill the hub's workers and OnionHome's analytics
# disk, and direct the hub's audit fetches. A content address keeps at
# most MAX_ROWS_PER_ADDRESS (content, healthcheck) rows that are not
# unregistered; the hub takes at most MAX_REGISTRANTS content addresses
# (a new one past that is told the hub is full, before anything is
# verified or written); OnionHome stores analytics for at most
# ANALYTICS_MAX_PAIRS_PER_ADDRESS pairs per content address and
# ANALYTICS_MAX_INSTANCES pairs in all. A pair that already exists is
# never refused by these: heartbeats and uploads of the registered keep
# landing when the hub is full.
MAX_ROWS_PER_ADDRESS = int(os.environ.get("ONIONHEAVEN_MAX_ROWS_PER_ADDRESS", "4"))
MAX_REGISTRANTS = int(os.environ.get("ONIONHEAVEN_MAX_REGISTRANTS", "5000"))
ANALYTICS_MAX_PAIRS_PER_ADDRESS = int(os.environ.get("ONIONHOME_ANALYTICS_MAX_PAIRS_PER_ADDRESS", "4"))
ANALYTICS_MAX_INSTANCES = int(os.environ.get("ONIONHOME_ANALYTICS_MAX_INSTANCES", "10000"))


def registration_room(conn, content_address, healthcheck_address):
    """(status, error) refusing a registration the caps leave no room for,
    or (None, None). A pair already in the registry (not unregistered)
    always has room: its heartbeats are what keeps it reachable."""
    if healthcheck_address:
        row = conn.execute(
            "SELECT 1 FROM registry WHERE content_address = ? AND healthcheck_address = ? "
            "AND unregistered_at IS NULL", (content_address, healthcheck_address)).fetchone()
        if row:
            return None, None
    rows = conn.execute(
        "SELECT COUNT(*) FROM registry WHERE content_address = ? AND unregistered_at IS NULL",
        (content_address,)).fetchone()[0]
    if rows:
        if rows >= MAX_ROWS_PER_ADDRESS:
            return 429, (f"This content address already has {rows} healthcheck addresses "
                         f"registered (the limit is {MAX_ROWS_PER_ADDRESS}); unregister one first")
        return None, None
    registrants = conn.execute(
        "SELECT COUNT(DISTINCT content_address) FROM registry WHERE unregistered_at IS NULL"
    ).fetchone()[0]
    if registrants >= MAX_REGISTRANTS:
        return 503, (f"This hub is full ({registrants} registered sites); "
                     f"it takes no new site. Use another OnionHeaven hub.")
    return None, None


def analytics_room(content_address, healthcheck_address, analytics_dir=None):
    """(status, error) refusing a new analytics pair the caps leave no room
    for, or (None, None). An existing pair's directory always has room."""
    base = ANALYTICS_DIR if analytics_dir is None else analytics_dir
    site_dir = os.path.join(base, content_address, healthcheck_address)
    if os.path.isdir(site_dir):
        return None, None
    addr_dir = os.path.join(base, content_address)
    try:
        pairs_here = sum(1 for e in os.scandir(addr_dir) if e.is_dir(follow_symlinks=False))
    except OSError:
        pairs_here = 0
    if pairs_here >= ANALYTICS_MAX_PAIRS_PER_ADDRESS:
        return 429, (f"analytics are kept for at most {ANALYTICS_MAX_PAIRS_PER_ADDRESS} "
                     f"healthcheck addresses per site")
    total = 0
    try:
        for addr in os.scandir(base):
            if not addr.is_dir(follow_symlinks=False):
                continue
            try:
                total += sum(1 for e in os.scandir(addr.path) if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
            if total >= ANALYTICS_MAX_INSTANCES:
                break
    except OSError:
        total = 0
    if total >= ANALYTICS_MAX_INSTANCES:
        return 507, f"analytics storage is full ({total} sites); no new site is taken"
    return None, None


# A manifest offers a day's log files; more names than this in one is
# nothing a client sends, and each is written to the pair's audit file.
MAX_MANIFEST_FILES = 1000


def _v3(value):
    """True if value is one v3 onion address (a str; JSON carries anything)."""
    return isinstance(value, str) and ONION_RE.fullmatch(value) is not None


def analytics_disk_full():
    """True when the filesystem holding ANALYTICS_DIR is past
    ANALYTICS_DISK_THRESHOLD. ANALYTICS_DIR is a bind mount of the host's
    folder (on a Mac, the Mac's disk); "/" is the container's own disk,
    which the analytics never fill (issue #468). A usage that cannot be
    read counts as not full, as before."""
    try:
        usage = shutil.disk_usage(ANALYTICS_DIR)
    except OSError:
        return False
    return usage.used / usage.total > ANALYTICS_DISK_THRESHOLD


def analytics_pair_bytes(site_dir):
    """The bytes a pair's folder holds: its regular files, links not
    followed; 0 for a folder that is not there."""
    total = 0
    try:
        with os.scandir(site_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def analytics_pair_room(site_dir, incoming, replacing=None):
    """(status, error) refusing a write of `incoming` bytes into a pair's
    folder that would put it past ANALYTICS_PER_INSTANCE_QUOTA, or
    (None, None); `replacing` names a regular file there the write
    replaces, whose size is freed first. Checked before every write
    (issue #468): the quota used to be held only by the daily cleanup,
    so a pair could put a day's uploads past it, and files a client
    sent with an empty healthcheck address landed beside the pairs
    where the cleanup never looked."""
    held = analytics_pair_bytes(site_dir)
    if replacing:
        try:
            st = os.lstat(os.path.join(site_dir, replacing))
            if stat.S_ISREG(st.st_mode):
                held -= st.st_size
        except OSError:
            pass
    if held + incoming > ANALYTICS_PER_INSTANCE_QUOTA:
        return 507, (f"this site's analytics are at their quota "
                     f"({ANALYTICS_PER_INSTANCE_QUOTA // 1048576} MB); nothing more is "
                     f"stored until older files expire")
    return None, None


def _load_key_convert():
    """key-convert.py, beside this file: the parser the takeover path uses
    (tor-manager and the queue manager run its pem-to-* commands)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key-convert.py")
    spec = importlib.util.spec_from_file_location("key_convert", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


key_convert = _load_key_convert()

SERVER_VERSION = os.environ.get("ONIONPRESS_VERSION", "unknown")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8083

# OnionHome address — the /logs/* and /api/name/* endpoints only accept
# requests when this instance IS OnionHome. Derived from the actual onion
# hostname (same source _get_own_address() uses); the previous
# /var/lib/onionpress/onion_address path depended on the launcher's
# docker-exec running after readiness checks, which silently never happened
# on boots that timed out — leaving op2home permanently returning 403/404.
_ONIONHOME_ADDRESS = ONIONHOME_ADDRESS     # onionheaven_common's
_is_onionhome_cache = None


def _is_onionhome():
    """Return True if this instance is OnionHome. Cached after first check."""
    global _is_onionhome_cache
    if _is_onionhome_cache is not None:
        return _is_onionhome_cache
    addr = _get_own_address()
    if addr is None:
        # Hostname file not written yet — don't cache, keep retrying so we
        # flip to True as soon as Tor finalizes the hidden service.
        return False
    _is_onionhome_cache = (addr == _ONIONHOME_ADDRESS)
    return _is_onionhome_cache

# Analytics storage
ANALYTICS_DIR = "/var/lib/onionhome/analytics"


def _append_audit(site_dir, record):
    """Append one JSONL entry to ``<site_dir>/audit-YYYY-MM-DD.jsonl``.

    Records per-request telemetry (manifest offers + upload receipts)
    so questions like "what did this instance offer, and what did we
    actually accept?" can be answered without grepping container
    stderr. The file is naturally bounded: it rotates daily by name
    and is subject to the normal per-instance quota + age-expiry
    cleanup.
    """
    try:
        os.makedirs(site_dir, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = os.path.join(site_dir, f"audit-{now.strftime('%Y-%m-%d')}.jsonl")
        entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        entry.update(record)
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        # O_APPEND makes concurrent writes from threaded request
        # handlers atomic per line on POSIX.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass
ANALYTICS_DISK_THRESHOLD = 0.85  # 85% full → stop accepting / clean up
ANALYTICS_MAX_AGE_DAYS = 90      # hard age ceiling — files older than this
                                 # are deleted on every cleanup cycle, disk
                                 # or no disk pressure
ANALYTICS_PER_INSTANCE_QUOTA = 524_288_000  # 500 MB per (content, healthcheck)
                                            # pair before we start trimming
                                            # that instance's oldest files
ANALYTICS_CLEANUP_INTERVAL = 86_400  # periodic cleanup cadence (seconds)
# Permissive safe-filename check. We accept whatever clients offer so
# that log naming can evolve without a server redeploy; only filenames
# that could escape the site directory or pollute it with hidden files
# are rejected. If a rogue client starts spamming junk names, tighten
# this regex on the OnionHome instance.
ANALYTICS_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")

# Read our own onion address so we can reject self-registration
OWN_ONION_ADDRESS = None
_HOSTNAME_PATH = OWN_HOSTNAME_PATH


def _get_own_address():
    """Return this server's own .onion address (cached after first read)."""
    global OWN_ONION_ADDRESS
    if OWN_ONION_ADDRESS is None:
        try:
            with open(_HOSTNAME_PATH) as f:
                addr = f.read().strip()
            if ONION_RE.fullmatch(addr):
                OWN_ONION_ADDRESS = addr
                log(f"Own onion address: {addr}")
        except OSError:
            pass  # file not written yet, or not readable (e.g. outside container)
    return OWN_ONION_ADDRESS


def _accepts_registrations():
    """True if this node is an OnionHeaven hub and may take registrations.

    The canonical hub address (read from the Tor hostname file, like
    _is_onionhome()) or the operator's ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes.
    Not cached: until the hostname exists only the opt-in admits, and the
    answer flips as soon as Tor writes it.
    """
    return is_registration_hub(_get_own_address())


NOT_A_HUB_ERROR = ("This node is not an OnionHeaven hub and does not accept "
                   "registrations")

# Linux launcher hint (linux/onionpress): keep the onionheaven container up.
ACTIVATE_MARKER = os.path.join(ONIONHEAVEN_DATA_DIR, "activate")


def _sync_activate_marker(poll_seconds=5):
    """Make the activation marker follow hub admission, once it is known.

    Written on a hub and removed on every other node; a registration never
    writes it (it used to, from /online). Waits for the own address first,
    as an unknown address cannot tell the two apart.
    """
    import time as _time
    while _get_own_address() is None and not registrations_opted_in():
        _time.sleep(poll_seconds)
    if _accepts_registrations():
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(ACTIVATE_MARKER, "w") as f:
                f.write(now + "\n")
        except OSError as e:
            log(f"WARNING: could not write activation flag: {e}")
        return
    try:
        os.unlink(ACTIVATE_MARKER)
        log("Removed the OnionHeaven activation flag — this node is not a hub")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"WARNING: could not remove activation flag: {e}")


# ---------------------------------------------------------------------------
# OpenSSH PEM key builder (reimplemented from key_manager.py)
# ---------------------------------------------------------------------------

OPENSSH_MAGIC = b"openssh-key-v1\x00"
ARTI_KEY_TYPE = b"ed25519-expanded@spec.torproject.org"


def validate_arti_pem(pem_bytes):
    """Validate that an Arti PEM key is structurally sound.

    Checks for:
    - Proper PEM header/footer
    - No NUL bytes in the PEM envelope (the error Arti reports)
    - Base64 payload decodes successfully
    - OpenSSH magic header present in decoded data
    - Minimum size for ed25519-expanded key (64 bytes private + 32 bytes public)

    Returns True if valid, False if corrupted.
    """
    try:
        text = pem_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False

    lines = text.strip().splitlines()
    if len(lines) < 3:
        return False
    if not lines[0].startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        return False
    if not lines[-1].startswith("-----END OPENSSH PRIVATE KEY-----"):
        return False

    # Extract base64 payload between header and footer
    b64_payload = "".join(lines[1:-1])

    # Check for NUL bytes in the PEM text (the specific Arti error)
    if "\x00" in b64_payload:
        return False

    # Decode and verify OpenSSH structure
    try:
        decoded = base64.b64decode(b64_payload)
    except Exception:
        return False

    if not decoded.startswith(OPENSSH_MAGIC):
        return False

    # Minimum size: magic(15) + ciphername(8) + kdfname(8) + kdfoptions(4)
    # + nkeys(4) + pubkey(~50) + privkey(~120) = ~200+ bytes
    if len(decoded) < 100:
        return False

    return True


def extract_public_key_from_arti_pem(pem_bytes):
    """Return the 32-byte Ed25519 public key embedded in an Arti OpenSSH PEM,
    or None if the PEM is malformed. Caller is responsible for derivation.

    The format (mirror of build_openssh_key above):
      OPENSSH_MAGIC | str("none") | str("none") | str("") | u32(1)
      | str(pub_blob) | str(priv_blob)
    where pub_blob = str(ARTI_KEY_TYPE) | str(public_key_32).
    """
    try:
        text = pem_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = [l for l in text.strip().splitlines()
             if not l.startswith("-----")]
    try:
        decoded = base64.b64decode("".join(lines))
    except Exception:
        return None
    if not decoded.startswith(OPENSSH_MAGIC):
        return None

    def _read_str(buf, off):
        if off + 4 > len(buf):
            raise ValueError("truncated")
        n = struct.unpack(">I", buf[off:off + 4])[0]
        if off + 4 + n > len(buf):
            raise ValueError("truncated")
        return buf[off + 4:off + 4 + n], off + 4 + n

    try:
        off = len(OPENSSH_MAGIC)
        _, off = _read_str(decoded, off)   # ciphername
        _, off = _read_str(decoded, off)   # kdfname
        _, off = _read_str(decoded, off)   # kdfoptions
        off += 4                            # nkeys (always 1)
        pub_blob, _ = _read_str(decoded, off)
        ktype, sub = _read_str(pub_blob, 0)
        if ktype != ARTI_KEY_TYPE:
            return None
        public_key, _ = _read_str(pub_blob, sub)
        if len(public_key) != 32:
            return None
        return public_key
    except (ValueError, struct.error):
        return None


def _pack_string(data):
    """Pack bytes as uint32 big-endian length + data."""
    return struct.pack(">I", len(data)) + data


def build_openssh_key(private_key, public_key):
    """Build an OpenSSH PEM private key for Arti from raw Ed25519 keys.

    private_key: 64 bytes (expanded Ed25519)
    public_key: 32 bytes
    Returns bytes (PEM-encoded).
    """
    # Build public key blob
    pub_blob = _pack_string(ARTI_KEY_TYPE) + _pack_string(public_key)

    # Build private key blob
    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (
        check + check +
        _pack_string(ARTI_KEY_TYPE) +
        _pack_string(public_key) +
        _pack_string(private_key) +
        _pack_string(b"")  # empty comment
    )
    # Pad to 8-byte boundary
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))

    binary = (
        OPENSSH_MAGIC +
        _pack_string(b"none") +
        _pack_string(b"none") +
        _pack_string(b"") +
        struct.pack(">I", 1) +
        _pack_string(pub_blob) +
        _pack_string(priv_blob)
    )

    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem.encode("utf-8")


_warned_no_sodium = False


def private_key_problem(pem_bytes, public_key):
    """Why the PEM's private key is not public_key's, or None if it is.

    public_key is the PEM's public half, already found to derive to the
    content address. That alone says nothing about the private half, which
    is what a takeover hands to Tor: a PEM with the right public key and a
    wrong secret scalar would overwrite a good stored key, and that site's
    next takeover would fail. So the private section is read the way the
    takeover path reads it (key-convert.py), must carry the same public
    key, and its secret scalar times the base point must be that public key
    (libsodium through PyNaCl: key_convert.keypair_error).

    The request signature covers endpoint, addresses and timestamp, not the
    PEM; with this check the PEM proves itself, since only the address's
    key holder can produce a matching scalar.

    Without PyNaCl (an image built without python3-nacl, which the
    Dockerfile installs) the scalar is not checked: refusing every /online
    would have the hub take over every registered site. Logged once.
    """
    global _warned_no_sodium
    try:
        expanded, inner_public = key_convert.parse_arti_pem_bytes(pem_bytes)
    except Exception:
        return "the private section cannot be read"
    if inner_public != public_key:
        return "the private section carries a different public key"
    try:
        return key_convert.keypair_error(expanded, public_key)
    except key_convert.SodiumUnavailable as e:
        if not _warned_no_sodium:
            _warned_no_sodium = True
            log(f"WARNING: PyNaCl is not available ({e}): registrants' private "
                f"keys are stored without checking them against their "
                f"public keys")
        return None


# ---------------------------------------------------------------------------
# Tor v3 address derivation
# ---------------------------------------------------------------------------

BASE32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def base32_encode(data):
    """RFC 4648 base32 encode (lowercase, no padding)."""
    bits = ""
    for byte in data:
        bits += format(byte, "08b")
    result = []
    for i in range(0, len(bits) - 4, 5):
        result.append(BASE32_ALPHABET[int(bits[i:i + 5], 2)])
    return "".join(result)


def derive_onion_address(public_key_32):
    """Derive a Tor v3 .onion address from a 32-byte Ed25519 public key."""
    checksum_input = b".onion checksum" + public_key_32 + b"\x03"
    checksum = hashlib.sha3_256(checksum_input).digest()[:2]
    addr_bytes = public_key_32 + checksum + b"\x03"
    return base32_encode(addr_bytes) + ".onion"


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

# Only one /reset-onionheaven may run at a time (issue #126). The operation
# tears down and respawns every takeover worker and waits up to 90s for
# them to bootstrap; two overlapping runs would double-spawn workers and
# race each other's registry edits. Non-blocking acquire → 409 on overlap.
_RESET_LOCK = threading.Lock()


NOT_REGISTERED_ERROR = "Entry not found"


def _registry_has(conn, content_address, healthcheck_address):
    """True if the registry holds a row for the address (the pair, when a
    healthcheck address is given), registered or unregistered."""
    if healthcheck_address:
        row = conn.execute(
            "SELECT 1 FROM registry WHERE content_address = ? AND healthcheck_address = ?",
            (content_address, healthcheck_address)).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM registry WHERE content_address = ? LIMIT 1",
            (content_address,)).fetchone()
    return row is not None


def _verify_signature(handler, data, endpoint, registered_only=False):
    """Verify the ed25519 signature on a request.

    Returns (ok, status, error_message): status is the HTTP status to
    answer a refusal with (403, or 404 for an address the registry has
    never seen when registered_only is set) and None when ok.
    Verification is unconditional: the signature IS the ownership
    proof, and the source address carries no information — Tor delivers
    every onion-service request from 127.0.0.1, so a loopback "local
    request" bypass (removed in the fix for #126) let anyone over Tor
    /offline or /unregister any address. Every legitimate caller (Mac
    menubar app, Linux heartbeat daemon, stress-test workers) signs;
    nothing inside the container posts to these endpoints.

    libsodium checks the signature (onion_auth.verify, PyNaCl). Without
    PyNaCl this raises VerificationUnavailable and do_POST answers 503:
    an unchecked /offline or /unregister would let anyone release or
    remove another site's address.

    registered_only (issue #467): the request is for an address the
    registry holds a row for, and one it does not is answered 404
    before a replay mark or an address log line is written for it.
    Any key signs for its own address, so /offline, /unregister and
    the signed status query from a fresh key must leave nothing behind
    on the hub; only /online, which registration_room bounds, may.
    """
    content_address = data.get("content_address", "")
    healthcheck_address = data.get("healthcheck_address", "")
    timestamp = data.get("timestamp", "")
    signature = data.get("signature", "")

    ok, err = verify_payload(
        content_address, endpoint, healthcheck_address,
        timestamp, signature
    )
    if not ok:
        return False, 403, err
    # Verified, and not seen before (issue #400): a captured /offline
    # replayed within the timestamp tolerance forced a takeover of a live
    # site. The mark moves only on a verified signature, so this comes
    # after the check, and the registry is opened only here.
    try:
        conn = db_connect()
        try:
            db_ensure_schema(conn)
            if registered_only and not _registry_has(conn, content_address,
                                                      healthcheck_address):
                return False, 404, NOT_REGISTERED_ERROR
            fresh, why = note_signed_request(conn, content_address, timestamp, signature)
        finally:
            conn.close()
    except Exception as e:
        log(f"REJECT-REPLAY-CHECK /{endpoint}: {content_address}: {e}")
        return False, 403, "Could not check the request against replay"
    if not fresh:
        log(f"REJECT-REPLAY /{endpoint}: {content_address}: {why}")
        return False, 403, why
    return True, None, ""


# Endpoint label of the signed per-address status query (POST
# /status/<address>, issue #469): the full answer goes only to the
# address's own key. src/onionpress/onionheaven.py signs with the same
# literal; a test checks.
STATUS_ENDPOINT = "status"


# Endpoint label of the signature that backs an is_onionheaven claim on
# /online (field onionheaven_peer_signature, same timestamp). Distinct from
# every real endpoint, so neither signature can stand in for the other.
# linux/onionpress-heartbeat.py signs with the same literal; a test checks.
PEER_CLAIM_ENDPOINT = "onionheaven-peer"


def _signed_peer_claim(data):
    """True if /online carries an is_onionheaven claim its sender signed.

    The claim earns ONIONHEAVEN_PEER_GRACE (30 min) before a takeover. The
    field is outside the signed canonical string, so on its own it is not
    trusted: it counts only with its own signature.
    """
    if not data.get("is_onionheaven"):
        return False
    try:
        ok, _ = verify_payload(
            data.get("content_address", ""), PEER_CLAIM_ENDPOINT,
            data.get("healthcheck_address", ""), data.get("timestamp", ""),
            data.get("onionheaven_peer_signature", ""))
    except VerificationUnavailable:
        return False  # a claim nobody could check earns nothing
    return ok


_warned_no_verification = False


def _note_verification_unavailable(error):
    """Log, once per process, that signed requests are being refused."""
    global _warned_no_verification
    if _warned_no_verification:
        return
    _warned_no_verification = True
    log(f"ERROR: signatures cannot be verified ({error}): every signed "
        f"request (/online, /offline, /unregister, names, logs) is refused "
        f"with 503 until python3-nacl is installed. The heartbeat monitor "
        f"takes over no site for a missed heartbeat meanwhile.")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

_large_bodies = threading.BoundedSemaphore(MAX_LARGE_BODIES)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most MAX_CONNECTIONS connections; one
    more is closed at once rather than given a thread (#461)."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class OnionHeavenHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 with keep-alive so clients can hold a persistent socket
    # to the hub and send /offline within milliseconds during system
    # suspend (NetworkManager's WiFi teardown only gives us ~30ms on
    # Linux — no fresh SOCKS5/Tor circuit setup fits inside that). The
    # client's regular /online heartbeats keep the connection warm.
    protocol_version = "HTTP/1.1"

    # Evict connections that have sat idle for too long. Heartbeats run
    # every 60s, so 300s is a comfortable cushion that still bounds
    # thread-per-connection memory under ThreadingHTTPServer.
    timeout = 300

    def log_message(self, format, *args):
        """Override to add timestamp prefix (local time to match host logs)."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] web-server: {format % args}\n")
        sys.stderr.flush()

    def _send_json(self, status_code, data):
        # HTTP/1.1 keep-alive requires Content-Length on every response
        # so the client knows where the body ends without waiting for
        # the socket to close. Encode first, then send the header.
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse_unless_hub(self):
        """Answer 403 and return True if this node is not an OnionHeaven hub.

        Runs before the body is parsed, so nothing an outsider sends is
        stored or acted on. The body is still drained (within the usual
        size limit): closing a socket with unread data can turn into a
        reset that loses the 403, and keep-alive needs it consumed.
        """
        if _accepts_registrations():
            return False
        if self._read_body() is None:
            return True  # answered already
        self._send_json(403, {"error": NOT_A_HUB_ERROR})
        return True

    def _body_length(self, max_size):
        """The request's body length, or None with the answer sent.

        Content-Length must be plain digits (a ``-1`` once read to EOF,
        #461); chunked bodies are refused. Missing means no body. Nothing
        is read on a refusal and the connection is closed.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send_json(411, {"error": "Content-Length required"})
            return None
        values = self.headers.get_all("Content-Length") or []
        if not values:
            return 0
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,12}", values[0].strip()):
            self.close_connection = True
            self._send_json(400, {"error": "Invalid Content-Length"})
            return None
        length = int(values[0])
        if length > max_size:
            self.close_connection = True
            self._send_json(413, {"error": "Request body too large"})
            return None
        return length

    def _read_body(self):
        """The body do_POST admitted, read once under a deadline; None, with
        the answer sent, when it does not arrive in time."""
        if self._body is not None:
            return self._body
        length = self._body_len
        deadline = time.monotonic() + BODY_DEADLINE_FLOOR + length / BODY_MIN_RATE
        large = length > MAX_REQUEST_BODY
        if large and not _large_bodies.acquire(blocking=False):
            self.close_connection = True
            self._send_json(503, {"error": "Busy, try again later"})
            return None
        chunks = []
        try:
            left = length
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    raise ConnectionError("body cut short")
                chunks.append(chunk)
                left -= len(chunk)
        except (TimeoutError, OSError):
            self.close_connection = True
            try:
                self._send_json(408, {"error": "Request body not received in time"})
            except OSError:
                pass
            return None
        finally:
            if large:
                _large_bodies.release()
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        self._body = b"".join(chunks)
        return self._body

    def _read_json(self, max_size=None):
        # The length was checked against this path's limit in do_POST.
        body = self._read_body()
        if body is None:
            return False  # answered already — caller must check
        if not body:
            return None
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    # -- GET dispatch -------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status":
            self._handle_status()
        elif path.startswith("/status/"):
            addr = path[len("/status/"):]
            self._handle_status_detail(addr)
        elif path == "/api/name/suggest":
            self._handle_name_suggest()
        elif path == "/api/name/check":
            self._handle_name_check()
        elif path.startswith("/api/name/lookup/"):
            name = path[len("/api/name/lookup/"):]
            self._handle_name_lookup(name)
        else:
            self._send_json(404, {"error": "Not found"})

    # -- GET /status --------------------------------------------------------

    def _handle_status(self):
        try:
            conn = db_connect()
            db_ensure_schema(conn)
            total = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE unregistered_at IS NULL"
            ).fetchone()[0]
            online = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online'"
            ).fetchone()[0]
            taken_over = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='taken-over'"
            ).fetchone()[0]
            # The unregistered rows the hub still keeps, not every one
            # there ever was: unregister_entry prunes them (issue #492).
            unregistered = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE unregistered_at IS NOT NULL"
            ).fetchone()[0]
            # Entries with a recent heartbeat
            heartbeat_healthy = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online' AND last_healthy IS NOT NULL"
            ).fetchone()[0]
            # Entries where WordPress reported unhealthy in last heartbeat
            wp_unhealthy = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online' AND wordpress_healthy = 0"
            ).fetchone()[0]
            # OnionHeaven peer count (protected from takeover)
            onionheaven_peers = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE is_onionheaven = 1"
            ).fetchone()[0]
            # Takeover container counts and queue stats
            try:
                tc_rows = conn.execute(
                    "SELECT container_name, bootstrapped, assigned_count FROM takeover_containers"
                ).fetchall()
                takeover_containers = len(tc_rows)
            except Exception:
                tc_rows = []
                takeover_containers = 0
            conn.close()

            # Aggregate queue stats. Counts only: the per-worker list
            # (container names, each one's queue) went out to anyone
            # until issue #469, and nothing consumed it.
            total_queued = 0
            total_in_flight = 0
            total_active = 0
            total_failed = 0
            for tc in tc_rows:
                if tc["bootstrapped"]:
                    try:
                        from onionheaven_common import get_queue_status
                        qs = get_queue_status(tc["container_name"])
                        if qs:
                            total_queued += qs.get("queued", 0)
                            total_in_flight += qs.get("in_flight", 0)
                            total_active += qs.get("active", 0)
                            total_failed += qs.get("failed", 0)
                    except Exception:
                        pass

            self._send_json(200, {
                "version": SERVER_VERSION,
                "accepting_registrations": _accepts_registrations(),
                "total": total,
                "online": online,
                "taken_over": taken_over,
                "unregistered": unregistered,
                "heartbeat_healthy": heartbeat_healthy,
                "wordpress_unhealthy": wp_unhealthy,
                "onionheaven_peers": onionheaven_peers,
                "takeover_containers": takeover_containers,
                "takeover_queued": total_queued,
                "takeover_in_flight": total_in_flight,
                "takeover_active": total_active,
                "takeover_failed": total_failed,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # -- GET /status/<address> ------------------------------------------------
    #
    # Issue #469: the per-address detail (the healthcheck address, the
    # version, every online/offline/takeover time, the audit, the address
    # log with its "/offline received" lines) went to anyone over Tor.
    # Polled, that was a minute-resolution record of when each publisher's
    # computer slept and woke, and it tied every content address to its
    # healthcheck onion both ways. The plain GET now says only whether a
    # content address is registered here and its status word; the detail
    # answers a POST /status/<address> signed by the address's own key
    # (STATUS_ENDPOINT, replay-marked like /offline). A healthcheck
    # address is looked up by neither: it names nothing on its own.

    # The registry's status words, most alive first: the coarse answer
    # for an address with several rows is the first of these any live
    # row has.
    COARSE_STATUS_ORDER = ("online", "taken-over", "unregistered")

    def _handle_status_detail(self, address):
        """The coarse, unauthenticated answer."""
        if not ONION_RE.fullmatch(address):
            self._send_json(400, {"error": "Invalid .onion address format"})
            return
        try:
            conn = db_connect()
            try:
                db_ensure_schema(conn)
                rows = conn.execute(
                    "SELECT status, unregistered_at FROM registry WHERE content_address = ?",
                    (address,)).fetchall()
            finally:
                conn.close()
            if not rows:
                self._send_json(404, {"error": "No entries found for this address"})
                return
            live = [r["status"] for r in rows if r["unregistered_at"] is None]
            word = "unregistered"
            for candidate in self.COARSE_STATUS_ORDER:
                if candidate in live:
                    word = candidate
                    break
            self._send_json(200, {
                "address": address,
                "registered": bool(live),
                "status": word,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # -- POST /status/<address> (signed) ---------------------------------------

    def _handle_status_signed(self, address):
        """The full per-address answer, for the address's own key: the
        body is signed like /offline's, with STATUS_ENDPOINT, and its
        content_address is the address in the path."""
        if not ONION_RE.fullmatch(address):
            self._send_json(400, {"error": "Invalid .onion address format"})
            return
        if self._refuse_unless_hub():
            return
        data = self._read_json()
        if data is False:
            return  # answered already
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        if data.get("content_address", "") != address:
            self._send_json(400, {"error": "content_address must be the address in the path"})
            return
        healthcheck_address = data.get("healthcheck_address", "")
        if healthcheck_address and not ONION_RE.fullmatch(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return
        ok, status, err = _verify_signature(self, data, STATUS_ENDPOINT, registered_only=True)
        if not ok:
            self._send_json(status, {"error": err})
            return
        try:
            self._send_json(200, self._status_detail(address))
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _status_detail(self, address):
        """Every row of a content address, with the address log."""
        conn = db_connect()
        try:
            db_ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? ORDER BY registered_at",
                (address,)
            ).fetchall()
            now = datetime.now(timezone.utc)
            entries = []
            for row in rows:
                entry = {
                    "content_address": row["content_address"],
                    "healthcheck_address": row["healthcheck_address"],
                    "status": row["status"],
                    "registered_at": row["registered_at"],
                    "unregistered_at": row["unregistered_at"],
                    "unregistered_reason": row["unregistered_reason"],
                    "version": row["version"],
                    "last_checked": row["last_checked"],
                    "last_healthy": row["last_healthy"],
                    "last_released": row["last_released"],
                    "last_taken_over": row["last_taken_over"],
                    "last_redirect": row["last_redirect"],
                    "wordpress_healthy": row["wordpress_healthy"],
                    "audit_result": row["audit_result"],
                    "audit_pending": row["audit_pending"],
                    "is_onionheaven": bool(row["is_onionheaven"]),
                }

                # serving_status: registered, queued-to-activate, activating, active, failed
                from onionheaven_common import get_addr_serving_status
                entry["serving_status"] = get_addr_serving_status(conn, row["content_address"])

                # Add computed debugging fields
                entry["seconds_since_last_checked"] = self._seconds_since(row["last_checked"], now)
                entry["seconds_since_last_healthy"] = self._seconds_since(row["last_healthy"], now)
                entry["seconds_since_last_taken_over"] = self._seconds_since(row["last_taken_over"], now)
                entry["seconds_since_last_released"] = self._seconds_since(row["last_released"], now)

                # Would the heartbeat monitor take over right now?
                # Conditions: status == 'online', last_healthy is stale (> PROPAGATION_DELAY)
                stale = True
                if row["last_healthy"]:
                    age = self._seconds_since(row["last_healthy"], now)
                    if age is not None:
                        stale = age > PROPAGATION_DELAY
                entry["last_healthy_stale"] = stale if row["status"] == "online" else None
                entry["propagation_delay_seconds"] = PROPAGATION_DELAY

                entries.append(entry)
        finally:
            conn.close()

        from onionheaven_common import get_addr_logs
        return {
            "lookup_type": "content_address",
            "address": address,
            "entries": entries,
            "count": len(entries),
            "recent_logs": get_addr_logs(address),
            "server_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    @staticmethod
    def _seconds_since(ts_str, now):
        """Return seconds elapsed since a timestamp string, or None."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return round((now - ts).total_seconds())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _ts_gte(a, b):
        """Return True if timestamp a >= b, None if either is missing."""
        if not a or not b:
            return None
        try:
            ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
            tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
            return ta >= tb
        except (ValueError, TypeError):
            return None

    # -- POST dispatch ------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?")[0]
        handlers = {
            "/unregister": self._handle_unregister,
            "/online": self._handle_online,
            "/offline": self._handle_offline,
            "/reset-onionheaven": self._handle_reset_onionheaven,
            "/logs/manifest": self._handle_logs_manifest,
            "/logs/upload": self._handle_logs_upload,
            "/api/name/register": self._handle_name_register,
            "/api/name/release": self._handle_name_release,
            "/api/name/register-local": self._handle_name_register_local,
            "/api/name/release-local": self._handle_name_release_local,
        }
        handler = handlers.get(path)
        if handler is None and path.startswith("/status/"):
            # The signed per-address status query (issue #469).
            address = path[len("/status/"):]
            handler = lambda: self._handle_status_signed(address)  # noqa: E731
        if handler is None:
            self.close_connection = True  # the body is never read
            self._send_json(404, {"error": "Not found"})
            return
        # Every POST's length is checked here, before any handler (#461).
        self._body = None
        self._body_len = self._body_length(
            MAX_LOG_UPLOAD_BODY if path == "/logs/upload" else MAX_REQUEST_BODY)
        if self._body_len is None:
            return
        try:
            handler()
        except VerificationUnavailable as e:
            # Every handler verifies before it changes anything, so nothing
            # was done. 503, not 403: the request may well be genuine.
            _note_verification_unavailable(e)
            self._send_json(503, {"error": "signature verification is "
                                           "unavailable on this server"})
        except Exception as e:
            self.log_message("ERROR in %s: %s", path, e)
            self._send_json(500, {"error": str(e)})
        finally:
            if self._body_len and self._body is None:
                # A handler that answered without reading the body: the
                # unread bytes must not be taken for the next request.
                self.close_connection = True

    # -- Name registry -----------------------------------------------------
    #
    # All /api/name/* endpoints are OnionHome-only. Non-OnionHome instances
    # return 404 so clients don't try to talk to them.
    #
    # Signature verification for register/release is unconditional — the
    # signature IS the ownership proof. Source IP is irrelevant (OnionHome
    # sees every .onion-origin request as coming from the Docker network).

    def _name_query(self):
        return parse_qs(urlparse(self.path).query)

    def _name_registry_ready(self):
        """False, with the answer sent, unless this is OnionHome and its
        registry is in place on its own volume. Never serve or change names
        from a registry that started empty beside the real one: every name
        would be free to claim (onionnames.registry_in_place)."""
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
            return False
        if not onionnames.registry_in_place():
            self._send_json(503, {"error": "Name registry unavailable"})
            return False
        return True

    def _handle_name_suggest(self):
        if not self._name_registry_ready():
            return
        params = self._name_query()
        lang = (params.get("lang", ["en"])[0] or "en").lower()
        conn = onionnames.db_connect()
        try:
            onionnames.db_init(conn)
            name = onionnames.suggest_name(conn, lang=lang)
        finally:
            conn.close()
        if not name:
            self._send_json(503, {"error": "Unable to generate suggestion"})
            return
        self._send_json(200, {"onionname": name, "lang": lang})

    def _handle_name_check(self):
        if not self._name_registry_ready():
            return
        params = self._name_query()
        name = params.get("name", [""])[0]
        if not name:
            self._send_json(400, {"error": "Missing required parameter: name"})
            return
        conn = onionnames.db_connect()
        try:
            onionnames.db_init(conn)
            result = onionnames.check_name(conn, name)
            if not result["available"] and result["reason"] in ("taken", "reserved"):
                result["suggestions"] = onionnames.generate_alternatives(conn, name)
        finally:
            conn.close()
        self._send_json(200, result)

    def _handle_name_lookup(self, name):
        if not self._name_registry_ready():
            return
        if not name:
            self._send_json(400, {"error": "Missing name"})
            return
        conn = onionnames.db_connect()
        try:
            onionnames.db_init(conn)
            entry = onionnames.lookup_name(conn, name)
        finally:
            conn.close()
        if entry is None:
            self._send_json(404, {"error": "Not found"})
            return
        self._send_json(200, entry)

    def _handle_name_register(self):
        if not self._name_registry_ready():
            return
        data = self._read_json()
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        onionaddress = data.get("onionaddress", "")
        name = data.get("onionname", "")
        timestamp = data.get("timestamp", "")
        signature = data.get("signature", "")

        if not onionaddress or not ONION_RE.fullmatch(onionaddress):
            self._send_json(400, {"error": "Invalid or missing onionaddress"})
            return
        if not name:
            self._send_json(400, {"error": "Missing onionname"})
            return

        ok, err = verify_name_payload(
            onionaddress, "register", name, timestamp, signature
        )
        if not ok:
            self._send_json(403, {"error": err})
            return

        conn = onionnames.db_connect()
        try:
            onionnames.db_init(conn)
            # If the same address has already registered this exact name, the
            # second call is a no-op success (idempotent retry).
            existing = onionnames.lookup_name(conn, name)
            if (existing and existing["onionaddress"] == onionaddress
                    and existing["onionname"].lower() == name.lower()):
                self._send_json(200, {
                    "onionname": existing["onionname"],
                    "onionaddress": onionaddress,
                    "url": existing["url"],
                    "already_registered": True,
                })
                return
            ok, reason, alternatives = onionnames.register_name(
                conn, name, onionaddress
            )
        finally:
            conn.close()

        if not ok:
            status = 400
            if reason in ("taken", "reserved"):
                status = 409
            body = {"error": reason}
            if alternatives:
                body["suggestions"] = alternatives
            self._send_json(status, body)
            return

        self._send_json(201, {
            "onionname": name,
            "onionaddress": onionaddress,
            "url": f"http://{onionaddress}/{name}",
        })

    def _handle_name_release(self):
        if not self._name_registry_ready():
            return
        data = self._read_json()
        if data is False:
            return
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        onionaddress = data.get("onionaddress", "")
        name = data.get("onionname", "")
        timestamp = data.get("timestamp", "")
        signature = data.get("signature", "")

        if not onionaddress or not ONION_RE.fullmatch(onionaddress):
            self._send_json(400, {"error": "Invalid or missing onionaddress"})
            return
        if not name:
            self._send_json(400, {"error": "Missing onionname"})
            return

        ok, err = verify_name_payload(
            onionaddress, "release", name, timestamp, signature
        )
        if not ok:
            self._send_json(403, {"error": err})
            return

        conn = onionnames.db_connect()
        try:
            onionnames.db_init(conn)
            ok, reason = onionnames.release_name(conn, name, onionaddress)
        finally:
            conn.close()
        if not ok:
            status = 404 if reason == "not_found" else 403
            self._send_json(status, {"error": reason})
            return
        self._send_json(200, {"released": True, "onionname": name})

    # -- Docker-network-only endpoints ------------------------------------
    #
    # /api/name/{register,release}-local are called by the WordPress
    # mu-plugin on user create/delete; /reset-onionheaven by the
    # `onionpress reset` CLI via docker exec until onionpress-tor lost the
    # Docker socket (it now answers 503). None of them are meant to be
    # reachable from outside the Docker network; we enforce that by
    # rejecting any source IP that isn't on the Docker bridge range.
    # Tor-proxied traffic arrives at 127.0.0.1 (Tor forwards onion service
    # connections locally), so denying loopback blocks Tor-origin callers.

    def _docker_network_src_ok(self):
        src = self.client_address[0]
        if src.startswith("172.") or src.startswith("10."):
            return True
        return False

    def _passthrough(self, status, body):
        try:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def _handle_name_local(self, endpoint):
        """Shared implementation for /api/name/{register,release}-local."""
        if not self._docker_network_src_ok():
            self._send_json(403, {"error": "local-only endpoint"})
            return

        data = self._read_json()
        if data is False:
            return
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        name = (data.get("onionname") or "").strip()
        if not name:
            self._send_json(400, {"error": "Missing onionname"})
            return

        own_address = _get_own_address()
        if not own_address:
            self._send_json(503, {"error": "local onion address not ready"})
            return

        if _is_onionhome():
            if not self._name_registry_ready():
                return
            if endpoint == "register":
                status, body = onionnames.local_register(name, own_address)
            else:
                status, body = onionnames.local_release(name, own_address)
            self._passthrough(status, body)
            return

        # Remote path — sign with our HS key and forward to OnionHome.
        status, body = onionnames.sign_and_forward(
            endpoint, name, own_address, _ONIONHOME_ADDRESS,
        )
        self._passthrough(status, body)

    def _handle_name_register_local(self):
        self._handle_name_local("register")

    def _handle_name_release_local(self):
        self._handle_name_local("release")

    # -- POST /unregister ---------------------------------------------------

    def _handle_unregister(self):
        if self._refuse_unless_hub():
            return
        data = self._read_json()
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.fullmatch(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return

        # Optional: target a specific healthcheck row
        healthcheck_address = data.get("healthcheck_address", "")
        if healthcheck_address and not ONION_RE.fullmatch(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Verify ed25519 signature; an address with no row is 404 before
        # anything is written for it (issue #467).
        ok, status, err = _verify_signature(self, data, "unregister", registered_only=True)
        if not ok:
            self._send_json(status, {"error": err})
            return

        conn = db_connect()
        db_ensure_schema(conn)

        # Find matching rows
        if healthcheck_address:
            rows = conn.execute(
                "SELECT * FROM registry "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (content_address, healthcheck_address)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM registry WHERE content_address = ?",
                (content_address,)
            ).fetchall()

        if not rows:
            conn.close()
            self._send_json(404, {"error": "Entry not found"})
            return

        # Unregister all matching rows (release + mark unregistered + delete keys)
        from onionheaven_common import unregister_entry
        for row in rows:
            try:
                unregister_entry(conn, row["content_address"],
                                 row["healthcheck_address"],
                                 reason="explicit-unregister")
            except Exception as e:
                log(f"ERROR: unregister_entry failed for "
                    f"{row['content_address']}: {e}")
        conn.close()

        try:
            self._send_json(200, {
                "unregistered": True,
                "content_address": content_address,
                "deleted_count": len(rows),
            })
        except BrokenPipeError:
            pass  # client disconnected — release already completed

    # -- POST /online (also handles /register) --------------------------------

    def _handle_online(self):
        """Unified handler for /online and /register.

        Always upserts the registry entry. If arti_key_pem is provided,
        validates and stores it (needed for takeover). Heartbeats can omit
        the key — only the first call needs it. Hub only.
        """
        if self._refuse_unless_hub():
            return
        data = self._read_json()
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")

        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.fullmatch(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.fullmatch(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Reject self-registration — an OnionHeaven server must never take over its own address
        own_addr = _get_own_address()
        if own_addr and content_address == own_addr:
            log(f"ERROR: Rejected self-registration — {content_address} is this server's own address")
            self._send_json(403, {"error": "Cannot register with yourself"})
            return

        # Verify ed25519 signature — accept both "online" and "register" actions
        # for backwards compatibility with older clients
        endpoint = self.path.split("?")[0].lstrip("/")
        ok, status, err = _verify_signature(self, data, endpoint)
        if not ok:
            self._send_json(status, {"error": err})
            return

        # Room under the caps (issue #391), once the signature is good and
        # before anything is stored: a new pair for an address at its row
        # cap, or a new address on a full hub, is refused here; an
        # existing pair's heartbeat never is. (After the signature: the
        # registry is opened for no unverified request.)
        try:
            room_conn = db_connect()
            try:
                db_ensure_schema(room_conn)
                status, err = registration_room(room_conn, content_address, healthcheck_address)
            finally:
                room_conn.close()
        except Exception as e:
            log(f"REJECT-CAPS /online: {content_address}: {e}")
            self._send_json(503, {"error": "Could not check the registry's room"})
            return
        if status:
            log(f"REJECT-CAPS /online: {content_address}: {err}")
            self._send_json(status, {"error": err})
            return

        # arti_key_pem is required on every /online request — proves the sender
        # has the private key and allows takeover if the site goes offline.
        arti_key_stored = False
        has_key = data.get("arti_key_pem")
        if not has_key:
            self._send_json(400, {"error": "Missing required field: arti_key_pem"})
            return
        if has_key:
            try:
                arti_pem = base64.b64decode(data["arti_key_pem"])
            except Exception:
                self._send_json(400, {"error": "Invalid arti_key_pem base64"})
                return
            if not arti_pem.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                self._send_json(400, {"error": "Invalid arti_key_pem format"})
                return
            if not validate_arti_pem(arti_pem):
                self._send_json(400, {"error": "Corrupted arti_key_pem: key data failed integrity check"})
                return

            # Verify the key derives to content_address. Without this, a
            # buggy client can overwrite a stored key with bytes that
            # decode to a different address. The key
            # is unrecoverable once clobbered — only the authoritative
            # OnionPress instance for that address holds the original.
            public_key = extract_public_key_from_arti_pem(arti_pem)
            if public_key is None:
                self._send_json(400, {"error": "arti_key_pem: cannot extract public key"})
                return
            derived = derive_onion_address(public_key)
            if derived != content_address:
                # Loud alert so we notice the next outbreak fast.
                log(f"REJECT-MISMATCH /online: content_address={content_address} "
                    f"key_derives_to={derived} client_ip={self.client_address[0]}")
                self._send_json(400, {
                    "error": "arti_key_pem does not derive to content_address",
                    "derived": derived,
                })
                return

            # The public half derives to content_address; the private half
            # must be that key's too, or it would replace a working key.
            problem = private_key_problem(arti_pem, public_key)
            if problem:
                log(f"REJECT-KEYPAIR /online: content_address={content_address} "
                    f"{problem} client_ip={self.client_address[0]}")
                self._send_json(400, {
                    "error": "arti_key_pem: the private key does not belong "
                             "to content_address",
                    "reason": problem,
                })
                return

            # Store plaintext PEM key: in one step (write_key_file), and
            # only when it changed. A heartbeat used to rewrite it in place,
            # and a crash or full disk at that moment left an empty key.
            keys_dir = os.path.join(KEYS_DIR, content_address)
            os.makedirs(keys_dir, mode=0o700, exist_ok=True)

            pem_path = os.path.join(keys_dir, "ks_hs_id.ed25519_expanded_private")
            write_key_file(pem_path, arti_pem)

            # Write hostname file
            hostname_path = os.path.join(keys_dir, "hostname")
            write_key_file(hostname_path, (content_address + "\n").encode("ascii"))

            # Remove old encrypted files if present (migration cleanup)
            for old_file in ("ks_hs_id.ed25519_expanded_private.enc",
                             "hs_ed25519_secret_key.enc", "hs_ed25519_public_key.enc",
                             "hs_ed25519_secret_key", "hs_ed25519_public_key"):
                old_path = os.path.join(keys_dir, old_file)
                try:
                    os.unlink(old_path)
                except FileNotFoundError:
                    pass

            arti_key_stored = True

        conn = db_connect()
        db_ensure_schema(conn)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Accept optional fields
        wordpress_healthy = data.get("wordpress_healthy")
        # A short printable string (issue #467): it is stored per row and
        # echoed in the signed status answer, and the body is 1 MB.
        version = short_text(data.get("version"), default="unknown") or "unknown"
        is_oh = 1 if _signed_peer_claim(data) else 0

        created = False

        if healthcheck_address:
            # Check if entry exists
            existing = conn.execute(
                "SELECT status FROM registry WHERE content_address = ? AND healthcheck_address = ?",
                (content_address, healthcheck_address)
            ).fetchone()

            # Upsert: create if new, update if exists.
            # For new entries, status starts as 'online'. For existing entries:
            #   - status='online'      → keep 'online'
            #   - status='taken-over'  → keep 'taken-over'; release_function
            #     (called below) owns the transition back to 'online' so it
            #     can also DEL_ONION on the worker and decrement assigned_count.
            #   - status='unregistered' → flip to 'online'. unregister_entry
            #     already ran release_function before marking unregistered,
            #     so no worker is holding the address; nothing to DEL_ONION.
            #     Without this transition, a previously-unregistered address
            #     could never be re-activated by a heartbeat — /online would
            #     clear unregistered_at/reason but leave status='unregistered'
            #     forever.
            wp_healthy_val = (1 if wordpress_healthy else 0) if wordpress_healthy is not None else None
            conn.execute("""INSERT INTO registry
                (content_address, healthcheck_address, registered_at, version,
                 last_healthy, status, is_onionheaven, wordpress_healthy, wordpress_checked_at)
                VALUES (?, ?, ?, ?, ?, 'online', ?, ?, ?)
                ON CONFLICT(content_address, healthcheck_address) DO UPDATE SET
                    status = CASE WHEN registry.status = 'unregistered'
                                  THEN 'online'
                                  ELSE registry.status END,
                    last_healthy = excluded.last_healthy,
                    version = CASE WHEN excluded.version != 'unknown' THEN excluded.version ELSE registry.version END,
                    is_onionheaven = excluded.is_onionheaven,
                    wordpress_healthy = COALESCE(excluded.wordpress_healthy, registry.wordpress_healthy),
                    wordpress_checked_at = COALESCE(excluded.wordpress_checked_at, registry.wordpress_checked_at),
                    unregistered_at = NULL,
                    unregistered_reason = NULL,
                    audit_result = NULL,
                    audit_at = NULL,
                    audit_pending = NULL""",
                (content_address, healthcheck_address, now, version, now, is_oh,
                 wp_healthy_val, now if wp_healthy_val is not None else None))
            db_commit_with_retry(conn)
            # release_function checks if taken-over and handles status change + DEL_ONION
            from onionheaven_common import addr_log
            status_before = existing[0] if existing else None
            addr_log(content_address, f"/online received from {content_address} (status={status_before}, version={version})")
            release_function(conn, content_address, healthcheck_address)
            if not existing:
                created = True
                addr_log(content_address, f"New registry entry for {content_address} / {healthcheck_address}")
        else:
            # No healthcheck_address — update all rows for this content_address
            existing = conn.execute(
                "SELECT healthcheck_address, status FROM registry WHERE content_address = ?",
                (content_address,)
            ).fetchall()
            if not existing:
                conn.close()
                self._send_json(400, {"error": "healthcheck_address required for new entries"})
                return
            # Update last_healthy but don't change status — release_function owns that
            conn.execute(
                "UPDATE registry SET last_healthy = ?, "
                "is_onionheaven = ?, "
                "unregistered_at = NULL, unregistered_reason = NULL, "
                "audit_result = NULL, audit_at = NULL, audit_pending = NULL "
                "WHERE content_address = ?",
                (now, is_oh, content_address)
            )
            db_commit_with_retry(conn)
            # release_function handles taken-over → online transition
            for row in existing:
                release_function(conn, content_address, row["healthcheck_address"])

        conn.close()

        # No activation flag here: a registration comes from outside, and
        # the flag follows hub admission only (_sync_activate_marker).

        self._send_json(200, {
            "online": True,
            "registered": True,  # backwards compat for old clients checking this
            "content_address": content_address,
            "created": created,
            "arti_key_stored": arti_key_stored,
        })

    # -- POST /offline ------------------------------------------------------

    def _handle_offline(self):
        if self._refuse_unless_hub():
            return
        data = self._read_json()
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")

        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.fullmatch(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.fullmatch(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Verify ed25519 signature; an address with no row is 404 before
        # a replay mark or an address log line is written for it (#467).
        ok, status, err = _verify_signature(self, data, "offline", registered_only=True)
        if not ok:
            self._send_json(status, {"error": err})
            return

        # Telemetry: log every authenticated /offline. Paired with the
        # existing /online addr_log line, this lets us measure the
        # suspend-race miss rate (an /online without a preceding /offline
        # from the same address ⇒ the client lost the suspend window
        # before its /offline POST could complete).
        from onionheaven_common import addr_log
        version = short_text(data.get("version"))
        addr_log(content_address, f"/offline received from {content_address} (version={version})")

        conn = db_connect()
        db_ensure_schema(conn)

        # Takeover via the shared function (force=True since we know it's offline)
        if healthcheck_address:
            takeover_function(conn, content_address, healthcheck_address, force=True)
        else:
            rows = conn.execute(
                "SELECT healthcheck_address FROM registry WHERE content_address = ?",
                (content_address,)
            ).fetchall()
            for row in rows:
                takeover_function(conn, content_address, row["healthcheck_address"], force=True)

        conn.close()

        self._send_json(200, {
            "offline": True,
            "content_address": content_address,
        })

    # -- POST /reset-onionheaven ---------------------------------------------

    def _handle_reset_onionheaven(self):
        """Clean stress tests + refresh all takeover workers with current code.

        Removes stress test entries, tears down old workers, spawns fresh ones
        from the current image, and migrates real takeovers. It drives
        Docker (through the farm controller), so it is restricted to
        Docker-bridge sources (same guard as the *-local name endpoints) —
        before #126 it had no gate at all and was callable by anyone over
        Tor. Serialized by _RESET_LOCK: a second call while one is running
        gets 409. It spawns takeover workers, so it is hub-only as well.

        This container cannot reach the farm controller (it never had the
        Docker socket either), so the reset runs in the onionheaven
        container (`onionpress reset-onionheaven`), and here it answers 503
        rather than half-run.
        """
        if not self._docker_network_src_ok():
            self._send_json(403, {"error": "local-only endpoint"})
            return
        if self._refuse_unless_hub():
            return
        from onionheaven_common import farm_available
        if not farm_available():
            self._send_json(503, {
                "error": "no farm controller in this container; run "
                         "reset_onionheaven in the onionheaven container "
                         "(onionpress reset-onionheaven)"})
            return
        if not _RESET_LOCK.acquire(blocking=False):
            self._send_json(409, {"error": "reset already in progress"})
            return
        try:
            from onionheaven_common import reset_onionheaven
            stats = reset_onionheaven()
            self._send_json(200, {"reset": True, **stats})
        finally:
            _RESET_LOCK.release()


    # -- POST /logs/manifest (OnionHome only) ---------------------------------

    def _handle_logs_manifest(self):
        if not _is_onionhome():
            self._send_json(403, {"error": "Not an OnionHome instance"})
            return

        data = self._read_json()
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")
        if not _v3(content_address):
            self._send_json(400, {"error": "Invalid content_address"})
            return
        # Required (issue #468): with "" the pair's folder was the
        # content address's own, where the per-instance quota never looked.
        if not _v3(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address"})
            return

        # Verify signature
        ok, err = verify_payload(
            content_address, "logs",
            healthcheck_address, data.get("timestamp", ""),
            data.get("signature", ""),
        )
        if not ok:
            self._send_json(403, {"error": f"Signature verification failed: {err}"})
            return

        files = data.get("files", [])
        if not isinstance(files, list) or len(files) > MAX_MANIFEST_FILES:
            self._send_json(400, {"error": "Invalid files"})
            return
        if not files:
            self._send_json(200, {"wanted": []})
            return

        # Check disk usage
        if analytics_disk_full():
            self.log_message("Analytics: disk >85%% full, rejecting manifest")
            self._send_json(200, {"wanted": []})
            return

        # Room under the caps (issue #391) before a new pair's directory.
        status, err = analytics_room(content_address, healthcheck_address)
        if status:
            self.log_message("Analytics: refusing %s: %s", content_address[:12], err)
            self._send_json(status, {"error": err})
            return

        # Instance metadata (free text cut short, #467) with dated
        # filename, within the pair's quota (#468).
        version = short_text(data.get("version"))
        meta = {
            "content_address": content_address,
            "healthcheck_address": healthcheck_address,
            "version": version,
            "tor_impl": short_text(data.get("tor_impl")),
            "os_version": short_text(data.get("os_version")),
            "timestamp": short_text(data.get("timestamp")),
        }
        meta_text = json.dumps(meta, indent=2)
        site_dir = os.path.join(ANALYTICS_DIR, content_address, healthcheck_address)
        status, err = analytics_pair_room(site_dir, len(meta_text))
        if status:
            self.log_message("Analytics: refusing %s: %s", content_address[:12], err)
            self._send_json(status, {"error": err})
            return
        os.makedirs(site_dir, exist_ok=True)
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            seq = 1
            while True:
                meta_name = f"meta-{today}-{seq:04d}.json"
                meta_path = os.path.join(site_dir, meta_name)
                if not os.path.exists(meta_path):
                    break
                seq += 1
            with open(meta_path, "w") as f:
                f.write(meta_text)
        except OSError:
            pass

        # Determine which files we need (new, or grown since last upload).
        # Active (uncompressed) logs grow over time, so we re-request when
        # the offered size exceeds what we have. Once the client finishes
        # rolling a day, it offers the .gz final; at that point the .log
        # partial is superseded and we stop re-requesting it (the partial
        # itself is deleted during the subsequent .log.gz upload).
        wanted = []
        rejected = []  # names dropped (unsafe) — recorded in the audit trail
        superseded = []  # .log offered when .log.gz already stored
        offered = []  # every name, as the audit trail and the log may hold it
        for f in files:
            name = f.get("name", "") if isinstance(f, dict) else ""
            shown = short_text(name, 200) or "<empty>"
            offered.append(shown)
            if not isinstance(name, str) or not ANALYTICS_LOG_NAME_RE.fullmatch(name):
                rejected.append(shown)
                self.log_message(
                    "Analytics: rejected %s from %s (unsafe name)",
                    shown, content_address,
                )
                continue
            local_path = os.path.join(site_dir, name)
            if name.endswith(".log") and os.path.exists(local_path + ".gz"):
                superseded.append(name)
                continue
            remote_size = f.get("size", 0)
            if isinstance(remote_size, bool) or not isinstance(remote_size, int):
                remote_size = 0
            if not os.path.exists(local_path):
                wanted.append(name)
                continue
            try:
                local_size = os.path.getsize(local_path)
            except OSError:
                wanted.append(name)
                continue
            if remote_size > local_size:
                wanted.append(name)

        _append_audit(site_dir, {
            "event": "manifest",
            "offered": offered,
            "wanted": wanted,
            "rejected": rejected,
            "superseded": superseded,
            "version": version,
        })

        self._send_json(200, {"wanted": wanted})

    # -- POST /logs/upload (OnionHome only) ------------------------------------

    def _handle_logs_upload(self):
        if not _is_onionhome():
            self._send_json(403, {"error": "Not an OnionHome instance"})
            return

        data = self._read_json(max_size=MAX_LOG_UPLOAD_BODY)
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")
        file_name = data.get("file_name", "")

        if not _v3(content_address):
            self._send_json(400, {"error": "Invalid content_address"})
            return
        if not _v3(healthcheck_address):  # required, as on /logs/manifest (#468)
            self._send_json(400, {"error": "Invalid healthcheck_address"})
            return
        if not isinstance(file_name, str) or not ANALYTICS_LOG_NAME_RE.fullmatch(file_name):
            self._send_json(400, {"error": "Invalid file_name"})
            return

        # Verify signature
        ok, err = verify_payload(
            content_address, "logs",
            healthcheck_address, data.get("timestamp", ""),
            data.get("signature", ""),
        )
        if not ok:
            self._send_json(403, {"error": f"Signature verification failed: {err}"})
            return

        file_content_b64 = data.get("file_content", "")
        try:
            file_bytes = base64.b64decode(file_content_b64)
        except Exception:
            self._send_json(400, {"error": "Invalid file_content base64"})
            return

        # Check / manage disk space
        if analytics_disk_full():
            self._analytics_cleanup()
            if analytics_disk_full():  # re-check after cleanup
                self._send_json(507, {"error": "Disk full"})
                return

        # Room under the caps (issue #391) before a new pair's directory,
        # and under the pair's quota (#468) before the write.
        status, err = analytics_room(content_address, healthcheck_address)
        if status:
            self.log_message("Analytics: refusing %s: %s", content_address[:12], err)
            self._send_json(status, {"error": err})
            return
        site_dir = os.path.join(ANALYTICS_DIR, content_address, healthcheck_address)
        status, err = analytics_pair_room(site_dir, len(file_bytes), replacing=file_name)
        if status:
            self.log_message("Analytics: refusing %s: %s", content_address[:12], err)
            self._send_json(status, {"error": err})
            return

        # Write the file
        os.makedirs(site_dir, exist_ok=True)
        dest = os.path.join(site_dir, file_name)
        try:
            with open(dest, "wb") as f:
                f.write(file_bytes)
            self.log_message("Analytics: stored %s from %s", file_name, content_address[:12])
        except OSError as e:
            self._send_json(500, {"error": f"Write failed: {e}"})
            return

        # Supersede the uncompressed partial once the compressed final arrives.
        if file_name.endswith(".log.gz"):
            partial = dest[:-3]  # strip .gz
            try:
                os.remove(partial)
            except FileNotFoundError:
                pass
            except OSError:
                pass

        _append_audit(site_dir, {
            "event": "upload",
            "file": file_name,
            "bytes": len(file_bytes),
        })

        self._send_json(200, {"stored": True, "file_name": file_name})

    @staticmethod
    def _analytics_cleanup():
        """Three-phase cleanup of ``ANALYTICS_DIR``.

        1. **Age expiry** — delete any file older than
           :data:`ANALYTICS_MAX_AGE_DAYS`, unconditionally. Keeps long-
           retention growth in check even when the disk is fine.
        2. **Per-instance quota** — for each ``<content>/<healthcheck>``
           site directory that exceeds
           :data:`ANALYTICS_PER_INSTANCE_QUOTA`, delete its own oldest
           files until under quota. A single noisy instance can no
           longer starve its peers.
        3. **Disk-full fallback** — if the volume is still over the
           :data:`ANALYTICS_DISK_THRESHOLD` watermark, delete globally
           oldest files until under threshold.

        Meta/audit sidecar files are subject to the same rules as
        log files — we track cadence, not content.
        """
        if not os.path.isdir(ANALYTICS_DIR):
            return
        import time as _time

        now = _time.time()
        age_cutoff = now - ANALYTICS_MAX_AGE_DAYS * 86400

        # Phase 1: age expiry
        for root, _dirs, files in os.walk(ANALYTICS_DIR):
            for name in files:
                p = os.path.join(root, name)
                try:
                    if os.path.getmtime(p) < age_cutoff:
                        os.remove(p)
                except OSError:
                    continue

        # Phase 2: per-instance quota. Group remaining files by site
        # directory and trim oldest-first within each until under quota.
        # Files directly under a content address's folder (what an upload
        # with an empty healthcheck address left there before #468) are
        # held to the quota as a group of their own.
        try:
            content_dirs = os.listdir(ANALYTICS_DIR)
        except OSError:
            content_dirs = []
        for content in content_dirs:
            content_path = os.path.join(ANALYTICS_DIR, content)
            try:
                hc_dirs = os.listdir(content_path)
            except OSError:
                continue
            strays = []
            for hc in hc_dirs:
                site_dir = os.path.join(content_path, hc)
                if not os.path.isdir(site_dir):
                    strays.append(site_dir)
                    continue
                try:
                    paths = [os.path.join(site_dir, name) for name in os.listdir(site_dir)]
                except OSError:
                    continue
                OnionHeavenHandler._trim_to_quota(paths)
            OnionHeavenHandler._trim_to_quota(strays)

        # Phase 3: disk-full fallback. Only kicks in if the above two
        # phases didn't free enough space.
        if not analytics_disk_full():
            return
        all_files = []
        for root, _dirs, files in os.walk(ANALYTICS_DIR):
            for name in files:
                p = os.path.join(root, name)
                try:
                    all_files.append((os.path.getmtime(p), p))
                except OSError:
                    continue
        all_files.sort()  # oldest first
        for _mtime, path in all_files:
            try:
                if not analytics_disk_full():
                    break
                os.remove(path)
            except OSError:
                continue

    @staticmethod
    def _trim_to_quota(paths):
        """Remove the oldest of `paths` until they hold no more than
        ANALYTICS_PER_INSTANCE_QUOTA bytes together."""
        entries = []
        total = 0
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= ANALYTICS_PER_INSTANCE_QUOTA:
            return
        entries.sort()  # oldest first
        for _mtime, size, path in entries:
            if total <= ANALYTICS_PER_INSTANCE_QUOTA:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue


def _start_analytics_cleanup_thread():
    """Run :meth:`_analytics_cleanup` once a day in the background.

    Runs the first sweep ~60 s after boot so age-expired and over-quota
    files from a past outage get trimmed without waiting for an upload.
    Daemon thread: dies with the process.
    """
    def _loop():
        import time as _time
        _time.sleep(60)
        while True:
            try:
                OnionHeavenHandler._analytics_cleanup()
            except Exception:
                pass
            _time.sleep(ANALYTICS_CLEANUP_INTERVAL)
    t = threading.Thread(target=_loop, daemon=True, name="analytics-cleanup")
    t.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Ensure data directories exist
    os.makedirs(KEYS_DIR, exist_ok=True)
    if _is_onionhome():
        os.makedirs(ANALYTICS_DIR, exist_ok=True)
        os.makedirs(onionnames.DATA_DIR, exist_ok=True)

    # Initialize DB schema
    conn = db_connect()
    db_ensure_schema(conn)
    conn.close()

    # Initialize onionname registry DB and kick off reservation refresh.
    # Only OnionHome exposes the /api/name/* endpoints. The entrypoint has
    # moved the registry onto its own volume by now; if it is not in place
    # there, nothing opens it (the endpoints answer 503).
    if _is_onionhome():
        if not onionnames.registry_in_place():
            onionnames.log(
                f"ERROR: the name registry is not in place on its volume "
                f"({onionnames.DATA_DIR}); /api/name/* answers 503 until it "
                f"is (see this container's onionhome-registry-migrate lines)"
            )
        else:
            try:
                conn = onionnames.db_connect()
                try:
                    onionnames.db_init(conn)
                    # Synchronous refresh at boot so reservations are present
                    # before the first request. Failure is non-fatal — retry
                    # handled by the background thread.
                    count = onionnames.refresh_dynamic_reservations(conn)
                    if count is not None:
                        onionnames.log(
                            f"dynamic reservations refreshed on startup: {count}"
                        )
                finally:
                    conn.close()
            except Exception as e:
                onionnames.log(f"startup refresh failed: {e}")
            onionnames.start_refresh_thread()
        _start_analytics_cleanup_thread()

    # Shrink per-thread stack from the 8MB default to 256KB before any
    # request threads spawn. With HTTP/1.1 keep-alive each client now
    # holds a thread for the connection's lifetime (not just a single
    # request), so stack memory dominates at scale. 256KB × N clients
    # gives us comfortable headroom into the low thousands; the request
    # handlers are not stack-heavy (no deep recursion).
    import threading
    threading.stack_size(256 * 1024)

    threading.Thread(target=_sync_activate_marker, daemon=True,
                     name="activate-marker").start()

    # ThreadingHTTPServer: the register-local forward path can block up to
    # 30s waiting on Tor. Single-threaded serving would stall every other
    # endpoint (heartbeats, status) during that window. Existing handlers
    # are safe under concurrency (SQLite WAL, per-request connections).
    server = BoundedThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), OnionHeavenHandler)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] web-server: listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
