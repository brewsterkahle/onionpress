#!/usr/bin/env python3
"""Tests for onionpress.key_manager.extract_keys — focused on the
Arti/C Tor dual-path fallback that this test suite was born to guard.

Before the fix, extract_keys() hardcoded the Arti keystore path. Every
install that runs C Tor (the default since 2026-03-16, commit 5d91cb3e)
hit "No such file or directory" forever and OnionHeaven registration
could never succeed. These tests cover all three extraction paths and
failure reporting so a regression would fail loudly.
"""

import base64
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import key_manager


def _fresh_keypair():
    """Produce a matched (expanded_64_private, public_32, onion_address).

    We reuse key_manager.build_openssh_key for the Arti side, so we need a
    valid expanded/public pair. Ed25519's "expanded" private is the
    clamped scalar (32) + prefix (32); for purely synthetic test data we
    generate a random 64-byte blob and derive the public key such that
    sign/verify won't actually work — but neither the extractor nor the
    parser care. To keep the tests realistic AND let us exercise the
    hostname-derived pubkey path, we cheat by feeding a known public key
    and matching onion address in as fixtures instead.
    """
    # Random but deterministic for test stability.
    expanded = bytes(range(64))
    public = bytes([(x * 7) & 0xFF for x in range(32)])
    checksum = hashlib.sha3_256(b".onion checksum" + public + b"\x03").digest()[:2]
    addr = base64.b32encode(public + checksum + b"\x03").decode().lower() + ".onion"
    return expanded, public, addr


def _mock_run_factory(files):
    """Return a callable that stands in for subprocess.run inside
    key_manager._docker_cat. `files` maps container paths to (rc, stdout_bytes)
    — paths not in the map return rc=1 with a cat-style error message.
    """
    class FakeResult:
        def __init__(self, rc, stdout):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = (
                b"" if rc == 0
                else b"cat: %s: No such file or directory" % (stdout or b"")
            )

    def fake_run(argv, **kwargs):
        # argv = ["docker", "exec", CONTAINER, "cat", PATH]
        path = argv[-1]
        if path in files:
            rc, data = files[path]
            return FakeResult(rc, data)
        return FakeResult(1, path.encode())

    return fake_run


class TestExtractKeysArti(unittest.TestCase):
    def test_arti_path_returns_parsed_keys(self):
        expanded, public, _ = _fresh_keypair()
        pem = key_manager.build_openssh_key(expanded, public)

        files = {key_manager.ARTI_KEYSTORE_PATH: (0, pem)}
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            priv, pub = key_manager.extract_keys()

        self.assertEqual(priv, expanded)
        self.assertEqual(pub, public)


class TestExtractKeysCTor(unittest.TestCase):
    """C Tor mode: the Arti keystore does not exist. Identity lives at
    /var/lib/tor/hidden_service/wordpress/{hs_ed25519_secret_key,hostname}.
    """

    def test_uses_public_key_file_when_present(self):
        expanded, public, addr = _fresh_keypair()
        sec_blob = key_manager.CTOR_SECRET_HEADER + expanded
        pub_blob = key_manager.CTOR_PUBLIC_HEADER + public

        files = {
            # Arti missing
            key_manager.CTOR_SECRET_PATH: (0, sec_blob),
            key_manager.CTOR_PUBLIC_PATH: (0, pub_blob),
        }
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            priv, pub = key_manager.extract_keys()

        self.assertEqual(priv, expanded)
        self.assertEqual(pub, public)

    def test_derives_public_key_from_hostname_when_pub_file_missing(self):
        """Newer C Tor installs only keep hs_ed25519_secret_key + hostname
        (no hs_ed25519_public_key). We MUST recover the public key from
        the hostname — that's the real shape of fresh installs today.
        """
        expanded, public, addr = _fresh_keypair()
        sec_blob = key_manager.CTOR_SECRET_HEADER + expanded

        files = {
            # Arti missing, public key file missing
            key_manager.CTOR_SECRET_PATH: (0, sec_blob),
            key_manager.CTOR_HOSTNAME_PATH: (0, addr.encode() + b"\n"),
        }
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            priv, pub = key_manager.extract_keys()

        self.assertEqual(priv, expanded)
        self.assertEqual(
            pub, public,
            "pubkey recovered from hostname must match the one that "
            "generated the onion address",
        )

    def test_trailing_whitespace_in_hostname_is_tolerated(self):
        _, public, addr = _fresh_keypair()
        sec_blob = key_manager.CTOR_SECRET_HEADER + bytes(64)
        # Real hostname files have a trailing newline; mixed case should
        # work too since the .onion address canonicalises to lowercase.
        files = {
            key_manager.CTOR_SECRET_PATH: (0, sec_blob),
            key_manager.CTOR_HOSTNAME_PATH: (0, (addr.upper() + "\n\r").encode()),
        }
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            _, pub = key_manager.extract_keys()
        self.assertEqual(pub, public)


