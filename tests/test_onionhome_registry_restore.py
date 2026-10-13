#!/usr/bin/env python3
"""A restored OnionHome name registry is rebuilt by the tor image's own
checks, never put on the volume as the backup has it.

A backup's onionnames.db is whoever made the backup's: anyone can make a
backup file, and one the Settings page hands WordPress for download can be
swapped before the owner gets it. web-server.py opens the registry as root
in onionpress-tor, so a restore that copied the backup's file onto the
volume gave a crafted SQLite file (or its triggers, views and virtual
tables) to the most trusted reader there is, and its rows decided where
every name points. backup.restore_name_registry instead runs the tor
image's onionhome-registry-migrate.py on a private copy in onionpress-tor,
the code that moves the old location's untrusted files, and puts the
registry it builds (onionnames.py's schema, only rows that pass its checks)
in place of the one in use through SQLite's backup API. What the image
refuses leaves the registry in use as it was.

No Docker: onionpress-tor is played by this machine (_TorShim), with the
container's paths mapped into a sandbox and the real migration script.
"""

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
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
sys.path.insert(0, TESTS_DIR)

import onionnames  # noqa: E402
from onionpress import backup  # noqa: E402
import test_onionhome_registry_volume as vol  # noqa: E402

NAMES_DIR = vol.NAMES_DIR
ALICE, BOB, MALLORY = vol.ALICE, vol.BOB, vol.MALLORY


class _TorShim:
    """subprocess.run for the restore, with onionpress-tor played here:
    `docker exec onionpress-tor ...` runs the command on this machine with
    the container's paths (/tmp/tmp.*, /var/lib/onionnames, the image's
    script) mapped into a sandbox and python3 as this interpreter; `docker
    cp` into onionpress-tor copies the file. Anything else fails the
    test."""

    def __init__(self, test, root):
        self.test = test
        self.real = subprocess.run
        self.tmp = os.path.join(root, "container-tmp")
        self.names = os.path.join(root, "names")
        os.makedirs(self.tmp)
        os.makedirs(self.names, mode=0o700)
        self.calls = []

    def path(self, arg):
        if arg.startswith("/tmp/tmp."):
            return self.tmp + arg[len("/tmp"):]
        if arg == NAMES_DIR or arg.startswith(NAMES_DIR + "/"):
            return self.names + arg[len(NAMES_DIR):]
        if arg == backup.ONIONHOME_MIGRATE_SCRIPT:
            return vol.MIGRATE
        return arg

    def _done(self, argv, kw, stdout=""):
        if not kw.get("text") and not kw.get("encoding"):
            stdout = stdout.encode()
        return subprocess.CompletedProcess(argv, 0, stdout=stdout,
                                           stderr=type(stdout)())

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["docker", "exec", "onionpress-tor"]:
            cmd = argv[3:]
            if cmd == ["mktemp", "-d"]:
                name = "tmp." + secrets.token_hex(5)
                os.mkdir(os.path.join(self.tmp, name), 0o700)
                return self._done(argv, kw, f"/tmp/{name}\n")
            local = [sys.executable if i == 0 and c == "python3" else self.path(c)
                     for i, c in enumerate(cmd)]
            return self.real(local, **kw)
        if argv[:2] == ["docker", "cp"] and argv[3].startswith("onionpress-tor:"):
            shutil.copyfile(argv[2], self.path(argv[3][len("onionpress-tor:"):]))
            return self._done(argv, kw)
        self.test.fail(f"unexpected command: {argv}")


def _make_registry(directory, names=(("alice", ALICE), ("brewster", BOB)),
                   extra_sql=(), keep_open=None):
    os.makedirs(directory, exist_ok=True)
    conn = onionnames.db_connect(os.path.join(directory, "onionnames.db"))
    onionnames.db_init(conn)
    for name, address in names:
        ok, reason, _ = onionnames.register_name(conn, name, address)
        assert ok, reason
    for sql in extra_sql:
        conn.execute(sql)
    conn.commit()
    if keep_open is not None:
        keep_open.addCleanup(conn.close)
    else:
        conn.close()
    return conn


