"""Tests for onion_auth — Ed25519 signatures for OnionHeaven API.

Verification is libsodium's (PyNaCl's VerifyKey, crypto_sign_open): the
RFC 8032 vectors, every signature the expanded-key signer makes (Tor keys,
mkp224o vanity keys that have no seed), and the refusals a cofactorless
textbook verifier gets wrong (small-order public keys, S >= L) are checked
here. The pure-Python verifier it replaced accepted R=identity, S=0 as a
signature of any message for the identity public key: the onion address
aeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaahmjqd.onion could be
"signed for" by anyone. Without PyNaCl, verify raises; it never says yes.

Signing is RFC 8032 put together from libsodium's constant-time primitives
(scalar_reduce, scalarmult_ed25519_base_noclamp, scalar_mul, scalar_add):
byte for byte libsodium's own crypto_sign for a key from a seed, the RFC
8032 vectors, and byte for byte the pure-Python signer it replaced
(_reference_sign below) for every key, so the wire format is unchanged.
Without libsodium's ed25519 functions it falls back to that pure-Python
arithmetic, with a warning.

PyNaCl must be installed (the CI does; make test-unit uses a venv with it).
"""

import ast
import base64
import contextlib
import hashlib
import os
import sys
import unittest
from unittest import mock

# Allow importing from src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from onionpress import onion_auth
from nacl.signing import SigningKey, VerifyKey
from nacl import bindings as sodium

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPIES = (
    "src/onionpress/onion_auth.py",
    "app/Resources/docker/tor/onion_auth.py",
    "tests/stress/onion_auth.py",
)


def _expand_seed(seed_hex):
    """Derive expanded key + public key from an ed25519 seed (for test vectors)."""
    seed = bytes.fromhex(seed_hex)
    h = hashlib.sha512(seed).digest()
    a_bytes = bytearray(h[:32])
    # Clamp
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    prefix = h[32:]
    expanded = bytes(a_bytes) + prefix
    # Derive public key: A = a * B
    return expanded, onion_auth.derive_public_key(expanded)


class TestEddsaVerify(unittest.TestCase):
    """Verify against RFC 8032 §7.1 test vectors (verify-only)."""

    def test_rfc8032_vector1_empty_message(self):
        pub = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa62325af021a68f707511a"
        )
        sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        self.assertTrue(onion_auth.verify(pub, b"", sig))

    def test_rfc8032_vector2_one_byte(self):
        pub = bytes.fromhex(
            "3d4017c3e843895a92b70aa74d1b7ebc"
            "9c982ccf2ec4968cc0cd55f12af4660c"
        )
        sig = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540"
            "a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        )
        msg = bytes.fromhex("72")
        self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_bad_signature_rejected(self):
        pub = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa3f4a18446b0b8d183f8e3"
        )
        sig = bytes(64)  # all zeros
        self.assertFalse(onion_auth.verify(pub, b"", sig))


