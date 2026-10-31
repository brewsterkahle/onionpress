#!/usr/bin/env python3
"""onionpress.key_manager's on-disk key formats and its command line.

What is held here:

- read_ctor_keys strips C Tor's 32-byte headers and refuses a truncated
  file rather than hand back a short key;
- ensure_key_formats derives whichever of C Tor's pair and the delivered
  PEM is missing from the other, byte for byte the same key (the key is
  the install's identity: a conversion that changed it would be a new
  address), changes nothing when both are there, and raises when there
  is no key at all (never invents one);
- the `python -m onionpress.key_manager` command line the launchers call:
  usage on a missing argument (exit 1), the key read from stdin with `-`
  (never argv), the conversions, and errors as exit 1 with the reason.

Real keys (libsodium via PyNaCl, as the app's), temp folders, no docker.
"""

import contextlib
import hashlib
import io
import os
import runpy
import shutil
import stat
import sys
import tempfile
import unittest
import warnings
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import key_manager  # noqa: E402

try:
    import nacl.signing  # noqa: F401
    HAVE_NACL = True
except ImportError:  # pragma: no cover - PyNaCl is a test dependency
    HAVE_NACL = False


def _keypair(seed=b"\x07" * 32):
    """(expanded_64, public_32, address) for *seed*, as C Tor expands it."""
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    private = bytes(h)
    public = key_manager.derive_public_key(private)
    return private, public, key_manager.derive_onion_address(public)


SECRET = "hs_ed25519_secret_key"
PUBLIC = "hs_ed25519_public_key"
PEM = "ks_hs_id.ed25519_expanded_private"


class _KeyDirCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-keyfmt-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.private, self.public, self.address = _keypair()

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as f:
            f.write(data)

    def read(self, name):
        with open(self.path(name), "rb") as f:
            return f.read()


class TestReadCtorKeys(_KeyDirCase):

    def test_strips_the_headers(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        private, public = key_manager.read_ctor_keys(self.dir)
        self.assertEqual(private, self.private)
        self.assertEqual(public, self.public)
        self.assertEqual(self.read(SECRET)[:32], key_manager.CTOR_HEADER_SECRET)

    def test_a_short_secret_is_refused(self):
        self.write(SECRET, key_manager.CTOR_HEADER_SECRET + b"x" * 10)
        self.write(PUBLIC, key_manager.CTOR_HEADER_PUBLIC + self.public)
        with self.assertRaisesRegex(ValueError, "Secret key file too short"):
            key_manager.read_ctor_keys(self.dir)

    def test_a_short_public_key_is_refused(self):
        self.write(SECRET, key_manager.CTOR_HEADER_SECRET + self.private)
        self.write(PUBLIC, b"short")
        with self.assertRaisesRegex(ValueError, "Public key file too short"):
            key_manager.read_ctor_keys(self.dir)

    def test_a_missing_file_raises(self):
        with self.assertRaises(OSError):
            key_manager.read_ctor_keys(self.dir)


class TestEnsureKeyFormats(_KeyDirCase):

    def test_pem_derived_from_ctor_is_the_same_key(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        self.assertTrue(key_manager.ensure_key_formats(self.dir))
        private, public = key_manager.read_arti_key(self.dir)
        self.assertEqual((private, public), (self.private, self.public))
        self.assertEqual(stat.S_IMODE(os.stat(self.path(PEM)).st_mode), 0o600)
        self.assertEqual(key_manager.staged_key_address(self.dir), self.address)

    def test_ctor_derived_from_pem_is_the_same_key(self):
        key_manager.write_arti_key(self.dir, self.private, self.public)
        self.assertTrue(key_manager.ensure_key_formats(self.dir))
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))
        self.assertEqual(stat.S_IMODE(os.stat(self.path(SECRET)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path(PUBLIC)).st_mode), 0o644)

    def test_both_present_changes_nothing(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        key_manager.write_arti_key(self.dir, self.private, self.public)
        before = {n: self.read(n) for n in (SECRET, PUBLIC, PEM)}
        self.assertFalse(key_manager.ensure_key_formats(self.dir))
        self.assertEqual({n: self.read(n) for n in before}, before)

    def test_half_a_ctor_pair_with_a_pem_rewrites_the_pair_from_the_pem(self):
        key_manager.write_arti_key(self.dir, self.private, self.public)
        self.write(SECRET, b"stale")   # no public file: not a C Tor pair
        self.assertTrue(key_manager.ensure_key_formats(self.dir))
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))

    def test_no_key_at_all_raises_and_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "No key files found"):
            key_manager.ensure_key_formats(self.dir)
        self.assertEqual(os.listdir(self.dir), [])


