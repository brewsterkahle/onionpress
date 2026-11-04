"""onion_keys: the one reader/writer of an onion service key's OpenSSH PEM
and C Tor's key files, and the copies the tor image is built from
(build/sync-onion-keys.sh)."""

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


class TestCtor(unittest.TestCase):
    """C Tor's hs_ed25519_secret_key / hs_ed25519_public_key: a 32-byte
    header and the key, nothing else."""

    def test_round_trip(self):
        secret, public = onion_keys.build_ctor(PRIV, PUB)
        self.assertEqual(len(secret), 96)
        self.assertEqual(len(public), 64)
        self.assertEqual(secret[:32], b"== ed25519v1-secret: type0 ==\x00\x00\x00")
        self.assertEqual(public[:32], b"== ed25519v1-public: type0 ==\x00\x00\x00")
        self.assertEqual(onion_keys.parse_ctor_secret(secret), PRIV)
        self.assertEqual(onion_keys.parse_ctor_public(public), PUB)
        self.assertEqual(onion_keys.build_ctor(PRIV), (secret, None))

    def test_refusals(self):
        secret, public = onion_keys.build_ctor(PRIV, PUB)
        for bad in (b"", secret[:95], secret + b"\x00", secret[:32] + PRIV[:32],
                    public[:32] + PRIV,            # the public header on a secret key
                    b"== ed25519v1-secret: type0 ==\x01\x00\x00" + PRIV):
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                onion_keys.parse_ctor_secret(bad)
        for bad in (b"", public[:63], public + b"\x00", secret[:32] + PUB):
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                onion_keys.parse_ctor_public(bad)
        with self.assertRaisesRegex(ValueError, "hs_ed25519_secret_key is 95 bytes"):
            onion_keys.parse_ctor_secret(secret[:95])
        with self.assertRaises(ValueError):
            onion_keys.build_ctor(PRIV[:63], PUB)
        with self.assertRaises(ValueError):
            onion_keys.build_ctor(PRIV, PUB[:31])

    def test_key_manager_reads_through_it(self):
        secret, public = onion_keys.build_ctor(PRIV, PUB)
        self.assertIs(key_manager.CTOR_SECRET_HEADER, onion_keys.CTOR_SECRET_HEADER)
        self.assertIs(key_manager.CTOR_HEADER_PUBLIC, onion_keys.CTOR_PUBLIC_HEADER)
        import base64
        self.assertEqual(key_manager.decode_base32_key(base64.b32encode(secret).decode()), PRIV)
        with self.assertRaises(ValueError):   # a 96-byte key with a damaged header
            key_manager.decode_base32_key(base64.b32encode(b"x" + secret[1:]).decode())

    def test_a_hostname_is_decoded_with_its_checksum(self):
        address = onion_auth.derive_onion_address(PUB)
        self.assertEqual(key_manager._pubkey_from_hostname(address.encode() + b"\n"), PUB)
        # one character changed: the right length and alphabet, a bad checksum
        bad = ("b" if address[0] == "a" else "a") + address[1:]
        with self.assertRaisesRegex(ValueError, "checksum"):
            key_manager._pubkey_from_hostname(bad.encode())
        with self.assertRaises(ValueError):
            key_manager._pubkey_from_hostname(b"example.com")


class TestOneSource(unittest.TestCase):
    def test_image_copies_equal_the_source(self):
        for name, copies in (("onion_keys.py", ("app/Resources/docker/tor",)),
                             ("onion_auth.py", ("app/Resources/docker/tor", "tests/stress"))):
            for d in copies:
                self.assertEqual(_read(f"{d}/{name}"), _read(f"src/onionpress/{name}"),
                                 f"{d}/{name} differs: run build/sync-onion-keys.sh")

    def test_no_second_derivation_or_parser(self):
        """Address derivation, PEM building and C Tor's key-file format live
        in onion_auth and onion_keys only."""
        tor = "app/Resources/docker/tor"
        for rel in (f"{tor}/web-server.py", f"{tor}/key-convert.py",
                    f"{tor}/tor-watchdog.py", f"{tor}/onionnames.py",
                    f"{tor}/onionheaven_common.py", f"{tor}/onionheaven-queue-manager.py",
                    f"{tor}/onionheaven-takeover-worker.py", f"{tor}/tor_control.py",
                    "src/onionpress/key_manager.py"):
            text = _read(rel).decode()
            self.assertNotIn("sha3_256", text, rel)
            self.assertNotIn("openssh-key-v1", text, rel)
            # the header is onion_keys' (key-convert's docstring names it)
            code = re.sub(r'(?s)"""(.*?)"""', "", text)
            self.assertNotIn("ed25519v1-", code, rel)
            self.assertIsNone(re.search(r"\[32:\]", code), rel)
        self.assertIsNone(re.search(r"def (build_openssh_key|_pack_string)",
                                    _read(f"{tor}/web-server.py").decode()))

    def test_image_installs_both(self):
        self.assertIn("COPY onion_keys.py /onion_keys.py", _read("app/Resources/docker/tor/Dockerfile").decode())
        self.assertIn("scripts/onion_keys.py", _read("linux/install-tree.sh").decode())
        self.assertIn("onionpress.onion_keys", _read("setup.py").decode())


if __name__ == "__main__":
    unittest.main()
