#!/usr/bin/env python3
"""The move of the OnionHeaven hub data (onionheaven-data-migrate.py) holds
up against what older containers, WordPress and a full disk can do.

A review of the first version found that:

  * an older container still writing the registry (a takeover worker or
    onionpress-tor left running across the update) made every attempt
    "unstable", and with keys at stake both containers exited for good;
  * copies kept aside (.pre-migration-*, legacy-leftover-*) held private
    keys forever, past their registrant's unregistration;
  * WordPress, which owns the folder the old data lives in, could plant
    sparse or huge files there that root copied densely onto the hub's
    volume at every start, deep trees and odd entries that failed the first
    move, and a keys/ entry that made any node refuse to start;
  * a registry written at the old location after the move (an older
    OnionPress run again) was shelved silently;
  * the database went into place before its write-ahead log, so a reader
    in between deleted the log just moved;
  * a start after a failed move with no keys at stake left no marker, so
    the next start set aside what the hub had written since;
  * a container without the old volume marked the new one as current;
  * copies kept the old owner and mode (www-data's, after old restores);
  * special files left an empty legacy-leftover-* at every start;
  * nothing made the copy durable before the originals went.

Each is reproduced here on temporary directories; no Docker.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)

import test_onionheaven_data_volume as hd  # noqa: E402
import onionheaven_common as oc  # noqa: E402

A = "a" * 56 + ".onion"
B = "b" * 56 + ".onion"
C = "c" * 56 + ".onion"
D = "d" * 56 + ".onion"
E = "e" * 56 + ".onion"
KEY = "ks_hs_id.ed25519_expanded_private"
MARKER = ".migrated-from-persistent-data"


def _listing(root):
    out = []
    for r, dirs, files in os.walk(root):
        for name in dirs + files:
            out.append(os.path.relpath(os.path.join(r, name), root))
    return sorted(out)


def _integrity(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


class Case(hd.MigrationCase):

    def setUp(self):
        super().setUp()
        self.mod = hd._load_migrate()
        self.logs = []

    def migrate(self, attempts=2, **kw):
        with mock.patch.object(self.mod, "log", side_effect=self.logs.append):
            self.mod.migrate(self.parent, self.dest, attempts=attempts, delay=0, **kw)

    def logged(self, text):
        return any(text in line for line in self.logs)

    def kept(self, prefix):
        return sorted(os.path.join(self.dest, n) for n in os.listdir(self.dest)
                      if n.startswith(prefix))

    def leftovers(self):
        return self.kept("legacy-leftover-")

    def aside(self):
        return self.kept(".pre-migration-")


# ---------------------------------------------------------------------------
# Older containers still writing (review item 1)
# ---------------------------------------------------------------------------

class TestOlderWriters(Case):

    def _writer(self, period, keep_open):
        """An older container's registry writes, every period seconds."""
        db = os.path.join(self.legacy, "registry.db")
        stop, writes = threading.Event(), [0]

        def run():
            held = sqlite3.connect(db, timeout=30) if keep_open else None
            while not stop.is_set():
                conn = held or sqlite3.connect(db, timeout=30)
                try:
                    conn.execute("INSERT INTO registry VALUES ('heartbeat')")
                    conn.commit()
                    writes[0] += 1
                except sqlite3.Error:
                    pass
                finally:
                    if held is None:
                        conn.close()
                time.sleep(period)
            if held is not None:
                held.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def done():
            stop.set()
            thread.join(10)
        self.addCleanup(done)
        deadline = time.monotonic() + 10
        while writes[0] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        return done

    def _check_moved(self, rows_at_least, key_dirs):
        db = os.path.join(self.dest, "registry.db")
        self.assertEqual(_integrity(db), "ok")
        self.assertGreaterEqual(hd._rows(db), rows_at_least)
        self.assertEqual(self.marker()["key_dirs"], key_dirs)
        self.assertFalse([n for n in os.listdir(self.dest) if n.startswith("registry.db-")])

    def test_a_registry_an_older_container_keeps_writing_still_moves(self):
        self.make_registry(rows=50)
        for i in range(30):
            self.add_registrant(hd._address(i))
        done = self._writer(0.002, keep_open=True)
        self.migrate()
        done()
        self._check_moved(53, 30)
        # It kept changing: it stays at the old location (a later arrival,
        # kept aside by the next run); the keys are gone from there.
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "registry.db")))
        self.assertFalse(os.path.exists(os.path.join(self.legacy, "keys")))

    def test_a_registry_reopened_for_every_write_moves_too(self):
        self.make_registry(rows=50)
        self.add_registrant()
        done = self._writer(0.02, keep_open=False)
        self.migrate(attempts=3)
        done()
        self._check_moved(50, 1)

    def test_a_key_rewritten_all_the_time_never_stops_the_move(self):
        self.make_registry()
        kdir = self.add_registrant(B)
        self.add_registrant(A)
        key = os.path.join(kdir, KEY)
        stop = threading.Event()

        def rewrite():
            while not stop.is_set():
                with open(key, "w") as f:       # truncate, then write
                    f.write("PEM-KEY")

        thread = threading.Thread(target=rewrite, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.mod, "FILE_RETRY_DELAY", 0):
                self.migrate()
        finally:
            stop.set()
            thread.join(10)
        self.assertIn("key_dirs", self.marker())
        moved = os.path.join(self.dest, "keys", B, KEY)
        if os.path.exists(moved):
            with open(moved) as f:
                self.assertEqual(f.read(), "PEM-KEY", "never a half-written copy")
        else:
            self.assertTrue(self.logged("did not copy keys/" + B))
            self.assertTrue(os.path.exists(key), "a key not copied stays")
        with open(os.path.join(self.dest, "keys", A, KEY)) as f:
            self.assertEqual(f.read(), "PEM-KEY")

    def test_a_registry_that_is_no_database_moves_as_it_is(self):
        self.write("registry.db", b"\x00not a database" * 100)
        self.add_registrant()
        self.migrate()
        with open(os.path.join(self.dest, "registry.db"), "rb") as f:
            self.assertEqual(f.read(), b"\x00not a database" * 100)
        self.assertEqual(self.marker()["key_dirs"], 1)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_sqlite_never_opens_a_file_at_the_old_location(self):
        """A link planted as registry.db-shm would have root's SQLite write
        through it (it truncates and writes the index)."""
        self.make_registry(rows=3, keep_open=True)
        self.add_registrant()
        victim = self.write("victim", "PRECIOUS", root=self.base)
        os.unlink(os.path.join(self.legacy, "registry.db-shm"))
        os.symlink(victim, os.path.join(self.legacy, "registry.db-shm"))
        self.migrate()
        with open(victim) as f:
            self.assertEqual(f.read(), "PRECIOUS")
        self.assertEqual(hd._rows(os.path.join(self.dest, "registry.db")), 3)


