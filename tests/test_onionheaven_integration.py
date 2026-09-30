#!/usr/bin/env python3
"""
Local integration test for OnionHeaven ed25519 signature auth.

Starts web-server.py on localhost and exercises register/unregister/
online/offline with real ed25519 signatures. No Tor, no Docker required.

Usage:
    python3 tests/test_onionheaven_integration.py
"""

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

# Allow importing from src/ and docker/tor/
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import onion_auth

# ---------------------------------------------------------------------------
# Test key generation helpers
# ---------------------------------------------------------------------------

def _make_test_keypair(seed_hex=None):
    """Generate an expanded key + public key + onion address for testing.

    Returns (expanded_key_64, public_key_32, onion_address).
    """
    if seed_hex:
        seed = bytes.fromhex(seed_hex)
    else:
        seed = os.urandom(32)
    h = hashlib.sha512(seed).digest()
    a_bytes = bytearray(h[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    expanded = bytes(a_bytes) + h[32:]
    a = int.from_bytes(a_bytes, 'little')
    A = onion_auth._scalar_mult(a, onion_auth._B)
    pub = onion_auth._encode_point(A)
    addr = onion_auth.derive_onion_address(pub)
    return expanded, pub, addr


def _make_arti_pem(expanded_key, public_key):
    """Build a fake Arti PEM from expanded key + public key."""
    import struct as _struct
    OPENSSH_MAGIC = b"openssh-key-v1\x00"
    KEY_TYPE = b"ed25519-expanded@spec.torproject.org"

    def pack(data):
        return _struct.pack(">I", len(data)) + data

    pub_blob = pack(KEY_TYPE) + pack(public_key)
    check = _struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (check + check + pack(KEY_TYPE) + pack(public_key) +
                 pack(expanded_key) + pack(b""))
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))
    binary = (OPENSSH_MAGIC + pack(b"none") + pack(b"none") + pack(b"") +
              _struct.pack(">I", 1) + pack(pub_blob) + pack(priv_blob))
    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem.encode("utf-8")


# ---------------------------------------------------------------------------
# Server process management
# ---------------------------------------------------------------------------

SERVER_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
SERVER_SCRIPT = os.path.join(SERVER_DIR, "web-server.py")


