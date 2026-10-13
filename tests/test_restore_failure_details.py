#!/usr/bin/env python3
"""A failed restore says why (#323, follow-up to #312).

The menubar's Restore... reported a failed `onionpress restore` with only
"Check the log for details". It now goes through
launcher_ops.run_launcher_restore, which shows the launcher's last lines
(the Python CLI's log lines, timestamps dropped), the staging folder kept
for the next start's import (the .install-from-backup marker), and the
log's path. These tests run a fake launcher script that exits 1.
"""

import os
import re
import stat
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import launcher_ops  # noqa: E402

SECRET = "correct horse battery staple"


class TestRunLauncherRestore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)
        self.log_path = os.path.join(self.data_dir, "onionpress.log")
        self.argv_log = os.path.join(self.tmp, "argv")
        self.env_log = os.path.join(self.tmp, "env")

    def tearDown(self):
        self._tmp.cleanup()

    def _launcher(self, body):
        path = os.path.join(self.tmp, "onionpress")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n"
                    f'printf "%s\\n" "$*" > "{self.argv_log}"\n'
                    f'printf "%s" "${launcher_ops.BACKUP_PASSWORD_ENV}" > "{self.env_log}"\n'
                    + textwrap.dedent(body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def _run(self, launcher):
        ok, message, _stderr = launcher_ops.run_launcher_restore(
            launcher, "/backups/b.zip", SECRET,
            data_dir=self.data_dir, log_path=self.log_path)
        return ok, message

    def test_success(self):
        ok, message = self._run(self._launcher('echo "Done!"\nexit 0\n'))
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_password_in_environment_not_argv(self):
        self._run(self._launcher("exit 1\n"))
        with open(self.argv_log) as f:
            argv = f.read()
        self.assertEqual(argv.strip(), "restore /backups/b.zip")
        self.assertNotIn(SECRET, argv)
        with open(self.env_log) as f:
            self.assertEqual(f.read(), SECRET)

    def test_refused_backup_shows_the_reason(self):
        ok, message = self._run(self._launcher("""\
            echo "[2026-10-12 10:30:00] Restore: extracting backup" >&2
            echo "[2026-10-12 10:30:01] Restore failed during extract/seed: Wrong password, or the backup is damaged." >&2
            echo "ERROR: Restore failed — see $HOME/.onionpress/onionpress.log" >&2
            exit 1
            """))
        self.assertFalse(ok)
        self.assertIn("Restore failed during extract/seed: Wrong password, "
                      "or the backup is damaged.", message)
        self.assertNotIn("[2026-10-12", message)
        self.assertNotIn("Check the log for details", message)
        # The launcher's "see <log>" line is replaced by the path, once.
        self.assertNotIn("ERROR: Restore failed — see", message)
        self.assertTrue(message.endswith(f"Full details: {self.log_path}"))
        self.assertNotIn("kept in", message)

    def test_kept_staging_is_named(self):
        staging = os.path.join(self.data_dir, "restore-staging")
        os.mkdir(staging)
        with open(os.path.join(self.data_dir, ".install-from-backup"), "w") as f:
            f.write(staging + "\n")
        ok, message = self._run(self._launcher("""\
            echo "[2026-10-12 10:31:00] Restore: artifact import failed — staging kept for retry" >&2
            exit 1
            """))
        self.assertFalse(ok)
        self.assertIn("artifact import failed", message)
        self.assertIn(f"The unpacked backup is kept in {staging}; OnionPress "
                      "imports it again at its next start.", message)

    def test_marker_without_staging_is_not_named(self):
        with open(os.path.join(self.data_dir, ".install-from-backup"), "w") as f:
            f.write(os.path.join(self.data_dir, "gone") + "\n")
        ok, message = self._run(self._launcher("exit 1\n"))
        self.assertFalse(ok)
        self.assertNotIn("kept in", message)
        self.assertIn("Restore failed", message)

    def test_only_the_last_lines_printable_and_short(self):
        ok, message = self._run(self._launcher("""\
            i=1
            while [ $i -le 20 ]; do echo "line $i" >&2; i=$((i+1)); done
            printf 'name\\033[2J\\342\\200\\256x\\n' >&2
            printf '%0300d\\n' 0 >&2
            exit 1
            """))
        self.assertFalse(ok)
        self.assertNotIn("line 16\n", message)   # six lines: 17-20 and two more
        self.assertIn("line 17", message)
        self.assertIn("name[2Jx", message)          # ESC and bidi override dropped
        self.assertNotIn("\x1b", message)
        self.assertNotIn("‮", message)
        self.assertIn("0" * 200 + "…", message)
        self.assertNotIn("0" * 201, message)


class TestMenubarRestoreUsesIt(unittest.TestCase):
    """src/menubar.py (AppKit, so checked as source)."""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "src", "menubar.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("        def do_restore():")
        self.body = src[start:src.index("threading.Thread(target=do_restore", start)]

    def test_failure_dialog_shows_the_details(self):
        self.assertIn("launcher_ops.run_launcher_restore(", self.body)
        self.assertIn('pw.finish(failure,', self.body)
        self.assertNotIn("Check the log for details", self.body)
        self.assertIsNone(re.search(r"subprocess\.run\(\s*\[self\.launcher_script, \"restore\"",
                                    self.body))


if __name__ == "__main__":
    unittest.main()