class TestExpandedKeySignVerify(unittest.TestCase):
    """Sign with expanded keys derived from RFC 8032 seeds, then verify."""

    def test_vector1_sign_verify(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"")
        self.assertTrue(onion_auth.verify(pub, b"", sig))

    def test_vector1_matches_nacl_signature(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expected_sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"")
        self.assertEqual(sig, expected_sig)

    def test_vector2_sign_verify(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expanded, pub = _expand_seed(seed)
        msg = bytes.fromhex("72")
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_vector2_matches_nacl_signature(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expected_sig = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540"
            "a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        )
        expanded, pub = _expand_seed(seed)
        msg = bytes.fromhex("72")
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertEqual(sig, expected_sig)

    def test_wrong_message_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"hello")
        self.assertFalse(onion_auth.verify(pub, b"world", sig))

    def test_random_key_roundtrip(self):
        """Generate a random expanded key, sign, verify."""
        h = hashlib.sha512(os.urandom(32)).digest()
        a_bytes = bytearray(h[:32])
        a_bytes[0] &= 248
        a_bytes[31] &= 127
        a_bytes[31] |= 64
        expanded = bytes(a_bytes) + h[32:]
        pub = onion_auth.derive_public_key(expanded)
        msg = b"test message for ed25519"
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertTrue(onion_auth.verify(pub, msg, sig))
        self.assertFalse(onion_auth.verify(pub, b"tampered", sig))


class TestOnionAddress(unittest.TestCase):
    """Test onion address encode / decode round-trip."""

    def test_derive_then_decode_roundtrip(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        _, pub = _expand_seed(seed)
        address = onion_auth.derive_onion_address(pub)
        self.assertTrue(address.endswith('.onion'))
        self.assertEqual(len(address), 62)  # 56 chars + ".onion"
        decoded = onion_auth.decode_onion_address(address)
        self.assertEqual(decoded, pub)

    def test_decode_known_address(self):
        """OnionHeaven address decodes without error."""
        addr = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        pub = onion_auth.decode_onion_address(addr)
        self.assertEqual(len(pub), 32)
        # Re-derive address to verify
        self.assertEqual(onion_auth.derive_onion_address(pub), addr)

    def test_bad_checksum_rejected(self):
        addr = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2a.onion"
        with self.assertRaises(ValueError):
            onion_auth.decode_onion_address(addr)


class TestPayloadSignVerify(unittest.TestCase):
    """Test the high-level payload sign/verify helpers."""

    def test_roundtrip(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)
        hc_addr = "abc" * 18 + "aa.onion"  # dummy, won't be decoded

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "register", content_addr, hc_addr, ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "register", hc_addr, ts, sig_b64
        )
        self.assertTrue(ok, f"Verification failed: {err}")

    def test_wrong_endpoint_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "register", content_addr, "", ts
        )

        ok, _ = onion_auth.verify_payload(
            content_addr, "unregister", "", ts, sig_b64
        )
        self.assertFalse(ok)

    def test_expired_timestamp_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = "2020-01-01T00:00:00Z"  # far in the past
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "online", content_addr, "", ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "online", "", ts, sig_b64
        )
        self.assertFalse(ok)
        self.assertIn("expired", err.lower())

    def test_empty_healthcheck_address(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "unregister", content_addr, "", ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "unregister", "", ts, sig_b64
        )
        self.assertTrue(ok, f"Verification failed: {err}")


# ─── Verification is libsodium's ─────────────────────────────────────────

# RFC 8032 §7.1: (secret seed, public key, message, signature).
RFC8032_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
     "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
     "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]

# Small-order points (libsodium's blocklist): the identity, order 2 and 4,
# the two order-8 points, and a non-canonical identity (y = p + 1).
SMALL_ORDER = [
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
]
IDENTITY = bytes.fromhex(SMALL_ORDER[0])
_L = 2**252 + 27742317777372353535851937790883648493


