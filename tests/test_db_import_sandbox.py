#!/usr/bin/env python3
"""A restored dump, and the DB password re-set after it, run only SQL.

The MariaDB client reads `source`, `\\!`, `system`, `\\.`, `tee` and `pager`
inside a sourced file and runs them as OS root in onionpress-db, and
LOAD DATA LOCAL INFILE reads the exec'd process's inherited
MYSQL_ROOT_PASSWORD out of /proc. The Settings page's restore reaches this
path from WordPress, so a crafted dump — or a wp-config.php password poisoned
to `x'; \\! cmd` for the ALTER USER re-set — gave a root shell in the db
container.

So the one import, backup.db_import (Phase 4, #120: what
restore_container_artifacts runs on both platforms; the Linux launcher's
own db_import went with issue #406), streams
the dump on stdin (never docker cp'd to a fixed /tmp/wordpress.sql and
sourced) through a client run with ``--sandbox --binary-mode
--local-infile=0``, and the password re-set validates its value (no
quote/backslash/backtick/control char) and runs the root client
``--binary-mode --sandbox``. These tests scan the shipped import paths and
exercise backup.py's import + validation and the CLI; nothing here talks to
Docker (subprocess is mocked, or `docker` is a stub on PATH).
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import backup, cli, db_import, wp_files  # noqa: E402

MAC = os.path.join(PROJECT_DIR, "app", "MacOS", "onionpress")
LINUX = os.path.join(PROJECT_DIR, "linux", "onionpress")
BACKUP_PY = os.path.join(PROJECT_DIR, "src", "onionpress", "backup.py")
# The import itself (backup.db_import is a re-export of it).
DB_IMPORT_PY = os.path.join(PROJECT_DIR, "src", "onionpress", "db_import.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text,
                  re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


# ── Source scan: no import path uses docker cp / source / \. ───────────────

class TestNoInsecureImportInSources(unittest.TestCase):
    """Every shipped import path, scanned as text."""

    def test_launchers_have_no_docker_cp_of_a_dump(self):
        for path in (MAC, LINUX):
            text = _read(path)
            self.assertNotIn("onionpress-db:/tmp/wordpress.sql", text,
                             f"{path}: still docker cp's a dump into the db container")
            self.assertNotIn("source /tmp/wordpress.sql", text,
                             f"{path}: still sources a dump file")

    def test_backup_py_has_no_docker_cp_of_a_dump(self):
        text = _read(BACKUP_PY) + _read(DB_IMPORT_PY)
        self.assertNotIn("onionpress-db:/tmp/wordpress.sql", text)
        self.assertNotIn("/tmp/wordpress.sql", text)
        self.assertNotIn("source /tmp/wordpress.sql", text)
        # No `-e 'source ...'` SQL to run a file either.
        self.assertNotRegex(text, r"source\s+/tmp/")

    def test_launchers_import_no_dump(self):
        # Every restore is onionpress.cli's (backup.py's sandboxed import):
        # the launchers' own restore_backup and db_import are gone (the
        # Linux ones with issue #406).
        for path in (MAC, LINUX):
            with self.subTest(path):
                text = _read(path)
                self.assertNotIn("db_import", text)
                self.assertNotRegex(text, r"\bmariadb\b(?! *--sandbox)[^\n]*wordpress\s*$")


# ── backup.db_import: the one implementation ───────────────────────────────

class TestDbImport(unittest.TestCase):
    """backup.db_import against a mocked subprocess.run."""

    DUMP = b"/*M!999999\\- enable the sandbox mode */\nSELECT 1;\n\x00\xff bin\n"
    PASSWORD = "p a$s\\w'o\"rd`x"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="db-import-fn-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sql = os.path.join(self.tmp, "wordpress.sql")
        with open(self.sql, "wb") as f:
            f.write(self.DUMP)

    def _runner(self, *, probe_rc=0, import_rc=0, stderr=b""):
        runs = []

        def fake_run(argv, **kw):
            argv = list(argv)
            runs.append((argv, kw))
            if "grep -q -- --sandbox" in " ".join(argv):
                return subprocess.CompletedProcess(argv, probe_rc, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(argv, import_rc, stdout=b"", stderr=stderr)
        return runs, fake_run

    def test_streams_the_dump_after_the_password_into_a_sandboxed_client(self):
        runs, fake_run = self._runner()
        with mock.patch.object(backup.subprocess, "run", fake_run):
            backup.db_import(self.sql, self.PASSWORD)
        self.assertEqual(len(runs), 2, runs)
        probe, (argv, kw) = runs[0][0], runs[1]
        # The capability check comes first, in the db container.
        self.assertEqual(probe[:3], ["docker", "exec", "onionpress-db"])
        self.assertIn("mariadb --help", " ".join(probe))
        # The import: a mariadb client in onionpress-db via docker exec -i,
        # every safe flag, the wordpress user and database.
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "onionpress-db"])
        self.assertEqual(argv[-7:], ["mariadb", "--sandbox", "--binary-mode",
                                     "--local-infile=0", "-u", "wordpress",
                                     "wordpress"])
        # No docker cp, no /tmp path, the password nowhere in argv.
        self.assertNotIn("cp", argv)
        self.assertNotIn("/tmp/wordpress.sql", " ".join(argv))
        for a in argv:
            self.assertNotIn(self.PASSWORD, a)
        for a in probe:
            self.assertNotIn(self.PASSWORD, a)
        # stdin is the password line followed by the dump bytes verbatim.
        self.assertEqual(kw["input"], self.PASSWORD.encode() + b"\n" + self.DUMP)
        # Sized to the dump (issue #317), never a fixed limit.
        self.assertEqual(kw["timeout"], wp_files.sized_timeout(
            len(self.DUMP), backup._DB_IMPORT_FLOOR_S, backup._DB_IMPORT_BYTES_PER_S))

    def test_user_and_database_are_the_callers(self):
        runs, fake_run = self._runner()
        with mock.patch.object(backup.subprocess, "run", fake_run):
            backup.db_import(self.sql, self.PASSWORD, user="wpu", name="wpdb")
        self.assertEqual(runs[1][0][-3:], ["-u", "wpu", "wpdb"])

    def test_timeout_grows_with_the_dump(self):
        big = os.path.join(self.tmp, "big.sql")
        with open(big, "wb") as f:
            f.truncate(64 << 20)
        runs, fake_run = self._runner()
        with mock.patch.object(backup.subprocess, "run", fake_run):
            backup.db_import(big, self.PASSWORD)
        self.assertEqual(runs[1][1]["timeout"],
                         backup._DB_IMPORT_FLOOR_S + (64 << 20) // backup._DB_IMPORT_BYTES_PER_S)
        self.assertGreater(runs[1][1]["timeout"], backup._DB_IMPORT_FLOOR_S)

    def test_old_client_is_refused_before_anything_is_sent(self):
        runs, fake_run = self._runner(probe_rc=1)
        with mock.patch.object(backup.subprocess, "run", fake_run):
            with self.assertRaises(Exception) as cm:
                backup.db_import(self.sql, self.PASSWORD)
        self.assertIn("too old", str(cm.exception).lower())
        self.assertEqual(len(runs), 1, "the import ran against an old client")

    def test_a_failed_import_raises_with_the_clients_words(self):
        runs, fake_run = self._runner(import_rc=1, stderr=b"ERROR 1064 near 'x'")
        with mock.patch.object(backup.subprocess, "run", fake_run):
            with self.assertRaises(Exception) as cm:
                backup.db_import(self.sql, self.PASSWORD)
        self.assertIn("Database import failed", str(cm.exception))
        self.assertIn("ERROR 1064", str(cm.exception))

    def test_a_password_that_is_not_one_stdin_line_is_refused(self):
        runs, fake_run = self._runner()
        for bad in ("", "a\nb", "a\rb", "x\n"):
            with self.subTest(password=bad), \
                    mock.patch.object(backup.subprocess, "run", fake_run):
                with self.assertRaises(Exception):
                    backup.db_import(self.sql, bad)
        self.assertEqual(runs, [])


# ── backup.py restore behaviour ────────────────────────────────────────────

def _staging_with_dump(dump_bytes):
    staging = tempfile.mkdtemp(prefix="db-import-test-")
    os.makedirs(os.path.join(staging, "database"))
    with open(os.path.join(staging, "database", "wordpress.sql"), "wb") as f:
        f.write(dump_bytes)
    return staging


def _is_import_run(argv, stdin):
    return (isinstance(stdin, (bytes, bytearray))
            and "mariadb" in argv and "--sandbox" in argv)


class TestBackupImportStreamsSandboxed(unittest.TestCase):
    """restore_container_artifacts: the DB import and ALTER USER re-set."""

    DUMP = b"/*M!999999\\- enable the sandbox mode */\nSELECT 1;\n\x00\xff bin\n"
    PASSWORD = "Abc123SecretsFilePwXYZ0987654321"

    def _run(self, *, db_user="wordpress", db_name="wordpress"):
        staging = _staging_with_dump(self.DUMP)
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        runs = []

        def fake_run(argv, **kw):
            argv = list(argv)
            runs.append((argv, kw.get("input")))
            # The sandbox capability probe: pretend the client supports it.
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        with mock.patch.object(backup, "check_staged_tree"), \
             mock.patch.object(db_import, "_assert_db_client_sandboxed"), \
             mock.patch.object(backup, "_get_db_credentials",
                               return_value={"name": db_name, "user": db_user,
                                             "password": "wpconfig-pw-ignored"}), \
             mock.patch.object(backup, "_restore_db_password",
                               return_value=self.PASSWORD), \
             mock.patch.object(backup, "_log_workload_stats"), \
             mock.patch.object(backup, "_ensure_multisite_constants"), \
             mock.patch.object(backup.subprocess, "run", fake_run):
            backup.restore_container_artifacts(staging, {}, lambda *a: None,
                                               hub_volumes=False)
        return runs

    def test_import_uses_safe_flags_and_streams_on_stdin(self):
        runs = self._run()
        imports = [(a, s) for a, s in runs if _is_import_run(a, s)]
        self.assertEqual(len(imports), 1, f"expected one import run: {runs}")
        argv, stdin = imports[0]
        for flag in ("--sandbox", "--binary-mode", "--local-infile=0"):
            self.assertIn(flag, argv, f"import argv missing {flag}: {argv}")
        # It runs a mariadb client in onionpress-db via docker exec -i.
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "onionpress-db"])
        # No docker cp, no /tmp path.
        self.assertNotIn("cp", argv)
        self.assertNotIn("/tmp/wordpress.sql", " ".join(argv))
        # stdin is the password line followed by the dump bytes verbatim.
        self.assertEqual(stdin, self.PASSWORD.encode() + b"\n" + self.DUMP)
        # Password never in argv.
        for a in argv:
            self.assertNotIn(self.PASSWORD, a)

    def test_no_docker_cp_or_rm_of_a_dump(self):
        runs = self._run()
        for argv, _ in runs:
            self.assertNotIn("cp", argv, f"unexpected docker cp: {argv}")
            self.assertNotIn("/tmp/wordpress.sql", " ".join(argv))

    def test_alter_user_runs_sandboxed_binary_mode(self):
        runs = self._run()
        alters = [(argv, stdin) for argv, stdin in runs
                  if isinstance(stdin, str) and "ALTER USER" in stdin]
        self.assertEqual(len(alters), 1, f"expected one ALTER USER run: {runs}")
        argv, stdin = alters[0]
        shell = argv[argv.index("-c") + 1]
        self.assertIn("--binary-mode", shell)
        self.assertIn("--sandbox", shell)
        self.assertIn("mariadb -uroot", shell)
        # Validated password is spliced into the (quoted) SQL literal; the
        # root password stays in MYSQL_PWD from the container env.
        self.assertIn(f"IDENTIFIED BY '{self.PASSWORD}'", stdin)
        for a in argv:
            self.assertNotIn(self.PASSWORD, a)

    def test_old_client_is_refused(self):
        staging = _staging_with_dump(self.DUMP)
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)

        def fake_run(argv, **kw):
            # the sandbox capability probe greps the client help: non-zero
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")

        with mock.patch.object(backup, "check_staged_tree"), \
             mock.patch.object(backup, "_get_db_credentials",
                               return_value={"name": "wordpress", "user": "wordpress",
                                             "password": "x"}), \
             mock.patch.object(backup, "_restore_db_password", return_value="x"), \
             mock.patch.object(backup.subprocess, "run", fake_run):
            with self.assertRaises(Exception) as cm:
                backup.restore_container_artifacts(staging, {}, lambda *a: None,
                                                   hub_volumes=False)
        self.assertIn("too old", str(cm.exception).lower())


# ── Credential validation ──────────────────────────────────────────────────

class TestDbValueValidation(unittest.TestCase):

    def test_password_injection_is_refused(self):
        for bad in ("x'; \\! id #", 'a"b', "a\\b", "a`b", "a\nb", "a\x00b",
                    "a\tb", ""):
            with self.assertRaises(Exception):
                backup._reject_unsafe_db_value("DB_PASSWORD", bad)

    def test_legit_passwords_pass(self):
        for good in ("Abc123XYZ0000000000000000000000x",
                     "p@ss-word.1+under_score", "A" * 32):
            self.assertEqual(backup._reject_unsafe_db_value("DB_PASSWORD", good),
                             good)

    def test_identifier_leading_dash_refused_as_argv(self):
        with self.assertRaises(Exception):
            backup._reject_unsafe_db_value("DB_NAME", "-local-infile=1",
                                           as_argv=True)
        # but fine as a password (never used as argv)
        self.assertEqual(backup._reject_unsafe_db_value("P", "-x"), "-x")

    def test_get_db_credentials_validates(self):
        def creds_run(values):
            seq = iter(values)

            def _run(argv, **kw):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=next(seq), stderr="")
            return _run

        # a poisoned DB_PASSWORD (quote) is refused
        with mock.patch.object(backup.subprocess, "run",
                               creds_run(["wordpress\n", "wordpress\n",
                                          "x'; DROP\n"])):
            with self.assertRaises(Exception):
                backup._get_db_credentials()
        # a DB_NAME beginning with '-' is refused
        with mock.patch.object(backup.subprocess, "run",
                               creds_run(["-evil\n", "wordpress\n", "ok\n"])):
            with self.assertRaises(Exception):
                backup._get_db_credentials()
        # legit values pass
        with mock.patch.object(backup.subprocess, "run",
                               creds_run(["wordpress\n", "wordpress\n",
                                          "Abc123\n"])):
            creds = backup._get_db_credentials()
            self.assertEqual(creds["name"], "wordpress")
            self.assertEqual(creds["password"], "Abc123")


class TestRestoreDbPasswordSource(unittest.TestCase):
    """The host secrets file is the source of truth, not wp-config.php."""

    def test_prefers_secrets_file(self):
        d = tempfile.mkdtemp(prefix="secrets-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "secrets"), "w") as f:
            f.write("WORDPRESS_DB_PASSWORD='FromSecretsFile12345'\n")
        self.assertEqual(
            backup._restore_db_password(d, "from-wp-config"),
            "FromSecretsFile12345")

    def test_falls_back_to_wp_config_when_no_secrets(self):
        d = tempfile.mkdtemp(prefix="secrets-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertEqual(
            backup._restore_db_password(d, "FromWpConfig123"),
            "FromWpConfig123")

    def test_refuses_an_unsafe_fallback(self):
        d = tempfile.mkdtemp(prefix="secrets-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with self.assertRaises(Exception):
            backup._restore_db_password(d, "x'; \\! id #")


if __name__ == "__main__":
    unittest.main()
