#!/usr/bin/env python3
"""Issue #469: the hub's per-address status answers the public coarsely
and the address's own key in full.

GET /status/<address> used to hand anyone over Tor every registered
site's healthcheck address, version, online/offline/takeover times,
audit state and address log (with its "/offline received" lines, sent at
host suspend): polled, a minute-resolution record of when each
publisher's computer slept and woke, and both halves of the content /
healthcheck pair. Now the plain GET says only whether a content address
is registered and its status word, a healthcheck address is looked up by
nothing, and the detail answers POST /status/<address> signed by the
address's own key (web-server.py's STATUS_ENDPOINT, replay-marked like
/offline, 404 for an address the registry never had). The aggregate
/status keeps its counts and drops the worker list.

Consumers (census of 2026-10-29): src/onionpress/onionheaven.py's
_heartbeat_landed (the Mac daemon: did a timed-out /online land?) needs
last_healthy and signs its query; the launchers' hub-address check and
the heartbeats read only the aggregate /status; linux/, the plugins and
tests/stress read neither per-address form. No Docker: the handler runs
in-process on a shared in-memory registry.
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

import onionheaven_common as oc  # noqa: E402
from onionpress import onion_auth, onionheaven, key_manager  # noqa: E402
from tests.test_hub_signature_verification import _keypair  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _load("web_server_status_privacy", "web-server.py")

SENSITIVE_KEYS = ("healthcheck_address", "version", "last_healthy", "last_taken_over",
                  "last_released", "last_checked", "last_redirect", "registered_at",
                  "unregistered_at", "audit_result", "audit_pending", "recent_logs",
                  "seconds_since_last_healthy", "serving_status", "wordpress_healthy",
                  "is_onionheaven", "entries", "lookup_type", "server_time")


class _Handler(ws.OnionHeavenHandler):
    """The real handler without a socket."""

    def __init__(self, path, data=None):  # noqa: D401 - no BaseHTTPRequestHandler setup
        self.path = path
        self.client_address = ("127.0.0.1", 0)
        self._data = data
        self._body = b""
        self._body_len = 0
        self.replies = []

    def _body_length(self, max_size):
        return 0

    def _read_body(self):
        return b""  # what _refuse_unless_hub drains

    def _read_json(self, max_size=None):
        return self._data

    def _send_json(self, status_code, data):
        self.replies.append((status_code, data))


class _RegistryTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hub-status-")
        self.addCleanup(shutil.rmtree, self.base, True)
        uri = f"file:status-{id(self)}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)
        self.addCleanup(self.keeper.close)

        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        self.logged = []
        for target, name, value in (
                (ws, "db_connect", connect),
                (ws, "_accepts_registrations", lambda: True),
                (ws, "_get_own_address", lambda: None),
                (ws, "log", self.logged.append),
                (ws, "takeover_function", lambda *a, **k: None),
                (oc, "log", lambda *a: None),
                (oc, "_addr_logs", type(oc._addr_logs)())):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        oc.db_ensure_schema(self.keeper)
        self.expanded, self.public, self.address = _keypair()
        self.hc = _keypair()[2]

    def _row(self, address=None, hc=None, status="online", unregistered=False,
             version="2.6.0", healthy="2026-10-29T10:00:00Z"):
        self.keeper.execute(
            "INSERT INTO registry (content_address, healthcheck_address, registered_at, "
            "version, last_healthy, last_taken_over, status, unregistered_at) "
            "VALUES (?, ?, '2026-10-01T00:00:00Z', ?, ?, '2026-10-28T03:00:00Z', ?, ?)",
            (self.address if address is None else address, self.hc if hc is None else hc,
             version, healthy, status, "2026-10-29T00:00:00Z" if unregistered else None))
        self.keeper.commit()

    def _get(self, path):
        h = _Handler(path)
        h.do_GET()
        self.assertEqual(len(h.replies), 1, h.replies)
        return h.replies[0]

    def _post(self, path, data):
        h = _Handler(path, data)
        h.do_POST()
        self.assertEqual(len(h.replies), 1, h.replies)
        return h.replies[0]

    def _query(self, hc="", endpoint=None, expanded=None, public=None, address=None,
               path=None, ts=None):
        address = self.address if address is None else address
        ts = ts or onion_auth.make_timestamp()
        data = {
            "content_address": address, "healthcheck_address": hc, "timestamp": ts,
            "signature": onion_auth.sign_payload(
                self.expanded if expanded is None else expanded,
                self.public if public is None else public,
                ws.STATUS_ENDPOINT if endpoint is None else endpoint, address, hc, ts)}
        return self._post(f"/status/{address}" if path is None else path, data)


class CoarseGetTest(_RegistryTest):

    def test_a_registered_address_gets_three_fields(self):
        self._row()
        oc.addr_log(self.address, f"/offline received from {self.address} (version=2.6.0)")
        status, body = self._get(f"/status/{self.address}")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"address": self.address, "registered": True, "status": "online"})
        text = json.dumps(body)
        self.assertNotIn(self.hc, text)
        for key in SENSITIVE_KEYS:
            self.assertNotIn(key, body)

    def test_a_healthcheck_address_names_nothing(self):
        self._row()
        self.assertEqual(self._get(f"/status/{self.hc}")[0], 404)

    def test_unknown_and_malformed(self):
        self.assertEqual(self._get(f"/status/{_keypair()[2]}")[0], 404)
        self.assertEqual(self._get("/status/x.onion")[0], 400)
        self.assertEqual(self._get(f"/status/{self.address.upper()}")[0], 400)

    def test_the_word_is_the_most_alive_live_row(self):
        self._row(status="taken-over")
        self.assertEqual(self._get(f"/status/{self.address}")[1]["status"], "taken-over")
        self._row(hc=_keypair()[2], status="online")
        self.assertEqual(self._get(f"/status/{self.address}")[1]["status"], "online")
        self._row(hc=_keypair()[2], status="unregistered", unregistered=True)
        self.assertEqual(self._get(f"/status/{self.address}")[1],
                         {"address": self.address, "registered": True, "status": "online"})

    def test_an_address_with_only_unregistered_rows(self):
        self._row(status="unregistered", unregistered=True)
        self.assertEqual(self._get(f"/status/{self.address}")[1],
                         {"address": self.address, "registered": False, "status": "unregistered"})

    def test_the_get_opens_the_registry_for_two_columns_only(self):
        src = open(os.path.join(DOCKER_TOR_DIR, "web-server.py"), encoding="utf-8").read()
        body = src[src.index("def _handle_status_detail(self"):src.index("def _handle_status_signed(self")]
        self.assertIn("SELECT status, unregistered_at FROM registry WHERE content_address = ?", body)
        self.assertNotIn("healthcheck_address = ?", body)
        self.assertNotIn("get_addr_logs", body)
        self.assertNotIn("SELECT *", body)


class SignedDetailTest(_RegistryTest):

    def test_the_addresss_own_key_gets_the_detail(self):
        self._row()
        other_hc = _keypair()[2]
        self._row(hc=other_hc, status="taken-over")
        oc.addr_log(self.address, "a line")
        status, body = self._query()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["lookup_type"], "content_address")
        self.assertEqual(body["address"], self.address)
        self.assertEqual(body["count"], 2)
        entries = {e["healthcheck_address"]: e for e in body["entries"]}
        self.assertEqual(set(entries), {self.hc, other_hc})
        self.assertEqual(entries[self.hc]["version"], "2.6.0")
        self.assertEqual(entries[self.hc]["last_healthy"], "2026-10-29T10:00:00Z")
        self.assertEqual(entries[other_hc]["status"], "taken-over")
        self.assertIn("seconds_since_last_healthy", entries[self.hc])
        self.assertEqual(body["recent_logs"][0][-6:], "a line")
        # With its healthcheck address in the signed body too.
        self.assertEqual(self._query(hc=self.hc)[0], 200)

    def test_another_key_or_label_gets_nothing(self):
        self._row()
        other_expanded, other_public, _ = _keypair()
        self.assertEqual(self._query(expanded=other_expanded, public=other_public)[0], 403)
        for endpoint in ("offline", "online", "unregister", "logs", ws.PEER_CLAIM_ENDPOINT):
            with self.subTest(endpoint=endpoint):
                status, body = self._query(endpoint=endpoint)
                self.assertEqual(status, 403, body)
        self.assertEqual(self._query(hc="x.onion")[0], 400)
        self.assertEqual(self._query(path=f"/status/{_keypair()[2]}")[0], 400,
                         "the body's address must be the path's")
        self.assertEqual(self._post(f"/status/{self.address}", {})[0], 400)
        self.assertEqual(self._post("/status/x.onion", {"content_address": "x.onion"})[0], 400)

    def test_an_address_the_hub_never_had_is_404_and_leaves_no_mark(self):
        self.assertEqual(self._query()[0], 404)
        self.assertEqual(self.keeper.execute("SELECT COUNT(*) FROM signed_requests").fetchone()[0], 0)
        self._row(hc=_keypair()[2])
        self.assertEqual(self._query(hc=self.hc)[0], 404, "the pair is not there")
        self.assertEqual(self._query()[0], 200, "the address is")

    def test_a_query_is_accepted_once(self):
        self._row()
        ts = onion_auth.make_timestamp()
        self.assertEqual(self._query(ts=ts)[0], 200)
        status, body = self._query(ts=ts)
        self.assertEqual(status, 403)
        self.assertIn("replay", body["error"])
        earlier = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._query(ts=earlier)[0], 403)

    def test_a_node_that_is_no_hub_refuses(self):
        self._row()
        with mock.patch.object(ws, "_accepts_registrations", lambda: False):
            status, body = self._query()
        self.assertEqual(status, 403)
        self.assertIn("not an OnionHeaven hub", body["error"])

    def test_the_endpoint_literal_is_the_clients(self):
        self.assertEqual(ws.STATUS_ENDPOINT, "status")
        self.assertEqual(onionheaven.STATUS_ENDPOINT, ws.STATUS_ENDPOINT)
        self.assertNotIn(ws.STATUS_ENDPOINT,
                         ("online", "offline", "unregister", "logs", ws.PEER_CLAIM_ENDPOINT))


class AggregateStatusTest(_RegistryTest):

    def test_counts_but_no_worker_names(self):
        self._row()
        self.keeper.execute(
            "INSERT INTO takeover_containers (container_name, bootstrapped, assigned_count) "
            "VALUES ('onionheaven-takeover-7', 1, 3)")
        self.keeper.commit()
        with mock.patch.object(oc, "get_queue_status", lambda name: {"queued": 2, "active": 3}):
            status, body = self._get("/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["takeover_containers"], 1)
        self.assertEqual(body["takeover_queued"], 2)
        self.assertEqual(body["takeover_active"], 3)
        self.assertEqual(body["total"], 1)
        self.assertNotIn("takeover_workers", body)
        self.assertNotIn("onionheaven-takeover-7", json.dumps(body))
        self.assertNotIn(self.address, json.dumps(body))


class _FakeApp:
    def __init__(self, tmpdir, onion_address, healthcheck_address):
        self.onion_address = onion_address
        self.healthcheck_address = healthcheck_address
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = tmpdir
        self.colima_home = os.path.join(tmpdir, "colima")
        self.bin_dir = os.path.join(tmpdir, "bin")
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def read_config_value(self, key, default=""):
        return "yes" if key == "REGISTER_WITH_ONIONHEAVEN" else default


class _Recorder:
    """Stands in for subprocess.run: records argv and stdin, answers *reply*."""

    def __init__(self, reply=""):
        self.calls = []
        self.reply = reply

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, stdout=self.reply, stderr="")


class ClientTest(unittest.TestCase):
    """The Mac daemon's _heartbeat_landed is the one consumer of the
    per-address detail: it signs its query."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="op-hub-status-client-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        priv = os.urandom(64)
        pub = onion_auth.derive_public_key(priv)
        self.site = onion_auth.derive_onion_address(pub)
        p = mock.patch.object(key_manager, "extract_keys", return_value=(priv, pub))
        p.start()
        self.addCleanup(p.stop)
        self.app = _FakeApp(self.tmpdir, self.site, _keypair()[2])
        self.hub = _keypair()[2]

    def _landed(self, reply):
        rec = _Recorder(reply)
        with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", self.hub), \
                mock.patch.object(onionheaven.subprocess, "run", rec):
            result = onionheaven._heartbeat_landed(self.app, self.site)
        return result, rec.calls

    def test_the_query_is_a_signed_post_the_hub_accepts(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        landed, calls = self._landed(json.dumps({"entries": [{"last_healthy": now}]}))
        self.assertTrue(landed)
        self.assertEqual(len(calls), 1)
        argv, stdin = calls[0]
        self.assertEqual(argv[-1], f"http://{self.hub}:8083/status/{self.site}")
        self.assertIn("POST", argv)
        self.assertIn("--data-binary", argv)
        self.assertIn("-i", argv[:3], "the body goes on stdin")
        body = json.loads(stdin)
        self.assertEqual(body["content_address"], self.site)
        self.assertEqual(body["healthcheck_address"], "")
        self.assertEqual(onion_auth.verify_payload(
            self.site, ws.STATUS_ENDPOINT, "", body["timestamp"], body["signature"]), (True, ""))
        self.assertNotIn("arti_key_pem", body)

    def test_a_refusal_or_a_stale_beat_is_not_landed(self):
        self.assertFalse(self._landed(json.dumps({"error": "Entry not found"}))[0])
        stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(self._landed(json.dumps({"entries": [{"last_healthy": stale}]}))[0])
        self.assertFalse(self._landed(json.dumps({"registered": True, "status": "online"}))[0],
                         "the coarse answer says nothing about the beat")

    def test_another_keys_address_is_not_queried(self):
        rec = _Recorder("{}")
        with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", self.hub), \
                mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertFalse(onionheaven._heartbeat_landed(self.app, _keypair()[2]))
        self.assertEqual(rec.calls, [])


class ConsumerCensusTest(unittest.TestCase):
    """Every reader of /status/<address> in the code that ships."""

    def _read(self, rel):
        with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
            return f.read()

    def test_the_mac_client_signs_and_nothing_else_reads_the_detail(self):
        src = self._read("src/onionpress/onionheaven.py")
        body = src[src.index("def _heartbeat_landed("):src.index("def _send_heartbeat(")]
        self.assertIn("STATUS_ENDPOINT", body)
        self.assertIn('"POST"', body)
        self.assertEqual(src.count('_hub_url("status/'), 1)
        for rel in ("linux/onionpress-heartbeat.py", "linux/onionpress", "linux/onionpress-tray",
                    "app/MacOS/onionpress", "app/Resources/plugins/onionpress-status.php",
                    "app/Resources/docker/tor/onionheaven-heartbeat.py",
                    "tests/stress/worker-server.py", "tests/stress/worker-bootstrap.py",
                    "tests/stress/verify-worker.py"):
            with self.subTest(file=rel):
                text = self._read(rel)
                self.assertNotIn("8083/status/", text)
                self.assertNotIn("/status/\" +", text)
                self.assertNotIn("status/{", text.replace("status/{addr}", "") if "web-server" in rel else text)

    def test_the_server_names_both_forms(self):
        src = self._read("app/Resources/docker/tor/web-server.py")
        self.assertIn("GET  /status/<addr> — Coarse", src)
        self.assertIn("POST /status/<addr> — Per-address detail, signed", src)
        self.assertIn('path.startswith("/status/")', src[src.index("def do_POST"):])


if __name__ == "__main__":
    unittest.main()
