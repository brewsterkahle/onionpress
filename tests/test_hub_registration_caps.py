"""Issue #391: a free registration is bounded.

Minting a key is instant, and nothing capped what one Tor client could
take from the hub: a registry row and key dir per (content, healthcheck)
pair with pairs unlimited, a worker slot per /offline, names without
count on OnionHome, 500 MB of analytics per pair with pairs unlimited.
Now web-server.py's registration_room refuses, before anything is
verified or written, a new pair for an address at MAX_ROWS_PER_ADDRESS
and a new address on a hub at MAX_REGISTRANTS; analytics_room refuses a
new analytics pair past ANALYTICS_MAX_PAIRS_PER_ADDRESS per address or
ANALYTICS_MAX_INSTANCES in all; onionnames.register_name refuses the
MAX_NAMES_PER_ADDRESS+1th name of an address. A pair that exists is
never refused by these.
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _load("web_server_registration_caps", "web-server.py")
import onionheaven_common as oc  # noqa: E402
import onionnames  # noqa: E402
from onionpress import onion_auth  # noqa: E402
from nacl import bindings as sodium  # noqa: E402


def _keypair():
    seed = os.urandom(32)
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    public = sodium.crypto_sign_seed_keypair(seed)[0]
    return bytes(h), public, onion_auth.derive_onion_address(public)


class _Handler(ws.OnionHeavenHandler):
    def __init__(self, path, data):  # noqa: D401
        self.path = path
        self.client_address = ("127.0.0.1", 0)
        self._data = data
        self.replies = []

    def _refuse_unless_hub(self):
        return False

    def _read_json(self, max_size=None):
        return self._data

    def _send_json(self, status_code, data):
        self.replies.append((status_code, data))


class OnlineCapsTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hub-caps-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.logged = []
        self.acted = []
        uri = f"file:caps-{id(self)}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)
        self.addCleanup(self.keeper.close)

        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        for target, name, value in (
                (ws, "db_connect", connect),
                (ws, "write_key_file", lambda *a, **k: self.acted.append("write_key_file")),
                (ws, "release_function", lambda *a, **k: self.acted.append("release")),
                (ws, "KEYS_DIR", os.path.join(self.base, "keys")),
                (ws, "_get_own_address", lambda: None),
                (ws, "log", self.logged.append),
                (oc, "log", lambda *a: None),
                (oc, "addr_log", lambda *a: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        conn = connect()
        oc.db_ensure_schema(conn)
        conn.close()

    def _online(self, expanded, public, address, hc):
        ts = onion_auth.make_timestamp()
        data = {"content_address": address, "healthcheck_address": hc, "timestamp": ts,
                "signature": onion_auth.sign_payload(expanded, public, "online", address, hc, ts),
                "arti_key_pem": base64.b64encode(b"unused").decode()}
        h = _Handler("/online", data)
        h.do_POST()
        self.assertEqual(len(h.replies), 1)
        return h.replies[0]

    def _row(self, address, hc, unregistered=False):
        self.keeper.execute(
            "INSERT INTO registry (content_address, healthcheck_address, registered_at, "
            "last_healthy, status, unregistered_at) VALUES (?, ?, 'x', 'x', 'online', ?)",
            (address, hc, "2026-01-01T00:00:00Z" if unregistered else None))
        self.keeper.commit()

    def test_a_fifth_healthcheck_address_is_refused_before_any_write(self):
        expanded, public, address = _keypair()
        for _ in range(ws.MAX_ROWS_PER_ADDRESS):
            self._row(address, _keypair()[2])
        status, body = self._online(expanded, public, address, _keypair()[2])
        self.assertEqual(status, 429, body)
        self.assertIn("unregister one first", body["error"])
        self.assertEqual(self.acted, [], "no key written, nothing released")
        self.assertEqual(self.keeper.execute(
            "SELECT COUNT(*) FROM registry WHERE content_address = ?", (address,)).fetchone()[0],
            ws.MAX_ROWS_PER_ADDRESS, "no row added")
        self.assertTrue(any("REJECT-CAPS /online" in m for m in self.logged))

    def test_an_existing_pair_heartbeats_past_the_row_cap(self):
        expanded, public, address = _keypair()
        hcs = [_keypair()[2] for _ in range(ws.MAX_ROWS_PER_ADDRESS)]
        for hc in hcs:
            self._row(address, hc)
        status, body = self._online(expanded, public, address, hcs[0])
        self.assertNotEqual(status, 429, body)     # past the cap (the fake PEM fails later)
        self.assertNotEqual(status, 503, body)

    def test_unregistered_rows_do_not_count(self):
        expanded, public, address = _keypair()
        for _ in range(ws.MAX_ROWS_PER_ADDRESS + 2):
            self._row(address, _keypair()[2], unregistered=True)
        status, body = self._online(expanded, public, address, _keypair()[2])
        self.assertNotIn(status, (429, 503), body)

    def test_a_full_hub_takes_no_new_address_but_keeps_the_registered(self):
        with mock.patch.object(ws, "MAX_REGISTRANTS", 2):
            a = _keypair()
            b = _keypair()
            self._row(a[2], _keypair()[2])
            self._row(b[2], _keypair()[2])
            expanded, public, address = _keypair()
            status, body = self._online(expanded, public, address, _keypair()[2])
            self.assertEqual(status, 503, body)
            self.assertIn("hub is full", body["error"])
            self.assertEqual(self.acted, [])
            # A registered address, even with a new healthcheck address, has room.
            status, body = self._online(a[0], a[1], a[2], _keypair()[2])
            self.assertNotIn(status, (429, 503), body)

    def test_defaults(self):
        self.assertEqual(ws.MAX_ROWS_PER_ADDRESS, 4)
        self.assertEqual(ws.MAX_REGISTRANTS, 5000)
        self.assertEqual(ws.ANALYTICS_MAX_PAIRS_PER_ADDRESS, 4)
        self.assertEqual(ws.ANALYTICS_MAX_INSTANCES, 10000)
        self.assertEqual(onionnames.MAX_NAMES_PER_ADDRESS, 10)

    def test_the_room_check_precedes_the_signature_check(self):
        src = open(os.path.join(DOCKER_TOR_DIR, "web-server.py"), encoding="utf-8").read()
        body = src[src.index("def _handle_online(self):"):src.index("def _handle_offline(self):")]
        # After the signature (the registry is opened for no unverified
        # request), before the key is looked at or anything is stored.
        self.assertLess(body.index('_verify_signature(self, data, endpoint)'),
                        body.index("registration_room(room_conn"))
        self.assertLess(body.index("registration_room(room_conn"),
                        body.index('data.get("arti_key_pem")'))
        for fn in ("_handle_logs_manifest", "_handle_logs_upload"):
            b = src[src.index(f"def {fn}(self):"):]
            b = b[:b.index("\n    def ", 10)]
            with self.subTest(handler=fn):
                self.assertLess(b.index("analytics_room(content_address, healthcheck_address)"),
                                b.index("os.makedirs(site_dir, exist_ok=True)"))


class AnalyticsRoomTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-analytics-room-")
        self.addCleanup(shutil.rmtree, self.base, True)

    def _pair(self, addr, hc):
        os.makedirs(os.path.join(self.base, addr, hc))

    def test_per_address_pairs(self):
        for i in range(ws.ANALYTICS_MAX_PAIRS_PER_ADDRESS):
            self._pair("site-a", f"hc{i}")
        self.assertEqual(ws.analytics_room("site-a", "hc-new", self.base)[0], 429)
        self.assertEqual(ws.analytics_room("site-a", "hc0", self.base), (None, None))
        self.assertEqual(ws.analytics_room("site-b", "hc0", self.base), (None, None))

    def test_total_instances(self):
        with mock.patch.object(ws, "ANALYTICS_MAX_INSTANCES", 3):
            self._pair("a", "1")
            self._pair("b", "1")
            self._pair("c", "1")
            self.assertEqual(ws.analytics_room("d", "1", self.base)[0], 507)
            self.assertEqual(ws.analytics_room("a", "1", self.base), (None, None))
        self.assertEqual(ws.analytics_room("d", "1", self.base), (None, None))

    def test_links_and_files_are_not_pairs(self):
        self._pair("a", "1")
        os.symlink(os.path.join(self.base, "a", "1"), os.path.join(self.base, "a", "link"))
        with open(os.path.join(self.base, "a", "file"), "w") as f:
            f.write("x")
        with mock.patch.object(ws, "ANALYTICS_MAX_PAIRS_PER_ADDRESS", 2):
            self.assertEqual(ws.analytics_room("a", "2", self.base), (None, None))


class NamesPerAddressTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        onionnames.db_init(self.conn)
        self.addr = _keypair()[2]

    def test_the_eleventh_name_is_too_many(self):
        for i in range(onionnames.MAX_NAMES_PER_ADDRESS):
            ok, reason, _ = onionnames.register_name(self.conn, f"mysite{i}", self.addr)
            self.assertTrue(ok, reason)
        ok, reason, alts = onionnames.register_name(self.conn, "onemore", self.addr)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_many")
        self.assertIsNone(alts)
        # Another address is not affected.
        self.assertTrue(onionnames.register_name(self.conn, "onemore", _keypair()[2])[0])

    def test_local_register_answers_429(self):
        tmp = tempfile.mkdtemp(prefix="op-names-")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "onionnames.db")
        with mock.patch.object(onionnames, "MAX_NAMES_PER_ADDRESS", 1):
            self.assertEqual(onionnames.local_register("first", self.addr, db_path=db)[0], 201)
            status, body = onionnames.local_register("second", self.addr, db_path=db)
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "too_many")


if __name__ == "__main__":
    unittest.main()
