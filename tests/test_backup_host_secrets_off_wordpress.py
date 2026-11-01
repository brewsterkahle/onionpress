#!/usr/bin/env python3
"""The host's secrets never pass through WordPress in a backup.

The Settings page's backup is handed to the WordPress container for
download, and its password came from there too: code running in WordPress
can ask for one with a password of its choosing. The page's Cloudflare
Tunnel field is write-only so the token never reaches WordPress, but the
Mac's backup carried it in config-overrides.json (it differs from its
default once set) and Linux's carried the whole config file. Now:

- The Settings page's backup (Mac handler, Linux branch) carries only the
  config WordPress's status page already shows (config.redact_config): no
  Cloudflare tunnel token, and on Linux no image overrides, bind host or
  hub admission either. Linux leaves out the healthcheck onion service's
  key too (a start makes a new one). metadata.json says so
  (excludes_host_secrets) and names the secrets that were set
  (secret_config_left_out). Backups made from the app or the CLI keep it
  all.
- No restore takes the token from any backup (RESTORABLE_CONFIG): this
  install's token stays, and the restore says to enter it again when the
  backed-up site had one and this install has none.

No docker: the container side is the stub of
test_backup_hub_data_off_wordpress, a bash function, or nothing at all.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, cli, config  # noqa: E402
import test_backup_hub_data_off_wordpress as hub  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402
import test_backup_staging_links as bsl  # noqa: E402

PW = mv.PW
TOKEN = "eyJhIjoiY2xvdWRmbGFyZS10dW5uZWwtdG9rZW4tZm9yLXRoZS10ZXN0cyJ9" + "Q" * 40
TOKEN_KEY = "CLOUDFLARE_TUNNEL_TOKEN"
HEALTHCHECK_SECRET = b"HEALTHCHECK-SECRET-KEY-" + b"H" * 64
DOCKER_HOST = hub.DOCKER_HOST

# An install's config, as ~/.onionpress/config holds it on either platform.
CONFIG = {
    "ADDRESS_PREFIX": "abc",
    "PREVENT_SLEEP": "never",
    "VM_MEMORY": "4",
    TOKEN_KEY: TOKEN,
    "REGISTER_WITH_ONIONHEAVEN": "yes",
    "ONIONPRESS_BIND_HOST": "0.0.0.0",
    "ONIONPRESS_TOR_IMAGE": "ghcr.io/me/onionpress-tor:mine",
    # Set, but not "yes": a node that accepts registrations is a hub, whose
    # Settings-page backup is refused outright
    # (backup.settings_page_backup_refusal).
    "ONIONHEAVEN_ACCEPT_REGISTRATIONS": "no",
}


def _write_config(data_dir, values=CONFIG):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "config")
    with open(path, "w") as f:
        f.write("# OnionPress config\n")
        f.writelines(f"{k}={v}\n" for k, v in values.items())
    return path


def _assert_no_token(test, members, logs=""):
    for name, data in members.items():
        test.assertNotIn(TOKEN.encode(), data, name)
    test.assertNotIn(TOKEN, logs)


# ---------------------------------------------------------------------------
# Creating a backup
# ---------------------------------------------------------------------------

class TestCreateBackup(unittest.TestCase):
    """backup.create_backup, the Mac's (and `onionpress.cli backup`'s)."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-secrets-off-wp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        _write_config(self.data_dir)
        self.logs = []

    def _backup(self, include_host_secrets):
        hub._Container(self, self.tmp, hub=False)
        out = os.path.join(self.tmp, "backup.zip")
        meta = backup.create_backup(mv.ADDR, "admin", PW, out, "2.5.3",
                                    self.logs.append, data_dir=self.data_dir,
                                    include_hub_data=include_host_secrets,
                                    include_host_secrets=include_host_secrets)
        return meta, bsl._inner_names_and_bytes(out, PW)

    def test_a_backup_for_wordpress_carries_no_token(self):
        meta, members = self._backup(include_host_secrets=False)
        _assert_no_token(self, members, "\n".join(self.logs))
        overrides = json.loads(members["config-overrides.json"])
        self.assertNotIn(TOKEN_KEY, overrides)
        # What a restore takes is still there.
        self.assertEqual(overrides["PREVENT_SLEEP"], "never")
        self.assertEqual(overrides["VM_MEMORY"], "4")
        self.assertEqual(set(overrides) - config.SAFE_CONFIG_KEYS, set())
        stored = json.loads(members["metadata.json"])
        for m in (meta, stored):
            self.assertIs(m["excludes_host_secrets"], True)
            self.assertEqual(m["secret_config_left_out"], [TOKEN_KEY])
        self.assertTrue([m for m in self.logs if "left 1 setting(s) out" in m
                         and TOKEN_KEY in m], self.logs)
        # The site's own key stays: it is what the backup restores.
        self.assertIn("tor-keys/" + hub.KEY, members)

    def test_without_a_token_nothing_is_named(self):
        _write_config(self.data_dir, {"PREVENT_SLEEP": "never"})
        meta, members = self._backup(include_host_secrets=False)
        self.assertEqual(meta["secret_config_left_out"], [])
        self.assertIs(meta["excludes_host_secrets"], True)

    def test_a_backup_for_the_owners_disk_keeps_it(self):
        meta, members = self._backup(include_host_secrets=True)
        self.assertEqual(json.loads(members["config-overrides.json"])[TOKEN_KEY], TOKEN)
        self.assertNotIn("excludes_host_secrets", meta)
        self.assertNotIn("secret_config_left_out", meta)