def _names(directory):
    conn = sqlite3.connect(os.path.join(directory, "onionnames.db"))
    try:
        return dict(conn.execute("SELECT name, onionaddress FROM onionnames"))
    finally:
        conn.close()


def _digest(directory):
    out = {}
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


class TestRestoreNameRegistry(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="op-names-restore-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shim = _TorShim(self, self.root)
        patcher = mock.patch.object(backup.subprocess, "run", side_effect=self.shim)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = os.path.join(self.root, "staging", "onionhome")
        self.logs = []

    def _live(self, names=(("zelda", MALLORY),)):
        """The registry in use, as web-server.py keeps it (WAL mode)."""
        _make_registry(self.shim.names, names=names)

    def _restore(self):
        backup.restore_name_registry(self.src, self.logs.append)

    def _assert_container_tmp_cleaned(self):
        self.assertEqual(os.listdir(self.shim.tmp), [])

    def test_the_backups_names_replace_the_registry_in_use(self):
        self._live()
        # A backup made while web-server.py had changes in the -wal: the
        # registry's two files, as create_backup copies them.
        live_backup = os.path.join(self.root, "backed-up")
        _make_registry(live_backup, keep_open=self)
        os.makedirs(self.src)
        for name in backup.ONIONHOME_REGISTRY_FILES:
            if os.path.exists(os.path.join(live_backup, name)):
                shutil.copyfile(os.path.join(live_backup, name), os.path.join(self.src, name))
        self._restore()
        self.assertEqual(_names(self.shim.names), {"alice": ALICE, "brewster": BOB})
        conn = sqlite3.connect(os.path.join(self.shim.names, "onionnames.db"))
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        finally:
            conn.close()
        self.assertEqual(os.stat(os.path.join(self.shim.names, "onionnames.db")).st_mode
                         & 0o777, 0o600)
        self.assertIn("OnionHome name registry restored: 2 names, 0 rows", self.logs[-1])
        self.assertIn("it held 1 names before", self.logs[-1])
        self._assert_container_tmp_cleaned()

    def test_rows_that_fail_the_checks_and_foreign_schema_are_left_out(self):
        self._live()
        _make_registry(self.src, extra_sql=(
            "INSERT INTO onionnames VALUES ('a b', 'a b', '%s', 'x', NULL)" % ALICE,
            "INSERT INTO onionnames VALUES ('carol', 'carol', 'carol.onion', 'x', NULL)",
            "INSERT INTO onionnames VALUES ('mallory\n', 'mallory\n', '%s', 'x', NULL)"
            % MALLORY,
            "CREATE TABLE extra (x)",
            "CREATE TRIGGER t AFTER INSERT ON onionnames BEGIN DELETE FROM onionnames; END",
            "CREATE VIEW v AS SELECT 1",
        ))
        self._restore()
        self.assertEqual(_names(self.shim.names), {"alice": ALICE, "brewster": BOB})
        conn = sqlite3.connect(os.path.join(self.shim.names, "onionnames.db"))
        try:
            objects = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        finally:
            conn.close()
        self.assertEqual(objects, {"onionnames", "onionnames_by_address",
                                   "dynamic_reservations"})
        self.assertIn("3 rows in the backup that fail", self.logs[-1])

    def test_a_fresh_volume_gets_one(self):
        _make_registry(self.src)
        self._restore()
        self.assertEqual(_names(self.shim.names), {"alice": ALICE, "brewster": BOB})
        self.assertIn("no registry before", self.logs[-1])

    def test_what_the_image_refuses_leaves_the_registry_in_use_alone(self):
        def garbage(d):
            os.makedirs(d)
            with open(os.path.join(d, "onionnames.db"), "wb") as f:
                f.write(b"not a database at all" * 100)

        def no_registry_table(d):
            os.makedirs(d)
            conn = sqlite3.connect(os.path.join(d, "onionnames.db"))
            conn.execute("CREATE TABLE something_else (x)")
            conn.commit()
            conn.close()

        def damaged(d):
            _make_registry(d, names=[(f"name{i:04d}", ALICE) for i in range(400)])
            with open(os.path.join(d, "onionnames.db"), "r+b") as f:
                f.seek(4096 * 3 + 8)
                f.write(b"\xff" * 2000)

        for label, make, why in (("not SQLite", garbage, "not a name registry"),
                                 ("no onionnames table", no_registry_table,
                                  "not a name registry"),
                                 ("damaged", damaged, "checks refused it")):
            with self.subTest(label):
                shutil.rmtree(self.shim.names)
                os.makedirs(self.shim.names)
                self._live()
                before = _digest(self.shim.names)
                shutil.rmtree(self.src, ignore_errors=True)
                make(self.src)
                with self.assertRaises(backup.NameRegistryNotRestored) as cm:
                    self._restore()
                self.assertIn(why, str(cm.exception))
                self.assertEqual(_digest(self.shim.names), before)
                self._assert_container_tmp_cleaned()

    def test_nothing_goes_into_the_container_without_a_db_file(self):
        os.makedirs(self.src)
        with open(os.path.join(self.src, "onionnames.db-wal"), "wb") as f:
            f.write(b"\x37\x7f\x06\x82" + b"\x00" * 28)
        os.symlink(os.path.join(self.root, "elsewhere.db"),
                   os.path.join(self.src, "onionnames.db"))
        with self.assertRaises(backup.NameRegistryNotRestored):
            self._restore()
        self.assertEqual(self.shim.calls, [])

    def test_only_the_registrys_files_go_in_and_only_to_a_private_folder(self):
        _make_registry(self.src)
        for extra in ("onionnames.db-shm", vol.MARKER, ".migrate.lock"):
            with open(os.path.join(self.src, extra), "w") as f:
                f.write(extra)
        self._restore()
        copies = [c for c in self.shim.calls if c[:2] == ["docker", "cp"]]
        self.assertEqual([os.path.basename(c[2]) for c in copies], ["onionnames.db"])
        for c in copies:
            self.assertRegex(c[3], r"^onionpress-tor:/tmp/tmp\.[0-9a-f]+/backup/onionhome/")
        # The volume is written only by SQLite's backup API, in the container.
        writes = [c for c in self.shim.calls if any(a.startswith(NAMES_DIR) for a in c)]
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][3:6], ["python3", "-c", backup._ONIONHOME_PUT_IN_PLACE_PY])


