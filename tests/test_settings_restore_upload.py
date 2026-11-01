#!/usr/bin/env python3
"""The Settings page's restore reads only a regular file as the backup.

The page saves the uploaded backup in the WordPress container as
/var/lib/onionpress/restore-upload.zip, and the host copies it out with
docker cp, which copies a symbolic link as a link. Code running as
www-data can put a link there, pointing at a host path, and the restore
would read that host file as the backup. Both platforms copy into a folder
of their own and refuse anything but a regular file before the restore.

No docker: the Mac handler is AppKit-bound and checked as source; the Linux
branch runs for real with docker stubbed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402

SECRET = b"host file the site wants read as its backup\n"


class TestCheckUploadedBackup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.secret = os.path.join(self.tmp, "secret")
        with open(self.secret, "wb") as f:
            f.write(SECRET)

    def test_a_regular_file_passes(self):
        backup.check_uploaded_backup(self.secret)

    def test_anything_else_is_refused(self):
        link = os.path.join(self.tmp, "link.zip")
        os.symlink(self.secret, link)
        dir_link = os.path.join(self.tmp, "dirlink.zip")
        os.symlink(self.tmp, dir_link)
        fifo = os.path.join(self.tmp, "fifo.zip")
        os.mkfifo(fifo)
        for path in (link, dir_link, fifo, self.tmp):
            with self.subTest(os.path.basename(path)):
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    backup.check_uploaded_backup(path)
        with self.assertRaisesRegex(ValueError, "No backup file"):
            backup.check_uploaded_backup(os.path.join(self.tmp, "missing.zip"))


class TestHandlerSettingsRestore(unittest.TestCase):
    """The shared handler's restore-backup (onionpress.handle_action, both
    platforms): the upload is copied into a folder of its own, checked to
    be a regular file before anything reads it, the copy sized and
    removed. tests/test_handle_action.py runs it with a link, a FIFO, a
    missing upload and a regular file; this reads the source."""

    def setUp(self):
        src = mv._read("src/onionpress/handle_action.py")
        self.body = src[src.index("def _restore_backup(host, confirmed):"):
                        src.index("def _settings_page_restore(")]

    def test_the_copy_is_checked_before_the_restore_reads_it(self):
        check = self.body.index("check_uploaded_backup(local_zip)")
        self.assertLess(self.body.index('["cp", f"onionpress-wordpress:{upload}", local_zip]'), check)
        self.assertLess(check, self.body.index("_settings_page_restore(host, local_zip"))

    def test_no_fixed_time_limit_on_the_menu_restore(self):
        """Issue #317: a fixed 1800 s ended a large site's restore halfway."""
        src = mv._read("src/menubar.py")
        start = src.index("    def do_restore():")
        body = src[start:src.index("threading.Thread(target=do_restore", start)]
        self.assertIn("ui_client.run_restore(", body)
        src = mv._read("src/onionpress/ui_client.py")
        body = src[src.index("def run_restore("):src.index("def restore_summary(")]
        call = body[body.index("launcher_ops.run_launcher_restore("):]
        self.assertIn("timeout=None)", call[:call.index("return RestoreResult")])

    def test_the_copy_is_sized_and_checked(self):
        self.assertNotIn("timeout=60\n", self.body)
        self.assertIn("timeout=wp_files.sized_timeout(size, 60)", self.body)
        self.assertIn("if not r.ok:", self.body)

    def test_the_copy_lands_in_a_folder_of_its_own(self):
        self.assertIn("tempfile.mkdtemp(", self.body)
        self.assertNotIn("gettempdir()", self.body)
        self.assertNotIn("os.path.exists(local_zip)", self.body)
        self.assertIn("shutil.rmtree(work", self.body)

    def test_the_launchers_copy_nothing_out_themselves(self):
        for rel in ("linux/onionpress", "app/MacOS/onionpress", "src/menubar.py"):
            with self.subTest(rel):
                self.assertNotIn("restore-upload.zip", mv._read(rel))


if __name__ == "__main__":
    unittest.main()
