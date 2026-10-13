#!/usr/bin/env python3
"""A restored dump, and the DB password re-set after it, run only SQL.

The MariaDB client reads `source`, `\\!`, `system`, `\\.`, `tee` and `pager`
inside a sourced file and runs them as OS root in onionpress-db, and
LOAD DATA LOCAL INFILE reads the exec'd process's inherited
MYSQL_ROOT_PASSWORD out of /proc. The Settings page's restore reaches this
path from WordPress, so a crafted dump — or a wp-config.php password poisoned
to `x'; \\! cmd` for the ALTER USER re-set — gave a root shell in the db
container.

So every import streams the dump on stdin (never docker cp'd to a fixed
/tmp/wordpress.sql and sourced) through a client run with
``--sandbox --binary-mode --local-infile=0``, and the password re-set
validates its value (no quote/backslash/backtick/control char) and runs the
root client ``--binary-mode --sandbox``. These tests scan the shipped import
paths and exercise backup.py's import + validation; nothing here talks to
Docker (subprocess is mocked).
"""
import os
import re
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

from onionpress import backup  # noqa: E402

MAC = os.path.join(PROJECT_DIR, "app", "MacOS", "onionpress")
LINUX = os.path.join(PROJECT_DIR, "linux", "onionpress")
BACKUP_PY = os.path.join(PROJECT_DIR, "src", "onionpress", "backup.py")


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
        text = _read(BACKUP_PY)
        self.assertNotIn("onionpress-db:/tmp/wordpress.sql", text)
        self.assertNotIn("/tmp/wordpress.sql", text)
        self.assertNotIn("source /tmp/wordpress.sql", text)
        # No `-e 'source ...'` SQL to run a file either.
        self.assertNotRegex(text, r"source\s+/tmp/")

    def test_launcher_db_import_uses_the_safe_flags_on_stdin(self):
        for path in (LINUX,):
            fn = _shell_function(_read(path), "db_import")
            for flag in ("--sandbox", "--binary-mode", "--local-infile=0"):
                self.assertIn(flag, fn, f"{path}: db_import missing {flag}")
            # dump streamed after the password line, no docker cp
            self.assertIn('printf \'%s\\n\' "$db_pass"', fn)
            self.assertIn('cat "$sql_file"', fn)
            self.assertNotIn("docker cp", fn)
            # refuses an old client rather than falling back unsandboxed
            self.assertIn("grep -q -- --sandbox", fn)

    def test_launcher_restore_calls_db_import(self):
        self.assertRegex(_read(LINUX), r'db_import "\$db_pass" "\$sql_file"',
                         f"{LINUX}: restore does not call db_import")

    def test_mac_launcher_imports_no_dump(self):
        # Its `restore` is onionpress.cli restore (backup.py's sandboxed
        # import); the launcher's own restore_backup and db_import are gone.
        text = _read(MAC)
        self.assertNotIn("db_import", text)
        self.assertNotRegex(text, r"\bmariadb\b(?! *--sandbox)[^\n]*wordpress\s*$")


# ── backup.py import behaviour ─────────────────────────────────────────────

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
             mock.patch.object(backup, "_assert_db_client_sandboxed"), \
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