# ---------------------------------------------------------------------------
# The log goes into place before its database (review item 5)
# ---------------------------------------------------------------------------

class TestPlacingOrder(Case):

    def test_a_log_goes_in_before_its_database(self):
        staging = os.path.join(self.dest, ".migrating")
        os.makedirs(os.path.join(staging, "keys"))
        for name in ("registry.db", "registry.db-wal", "registry.db-journal",
                     "heartbeat.log"):
            self.write(name, name, root=staging)
        order = []
        real = os.rename

        def spy(src, dst):
            order.append(os.path.basename(dst))
            return real(src, dst)

        with mock.patch.object(self.mod.os, "rename", side_effect=spy):
            self.mod._put_in_place(self.dest, staging, "20261012T000000Z")
        self.assertLess(order.index("registry.db-wal"), order.index("registry.db"))
        self.assertLess(order.index("registry.db-journal"), order.index("registry.db"))
        self.assertEqual(order[-1], "registry.db")

    def test_a_reader_between_the_renames_loses_nothing(self):
        """Rows only in the log, as a hub that stopped uncleanly leaves it;
        the launcher's sqlite3 reads the registry whenever onionheaven is
        up, including while its entrypoint moves the data."""
        live = os.path.join(self.base, "live")
        os.makedirs(live)
        conn = sqlite3.connect(os.path.join(live, "registry.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE registry (content_address TEXT)")
        conn.executemany("INSERT INTO registry VALUES (?)", [(f"r{i}",) for i in range(25)])
        conn.commit()
        os.makedirs(self.legacy)
        for name in ("registry.db", "registry.db-wal"):
            shutil.copy2(os.path.join(live, name), os.path.join(self.legacy, name))
        conn.close()
        self.add_registrant()

        real = os.rename
        readers = []
        placed = os.path.join(self.dest, "registry.db")

        def rename(src, dst):
            real(src, dst)
            if dst == placed and not readers:
                readers.append(sqlite3.connect(dst))
                readers[0].execute("SELECT COUNT(*) FROM sqlite_master").fetchone()

        with mock.patch.object(self.mod.os, "rename", side_effect=rename):
            self.migrate(attempts=1)
        self.assertTrue(readers)
        readers[0].close()          # the sqlite3 command exits
        self.assertEqual(hd._rows(placed), 25)


# ---------------------------------------------------------------------------
# What WordPress can plant at the old location (review items 3, 6, 10)
# ---------------------------------------------------------------------------

class TestPlanted(Case):

    def _deep(self, root, levels, width=250):
        """levels folders inside each other, each name width long: deeper
        than PATH_MAX allows a path to reach."""
        os.makedirs(root, exist_ok=True)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        for i in range(levels):
            name = (f"{i:03d}" + "d" * width)[:width]
            os.mkdir(name, dir_fd=fd)
            nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.close(fd)

    def test_a_deep_tree_cannot_stop_the_first_move(self):
        self.make_registry()
        self.add_registrant()
        self._deep(self.legacy, 20)
        self._deep(os.path.join(self.legacy, "keys", A), 20)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.marker()["key_dirs"], 1)
        self.assertIn("not OnionHeaven data", err)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.lexists(self.legacy), "the next run removes it")

    def test_only_real_keys_are_at_stake(self):
        outside = self.write("outside", "PEM", root=self.base)
        self.write(f"keys/x/{KEY}", "PEM")                  # not an address
        os.makedirs(os.path.join(self.legacy, "keys", A))   # no key in it
        self.write(f"keys/{B}/hostname", B)                 # no key in it
        os.makedirs(os.path.join(self.legacy, "keys", C))
        os.symlink(outside, os.path.join(self.legacy, "keys", C, KEY))   # a link
        self.write(f"keys/{D}/{KEY}", "")                   # empty
        self.assertFalse(self.mod._keys_at_stake(self.parent))
        self.write(f"keys/{E}/hs_ed25519_secret_key", "SECRET")
        self.assertTrue(self.mod._keys_at_stake(self.parent))

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_made_up_keys_and_a_failure_still_start(self):
        """WordPress renames the old folder away, makes its own with a keys/
        entry, and something the move fails on."""
        self.write(f"keys/x/{KEY}", "PEM")
        log = self.write("heartbeat.log", "x")
        os.chmod(log, 0o000)
        rc, err = self.run_script()
        os.chmod(log, 0o600)
        self.assertEqual(rc, 0, err)
        self.assertIn("starting anyway", err)

    def _sparse(self, rel, size):
        path = os.path.join(self.legacy, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(size)
        fd = os.open(path, os.O_RDONLY)
        try:
            if not hasattr(os, "SEEK_HOLE") or os.lseek(fd, 0, os.SEEK_HOLE) >= size:
                self.skipTest("this filesystem makes no sparse files")
        finally:
            os.close(fd)
        return path

    def test_sparse_and_oversized_later_arrivals_are_never_copied(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        self._sparse("x.bin", 512 << 20)                # 512 MiB, almost no blocks
        self._sparse("heartbeat.log", 8 << 20)          # a log's name, sparse
        self.write("activate", "x" * (100 << 10))       # dense, over its limit
        started = time.monotonic()
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("sparse", err)
        self.assertFalse(self.leftovers())
        self.assertFalse(os.path.lexists(self.legacy))

    def _registry_with_a_hole(self, rows, hole):
        """A sound registry whose file ends in a hole (SQLite reads only
        the pages its header counts): what ZFS with compression makes of a
        database's all-zero blocks."""
        self.make_registry(rows=rows)
        db = os.path.join(self.legacy, "registry.db")
        os.truncate(db, os.path.getsize(db) + hole)
        fd = os.open(db, os.O_RDONLY)
        try:
            if not hasattr(os, "SEEK_HOLE") or os.lseek(fd, 0, os.SEEK_HOLE) >= os.fstat(fd).st_size:
                self.skipTest("this filesystem makes no sparse files")
        finally:
            os.close(fd)
        return db

    def test_a_registry_with_holes_still_moves(self):
        self._registry_with_a_hole(rows=9, hole=1 << 20)
        self.add_registrant()
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("sparse", err)
        self.assertEqual(hd._rows(os.path.join(self.dest, "registry.db")), 9)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_a_sparse_later_registry_takes_no_more_room_on_the_volume(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        self._registry_with_a_hole(rows=2, hole=1 << 30)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        kept = os.path.join(self.leftovers()[0], "registry.db")
        st = os.stat(kept)
        self.assertGreater(st.st_size, 1 << 30)
        self.assertLess(st.st_blocks * 512, 16 << 20)

    def test_logs_never_stop_the_first_move_for_room(self):
        """Any number of files can carry a log's name; the registry and the
        keys are what must fit."""
        self.make_registry()
        self.add_registrant()
        for i in range(3):
            self.write(f"heartbeat.{i}.log", "x" * (3 << 20))
        with mock.patch.object(self.mod, "FIRST_MOVE_MARGIN", 0), \
                mock.patch.object(self.mod, "_free_bytes", return_value=2 << 20):
            self.migrate()
        self.assertEqual(self.marker()["key_dirs"], 1)
        self.assertTrue(self.logged("no room for it on the volume"))
        self.assertFalse([n for n in os.listdir(self.dest) if n.endswith(".log")])
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "heartbeat.0.log")))

    def test_later_arrivals_keep_only_what_the_hub_wrote(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        self.add_registrant(B, key="LATE-KEY")
        self.write(f"keys/{B}/evil.php", "<?php")
        self.write("wp-content/x.php", "<?php")
        os.mkfifo(os.path.join(self.legacy, "keys", "fifo"))
        os.symlink(os.path.join(self.parent, "status.json"),
                   os.path.join(self.legacy, "heartbeat.log"))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        leftovers = self.leftovers()
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(_listing(leftovers[0]),
                         sorted(["keys", f"keys/{B}", f"keys/{B}/{KEY}",
                                 f"keys/{B}/hostname"]))
        self.assertFalse(os.path.lexists(self.legacy))
        self.assertTrue(os.path.exists(os.path.join(self.parent, "status.json")))

    def test_later_arrivals_that_do_not_fit_stay_where_they_are(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        late = self.add_registrant(B, key="LATE-KEY")
        self.write("wp-content/x.php", "<?php")
        with mock.patch.object(self.mod, "_free_bytes", return_value=1 << 20):
            self.migrate()
        self.assertFalse(self.leftovers())
        self.assertTrue(self.logged("left there, not used"))
        self.assertTrue(os.path.exists(os.path.join(late, KEY)))
        self.assertFalse(os.path.exists(os.path.join(self.legacy, "wp-content")))

    def test_special_files_never_leave_empty_copies(self):
        self.make_registry()
        self.add_registrant()
        os.mkfifo(os.path.join(self.legacy, "keys", "fifo"))
        for run in range(4):
            rc, err = self.run_script()
            self.assertEqual(rc, 0, err)
            self.assertFalse(self.leftovers(), f"run {run}: {err}")
        self.assertFalse(os.path.lexists(self.legacy))

    def test_links_fifos_and_odd_names_at_the_first_move(self):
        self.make_registry()
        self.add_registrant()
        os.mkfifo(os.path.join(self.legacy, "keys", "fifo"))
        os.mkfifo(os.path.join(self.legacy, "fifo"))
        outside = os.path.join(self.parent, "status.json")
        os.link(outside, os.path.join(self.legacy, "hard"))
        self.write("new\nline", "nl")
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.marker()["key_dirs"], 1)
        with open(outside) as f:
            self.assertEqual(f.read(), "{}")
        moved = _listing(self.dest)
        self.assertNotIn("hard", moved)
        self.assertNotIn("new\nline", moved)
        for line in err.splitlines():
            self.assertTrue(line.startswith("["), f"one line per message: {line!r}")


# ---------------------------------------------------------------------------
# The marker (review items 7 and 8)
# ---------------------------------------------------------------------------

class TestMarker(Case):

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_a_start_anyway_keeps_what_the_hub_wrote_since(self):
        self.make_registry(rows=7)
        os.makedirs(os.path.join(self.legacy, "keys"))
        bad = self.write("heartbeat.log", "x")
        os.chmod(bad, 0o000)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("starting anyway", err)

        # The hub runs on the volume: a registry of its own, a new key.
        conn = sqlite3.connect(os.path.join(self.dest, "registry.db"))
        conn.execute("CREATE TABLE registry (content_address TEXT)")
        conn.execute("INSERT INTO registry VALUES ('new')")
        conn.commit()
        conn.close()
        self.write(f"keys/{A}/{KEY}", "NEW-REGISTRANT-KEY", root=self.dest)

        os.chmod(bad, 0o600)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(hd._rows(os.path.join(self.dest, "registry.db")), 1)
        with open(os.path.join(self.dest, "keys", A, KEY)) as f:
            self.assertEqual(f.read(), "NEW-REGISTRANT-KEY")
        self.assertFalse(self.aside())
        leftovers = self.leftovers()
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(hd._rows(os.path.join(leftovers[0], "registry.db")), 7)

    def test_no_marker_without_the_old_volume(self):
        missing = os.path.join(self.base, "not-mounted")
        with mock.patch.object(self.mod, "log"):
            self.mod.migrate(missing, self.dest, legacy_mounted=False)
        self.assertFalse(os.path.exists(os.path.join(self.dest, MARKER)))

        dest = os.path.realpath(self.dest)
        with mock.patch.object(self.mod, "is_mount_point",
                               side_effect=lambda p, **k: os.path.realpath(p) == dest), \
                mock.patch.object(self.mod, "log"):
            self.assertEqual(self.mod.main(["--legacy-parent", missing,
                                            "--dest", self.dest]), 0)
        self.assertFalse(os.path.exists(os.path.join(self.dest, MARKER)))

    def test_an_old_location_that_is_not_mounted_is_not_read(self):
        """/var/lib/onionpress without its volume is the image's empty
        folder: nothing there says the old data is gone."""
        self.make_registry()
        self.add_registrant()
        before = self.tree(self.legacy)
        dest = os.path.realpath(self.dest)
        with mock.patch.object(self.mod, "is_mount_point",
                               side_effect=lambda p, **k: os.path.realpath(p) == dest), \
                mock.patch.object(self.mod, "log"):
            self.assertEqual(self.mod.main(["--legacy-parent", self.parent,
                                            "--dest", self.dest]), 0)
        self.assertEqual(self.tree(self.legacy), before)
        self.assertEqual(os.listdir(self.dest), [".migrate.lock"])

    def test_an_error_after_the_move_never_stops_the_container(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        self.add_registrant(B)
        with mock.patch.object(self.mod, "_scan", side_effect=OSError(5, "I/O error")), \
                mock.patch.object(self.mod, "log", side_effect=self.logs.append):
            rc = self.mod.main(["--legacy-parent", self.parent, "--dest", self.dest,
                                "--no-mount-check"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.logged("moved earlier"))


# ---------------------------------------------------------------------------
# Owner and mode (review item 9)
# ---------------------------------------------------------------------------

class TestOwnership(Case):

    def _hub(self):
        self.make_registry()
        self.add_registrant()
        self.add_registrant(B)
        self.write("heartbeat.log", "pass\n", 0o644)
        self.write("activate", "now\n", 0o666)
        for r, dirs, _ in os.walk(self.legacy):
            for name in dirs:
                os.chmod(os.path.join(r, name), 0o755)

    def test_everything_on_the_volume_is_private(self):
        self._hub()
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        for r, dirs, files in os.walk(self.dest):
            for name in dirs:
                with self.subTest(dir=name):
                    self.assertEqual(os.stat(os.path.join(r, name)).st_mode & 0o777, 0o700)
            for name in files:
                with self.subTest(file=name):
                    self.assertEqual(os.stat(os.path.join(r, name)).st_mode & 0o777, 0o600)

    def test_copies_are_given_to_root(self):
        self._hub()
        calls = []
        with mock.patch.object(self.mod.os, "fchown",
                               side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
            self.migrate()
        made = len(_listing(self.dest)) - 2          # less the marker and the lock
        self.assertGreaterEqual(len(calls), made)
        self.assertEqual(set(calls), {(0, 0)})


# ---------------------------------------------------------------------------
# Kept copies do not keep keys (review item 2)
# ---------------------------------------------------------------------------

class TestKeptCopies(Case):

    def test_a_late_key_the_volume_holds_the_same_leaves_no_copy(self):
        self.make_registry()
        self.add_registrant(A, key="KEY-A")
        self.assertEqual(self.run_script()[0], 0)
        self.add_registrant(A, key="KEY-A")     # the same key, again
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.leftovers())
        self.assertFalse(os.path.lexists(self.legacy))

    def test_a_kept_key_goes_when_its_address_unregisters(self):
        self.make_registry()
        self.add_registrant(A, key="KEY-A")
        self.assertEqual(self.run_script()[0], 0)
        self.add_registrant(A, key="OTHER-BYTES")
        self.add_registrant(B, key="KEY-B")
        self.assertEqual(self.run_script()[0], 0)
        leftover = self.leftovers()[0]
        self.assertTrue(os.path.isdir(os.path.join(leftover, "keys", A)))
        aside = os.path.join(self.dest, ".pre-migration-20261001T000000Z", "keys-2", B)
        os.makedirs(aside)
        self.write(KEY, "KEY-B", root=aside)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        with mock.patch.object(oc, "log"):
            oc.db_ensure_schema(conn)
        conn.execute("INSERT INTO registry (content_address, healthcheck_address,"
                     " registered_at, status) VALUES (?, ?, 'now', 'online')",
                     (B, "h" * 56 + ".onion"))
        conn.commit()
        with mock.patch.object(oc, "ONIONHEAVEN_DATA_DIR", self.dest), \
                mock.patch.object(oc, "KEYS_DIR", os.path.join(self.dest, "keys")), \
                mock.patch.object(oc, "release_function"), \
                mock.patch.object(oc, "log"):
            oc.unregister_entry(conn, B, "h" * 56 + ".onion")
        self.assertFalse(os.path.exists(os.path.join(leftover, "keys", B)))
        self.assertFalse(os.path.exists(aside))
        self.assertTrue(os.path.isdir(os.path.join(leftover, "keys", A)),
                        "another address's kept key stays")

    def test_the_move_drops_keys_it_brought_from_what_it_set_aside(self):
        self.make_registry()
        self.add_registrant(A, key="KEY-A")
        # The volume holds A's key already (an earlier start), and B's.
        self.write(f"keys/{A}/{KEY}", "KEY-A", root=self.dest)
        self.write(f"keys/{A}/hostname", A + "\n", root=self.dest)
        self.write(f"keys/{B}/{KEY}", "KEY-B", root=self.dest)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        aside = self.aside()
        self.assertEqual(len(aside), 1)
        kept = _listing(aside[0])
        self.assertIn(f"keys/{B}/{KEY}", kept)
        self.assertFalse([p for p in kept if A in p])

    def test_kept_copies_go_after_keep_days(self):
        self.assertEqual(self.run_script()[0], 0)
        now = datetime.now(timezone.utc)
        recent = "legacy-leftover-" + (now - timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
        for name in ("legacy-leftover-20200101T000000Z",
                     ".pre-migration-20200101T000000Z-2", recent):
            self.write(f"keys/{B}/{KEY}", "KEY-B", root=os.path.join(self.dest, name))
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.leftovers(), [os.path.join(self.dest, recent)])
        self.assertFalse(self.aside())
        with mock.patch.object(self.mod, "log"):
            self.mod.tidy_kept(self.dest, now=now + timedelta(days=self.mod.KEEP_DAYS))
        self.assertFalse(self.leftovers())


# ---------------------------------------------------------------------------
# A registry written at the old location after the move (review item 4)
# ---------------------------------------------------------------------------

class TestDowngrade(Case):

    def test_a_registry_written_after_the_move_gets_a_loud_warning(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        marker = self.marker()
        marker["migrated_at"] = "20260101T000000Z"
        with open(os.path.join(self.dest, MARKER), "w") as f:
            json.dump(marker, f)
        self.make_registry(rows=4)          # an older OnionPress, run again
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("after the move", err)
        self.assertIn("Never run an older OnionPress on this hub again", err)
        self.assertEqual(hd._rows(os.path.join(self.leftovers()[0], "registry.db")), 4)

    def test_the_docs_say_never_to_downgrade_a_hub(self):
        doc = hd._read("docs/DOCKER-SOCKET.md")
        section = doc[doc.index("## The OnionHeaven hub data"):]
        section = section[:section.index("\n## ", 1)]
        self.assertIn("Never downgrade a hub", section)


# ---------------------------------------------------------------------------
# On disk before the originals go (review item 11)
# ---------------------------------------------------------------------------

class TestDurability(Case):

    def _hub(self, keys):
        self.make_registry()
        for i in range(keys):
            self.add_registrant(hd._address(i))

    def test_the_copy_is_on_disk_before_anything_old_goes(self):
        self._hub(5)
        events = []
        real_sync, real_remove = os.sync, self.mod._remove_old

        def remove(*a, **k):
            events.append("remove")
            return real_remove(*a, **k)

        def sync():
            events.append("sync")
            real_sync()

        with mock.patch.object(self.mod.os, "sync", side_effect=sync), \
                mock.patch.object(self.mod, "_remove_old", side_effect=remove):
            self.migrate()
        self.assertIn("remove", events)
        self.assertEqual(events[events.index("remove") - 1], "sync")
        self.assertFalse(os.path.lexists(self.legacy))

    def test_no_fsync_for_every_file(self):
        """compose stops a container after 10 s; a big hub's move must not
        spend that on one fsync per key."""
        self._hub(40)
        real, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            real(fd)

        with mock.patch.object(self.mod.os, "fsync", side_effect=fsync):
            self.migrate()
        self.assertEqual(self.marker()["key_dirs"], 40)
        self.assertLessEqual(len(calls), 4)


# ---------------------------------------------------------------------------
# Older containers are gone before anything starts (review items 1, 12)
# ---------------------------------------------------------------------------

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
OLD_MOUNTS = "onionpress-persistent-data:/var/lib/onionpress "
NEW_MOUNTS = ("onionpress-persistent-data:/var/lib/onionpress "
              "onionpress-onionheaven-data:/var/lib/onionheaven ")


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end + 3]


DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$1" in
    ps) cat "$STUB_DIR/names" ;;
    inspect) cat "$STUB_DIR/mounts/$4" 2>/dev/null || exit 1 ;;
    stop) sleep "${STUB_STOP_SECONDS:-0}" ;;
