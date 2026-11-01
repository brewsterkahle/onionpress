#!/usr/bin/env python3
"""The healthcheck onion service's key is in no backup and no restore.

Through 2.5.x Linux backups carried it (tor-keys/healthcheck/), a restore
staged it in ~/.onionpress/shared/healthcheck-keys and the next start put
it back in the tor-state volume; Mac backups never did. Both platforms now
behave as the Mac: a start without one makes a new key and the heartbeat
gives the hub its address. One restore path, and one key fewer in every
backup (whoever runs the healthcheck service can make the hub release a
takeover, issue #455).

- backup.create_backup (both platforms' since issue #406) reads nothing of
  hidden_service/healthcheck: the site's key alone (key_manager.extract_keys).
- backup.restore_from_backup (the Settings page's restore on both
  platforms) takes no healthcheck key from any backup, says so when the
  backup holds one, and removes a key an older restore staged
  (leave_healthcheck_key_out).
- start_containers no longer seeds one; nothing reads the staging folder.

No docker: the restore runs with its container steps patched.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, key_manager  # noqa: E402
import test_backup_hub_data_off_wordpress as hub  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402

PW = mv.PW
HC_PRIV = b"\x09" * 64
HC_PUB = key_manager.derive_public_key(HC_PRIV)
HC_ADDR = key_manager.derive_onion_address(HC_PUB)
OTHER_PUB = key_manager.derive_public_key(b"\x0b" * 64)
OTHER_ADDR = key_manager.derive_onion_address(OTHER_PUB)
INJECTION = HC_ADDR + "\r\nSETCONF SocksPort=0.0.0.0:9050\r\n"


def _healthcheck(staging, *, public=HC_PUB, hostname=HC_ADDR + "\n", extra=False):
    hc = os.path.join(staging, "tor-keys", "healthcheck")
    os.makedirs(hc)
    with open(os.path.join(hc, "hs_ed25519_secret_key"), "wb") as f:
        f.write(key_manager.CTOR_SECRET_HEADER + HC_PRIV)
    if public is not None:
        with open(os.path.join(hc, "hs_ed25519_public_key"), "wb") as f:
            f.write(key_manager.CTOR_PUBLIC_HEADER + public)
    if hostname is not None:
        with open(os.path.join(hc, "hostname"), "w", newline="") as f:
            f.write(hostname)
    if extra:
        with open(os.path.join(hc, "torrc"), "w") as f:
            f.write("ControlPort 0.0.0.0:9051\n")
    return hc


class TestRestoreLeavesItOut(unittest.TestCase):
    """backup.restore_from_backup with such a backup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)

    def _backup(self, name, **kw):
        staging = hub._staging(self.tmp, name + ".staging")
        _healthcheck(staging, **kw)
        out = os.path.join(self.tmp, name)
        backup.pack_backup(staging, out, PW, lambda m: None)
        shutil.rmtree(staging)
        return out

    def _run(self, path, *, through_wordpress=False, leftover=False):
        home = os.path.join(self.tmp, "home")
        shutil.rmtree(home, True)
        data = os.path.join(home, ".onionpress")
        os.makedirs(data)
        if leftover:
            # What an older restore staged for the next start.
            os.makedirs(os.path.join(data, "shared"))
            os.rename(_healthcheck(os.path.join(self.tmp, "leftover")),
                      os.path.join(data, backup.HEALTHCHECK_KEY_STAGING))
        with open(os.path.join(data, "config"), "w") as f:
            f.write("ONIONNAME=me\n")
        logs = []
        # The tor container serves the backup's own address, and the
        # container-side steps are not the point here.
        with mock.patch("onionpress.backup.subprocess.run", mv._NoDocker(self)), \
                mock.patch.object(backup, "served_onion_address", return_value=mv.ADDR), \
                mock.patch.object(backup, "hub_data_state", return_value="none"), \
                mock.patch.object(backup, "onionhome_registry_present", return_value=False), \
                mock.patch.object(backup, "_restore_backup_key"), \
                mock.patch.object(backup, "restore_container_artifacts"), \
                mock.patch.object(backup, "apply_config_overrides"):
            backup.restore_from_backup(path, PW, logs.append, data_dir=data,
                                       through_wordpress=through_wordpress)
        hc = os.path.join(data, backup.HEALTHCHECK_KEY_STAGING)
        restored = {}
        if os.path.isdir(hc):
            for name in os.listdir(hc):
                with open(os.path.join(hc, name), "rb") as f:
                    restored[name] = f.read()
        return restored, logs

    def test_no_backup_brings_its_healthcheck_key(self):
        for label, kw, through in (("owner", {}, False),
                                   ("settings page", {}, True),
                                   ("injection", {"hostname": INJECTION}, False)):
            with self.subTest(label):
                restored, logs = self._run(
                    self._backup(label.replace(" ", "-") + ".zip", **kw),
                    through_wordpress=through)
                self.assertEqual(restored, {})
                self.assertTrue([m for m in logs
                                 if "healthcheck key left out (a start makes a new one)" in m], logs)
                # The rest of the restore went ahead.
                self.assertIn("Restore: files restored successfully", logs)

    def test_a_key_an_older_restore_staged_is_removed(self):
        restored, _ = self._run(self._backup("old.zip"), leftover=True)
        self.assertEqual(restored, {})

    def test_the_helper_alone(self):
        data = os.path.join(self.tmp, "d")
        os.makedirs(os.path.join(data, "shared"))
        staged = os.path.join(data, backup.HEALTHCHECK_KEY_STAGING)
        logs = []
        # Nothing there: nothing to say.
        backup.leave_healthcheck_key_out(None, data, logs.append)
        self.assertEqual(logs, [])
        # A link at the name is removed as a link, never followed.
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        with open(os.path.join(target, "keep"), "w") as f:
            f.write("x")
        os.symlink(target, staged)
        backup.leave_healthcheck_key_out(None, data, logs.append)
        self.assertFalse(os.path.lexists(staged))
        self.assertTrue(os.path.exists(os.path.join(target, "keep")))


class TestNothingSeedsOrStagesOne(unittest.TestCase):

    def test_the_launchers_never_back_up_or_seed_a_healthcheck_key(self):
        for path in ("linux/onionpress", "app/MacOS/onionpress"):
            text = mv._read(path)
            with self.subTest(path):
                for name in ("create_backup", "start_containers"):
                    if f"\n{name}() {{" not in text:
                        continue
                    body = mv._bash_function(text, name)
                    self.assertNotIn("hidden_service/healthcheck", body, name)
                    self.assertNotIn("healthcheck-keys", body, name)
                    self.assertNotIn("tor-keys/healthcheck", body, name)

    def test_only_the_python_restore_names_the_old_staging_folder(self):
        for path in ("linux/onionpress", "app/MacOS/onionpress"):
            self.assertNotIn("healthcheck-keys", mv._read(path), path)
        self.assertEqual(backup.HEALTHCHECK_KEY_STAGING, os.path.join("shared", "healthcheck-keys"))
        src = mv._read("src/onionpress/backup.py")
        self.assertEqual(src.count("'healthcheck-keys'"), 1)
        # Called by the in-place restore, after the key step.
        body = src[src.index("def restore_from_backup("):src.index("def _restore_backup_key(")]
        self.assertLess(body.index("_restore_backup_key(tor_dir"),
                        body.index("leave_healthcheck_key_out(tor_dir, _data_dir, log_func)"))


if __name__ == "__main__":
    unittest.main()
