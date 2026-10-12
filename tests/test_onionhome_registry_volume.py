#!/usr/bin/env python3
"""OnionHome's name registry has a volume of its own, which WordPress never
mounts.

onionnames.db, which onionname points at which onion address for every
lookup and follow by name, lived in onionhome/ on onionpress-persistent-data,
mounted at /var/lib/onionpress, which the wordpress container mounts
read-write too (www-data owns that folder, and fix_onionpress_permissions
handed it onionhome/ as well). Code running in WordPress on OnionHome could
point any name at an onion address of its choosing, or delete the registry.
Now:

  * onionpress-onionnames-data, mounted at /var/lib/onionnames by
    onionpress-tor alone: web-server.py is its only reader and writer, and
    WordPress looks names up through /api/name/lookup.
  * onionhome-registry-migrate.py (tor image, run by onionpress-tor's
    entrypoint) moves the old registry over, reading the old location as
    untrusted: only the db and its journals, by O_NOFOLLOW descriptor,
    size-capped, into a private copy SQLite then reads; the new registry
    holds only rows that pass onionnames.py's checks. A registry that cannot
    be moved never stops the container.
  * web-server.py serves no name until the move's marker is on the volume:
    a registry opened anywhere else would start empty, every name free to
    claim.
  * Backup and restore copy the registry's own files through onionpress-tor.

No PyYAML and no Docker: the compose file is read with a line-based
splitter, subprocess is mocked, and the move runs on temporary directories.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionnames  # noqa: E402
from onionpress import backup  # noqa: E402

COMPOSE = "app/Resources/docker/docker-compose.yml"
MIGRATE = os.path.join(DOCKER_TOR_DIR, "onionhome-registry-migrate.py")
VOLUME = "onionpress-onionnames-data"
NAMES_DIR = "/var/lib/onionnames"
LEGACY_PATH = "/var/lib/onionpress/onionhome"
MARKER = ".migrated-from-persistent-data"
ALICE = "a" * 56 + ".onion"
BOB = "b" * 56 + ".onion"
MALLORY = "m" * 56 + ".onion"
SECRET = "root-only secret\n"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = _load("web_server_onionnames_volume", os.path.join(DOCKER_TOR_DIR, "web-server.py"))
mig = _load("onionhome_registry_migrate", MIGRATE)


def _services(text):
    """{service: block text} for a compose file, comment lines dropped."""
    services, current, in_services = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = []
        elif current:
            services[current].append(line)
    return {name: "\n".join(lines) for name, lines in services.items()}


def _volume_items(block):
    m = re.search(r"^    volumes:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    return [] if not m else [l.strip()[2:].strip() for l in m.group(1).splitlines()]


def _has_fts5():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _code_files():
    for top in ("app", "src", "linux", "build", "tests"):
        for root, dirs, files in os.walk(os.path.join(PROJECT_ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            for name in files:
                if not name.endswith((".png", ".icns", ".jpg", ".gif", ".zip", ".pyc")):
                    yield os.path.join(root, name)


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

class TestCompose(unittest.TestCase):

    def setUp(self):
        self.text = _read(COMPOSE)
        self.services = _services(self.text)

    def test_volume_is_named(self):
        self.assertRegex(self.text, rf"(?m)^  onionnames-data:\n    name: {VOLUME}$")

    def test_only_tor_mounts_it(self):
        mounting = {name for name, block in self.services.items()
                    if any(item.split(":", 1)[0] == "onionnames-data"
                           for item in _volume_items(block))}
        self.assertEqual(mounting, {"tor"})
        self.assertIn(f"onionnames-data:{NAMES_DIR}", _volume_items(self.services["tor"]))

    def test_wordpress_never_sees_it(self):
        wp = self.services["wordpress"]
        for needle in ("onionnames-data", NAMES_DIR, VOLUME, "onionnames.db"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, wp)

    def test_other_compose_files_leave_it_alone(self):
        text = _read("app/Resources/docker/docker-compose.cloudflare.yml")
        self.assertNotIn("onionnames-data", text)
        self.assertNotIn(NAMES_DIR, text)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

class TestPaths(unittest.TestCase):

    def test_constants_agree(self):
        self.assertEqual(onionnames.DATA_DIR, NAMES_DIR)
        self.assertEqual(onionnames.DB_PATH, f"{NAMES_DIR}/onionnames.db")
        self.assertEqual(mig.DEST, NAMES_DIR)
        self.assertEqual(f"{mig.LEGACY_PARENT}/{mig.LEGACY_NAME}", LEGACY_PATH)
        self.assertEqual(mig.DB, os.path.basename(onionnames.DB_PATH))
        self.assertEqual(onionnames.VOLUME_MARKER, os.path.join(NAMES_DIR, mig.MARKER))
        self.assertEqual(mig.MARKER, MARKER)
        self.assertEqual(backup.ONIONHOME_REGISTRY_DIR, NAMES_DIR)
        self.assertEqual(backup.ONIONHOME_REGISTRY_FILES,
                         ("onionnames.db", "onionnames.db-wal"))

    def test_the_old_path_is_gone(self):
        me = os.path.abspath(__file__)
        offenders = []
        for path in _code_files():
            if os.path.abspath(path) == me:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            if LEGACY_PATH in text:
                offenders.append(os.path.relpath(path, PROJECT_ROOT))
        self.assertEqual(offenders, [], f"the name registry is at {NAMES_DIR} now")

    def test_no_second_way_into_the_volume(self):
        """The move is the only thing that reads the old location."""
        self.assertFalse(hasattr(onionnames, "migrate_legacy_db"))
        self.assertNotIn("migrate_legacy_db", _read("app/Resources/docker/tor/web-server.py"))

    def test_the_hub_data_script_is_not_involved(self):
        """The registry's move is its own script, apart from the hub data's."""
        self.assertNotIn("onionhome", _read("app/Resources/docker/tor/onionheaven-data-migrate.py"))

    def test_wordpress_side_code_does_not_know_it(self):
        for top in ("app/Resources/plugins", "app/Resources/themes",
                    "app/Resources/docker/wordpress"):
            for root, _, files in os.walk(os.path.join(PROJECT_ROOT, top)):
                for name in files:
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    with self.subTest(file=os.path.relpath(path, PROJECT_ROOT)):
                        self.assertNotIn(NAMES_DIR, text)
                        self.assertNotIn("onionnames.db", text)

    def test_the_directory_plugin_asks_the_api(self):
        php = _read("app/Resources/plugins/onionpress-directory.php")
        self.assertIn("'http://onionpress-tor:8083/api/name/lookup/'", php)

    def test_host_code_reaches_it_through_onionpress_tor(self):
        src = _read("src/onionpress/backup.py")
        containers = re.compile(r"onionpress-wordpress|onionpress-tor|onionheaven(?=[\"':\s])")
        checked = 0
        for m in re.finditer(r"\{ONIONHOME_REGISTRY_DIR\}", src):
            found = containers.findall(src[max(0, m.start() - 300):m.start()])
            with self.subTest(at=m.start()):
                self.assertTrue(found)
                self.assertEqual(found[-1], "onionpress-tor")
            checked += 1
        self.assertGreaterEqual(checked, 3)


# ---------------------------------------------------------------------------
# Image: entrypoint and Dockerfile
# ---------------------------------------------------------------------------

class TestImage(unittest.TestCase):

    def setUp(self):
        self.entrypoint = _read("app/Resources/docker/tor/entrypoint.sh")
        self.main_mode = self.entrypoint[
            self.entrypoint.index("# ==================== Onion service mode"):]

    def test_dockerfile_ships_the_script_and_the_mount_point(self):
        dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        self.assertIn("COPY onionhome-registry-migrate.py /onionhome-registry-migrate.py",
                      dockerfile)
        self.assertIn(f"RUN mkdir -m 700 {NAMES_DIR}", dockerfile)

    def test_a_registry_that_cannot_move_never_stops_the_container(self):
        fn = self.entrypoint[self.entrypoint.index("migrate_onionhome_registry() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("python3 /onionhome-registry-migrate.py", fn)
        self.assertNotIn("exit", fn)

    def test_tor_moves_it_before_the_api_server(self):
        self.assertLess(self.main_mode.index("\nmigrate_onionhome_registry\n"),
                        self.main_mode.index("python3 /web-server.py"))

    def test_only_the_onion_service_container_moves_it(self):
        """onionheaven and the takeover workers do not mount the volume."""
        before = self.entrypoint[:len(self.entrypoint) - len(self.main_mode)]
        self.assertEqual(before.count("migrate_onionhome_registry"), 1)  # the definition


# ---------------------------------------------------------------------------
# web-server.py serves names only from the registry's volume
# ---------------------------------------------------------------------------

class TestRegistryGuard(unittest.TestCase):

    UNAVAILABLE = (503, {"error": "Name registry unavailable"})

    def _handler(self):
        h = object.__new__(ws.OnionHeavenHandler)
        h.path = "/api/name/check?name=alice"
        h.client_address = ("172.18.0.3", 40000)
        h.sent = []
        h._send_json = lambda status, data: h.sent.append((status, data))
        h._passthrough = lambda status, body: h.sent.append((status, body))
        h._read_json = lambda *a, **kw: {"onionname": "alice", "onionaddress": ALICE,
                                         "timestamp": "0", "signature": "x"}
        return h

    def _entry_points(self):
        return {
            "suggest": lambda h: h._handle_name_suggest(),
            "check": lambda h: h._handle_name_check(),
            "lookup": lambda h: h._handle_name_lookup("alice"),
            "register": lambda h: h._handle_name_register(),
            "release": lambda h: h._handle_name_release(),
            "register-local": lambda h: h._handle_name_local("register"),
            "release-local": lambda h: h._handle_name_local("release"),
        }

    def test_registry_in_place_needs_the_marker(self):
        tmp = tempfile.mkdtemp(prefix="op-names-")
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = os.path.join(tmp, MARKER)
        self.assertFalse(onionnames.registry_in_place(marker))
        open(marker, "w").close()
        self.assertTrue(onionnames.registry_in_place(marker))

    def test_without_the_marker_no_name_is_served_or_changed(self):
        opened = AssertionError("the registry was opened")
        with mock.patch.object(ws, "_is_onionhome", return_value=True), \
                mock.patch.object(ws, "_get_own_address", return_value=ALICE), \
                mock.patch.object(ws.onionnames, "registry_in_place", return_value=False), \
                mock.patch.object(ws.onionnames, "db_connect", side_effect=opened), \
                mock.patch.object(ws.onionnames, "local_register", side_effect=opened), \
                mock.patch.object(ws.onionnames, "local_release", side_effect=opened):
            for endpoint, call in self._entry_points().items():
                h = self._handler()
                call(h)
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(h.sent, [self.UNAVAILABLE])

    def test_with_the_marker_names_are_served(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        onionnames.db_init(conn)
        onionnames.register_name(conn, "alice", ALICE)
        with mock.patch.object(ws, "_is_onionhome", return_value=True), \
                mock.patch.object(ws.onionnames, "registry_in_place", return_value=True), \
                mock.patch.object(ws.onionnames, "db_connect", return_value=conn):
            h = self._handler()
            h._handle_name_lookup("alice")
        self.assertEqual(h.sent[0][0], 200)
        self.assertEqual(h.sent[0][1]["onionaddress"], ALICE)

    def test_off_onionhome_it_is_still_404(self):
        with mock.patch.object(ws, "_is_onionhome", return_value=False):
            for endpoint, call in self._entry_points().items():
                if endpoint.endswith("-local"):
                    continue    # off OnionHome these sign and forward
                h = self._handler()
                call(h)
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(h.sent, [(404, {"error": "Not found"})])

    def test_every_handler_that_opens_the_registry_checks_first(self):
        src = _read("app/Resources/docker/tor/web-server.py")
        cls = src[src.index("class OnionHeavenHandler"):src.index("\ndef main():")]
        checked = 0
        for method in re.split(r"\n    def ", cls)[1:]:
            name = method.split("(", 1)[0]
            uses = [m.start() for m in re.finditer(
                r"onionnames\.(db_connect|local_register|local_release)\(", method)]
            if not uses or name == "_name_registry_ready":
                continue
            checked += 1
            with self.subTest(method=name):
                self.assertIn("self._name_registry_ready()", method)
                self.assertLess(method.index("self._name_registry_ready()"), min(uses))
        self.assertGreaterEqual(checked, 6)

    def test_startup_opens_the_registry_only_when_in_place(self):
        src = _read("app/Resources/docker/tor/web-server.py")
        main = src[src.index("\ndef main():"):]
        guard = main.index("onionnames.registry_in_place()")
        self.assertLess(guard, main.index("onionnames.db_connect()"))
        self.assertLess(guard, main.index("onionnames.start_refresh_thread()"))


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------

class MoveCase(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-names-move-")
        self.addCleanup(self._cleanup)
        self.parent = os.path.join(self.base, "persistent")   # /var/lib/onionpress
        self.legacy = os.path.join(self.parent, "onionhome")
        self.dest = os.path.join(self.base, "names")          # /var/lib/onionnames
        os.makedirs(self.parent)
        os.makedirs(self.dest)
        with open(os.path.join(self.parent, "status.json"), "w") as f:
            f.write("{}")
        self.secret = os.path.join(self.base, "secret.txt")
        with open(self.secret, "w") as f:
            f.write(SECRET)

    def _cleanup(self):
        for root, dirs, files in os.walk(self.base):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o700, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(self.base, ignore_errors=True)

    def run_script(self, *extra, mount_check=False):
        argv = [sys.executable, MIGRATE, "--legacy-parent", self.parent, "--dest", self.dest]
        if not mount_check:
            argv.append("--no-mount-check")
        result = subprocess.run(argv + list(extra), capture_output=True, text=True, timeout=60)
        return result.returncode, result.stderr

    def make_registry(self, names=(("alice", ALICE), ("brewster", BOB)), root=None,
                      keep_open=False, extra_sql=()):
        root = root or self.legacy
        os.makedirs(root, exist_ok=True)
        conn = onionnames.db_connect(os.path.join(root, "onionnames.db"))
        onionnames.db_init(conn)
        for name, address in names:
            ok, reason, _ = onionnames.register_name(conn, name, address)
            self.assertTrue(ok, reason)
        conn.execute("INSERT INTO dynamic_reservations VALUES ('about-us', 'wp-pages', "
                     "'2026-10-01T00:00:00Z')")
        for sql in extra_sql:
            conn.execute(sql)
        conn.commit()
        if keep_open:
            self.addCleanup(conn.close)
        else:
            conn.close()

    def names(self, root=None):
        conn = sqlite3.connect(os.path.join(root or self.dest, "onionnames.db"))
        try:
            return dict(conn.execute("SELECT name, onionaddress FROM onionnames"))
        finally:
            conn.close()

    def marker(self):
        with open(os.path.join(self.dest, MARKER)) as f:
            return json.load(f)

    def in_place(self):
        return onionnames.registry_in_place(os.path.join(self.dest, MARKER))

    def tree(self, root):
        out = {}
        for r, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(r, name)
                st = os.lstat(path)
                rel = os.path.relpath(path, root)
                if stat.S_ISLNK(st.st_mode):
                    out[rel] = ("link", os.readlink(path))
                elif stat.S_ISDIR(st.st_mode):
                    out[rel] = ("dir", st.st_mode & 0o777)
                else:
                    with open(path, "rb") as f:
                        out[rel] = ("file", st.st_mode & 0o777,
                                    hashlib.sha256(f.read()).hexdigest())
        return out

    def volume_names(self):
        return sorted(os.listdir(self.dest))


class TestMove(MoveCase):

    def test_fresh_install_marks_the_volume(self):
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.marker()["moved"])
        self.assertTrue(self.in_place())
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)

    def test_moves_the_registry(self):
        self.make_registry(keep_open=True)       # leaves -wal and -shm behind
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "onionnames.db-wal")))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("moved the OnionHome name registry to its own volume: 2 names, "
                      "1 reserved page names, 0 invalid rows left out", err)
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB})
        self.assertEqual(self.marker()["names"], 2)
        self.assertTrue(self.in_place())
        # Only the registry, the marker and the lock; nothing kept aside.
        self.assertEqual(self.volume_names(), [".migrate.lock", MARKER, "onionnames.db"])
        for name in self.volume_names():
            with self.subTest(name=name):
                self.assertEqual(os.stat(os.path.join(self.dest, name)).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)
        self.assertFalse(os.path.lexists(self.legacy), "the old directory is removed")
        self.assertTrue(os.path.exists(os.path.join(self.parent, "status.json")))

    def test_the_new_registry_holds_only_valid_rows_and_its_own_schema(self):
        self.make_registry(extra_sql=(
            "INSERT INTO onionnames VALUES ('a b', 'a b', '%s', 'x', NULL)" % ALICE,
            "INSERT INTO onionnames VALUES ('carol', 'carol', 'carol.onion', 'x', NULL)",
            "INSERT INTO onionnames VALUES ('dave1', 'dave1', '%s', 'yesterday', 7)" % BOB,
            "INSERT INTO dynamic_reservations VALUES ('caf%c3%a9', 'wp-pages', "
            "'2026-10-01T00:00:00Z')",
            "CREATE TABLE extra (x)",
            "CREATE TRIGGER t AFTER INSERT ON onionnames BEGIN DELETE FROM onionnames; END",
            "CREATE VIEW v AS SELECT 1",
        ))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("2 invalid rows left out", err)
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB, "dave1": BOB})
        conn = sqlite3.connect(os.path.join(self.dest, "onionnames.db"))
        try:
            objects = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
            dave = conn.execute("SELECT registered_at, last_seen_at FROM onionnames "
                                "WHERE name_lower = 'dave1'").fetchone()
            reserved = [row[0] for row in conn.execute(
                "SELECT name_lower FROM dynamic_reservations")]
        finally:
            conn.close()
        self.assertEqual(objects, {"onionnames", "onionnames_by_address",
                                   "dynamic_reservations"})
        self.assertRegex(dave[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(dave[1])
        self.assertEqual(reserved, ["about-us"])

    def test_the_hub_data_is_not_touched(self):
        self.make_registry()
        hub = os.path.join(self.parent, "onionheaven")
        os.makedirs(os.path.join(hub, "keys", ALICE))
        with open(os.path.join(hub, "keys", ALICE, "key"), "w") as f:
            f.write("PEM")
        before = self.tree(hub)
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(self.tree(hub), before)

    def test_what_is_not_a_registry_is_left_alone_and_never_read_again(self):
        cases = {
            "a text file": lambda: self._write_old("onionnames.db", "not a database"),
            "a database without the table": lambda: self._plain_sqlite(),
            "a link to a database": lambda: self._linked_db(),
            "a folder": lambda: os.makedirs(os.path.join(self.legacy, "onionnames.db")),
        }
        if _has_fts5():
            cases["a virtual table"] = lambda: self._virtual_table()
        for label, plant in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.legacy, ignore_errors=True)
                for name in os.listdir(self.dest):
                    os.unlink(os.path.join(self.dest, name))
                os.makedirs(self.legacy)
                plant()
                before = self.tree(self.legacy)
                rc, err = self.run_script()
                self.assertEqual(rc, 0, err)
                self.assertIn("marked this volume as current", err)
                self.assertTrue(self.in_place())
                self.assertFalse(os.path.exists(os.path.join(self.dest, "onionnames.db")))
                self.assertEqual(self.tree(self.legacy), before)
                self.assertEqual(self.run_script()[0], 0)
                self.assertFalse(os.path.exists(os.path.join(self.dest, "onionnames.db")))
                self.assertEqual(self.tree(self.legacy), before)

    def _write_old(self, name, data):
        with open(os.path.join(self.legacy, name), "w") as f:
            f.write(data)

    def _plain_sqlite(self):
        conn = sqlite3.connect(os.path.join(self.legacy, "onionnames.db"))
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
        conn.close()

    def _linked_db(self):
        crafted = os.path.join(self.base, "crafted")
        self.make_registry(names=(("alice", MALLORY),), root=crafted)
        os.symlink(os.path.join(crafted, "onionnames.db"),
                   os.path.join(self.legacy, "onionnames.db"))

    def _virtual_table(self):
        conn = sqlite3.connect(os.path.join(self.legacy, "onionnames.db"))
        conn.execute("CREATE VIRTUAL TABLE onionnames USING fts5("
                     "name, onionaddress, registered_at, last_seen_at)")
        conn.execute("INSERT INTO onionnames VALUES ('alice', ?, NULL, NULL)", (MALLORY,))
        conn.commit()
        conn.close()

    def test_a_symlinked_old_directory_is_not_followed(self):
        crafted = os.path.join(self.base, "crafted")
        self.make_registry(names=(("alice", MALLORY),), root=crafted)
        os.symlink(crafted, self.legacy)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("not a directory", err)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "onionnames.db")))
        self.assertTrue(os.path.islink(self.legacy))
        self.assertTrue(os.path.exists(os.path.join(crafted, "onionnames.db")))

    def test_a_linked_journal_is_never_read(self):
        self.make_registry()
        os.symlink(self.secret, os.path.join(self.legacy, "onionnames.db-wal"))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("left onionnames.db-wal at the old location out (not a regular file)",
                      err)
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB})
        for root, _, files in os.walk(self.dest):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(SECRET.encode(), f.read())
        with open(self.secret) as f:
            self.assertEqual(f.read(), SECRET)

    def test_huge_and_sparse_files_are_not_read(self):
        for label, size in (("larger than", mig.MAX_BYTES + 1), ("sparse", 32 << 20)):
            with self.subTest(label):
                shutil.rmtree(self.legacy, ignore_errors=True)
                for name in os.listdir(self.dest):
                    os.unlink(os.path.join(self.dest, name))
                os.makedirs(self.legacy)
                path = os.path.join(self.legacy, "onionnames.db")
                with open(path, "wb") as f:
                    f.write(mig.SQLITE_HEADER)
                    f.truncate(size)
                if os.stat(path).st_blocks * 512 >= size // 2:
                    self.skipTest("this filesystem does not make sparse files")
                rc, err = self.run_script()
                self.assertEqual(rc, 0, err)
                self.assertIn(label, err)
                self.assertTrue(self.in_place())
                self.assertFalse(os.path.exists(os.path.join(self.dest, "onionnames.db")))

    def test_a_damaged_registry_stays_and_names_stay_unavailable(self):
        self.make_registry(names=[(f"name{i:04d}", ALICE) for i in range(400)])
        path = os.path.join(self.legacy, "onionnames.db")
        with open(path, "r+b") as f:
            f.seek(4096 * 3 + 8)
            f.write(b"\xff" * 2000)
        before = self.tree(self.legacy)
        rc, err = self.run_script()
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot be moved", err)
        self.assertIn("503", err)
        self.assertFalse(self.in_place())
        self.assertEqual(self.tree(self.legacy), before, "nothing was removed")
        self.assertEqual(self.volume_names(), [".migrate.lock"], "no private copy left")

    def test_a_registry_already_on_the_volume_wins(self):
        """A restore, or a run interrupted between the rename and the marker."""
        self.make_registry(names=(("alice", BOB),), root=self.dest)
        self.make_registry()
        before = self.tree(self.legacy)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("holds a registry already", err)
        self.assertEqual(self.names(), {"alice": BOB})
        self.assertEqual(self.tree(self.legacy), before)

    def test_a_registry_planted_after_the_move_is_never_read(self):
        self.make_registry()
        self.assertEqual(self.run_script()[0], 0)
        in_use = self.tree(self.dest)
        # WordPress code writes a registry pointing alice elsewhere.
        self.make_registry(names=(("alice", MALLORY),))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB})
        in_use.pop(".migrate.lock")
        now = self.tree(self.dest)
        now.pop(".migrate.lock")
        self.assertEqual(now, in_use)
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "onionnames.db")))

    def test_nothing_moves_unless_both_are_volumes(self):
        self.make_registry()
        before = self.tree(self.legacy)
        rc, err = self.run_script(mount_check=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("is not a volume", err)
        self.assertEqual(self.tree(self.legacy), before)
        self.assertEqual(os.listdir(self.dest), [])
        self.assertFalse(self.in_place())

    def test_the_old_volume_must_be_mounted_too(self):
        """Without onionpress-persistent-data, "no registry there" proves nothing."""
        logs = []
        with mock.patch.object(mig, "is_mount_point", side_effect=lambda p: p == self.dest), \
                mock.patch.object(mig, "log", side_effect=logs.append):
            rc = mig.main(["--legacy-parent", self.parent, "--dest", self.dest])
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.parent} is not a volume", " ".join(logs))
        self.assertFalse(self.in_place())

    def test_the_volume_is_root_only_after_every_start(self):
        self.assertEqual(self.run_script()[0], 0)
        os.chmod(self.dest, 0o755)
        restored = os.path.join(self.dest, "onionnames.db")
        self.make_registry(root=self.dest)
        os.chmod(restored, 0o644)
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(restored).st_mode & 0o777, 0o600)

    def test_two_runs_at_once_move_it_once(self):
        self.make_registry()
        argv = [sys.executable, MIGRATE, "--legacy-parent", self.parent,
                "--dest", self.dest, "--no-mount-check"]
        procs = [subprocess.Popen(argv, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        errs = []
        for p in procs:
            errs.append(p.communicate(timeout=60)[1])
            self.assertEqual(p.returncode, 0, errs[-1])
        self.assertEqual(sum("moved the OnionHome name registry" in e for e in errs), 1)
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB})


class TestMoveInternals(MoveCase):

    def test_a_registry_that_keeps_changing_is_not_moved(self):
        self.make_registry()
        db = os.path.join(self.legacy, "onionnames.db")
        real = mig._copy_fd

        def copy_and_touch(fd, path):
            real(fd, path)
            with open(db, "ab") as f:      # a writer that never stops
                f.write(b"\0")

        with mock.patch.object(mig, "_copy_fd", side_effect=copy_and_touch), \
                mock.patch.object(mig, "log"):
            with self.assertRaises(mig.Unusable):
                mig.migrate(self.parent, self.dest, attempts=3, delay=0)
        self.assertFalse(self.in_place())
        self.assertEqual(os.listdir(self.dest), [])
        self.assertTrue(os.path.exists(db))

    def test_a_change_during_the_copy_is_retried(self):
        self.make_registry()
        real = mig._copy_fd
        calls = []

        def copy_then_touch_once(fd, path):
            real(fd, path)
            calls.append(path)
            if len(calls) == 1:
                os.utime(os.path.join(self.legacy, "onionnames.db"), ns=(1, 1))

        with mock.patch.object(mig, "_copy_fd", side_effect=copy_then_touch_once), \
                mock.patch.object(mig, "log"):
            mig.migrate(self.parent, self.dest, attempts=3, delay=0)
        self.assertTrue(self.in_place())
        self.assertEqual(self.names(), {"alice": ALICE, "brewster": BOB})

    def test_stale_journals_on_the_volume_go_before_the_rename(self):
        for name in mig.SIDECARS:
            with open(os.path.join(self.dest, name), "w") as f:
                f.write("stale")
        new = os.path.join(self.base, "new.db")
        self.make_registry(root=self.base)
        os.rename(os.path.join(self.base, "onionnames.db"), new)
        mig._put_in_place(self.dest, new)
        self.assertEqual(sorted(os.listdir(self.dest)), ["onionnames.db"])

    def test_mountinfo(self):
        info = os.path.join(self.base, "mountinfo")
        target = os.path.realpath(self.dest)
        with open(info, "w") as f:
            f.write("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
            f.write("90 22 8:1 /volumes/x/_data "
                    + target.replace(" ", "\\040") + " rw - ext4 /dev/sda1 rw\n")
        self.assertTrue(mig.is_mount_point(self.dest, mountinfo=info))
        self.assertFalse(mig.is_mount_point(self.parent, mountinfo=info))


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

class TestRestore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-names-restore-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(os.path.join(self.staging, "database"))
        with open(os.path.join(self.staging, "database", "wordpress.sql"), "w") as f:
            f.write("SELECT 1;\n")
        self.names = os.path.join(self.staging, "onionhome")
        for rel in ("onionnames.db", "onionnames.db-wal", "onionnames.db-shm",
                    MARKER, ".migrate.lock", "leftover/onionnames.db"):
            path = os.path.join(self.names, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(rel)

    def test_only_the_registry_goes_back_through_onionpress_tor(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            out = "wordpress\n" if "get" in argv else ""
            return subprocess.CompletedProcess(
                argv, 0, stdout=out if kwargs.get("text") else out.encode(),
                stderr="" if kwargs.get("text") else b"")

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            backup.restore_container_artifacts(self.staging, {}, lambda msg: None)

        mine = [argv for argv in calls
                if any("onionnames" in a or "onionhome" in a for a in argv)]
        self.assertEqual(mine, [
            ["docker", "cp", os.path.join(self.names, "onionnames.db"),
             f"onionpress-tor:{NAMES_DIR}/onionnames.db"],
            ["docker", "cp", os.path.join(self.names, "onionnames.db-wal"),
             f"onionpress-tor:{NAMES_DIR}/onionnames.db-wal"],
        ])


if __name__ == "__main__":
    unittest.main()