class TestCommandLine(_KeyDirCase):
    """`python -m onionpress.key_manager …` (the launchers' entry), run
    in-process through runpy with argv and stdin replaced."""

    def main(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["key_manager.py", *argv]), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                runpy.run_module("onionpress.key_manager", run_name="__main__")
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def test_no_command_prints_usage_and_fails(self):
        code, out, _ = self.main()
        self.assertEqual(code, 1)
        self.assertIn("Usage:", out)
        self.assertIn("ensure-formats <key_dir>", out)

    def test_an_unknown_command_prints_usage(self):
        code, out, _ = self.main("frobnicate")
        self.assertEqual(code, 1)
        self.assertIn("Usage:", out)

    def test_every_command_without_its_argument_prints_usage(self):
        for cmd in ("ensure-formats", "ctor-to-arti", "arti-to-ctor", "write-keys",
                    "import-key", "decode-key", "encode-key-file", "staged-address",
                    "keep-current"):
            with self.subTest(cmd=cmd):
                code, out, _ = self.main(cmd)
                self.assertEqual(code, 1)
                self.assertIn("Usage:", out)

    def test_ensure_formats(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        code, out, _ = self.main("ensure-formats", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("Converted key formats", out)
        code, out, _ = self.main("ensure-formats", self.dir)
        self.assertIn("Both formats already present", out)

    def test_ensure_formats_on_an_empty_folder_is_an_error(self):
        code, _, err = self.main("ensure-formats", self.dir)
        self.assertEqual(code, 1)
        self.assertIn("Error: No key files found", err)

    def test_ctor_to_arti_and_back(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        self.assertEqual(self.main("ctor-to-arti", self.dir)[0], 0)
        os.remove(self.path(SECRET))
        os.remove(self.path(PUBLIC))
        code, out, _ = self.main("arti-to-ctor", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("Wrote C Tor keys", out)
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))

    def test_write_keys_writes_both_formats(self):
        code, _, _ = self.main("write-keys", self.private.hex(), self.public.hex(), self.dir)
        self.assertEqual(code, 0)
        self.assertEqual(key_manager.read_arti_key(self.dir), (self.private, self.public))
        self.assertEqual(key_manager.read_ctor_keys(self.dir), (self.private, self.public))

    @unittest.skipUnless(HAVE_NACL, "needs PyNaCl")
    def test_decode_key_reads_the_key_from_stdin(self):
        import base64
        b32 = base64.b32encode(self.private).decode()
        code, out, _ = self.main("decode-key", "-", stdin=b32 + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), self.address)

    @unittest.skipUnless(HAVE_NACL, "needs PyNaCl")
    def test_import_key_from_stdin_stages_the_address(self):
        import base64
        out_dir = os.path.join(self.dir, "staged")
        os.makedirs(out_dir)
        b32 = base64.b32encode(self.private).decode()
        code, out, err = self.main("import-key", "-", out_dir, stdin=b32)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), self.address)
        addr_dir = os.path.join(out_dir, self.address)
        self.assertEqual(self.main("staged-address", addr_dir)[1].strip(), self.address)
        with open(os.path.join(addr_dir, "hostname")) as f:
            self.assertEqual(f.read(), self.address + "\n")

    def test_encode_key_file(self):
        key_manager.write_ctor_keys(self.dir, self.private, self.public)
        code, out, err = self.main("encode-key-file", self.path(SECRET))
        self.assertEqual(code, 0, err)
        self.assertEqual(key_manager.decode_base32_key(out.strip()), self.private)

    def test_keep_current_exit_codes(self):
        root = os.path.join(self.dir, "replaced")
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        code, out, _ = self.main("keep-current", "--no-served", root, empty)
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), "")
        key_dir = os.path.join(self.dir, "key")
        os.makedirs(key_dir)
        key_manager.write_ctor_keys(key_dir, self.private, self.public)
        code, out, _ = self.main("keep-current", "--no-served", root, key_dir)
        self.assertEqual(code, 0)
        kept = out.strip().splitlines()
        self.assertEqual(len(kept), 1)
        self.assertIn(self.address.removesuffix(".onion"), os.path.basename(kept[0]))

    def test_keep_current_needs_a_folder(self):
        code, out, _ = self.main("keep-current", "--no-served")
        self.assertEqual(code, 1)
        self.assertIn("Usage:", out)

    def test_extract_reports_a_missing_container_as_an_error(self):
        class _R:
            returncode = 1
            stdout = b""
            stderr = b"Error: No such container: onionpress-tor"
        with mock.patch("subprocess.run", return_value=_R()):
            code, _, err = self.main("extract")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)


if __name__ == "__main__":
    unittest.main()
