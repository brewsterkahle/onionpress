#!/usr/bin/env python3
"""Backup and restore time limits grow with the site (issue #317).

Fixed limits (120 s for the database dump, 30 s + 120 s for its import,
300 s for wp-content, 300 s / 600 s in the Linux tray, 1800 s in the menu)
ended large sites' backups and restores halfway. Each copy now gets a floor
plus its size at wp_files.COPY_BYTES_PER_S, a process that runs over it is
killed, the dump streams to its file instead of memory, and the UIs that
start the launcher wait for it, telling the user when it fails.

No docker: the container side is a stub script.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import backup, wp_files  # noqa: E402

TRAY = os.path.join(PROJECT_ROOT, "linux", "onionpress-tray")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestSizedTimeout(unittest.TestCase):

    def test_floor_plus_size(self):
        self.assertEqual(wp_files.sized_timeout(0, 300), 300)
        self.assertEqual(wp_files.sized_timeout(None, 300), 300)
        self.assertEqual(wp_files.sized_timeout(-5, 300), 300)
        gib = 1 << 30
        self.assertEqual(wp_files.sized_timeout(10 * gib, 300),
                         300 + 10 * gib // wp_files.COPY_BYTES_PER_S)
        self.assertEqual(wp_files.sized_timeout(gib, 120, 256 << 10), 120 + 4096)

    def test_a_large_site_gets_more_than_the_old_fixed_limits(self):
        gib = 1 << 30
        self.assertGreater(wp_files.sized_timeout(gib, backup._DB_DUMP_FLOOR_S), 120)
        self.assertGreater(wp_files.sized_timeout(
            gib, backup._DB_IMPORT_FLOOR_S, backup._DB_IMPORT_BYTES_PER_S), 120)
        self.assertGreater(wp_files.sized_timeout(
            10 * gib, backup._STAGE_COPY_FLOOR_S), 300)


class _Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path


class TestDumpToFile(_Tmp):

    def test_streams_to_the_file_and_reads_the_password_from_stdin(self):
        client = self._script("client", 'read -r pw; echo "dump for $pw"; echo more\n')
        out = os.path.join(self.tmp, "wordpress.sql")
        backup._run_db_client_to_file([client], "s3cret", out, 30, "Database export")
        self.assertEqual(_read(out), "dump for s3cret\nmore\n")

    def test_a_failure_raises_with_its_stderr(self):
        client = self._script("client", 'cat >/dev/null; echo "access denied" >&2; exit 2\n')
        with self.assertRaisesRegex(Exception, "Database export failed: access denied"):
            backup._run_db_client_to_file([client], "pw", os.path.join(self.tmp, "o"),
                                          30, "Database export")

    def test_a_hang_is_killed_at_the_limit(self):
        client = self._script("client", 'cat >/dev/null; exec sleep 30\n')
        start = time.monotonic()
        with self.assertRaisesRegex(Exception, "did not finish within 1 s"):
            backup._run_db_client_to_file([client], "pw", os.path.join(self.tmp, "o"),
                                          1, "Database export")
        self.assertLess(time.monotonic() - start, 15)


class TestStageCopyLimit(_Tmp):

    def test_a_stuck_copy_is_killed_and_named(self):
        tar = self._script("tar", "exec sleep 30\n")
        start = time.monotonic()
        with self.assertRaisesRegex(Exception, "wp-content copy did not finish within 1 s"):
            backup._stage_container_tar([tar], os.path.join(self.tmp, "dest"),
                                        lambda m: None, "wp-content", timeout=1)
        self.assertLess(time.monotonic() - start, 15)

    def test_stage_wp_content_sizes_its_limit(self):
        seen = {}

        def fake(argv, dest, log_func, where, timeout):
            seen["timeout"] = timeout
        with mock.patch.object(backup, "_stage_container_tar", fake), \
             mock.patch.object(backup, "scrub_staged_tree"), \
             mock.patch.object(backup, "_du_bytes", return_value=5 << 30) as du:
            backup.stage_wp_content(os.path.join(self.tmp, "a"), print,
                                    "./creations", ".thumbs")
            du.assert_called_once_with("onionpress-wordpress", "/var/www/html/wp-content",
                                       excludes=("creations", ".thumbs"))
            self.assertEqual(seen["timeout"],
                             wp_files.sized_timeout(5 << 30, backup._STAGE_COPY_FLOOR_S))
            du.reset_mock()
            backup.stage_wp_content(os.path.join(self.tmp, "b"), print,
                                    wpcontent_bytes=1 << 30)
            du.assert_not_called()
            self.assertEqual(seen["timeout"],
                             wp_files.sized_timeout(1 << 30, backup._STAGE_COPY_FLOOR_S))


class TestPutTreeLimit(_Tmp):

    def test_sized_from_the_tree_when_not_given(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        with open(os.path.join(src, "big"), "wb") as f:
            f.truncate(3 << 20)
        timeouts = []

        class _Proc:
            stdin = open(os.devnull, "wb")

            def __init__(self, *a, **k):
                pass

            def wait(self):
                return 0

            def kill(self):
                pass

        def fake_own(*a, timeout, **k):
            timeouts.append(timeout)
        with mock.patch.object(wp_files, "own", fake_own), \
             mock.patch.object(wp_files.subprocess, "Popen", _Proc), \
             mock.patch.object(wp_files.threading, "Timer") as timer:
            wp_files.put_tree(src, "/var/www/html/wp-content", merge=True)
        expected = wp_files.sized_timeout(3 << 20, wp_files.PUT_TREE_FLOOR_S)
        self.assertEqual(timeouts, [expected])
        self.assertEqual(timer.call_args.args[0], expected)


class TestSources(unittest.TestCase):
    """The places that had fixed limits."""

    def setUp(self):
        self.backup = _read(os.path.join(PROJECT_ROOT, "src/onionpress/backup.py"))

    def _function(self, name):
        start = self.backup.index(f"\ndef {name}(")
        return self.backup[start:self.backup.index("\ndef ", start + 1)]

    def test_the_dump_is_not_held_in_memory(self):
        body = self._function("create_backup")
        self.assertIn("_run_db_client_to_file(", body)
        self.assertNotIn("timeout=120", body)
        self.assertIn("wpcontent_bytes=stats.get('wpcontent_bytes')", body)

    def test_the_import_is_sized(self):
        body = self._function("restore_container_artifacts")
        self.assertNotIn("timeout=30, check=True", body)
        self.assertNotIn("errors='replace', timeout=120", body)
        self.assertIn("sized_timeout(sql_bytes", body)

    def test_zip_and_unzip_are_sized(self):
        self.assertIn("sized_timeout(_tree_bytes(staging), _ZIP_FLOOR_S)",
                      self._function("pack_backup"))
        self.assertIn("max_total_bytes, _UNZIP_TIMEOUT_S)",
                      self._function("_unzip_cli_extract"))


class TestTray(unittest.TestCase):

    def setUp(self):
        self.tree = ast.parse(_read(TRAY))

    def _function(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(name)

    def _launcher_calls(self, fn):
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Launcher"):
                yield node

    def test_backup_and_restore_wait_for_the_launcher(self):
        for name, cmd in (("show_backup_dialog", "backup"),
                          ("show_restore_dialog", "restore")):
            with self.subTest(name):
                calls = [c for c in self._launcher_calls(self._function(name))
                         if isinstance(c.args[0], ast.Constant) and c.args[0].value == cmd]
                self.assertEqual(len(calls), 1)
                timeout = {k.arg: k.value for k in calls[0].keywords}["timeout"]
                self.assertIsInstance(timeout, ast.Constant)
                self.assertIsNone(timeout.value)

    def test_a_failure_to_run_is_notified(self):
        for name, title in (("show_backup_dialog", "Backup failed"),
                            ("show_restore_dialog", "Restore failed")):
            with self.subTest(name):
                fn = self._function(name)
                handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
                self.assertTrue(any(
                    isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_notify_failed"
                    and c.args[0].value == title
                    for h in handlers for c in ast.walk(h)), name)


if __name__ == "__main__":
    unittest.main()
