#!/usr/bin/env python3
"""Passwords posted to the OnionPress plugins are unslashed before use.

WordPress adds slashes to $_POST (wp_magic_quotes). The settings page read
op_backup_password and op_restore_password raw, so a password containing
' " or \\ reached wp_check_password() and the backup-password file as
it\\'s instead of it's: an admin whose password was set by OnionPress setup
(wp-cli, hashed as typed) could never back up from the web page, and a
restore never matched a backup made with the password as typed.

Now the backup check tries the typed password, then the slashed form
WordPress's own forms store (wp_signon() and edit_user() never unslash),
and hands the host the form that matched; the restore password is passed as
typed. The onboarding wizard's new password stays slashed on purpose, like
WordPress's profile form, so it keeps matching at wp-login.php.

No php locally, so these are source checks.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins")
SETTINGS = os.path.join(PLUGINS, "onionpress-settings.php")

SECRET_FIELD = re.compile(r"\$_POST\[\s*'([^']*(?:pass|secret|token|pwd|key)[^']*)'\s*\]",
                          re.I)

# (file, field): why it is read without wp_unslash.
ALLOWED_RAW = {
    ("onionpress-settings.php", "op_backup_password"):
        "the slashed form is the second candidate wp_check_password() tries",
    ("onionpress-onboarding.php", "new_password"):
        "kept slashed like WordPress's own password forms",
    ("onionpress-onboarding.php", "confirm_password"):
        "compared with new_password only",
}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _statements(src):
    """PHP statements, roughly: text between semicolons/braces, with
    comment lines dropped."""
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith(("//", "*", "/*", "#")))
    return re.split(r"[;{}]", code)


class TestSecretsAreUnslashed(unittest.TestCase):

    def test_every_secret_post_field_is_unslashed_or_allowed(self):
        offenders = []
        for root, _dirs, files in os.walk(PLUGINS):
            for fn in files:
                if not fn.endswith(".php"):
                    continue
                for stmt in _statements(_read(os.path.join(root, fn))):
                    for field in SECRET_FIELD.findall(stmt):
                        if "nonce" in field:
                            continue
                        if "wp_unslash" in stmt or (fn, field) in ALLOWED_RAW:
                            continue
                        # isset()/empty() guards read nothing.
                        reads = re.sub(r"(isset|empty|is_string)\s*\(\s*\$_POST\[[^\]]+\]\s*\)",
                                       "", stmt)
                        if SECRET_FIELD.search(reads):
                            offenders.append(f"{fn}: {field}")
        self.assertEqual(offenders, [])

    def test_backup_password_checks_typed_then_slashed(self):
        src = _read(SETTINGS)
        body = src[src.index("if ( $action === 'create-backup' )"):
                   src.index("if ( $action === 'restore-backup' )")]
        self.assertIn("$bp_typed = wp_unslash( $bp_raw );", body)
        typed = body.index("wp_check_password( $bp_typed,")
        raw = body.index("wp_check_password( $bp_raw,")
        self.assertLess(typed, raw)
        # The file gets the form that matched, never the raw POST value
        # unchecked.
        self.assertIn("file_put_contents( '/var/lib/onionpress/backup-password', $bp_pass )",
                      body)
        self.assertEqual(body.count("$bp_pass = "), 2)

    def test_restore_password_is_unslashed(self):
        self.assertIn("wp_unslash( $_POST['op_restore_password'] )", _read(SETTINGS))

    def test_passwords_are_never_logged(self):
        for fn in ("onionpress-settings.php", "onionpress-onboarding.php"):
            for line in _read(os.path.join(PLUGINS, fn)).splitlines():
                if "error_log" in line:
                    self.assertNotRegex(line, r"\$(bp_|rp_|new\b|confirm\b|ia_password)",
                                        f"{fn}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