def _mkp224o_style_key(steps):
    """An expanded key the way mkp224o makes a vanity key: a clamped
    scalar advanced by multiples of 8 (no seed gives it). The public key is
    libsodium's, not the code under test's."""
    h = bytearray(hashlib.sha512(os.urandom(32)).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(h[:32], "little") + 8 * steps
    expanded = a.to_bytes(32, "little") + bytes(h[32:])
    return expanded, sodium.crypto_scalarmult_ed25519_base_noclamp(expanded[:32])


# ─── Signing is libsodium's primitives ───────────────────────────────────

def _reference_sign(expanded_key, public_key, message):
    """The arithmetic of the pure-Python signer onion_auth shipped before
    libsodium signed (RFC 8032 in Python integers, the scalar used as it
    is), and the public key it gave: what libsodium signs must be byte for
    byte what it made. Returns (signature, a·B)."""
    p = 2**255 - 19
    d = -121665 * pow(121666, p - 2, p) % p

    def add(P, Q):
        X1, Y1, Z1, T1 = P
        X2, Y2, Z2, T2 = Q
        A = (Y1 - X1) * (Y2 - X2) % p
        B_ = (Y1 + X1) * (Y2 + X2) % p
        C = T1 * 2 * d * T2 % p
        D = Z1 * 2 * Z2 % p
        E, F, G, H = (B_ - A) % p, (D - C) % p, (D + C) % p, (B_ + A) % p
        return (E * F % p, G * H % p, F * G % p, E * H % p)

    def mult(s):
        By = 4 * pow(5, p - 2, p) % p
        Bx = 15112221349535400772501151409588531511454012693041857206046113283949847762202
        P, Q = (Bx, By, 1, Bx * By % p), (0, 1, 1, 0)
        while s > 0:
            if s & 1:
                Q = add(Q, P)
            P = add(P, P)
            s >>= 1
        X, Y, Z, _ = Q
        zi = pow(Z, p - 2, p)
        enc = bytearray((Y * zi % p).to_bytes(32, "little"))
        if X * zi % p & 1:
            enc[31] |= 0x80
        return bytes(enc)

    a = int.from_bytes(expanded_key[:32], "little")
    r = int.from_bytes(hashlib.sha512(expanded_key[32:] + message).digest(), "little") % _L
    R = mult(r)
    h = int.from_bytes(hashlib.sha512(R + public_key + message).digest(), "little") % _L
    return R + ((r + h * a) % _L).to_bytes(32, "little"), mult(a)


def _keys_of_every_kind(count):
    """(expanded, public) pairs: from seeds (libsodium can sign with these
    too), mkp224o-style without a seed, and 64 random bytes (scalar bit
    255 set half the time; the scalar is taken mod L either way)."""
    keys = []
    for i in range(count):
        keys.append(_expand_seed(os.urandom(32).hex()))
        keys.append(_mkp224o_style_key(i * 997))
        raw = os.urandom(64)
        keys.append((raw, _reference_sign(raw, bytes(32), b"")[1]))
    return keys


class _Spy:
    """Stands in for nacl.bindings and records which functions are called."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        real = getattr(sodium, name)
        if not callable(real):
            return real

        def call(*args):
            self.calls.append(name)
            return real(*args)
        return call


class TestSigningIsLibsodium(unittest.TestCase):

    def test_rfc8032_vectors_from_the_expanded_key(self):
        for seed, pub, msg, sig in RFC8032_VECTORS:
            with self.subTest(pub=pub[:8]):
                expanded, derived = _expand_seed(seed)
                pub, msg, sig = map(bytes.fromhex, (pub, msg, sig))
                self.assertEqual(derived, pub)
                self.assertEqual(onion_auth.sign_expanded(expanded, pub, msg), sig)

    def test_byte_for_byte_libsodiums_crypto_sign(self):
        for i in range(200):
            seed = os.urandom(32)
            expanded, pub = _expand_seed(seed.hex())
            self.assertEqual(pub, SigningKey(seed).verify_key.encode())
            msg = os.urandom(i * 7 % 300)
            self.assertEqual(onion_auth.sign_expanded(expanded, pub, msg),
                             SigningKey(seed).sign(msg).signature)

    def test_seedless_keys_verify_with_libsodium(self):
        for steps in (0, 1, 8, 1000, 2**20, 2**40):
            expanded, pub = _mkp224o_style_key(steps)
            self.assertEqual(onion_auth.derive_public_key(expanded), pub)
            msg = b"online|" + os.urandom(40)
            sig = onion_auth.sign_expanded(expanded, pub, msg)
            self.assertEqual(VerifyKey(pub).verify(msg, sig), msg)

    def test_the_wire_format_is_unchanged(self):
        """Same bytes as the pure-Python signer it replaced, for every kind
        of key, with libsodium and with the fallback."""
        with mock.patch.object(onion_auth, "_fallback_logged", {"Signing", "Deriving a public key"}):
            for expanded, pub in _keys_of_every_kind(25):
                msg = os.urandom(int.from_bytes(os.urandom(1), "little"))
                with self.subTest(key=expanded[:8].hex()):
                    old, old_pub = _reference_sign(expanded, pub, msg)
                    self.assertEqual(onion_auth.derive_public_key(expanded), old_pub)
                    self.assertEqual(onion_auth.sign_expanded(expanded, pub, msg), old)
                    with mock.patch.object(onion_auth, "_sodium", return_value=None):
                        self.assertEqual(onion_auth.derive_public_key(expanded), old_pub)
                        self.assertEqual(onion_auth.sign_expanded(expanded, pub, msg), old)

    def test_only_libsodium_does_the_arithmetic(self):
        expanded, pub = _mkp224o_style_key(5)
        spy = _Spy()
        fail = mock.Mock(side_effect=AssertionError("pure-Python arithmetic used"))
        with mock.patch.object(onion_auth, "_sodium", return_value=spy), \
                mock.patch.object(onion_auth, "_scalar_mult", fail), \
                mock.patch.object(onion_auth, "_python_sign", fail), \
                mock.patch.object(onion_auth, "_python_public_key", fail):
            sig = onion_auth.sign_expanded(expanded, pub, b"offline|x")
            self.assertEqual(onion_auth.derive_public_key(expanded), pub)
        self.assertTrue(onion_auth.verify(pub, b"offline|x", sig))
        self.assertEqual(spy.calls, [
            # a mod L, and A = a·B checked against the public key
            "crypto_core_ed25519_scalar_reduce", "crypto_scalarmult_ed25519_base_noclamp",
            # r, R = r·B, k, S = r + k·a
            "crypto_core_ed25519_scalar_reduce", "crypto_scalarmult_ed25519_base_noclamp",
            "crypto_core_ed25519_scalar_reduce", "crypto_core_ed25519_scalar_mul",
            "crypto_core_ed25519_scalar_add",
            # derive_public_key
            "crypto_core_ed25519_scalar_reduce", "crypto_scalarmult_ed25519_base_noclamp"])

    def test_no_arithmetic_of_its_own_on_the_libsodium_path(self):
        with open(os.path.join(PROJECT_DIR, COPIES[0]), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("sign_expanded", "derive_public_key", "_sodium_public_key"):
            with self.subTest(name):
                for node in ast.walk(funcs[name]):
                    if isinstance(node, ast.BinOp):
                        # Only bytes are put together (+).
                        self.assertIsInstance(node.op, ast.Add, ast.unparse(node))
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, {"pow", "int", "_scalar_mult",
                                                        "_point_add", "_encode_point"})
                # The fallback is reached only without libsodium.
                fallback = [n for n in ast.walk(funcs[name]) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Name)
                            and n.func.id.startswith("_python_")]
                for call in fallback:
                    guard = next(n for n in ast.walk(funcs[name]) if isinstance(n, ast.If)
                                 and call in list(ast.walk(n)))
                    self.assertEqual(ast.unparse(guard.test), "sodium is None")

    def test_a_public_key_not_its_own_is_refused(self):
        """Two signatures of one message under two public keys give the
        secret scalar away; the signer takes only a·B."""
        expanded, pub = _mkp224o_style_key(3)
        _, other = _mkp224o_style_key(4)
        for sodium_present in (True, False):
            with self.subTest(libsodium=sodium_present), \
                    mock.patch.object(onion_auth, "_fallback_logged", {"Signing"}):
                patch = (contextlib.nullcontext() if sodium_present
                         else mock.patch.object(onion_auth, "_sodium", return_value=None))
                with patch:
                    for bad in (other, IDENTITY, bytes(32), pub[:31] + bytes([pub[31] ^ 0x80])):
                        with self.assertRaises(ValueError) as cm:
                            onion_auth.sign_expanded(expanded, bad, b"m")
                        self.assertIn("not the expanded key's", str(cm.exception))

    def test_a_scalar_with_no_public_key(self):
        prefix = os.urandom(32)
        for scalar in (bytes(32), _L.to_bytes(32, "little"), (3 * _L).to_bytes(32, "little")):
            for sodium_present in (True, False):
                with self.subTest(scalar=scalar.hex(), libsodium=sodium_present), \
                        mock.patch.object(onion_auth, "_fallback_logged",
                                          {"Signing", "Deriving a public key"}):
                    patch = (contextlib.nullcontext() if sodium_present
                             else mock.patch.object(onion_auth, "_sodium", return_value=None))
                    with patch:
                        with self.assertRaises(ValueError) as cm:
                            onion_auth.derive_public_key(scalar + prefix)
                        self.assertIn("no public key", str(cm.exception))
                        with self.assertRaises(ValueError):
                            onion_auth.sign_expanded(scalar + prefix, IDENTITY, b"m")

    def test_byte_like_arguments(self):
        expanded, pub = _mkp224o_style_key(9)
        sig = onion_auth.sign_expanded(expanded, pub, b"hello")
        self.assertEqual(onion_auth.sign_expanded(bytearray(expanded), memoryview(pub),
                                                  bytearray(b"hello")), sig)
        self.assertEqual(onion_auth.derive_public_key(bytearray(expanded)), pub)
        for bad in (expanded[:63], expanded + b"\0"):
            with self.assertRaises(ValueError):
                onion_auth.sign_expanded(bad, pub, b"hello")
            with self.assertRaises(ValueError):
                onion_auth.derive_public_key(bad)
        with self.assertRaises(ValueError):
            onion_auth.sign_expanded(expanded, pub[:31], b"hello")


class TestSigningFallback(unittest.TestCase):
    """Without libsodium's ed25519 functions, signing goes on (a heartbeat
    that cannot be signed lets the hub take the site over) on the old
    arithmetic, and says so once."""

    HIDDEN = {"nacl": None, "nacl.signing": None, "nacl.exceptions": None,
              "nacl.bindings": None}

    def test_without_pynacl(self):
        expanded, pub = _mkp224o_style_key(11)
        sig = onion_auth.sign_expanded(expanded, pub, b"online|x")
        self.assertTrue(onion_auth.signing_uses_libsodium())
        with mock.patch.dict(sys.modules, self.HIDDEN), \
                mock.patch.object(onion_auth, "_fallback_logged", set()):
            self.assertFalse(onion_auth.signing_uses_libsodium())
            with self.assertLogs(onion_auth.__name__, "WARNING") as logs:
                for _ in range(3):
                    self.assertEqual(onion_auth.sign_expanded(expanded, pub, b"online|x"), sig)
                    self.assertEqual(onion_auth.derive_public_key(expanded), pub)
        self.assertEqual(len(logs.output), 2, logs.output)
        for line in logs.output:
            self.assertIn("python3-nacl", line)
            self.assertIn("not constant-time", line)
        self.assertTrue(onion_auth.signing_uses_libsodium())

    def test_a_minimal_libsodium(self):
        """PyNaCl on a libsodium built --enable-minimal has no ed25519 core
        functions (they raise UnavailableError)."""
        for flag in ("has_crypto_core_ed25519", "has_crypto_scalarmult_ed25519"):
            with self.subTest(flag), mock.patch.object(sodium, flag, False):
                self.assertIsNone(onion_auth._sodium())
                self.assertFalse(onion_auth.signing_uses_libsodium())
        self.assertIs(onion_auth._sodium(), sodium)


class TestCopiesAreOneFile(unittest.TestCase):

    def test_identical(self):
        texts = set()
        for rel in COPIES:
            with open(os.path.join(PROJECT_DIR, rel), "rb") as f:
                texts.add(f.read())
        self.assertEqual(len(texts), 1, f"{COPIES} must be identical")


class TestLibsodiumVerifies(unittest.TestCase):

    def test_rfc8032_vectors(self):
        for seed, pub, msg, sig in RFC8032_VECTORS:
            seed, pub, msg, sig = map(bytes.fromhex, (seed, pub, msg, sig))
            # The vectors are libsodium's own output too.
            self.assertEqual(SigningKey(seed).verify_key.encode(), pub)
            self.assertEqual(SigningKey(seed).sign(msg).signature, sig)
            self.assertTrue(onion_auth.verify(pub, msg, sig))
            self.assertFalse(onion_auth.verify(pub, msg + b"\0", sig))
            # The expanded-key signer makes the same bytes from the seed's
            # expansion: the wire format is RFC 8032's.
            expanded, _ = _expand_seed(seed.hex())
            self.assertEqual(onion_auth.sign_expanded(expanded, pub, msg), sig)

    def test_the_pure_python_signer_is_accepted(self):
        for _ in range(25):
            seed = os.urandom(32)
            expanded, pub = _expand_seed(seed.hex())
            self.assertEqual(pub, SigningKey(seed).verify_key.encode())
            msg = os.urandom(int.from_bytes(os.urandom(1), "little"))
            sig = onion_auth.sign_expanded(expanded, pub, msg)
            self.assertEqual(sig, SigningKey(seed).sign(msg).signature)
            self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_seedless_vanity_keys_are_accepted(self):
        for steps in (0, 1, 1000, 2**20):
            expanded, pub = _mkp224o_style_key(steps)
            msg = b"online|" + os.urandom(16)
            self.assertTrue(onion_auth.verify(
                pub, msg, onion_auth.sign_expanded(expanded, pub, msg)))

    def test_payloads_from_the_signer(self):
        expanded, pub = _mkp224o_style_key(7)
        addr = onion_auth.derive_onion_address(pub)
        hc = onion_auth.derive_onion_address(_mkp224o_style_key(3)[1])
        ts = onion_auth.make_timestamp()
        for endpoint in ("online", "offline", "unregister", "logs", "onionheaven-peer"):
            sig = onion_auth.sign_payload(expanded, pub, endpoint, addr, hc, ts)
            self.assertEqual(onion_auth.verify_payload(addr, endpoint, hc, ts, sig),
                             (True, ""))
            self.assertFalse(onion_auth.verify_payload(addr, endpoint, "", ts, sig)[0])
        for endpoint in ("register", "release"):
            sig = onion_auth.sign_name_payload(expanded, pub, endpoint, addr, "Name", ts)
            self.assertEqual(
                onion_auth.verify_name_payload(addr, endpoint, "name", ts, sig), (True, ""))
            self.assertFalse(
                onion_auth.verify_name_payload(addr, endpoint, "other", ts, sig)[0])


class TestStrictRefusals(unittest.TestCase):

    def setUp(self):
        seed, pub, msg, sig = map(bytes.fromhex, RFC8032_VECTORS[2])
        self.pub, self.msg, self.sig = pub, msg, sig

    def test_s_not_below_l(self):
        s = int.from_bytes(self.sig[32:], "little")
        for bad in (s + _L, _L, 2**256 - 1):
            self.assertFalse(onion_auth.verify(
                self.pub, self.msg, self.sig[:32] + bad.to_bytes(32, "little")))

    def test_small_order_public_keys(self):
        """R = identity, S = 0 'signs' anything for a small-order key under
        cofactorless textbook verification; libsodium refuses the key."""
        forged = IDENTITY + bytes(32)
        for hexpub in SMALL_ORDER:
            pub = bytes.fromhex(hexpub)
            for msg in (b"", b"offline|x", os.urandom(32)):
                self.assertFalse(onion_auth.verify(pub, msg, forged), hexpub)
                s = os.urandom(31) + b"\0"
                r_enc = sodium.crypto_scalarmult_ed25519_base_noclamp(s)
                self.assertFalse(onion_auth.verify(pub, msg, r_enc + s), hexpub)

    def test_small_order_onion_address_cannot_be_signed_for(self):
        addr = onion_auth.derive_onion_address(IDENTITY)
        self.assertEqual(addr,
                         "aeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaahmjqd.onion")
        ts = onion_auth.make_timestamp()
        forged = base64.b64encode(IDENTITY + bytes(32)).decode()
        for endpoint in ("offline", "unregister", "online", "logs"):
            self.assertEqual(onion_auth.verify_payload(addr, endpoint, "", ts, forged),
                             (False, "Invalid signature"))
        self.assertEqual(onion_auth.verify_name_payload(addr, "register", "x", ts, forged),
                         (False, "Invalid signature"))

    def test_small_order_r(self):
        for hexr in SMALL_ORDER:
            self.assertFalse(onion_auth.verify(
                self.pub, self.msg, bytes.fromhex(hexr) + self.sig[32:]))

    def test_non_canonical_public_key(self):
        """y >= p: the second encoding of y - p (and of its negative)."""
        p = 2**255 - 19
        for y in range(p, 2**255):
            for sign in (0, 1 << 255):
                pub = (y | sign).to_bytes(32, "little")
                self.assertFalse(onion_auth.verify(pub, self.msg, self.sig))
                self.assertFalse(onion_auth.verify(pub, b"", IDENTITY + bytes(32)))

    def test_malformed(self):
        for pub, msg, sig in (
                (self.pub, self.msg, self.sig[:63]),
                (self.pub, self.msg, self.sig + b"\0"),
                (self.pub[:31], self.msg, self.sig),
                (self.pub, self.msg, bytes(64)),
                (self.pub.hex(), self.msg, self.sig),
                (self.pub, self.msg.hex(), self.sig),
                (32, self.msg, self.sig),        # bytes(32) would be a key of zeros
                (self.pub, self.msg, 64),
                (None, self.msg, self.sig),
                (self.pub, None, self.sig)):
            self.assertFalse(onion_auth.verify(pub, msg, sig), (pub, msg, sig))
        self.assertTrue(onion_auth.verify(bytearray(self.pub), memoryview(self.msg),
                                          bytearray(self.sig)))

    def test_payload_fields(self):
        expanded, pub = _mkp224o_style_key(1)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "offline", addr, "", ts)
        ok = onion_auth.verify_payload
        self.assertTrue(ok(addr, "offline", "", ts, sig)[0])
        # Only the standard alphabet, nothing between: the decoder used to
        # skip characters it did not know.
        for bad in (sig[:10] + "\n" + sig[10:], sig[:10] + "$" + sig[10:],
                    sig.replace("+", "-").replace("/", "_") if ("+" in sig or "/" in sig)
                    else sig + "!", sig.rstrip("=")):
            if bad != sig:
                self.assertEqual(ok(addr, "offline", "", ts, bad),
                                 (False, "Invalid signature base64"))
        for bad_ts, bad_sig in ((None, sig), (5, sig), ("", sig), (ts, None),
                                (ts, ["x"]), (ts, 5)):
            self.assertFalse(ok(addr, "offline", "", bad_ts, bad_sig)[0])
        for bad_addr in (None, 5, ["x"], addr.upper(), addr[:-6]):
            self.assertFalse(ok(bad_addr, "offline", "", ts, sig)[0])
        self.assertEqual(onion_auth.verify_name_payload(addr, "register", None, ts, sig),
                         (False, "Invalid name"))


class TestWithoutPyNaCl(unittest.TestCase):
    """No PyNaCl: no answer, and above all never a yes."""

    HIDDEN = {"nacl": None, "nacl.signing": None, "nacl.exceptions": None,
              "nacl.bindings": None}

    def test_verify_raises(self):
        seed, pub, msg, sig = map(bytes.fromhex, RFC8032_VECTORS[0])
        with mock.patch.dict(sys.modules, self.HIDDEN):
            self.assertFalse(onion_auth.verification_available())
            with self.assertRaises(onion_auth.VerificationUnavailable):
                onion_auth.verify(pub, msg, sig)
        self.assertTrue(onion_auth.verification_available())
        self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_payload_verification_raises(self):
        expanded, pub = _mkp224o_style_key(2)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_payload(expanded, pub, "unregister", addr, "", ts)
        name_sig = onion_auth.sign_name_payload(expanded, pub, "release", addr, "n", ts)
        with mock.patch.dict(sys.modules, self.HIDDEN), \
                mock.patch.object(onion_auth, "_fallback_logged", set()):
            with self.assertRaises(onion_auth.VerificationUnavailable):
                onion_auth.verify_payload(addr, "unregister", "", ts, sig)
            with self.assertRaises(onion_auth.VerificationUnavailable):
                onion_auth.verify_name_payload(addr, "release", "n", ts, name_sig)
            # Signing falls back on the pure-Python arithmetic: the same
            # signature, and a warning.
            with self.assertLogs(onion_auth.__name__, "WARNING"):
                self.assertEqual(onion_auth.sign_payload(expanded, pub, "unregister",
                                                         addr, "", ts), sig)

    def test_unavailable_is_not_a_value_error(self):
        # Callers that catch ValueError for bad input must not swallow it.
        self.assertFalse(issubclass(onion_auth.VerificationUnavailable, ValueError))


class TestNoVerifierOfOurOwn(unittest.TestCase):
    """verify() hands the decision to libsodium; nothing else in the code
    that ships decodes points for a verification."""

    def test_verify_is_libsodium(self):
        with open(os.path.join(PROJECT_DIR, COPIES[0]), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertNotIn("_decode_point", funcs)
        body = ast.unparse(funcs["verify"])
        self.assertIn("VerifyKey(public_key).verify(message, signature)", body)
        self.assertNotIn("_scalar_mult", body)
        self.assertNotIn("_point_add", body)

    def test_verification_call_sites(self):
        """Every signature check in the code that ships calls onion_auth's
        verify_payload / verify_name_payload; web-server.py is the only
        verifier (the hub). A new one has to be looked at here."""
        expected = {
            ("app/Resources/docker/tor/web-server.py", "_verify_signature", "verify_payload"),
            ("app/Resources/docker/tor/web-server.py", "_signed_peer_claim", "verify_payload"),
            ("app/Resources/docker/tor/web-server.py", "_handle_name_register",
             "verify_name_payload"),
            ("app/Resources/docker/tor/web-server.py", "_handle_name_release",
             "verify_name_payload"),
            ("app/Resources/docker/tor/web-server.py", "_handle_logs_manifest",
             "verify_payload"),
            ("app/Resources/docker/tor/web-server.py", "_handle_logs_upload",
             "verify_payload"),
        }
        names = {"verify", "verify_payload", "verify_name_payload"}
        found = set()
        for root in ("src", "linux", os.path.join("app", "Resources", "docker", "tor"),
                     os.path.join("app", "MacOS")):
            for dirpath, dirnames, filenames in os.walk(os.path.join(PROJECT_DIR, root)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fname in filenames:
                    if not fname.endswith(".py"):
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, fname), PROJECT_DIR)
                    if rel in COPIES:
                        continue
                    with open(os.path.join(dirpath, fname), encoding="utf-8") as f:
                        tree = ast.parse(f.read(), rel)
                    for func in ast.walk(tree):
                        if not isinstance(func, ast.FunctionDef):
                            continue
                        for node in ast.walk(func):
                            if not isinstance(node, ast.Call):
                                continue
                            f_ = node.func
                            name = (f_.id if isinstance(f_, ast.Name)
                                    else f_.attr if isinstance(f_, ast.Attribute) else None)
                            if name in names:
                                found.add((rel, func.name, name))
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
