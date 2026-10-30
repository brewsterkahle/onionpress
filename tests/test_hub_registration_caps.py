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
import collections
import hashlib
import importlib.util
import json
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

    def _body_length(self, max_size):
        return 0  # do_POST's length gate: test_hub_request_limits.py

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


class BoundedGrowthTest(unittest.TestCase):
    """Issue #467: what a request may leave in the hub's memory is a line
    per address, for a bounded number of addresses."""

    def setUp(self):
        for target, name, value in ((oc, "log", lambda *a: None),
                                    (oc, "_addr_logs", collections.OrderedDict())):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_short_text(self):
        limit = oc.SHORT_TEXT_LIMIT
        self.assertEqual(limit, 64)
        self.assertEqual(oc.short_text("2.6.0"), "2.6.0")
        self.assertEqual(oc.short_text("v" * 1_000_000), "v" * limit)
        self.assertEqual(oc.short_text("a\nb\x00c\x1b[31md\te"), "abc[31mde")
        self.assertEqual(oc.short_text("\x00" * 1000 + "x"), "")  # looks no further than 4x limit
        self.assertEqual(oc.short_text(12), "12")
        self.assertEqual(oc.short_text(None), "")
        self.assertEqual(oc.short_text(None, default="unknown"), "unknown")
        self.assertEqual(oc.short_text(True), "")
        self.assertEqual(oc.short_text({"a": 1}), "")
        self.assertEqual(oc.short_text(["a"] * 1000, default="d"), "d")
        self.assertEqual(oc.short_text("abcdef", limit=3), "abc")

    def test_the_address_logs_are_an_lru_with_a_cap(self):
        cap = oc._ADDR_LOGS_MAX_ADDRESSES
        self.assertGreaterEqual(cap, ws.MAX_REGISTRANTS, "every registered address keeps its lines")
        for i in range(cap):
            oc.addr_log(f"a{i}", "x")
        self.assertEqual(len(oc._addr_logs), cap)
        oc.addr_log("a0", "y")  # written to again: the most recent now
        for i in range(5):
            oc.addr_log(f"b{i}", "x")
        self.assertEqual(len(oc._addr_logs), cap)
        self.assertEqual(oc.get_addr_logs("a0")[1][-1:], "y")
        for i in range(1, 6):
            self.assertEqual(oc.get_addr_logs(f"a{i}"), [], "the oldest went")
        self.assertEqual(len(oc.get_addr_logs("a6")), 1)
        for _ in range(oc._ADDR_LOG_MAX + 5):
            oc.addr_log("a6", "z")
        self.assertEqual(len(oc.get_addr_logs("a6")), oc._ADDR_LOG_MAX)