def _find_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ServerProcess:
    """Manages a local web-server.py process for testing."""

    def __init__(self, port=None):
        self.port = port or _find_free_port()
        self.proc = None
        self._tmpdir = None

    def start(self):
        self._tmpdir = tempfile.mkdtemp(prefix="onionheaven-test-")
        data_dir = os.path.join(self._tmpdir, "onionheaven")
        keys_dir = os.path.join(data_dir, "keys")
        os.makedirs(keys_dir, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = SERVER_DIR
        # Override paths so the server uses our temp dir
        env["ONIONHEAVEN_DATA_DIR"] = data_dir
        env["ONIONHEAVEN_LISTEN_PORT"] = str(self.port)
        env["ONIONHEAVEN_ENFORCE_AUTH"] = "1"

        self.proc = subprocess.Popen(
            [sys.executable, "-c", f"""
import importlib.util, os, sys
sys.path.insert(0, {SERVER_DIR!r})

# Patch constants before importing the server
import onionheaven_common
onionheaven_common.ONIONHEAVEN_DATA_DIR = {data_dir!r}
onionheaven_common.DB_PATH = os.path.join({data_dir!r}, "registry.db")
onionheaven_common.KEYS_DIR = {keys_dir!r}
def _stub_takeover(conn, ca, ha, force=False):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE registry SET status='taken-over', last_taken_over=? "
        "WHERE content_address=? AND healthcheck_address=?",
        (now, ca, ha))
    conn.commit()
def _stub_release(conn, ca, ha, force=False):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE registry SET status='online', last_released=? "
        "WHERE content_address=? AND healthcheck_address=?",
        (now, ca, ha))
    conn.commit()
onionheaven_common.takeover_function = _stub_takeover
onionheaven_common.release_function = _stub_release

# Import web-server.py (dash in filename, can't use normal import)
spec = importlib.util.spec_from_file_location(
    "onionheaven_server",
    os.path.join({SERVER_DIR!r}, "web-server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["onionheaven_server"] = srv
spec.loader.exec_module(srv)

srv.LISTEN_PORT = {self.port}
srv.KEYS_DIR = {keys_dir!r}
srv.main()
"""],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # Wait for server to start
        for _ in range(50):
            time.sleep(0.1)
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.5):
                    return
            except (ConnectionRefusedError, OSError):
                pass
        raise RuntimeError("Server did not start within 5 seconds")

    def stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            # Close the PIPE fds explicitly; wait() leaves them open and
            # dropping the Popen reference relies on GC, which trips
            # ResourceWarning at interpreter shutdown.
            for stream in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
                if stream is not None:
                    stream.close()
            self.proc = None
        if self._tmpdir:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _post(url, data):
    """POST JSON, return (status_code, response_dict)."""
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    """GET request, return (status_code, response_dict)."""
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

server = None


def setUpModule():
    global server
    server = ServerProcess()
    server.start()
    print(f"\nServer started on port {server.port}")


def tearDownModule():
    global server
    if server:
        server.stop()
        print("Server stopped")


class TestStatus(unittest.TestCase):
    """GET /status should work without auth."""

    def test_status_ok(self):
        code, data = _get(f"{server.base_url}/status")
        self.assertEqual(code, 200)
        self.assertIn("version", data)
        self.assertIn("total", data)


class TestRegister(unittest.TestCase):

    def test_register_success(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(expanded, pub)

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "online", addr, hc_addr, ts)

        code, data = _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "version": "test-1.0",
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 200, f"Register failed: {data}")
        self.assertTrue(data.get("registered"))

    def test_register_bad_signature_rejected(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(expanded, pub)

        ts = onion_auth.make_timestamp()
        # Sign with wrong endpoint
        sig = onion_auth.sign_payload(expanded, pub, "unregister", addr, hc_addr, ts)

        code, data = _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 403)
        self.assertIn("error", data)

    def test_register_wrong_key_rejected(self):
        """Signature from a different key should be rejected."""
        expanded1, pub1, addr1 = _make_test_keypair()
        expanded2, pub2, addr2 = _make_test_keypair()
        pem1 = _make_arti_pem(expanded1, pub1)

        ts = onion_auth.make_timestamp()
        # Sign with key2 but claim to be addr1
        sig = onion_auth.sign_payload(expanded2, pub2, "online", addr1, addr2, ts)

        code, data = _post(f"{server.base_url}/online", {
            "content_address": addr1,
            "healthcheck_address": addr2,
            "arti_key_pem": base64.b64encode(pem1).decode('ascii'),
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 403)

    def test_register_missing_pem_rejected(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "online", addr, hc_addr, ts)

        code, data = _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 400)

    def test_register_expired_timestamp_rejected(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(expanded, pub)

        ts = "2020-01-01T00:00:00Z"
        sig = onion_auth.sign_payload(expanded, pub, "online", addr, hc_addr, ts)

        code, data = _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 403)


