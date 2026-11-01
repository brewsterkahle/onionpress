#!/usr/bin/env python3
"""The launchers' setup_db_passwords() is `onionpress ensure-secrets`
(issue #529): secrets_file.ensure_secrets makes the file once, the
subcommand refuses an incomplete one (the Mac's truncated-file guard, on
both platforms now), and the bash keeps only the `source` and the export
compose reads."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from onionpress import cli, secrets_file  # noqa: E402
from launcher_harness import BASH, Launcher  # noqa: E402


class TestSubcommand(unittest.TestCase):

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-secrets-")
        self.addCleanup(shutil.rmtree, self.data, True)

    def run_cmd(self):
        out = io.StringIO()
        with mock.patch.object(cli, "OnionPressCLI") as MockCLI, contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", self.data, "ensure-secrets"])
        MockCLI.assert_not_called()
        return rc, [l.split("] ", 1)[1] for l in out.getvalue().splitlines()]

    def test_the_file_is_made_once_and_said_so(self):
        rc, lines = self.run_cmd()
        self.assertEqual(rc, 0)
        path = os.path.join(self.data, "secrets")
        self.assertEqual(lines, ["Generating random database passwords...",
                                 f"Database passwords generated and saved to {path}"])
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        s = secrets_file.load_secrets(path)
        self.assertEqual(len(s.wordpress_db_password), 32)
        rc, lines = self.run_cmd()
        self.assertEqual((rc, lines), (0, []))
        self.assertEqual(secrets_file.load_secrets(path), s, "never regenerated")

    def test_an_incomplete_file_is_refused(self):
        path = os.path.join(self.data, "secrets")
        with open(path, "w") as f:
            f.write("# truncated\nWORDPRESS_DB_PASSWORD=''\n")
        rc, lines = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertEqual(lines, [f"ERROR: {path} is incomplete (empty database password) — not "
                                 "starting. The database was initialised with these passwords, "
                                 "so restore the file rather than regenerating it."])

    def test_a_file_that_cannot_be_made_is_an_error_line(self):
        with mock.patch.object(secrets_file, "ensure_secrets", side_effect=OSError("disk full")):
            rc, lines = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("could not be made or read: disk full", lines[-1])


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class TestLauncherSourcesTheFile(unittest.TestCase):
    """setup_db_passwords under both launchers: the CLI call, then the
    file sourced and exported; the CLI's refusal ends the verb."""

    RECORD = ('stop_containers() { _op_rec "env ${WORDPRESS_DB_PASSWORD:-unset}'
              ' ${MYSQL_PASSWORD:-unset} ${MYSQL_ROOT_PASSWORD:-unset}"; }')

    def launcher(self, platform):
        launcher = Launcher(self, platform, stub=("stop_onionheaven_farm", "daemon_offline"))
        launcher.add_stubs(self.RECORD)
        return launcher

    def test_the_passwords_reach_the_environment(self):
        for platform in ("mac", "linux"):
            with self.subTest(platform=platform):
                launcher = self.launcher(platform)
                r = launcher.run("stop")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"--data-dir {launcher.data_dir} ensure-secrets", launcher.py_calls())
                self.assertIn("env stub-wp stub-wp stub-root", launcher.calls())

    def test_a_refused_file_ends_the_verb(self):
        for platform in ("mac", "linux"):
            with self.subTest(platform=platform):
                launcher = self.launcher(platform)
                r = launcher.run("stop", env={"STUB_RC_ENSURE_SECRETS": "1"})
                self.assertEqual(r.returncode, 1)
                self.assertFalse(any(c.startswith("env ") for c in launcher.calls()))

    def test_the_bash_bodies_hold_only_the_source_and_the_export(self):
        root = os.path.dirname(HERE)
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read()
            i = text.index("\nsetup_db_passwords() {")
            body = text[i:text.index("\n}\n", i)]
            with self.subTest(rel):
                self.assertIn('ensure-secrets 2>&1 | tee -a "$LOG_FILE"', body)
                self.assertIn('source "$DATA_DIR/secrets"', body)
                self.assertNotIn("urandom", text)


if __name__ == "__main__":
    unittest.main()
