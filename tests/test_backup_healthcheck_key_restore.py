#!/usr/bin/env python3
"""A restored healthcheck key is checked against its address, and never
taken from the Settings page.

Linux backups carry the healthcheck onion service's key files and hostname
(tor-keys/healthcheck/). linux/onionpress restore_backup copied whatever
that folder held into ~/.onionpress/shared/healthcheck-keys, and the next
start installed key and hostname into the tor container, where
tor-watchdog.py takes the service id from the hostname for DEL_ONION on
the control port: a hostname with a CR/LF in it added a control-port
command of the backup author's choosing. That restore is the Settings
page's, whose upload WordPress can make. Now:

- From the Settings page the healthcheck keys are left out (a start makes
  new ones; the heartbeat gives the hub the new address).
- Otherwise only a key whose files parse and agree and whose hostname
  names exactly its address (backup.staged_healthcheck_address, `onionpress.cli
  healthcheck-key-address`), and only the key files; the hostname is
  written from the checked address.

No docker: the launcher's restore_backup runs for real with docker
stubbed (the harness of test_backup_hub_data_off_wordpress).
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


class TestStagedHealthcheckAddress(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _check(self, name, **kw):
        return backup.staged_healthcheck_address(_healthcheck(os.path.join(self.tmp, name), **kw))

    def test_a_key_and_its_hostname(self):
        self.assertEqual(self._check("a"), HC_ADDR)
        self.assertEqual(self._check("b", hostname=HC_ADDR), HC_ADDR)
        self.assertEqual(self._check("c", hostname=None, public=None), HC_ADDR)

    def test_refused(self):
        for label, kw in (("injection", {"hostname": INJECTION}),
                          ("another address", {"hostname": OTHER_ADDR + "\n"}),
                          ("two lines", {"hostname": HC_ADDR + "\n" + HC_ADDR + "\n"}),
                          ("public key of another", {"public": OTHER_PUB})):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self._check(label.replace(" ", "-"), **kw)

    def test_a_linked_hostname_is_refused(self):
        hc = _healthcheck(os.path.join(self.tmp, "l"), hostname=None)
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write(HC_ADDR + "\n")
        os.symlink(target, os.path.join(hc, "hostname"))
        with self.assertRaises(ValueError):
            backup.staged_healthcheck_address(hc)

    def test_no_key_is_refused(self):
        hc = os.path.join(self.tmp, "none")
        os.makedirs(hc)
        with self.assertRaises(ValueError):
            backup.staged_healthcheck_address(hc)


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

    def _run(self, path, *args):
        home = os.path.join(self.tmp, "home")
        shutil.rmtree(home, True)
        data = os.path.join(home, ".onionpress")
        os.makedirs(data)
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

    def test_a_key_that_checks_out_is_restored_and_only_its_files(self):
        restored, _ = self._run(self._backup("ok.zip", extra=True))
        self.assertEqual(sorted(restored), ["hostname", "hs_ed25519_public_key",
                                            "hs_ed25519_secret_key"])
        self.assertEqual(restored["hostname"], (HC_ADDR + "\n").encode())
        self.assertEqual(restored["hs_ed25519_secret_key"],
                         key_manager.CTOR_SECRET_HEADER + HC_PRIV)

    def test_the_hostname_is_written_from_the_key(self):
        restored, _ = self._run(self._backup("nohost.zip", hostname=None))
        self.assertEqual(restored["hostname"], (HC_ADDR + "\n").encode())

    def test_what_does_not_check_out_is_left_out(self):
        for label, kw in (("injection", {"hostname": INJECTION}),
                          ("another address", {"hostname": OTHER_ADDR + "\n"}),
                          ("public key of another", {"public": OTHER_PUB})):
            with self.subTest(label):
                restored, out = self._run(self._backup(label.replace(" ", "-") + ".zip", **kw))
                self.assertEqual(restored, {})
                self.assertIn("healthcheck keys do not check out", out)
                # The rest of the restore went ahead.
                self.assertEqual(out.rstrip("\n").rsplit("\n", 1)[-1], mv.ADDR)

    def test_never_from_the_settings_page(self):
        restored, out = self._run(self._backup("sp.zip"), "--from-settings-page")
        self.assertEqual(restored, {})
        self.assertIn("healthcheck keys left out (the Settings page's restore", out)


if __name__ == "__main__":
    unittest.main()