class AnalyticsQuotaTest(unittest.TestCase):
    """Issue #468: /logs/manifest and /logs/upload take a v3 healthcheck
    address only (with "" the files landed in the content address's own
    folder, where the per-instance quota never looked), hold the pair's
    folder to ANALYTICS_PER_INSTANCE_QUOTA before every write, and
    measure the analytics folder's disk, not the container's."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-analytics-quota-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.logged = []
        # The disk this runs on may well be past the threshold.
        self.real_disk_full = ws.analytics_disk_full
        for target, name, value in ((ws, "ANALYTICS_DIR", self.base),
                                    (ws, "_is_onionhome", lambda: True),
                                    (ws, "analytics_disk_full", lambda: False),
                                    (ws, "log", self.logged.append),
                                    (oc, "log", lambda *a: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.expanded, self.public, self.address = _keypair()
        self.hc = _keypair()[2]
        self.site = os.path.join(self.base, self.address, self.hc)

    def _signed(self, hc, **extra):
        ts = onion_auth.make_timestamp()
        data = {"content_address": self.address, "healthcheck_address": hc, "timestamp": ts,
                "signature": onion_auth.sign_payload(self.expanded, self.public, "logs",
                                                     self.address, hc, ts)}
        data.update(extra)
        return data

    def _post(self, path, data):
        h = _Handler(path, data)
        h.do_POST()
        self.assertEqual(len(h.replies), 1, h.replies)
        return h.replies[0]

    def _manifest(self, files, hc=None, **extra):
        hc = self.hc if hc is None else hc
        return self._post("/logs/manifest", self._signed(hc, files=files, **extra))

    def _upload(self, name, content, hc=None):
        hc = self.hc if hc is None else hc
        return self._post("/logs/upload", self._signed(
            hc, file_name=name, file_content=base64.b64encode(content).decode()))

    def _write(self, name, size, where=None):
        where = self.site if where is None else where
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, name), "wb") as f:
            f.write(b"x" * size)

    def test_an_empty_or_malformed_healthcheck_address_is_refused_before_the_signature(self):
        for hc in ("", "x.onion", 5, None, self.hc.upper()):
            for path, extra in (("/logs/manifest", {"files": [{"name": "a.log", "size": 1}]}),
                                ("/logs/upload", {"file_name": "a.log",
                                                  "file_content": base64.b64encode(b"x").decode()})):
                with self.subTest(hc=hc, path=path):
                    unsigned = {"content_address": self.address, "healthcheck_address": hc,
                                **extra}
                    status, body = self._post(path, unsigned)
                    self.assertEqual(status, 400, body)
                    self.assertEqual(body["error"], "Invalid healthcheck_address")
        # Signed for "" by the key itself: refused the same way.
        self.assertEqual(self._manifest([{"name": "a.log", "size": 1}], hc="")[0], 400)
        self.assertEqual(self._upload("a.log", b"x", hc="")[0], 400)
        self.assertEqual(os.listdir(self.base), [], "nothing written")

    def test_an_upload_past_the_pairs_quota_is_refused(self):
        quota = 1000
        with mock.patch.object(ws, "ANALYTICS_PER_INSTANCE_QUOTA", quota):
            self._write("old.log", 900)
            status, body = self._upload("new.log", b"n" * 200)
            self.assertEqual(status, 507, body)
            self.assertIn("quota", body["error"])
            self.assertFalse(os.path.exists(os.path.join(self.site, "new.log")))
            self.assertEqual(self._upload("new.log", b"n" * 50)[0], 200)
            # Replacing a file frees its size first (a grown .log). The
            # audit lines count too, so the room is measured.
            room = quota - ws.analytics_pair_bytes(self.site) + 900
            self.assertEqual(self._upload("old.log", b"o" * room)[0], 200)
            room = quota - ws.analytics_pair_bytes(self.site) + room
            self.assertEqual(self._upload("old.log", b"o" * (room + 1))[0], 507)
        self.assertEqual(self._upload("big.log", b"b" * 5000)[0], 200, "the real quota")

    def test_a_manifest_past_the_quota_writes_nothing(self):
        with mock.patch.object(ws, "ANALYTICS_PER_INSTANCE_QUOTA", 1000):
            self._write("old.log", 1000)
            status, body = self._manifest([{"name": "a.log", "size": 1}])
            self.assertEqual(status, 507, body)
            self.assertEqual(os.listdir(self.site), ["old.log"], "no meta, no audit")
            os.remove(os.path.join(self.site, "old.log"))
            status, body = self._manifest([{"name": "a.log", "size": 1}])
            self.assertEqual(status, 200, body)
            self.assertEqual(body["wanted"], ["a.log"])
            self.assertTrue(any(n.startswith("meta-") for n in os.listdir(self.site)))

    def test_the_disk_check_measures_the_analytics_folder(self):
        asked = []

        def disk_usage(path):
            asked.append(path)
            return shutil._ntuple_diskusage(total=100, used=90, free=10)  # 90 %: full
        cleaned = []
        with mock.patch.object(ws.shutil, "disk_usage", disk_usage), \
                mock.patch.object(ws, "analytics_disk_full", self.real_disk_full), \
                mock.patch.object(ws.OnionHeavenHandler, "_analytics_cleanup",
                                  staticmethod(lambda: cleaned.append(1))):
            status, body = self._manifest([{"name": "a.log", "size": 1}])
            self.assertEqual((status, body), (200, {"wanted": []}))
            status, body = self._upload("a.log", b"x")
            self.assertEqual(status, 507, body)
            self.assertEqual(body["error"], "Disk full")
            self.assertEqual(cleaned, [1])
        self.assertTrue(asked)
        self.assertEqual(set(asked), {self.base})
        self.assertFalse(os.path.exists(self.site))
        src = open(os.path.join(DOCKER_TOR_DIR, "web-server.py"), encoding="utf-8").read()
        self.assertNotIn('disk_usage("/")', src)
        self.assertEqual(src.count("shutil.disk_usage("), 1, "one reader: analytics_disk_full")

    def test_a_missing_folder_counts_as_not_full(self):
        with mock.patch.object(ws, "ANALYTICS_DIR", os.path.join(self.base, "absent")):
            self.assertFalse(self.real_disk_full())

    def test_the_cleanup_holds_stray_files_to_the_quota_too(self):
        """Files an older server left directly under the content address."""
        content = os.path.join(self.base, self.address)
        with mock.patch.object(ws, "ANALYTICS_PER_INSTANCE_QUOTA", 100):
            self._write("older.log", 60, where=content)
            self._write("newer.log", 60, where=content)
            os.utime(os.path.join(content, "older.log"), (1_000_000, 1_000_000))
            self._write("pair.log", 60)  # the pair's own, under quota
            ws.OnionHeavenHandler._analytics_cleanup()
        self.assertEqual(sorted(os.listdir(content)), sorted([self.hc, "newer.log"]))
        self.assertEqual(os.listdir(self.site), ["pair.log"])

    def test_odd_file_entries_do_not_break_a_manifest(self):
        files = [1, "x", {"name": 5}, {"name": "a.log", "size": "big"},
                 {"name": "b.log", "size": 3}, {"name": "../c", "size": 1}]
        status, body = self._manifest(files, version="v" * 1000, os_version="mac\nos")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wanted"], ["a.log", "b.log"])
        names = os.listdir(self.site)
        audit = [n for n in names if n.startswith("audit-")]
        meta = [n for n in names if n.startswith("meta-")]
        self.assertEqual((len(audit), len(meta)), (1, 1))
        with open(os.path.join(self.site, audit[0])) as f:
            record = json.loads(f.read().splitlines()[0])
        self.assertEqual(record["offered"], ["<empty>", "<empty>", "5", "a.log", "b.log", "../c"])
        self.assertEqual(record["rejected"], ["<empty>", "<empty>", "5", "../c"])
        self.assertEqual(record["version"], "v" * oc.SHORT_TEXT_LIMIT)
        with open(os.path.join(self.site, meta[0])) as f:
            stored = json.load(f)
        self.assertEqual(stored["version"], "v" * oc.SHORT_TEXT_LIMIT)
        self.assertEqual(stored["os_version"], "macos")
        self.assertEqual(self._manifest([{"name": "a.log", "size": 1}] * (ws.MAX_MANIFEST_FILES + 1))[0], 400)
        self.assertEqual(self._manifest({"name": "a.log"})[0], 400)


if __name__ == "__main__":
    unittest.main()
