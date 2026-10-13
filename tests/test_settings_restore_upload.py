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


class TestMenubarSettingsRestore(unittest.TestCase):
    """src/menubar.py's restore-backup action (AppKit, so checked as source)."""

    def setUp(self):
        src = mv._read("src/menubar.py")
        start = src.index('elif action == "restore-backup":')
        self.body = src[start:src.index("\n            else:", start)]

    def test_the_copy_is_checked_before_the_restore_reads_it(self):
        check = self.body.index("backup_manager.check_uploaded_backup(local_zip)")
        self.assertLess(self.body.index('"docker", "cp"'), check)
        self.assertLess(check, self.body.index("read_backup_metadata(local_zip"))
        self.assertLess(check, self.body.index("_launcher_restore(local_zip"))

    def test_it_is_the_menus_restore(self):
        """Issue #317: the page's restore was an in-place overwrite of its
        own, separate from the menu's install-from-backup; now both go
        through _launcher_restore, the password checked before anything is
        torn down."""
        self.assertNotIn("restore_from_backup(", self.body)
        self.assertLess(self.body.index("read_backup_metadata(local_zip, password)"),
                        self.body.index("self._launcher_restore(local_zip, password)"))
        src = mv._read("src/menubar.py")
        start = src.index("    def do_restore():")
        self.assertIn("self._launcher_restore(zip_path, password)",
                      src[start:src.index("threading.Thread(target=do_restore", start)])

    def test_no_fixed_time_limit_on_the_restore(self):
        src = mv._read("src/menubar.py")
        start = src.index("    def _launcher_restore(self")
        body = src[start:src.index("\n    def ", start + 10)]
        call = body[body.index("subprocess.run("):]
        self.assertNotIn("timeout", call)

    def test_the_copy_is_sized_and_checked(self):
        self.assertNotIn("timeout=60\n", self.body)
        self.assertIn("timeout=wp_files.sized_timeout(size, 60)", self.body)
        self.assertIn("if r.returncode != 0:", self.body)

    def test_the_copy_lands_in_a_folder_of_its_own(self):
        self.assertIn("tempfile.mkdtemp(", self.body)
        self.assertNotIn("gettempdir()", self.body)
        self.assertNotIn("os.path.exists(local_zip)", self.body)
        self.assertIn("shutil.rmtree(upload_dir", self.body)


class TestLinuxSettingsRestore(unittest.TestCase):
    """linux/onionpress's restore-backup settings action, run for real with
    docker stubbed: `docker cp` makes whatever the case puts at the copy's
    path, as docker cp would copy it out of the container."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { :; }
stop_containers() { :; }
start_containers() { :; }
wait_for_services() { :; }
write_status() { :; }
write_restore_result() { echo "$1|$2" >> "$RESULT"; }
restore_backup() { echo "$2" >> "$CALLS"; cat "$2" > "$READ"; echo "$ADDR"; }
docker() {
    case "$1" in
        cp)
            case "$MODE" in
                link) ln -s "$SECRET_FILE" "$3" ;;
                fifo) mkfifo "$3" ;;
                file) printf 'uploaded zip' > "$3" ;;
                *) return 1 ;;
            esac ;;
        exec) [ "$2 $3 $5" = "-u www-data cat" ] && echo "$PW" ;;
    esac
    return 0
}
'''

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.secret = os.path.join(self.tmp, "secret")
        with open(self.secret, "wb") as f:
            f.write(SECRET)
        text = mv._read("linux/onionpress")
        head = "                restore-backup)\n"
        start = text.index(head) + len(head)
        self.branch = text[start:text.index("\n                    ;;\n", start)]
        self.is_onion = (mv._bash_function(text, "is_onion_address")
                         + mv._bash_function(text, "wp_exec"))

    def _run(self, mode):
        tmpdir = os.path.join(self.tmp, f"t-{mode}")
        os.makedirs(tmpdir)
        files = {k: os.path.join(self.tmp, f"{k}-{mode}")
                 for k in ("log", "calls", "result", "read")}
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(self.tmp, "home"),
            "TMPDIR": tmpdir, "MODE": mode, "SECRET_FILE": self.secret,
            "PW": "pw", "ADDR": mv.ADDR, "LOG_FILE": files["log"],
            "CALLS": files["calls"], "RESULT": files["result"],
            "READ": files["read"],
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
        }
        script = (self.PRELUDE + self.is_onion
                  + "run_branch() {\n" + self.branch + "\n}\nrun_branch\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

        def read(key):
            try:
                with open(files[key], "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return None
        # Nothing is left behind in TMPDIR, whatever happened.
        self.assertEqual(os.listdir(tmpdir), [])
        return read("result"), read("calls"), read("read")

    def test_a_link_is_refused_unread(self):
        result, calls, read = self._run("link")
        self.assertEqual(result, b"false|The uploaded backup is not a regular file\n")
        self.assertIsNone(calls)
        self.assertIsNone(read)

    def test_a_fifo_is_refused(self):
        result, calls, _ = self._run("fifo")
        self.assertEqual(result, b"false|The uploaded backup is not a regular file\n")
        self.assertIsNone(calls)

    def test_no_upload(self):
        result, calls, _ = self._run("none")
        self.assertEqual(result, b"false|Missing password or backup file\n")
        self.assertIsNone(calls)

    def test_a_regular_file_is_restored_from_a_private_folder(self):
        result, calls, read = self._run("file")
        self.assertEqual(result, f"true|{mv.ADDR}\n".encode())
        self.assertEqual(read, b"uploaded zip")
        path = calls.decode().strip()
        self.assertEqual(os.path.basename(path), "restore-upload.zip")
        self.assertTrue(os.path.basename(os.path.dirname(path))
                        .startswith("onionpress-restore-upload-"), path)


if __name__ == "__main__":
    unittest.main()
