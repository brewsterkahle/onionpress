#!/usr/bin/env python3
"""What the OnionHeaven hub stores as a registrant's key, and how.

/online carries the site's onion service key as an OpenSSH PEM
(arti_key_pem). The hub checked only that the PEM's public half derives to
content_address. The request signature covers endpoint, addresses and
timestamp, not the PEM, and nothing looked at the private half, which is
what a takeover hands to Tor: a PEM with the right public key and a wrong
secret scalar replaced a good stored key, and that site's next takeover
failed. Now the private section is read the way the takeover path reads it
(key-convert.py), must carry the same public key, and its scalar times the
base point must be that public key, computed by libsodium through PyNaCl
(python3-nacl in the tor image). With that, the PEM proves itself.

And the key was rewritten in place on every heartbeat (open(path, "wb")):
a crash or full disk at that moment left an empty key. write_key_file skips
identical bytes and otherwise writes a new file and renames it into place.
key-convert.py (the node's own key at start) and the watchdog's NEW:BEST
save write the same way.

PyNaCl must be installed for the scalar tests (the CI does; make test-unit
runs them via uv). No Docker: the /online handler runs in-process on a
temp directory.
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

import onionheaven_common as oc  # noqa: E402
from onionpress import onion_auth  # noqa: E402

try:
    from nacl import bindings as sodium
except ImportError:  # pragma: no cover - the CI installs PyNaCl
    sodium = None

needs_sodium = unittest.skipIf(sodium is None, "PyNaCl is not installed")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kc = _load("key_convert_stored_keys", "key-convert.py")
ws = _load("web_server_stored_keys", "web-server.py")
wd = _load("tor_watchdog_stored_keys", "tor-watchdog.py")


def _keypair(seed=None):
    """(expanded_64, public_32, address) the way Tor and mkp224o make them:
    the scalar is SHA-512(seed)'s first half, clamped. The public key is
    libsodium's for that seed, so these tests do not lean on the code under
    test to make their keys."""
    seed = seed or os.urandom(32)
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    public = sodium.crypto_sign_seed_keypair(seed)[0]
    return bytes(h), public, onion_auth.derive_onion_address(public)


def _pack(data):
    return struct.pack(">I", len(data)) + data


def _pem(expanded, public, inner_public=None, check2=None):
    """An Arti PEM; inner_public / check2 doctor the private section."""
    kt = b"ed25519-expanded@spec.torproject.org"
    check = b"\x01\x02\x03\x04"
    priv = (check + (check2 or check) + _pack(kt) + _pack(inner_public or public)
            + _pack(expanded) + _pack(b""))
    priv += bytes(range(1, (8 - len(priv) % 8) % 8 + 1))
    binary = (b"openssh-key-v1\x00" + _pack(b"none") + _pack(b"none") + _pack(b"")
              + struct.pack(">I", 1) + _pack(_pack(kt) + _pack(public)) + _pack(priv))
    b64 = base64.b64encode(binary).decode()
    body = "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
    return (f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n"
            f"-----END OPENSSH PRIVATE KEY-----\n").encode()


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

@needs_sodium
class KeypairErrorTest(unittest.TestCase):

    def test_real_keys_pass(self):
        rfc8032_seed = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        for seed in [rfc8032_seed] + [os.urandom(32) for _ in range(20)]:
            expanded, public, _ = _keypair(seed)
            self.assertIsNone(kc.keypair_error(expanded, public))

    def test_a_changed_scalar_fails(self):
        expanded, public, _ = _keypair()
        for i in (0, 5, 30):
            doctored = bytearray(expanded)
            doctored[i] ^= 0x08
            self.assertIn("does not give the public key",
                          kc.keypair_error(bytes(doctored), public))

    def test_another_keys_scalar_fails(self):
        expanded, _, _ = _keypair()
        _, public, _ = _keypair()
        self.assertIsNotNone(kc.keypair_error(expanded, public))

    def test_the_nonce_half_is_not_the_key(self):
        """Only the scalar makes the address; the second half seeds signing
        nonces, and any value there signs validly."""
        expanded, public, _ = _keypair()
        self.assertIsNone(kc.keypair_error(expanded[:32] + os.urandom(32), public))

    def test_a_top_bit_is_refused(self):
        """libsodium would mask it and check another scalar than Tor gets."""
        expanded, public, _ = _keypair()
        doctored = bytearray(expanded)
        doctored[31] |= 0x80
        self.assertIn("2^255", kc.keypair_error(bytes(doctored), public))

    def test_zero_and_group_order(self):
        _, public, _ = _keypair()
        L = 2**252 + 27742317777372353535851937790883648493
        for scalar in (bytes(32), L.to_bytes(32, "little")):
            self.assertIn("zero or a multiple",
                          kc.keypair_error(scalar + bytes(32), public))

    def test_lengths(self):
        expanded, public, _ = _keypair()
        self.assertIsNotNone(kc.keypair_error(expanded[:63], public))
        self.assertIsNotNone(kc.keypair_error(expanded, public[:31]))

    def test_without_pynacl(self):
        expanded, public, _ = _keypair()
        with mock.patch.dict(sys.modules, {"nacl": None, "nacl.bindings": None}):
            with self.assertRaises(kc.SodiumUnavailable):
                kc.keypair_error(expanded, public)


@needs_sodium
class PrivateKeyProblemTest(unittest.TestCase):
    """web-server.py's check of a registration's PEM."""

    def setUp(self):
        self.expanded, self.public, self.address = _keypair()

    def test_a_good_pem(self):
        self.assertIsNone(ws.private_key_problem(_pem(self.expanded, self.public),
                                                 self.public))

    def test_the_takeover_path_reads_the_same_key(self):
        pem = _pem(self.expanded, self.public)
        self.assertEqual(kc.parse_arti_pem_bytes(pem), (self.expanded, self.public))
        with tempfile.NamedTemporaryFile(suffix=".pem") as f:
            f.write(pem)
            f.flush()
            self.assertEqual(kc.parse_arti_pem(f.name), (self.expanded, self.public))
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                kc.pem_to_ed25519_base64(f.name)
            self.assertEqual(base64.b64decode(out.getvalue()), self.expanded)

    def test_right_public_key_wrong_scalar(self):
        """The reported case: a doctored PEM a signature does not cover."""
        other, _, _ = _keypair()
        self.assertIn("does not give the public key",
                      ws.private_key_problem(_pem(other, self.public), self.public))

    def test_private_section_names_another_key(self):
        other_expanded, other_public, _ = _keypair()
        pem = _pem(other_expanded, self.public, inner_public=other_public)
        self.assertIn("different public key", ws.private_key_problem(pem, self.public))

    def test_unreadable_private_section(self):
        pem = _pem(self.expanded, self.public, check2=b"\x09\x09\x09\x09")
        self.assertIn("cannot be read", ws.private_key_problem(pem, self.public))
        self.assertIn("cannot be read", ws.private_key_problem(b"\xff\xfe", self.public))

    def test_without_pynacl_it_is_stored_and_said_once(self):
        other, _, _ = _keypair()
        logged = []
        with mock.patch.dict(sys.modules, {"nacl": None, "nacl.bindings": None}), \
                mock.patch.object(ws, "log", side_effect=logged.append), \
                mock.patch.object(ws, "_warned_no_sodium", False):
            for _ in range(3):
                self.assertIsNone(ws.private_key_problem(_pem(other, self.public),
                                                         self.public))
            # The structural checks still apply without it.
            pem = _pem(self.expanded, self.public, inner_public=_keypair()[1])
            self.assertIsNotNone(ws.private_key_problem(pem, self.public))
        self.assertEqual(len(logged), 1)
        self.assertIn("PyNaCl", logged[0])


class DockerfileTest(unittest.TestCase):

    def test_the_image_has_pynacl(self):
        with open(os.path.join(DOCKER_TOR_DIR, "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        runtime = text[text.rindex("\nFROM ${TOR_IMAGE}\n"):]
        install = runtime[runtime.index("RUN apt-get update"):]
        install = install[:install.index("\n\n") if "\n\n" in install else None]
        self.assertRegex(install, r"\bpython3-nacl\b")
        self.assertIn("crypto_scalarmult_ed25519_base_noclamp", install)


# ---------------------------------------------------------------------------
# /online, in-process
# ---------------------------------------------------------------------------

class _Handler(ws.OnionHeavenHandler):
    """The real handler without a socket: the body and the reply are
    plain attributes."""

    def __init__(self, data):  # noqa: D401 - no BaseHTTPRequestHandler setup
        self.path = "/online"
        self.client_address = ("127.0.0.1", 0)
        self._data = data
        self.replies = []

    def _refuse_unless_hub(self):
        return False

    def _read_json(self, max_size=None):
        return self._data

    def _send_json(self, status_code, data):
        self.replies.append((status_code, data))


@needs_sodium
class OnlineStoresKeysTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hub-stored-keys-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.keys = os.path.join(self.base, "keys")
        os.makedirs(self.keys)
        for target, name, value in (
                (ws, "KEYS_DIR", self.keys),
                (oc, "ONIONHEAVEN_DATA_DIR", self.base),
                (oc, "DB_PATH", os.path.join(self.base, "registry.db")),
                (ws, "release_function", lambda *a, **k: None),
                (ws, "_get_own_address", lambda: None),
                (ws, "log", lambda *a: None),
                (oc, "log", lambda *a: None),
                (oc, "addr_log", lambda *a: None)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.expanded, self.public, self.address = _keypair()
        self.hc = _keypair()[2]
        self.key_path = os.path.join(self.keys, self.address,
                                     "ks_hs_id.ed25519_expanded_private")

    def _online(self, pem, **extra):
        # Each request a second later than the last: a verified request
        # is accepted once per address and timestamp (issue #400), and a
        # client never sends two heartbeats in one second.
        self._tick = getattr(self, "_tick", 0) + 1
        ts = (datetime.now(timezone.utc) + timedelta(seconds=self._tick)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {
            "content_address": self.address,
            "healthcheck_address": self.hc,
            "timestamp": ts,
            "signature": onion_auth.sign_payload(self.expanded, self.public, "online",
                                                 self.address, self.hc, ts),
            "arti_key_pem": base64.b64encode(pem).decode(),
        }
        data.update(extra)
        handler = _Handler(data)
        handler._handle_online()
        self.assertEqual(len(handler.replies), 1)
        return handler.replies[0]

    def _stored(self):
        with open(self.key_path, "rb") as f:
            return f.read()

    def test_a_good_key_is_stored_once(self):
        pem = _pem(self.expanded, self.public)
        status, body = self._online(pem)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["arti_key_stored"])
        self.assertEqual(self._stored(), pem)
        st = os.stat(self.key_path)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        with open(os.path.join(self.keys, self.address, "hostname")) as f:
            self.assertEqual(f.read(), self.address + "\n")

        # Every heartbeat carries the key; the same bytes are not rewritten.
        self.assertEqual(self._online(pem)[0], 200)
        again = os.stat(self.key_path)
        self.assertEqual((again.st_ino, again.st_mtime_ns), (st.st_ino, st.st_mtime_ns))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.key_path))),
                         ["hostname", "ks_hs_id.ed25519_expanded_private"])

    def test_the_stored_version_is_a_short_printable_string(self):
        """Issue #467: the body is 1 MB and version went into the row as
        sent, so 5000 x 4 rows x 1 MB of registry.db was a client's to
        fill; the signed status answer echoes it too."""
        pem = _pem(self.expanded, self.public)
        cases = (("v" * 100_000 + "\n\x00end", "v" * oc.SHORT_TEXT_LIMIT),
                 ({"a": ["b"] * 1000}, "unknown"),
                 ("", "unknown"),
                 (None, "unknown"),
                 ("2.6.0", "2.6.0"))
        for sent, stored in cases:
            with self.subTest(sent=str(sent)[:20]):
                status, body = self._online(pem, version=sent)
                self.assertEqual(status, 200, body)
                conn = sqlite3.connect(oc.DB_PATH)
                row = conn.execute("SELECT version FROM registry WHERE content_address = ?",
                                   (self.address,)).fetchone()
                conn.close()
                self.assertEqual(row[0], stored if stored != "unknown" else row[0])
                self.assertLessEqual(len(row[0]), oc.SHORT_TEXT_LIMIT)
        # "unknown" keeps the version a heartbeat recorded before.
        self.assertEqual(self._online(pem, version="2.6.1")[0], 200)
        self.assertEqual(self._online(pem, version="")[0], 200)
        conn = sqlite3.connect(oc.DB_PATH)
        self.assertEqual(conn.execute("SELECT version FROM registry WHERE content_address = ?",
                                      (self.address,)).fetchone()[0], "2.6.1")
        conn.close()

    def test_a_doctored_key_never_replaces_a_good_one(self):
        good = _pem(self.expanded, self.public)
        self.assertEqual(self._online(good)[0], 200)
        other_expanded, other_public, _ = _keypair()
        for doctored in (_pem(other_expanded, self.public),
                         _pem(other_expanded, self.public, inner_public=other_public)):
            status, body = self._online(doctored)
            self.assertEqual(status, 400)
            self.assertIn("does not belong", body["error"])
            self.assertEqual(self._stored(), good)

    def test_a_doctored_first_key_is_not_stored(self):
        other, _, _ = _keypair()
        self.assertEqual(self._online(_pem(other, self.public))[0], 400)
        self.assertFalse(os.path.exists(self.key_path))

    def test_a_key_change_is_written_in_one_step(self):
        """A changed key (a new PEM for the same key, e.g. new check ints)
        goes through write_key_file: rename, not rewrite."""
        self.assertEqual(self._online(_pem(self.expanded, self.public))[0], 200)
        before = os.stat(self.key_path).st_ino
        changed = _pem(self.expanded, self.public, check2=None).replace(b"\n", b"\r\n")
        self.assertEqual(self._online(changed)[0], 200)
        self.assertEqual(self._stored(), changed)
        self.assertNotEqual(os.stat(self.key_path).st_ino, before)

    def test_the_handler_writes_keys_only_through_write_key_file(self):
        with open(os.path.join(DOCKER_TOR_DIR, "web-server.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("    def _handle_online(self):"):
                   src.index("    # -- POST /offline")]
        self.assertNotIn("open(", body)
        self.assertIn("write_key_file(pem_path, arti_pem)", body)
        self.assertLess(body.index("private_key_problem(arti_pem, public_key)"),
                        body.index("write_key_file(pem_path"))


# ---------------------------------------------------------------------------
# write_key_file
# ---------------------------------------------------------------------------

class WriteKeyFileTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-write-key-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "ks_hs_id.ed25519_expanded_private")

    def _names(self):
        return sorted(os.listdir(self.dir))

    def test_new_file(self):
        self.assertTrue(oc.write_key_file(self.path, b"key"))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"key")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self._names(), [os.path.basename(self.path)])

    def test_identical_bytes_are_left_alone_but_made_private(self):
        with open(self.path, "wb") as f:
            f.write(b"key")
        os.chmod(self.path, 0o644)
        ino = os.stat(self.path).st_ino
        self.assertFalse(oc.write_key_file(self.path, b"key"))
        self.assertEqual(os.stat(self.path).st_ino, ino)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_planted_link_is_replaced_not_followed(self):
        target = os.path.join(self.dir, "elsewhere")
        with open(target, "wb") as f:
            f.write(b"key")
        os.symlink(target, self.path)
        self.assertTrue(oc.write_key_file(self.path, b"key"))
        self.assertFalse(os.path.islink(self.path))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"key")
        os.unlink(self.path)
        os.symlink(target, self.path)
        oc.write_key_file(self.path, b"new")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"key")

    def test_a_failed_write_leaves_the_old_key(self):
        """Full disk, or a crash: the key in place stays whole."""
        with open(self.path, "wb") as f:
            f.write(b"old key")
        for failing in ("fsync", "rename"):
            with self.subTest(failing=failing), \
                    mock.patch.object(oc.os, failing, side_effect=OSError(28, "No space")):
                with self.assertRaises(OSError):
                    oc.write_key_file(self.path, b"new key")
            with open(self.path, "rb") as f:
                self.assertEqual(f.read(), b"old key")
            self.assertEqual(self._names(), [os.path.basename(self.path)])

    def test_temp_files_a_crash_left_are_cleaned_up_later(self):
        name = os.path.basename(self.path)
        old = os.path.join(self.dir, f".{name}.deadbeef.tmp")
        young = os.path.join(self.dir, f".{name}.cafef00d.tmp")
        for p in (old, young):
            with open(p, "wb") as f:
                f.write(b"partial")
        past = os.stat(old).st_mtime - oc._STALE_TEMP_SECONDS - 60
        os.utime(old, (past, past))
        oc.write_key_file(self.path, b"key")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young))  # may be a concurrent /online's


# ---------------------------------------------------------------------------
# The other key writers in the image
# ---------------------------------------------------------------------------

class KeyConvertWritesTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-key-convert-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.expanded, self.public = os.urandom(64), os.urandom(32)
        self.pem = os.path.join(self.dir, "in.pem")
        with open(self.pem, "wb") as f:
            f.write(_pem(self.expanded, self.public))
        self.out = os.path.join(self.dir, "hs")

    def test_arti_to_ctor(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            kc.arti_to_ctor(self.pem, self.out)
        with open(os.path.join(self.out, "hs_ed25519_secret_key"), "rb") as f:
            self.assertEqual(f.read(), kc.CTOR_SECRET_HEADER + self.expanded)
        with open(os.path.join(self.out, "hs_ed25519_public_key"), "rb") as f:
            self.assertEqual(f.read(), kc.CTOR_PUBLIC_HEADER + self.public)
        with open(os.path.join(self.out, "hostname")) as f:
            self.assertEqual(f.read(), kc._onion_address(self.public) + ".onion\n")
        for name in os.listdir(self.out):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode),
                             0o644 if name == "hostname" else 0o600, name)
        self.assertEqual(sorted(os.listdir(self.out)),
                         ["hostname", "hs_ed25519_public_key", "hs_ed25519_secret_key"])

    def test_no_short_secret_key_after_a_failure(self):
        """The entrypoint converts only while the secret key is missing: a
        failure must leave it missing, never short."""
        real_rename = os.rename

        def rename(src, dst):
            if dst.endswith("hs_ed25519_secret_key"):
                raise OSError(28, "No space left on device")
            return real_rename(src, dst)
        with mock.patch.object(kc.os, "rename", side_effect=rename), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(OSError):
                kc.arti_to_ctor(self.pem, self.out)
        self.assertEqual(os.listdir(self.out), ["hs_ed25519_public_key"])

    def test_ctor_to_arti(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            kc.arti_to_ctor(self.pem, self.out)
            out_pem = os.path.join(self.dir, "out.pem")
            kc.ctor_to_arti(os.path.join(self.out, "hs_ed25519_secret_key"), out_pem)
        self.assertEqual(kc.parse_arti_pem(out_pem), (self.expanded, self.public))
        self.assertEqual(stat.S_IMODE(os.stat(out_pem).st_mode), 0o600)

    def test_no_in_place_key_writes_left(self):
        with open(os.path.join(DOCKER_TOR_DIR, "key-convert.py"), encoding="utf-8") as f:
            src = f.read()
        # The builtin open() in a write mode; os.fdopen on _write_atomic's
        # O_EXCL descriptor is the one way key files are written.
        found = re.search(r"(?<![\w.])open\([^)]*,\s*[\"'][wa]", src)
        self.assertIsNone(found, found and found.group(0))


class WatchdogGeneratedKeyTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-watchdog-key-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.object(wd, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = os.urandom(64)
        self.sid = "a" * 56
        self.resp = (f"250-ServiceID={self.sid}\r\n250-PrivateKey=ED25519-V3:"
                     f"{base64.b64encode(self.key).decode()}\r\n250 OK\r\n")

    def test_saved_whole(self):
        sid, key_b64 = wd._parse_generated_key(self.resp)
        self.assertEqual(sid, self.sid)
        self.assertTrue(wd._save_generated_key(self.dir, sid, key_b64))
        with open(os.path.join(self.dir, "hs_ed25519_secret_key"), "rb") as f:
            self.assertEqual(f.read(), b"== ed25519v1-secret: type0 ==\x00\x00\x00" + self.key)
        with open(os.path.join(self.dir, "hostname")) as f:
            self.assertEqual(f.read(), self.sid + ".onion\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["hostname", "hs_ed25519_secret_key"])

    def test_a_failed_save_leaves_no_short_key(self):
        path = os.path.join(self.dir, "hs_ed25519_secret_key")
        with open(path, "wb") as f:
            f.write(b"x" * 96)
        with mock.patch.object(wd.os, "fsync", side_effect=OSError(28, "No space")):
            self.assertFalse(wd._save_generated_key(
                self.dir, *wd._parse_generated_key(self.resp)))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"x" * 96)
        self.assertEqual(os.listdir(self.dir), ["hs_ed25519_secret_key"])


if __name__ == "__main__":
    unittest.main()
