#!/usr/bin/env python3
"""A key import refuses a key Tor would not serve as the address it names.

key_manager.check_expanded_secret_key is the gate every import goes through
(decode-key, import-key, a restored backup's key files): 64 bytes, bit 255
of the secret scalar clear, the scalar nonzero mod the group order, and,
with PyNaCl present, libsodium's scalar·B
(crypto_scalarmult_ed25519_base_noclamp) the same public key as
derive_public_key finds. decode_base32_key and import_key_b32 used to take
a scalar with bit 255 set (libsodium and OnionPress then disagree on the
address) or a zero scalar (no public key at all), and the Linux Settings
page removed the current key before the new one was even decoded.

Nothing here talks to Docker.
"""
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import key_manager  # noqa: E402

KEY_MANAGER = os.path.join(PROJECT_DIR, "src", "onionpress", "key_manager.py")

try:
    import nacl.bindings as _nacl_bindings
    HAVE_NACL = bool(getattr(_nacl_bindings, "has_crypto_scalarmult_ed25519", False))
except ImportError:
    HAVE_NACL = False


def expanded_key(seed):
    """An expanded ed25519 secret key the way Tor and mkp224o make one:
    SHA-512 of a seed, clamped."""
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


GOOD = expanded_key(b"onionpress key validation")
TOP_BIT = GOOD[:31] + bytes([GOOD[31] | 0x80]) + GOOD[32:]
ZERO = bytes(32) + GOOD[32:]
ORDER = key_manager.ED25519_ORDER.to_bytes(32, "little") + GOOD[32:]


def b32(key):
    return base64.b32encode(key).decode("ascii")


class TestCheckExpandedSecretKey(unittest.TestCase):

    def test_a_key_tor_makes_passes(self):
        pub = key_manager.check_expanded_secret_key(GOOD)
        self.assertEqual(pub, key_manager.derive_public_key(GOOD))
        self.assertEqual(key_manager.check_expanded_secret_key(GOOD, pub), pub)

    @unittest.skipUnless(HAVE_NACL, "PyNaCl is not installed")
    def test_libsodium_agrees(self):
        self.assertEqual(key_manager.check_expanded_secret_key(GOOD),
                         _nacl_bindings.crypto_scalarmult_ed25519_base_noclamp(GOOD[:32]))

    def test_refused(self):
        for name, key, pub, words in (
                ("63 bytes", GOOD[:63], None, "64 bytes"),
                ("not bytes", "x" * 64, None, "64 bytes"),
                ("bit 255 set", TOP_BIT, None, "top bit"),
                ("zero scalar", ZERO, None, "zero"),
                ("scalar = the group order", ORDER, None, "zero"),
                ("another key's public half", GOOD,
                 key_manager.derive_public_key(expanded_key(b"other")), "does not belong")):
            with self.subTest(name):
                with self.assertRaises(ValueError) as cm:
                    key_manager.check_expanded_secret_key(key, pub)
                self.assertIn(words, str(cm.exception))

    def test_without_pynacl_the_checks_still_hold(self):
        with mock.patch.object(key_manager, "_nacl_public_key", return_value=None):
            self.assertEqual(key_manager.check_expanded_secret_key(GOOD),
                             key_manager.derive_public_key(GOOD))
            for key in (TOP_BIT, ZERO, ORDER):
                with self.subTest(key=key[:4].hex()):
                    with self.assertRaises(ValueError):
                        key_manager.check_expanded_secret_key(key)

    def test_pynacl_missing_is_none(self):
        with mock.patch.dict(sys.modules, {"nacl": None, "nacl.bindings": None,
                                           "nacl.exceptions": None}):
            self.assertIsNone(key_manager._nacl_public_key(GOOD[:32]))

    def test_libsodium_disagreeing_is_refused(self):
        with mock.patch.object(key_manager, "_nacl_public_key", return_value=b"\x01" * 32):
            with self.assertRaises(ValueError) as cm:
                key_manager.check_expanded_secret_key(GOOD)
        self.assertIn("libsodium", str(cm.exception))

    @unittest.skipUnless(HAVE_NACL, "PyNaCl is not installed")
    def test_libsodium_refusing_a_scalar_is_a_value_error(self):
        with self.assertRaises(ValueError):
            key_manager._nacl_public_key(ORDER[:32])


class TestEveryImportPathChecks(unittest.TestCase):

    def test_decode_base32_key(self):
        self.assertEqual(key_manager.decode_base32_key(b32(GOOD)), GOOD)
        header = key_manager.CTOR_HEADER_SECRET
        self.assertEqual(key_manager.decode_base32_key(b32(header + GOOD)), GOOD)
        for key in (TOP_BIT, ZERO, header + TOP_BIT):
            with self.subTest(key=len(key)):
                with self.assertRaises(ValueError):
                    key_manager.decode_base32_key(b32(key))
        with self.assertRaises(ValueError) as cm:
            key_manager.decode_base32_key("not base32 at all!")
        self.assertIn("base32", str(cm.exception))

    def test_import_key_b32_writes_nothing_for_a_refused_key(self):
        with tempfile.TemporaryDirectory() as out:
            for key in (TOP_BIT, ZERO):
                with self.assertRaises(ValueError):
                    key_manager.import_key_b32(b32(key), out)
            self.assertEqual(os.listdir(out), [])
            address = key_manager.import_key_b32(b32(GOOD), out)
            self.assertEqual(key_manager.staged_key_address(os.path.join(out, address)),
                             address)

    def test_key_files_with_a_refused_key(self):
        """A restore's staged_key_address and key_pair_onion_address go
        through the same gate."""
        with self.assertRaises(ValueError):
            key_manager.key_pair_onion_address(TOP_BIT, key_manager.derive_public_key(TOP_BIT))
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "hs_ed25519_secret_key"), "wb") as f:
                f.write(key_manager.CTOR_HEADER_SECRET + ZERO)
            with self.assertRaises(ValueError):
                key_manager.staged_key_address(d)

    def _cli(self, *args, key):
        return subprocess.run([sys.executable, KEY_MANAGER, *args], input=key + "\n",
                              capture_output=True, text=True, timeout=60)

    def test_the_command_line(self):
        address = key_manager.derive_onion_address(key_manager.derive_public_key(GOOD))
        out = self._cli("decode-key", "-", key=b32(GOOD))
        self.assertEqual((out.returncode, out.stdout.strip()), (0, address), out.stderr)
        with tempfile.TemporaryDirectory() as d:
            for key in (TOP_BIT, ZERO, b"\x00" * 13):
                with self.subTest(size=len(key)):
                    out = self._cli("decode-key", "-", key=b32(key))
                    self.assertEqual(out.returncode, 1)
                    self.assertTrue(out.stderr.startswith("Error: "), out.stderr)
                    out = self._cli("import-key", "-", d, key=b32(key))
                    self.assertEqual(out.returncode, 1)
            self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
