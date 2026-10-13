#!/usr/bin/env python3
"""The hub checks request signatures with libsodium, and without it refuses
them without taking anyone's site over.

web-server.py verifies every signed request (/online, /offline,
/unregister, the OnionHome name and log endpoints) through onion_auth,
whose verify() is libsodium's (PyNaCl's VerifyKey). The pure-Python
verifier it replaced accepted R = identity, S = 0 as a signature of any
message for a small-order public key, so anyone could act for the address
aeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaahmjqd.onion.

Without PyNaCl (the tor image installs python3-nacl and checks it at
build), verification raises: every signed request gets 503 and nothing is
done, since an unchecked /offline or /unregister would let anyone release
or remove another site's address. That stops every /online too, so the
heartbeat monitor takes over no site for a missed heartbeat meanwhile, and
when verification is back every online site first gets a fresh grace
period.

PyNaCl must be installed (the CI does). No Docker: handlers run in-process
on temp directories, and the monitor runs one pass against a temp registry.
"""

import base64
import hashlib
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

import onionheaven_common as oc  # noqa: E402
import onionnames  # noqa: E402
from onionpress import onion_auth  # noqa: E402
from nacl import bindings as sodium  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _load("web_server_signature_verification", "web-server.py")
hbm = _load("onionheaven_heartbeat_signature_verification", "onionheaven-heartbeat.py")

NO_PYNACL = {"nacl": None, "nacl.signing": None, "nacl.exceptions": None,
             "nacl.bindings": None}
IDENTITY = (1).to_bytes(32, "little")
FORGED = base64.b64encode(IDENTITY + bytes(32)).decode()  # R = identity, S = 0
SMALL_ORDER_ADDRESS = onion_auth.derive_onion_address(IDENTITY)


def _keypair():
    seed = os.urandom(32)
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    public = sodium.crypto_sign_seed_keypair(seed)[0]
    return bytes(h), public, onion_auth.derive_onion_address(public)


class _Handler(ws.OnionHeavenHandler):
    """The real handler without a socket."""

    def __init__(self, path, data):  # noqa: D401 - no BaseHTTPRequestHandler setup
        self.path = path
        self.client_address = ("127.0.0.1", 0)
        self._data = data
        self.replies = []

    def _refuse_unless_hub(self):
        return False

    def _name_registry_ready(self):
        return True

    def _read_json(self, max_size=None):
        return self._data

    def _send_json(self, status_code, data):
        self.replies.append((status_code, data))