class TestFullLifecycle(unittest.TestCase):
    """Register, then unregister/online/offline with the same key."""

    def setUp(self):
        self.expanded, self.pub, self.addr = _make_test_keypair()
        self.hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(self.expanded, self.pub)

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(
            self.expanded, self.pub, "online",
            self.addr, self.hc_addr, ts
        )
        code, data = _post(f"{server.base_url}/online", {
            "content_address": self.addr,
            "healthcheck_address": self.hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "version": "test-lifecycle",
            "timestamp": ts,
            "signature": sig,
        })
        assert code == 200 and data.get("registered"), f"Setup register failed: {data}"

    def _signed_post(self, endpoint, extra=None):
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(
            self.expanded, self.pub, endpoint,
            self.addr, self.hc_addr, ts
        )
        payload = {
            "content_address": self.addr,
            "healthcheck_address": self.hc_addr,
            "timestamp": ts,
            "signature": sig,
        }
        if extra:
            payload.update(extra)
        return _post(f"{server.base_url}/{endpoint}", payload)

    def test_offline_then_online(self):
        # Go offline
        code, data = self._signed_post("offline")
        self.assertEqual(code, 200, f"offline failed: {data}")
        self.assertTrue(data.get("offline"))

        # Check status shows taken-over
        code, data = _get(f"{server.base_url}/status/{self.addr}")
        self.assertEqual(code, 200)
        self.assertEqual(data["entries"][0]["status"], "taken-over")

        # Come back online. /online now requires arti_key_pem on every call
        # (the server uses it to prove ownership of the key and to enable
        # takeover when the site goes offline), so include it even on a
        # re-online of an existing registration.
        pem = _make_arti_pem(self.expanded, self.pub)
        code, data = self._signed_post("online", extra={
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
        })
        self.assertEqual(code, 200, f"online failed: {data}")
        self.assertTrue(data.get("online"))

        # Check status shows online again
        code, data = _get(f"{server.base_url}/status/{self.addr}")
        self.assertEqual(code, 200)
        self.assertEqual(data["entries"][0]["status"], "online")

    def test_unregister(self):
        code, data = self._signed_post("unregister")
        self.assertEqual(code, 200, f"unregister failed: {data}")
        self.assertTrue(data.get("unregistered"))

    def test_re_register(self):
        """Re-registering should update the existing entry."""
        pem = _make_arti_pem(self.expanded, self.pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(
            self.expanded, self.pub, "online",
            self.addr, self.hc_addr, ts
        )
        code, data = _post(f"{server.base_url}/online", {
            "content_address": self.addr,
            "healthcheck_address": self.hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "version": "test-v2",
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 200)
        # The response no longer carries a "message" field; `created=False`
        # on a 200 is how the server signals "row existed, got updated".
        self.assertFalse(data.get("created"),
                         f"expected existing row update, got {data}")
        self.assertTrue(data.get("online"))

    def test_wrong_key_cannot_unregister(self):
        """A different key cannot unregister someone else's address."""
        other_exp, other_pub, _ = _make_test_keypair()
        ts = onion_auth.make_timestamp()
        # Sign with other key but use self.addr as content_address
        sig = onion_auth.sign_payload(
            other_exp, other_pub, "unregister",
            self.addr, self.hc_addr, ts
        )
        code, data = _post(f"{server.base_url}/unregister", {
            "content_address": self.addr,
            "healthcheck_address": self.hc_addr,
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 403)

    def test_wrong_key_cannot_go_offline(self):
        other_exp, other_pub, _ = _make_test_keypair()
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(
            other_exp, other_pub, "offline",
            self.addr, self.hc_addr, ts
        )
        code, data = _post(f"{server.base_url}/offline", {
            "content_address": self.addr,
            "healthcheck_address": self.hc_addr,
            "timestamp": ts,
            "signature": sig,
        })
        self.assertEqual(code, 403)


class TestStatusLookup(unittest.TestCase):
    """Test /status/<address> lookups."""

    def test_lookup_by_content_address(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(expanded, pub)

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "online", addr, hc_addr, ts)
        _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "timestamp": ts,
            "signature": sig,
        })

        code, data = _get(f"{server.base_url}/status/{addr}")
        self.assertEqual(code, 200)
        self.assertEqual(data["lookup_type"], "content_address")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["entries"][0]["content_address"], addr)

    def test_lookup_by_healthcheck_address(self):
        expanded, pub, addr = _make_test_keypair()
        hc_addr = _make_test_keypair()[2]
        pem = _make_arti_pem(expanded, pub)

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "online", addr, hc_addr, ts)
        _post(f"{server.base_url}/online", {
            "content_address": addr,
            "healthcheck_address": hc_addr,
            "arti_key_pem": base64.b64encode(pem).decode('ascii'),
            "timestamp": ts,
            "signature": sig,
        })

        code, data = _get(f"{server.base_url}/status/{hc_addr}")
        self.assertEqual(code, 200)
        self.assertEqual(data["lookup_type"], "healthcheck_address")

    def test_not_found(self):
        fake = "a" * 56 + ".onion"
        # This will likely fail checksum, but server just checks ONION_RE
        # Let's use a valid-looking one
        expanded, pub, addr = _make_test_keypair()
        code, data = _get(f"{server.base_url}/status/{addr}")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
