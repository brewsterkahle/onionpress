#!/usr/bin/env python3
"""A Linux backup holds the onion service key's PEM, or it is not made
(issue #519).

A Linux first start that could not seed a key (the postinst's, before any
image was pulled) left Tor to mint one: C Tor's hs_ed25519_secret_key and
hostname in onionpress-tor-state, no PEM in onionpress-onion-keys. The
bash create_backup then warned "No PEM key found", packed the C Tor files
and wrote the archive; every restore of it failed on the missing PEM.

Now the backup derives the PEM from C Tor's files (key_manager.py
ctor-to-arti; read_ctor_keys takes the public key from `hostname` when
tor wrote no hs_ed25519_public_key, as newer tor does); with no key at
all it marks the archive keyless (metadata excludes_onion_key, the Mac
page backup's word: the restore keeps the install's key) with a WARNING.
"""
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import key_manager  # noqa: E402

try:
    from nacl import signing
except ImportError:  # pragma: no cover
    signing = None

KM = os.path.join(ROOT, "src", "onionpress", "key_manager.py")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _expanded_key():
    """A real ed25519 expanded secret key (what C Tor and the PEM hold)
    and its public key."""
    seed = signing.SigningKey.generate().encode()
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 63
    h[31] |= 64
    expanded = bytes(h)
    return expanded, key_manager.derive_public_key(expanded)


@unittest.skipIf(signing is None, "PyNaCl not installed")
class TestCtorToPem(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-key-pem-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.private, self.public = _expanded_key()
        self.address = key_manager.key_pair_onion_address(self.private, self.public)
        with open(os.path.join(self.dir, "hs_ed25519_secret_key"), "wb") as f:
            f.write(key_manager.CTOR_HEADER_SECRET + self.private)
        with open(os.path.join(self.dir, "hostname"), "w") as f:
            f.write(self.address + "\n")

    def test_the_public_key_comes_from_the_hostname_when_tor_wrote_none(self):
        self.assertFalse(os.path.exists(os.path.join(self.dir, "hs_ed25519_public_key")))
        private, public = key_manager.read_ctor_keys(self.dir)
        self.assertEqual((private, public), (self.private, self.public))

    def test_ctor_to_arti_writes_the_pem_the_restore_needs(self):
        # Run by path, as the launcher runs it: its own folder (with
        # onion_auth.py and onion_keys.py beside it) on sys.path.
        r = subprocess.run([sys.executable, KM, "ctor-to-arti", self.dir],
                           capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ["PATH"]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pem = os.path.join(self.dir, "ks_hs_id.ed25519_expanded_private")
        self.assertTrue(os.path.isfile(pem))
        self.assertEqual(key_manager.read_arti_key(self.dir), (self.private, self.public))
        self.assertEqual(stat.S_IMODE(os.stat(pem).st_mode), 0o600)

    def test_a_public_key_file_still_wins_when_present(self):
        with open(os.path.join(self.dir, "hs_ed25519_public_key"), "wb") as f:
            f.write(key_manager.CTOR_HEADER_PUBLIC + self.public)
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))


class TestLinuxBackupKeys(unittest.TestCase):
    """The key section of linux/onionpress's create_backup, run under bash
    with key_volume_cat stubbed: from a C-Tor-only volume the PEM is
    derived; from no key at all there is no backup."""

    def _section(self):
        text = _read("linux/onionpress")
        i = text.index("\ncreate_backup() {")
        body = text[i:text.index("\n}\n", i)]
        start = body.index("    # 1. Extract Tor keys")
        end = body.index("    # 2. Dump WordPress database")
        return body[start:end]

    def test_the_section_derives_the_pem_and_refuses_without_a_key(self):
        section = self._section()
        self.assertIn('key_manager.py" ctor-to-arti "$staging/tor-keys"', section)
        self.assertIn("hidden_service/wordpress/hostname", section)
        self.assertIn("keyless_backup=yes", section)
        self.assertIn("WARNING: Backup: no onion service key could be read", section)
        self.assertNotIn("backup will not include keys", section)
        body = _read("linux/onionpress")
        self.assertIn('[ "$keyless_backup" = "yes" ] && meta_flags+=(--excludes-onion-key)', body)

    @unittest.skipIf(signing is None, "PyNaCl not installed")
    def test_under_bash(self):
        tmp = tempfile.mkdtemp(prefix="op-backup-keys-")
        self.addCleanup(shutil.rmtree, tmp, True)
        private, public = _expanded_key()
        address = key_manager.key_pair_onion_address(private, public)
        vol = os.path.join(tmp, "vol")
        os.makedirs(vol)
        with open(os.path.join(vol, "hs_ed25519_secret_key"), "wb") as f:
            f.write(key_manager.CTOR_HEADER_SECRET + private)
        with open(os.path.join(vol, "hostname"), "w") as f:
            f.write(address + "\n")
        scripts = os.path.join(tmp, "scripts")
        os.makedirs(scripts)
        # The install tree's scripts/ (linux/install-tree.sh): key_manager.py
        # with onion_auth.py and onion_keys.py beside it.
        for name in ("key_manager.py", "onion_auth.py", "onion_keys.py"):
            shutil.copy(os.path.join(ROOT, "src", "onionpress", name),
                        os.path.join(scripts, name))
        prelude = (
            'set -e\n'
            f'DATA_DIR="{tmp}/data"; LOG_FILE="{tmp}/log"; SCRIPTS_DIR="{scripts}"\n'
            'mkdir -p "$DATA_DIR/shared/vanity-keys"; : > "$LOG_FILE"\n'
            'log() { echo "$*"; echo "$*" >> "$LOG_FILE"; }\n'
            # the stub volume reader: the files of $VOL, by name
            'key_volume_cat() { [ -n "${VOL:-}" ] && cat "$VOL/$(basename "$2")" 2>/dev/null; }\n'
            f'staging="{tmp}/staging"; rm -rf "$staging"; mkdir -p "$staging"\n'
            'run_section() {\n' + self._section() + '\n echo "keyless=$keyless_backup"\n}\n'
        )
        with_key = subprocess.run(
            ["bash", "-c", prelude + "run_section; echo rc=$?\n"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": os.environ["PATH"], "VOL": vol, "PYTHONPATH": ""})
        self.assertIn("rc=0", with_key.stdout, with_key.stdout + with_key.stderr)
        self.assertIn("keyless=no", with_key.stdout)
        self.assertIn("onion service key PEM derived from C Tor's key files", with_key.stdout)
        staged = os.path.join(tmp, "staging", "tor-keys")
        self.assertTrue(os.path.isfile(os.path.join(staged, "ks_hs_id.ed25519_expanded_private")))
        self.assertFalse(os.path.exists(os.path.join(staged, "hostname")))
        self.assertEqual(key_manager.read_arti_key(staged), (private, public))

        without = subprocess.run(
            ["bash", "-c", prelude + "run_section; echo rc=$?\n"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": os.environ["PATH"], "VOL": os.path.join(tmp, "nowhere"), "PYTHONPATH": ""})
        self.assertIn("rc=0", without.stdout, without.stdout + without.stderr)
        self.assertIn("keyless=yes", without.stdout)
        self.assertIn("WARNING: Backup: no onion service key could be read", without.stdout)
        self.assertIn("this backup carries none", without.stderr)
        self.assertEqual(os.listdir(staged), [], "nothing half-read goes into a keyless archive")


if __name__ == "__main__":
    unittest.main()