class TestRestoreContainerArtifacts(unittest.TestCase):
    """A refused registry is logged, and the rest of the restore goes on."""

    def test_a_refusal_does_not_stop_the_restore(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        staging = os.path.join(root, "staging")
        os.makedirs(os.path.join(staging, "database"))
        with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
            f.write("SELECT 1;\n")
        _make_registry(os.path.join(staging, "onionhome"))
        calls, logs = [], []

        def run(argv, **kwargs):
            calls.append(list(argv))
            out = "wordpress_blogs wp_blogs\n" if "get" in argv or "query" in argv else ""
            return subprocess.CompletedProcess(
                argv, 0, stdout=out if kwargs.get("text") else out.encode(),
                stderr="" if kwargs.get("text") else b"")

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            backup.restore_container_artifacts(staging, {}, logs.append, data_dir=root)
        self.assertTrue([m for m in logs if "ERROR — OnionHome's name registry was not "
                         "restored" in m and "unchanged" in m], logs)
        self.assertFalse([c for c in calls if any(f"onionpress-tor:{NAMES_DIR}" in a
                                                  for a in c)])
        # The multisite constants, the step after it, still ran.
        self.assertTrue([c for c in calls if "MULTISITE" in c], calls[-3:])


class TestOnionpressRestore(unittest.TestCase):
    """`onionpress restore` (the app's Restore... too) tears the install down
    and imports before Tor starts, so onionpress-tor is not there during the
    import: the registry is restored after start_tor, not refused for want
    of a container."""

    def test_the_registry_is_restored_once_tor_runs(self):
        from onionpress import cli
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        staging = os.path.join(root, "restore-staging")
        _make_registry(os.path.join(staging, "onionhome"))
        order = []
        me = object.__new__(cli.OnionPressCLI)
        me.paths = mock.Mock(data_dir=root, log_file=os.path.join(root, "log"))
        me.log = lambda msg: None
        me.docker = mock.Mock()
        me.docker.run.return_value = mock.Mock(ok=True)
        me.containers = mock.Mock()
        for name in ("stop", "start_core", "wait_for_wordpress", "start_tor", "wait_for_tor"):
            getattr(me.containers, name).side_effect = (
                lambda *a, _n=name, **k: order.append(_n) or True)

        def imported(staging_arg, **kw):
            order.append(("import", kw))
            return 0

        me.cmd_import_backup_artifacts = imported
        with mock.patch("onionpress.backup.read_backup_metadata", return_value={}), \
                mock.patch("onionpress.backup.prepare_install_from_backup",
                           return_value=(staging, {"onion_address": ALICE})), \
                mock.patch("onionpress.backup.restore_staged_name_registry",
                           side_effect=lambda st, log: order.append(("names", st))), \
                mock.patch("onionpress.derivatives.spawn_regeneration"), \
                mock.patch("sys.stdout"):
            self.assertEqual(me.cmd_restore("pw", "/tmp/b.zip"), 0)
        self.assertEqual(order, ["stop", "start_core", "wait_for_wordpress",
                                 ("import", {"name_registry": False, "hub_data": None}),
                                 "start_tor", "wait_for_tor", ("names", staging)])

    def test_an_import_with_tor_running_does_it_inline(self):
        """The launcher's start hook (tor already up) and the default."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        staging = os.path.join(root, "staging")
        os.makedirs(os.path.join(staging, "database"))
        with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
            f.write("SELECT 1;\n")
        _make_registry(os.path.join(staging, "onionhome"))
        for kw, inline in (({}, True), ({"name_registry": False}, False)):
            with self.subTest(kw=kw):
                logs = []
                with mock.patch.object(backup, "restore_name_registry") as names, \
                        mock.patch.object(backup, "_get_db_credentials",
                                          return_value={"name": "wordpress",
                                                        "user": "wordpress",
                                                        "password": "wp"}), \
                        mock.patch.object(backup, "_restore_db_password",
                                          return_value="wp"), \
                        mock.patch.object(backup, "_assert_db_client_sandboxed"), \
                        mock.patch.object(backup.subprocess, "run", return_value=
                                          subprocess.CompletedProcess([], 0, stdout="", stderr="")):
                    backup.restore_container_artifacts(staging, {}, logs.append, **kw)
                self.assertEqual(names.called, inline)
                if not inline:
                    self.assertIn("Restore: OnionHome's name registry is restored once "
                                  "onionpress-tor runs", logs)


class TestTheImageShipsWhatTheRestoreRuns(unittest.TestCase):

    def test_the_script_and_its_marker(self):
        dockerfile = vol._read("app/Resources/docker/tor/Dockerfile")
        self.assertIn(f"COPY onionhome-registry-migrate.py {backup.ONIONHOME_MIGRATE_SCRIPT}\n",
                      dockerfile)
        self.assertIn("COPY onionnames.py /onionnames.py\n", dockerfile)
        self.assertEqual(backup.ONIONHOME_MIGRATE_MARKER, vol.mig.MARKER)
        self.assertEqual(vol.mig.LEGACY_NAME, "onionhome")
        source = vol._read("app/Resources/docker/tor/onionhome-registry-migrate.py")
        for flag in ("--legacy-parent", "--dest", "--no-mount-check"):
            self.assertIn(f'"{flag}"', source)

    def test_no_restore_copies_a_file_onto_the_volume(self):
        """docker cp names the volume only as what a backup copies out."""
        src = vol._read("src/onionpress/backup.py")
        found = 0
        for m in re.finditer(r"onionpress-tor:\{ONIONHOME_REGISTRY_DIR\}", src):
            before = re.sub(r"\s+", "", src[max(0, m.start() - 40):m.start()])
            self.assertTrue(before.endswith("'cp',f'"), src[m.start() - 80:m.end()])
            found += 1
        self.assertEqual(found, 1)
        for rel in ("src/onionpress/backup.py", "src/onionpress/cli.py",
                    "linux/onionpress", "app/MacOS/onionpress"):
            with self.subTest(rel):
                self.assertNotIn(f"onionpress-tor:{NAMES_DIR}", vol._read(rel))


if __name__ == "__main__":
    unittest.main()
