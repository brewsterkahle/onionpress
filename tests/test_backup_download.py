#!/usr/bin/env python3
"""The Settings page's backup download serves and deletes backups only.

wp_ajax_onionpress_download_backup sent, then deleted, any file in
/var/lib/onionpress/ whose name survived sanitize_file_name(): the folder
also holds backup-password (the backup's encryption password while a backup
is pending), status.json, config-current.json, the action results and the
restore upload. The handler is the owner's (test_settings_owner_only.py);
now it also takes only the name the create-backup action gives a backup,
OnionPress-<first 8 address characters>-<YYYY-MM-DD-HH-MM>.zip, as a plain
file name (basename, strict pattern), and only a regular file directly in
the folder (no symlink, realpath inside it). Nothing that fails is deleted.

There is no PHP in the test environment, so these read the source.
"""

import os
import re
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SETTINGS = os.path.join(ROOT, "app", "Resources", "plugins", "onionpress-settings.php")
MENUBAR = os.path.join(ROOT, "src", "menubar.py")
LINUX = os.path.join(ROOT, "linux", "onionpress")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _handler():
    php = _read(SETTINGS)
    start = php.index("add_action( 'wp_ajax_onionpress_download_backup', function () {")
    return php[start:php.index("\n} );\n", start)]


def _pattern():
    """The handler's filename pattern as a Python regex ($ with D is \\Z)."""
    m = re.search(r"preg_match\( '/(\^OnionPress-.*?)\$/D', \$filename \)", _handler())
    assert m, "no D-anchored OnionPress-*.zip pattern in the download handler"
    return re.compile(m.group(1) + r"\Z")


class TestDownloadHandler(unittest.TestCase):

    def setUp(self):
        self.body = _handler()

    def test_checks_come_before_sending_and_deleting(self):
        steps = [
            "if ( ! onionpress_settings_owner_allowed() )",
            "check_ajax_referer( 'onionpress_download_backup' );",
            "is_string( $_GET['file'] )",
            "$filename  = basename( $requested );",
            "if ( $filename !== $requested",
            "preg_match( '/^OnionPress-",
            "wp_die( 'Not a backup file.', '', array( 'response' => 400 ) );",
            "$dir      = realpath( '/var/lib/onionpress' );",
            "is_link( $filepath )",
            "! is_file( $filepath )",
            "realpath( $filepath ) !== $filepath",
            "wp_die( 'Backup file not found. It may have been cleaned up.' );",
            "readfile( $filepath );",
            "@unlink( $filepath );",
        ]
        at = [self.body.index(s) for s in steps]
        self.assertEqual(at, sorted(at))

    def test_no_sanitize_file_name_fallback(self):
        # sanitize_file_name() let every plain name in the folder through.
        self.assertNotIn("sanitize_file_name", self.body)

    def test_deletes_only_the_backup_and_its_result(self):
        unlinks = re.findall(r"unlink\( ([^)]*) \)", self.body)
        self.assertEqual(unlinks, ["$filepath", "'/var/lib/onionpress/backup-result.json'"])
        self.assertEqual(self.body.count("$filepath = "), 1)
        self.assertIn("$filepath = $dir . '/' . $filename;", self.body)


class TestBackupNamePattern(unittest.TestCase):

    def setUp(self):
        self.pattern = _pattern()

    def test_backup_names_are_accepted(self):
        for name in ("OnionPress-op2homei-2026-10-12-09-30.zip",
                     "OnionPress-site-2026-10-12-09-30.zip",
                     "OnionPress-Starting-2026-10-12-09-30.zip",
                     "OnionPress--2026-10-12-09-30.zip"):
            with self.subTest(name=name):
                self.assertIsNotNone(self.pattern.match(name))

    def test_everything_else_in_the_folder_is_refused(self):
        php = _read(SETTINGS)
        shared = set(re.findall(r"/var/lib/onionpress/([A-Za-z0-9._-]+)", php))
        self.assertIn("backup-password", shared)
        for name in sorted(shared) + ["onion_address", "version", "status.json",
                                      "config-current.json", "config-updates.json",
                                      "restore-upload.zip", "restore-password"]:
            with self.subTest(name=name):
                self.assertIsNone(self.pattern.match(name))

    def test_near_misses_are_refused(self):
        good = "OnionPress-op2homei-2026-10-12-09-30.zip"
        for name in (good + "\n", good + ".php", good + "/", "../" + good,
                     "./" + good, "x" + good, good.lower(), good[:-4] + ".ZIP",
                     "OnionPress-op2homeiw-2026-10-12-09-30.zip",
                     "OnionPress-op2/home-2026-10-12-09-30.zip",
                     "OnionPress-op2.home-2026-10-12-09-30.zip",
                     "OnionPress-op2homei-2026-10-12-9-30.zip",
                     "OnionPress-op2homei-2026-10-12-09-30-00.zip", ""):
            with self.subTest(name=name):
                self.assertIsNone(self.pattern.match(name))


class TestPatternMatchesWhatTheHostsWrite(unittest.TestCase):
    """The create-backup action names the file; a format change on either
    host must change the handler's pattern with it."""

    def test_mac(self):
        src = _read(MENUBAR)
        self.assertIn(
            "filename = f\"OnionPress-{onion_short}-{time.strftime('%Y-%m-%d-%H-%M')}.zip\"", src)
        self.assertIn('onion_short = _derived.replace(".onion", "")[:8]', src)
        stamp = time.strftime("%Y-%m-%d-%H-%M")
        for short in ("op2homei", "site", "Starting"):
            with self.subTest(short=short):
                self.assertIsNotNone(_pattern().match(f"OnionPress-{short}-{stamp}.zip"))

    def test_linux(self):
        src = _read(LINUX)
        self.assertIn('local bp_filename="OnionPress-${bp_onion}-$(date +%Y-%m-%d-%H-%M).zip"', src)
        self.assertIn('bp_onion="${bp_onion:0:8}"', src)
        self.assertIn('bp_onion="site"', src)
        stamp = time.strftime("%Y-%m-%d-%H-%M")
        for short in ("op2homei", "site"):
            with self.subTest(short=short):
                self.assertIsNotNone(_pattern().match(f"OnionPress-{short}-{stamp}.zip"))


if __name__ == "__main__":
    unittest.main()