class _HandlerTest(unittest.TestCase):
    """Every way a handler could act is recorded; none may run unverified."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hub-sigs-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.analytics = os.path.join(self.base, "analytics")
        self.acted = []
        self.logged = []

        def act(what):
            return lambda *a, **k: self.acted.append((what, a))

        def forbidden(what):
            def call(*a, **k):
                self.acted.append((what, a))
                raise AssertionError(f"{what} reached without a verified signature")
            return call

        for target, name, value in (
                (ws, "takeover_function", act("takeover")),
                (ws, "release_function", act("release")),
                (ws, "write_key_file", act("write_key_file")),
                (ws, "db_connect", forbidden("db_connect")),
                (oc, "unregister_entry", act("unregister")),
                (onionnames, "db_connect", forbidden("names db_connect")),
                (onionnames, "register_name", act("register_name")),
                (onionnames, "release_name", act("release_name")),
                (ws, "ANALYTICS_DIR", self.analytics),
                (ws, "KEYS_DIR", os.path.join(self.base, "keys")),
                (ws, "_get_own_address", lambda: None),
                (ws, "_is_onionhome", lambda: True),
                (ws, "_warned_no_verification", False),
                (ws, "log", self.logged.append),
                (oc, "log", lambda *a: None),
                (oc, "addr_log", lambda *a: None)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.expanded, self.public, self.address = _keypair()
        self.hc = _keypair()[2]

    def _signed(self, endpoint, hc=None, **extra):
        hc = self.hc if hc is None else hc
        ts = onion_auth.make_timestamp()
        data = {
            "content_address": self.address,
            "healthcheck_address": hc,
            "timestamp": ts,
            "signature": onion_auth.sign_payload(self.expanded, self.public, endpoint,
                                                 self.address, hc, ts),
        }
        data.update(extra)
        return data

    def _named(self, endpoint, name="mysite"):
        ts = onion_auth.make_timestamp()
        return {
            "onionaddress": self.address,
            "onionname": name,
            "timestamp": ts,
            "signature": onion_auth.sign_name_payload(self.expanded, self.public,
                                                      endpoint, self.address, name, ts),
        }

    def _requests(self):
        """One genuinely signed request per signed endpoint."""
        return [
            ("/offline", self._signed("offline")),
            ("/unregister", self._signed("unregister")),
            ("/online", self._signed("online", arti_key_pem="unused")),
            ("/api/name/register", self._named("register")),
            ("/api/name/release", self._named("release")),
            ("/logs/manifest", self._signed("logs", files=[{"name": "a.log", "size": 1}])),
            ("/logs/upload", self._signed("logs", file_name="a.log",
                                          file_content=base64.b64encode(b"x").decode())),
        ]

    def _post(self, path, data):
        handler = _Handler(path, data)
        handler.do_POST()
        self.assertEqual(len(handler.replies), 1, handler.replies)
        return handler.replies[0]


class WithoutPyNaClTest(_HandlerTest):

    def test_every_signed_endpoint_answers_503_and_does_nothing(self):
        with mock.patch.dict(sys.modules, NO_PYNACL):
            for path, data in self._requests():
                status, body = self._post(path, data)
                self.assertEqual(status, 503, (path, body))
                self.assertIn("signature verification is unavailable", body["error"])
        self.assertEqual(self.acted, [])
        self.assertFalse(os.path.exists(self.analytics))
        self.assertFalse(os.path.exists(os.path.join(self.base, "keys")))
        # Said once, naming the cure and what the monitor does meanwhile.
        errors = [m for m in self.logged if "cannot be verified" in m]
        self.assertEqual(len(errors), 1, self.logged)
        self.assertIn("python3-nacl", errors[0])
        self.assertIn("takes over no site", errors[0])

    def test_a_peer_claim_nobody_can_check_earns_nothing(self):
        data = self._signed("online", is_onionheaven=True)
        data["onionheaven_peer_signature"] = onion_auth.sign_payload(
            self.expanded, self.public, ws.PEER_CLAIM_ENDPOINT, self.address,
            self.hc, data["timestamp"])
        self.assertTrue(ws._signed_peer_claim(data))
        with mock.patch.dict(sys.modules, NO_PYNACL):
            self.assertFalse(ws._signed_peer_claim(data))

    def test_bad_requests_are_still_400_or_403(self):
        """Checks that need no libsodium still run first."""
        with mock.patch.dict(sys.modules, NO_PYNACL):
            data = self._signed("offline")
            data["content_address"] = "not-an-onion"
            self.assertEqual(self._post("/offline", data)[0], 400)
            data = self._signed("offline")
            data["timestamp"] = "2020-01-01T00:00:00Z"
            self.assertEqual(self._post("/offline", data)[0], 403)
        self.assertEqual(self.acted, [])


class WithPyNaClTest(_HandlerTest):

    def test_genuine_requests_are_acted_on(self):
        def memory_db():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            return conn

        with mock.patch.object(ws, "db_connect", memory_db):
            status, body = self._post("/offline", self._signed("offline"))
        self.assertEqual(status, 200, body)
        self.assertEqual([what for what, _ in self.acted], ["takeover"])
        self.acted.clear()
        with mock.patch.object(onionnames, "db_connect",
                               lambda *a: sqlite3.connect(":memory:")), \
                mock.patch.object(onionnames, "db_init", lambda conn: None), \
                mock.patch.object(onionnames, "lookup_name", lambda conn, name: None), \
                mock.patch.object(onionnames, "register_name",
                                  lambda conn, n, a: (True, None, None)):
            self.assertEqual(self._post("/api/name/register", self._named("register"))[0], 201)

    def test_small_order_forgery_is_refused(self):
        """What the pure-Python verifier accepted for any message."""
        ts = onion_auth.make_timestamp()
        for path, endpoint in (("/offline", "offline"), ("/unregister", "unregister"),
                               ("/logs/manifest", "logs")):
            data = {"content_address": SMALL_ORDER_ADDRESS, "healthcheck_address": "",
                    "timestamp": ts, "signature": FORGED, "files": []}
            status, body = self._post(path, data)
            self.assertEqual(status, 403, (path, body))
        status, _ = self._post("/online", {
            "content_address": SMALL_ORDER_ADDRESS, "healthcheck_address": self.hc,
            "timestamp": ts, "signature": FORGED, "arti_key_pem": "unused"})
        self.assertEqual(status, 403)
        for path in ("/api/name/register", "/api/name/release"):
            status, _ = self._post(path, {"onionaddress": SMALL_ORDER_ADDRESS,
                                          "onionname": "squat", "timestamp": ts,
                                          "signature": FORGED})
            self.assertEqual(status, 403)
        self.assertEqual(self.acted, [])

    def test_another_keys_signature_is_refused(self):
        other_expanded, other_public, _ = _keypair()
        data = self._signed("unregister")
        data["signature"] = onion_auth.sign_payload(
            other_expanded, other_public, "unregister", self.address, self.hc,
            data["timestamp"])
        self.assertEqual(self._post("/unregister", data)[0], 403)
        self.assertEqual(self.acted, [])


class DockerfileTest(unittest.TestCase):

    def test_the_build_checks_the_verifier(self):
        with open(os.path.join(DOCKER_TOR_DIR, "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        runtime = text[text.rindex("\nFROM ${TOR_IMAGE}\n"):]
        install = runtime[runtime.index("RUN apt-get update"):]
        install = install[:install.index("\n\n") if "\n\n" in install else None]
        self.assertRegex(install, r"\bpython3-nacl\b")
        self.assertIn("from nacl.signing import VerifyKey", install)
        self.assertLess(runtime.index("RUN apt-get update"),
                        runtime.index("COPY onion_auth.py /onion_auth.py"))


# ---------------------------------------------------------------------------
# The heartbeat monitor
# ---------------------------------------------------------------------------

class _Stop(BaseException):
    """Ends main() after one pass (its loop catches Exception only)."""


class MonitorTest(unittest.TestCase):
    """One pass of onionheaven-heartbeat.py's main loop on a temp registry."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hub-monitor-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.taken = []
        self.logged = []
        noop = lambda *a, **k: None  # noqa: E731
        for target, name, value in (
                (oc, "ONIONHEAVEN_DATA_DIR", self.base),
                (oc, "DB_PATH", os.path.join(self.base, "registry.db")),
                (oc, "log", self.logged.append),
                (hbm, "ONIONHEAVEN_DATA_DIR", self.base),
                (hbm, "log", self.logged.append),
                (hbm, "wait_for_hub_admission", noop),
                (hbm, "startup_reconciliation", noop),
                (hbm, "check_worker_bootstrap", noop),
                (hbm, "cleanup_dead_workers", noop),
                (hbm, "takeover_function",
                 lambda conn, ca, ha, force=False: self.taken.append((ca, force))),
                (hbm, "wall_sleep", self._stop),
                (hbm, "_verification_was_unavailable", False)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.address = _keypair()[2]
        self.hc = _keypair()[2]
        conn = oc.db_connect()
        oc.db_ensure_schema(conn)
        conn.execute(
            "INSERT INTO registry (content_address, healthcheck_address, registered_at, "
            "last_healthy, status) VALUES (?, ?, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', 'online')", (self.address, self.hc))
        conn.commit()
        conn.close()

    @staticmethod
    def _stop(_seconds):
        raise _Stop()

    def _pass(self):
        with self.assertRaises(_Stop):
            hbm.main()

    def _last_healthy(self):
        conn = sqlite3.connect(oc.DB_PATH)
        try:
            return conn.execute("SELECT last_healthy FROM registry").fetchone()[0]
        finally:
            conn.close()

    def test_a_stale_heartbeat_is_taken_over(self):
        self._pass()
        self.assertEqual(self.taken, [(self.address, False)])

    def test_no_takeover_while_signatures_cannot_be_verified(self):
        with mock.patch.dict(sys.modules, NO_PYNACL):
            self._pass()
            self._pass()
        self.assertEqual(self.taken, [])
        errors = [m for m in self.logged if "cannot be verified" in m and "ERROR" in m]
        self.assertEqual(len(errors), 1, self.logged)
        self.assertTrue(any("1 stale heartbeats not acted on" in m for m in self.logged),
                        self.logged)
        self.assertEqual(self._last_healthy(), "2026-01-01T00:00:00Z")

    def test_a_fresh_grace_period_when_verification_returns(self):
        with mock.patch.dict(sys.modules, NO_PYNACL):
            self._pass()
        self._pass()
        self.assertEqual(self.taken, [])
        self.assertGreater(self._last_healthy(), "2026-01-01T00:00:00Z")
        self.assertTrue(any("verified again" in m for m in self.logged), self.logged)

    def test_a_verified_offline_is_still_acted_on(self):
        """web-server.py records an /offline only after verifying it."""
        conn = sqlite3.connect(oc.DB_PATH)
        conn.execute("UPDATE registry SET last_healthy = '2999-01-01T00:00:00Z', "
                     "takeover_requested = '2999-01-01T00:00:01Z'")
        conn.commit()
        conn.close()
        with mock.patch.dict(sys.modules, NO_PYNACL):
            self._pass()
        self.assertEqual(self.taken, [(self.address, True)])


if __name__ == "__main__":
    unittest.main()