class TestWordPressSafeConfig(unittest.TestCase):

    def test_only_what_the_status_page_shows(self):
        logs = []
        kept, secrets = backup.wordpress_safe_config(dict(CONFIG), logs.append)
        self.assertEqual(kept, config.redact_config(CONFIG))
        self.assertEqual(secrets, [TOKEN_KEY])
        for key in (TOKEN_KEY, "ONIONPRESS_BIND_HOST", "ONIONPRESS_TOR_IMAGE",
                    "ONIONHEAVEN_ACCEPT_REGISTRATIONS"):
            self.assertNotIn(key, kept)
            self.assertIn(key, logs[0])
        self.assertNotIn(TOKEN, logs[0])

    def test_an_empty_token_is_no_secret_left_out(self):
        _, secrets = backup.wordpress_safe_config({TOKEN_KEY: ""}, lambda m: None)
        self.assertEqual(secrets, [])

    def test_every_preference_a_restore_takes_survives_it(self):
        """A restore takes RESTORABLE_CONFIG only; a backup made from the
        Settings page must still carry all of it."""
        self.assertLessEqual(set(backup.RESTORABLE_CONFIG), config.SAFE_CONFIG_KEYS)
        self.assertTrue(config.SECRET_CONFIG_KEYS.isdisjoint(config.SAFE_CONFIG_KEYS))
        self.assertTrue(config.SECRET_CONFIG_KEYS.isdisjoint(backup.RESTORABLE_CONFIG))

# ---------------------------------------------------------------------------
# Who leaves them out: every path that hands a backup to WordPress
# ---------------------------------------------------------------------------

