#!/usr/bin/env python3
"""The restore password prompt names a user only if the file name holds one.

Restore... asks "Enter the password of '<user>' ..." with <user> parsed from
the chosen zip's file name. The name is whatever the file is called, not
something the backup vouches for, and it went into the dialog unchecked:
"OnionPress-x-<any text>-<ts>.zip" put any text, newlines and bidi
overrides included, into a dialog asking for a password. It also misread
the Settings page's own backups (OnionPress-<addr>-<ts>.zip, no username)
as belonging to a user called "2026", and cut "jean-luc" to "jean".

backup.backup_filename_username() is now the one parser: backup_filename()'s
shape only, and a name that passes the metadata username rule or nothing.
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import backup  # noqa: E402

ADDR = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"
TS = "2026-10-12-10-30"


class TestBackupFilenameUsername(unittest.TestCase):
    def test_reads_back_what_backup_filename_wrote(self):
        for user in ("admin", "jean-luc", "John Smith", "a.b_c@d", "x" * 60):
            with self.subTest(user=user):
                name = backup.backup_filename(ADDR, user)
                self.assertEqual(backup.backup_filename_username(name), user)
                self.assertEqual(backup.backup_filename_username(
                    os.path.join("/Users/me/OnionPress/backups", name)), user)

    def test_names_without_a_username(self):
        for name in (f"OnionPress-abcdefgh-{TS}.zip",          # Settings page
                     f"OnionPress-backup-{TS}.zip",            # Linux settings
                     "OnionPress-abcdefgh-admin.zip",
                     f"OnionPress-abcdefgh-admin-{TS} (1).zip",
                     f"Backup-abcdefgh-admin-{TS}.zip",
                     "", None, 42):
            with self.subTest(name=name):
                self.assertIsNone(backup.backup_filename_username(name))

    def test_text_that_is_not_a_login_is_not_shown(self):
        for user in ("admin\nAlso enter your Mac login password",
                     "admin\n", "‮nimda", "ad​min", "ad\x07min",
                     "x" * 61, "<b>admin</b>", "admin's", "   "):
            with self.subTest(user=user):
                name = f"OnionPress-abcdefgh-{user}-{TS}.zip"
                self.assertIsNone(backup.backup_filename_username(name))


class TestMenubarRestorePrompt(unittest.TestCase):
    """src/menubar.py's Restore... (AppKit, so checked as source)."""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "src", "menubar.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("    def restore(self, _):")
        self.body = src[start:src.index("\n    @rumps.clicked", start + 1)]

    def test_uses_the_checked_parser(self):
        # The CLI answers `restore --check --json` without a password with
        # {"ask": "password", "backup_user": ...} from the checked parser.
        self.assertIn('backup_user = (ans or {}).get("backup_user") if (ans or {}).get("ask") == "password" else None',
                      self.body)
        self.assertNotIn(".split(\"-\")", self.body)
        with open(os.path.join(PROJECT_ROOT, "src", "onionpress", "cli.py"), encoding="utf-8") as f:
            cli_src = f.read()
        self.assertIn("backup_user=backup_filename_username(args.backup_file)", cli_src)

    def test_name_only_words_the_prompt(self):
        uses = [line.strip() for line in self.body.splitlines() if "backup_user" in line]
        self.assertEqual(len(uses), 3, uses)
        self.assertTrue(uses[1].startswith("if backup_user"))
        self.assertTrue(uses[2].startswith("f\"Enter the password of '{backup_user}'"))


if __name__ == "__main__":
    unittest.main()
