#!/usr/bin/env python3
"""
OnionPress Web Server

Lightweight HTTP server (Python stdlib only) that handles:
- OnionHeaven registration, unregistration, and lifecycle notifications
- OnionHome analytics log collection (auto-detected, no config needed)

Runs inside the tor container on port 8083, exposed through the onion service.

Endpoints:
  POST /online       — Heartbeat / register (upserts registry entry, optionally stores arti key)
  POST /unregister   — Release takeover (DEL_ONION + decrement worker), mark
                       row status='unregistered', delete arti key file.
                       Row is NOT deleted — a subsequent /online from the same
                       (content_address, healthcheck_address) transitions
                       status back to 'online' (re-registration).
  POST /offline      — Notify OnionHeaven that instance is going offline
  POST /reset-onionheaven — Clean stress tests + refresh workers with current code (internal only)
  POST /logs/manifest — (OnionHome only) Accept log file manifests for analytics sharing
  POST /logs/upload   — (OnionHome only) Accept log file uploads for analytics sharing
  GET  /status       — Public status summary (no auth)
  GET  /status/<addr> — Per-address detail (looks up by content or healthcheck address)
"""

MAX_REQUEST_BODY = 1_048_576  # 1 MB — reject larger POST bodies to prevent memory exhaustion

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from onion_auth import verify_payload, verify_name_payload
from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    takeover_function, release_function, flush_sighup_tor,
    KEYS_DIR, PROPAGATION_DELAY, ONIONHEAVEN_DATA_DIR,
)
import onionnames

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
_ONIONHOME_ADDRESS = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
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
_HOSTNAME_PATH = "/var/lib/tor/hidden_service/{}/hostname".format(
    os.environ.get("ONIONPRESS_BACKEND_NICKNAME", "wordpress")
)


def _get_own_address():
    """Return this server's own .onion address (cached after first read)."""
    global OWN_ONION_ADDRESS
    if OWN_ONION_ADDRESS is None:
        try:
            with open(_HOSTNAME_PATH) as f:
                addr = f.read().strip()
            if ONION_RE.match(addr):
                OWN_ONION_ADDRESS = addr
                log(f"Own onion address: {addr}")
        except OSError:
            pass  # file not written yet, or not readable (e.g. outside container)
    return OWN_ONION_ADDRESS


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

def is_local_request(handler):
    """Check if request is from localhost or Docker network (skip auth)."""
    if os.environ.get("ONIONHEAVEN_ENFORCE_AUTH") == "1":
        return False
    addr = handler.client_address[0]
    return (
        addr == "127.0.0.1"
        or addr == "::1"
        or addr.startswith("172.")
        or addr.startswith("10.")
    )