class TestEveryWordPressDeliveredBackupLeavesThemOut(unittest.TestCase):
    """The census of test_backup_hub_data_off_wordpress, for the host's
    secrets: the backup handed to WordPress leaves them out, every other
    backup says it keeps them."""

    def test_every_python_create_backup_call_says_which_it_is(self):
        calls = []
        census = hub.TestEveryWordPressDeliveredBackupLeavesItOut()
        for rel, text in census._python_sources():
            for m in re.finditer(r"\bcreate_backup\(", text):
                if text[max(0, m.start() - 4):m.start()] == "def ":
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                if "#" in text[line_start:m.start()]:
                    continue
                depth, i = 0, m.end() - 1
                while True:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    if depth == 0:
                        break
                    i += 1
                calls.append((rel, text[m.start():i + 1]))
        # cmd_backup (the owner's) and cmd_settings_page_backup (the page's),
        # both in the CLI (issue #366).
        self.assertEqual(len(calls), 2, calls)
        for rel, call in calls:
            with self.subTest(rel, call=call):
                self.assertRegex(call, r"include_host_secrets=(True|False)\b")
                # Both go together: what goes to WordPress leaves out both.
                self.assertEqual("include_host_secrets=False" in call,
                                 "include_hub_data=False" in call)
        falses = [rel for rel, c in calls if "include_host_secrets=False" in c]
        self.assertEqual(falses, ["src/onionpress/cli.py"])

    def test_the_linux_settings_page_backup_leaves_them_out(self):
        # The case is the CLI's settings-page-backup (issue #406), the one
        # path that passes include_host_secrets=False (the census above);
        # no bash backup is left to say otherwise.
        text = mv._read("linux/onionpress")
        self.assertNotRegex(text, r'\bcreate_backup "')
        branch = hub._linux_case(text, "settings-page-backup")
        self.assertIn('op_py --data-dir "$DATA_DIR" settings-page-backup "${@:2}"', branch)

    def test_the_mac_settings_page_backup_leaves_them_out(self):
        cli_src = mv._read("src/onionpress/cli.py")
        mac = cli_src[cli_src.index("def cmd_settings_page_backup("):cli_src.index("def cmd_settings_page_restore(")]
        create = mac.index("create_backup(")
        call = mac[create:mac.index("include_onion_key=False)", create)]
        self.assertIn("include_host_secrets=False", call)

    def test_the_settings_page_says_so(self):
        php = mv._read("app/Resources/plugins/onionpress-settings.php")
        section = hub._branch(php, "<h2>Backup</h2>", "<!-- Restore -->")
        self.assertIn("leaves out your Cloudflare Tunnel token", section)


# ---------------------------------------------------------------------------
# Restoring: the token is never taken, and the owner hears what to do
# ---------------------------------------------------------------------------

class TestRestoreSaysWhatBecameOfTheTunnel(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "config")
        self.logs = []

    def _restore(self, mine, *, backup_config=None, metadata=None):
        with open(self.config, "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in mine.items())
        staging = mv._make_staging(self.tmp, metadata or mv._metadata(),
                                   name=f"s{len(os.listdir(self.tmp))}",
                                   config=mv._config_text(backup_config)
                                   if backup_config is not None else None)
        self.logs.clear()
        backup.restore_backup_config(staging, self.config, self.logs.append, mv.ADDR)
        return config.read_config(self.config)

    def _note(self):
        notes = [m for m in self.logs if "Cloudflare tunnel" in m]
        self.assertLessEqual(len(notes), 1, self.logs)
        for m in self.logs:
            self.assertNotIn(TOKEN, m)
        return notes[0] if notes else None

    def test_a_settings_page_backup_on_a_new_machine(self):
        after = self._restore({}, backup_config={"VM_MEMORY": "4"},
                              metadata=mv._metadata(excludes_host_secrets=True,
                                                    secret_config_left_out=[TOKEN_KEY]))
        self.assertNotIn(TOKEN_KEY, after)
        self.assertIn("Enter it again in the Settings page", self._note())

    def test_an_owners_backup_never_brings_the_token_back_either(self):
        after = self._restore({}, backup_config=CONFIG)
        self.assertNotIn(TOKEN_KEY, after)
        self.assertIn("Enter it again", self._note())

    def test_this_installs_token_is_kept(self):
        mine = {TOKEN_KEY: "mine" + "M" * 40}
        for kw in ({"backup_config": dict(CONFIG, **{TOKEN_KEY: "theirs" + "T" * 40})},
                   {"metadata": mv._metadata(secret_config_left_out=[TOKEN_KEY])}):
            with self.subTest(kw=list(kw)):
                after = self._restore(mine, **kw)
                self.assertEqual(after[TOKEN_KEY], mine[TOKEN_KEY])
                self.assertIn("kept this install's Cloudflare tunnel token", self._note())

    def test_no_tunnel_no_note(self):
        self._restore({}, backup_config={"VM_MEMORY": "4"})
        self.assertIsNone(self._note())
        self._restore({}, metadata=mv._metadata(secret_config_left_out="CLOUDFLARE_TUNNEL_TOKEN"))
        self.assertIsNone(self._note())


if __name__ == "__main__":
    unittest.main()