esac
exit 0
"""


class TestLaunchers(unittest.TestCase):

    def _run(self, launcher, function, containers, stop_seconds=0):
        """Run one launcher function with a docker that lists containers
        ({name: mounts, or None when inspect fails}); returns (calls,
        seconds taken)."""
        text = hd._read(launcher)
        tmp = tempfile.mkdtemp(prefix="op-retire-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "bin"))
        os.makedirs(os.path.join(tmp, "mounts"))
        stub = os.path.join(tmp, "bin", "docker")
        with open(stub, "w") as f:
            f.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        with open(os.path.join(tmp, "names"), "w") as f:
            f.write("".join(name + "\n" for name in containers))
        for name, mounts in containers.items():
            if mounts is not None:
                with open(os.path.join(tmp, "mounts", name), "w") as f:
                    f.write(mounts)
        open(os.path.join(tmp, "calls"), "w").close()
        script = ("set -e\n"
                  f'LOG_FILE="{tmp}/log"\n'
                  'log() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                  + _bash_function(text, function) + function + "\n")
        started = time.monotonic()
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=60,
                             env={"PATH": os.path.join(tmp, "bin") + ":/usr/bin:/bin",
                                  "STUB_DIR": tmp, "STUB_STOP_SECONDS": str(stop_seconds)})
        took = time.monotonic() - started
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(tmp, "calls")) as f:
            return f.read().splitlines(), took

    def test_the_launchers_delegate_the_retirement(self):
        """One implementation, ContainerManager.retire_old_onionheaven_containers:
        start_core runs it (#442.6), and no launcher keeps a bash step."""
        for launcher in ("app/MacOS/onionpress", "linux/onionpress"):
            with self.subTest(launcher=launcher):
                self.assertNotIn("\nretire_old_onionheaven_containers() {", hd._read(launcher))

    def _retire(self, containers, rm_seconds=0):
        """ContainerManager.retire_old_onionheaven_containers against a
        docker that lists *containers* ({name: mounts JSON, or None when
        inspect fails}); returns (removed, calls, log lines, seconds)."""
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        from onionpress.docker import DockerResult
        from onionpress.platform import resolve_paths
        calls = []

        class FakeDocker:
            def run(self, args, **kw):
                calls.append(" ".join(args))
                if args[:2] == ["ps", "-a"]:
                    return DockerResult(0, "".join(n + "\n" for n in containers), "")
                if args[0] == "inspect":
                    mounts = containers.get(args[3])
                    if mounts is None:
                        return DockerResult(1, "", "Error: No such object")
                    return DockerResult(0, mounts + "\n", "")
                if args[:2] == ["rm", "-f"]:
                    time.sleep(rm_seconds)
                    return DockerResult(0, args[2] + "\n", "")
                return DockerResult(0, "", "")

        tmp = tempfile.mkdtemp(prefix="op-retire-py-")
        self.addCleanup(shutil.rmtree, tmp, True)
        logs = []
        cm = ContainerManager(FakeDocker(), resolve_paths(data_dir=tmp),
                              PortConfig(0, 8080, 9050, 9077), log_func=logs.append,
                              egress=lambda: None)
        started = time.monotonic()
        removed = cm.retire_old_onionheaven_containers()
        return removed, calls, logs, time.monotonic() - started

    OLD_MOUNTS_JSON = json.dumps([{"Name": "onionpress-persistent-data",
                                   "Destination": "/var/lib/onionpress"}])
    NEW_MOUNTS_JSON = json.dumps([{"Name": "onionpress-persistent-data",
                                   "Destination": "/var/lib/onionpress"},
                                  {"Name": "onionpress-onionheaven-data",
                                   "Destination": "/var/lib/onionheaven"}])

    def test_containers_without_the_volume_are_removed_before_compose_starts(self):
        containers = {
            "onionpress-tor": self.OLD_MOUNTS_JSON,
            "onionheaven": self.NEW_MOUNTS_JSON,
            "onionheaven-takeover-3": self.OLD_MOUNTS_JSON,
            "onionheaven-poll-1": "[]",
            "onionheaven-takeover-4": self.NEW_MOUNTS_JSON,
            "onionheaven-takeover-9": None,             # inspect fails: kept
            "onionheaven-farm": self.OLD_MOUNTS_JSON,   # the controller: never
            "onionpress-wordpress": self.OLD_MOUNTS_JSON,   # not one of them
        }
        removed, calls, logs, _ = self._retire(containers)
        self.assertEqual(sorted(removed), ["onionheaven-poll-1", "onionheaven-takeover-3",
                                           "onionpress-tor"])
        self.assertEqual(sorted(c.split()[-1] for c in calls if c.startswith("rm -f ")),
                         sorted(removed))
        for kept in ("onionpress-wordpress", "onionheaven-farm", "onionheaven-takeover-9",
                     "onionheaven-takeover-4", "onionheaven"):
            self.assertNotIn(f"rm -f {kept}", calls)
        self.assertEqual(sorted(logs), sorted(
            f"Removing {n}: it does not mount onionpress-onionheaven-data "
            "(started by an older OnionPress)" for n in removed))

    def test_the_retirement_removes_all_at_once(self):
        containers = {f"onionheaven-takeover-{i}": self.OLD_MOUNTS_JSON for i in range(4)}
        removed, _, _, took = self._retire(containers, rm_seconds=0.4)
        self.assertEqual(len(removed), 4)
        self.assertLess(took, 1.2, "four removals of 0.4 s each ran one after another")

    def test_nothing_to_retire_calls_no_rm(self):
        removed, calls, logs, _ = self._retire({"onionpress-tor": self.NEW_MOUNTS_JSON,
                                                "onionpress-wordpress": self.OLD_MOUNTS_JSON})
        self.assertEqual((removed, logs), ([], []))
        self.assertFalse([c for c in calls if c.startswith("rm")])
        removed, calls, _, _ = self._retire({})
        self.assertEqual(removed, [])

    def test_every_start_retires_them_before_compose_up(self):
        """Both launchers' start runs ContainerManager.start_core
        (`onionpress start-core`, #442.6), which retires them first; the
        order is held in tests/test_start_core.py."""
        for launcher in LAUNCHERS:
            with self.subTest(launcher=launcher):
                body = _bash_function(hd._read(launcher), "start_containers")
                self.assertIn("start-containers", body)
                self.assertNotIn("up -d wordpress db", body)

    def test_the_farm_stops_all_at_once(self):
        workers = {f"onionheaven-takeover-{i}": NEW_MOUNTS for i in range(4)}
        workers["onionheaven-poll-0"] = NEW_MOUNTS
        for launcher in LAUNCHERS:
            with self.subTest(launcher=launcher):
                calls, took = self._run(launcher, "stop_onionheaven_farm", workers,
                                        stop_seconds=1)
                self.assertEqual(sorted(c for c in calls if c.startswith("stop ")),
                                 sorted(f"stop -t 1 {name}" for name in workers))
                self.assertEqual(len([c for c in calls if c.startswith("rm ")]), 5)
                self.assertLess(took, 3.5, "one second's grace, all at once")

    def test_the_python_start_retires_them_before_compose_up(self):
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        from onionpress.docker import Docker, DockerResult
        from test_onionpress_containers import _make_paths

        tmp = tempfile.mkdtemp(prefix="op-retire-py-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mounts = {
            "onionpress-tor": hd._mounts(("onionpress-persistent-data", "/var/lib/onionpress")),
            "onionheaven": hd._mounts((hd.VOLUME, hd.HUB_DIR)),
            "onionheaven-takeover-3": hd._mounts(("onionpress-persistent-data",
                                                  "/var/lib/onionpress")),
            "onionheaven-poll-1": "not-json",
            "onionpress-wordpress": "[]",
        }
        events = []

        def run(args, **kwargs):
            events.append(("run", list(args)))
            if args[:2] == ["ps", "-a"]:
                return DockerResult(0, "\n".join(mounts) + "\n", "")
            if args[0] == "inspect":
                return DockerResult(0, mounts[args[-1]], "")
            return DockerResult(0, "", "")

        def compose(args, **kwargs):
            events.append(("compose", list(args)))
            return DockerResult(0, "", "")

        docker = mock.Mock(spec=Docker)
        docker.run.side_effect = run
        docker.compose.side_effect = compose
        cm = ContainerManager(docker, _make_paths(tmp), PortConfig(offset=0, wp_port=8080,
                                                                   socks_port=9050,
                                                                   proxy_port=9077))
        self.assertTrue(cm.start_core())
        removed = sorted(e[1][2] for e in events if e[1][:2] == ["rm", "-f"])
        self.assertEqual(removed, ["onionheaven-takeover-3", "onionpress-tor"])
        last_rm = max(i for i, e in enumerate(events) if e[1][:2] == ["rm", "-f"])
        first_up = min(i for i, e in enumerate(events) if e[0] == "compose")
        self.assertLess(last_rm, first_up)
        self.assertFalse([e for e in events if "onionpress-wordpress" in e[1]
                          and e[1][0] == "inspect"])


# ---------------------------------------------------------------------------
# Backups hold a hub's data (adjacent: they never did on the Mac)
# ---------------------------------------------------------------------------

class TestHubBackups(unittest.TestCase):
    """create_backup waited for a master-key.json nothing writes any more,
    so no Mac (or `onionpress.cli backup`) backup held a hub's registry or
    keys; Linux took any node with an onionheaven container and copied
    registry.db without its write-ahead log. The container side here is
    the real shell, run on a folder standing in for /var/lib/onionheaven."""

    def setUp(self):
        import test_backup_staging_links as bsl
        self.bsl = bsl
        bsl.mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-hub-backup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = os.path.join(self.tmp, "hub")
        os.makedirs(os.path.join(self.hub, "keys"))
        self.wp = os.path.join(self.tmp, "wp-content")
        os.makedirs(os.path.join(self.wp, "themes"))
        with open(os.path.join(self.wp, "themes", "a.css"), "w") as f:
            f.write("css")
        pem = os.path.join(self.tmp, "key.pem")
        with open(pem, "wb") as f:
            f.write(bsl.mv.key_manager.build_openssh_key(bsl.mv.PRIV, bsl.mv.PUB))
        self.calls = os.path.join(self.tmp, "calls")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(f"""#!/bin/bash
echo "$*" >> {self.calls!r}
if [ "$1 $2 $3 $4" = "exec onionheaven sh -c" ]; then
    export COPYFILE_DISABLE=1       # macOS tar: no ._ files
    exec sh -c "${{5//\\/var\\/lib\\/onionheaven/{self.hub}}}"
fi
case "$*" in
  *ks_hs_id.ed25519_expanded_private*) cat {pem!r} ;;
  *"wp config get DB_PASSWORD"*) echo pw ;;
  *"wp config get"*) echo wordpress ;;
  *mariadb-dump*) echo "CREATE TABLE wp_posts;" ;;
  *"tar -cf -"*) COPYFILE_DISABLE=1 tar -cf - -C {self.wp!r} . ;;
  *) exit 1 ;;
esac
""")
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep
                                               + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.logs = []

    def _registry(self, rows, keep_open=False):
        conn = sqlite3.connect(os.path.join(self.hub, "registry.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE registry (content_address TEXT)")
        conn.executemany("INSERT INTO registry VALUES (?)", [(f"r{i}",) for i in range(rows)])
        conn.commit()
        if keep_open:
            self.addCleanup(conn.close)
        else:
            conn.close()

    def _write(self, rel, data):
        path = os.path.join(self.hub, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(data)

    def _backup(self):
        from onionpress import backup
        out = os.path.join(self.tmp, "backup.zip")
        backup.create_backup(self.bsl.mv.ADDR, "admin", self.bsl.PW, out, "2.5.2",
                             self.logs.append, data_dir=self.data_dir)
        return self.bsl._inner_names_and_bytes(out, self.bsl.PW)

    def test_a_hub_backup_holds_its_registry_and_keys_and_nothing_else(self):
        self._registry(rows=6, keep_open=True)      # the rows are in -wal only
        self._write(f"keys/{A}/{KEY}", "KEY-A")
        self._write("heartbeat.log", "log")
        self._write(f"legacy-leftover-20261012T000000Z/keys/{B}/{KEY}", "KEY-B")
        self._write(MARKER, "{}")
        members = self._backup()
        names = {n.rstrip("/") for n in members}
        self.assertIn(f"onionheaven/keys/{A}/{KEY}", names)
        self.assertEqual(members[f"onionheaven/keys/{A}/{KEY}"], b"KEY-A")
        self.assertFalse([n for n in names if n.startswith("onionheaven/")
                          and not n.startswith(("onionheaven/keys", "onionheaven/registry.db"))],
                         names)
        self.assertNotIn("onionheaven/registry.db-wal", names)
        snapshot = os.path.join(self.tmp, "registry.db")
        with open(snapshot, "wb") as f:
            f.write(members["onionheaven/registry.db"])
        self.assertEqual(hd._rows(snapshot), 6)
        meta = json.loads(members["metadata.json"])
        self.assertTrue(meta["is_onionheaven"])

    def test_a_node_without_hub_data_backs_up_none(self):
        self._registry(rows=0)          # a status query made the schema
        members = self._backup()
        self.assertFalse([n for n in members if n.startswith("onionheaven")])
        self.assertFalse(json.loads(members["metadata.json"])["is_onionheaven"])

    def test_asking_never_makes_a_registry(self):
        from onionpress import backup
        self.assertEqual(backup.hub_data_state(), "none")
        self.assertFalse(os.path.exists(os.path.join(self.hub, "registry.db")))
        self._write(f"keys/{A}/{KEY}", "KEY-A")
        self.assertEqual(backup.hub_data_state(), "hub")

    def test_restore_goes_through_sqlite_and_takes_only_registry_and_keys(self):
        import io
        import tarfile
        from onionpress import backup, key_manager
        priv = b"\x0b" * 64
        pub = key_manager.derive_public_key(priv)
        addr = key_manager.derive_onion_address(pub)
        staging = os.path.join(self.tmp, "staging")
        os.makedirs(os.path.join(staging, "database"))
        with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
            f.write("SELECT 1;\n")
        src = os.path.join(staging, "onionheaven")
        files = {"registry.db": b"SQLite format 3\x00registry",
                 f"keys/{addr}/{KEY}": key_manager.build_openssh_key(priv, pub),
                 f"keys/{addr}/hostname": b"evil\n",
                 f"keys/{A}/{KEY}": b"not a key",
                 "master-key.json": b"m", ".master-key-unlocked": b"u",
                 "heartbeat.log": b"log"}
        for rel, data in files.items():
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs.get("input")))
            out = "restored dropped=0\n" if argv[-1] == backup.REGISTRY_RESTORE_SH else (
                "wordpress\n" if "get" in argv else "")
            return subprocess.CompletedProcess(
                argv, 0, stdout=out if kwargs.get("text") else out.encode(),
                stderr="" if kwargs.get("text") else b"")

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            backup.restore_container_artifacts(staging, {}, lambda msg: None,
                                               data_dir=self.tmp)
        hub_calls = [(c, i) for c, i in calls if any("onionheaven" in a for a in c)]
        self.assertEqual([c for c, _ in hub_calls], [
            ["docker", "exec", "onionheaven", "mkdir", "-p", "/var/lib/onionheaven/keys"],
            ["docker", "cp", "-", "onionheaven:/var/lib/onionheaven/keys/"],
            ["docker", "exec", "-i", "onionheaven", "sh", "-c", backup.REGISTRY_RESTORE_SH],
        ])
        # The keys go in from a tar made here: the checked key (a fresh
        # PEM of it) and a hostname of our own; the key that is no key, the
        # backup's hostname and everything else stay out.
        with tarfile.open(fileobj=io.BytesIO(hub_calls[1][1])) as tf:
            members = {m.name: (tf.extractfile(m).read() if m.isfile() else None)
                       for m in tf.getmembers()}
        self.assertEqual(sorted(members), [addr, f"{addr}/hostname", f"{addr}/{KEY}"])
        self.assertEqual(members[f"{addr}/hostname"], (addr + "\n").encode())
        self.assertEqual(key_manager.parse_openssh_key(members[f"{addr}/{KEY}"]), (priv, pub))
        # The registry goes to SQLite's .restore, through the container's
        # checks, on stdin.
        self.assertEqual(hub_calls[2][1], files["registry.db"])
        self.assertIn(".restore '$t'", backup.REGISTRY_RESTORE_SH)

if __name__ == "__main__":
    unittest.main()