def _verify_signature(handler, data, endpoint):
    """Verify ed25519 signature on a request.

    Returns (ok, error_message). Skips verification for local requests.
    """
    if is_local_request(handler):
        return True, ""

    content_address = data.get("content_address", "")
    healthcheck_address = data.get("healthcheck_address", "")
    timestamp = data.get("timestamp", "")
    signature = data.get("signature", "")

    return verify_payload(
        content_address, endpoint, healthcheck_address,
        timestamp, signature
    )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

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

    def _read_json(self, max_size=None):
        if max_size is None:
            max_size = MAX_REQUEST_BODY
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        if length > max_size:
            self._send_json(413, {"error": "Request body too large"})
            return False  # distinguishes from None (no body) — caller must check
        body = self.rfile.read(length)
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

            # Per-worker details and aggregate queue stats
            total_queued = 0
            total_in_flight = 0
            total_active = 0
            total_failed = 0
            workers = []
            for tc in tc_rows:
                worker_info = {
                    "name": tc["container_name"],
                    "bootstrapped": bool(tc["bootstrapped"]),
                    "assigned_count": tc["assigned_count"],
                }
                if tc["bootstrapped"]:
                    try:
                        from onionheaven_common import get_queue_status
                        qs = get_queue_status(tc["container_name"])
                        if qs:
                            worker_info["queued"] = qs.get("queued", 0)
                            worker_info["in_flight"] = qs.get("in_flight", 0)
                            worker_info["active"] = qs.get("active", 0)
                            worker_info["failed"] = qs.get("failed", 0)
                            total_queued += qs.get("queued", 0)
                            total_in_flight += qs.get("in_flight", 0)
                            total_active += qs.get("active", 0)
                            total_failed += qs.get("failed", 0)
                    except Exception:
                        pass
                workers.append(worker_info)

            self._send_json(200, {
                "version": SERVER_VERSION,
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
                "takeover_workers": workers,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # -- GET /status/<address> ------------------------------------------------

    def _handle_status_detail(self, address):
        if not ONION_RE.match(address):
            self._send_json(400, {"error": "Invalid .onion address format"})
            return

        try:
            conn = db_connect()
            db_ensure_schema(conn)

            # Try content_address first, then healthcheck_address
            rows = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? ORDER BY registered_at",
                (address,)
            ).fetchall()
            lookup_type = "content_address"

            if not rows:
                rows = conn.execute(
                    "SELECT * FROM registry WHERE healthcheck_address = ? ORDER BY registered_at",
                    (address,)
                ).fetchall()
                lookup_type = "healthcheck_address"

            if not rows:
                conn.close()
                self._send_json(404, {"error": "No entries found for this address (checked both content_address and healthcheck_address)"})
                return

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
            conn.close()

            from onionheaven_common import get_addr_logs
            recent_logs = get_addr_logs(address)

            self._send_json(200, {
                "lookup_type": lookup_type,
                "address": address,
                "entries": entries,
                "count": len(entries),
                "recent_logs": recent_logs,
                "server_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

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
        if handler is None:
            self._send_json(404, {"error": "Not found"})
            return
        try:
            handler()
        except Exception as e:
            self.log_message("ERROR in %s: %s", path, e)
            self._send_json(500, {"error": str(e)})

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

    def _handle_name_suggest(self):
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
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
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
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
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
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
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
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

        if not onionaddress or not ONION_RE.match(onionaddress):
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
        if not _is_onionhome():
            self._send_json(404, {"error": "Not found"})
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

        if not onionaddress or not ONION_RE.match(onionaddress):
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

    # -- Local sign-and-forward endpoints ----------------------------------
    #
    # These are called by the WordPress mu-plugin on user create/delete.
    # They are NOT meant to be reachable from outside the Docker network;
    # we enforce that by rejecting any source IP that isn't on the Docker
    # bridge range. Tor-proxied traffic arrives at 127.0.0.1 (arti forwards
    # connections locally), so explicitly denying 127.x blocks Tor-origin
    # callers.

    def _name_local_src_ok(self):
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
        if not self._name_local_src_ok():
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
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return

        # Optional: target a specific healthcheck row
        healthcheck_address = data.get("healthcheck_address", "")
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Verify ed25519 signature
        ok, err = _verify_signature(self, data, "unregister")
        if not ok:
            self._send_json(403, {"error": err})
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
        the key — only the first call needs it.
        """
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
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
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
        ok, err = _verify_signature(self, data, endpoint)
        if not ok:
            self._send_json(403, {"error": err})
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
            # buggy client (or a request that slipped past signature
            # verification via the local-bypass) can overwrite a stored
            # key with bytes that decode to a different address. The key
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

            # Store plaintext PEM key
            keys_dir = os.path.join(KEYS_DIR, content_address)
            os.makedirs(keys_dir, mode=0o700, exist_ok=True)

            pem_path = os.path.join(keys_dir, "ks_hs_id.ed25519_expanded_private")
            with open(pem_path, "wb") as f:
                f.write(arti_pem)
            os.chmod(pem_path, 0o600)

            # Write hostname file
            hostname_path = os.path.join(keys_dir, "hostname")
            with open(hostname_path, "w") as f:
                f.write(content_address + "\n")
            os.chmod(hostname_path, 0o600)

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
        is_onionheaven = data.get("is_onionheaven")
        version = data.get("version", "unknown")
        is_oh = 1 if is_onionheaven else 0

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
            flush_sighup_tor()

        conn.close()

        # Write activation flag — signals the host to start the heartbeat
        # monitor + takeover Arti container.  Written on every registration
        # (not just the first) so the host watcher can restart the container
        # if it was stopped externally.
        activate_path = os.path.join(ONIONHEAVEN_DATA_DIR, "activate")
        try:
            with open(activate_path, "w") as f:
                f.write(now + "\n")
        except OSError as e:
            log(f"WARNING: could not write activation flag: {e}")

        self._send_json(200, {
            "online": True,
            "registered": True,  # backwards compat for old clients checking this
            "content_address": content_address,
            "created": created,
            "arti_key_stored": arti_key_stored,
        })

    # -- POST /offline ------------------------------------------------------

    def _handle_offline(self):
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
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Verify ed25519 signature
        ok, err = _verify_signature(self, data, "offline")
        if not ok:
            self._send_json(403, {"error": err})
            return

        # Telemetry: log every authenticated /offline. Paired with the
        # existing /online addr_log line, this lets us measure the
        # suspend-race miss rate (an /online without a preceding /offline
        # from the same address ⇒ the client lost the suspend window
        # before its /offline POST could complete).
        from onionheaven_common import addr_log
        version = data.get("version", "")
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
        flush_sighup_tor()

        conn.close()

        self._send_json(200, {
            "offline": True,
            "content_address": content_address,
        })

    # -- POST /reset-onionheaven ---------------------------------------------

    def _handle_reset_onionheaven(self):
        """Clean stress tests + refresh all takeover workers with current code.

        Removes stress test entries, tears down old workers, spawns fresh ones
        with patched code, and migrates real takeovers. No auth required —
        reachable only from within the Docker network.
        """
        from onionheaven_common import reset_onionheaven
        stats = reset_onionheaven()
        self._send_json(200, {"reset": True, **stats})


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
        if not content_address or not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
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
        if not files:
            self._send_json(200, {"wanted": []})
            return

        # Check disk usage
        try:
            usage = shutil.disk_usage("/")
            if usage.used / usage.total > ANALYTICS_DISK_THRESHOLD:
                self.log_message("Analytics: disk >85%% full, rejecting manifest")
                self._send_json(200, {"wanted": []})
                return
        except OSError:
            pass

        # Persist instance metadata with dated filename
        site_dir = os.path.join(ANALYTICS_DIR, content_address, healthcheck_address)
        os.makedirs(site_dir, exist_ok=True)
        meta = {
            "content_address": content_address,
            "healthcheck_address": healthcheck_address,
            "version": data.get("version", ""),
            "tor_impl": data.get("tor_impl", ""),
            "os_version": data.get("os_version", ""),
            "timestamp": data.get("timestamp", ""),
        }
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
                json.dump(meta, f, indent=2)
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
        for f in files:
            name = f.get("name", "")
            if not ANALYTICS_LOG_NAME_RE.match(name):
                rejected.append(name or "<empty>")
                self.log_message(
                    "Analytics: rejected %s from %s (unsafe name)",
                    name or "<empty>", content_address,
                )
                continue
            local_path = os.path.join(site_dir, name)
            if name.endswith(".log") and os.path.exists(local_path + ".gz"):
                superseded.append(name)
                continue
            remote_size = f.get("size", 0)
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
            "offered": [f.get("name", "") for f in files],
            "wanted": wanted,
            "rejected": rejected,
            "superseded": superseded,
            "version": data.get("version", ""),
        })

        self._send_json(200, {"wanted": wanted})

    # -- POST /logs/upload (OnionHome only) ------------------------------------

    def _handle_logs_upload(self):
        if not _is_onionhome():
            self._send_json(403, {"error": "Not an OnionHome instance"})
            return

        data = self._read_json(max_size=10_485_760)  # 10 MB for log uploads
        if data is False:
            return  # 413 already sent
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")
        file_name = data.get("file_name", "")

        if not content_address or not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address"})
            return
        if not ANALYTICS_LOG_NAME_RE.match(file_name):
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
        try:
            usage = shutil.disk_usage("/")
            if usage.used / usage.total > ANALYTICS_DISK_THRESHOLD:
                self._analytics_cleanup()
                # Re-check after cleanup
                usage = shutil.disk_usage("/")
                if usage.used / usage.total > ANALYTICS_DISK_THRESHOLD:
                    self._send_json(507, {"error": "Disk full"})
                    return
        except OSError:
            pass

        # Write the file
        site_dir = os.path.join(ANALYTICS_DIR, content_address, healthcheck_address)
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
            for hc in hc_dirs:
                site_dir = os.path.join(content_path, hc)
                if not os.path.isdir(site_dir):
                    continue
                entries = []
                total = 0
                try:
                    for name in os.listdir(site_dir):
                        p = os.path.join(site_dir, name)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        entries.append((st.st_mtime, st.st_size, p))
                        total += st.st_size
                except OSError:
                    continue
                if total <= ANALYTICS_PER_INSTANCE_QUOTA:
                    continue
                entries.sort()  # oldest first
                for _mtime, size, path in entries:
                    if total <= ANALYTICS_PER_INSTANCE_QUOTA:
                        break
                    try:
                        os.remove(path)
                        total -= size
                    except OSError:
                        continue

        # Phase 3: disk-full fallback. Only kicks in if the above two
        # phases didn't free enough space.
        try:
            usage = shutil.disk_usage("/")
        except OSError:
            return
        if usage.used / usage.total <= ANALYTICS_DISK_THRESHOLD:
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
                usage = shutil.disk_usage("/")
                if usage.used / usage.total <= ANALYTICS_DISK_THRESHOLD:
                    break
                os.remove(path)
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
    # Only OnionHome exposes the /api/name/* endpoints, but it's harmless to
    # create an empty DB on non-OnionHome instances (won't be read).
    if _is_onionhome():
        # One-shot migration for installs that were running the pre-fix
        # DB location (/var/lib/onionhome/onionnames.db) — harmless no-op
        # on fresh installs. Runs BEFORE the first db_connect so we don't
        # create an empty DB at the new path and block the migration.
        try:
            onionnames.migrate_legacy_db()
        except Exception as e:
            onionnames.log(f"startup: migrate_legacy_db errored: {e}")
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

    # ThreadingHTTPServer: the register-local forward path can block up to
    # 30s waiting on Tor. Single-threaded serving would stall every other
    # endpoint (heartbeats, status) during that window. Existing handlers
    # are safe under concurrency (SQLite WAL, per-request connections).
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), OnionHeavenHandler)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] web-server: listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
