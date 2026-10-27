#!/usr/bin/env python3
"""The healthcheck onion service's key is in no backup and no restore.

Through 2.5.x Linux backups carried it (tor-keys/healthcheck/), a restore
staged it in ~/.onionpress/shared/healthcheck-keys and the next start put
it back in the tor-state volume; Mac backups never did. Both platforms now
behave as the Mac: a start without one makes a new key and the heartbeat
gives the hub its address. One restore path, and one key fewer in every
backup (whoever runs the healthcheck service can make the hub release a
takeover, issue #455).

- linux/onionpress create_backup reads nothing of hidden_service/healthcheck
  (tests/test_backup_host_secrets_off_wordpress.py runs it).
- restore_backup takes no healthcheck key from any backup, says so when the
  backup holds one, and removes a key an older restore staged.
- start_containers no longer seeds one; nothing reads the staging folder.

No docker: the launcher's restore_backup runs for real with docker stubbed
(the harness of test_backup_hub_data_off_wordpress).
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


class TestLinuxRestore(unittest.TestCase):
    """linux/onionpress restore_backup with such a backup."""

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

    def _run(self, path, *args, leftover=False):
        home = os.path.join(self.tmp, "home")
        shutil.rmtree(home, True)
        data = os.path.join(home, ".onionpress")
        os.makedirs(data)
        if leftover:
            # What an older restore staged for the next start.
            os.makedirs(os.path.join(data, "shared"))
            os.rename(_healthcheck(os.path.join(self.tmp, "leftover")),
                      os.path.join(data, "shared", "healthcheck-keys"))
        with open(os.path.join(data, "config"), "w") as f:
            f.write("ONIONNAME=me\n")
        tmpdir = os.path.join(self.tmp, "t")
        os.makedirs(tmpdir, exist_ok=True)
        env = {
            "PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmpdir,
            "DATA_DIR": data, "LOG_FILE": os.path.join(self.tmp, "log"),
            "CALLS": os.path.join(self.tmp, "calls"), "SRC": mv.SRC,
            "PY": sys.executable, "PW": PW, "DOCKER_HOST": hub.DOCKER_HOST,
            # The tor container serves the backup's own address: the
            # Settings page's restore never changes it.
            "CURRENT": mv.ADDR,
        }
        text = mv._read("linux/onionpress")
        funcs = "".join(mv._bash_function(text, name)
                        for name in ("is_onion_address", "restore_backup"))
        call = " ".join(["restore_backup", '"$PW"', repr(path)] + list(args))
        r = subprocess.run(["bash", "-c", hub.TestLinuxRestoreBackup.PRELUDE + funcs + call
                            + '\necho "rc=$?"'],
                           capture_output=True, text=True, timeout=120, env=env)
        out, rc = r.stdout.rsplit("rc=", 1)
        self.assertEqual(int(rc.strip()), 0, out + r.stderr)
        hc = os.path.join(data, "shared", "healthcheck-keys")
        restored = {}
        if os.path.isdir(hc):
            for name in os.listdir(hc):
                with open(os.path.join(hc, name), "rb") as f:
                    restored[name] = f.read()
        return restored, out

    def test_no_backup_brings_its_healthcheck_key(self):
        for label, kw, args in (("owner", {}, ()),
                                ("settings page", {}, ("--from-settings-page",)),
                                ("injection", {"hostname": INJECTION}, ())):
            with self.subTest(label):
                restored, out = self._run(
                    self._backup(label.replace(" ", "-") + ".zip", **kw), *args)
                self.assertEqual(restored, {})
                self.assertIn("healthcheck key left out (a start makes a new one)", out)
                # The rest of the restore went ahead.
                self.assertEqual(out.rstrip("\n").rsplit("\n", 1)[-1], mv.ADDR)

    def test_a_key_an_older_restore_staged_is_removed(self):
        restored, _ = self._run(self._backup("old.zip"), leftover=True)
        self.assertEqual(restored, {})


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

    def test_only_the_restore_names_the_old_staging_folder(self):
        text = mv._read("linux/onionpress")
        lines = [l.strip() for l in text.splitlines() if "healthcheck-keys" in l]
        self.assertEqual(lines, ['rm -rf "$DATA_DIR/shared/healthcheck-keys"'])


if __name__ == "__main__":
    unittest.main()