class TestRefreshPaths(unittest.TestCase):
    """extract_keys()/write_private_key() must re-read SITE_TYPE on every
    call, not just at module import — the menubar is a long-lived process
    and SITE_TYPE is written mid-session by first-run setup, after
    key_manager was already imported. A module-load-time-only nickname
    would leave the whole first session reading hss/wordpress paths on a
    freshly-chosen static install."""

    def setUp(self):
        self.tmp_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_home, ignore_errors=True)
        self.data_dir = os.path.join(self.tmp_home, ".onionpress")
        os.makedirs(self.data_dir)
        # os.path.expanduser("~") reads $HOME on POSIX — scope it to a
        # throwaway dir instead of monkeypatching the shared os.path module.
        self._patch = mock.patch.dict(os.environ, {"HOME": self.tmp_home})
        self._patch.start()
        # addCleanup runs LIFO: register _refresh_paths first so it's the
        # SECOND (later) callback to run, after the HOME patch is already
        # stopped — otherwise it would re-read the tmp_home config (which
        # may say static) instead of the real environment, leaving
        # BACKEND_NICKNAME pointing at "site" for every later test in
        # this file that reads key_manager.CTOR_SECRET_PATH etc. at call time.
        self.addCleanup(key_manager._refresh_paths)
        self.addCleanup(self._patch.stop)

    def _write_site_type(self, value):
        with open(os.path.join(self.data_dir, "config"), "w") as f:
            f.write(f"SITE_TYPE={value}\n")

    def test_defaults_to_wordpress_with_no_config(self):
        key_manager._refresh_paths()
        self.assertEqual(key_manager.BACKEND_NICKNAME, "wordpress")

    def test_picks_up_static_written_after_import(self):
        # Simulates: module already imported (constants frozen at
        # "wordpress"), then the welcome screen writes SITE_TYPE=static
        # mid-session, then a key operation runs.
        self.assertEqual(key_manager.BACKEND_NICKNAME, "wordpress")
        self._write_site_type("static")
        key_manager._refresh_paths()
        self.assertEqual(key_manager.BACKEND_NICKNAME, "site")
        self.assertIn("/hidden_service/site/", key_manager.CTOR_SECRET_PATH)
        self.assertIn("/hidden_service/site/", key_manager.CTOR_HOSTNAME_PATH)
        self.assertIn("/keystore/hss/site/", key_manager.ARTI_KEYSTORE_PATH)

    def test_extract_keys_uses_refreshed_nickname(self):
        self._write_site_type("static")
        expanded, public, _ = _fresh_keypair()
        pem = key_manager.build_openssh_key(expanded, public)

        # Keyed by the path AFTER refresh (i.e. the "site" nickname) —
        # extract_keys() must call _refresh_paths() before looking this up.
        key_manager._refresh_paths()
        files = {key_manager.ARTI_KEYSTORE_PATH: (0, pem)}
        self.assertIn("hss/site/", key_manager.ARTI_KEYSTORE_PATH)

        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            priv, pub = key_manager.extract_keys()
        self.assertEqual(priv, expanded)
        self.assertEqual(pub, public)


class TestExtractKeysErrors(unittest.TestCase):
    def test_no_keys_present_raises_with_informative_message(self):
        """When neither Arti nor C Tor paths exist, the error should tell
        the user something more useful than "unknown"; specifically it
        should mention the missing key file path since that's the fastest
        thing to investigate.
        """
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory({})):
            with self.assertRaises(Exception) as cm:
                key_manager.extract_keys()
        msg = str(cm.exception)
        self.assertIn("Failed to extract keys", msg)
        self.assertIn("Could not read key file", msg)

    def test_ctor_secret_corrupt_raises_specific_error(self):
        """A truncated or wrong-header C Tor secret should surface the
        reason, not silently fall back."""
        files = {
            key_manager.CTOR_SECRET_PATH: (0, b"not-a-key" * 10),
        }
        with mock.patch.object(key_manager.subprocess, "run",
                               side_effect=_mock_run_factory(files)):
            with self.assertRaises(Exception) as cm:
                key_manager.extract_keys()
        self.assertIn("C Tor secret key", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
