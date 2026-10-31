"""onion_keys: the one reader/writer of an onion service key's OpenSSH PEM,
and the copies the tor image is built from (build/sync-onion-keys.sh)."""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import key_manager, onion_auth, onion_keys  # noqa: E402

PRIV = bytes([7]) * 32 + bytes(range(32))
PUB = onion_auth.derive_public_key(PRIV)


def _read(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


class TestPem(unittest.TestCase):
    def test_round_trip(self):
        pem = onion_keys.build_pem(PRIV, PUB)
        self.assertEqual(onion_keys.parse_pem(pem), (PRIV, PUB))
        self.assertEqual(onion_keys.public_key_of_pem(pem), PUB)

    def test_key_manager_is_the_same_code(self):
        pem = key_manager.build_openssh_key(PRIV, PUB)
        self.assertEqual(onion_keys.parse_pem(pem), (PRIV, PUB))
        self.assertEqual(key_manager.parse_openssh_key(pem), (PRIV, PUB))
        self.assertIs(key_manager.derive_onion_address, onion_auth.derive_onion_address)

    def test_refusals(self):
        pem = onion_keys.build_pem(PRIV, PUB)
        for bad in (b"", b"junk", pem.replace(b"b3BlbnNzaC1rZXktdjE", b"AAAAAAAAAAAAAAAAAAA")):
            self.assertIsNone(onion_keys.public_key_of_pem(bad))
        with self.assertRaises(ValueError):
            onion_keys.build_pem(PRIV[:63], PUB)

    def test_embedded_public_key_only_when_allowed(self):
        raw = onion_keys.pack_string  # build a 96-byte-secret key by hand
        priv = (b"\x01\x02\x03\x04" * 2 + raw(onion_keys.KEY_TYPE) + raw(PUB)
                + raw(PRIV + PUB) + raw(b""))
        priv += bytes(range(1, (8 - len(priv) % 8) % 8 + 1))
        import base64
        binary = (onion_keys.OPENSSH_MAGIC + raw(b"none") + raw(b"none") + raw(b"")
                  + b"\x00\x00\x00\x01" + raw(raw(onion_keys.KEY_TYPE) + raw(PUB)) + raw(priv))
        pem = (onion_keys.PEM_BEGIN + "\n" + base64.b64encode(binary).decode() + "\n"
               + onion_keys.PEM_END + "\n").encode()
        with self.assertRaises(ValueError):
            onion_keys.parse_pem(pem)
        self.assertEqual(onion_keys.parse_pem(pem, allow_embedded_public=True), (PRIV, PUB))

    def test_keypair_error(self):
        self.assertIsNone(onion_keys.keypair_error(PRIV, PUB))
        self.assertTrue(onion_keys.keypair_error(PRIV, bytes(32)))


class TestOneSource(unittest.TestCase):
    def test_image_copies_equal_the_source(self):
        for name, copies in (("onion_keys.py", ("app/Resources/docker/tor",)),
                             ("onion_auth.py", ("app/Resources/docker/tor", "tests/stress"))):
            for d in copies:
                self.assertEqual(_read(f"{d}/{name}"), _read(f"src/onionpress/{name}"),
                                 f"{d}/{name} differs: run build/sync-onion-keys.sh")

    def test_no_second_derivation_or_parser(self):
        """Address derivation and PEM building live in onion_auth and onion_keys only."""
        tor = "app/Resources/docker/tor"
        for rel in (f"{tor}/web-server.py", f"{tor}/key-convert.py",
                    f"{tor}/tor-watchdog.py", "src/onionpress/key_manager.py"):
            text = _read(rel).decode()
            self.assertNotIn("sha3_256", text, rel)
            self.assertNotIn("openssh-key-v1", text, rel)
        self.assertIsNone(re.search(r"def (build_openssh_key|_pack_string)",
                                    _read(f"{tor}/web-server.py").decode()))

    def test_image_installs_both(self):
        self.assertIn("COPY onion_keys.py /onion_keys.py", _read("app/Resources/docker/tor/Dockerfile").decode())
        self.assertIn("scripts/onion_keys.py", _read("linux/install-tree.sh").decode())
        self.assertIn("onionpress.onion_keys", _read("setup.py").decode())


if __name__ == "__main__":
    unittest.main()
