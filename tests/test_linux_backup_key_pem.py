#!/usr/bin/env python3
"""A backup holds the onion service key's PEM, or says it has no key
(issue #519; both platforms' since issue #406).

A Linux first start that could not seed a key (the postinst's, before any
image was pulled) left Tor to mint one: C Tor's hs_ed25519_secret_key and
hostname in onionpress-tor-state, no PEM in onionpress-onion-keys. The
bash create_backup then warned "No PEM key found", packed the C Tor files
and wrote the archive; every restore of it failed on the missing PEM.

Now backup.create_backup reads the key out of the tor container with
key_manager.extract_keys (the delivered PEM, else C Tor's files, the
public key from `hostname` when tor wrote no hs_ed25519_public_key, as
newer tor does) and writes the PEM; with no key readable at all it marks
the archive keyless (metadata excludes_onion_key, the Mac page backup's
word: the restore keeps the install's key; onion_key_unreadable, so the
finished backup's notes say so) with a WARNING, never an archive no
restore accepts, and never a new address.
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
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import backup, key_manager  # noqa: E402

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

    def test_a_public_key_file_still_wins_when_present(self):
        with open(os.path.join(self.dir, "hs_ed25519_public_key"), "wb") as f:
            f.write(key_manager.CTOR_HEADER_PUBLIC + self.public)
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))


class TestBackupKeyFromTheContainer(unittest.TestCase):
    """backup.create_backup's key step (_stage_onion_key), with the tor
    container's answer patched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-backup-keys-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.staging)
        self.logs = []

    @unittest.skipIf(signing is None, "PyNaCl not installed")
    def test_the_key_the_container_holds_is_the_backups(self):
        private, public = _expanded_key()
        address = key_manager.key_pair_onion_address(private, public)
        with mock.patch.object(key_manager, "extract_keys", return_value=(private, public)):
            got = backup._stage_onion_key(self.staging, "", True, self.logs.append)
        self.assertEqual(got, (address, False))
        staged = os.path.join(self.staging, "tor-keys")
        self.assertEqual(os.listdir(staged), ["ks_hs_id.ed25519_expanded_private"])
        self.assertEqual(key_manager.read_arti_key(staged), (private, public))
        self.assertIn("Backup: extracting Tor keys...", self.logs)

    @unittest.skipIf(signing is None, "PyNaCl not installed")
    def test_left_out_on_purpose_derives_the_address_only(self):
        private, public = _expanded_key()
        with mock.patch.object(key_manager, "extract_keys", return_value=(private, public)):
            got = backup._stage_onion_key(self.staging, "", False, self.logs.append)
        self.assertEqual(got, (key_manager.key_pair_onion_address(private, public), False))
        self.assertEqual(os.listdir(self.staging), [])

    def test_no_key_readable_is_a_keyless_backup_with_a_warning(self):
        with mock.patch.object(key_manager, "extract_keys",
                               side_effect=Exception("Could not read key file: no such file")):
            got = backup._stage_onion_key(self.staging, "", True, self.logs.append)
        self.assertEqual(got, ("", True))
        self.assertEqual(os.listdir(self.staging), [], "nothing half-read goes into a keyless archive")
        [warning] = [m for m in self.logs if m.startswith("Backup: WARNING")]
        self.assertTrue(warning.startswith(backup.ONION_KEY_UNREADABLE_WARNING))
        self.assertIn("no such file", warning)

    def test_the_callers_address_is_kept_only_when_whole(self):
        whole = "a" * 56 + ".onion"
        with mock.patch.object(key_manager, "extract_keys", side_effect=Exception("down")):
            self.assertEqual(backup._stage_onion_key(self.staging, whole, True, self.logs.append),
                             (whole, True))
            self.assertEqual(backup._stage_onion_key(self.staging, "../x.onion", True, self.logs.append),
                             ("", True))

    def test_the_metadata_and_the_notes_say_so(self):
        # What create_backup records for the unreadable case, and what
        # `onionpress backup` prints with the finished backup.
        self.assertEqual(backup.unchecked_notes({"onion_key_unreadable": True}),
                         [backup.ONION_KEY_UNREADABLE_NOTE])
        self.assertEqual(backup.unchecked_notes({"excludes_onion_key": True}), [],
                         "left out on purpose (the Mac page's backup): no note here")
        src = _read("src/onionpress/backup.py")
        body = src[src.index("def create_backup("):src.index("def staged_onion_key_address(")]
        self.assertIn("if not include_onion_key or key_unreadable:\n"
                      "            # No tor-keys/", body)
        self.assertIn("if key_unreadable:\n", body)
        self.assertIn("metadata['onion_key_unreadable'] = True", body)
        # The restore keeps the install's key for such a backup, as for the
        # Mac page's (staged_onion_key_address on excludes_onion_key).
        self.assertIn("excludes_onion_key", src[src.index("def staged_onion_key_address("):][:1200])


if __name__ == "__main__":
    unittest.main()
