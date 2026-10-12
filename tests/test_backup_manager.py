#!/usr/bin/env python3
"""Tests for onionpress.backup module (formerly backup_manager).

Backups come in two formats: format 1 is a ZipCrypto zip from `zip -P`
(made through 2.5.2, must restore forever), format 2 is the encrypted
container from onionpress.backup_crypto (made from then on). Fixtures built
with `zip -P` below are format 1 on purpose.
"""

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from unittest import mock

# Add src/ to path so we can import both onionpress.backup and key_manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from onionpress import backup as backup_manager
from onionpress import backup_crypto
from onionpress import key_manager

_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


_FAKE_PUB = b"\x02" * 32
_FAKE_PRIV = b"\x01" * 64
# The .onion address that derives from _FAKE_PUB. create_backup and
# restore_from_backup both treat the key as authoritative and write this
# derived value into metadata regardless of any address the caller (or an
# older backup's metadata) provides. Fixtures that need a matching pair
# should use this constant.
_FAKE_DERIVED_ADDR = key_manager.derive_onion_address(_FAKE_PUB)


def _fake_arti_pem():
    """A deterministic, parseable OpenSSH PEM for test fixtures.

    The key bytes are arbitrary (not a real ed25519 pair) — build/parse is
    byte-level, not crypto. Tests round-trip these through backup+restore.
    """
    return key_manager.build_openssh_key(_FAKE_PRIV, _FAKE_PUB)


def _cheap_scrypt(test):
    """Write format-2 backups with scrypt N=2^10 for the rest of *test*.

    Readers take N from the header, so this only makes the tests fast; the
    shipped parameters are pinned by TestEncryptedBackupDefaults.
    """
    patcher = mock.patch.object(backup_crypto, "SCRYPT_LOG2_N", 10)
    patcher.start()
    test.addCleanup(patcher.stop)


def _inner_zip(test, zip_path, password):
    """Decrypt a format-2 backup; return its inner zip as an open ZipFile."""
    fd, inner = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    test.addCleanup(os.unlink, inner)
    backup_crypto.decrypt_archive(zip_path, password, inner)
    zf = zipfile.ZipFile(inner)
    test.addCleanup(zf.close)
    return zf


class TestBackupFilename(unittest.TestCase):
    """Test backup_filename() generation."""

    def test_basic_filename(self):
        name = backup_manager.backup_filename("abc12345xyz.onion", "admin")
        self.assertTrue(name.startswith("OnionPress-abc12345-admin-"))
        self.assertTrue(name.endswith(".zip"))

    def test_strips_onion_suffix(self):
        name = backup_manager.backup_filename("abcdefgh.onion", "user1")
        self.assertNotIn(".onion", name)

    def test_truncates_long_address(self):
        long_addr = "abcdefghijklmnopqrstuvwxyz1234567890abcdefghijklmnopqrstuv.onion"
        name = backup_manager.backup_filename(long_addr, "admin")
        # Should only use first 8 chars of the address
        self.assertIn("OnionPress-abcdefgh-admin-", name)

    def test_none_address(self):
        name = backup_manager.backup_filename(None, "admin")
        self.assertIn("unknown", name)

    def test_timestamp_format(self):
        name = backup_manager.backup_filename("test1234.onion", "admin")
        # Filename: OnionPress-test1234-admin-YYYY-MM-DD-HH-MM.zip
        parts = name.replace("OnionPress-test1234-admin-", "").replace(".zip", "")
        segments = parts.split("-")
        self.assertEqual(len(segments), 5)  # YYYY, MM, DD, HH, MM


class TestReadBackupMetadata(unittest.TestCase):
    """Test read_backup_metadata() with real format-1 (ZipCrypto) zip files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_zip(self, metadata, password, metadata_name="metadata.json"):
        """Helper: create a password-protected zip with metadata.json."""
        zip_path = os.path.join(self.tmpdir, "test.zip")
        staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(staging)

        with open(os.path.join(staging, "metadata.json"), "w") as f:
            json.dump(metadata, f)

        # Use system zip for password-protected archives (Python zipfile
        # can read ZipCrypt but not write it)
        subprocess.run(
            ["zip", "-r", "-P", password, zip_path, "."],
            cwd=staging, capture_output=True, check=True
        )
        shutil.rmtree(staging)
        return zip_path

    def test_valid_backup(self):
        metadata = {
            "onion_address": "abc123.onion",
            "backup_date": "2026-01-15T10:30:00Z",
            "onionpress_version": "2.2.84",
            "username": "admin",
        }
        zip_path = self._make_zip(metadata, "secret123")
        result = backup_manager.read_backup_metadata(zip_path, "secret123")
        self.assertEqual(result["onion_address"], "abc123.onion")
        self.assertEqual(result["username"], "admin")
        self.assertEqual(result["onionpress_version"], "2.2.84")

    def test_wrong_password(self):
        metadata = {"onion_address": "test.onion"}
        zip_path = self._make_zip(metadata, "correct")
        with self.assertRaises(ValueError) as ctx:
            backup_manager.read_backup_metadata(zip_path, "wrong")
        self.assertIn("password", str(ctx.exception).lower())

    # ZipCrypto checks only one byte of the decryption header, so about 1 in
    # 256 wrong passwords gets past it and the garbage plaintext blows up in
    # zlib (or the CRC check) instead of raising "Bad password". That made
    # test_wrong_password flaky, and showed users a zlib.error crash instead
    # of the wrong-password message. The next three tests pin the fix.

    def test_wrong_password_slipping_past_header_check(self):
        """End to end: a real wrong password that passes the one-byte check."""
        zip_path = self._make_zip({"onion_address": "test.onion"}, "correct")
        wrong = self._wrong_password_passing_header_check(zip_path)
        with self.assertRaises(ValueError) as ctx:
            backup_manager.read_backup_metadata(zip_path, wrong)
        self.assertIn("password", str(ctx.exception).lower())

    def _wrong_password_passing_header_check(self, zip_path):
        """Brute-force a wrong password that ZipCrypto's header check accepts.

        Deterministic in outcome: each candidate has ~1/256 odds and an
        attempt costs ~20us, so this finds one within a few ms. Hitting the
        20000-try cap has probability (255/256)**20000, about 1e-34.
        """
        with zipfile.ZipFile(zip_path) as zf:
            name = next(n for n in zf.namelist()
                        if n.rstrip('/').endswith('metadata.json'))
            for i in range(20000):
                candidate = f"wrong{i}"
                try:
                    zf.read(name, pwd=candidate.encode())
                except RuntimeError:
                    continue  # failed the header check: the common case
                except Exception:
                    return candidate  # passed it, then choked on garbage
                self.fail(f"wrong password {candidate!r} decrypted the member")
        self.fail("no wrong password slipped past the header check")

    def test_garbage_plaintext_errors_mean_wrong_password(self):
        """Deterministic: zlib.error or a CRC mismatch from the encrypted read."""
        zip_path = self._make_zip({"onion_address": "test.onion"}, "correct")
        garbage_errors = [
            zlib.error("Error -3 while decompressing data: invalid code lengths set"),
            zipfile.BadZipFile("Bad CRC-32 for file 'metadata.json'"),
        ]
        for err in garbage_errors:
            with self.subTest(error=type(err).__name__):
                with mock.patch.object(zipfile.ZipFile, "read", side_effect=err):
                    with self.assertRaises(ValueError) as ctx:
                        backup_manager.read_backup_metadata(zip_path, "wrong")
                self.assertIn("password", str(ctx.exception).lower())

    def test_garbage_plaintext_on_unencrypted_zip_is_not_a_password_error(self):
        """Only a password-protected read is reinterpreted: a corrupt
        unencrypted zip keeps its original error."""
        zip_path = os.path.join(self.tmpdir, "plain.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("metadata.json", json.dumps({"onion_address": "x.onion"}))
        with mock.patch.object(zipfile.ZipFile, "read",
                               side_effect=zlib.error("Error -3 while decompressing data")):
            with self.assertRaises(zlib.error):
                backup_manager.read_backup_metadata(zip_path, "irrelevant")

    def test_missing_metadata(self):
        """Zip without metadata.json should raise ValueError."""
        zip_path = os.path.join(self.tmpdir, "empty.zip")
        staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(staging)
        with open(os.path.join(staging, "other.txt"), "w") as f:
            f.write("not metadata")
        subprocess.run(
            ["zip", "-r", "-P", "pass", zip_path, "."],
            cwd=staging, capture_output=True, check=True
        )
        shutil.rmtree(staging)

        with self.assertRaises(ValueError) as ctx:
            backup_manager.read_backup_metadata(zip_path, "pass")
        self.assertIn("no metadata.json", str(ctx.exception))

    def test_not_a_zip(self):
        """Non-zip file should raise ValueError."""
        bad_path = os.path.join(self.tmpdir, "notazip.zip")
        with open(bad_path, "w") as f:
            f.write("this is not a zip file")
        with self.assertRaises(ValueError) as ctx:
            backup_manager.read_backup_metadata(bad_path, "pass")
        self.assertIn("Not a valid zip", str(ctx.exception))

    def test_dot_slash_prefix(self):
        """Metadata at ./metadata.json (as produced by `zip -r ... .`) should be found."""
        metadata = {"onion_address": "dotslash.onion", "username": "admin"}
        zip_path = self._make_zip(metadata, "pw")
        # Verify it actually has ./ prefix (system zip does this)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        has_dot_prefix = any(n == "./metadata.json" for n in names)
        has_plain = any(n == "metadata.json" for n in names)
        self.assertTrue(has_dot_prefix or has_plain,
                        f"Expected metadata.json in zip, got: {names}")
        # Either way, read_backup_metadata should find it
        result = backup_manager.read_backup_metadata(zip_path, "pw")
        self.assertEqual(result["onion_address"], "dotslash.onion")


class TestFindDir(unittest.TestCase):
    """Test _find_dir() helper."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_direct_path(self):
        os.makedirs(os.path.join(self.tmpdir, "tor-keys"))
        result = backup_manager._find_dir(self.tmpdir, "tor-keys")
        self.assertTrue(os.path.isdir(result))
        self.assertTrue(result.endswith("tor-keys"))

    def test_missing_returns_expected_path(self):
        """When dir doesn't exist, return the expected path anyway."""
        result = backup_manager._find_dir(self.tmpdir, "nonexistent")
        self.assertEqual(result, os.path.join(self.tmpdir, "nonexistent"))


class TestCreateBackupZipStructure(unittest.TestCase):
    """Test that create_backup produces a zip with the expected structure.

    Uses mocked Docker commands via a fake docker script.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_zip = os.path.join(self.tmpdir, "backup.zip")
        self.logs = []

        # Write a valid Arti keystore PEM that the fake docker will cat
        # when key_manager.extract_keys() runs.
        self.pem_path = os.path.join(self.tmpdir, "arti.pem")
        with open(self.pem_path, "wb") as f:
            f.write(_fake_arti_pem())

        # Create a fake docker script that returns test data
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            # Route based on subcommand + args
            f.write(f'if [[ "$1" == "exec" && "$*" == *"ks_hs_id.ed25519_expanded_private"* ]]; then\n')
            f.write(f'    cat "{self.pem_path}"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"wp config get DB_NAME"* ]]; then\n')
            f.write('    echo "wordpress"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"wp config get DB_USER"* ]]; then\n')
            f.write('    echo "wordpress"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"wp config get DB_PASSWORD"* ]]; then\n')
            f.write('    echo "testpass123"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"mariadb-dump"* ]]; then\n')
            f.write('    echo "CREATE TABLE wp_posts; INSERT INTO wp_posts VALUES (1);"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"test -f"* ]]; then\n')
            # OnionHeaven detection probe — not an OnionHeaven install
            f.write('    exit 1\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"tar -cf -"* ]]; then\n')
            # New wp-content backup flow: stream a tar of fake wp-content
            # (matching what the old `docker cp` branch used to fabricate).
            f.write('    fakedir=$(mktemp -d)\n')
            f.write('    mkdir -p "$fakedir/themes" "$fakedir/plugins" "$fakedir/uploads/2024/01"\n')
            f.write('    echo "theme data" > "$fakedir/themes/flavor.css"\n')
            f.write('    echo "plugin data" > "$fakedir/plugins/hello.php"\n')
            # An original with its regenerable sizes + -scaled copy, a true
            # original that merely ends in -WxH, and a video (issue #257).
            f.write('    for n in photo.jpg photo-300x200.jpg photo-scaled.jpg orphan-640x480.jpg clip.mp4; do\n')
            f.write('        echo "$n" > "$fakedir/uploads/2024/01/$n"\n')
            f.write('    done\n')
            f.write('    tar -cf - -C "$fakedir" .\n')
            f.write('    rm -rf "$fakedir"\n')
            f.write('    exit 0\n')
            f.write('elif [[ "$1" == "cp" ]]; then\n')
            # For `docker cp container:/path dest` (restore path still uses cp),
            # create the dest with sample content.
            f.write('    dest="${@: -1}"\n')
            f.write('    mkdir -p "$dest/themes" "$dest/plugins" "$dest/uploads"\n')
            f.write('    echo "theme data" > "$dest/themes/flavor.css"\n')
            f.write('    echo "plugin data" > "$dest/plugins/hello.php"\n')
            f.write('    exit 0\n')
            f.write('fi\n')
            f.write('exit 0\n')
        os.chmod(fake_docker, 0o755)

        # Prepend fake bin to PATH so subprocess finds our fake docker
        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.fake_bin + ":" + self.orig_path

        # Sandbox data_dir — passed explicitly to create_backup so it never
        # touches the real ~/.onionpress/.
        self.data_dir = os.path.join(self.tmpdir, "onionpress-data")
        os.makedirs(self.data_dir)
        _cheap_scrypt(self)

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_zip_structure(self):
        backup_manager.create_backup(
            onion_address="testaddr.onion",
            username="admin",
            password="testpass",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )
        self.assertTrue(os.path.exists(self.output_zip))

        # Format 2: the .zip itself holds only the README and the encrypted
        # payload; the members live in the zip inside the payload.
        with zipfile.ZipFile(self.output_zip, "r") as zf:
            self.assertEqual(sorted(zf.namelist()),
                             [backup_crypto.README_NAME, backup_crypto.PAYLOAD_NAME])
        names = _inner_zip(self, self.output_zip, "testpass").namelist()

        # Normalize ./ prefixes
        names_normalized = [n.lstrip("./") for n in names if n.lstrip("./")]

        self.assertIn("metadata.json", names_normalized)
        self.assertTrue(any("tor-keys/ks_hs_id.ed25519_expanded_private" in n for n in names_normalized))
        self.assertTrue(any("database/wordpress.sql" in n for n in names_normalized))
        self.assertTrue(any("wp-content/themes/" in n for n in names_normalized))
        self.assertTrue(any("wp-content/plugins/" in n for n in names_normalized))

    def test_uploads_carry_originals_only(self):
        """Issue #257: the -WxH sizes and the -scaled copy of an image whose
        original is in the backup are left out (the restore regenerates
        them); a -WxH-named file with no base, video and the rest stay. The
        archive carries the in-container regeneration marker and metadata
        says so."""
        backup_manager.create_backup(
            onion_address=_FAKE_DERIVED_ADDR,
            username="admin",
            password="testpass",
            output_path=self.output_zip,
            version="2.5.2",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )
        names = {n.lstrip("./")
                 for n in _inner_zip(self, self.output_zip, "testpass").namelist()}
        metadata = backup_manager.read_backup_metadata(self.output_zip, "testpass")
        uploads = {n[len("wp-content/uploads/"):] for n in names
                   if n.startswith("wp-content/uploads/") and not n.endswith("/")}
        self.assertEqual(uploads, {
            "2024/01/photo.jpg",
            "2024/01/orphan-640x480.jpg",
            "2024/01/clip.mp4",
            ".onionpress-regenerate-derivatives",
        })
        self.assertTrue(metadata.get("excludes_image_derivatives"))
        self.assertTrue(any("pruned 2 regenerable" in m for m in self.logs), self.logs)
        self.assertTrue(any("BACKUP_PHASE: name=prune_derivatives" in m for m in self.logs))

    def test_metadata_content(self):
        # Pass the address that actually derives from the fake key so the
        # caller-vs-derived agreement check is satisfied.
        backup_manager.create_backup(
            onion_address=_FAKE_DERIVED_ADDR,
            username="admin",
            password="testpass",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )

        data = backup_manager.read_backup_metadata(self.output_zip, "testpass")

        self.assertEqual(data["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertEqual(data["username"], "admin")
        self.assertEqual(data["onionpress_version"], "2.2.84")
        self.assertIn("backup_date", data)
        self.assertEqual(data["backup_format"], backup_crypto.BACKUP_FORMAT)
        # The metadata record and the archive's metadata.json are one file.
        inner = _inner_zip(self, self.output_zip, "testpass")
        self.assertEqual(json.loads(inner.read("metadata.json")), data)
        # No KEY-MISMATCH warning when caller and key agree.
        self.assertFalse(any("KEY-MISMATCH" in m for m in self.logs),
                         f"unexpected KEY-MISMATCH log: {self.logs}")

    def test_metadata_overrides_stale_caller_address(self):
        # Simulates the post-vanity-rotation / post-restore window where
        # self.onion_address (passed in as onion_address=) is still the
        # PRIOR address while the in-container key is already the NEW one.
        # Backup must record the key-derived address so the resulting zip
        # is internally consistent, and log a loud KEY-MISMATCH so the
        # source of stale caller input is greppable in analytics.
        stale_caller_addr = "stale123stale123stale123stale123stale123stale123stale1.onion"
        backup_manager.create_backup(
            onion_address=stale_caller_addr,
            username="admin",
            password="testpass",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )

        data = backup_manager.read_backup_metadata(self.output_zip, "testpass")

        # Derived wins over the stale caller-supplied value.
        self.assertEqual(data["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertNotEqual(data["onion_address"], stale_caller_addr)

        # Loud, structured log line with both addresses for cross-user grep.
        mismatch_logs = [m for m in self.logs if "KEY-MISMATCH" in m]
        self.assertEqual(len(mismatch_logs), 1,
                         f"expected exactly one KEY-MISMATCH log, got "
                         f"{len(mismatch_logs)}: {self.logs}")
        self.assertIn(stale_caller_addr, mismatch_logs[0])
        self.assertIn(_FAKE_DERIVED_ADDR, mismatch_logs[0])

    def test_password_protection(self):
        backup_manager.create_backup(
            onion_address=_FAKE_DERIVED_ADDR,
            username="admin",
            password="secret",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )

        # No member uses zip encryption (ZipCrypto) any more...
        with zipfile.ZipFile(self.output_zip, "r") as zf:
            self.assertFalse([zi.filename for zi in zf.infolist()
                              if zi.flag_bits & 0x1])
        # ...and nothing secret or even identifying is readable in the file:
        # not the key, the SQL, the content, nor the metadata's address.
        with open(self.output_zip, "rb") as f:
            raw = f.read()
        for plaintext in (b"openssh-key-v1", b"OPENSSH PRIVATE KEY",
                          b"CREATE TABLE", b"theme data",
                          _FAKE_DERIVED_ADDR.encode(), b"metadata.json"):
            self.assertNotIn(plaintext, raw)

        with self.assertRaises(ValueError) as ctx:
            backup_manager.read_backup_metadata(self.output_zip, "wrong")
        self.assertIn("password", str(ctx.exception).lower())
        data = backup_manager.read_backup_metadata(self.output_zip, "secret")
        self.assertEqual(data["onion_address"], _FAKE_DERIVED_ADDR)

    def test_failed_zip_leaves_no_backup_behind(self):
        """If `zip` fails partway, the archive's last record is never written
        and no file appears at the output path — not even a partial one."""
        fake_zip = os.path.join(self.fake_bin, "zip")
        with open(fake_zip, "w") as f:
            f.write('#!/bin/bash\nprintf "PK partial zip bytes"\n'
                    'echo "zip I/O error: disk full" >&2\nexit 15\n')
        os.chmod(fake_zip, 0o755)
        with self.assertRaises(Exception) as ctx:
            backup_manager.create_backup(
                onion_address=_FAKE_DERIVED_ADDR, username="admin",
                password="pw", output_path=self.output_zip, version="2.5.2",
                log_func=self.logs.append, data_dir=self.data_dir,
            )
        self.assertIn("zip failed", str(ctx.exception))
        self.assertIn("disk full", str(ctx.exception))
        self.assertFalse(os.path.exists(self.output_zip))
        self.assertFalse([n for n in os.listdir(self.tmpdir) if n.endswith(".partial")])

    def test_backup_file_is_owner_only(self):
        backup_manager.create_backup(
            onion_address=_FAKE_DERIVED_ADDR, username="admin", password="pw",
            output_path=self.output_zip, version="2.5.2",
            log_func=self.logs.append, data_dir=self.data_dir,
        )
        self.assertEqual(os.stat(self.output_zip).st_mode & 0o777, 0o600)

    def test_readme_names_the_version_and_format(self):
        backup_manager.create_backup(
            onion_address=_FAKE_DERIVED_ADDR, username="admin", password="pw",
            output_path=self.output_zip, version="9.8.7",
            log_func=self.logs.append, data_dir=self.data_dir,
        )
        with zipfile.ZipFile(self.output_zip) as zf:
            readme = zf.read(backup_crypto.README_NAME).decode()
        self.assertIn("OnionPress 9.8.7", readme)
        self.assertIn("backup format 2", readme)
        self.assertIn("backup_crypto.py", readme)

    def test_log_messages(self):
        backup_manager.create_backup(
            onion_address="test.onion",
            username="admin",
            password="pw",
            output_path=self.output_zip,
            version="1.0",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )
        log_text = " ".join(self.logs)
        self.assertIn("Tor keys", log_text)
        self.assertIn("database", log_text)
        self.assertIn("wp-content", log_text)
        self.assertIn("complete", log_text)

    def test_staging_cleaned_up(self):
        """Verify temp staging directory is removed after backup."""
        before = set(os.listdir(tempfile.gettempdir()))
        backup_manager.create_backup(
            onion_address="test.onion",
            username="admin",
            password="pw",
            output_path=self.output_zip,
            version="1.0",
            log_func=self.logs.append,
            data_dir=self.data_dir,
        )
        after = set(os.listdir(tempfile.gettempdir()))
        new_dirs = [d for d in (after - before) if d.startswith("onionpress-backup-")]
        self.assertEqual(len(new_dirs), 0, "Staging directory was not cleaned up")


class TestRestoreRoundTrip(unittest.TestCase):
    """Test that a backup zip can be read back by restore_from_backup.

    Uses a manually-created format-1 (ZipCrypto) zip — the backups every
    OnionPress through 2.5.2 made, which must keep restoring. The format-2
    subclass below runs the same tests on the encrypted container.
    Docker calls in restore are mocked.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logs = []

        # Create a fake docker that handles wp config get and succeeds otherwise
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            f.write('if [[ "$*" == *"wp config get DB_NAME"* ]]; then echo "wordpress"; exit 0\n')
            f.write('elif [[ "$*" == *"wp config get DB_USER"* ]]; then echo "wordpress"; exit 0\n')
            f.write('elif [[ "$*" == *"wp config get DB_PASSWORD"* ]]; then echo "testpw"; exit 0\n')
            f.write('fi\n')
            f.write('exit 0\n')
        os.chmod(fake_docker, 0o755)

        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.fake_bin + ":" + self.orig_path

        # Sandbox data_dir — passed explicitly to restore_from_backup so it
        # never touches the real ~/.onionpress/.
        self.data_dir = os.path.join(self.tmpdir, "onionpress-data")
        os.makedirs(self.data_dir)

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_backup_zip(self, password="testpw", metadata_address=None):
        """Create a realistic backup zip manually.

        metadata_address overrides the address recorded in metadata.json
        (default: the address that derives from the fake key, i.e. an
        internally-consistent backup). Tests pass a different value to
        simulate a corrupted backup whose metadata disagrees with its key.
        """
        staging = self._make_staging(metadata_address)
        zip_path = os.path.join(self.tmpdir, "backup.zip")
        subprocess.run(
            ["zip", "-r", "-P", password, zip_path, "."],
            cwd=staging, capture_output=True, check=True
        )
        shutil.rmtree(staging)
        return zip_path

    def _make_staging(self, metadata_address=None):
        """The staged tree a backup archives (see _make_backup_zip)."""
        staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(staging)

        # metadata — defaults to the key-derived address so the backup is
        # internally consistent. test_restore_overrides_mismatched_metadata
        # overrides this to exercise the corruption-detection path.
        metadata = {
            "onion_address": metadata_address or _FAKE_DERIVED_ADDR,
            "backup_date": "2026-02-01T12:00:00Z",
            "onionpress_version": "2.2.84",
            "username": "admin",
        }
        with open(os.path.join(staging, "metadata.json"), "w") as f:
            json.dump(metadata, f)

        # tor-keys — single Arti keystore file in OpenSSH PEM format
        tor_dir = os.path.join(staging, "tor-keys")
        os.makedirs(tor_dir)
        with open(os.path.join(tor_dir, "ks_hs_id.ed25519_expanded_private"), "wb") as f:
            f.write(_fake_arti_pem())

        # database
        db_dir = os.path.join(staging, "database")
        os.makedirs(db_dir)
        with open(os.path.join(db_dir, "wordpress.sql"), "w") as f:
            f.write("CREATE TABLE wp_posts;")

        # wp-content
        wpc_dir = os.path.join(staging, "wp-content")
        os.makedirs(os.path.join(wpc_dir, "themes"))
        os.makedirs(os.path.join(wpc_dir, "uploads"))
        with open(os.path.join(wpc_dir, "themes", "flavor.css"), "w") as f:
            f.write("body { color: red; }")
        return staging

    def test_restore_returns_metadata(self):
        zip_path = self._make_backup_zip()
        metadata = backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertEqual(metadata["username"], "admin")
        # No KEY-MISMATCH when backup metadata and key agree.
        self.assertFalse(any("KEY-MISMATCH" in m for m in self.logs),
                         f"unexpected KEY-MISMATCH log: {self.logs}")

    def test_restore_schedules_derivative_regeneration(self):
        """Issue #257: every restore leaves the host-side marker the
        launchers act on at the next start (an older, complete backup gets
        a no-op sweep)."""
        zip_path = self._make_backup_zip()
        marker = os.path.join(self.data_dir, ".regenerate-derivatives-pending")
        self.assertFalse(os.path.exists(marker))
        backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(any("resized images" in m for m in self.logs), self.logs)

    def test_prepare_install_from_backup_schedules_derivative_regeneration(self):
        zip_path = self._make_backup_zip()
        marker = os.path.join(self.data_dir, ".regenerate-derivatives-pending")
        backup_manager.prepare_install_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        self.assertTrue(os.path.exists(marker))

    def test_restore_overrides_mismatched_metadata(self):
        # Simulates a corrupted backup created by a stale-cached source
        # instance (op2ijk3-style incident): metadata claims one address
        # but tor-keys holds a key for a different address. Restore must
        # detect the mismatch, prefer the key-derived address everywhere
        # it persists state, and log loudly so the analytics pipeline
        # surfaces the source instance.
        stale_metadata_addr = "stalemd1stalemd1stalemd1stalemd1stalemd1stalemd1stale12.onion"
        zip_path = self._make_backup_zip(metadata_address=stale_metadata_addr)
        metadata = backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)

        # The returned metadata is mutated in-place so menubar.do_restore
        # picks up the derived address without further code changes.
        self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertNotEqual(metadata["onion_address"], stale_metadata_addr)

        # vanity-keys directory is named after the DERIVED address.
        vanity_dir = os.path.join(self.data_dir, "shared", "vanity-keys")
        self.assertEqual(os.listdir(vanity_dir), [_FAKE_DERIVED_ADDR])

        # Host-side cache file holds the DERIVED address.
        with open(os.path.join(self.data_dir, "onion_address")) as f:
            cached = f.read().strip()
        self.assertEqual(cached, _FAKE_DERIVED_ADDR)

        # Loud, greppable log line with both addresses.
        mismatch_logs = [m for m in self.logs if "KEY-MISMATCH" in m]
        self.assertEqual(len(mismatch_logs), 1,
                         f"expected exactly one KEY-MISMATCH log, got "
                         f"{len(mismatch_logs)}: {self.logs}")
        self.assertIn(stale_metadata_addr, mismatch_logs[0])
        self.assertIn(_FAKE_DERIVED_ADDR, mismatch_logs[0])

    # ── decomposed-function tests (install-from-backup primitive) ──────────

    def test_extract_backup_returns_staging_and_metadata(self):
        zip_path = self._make_backup_zip()
        staging, metadata = backup_manager.extract_backup(
            zip_path, "testpw", self.logs.append)
        try:
            self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)
            self.assertTrue(os.path.isfile(os.path.join(
                staging, "tor-keys", "ks_hs_id.ed25519_expanded_private")))
            self.assertTrue(os.path.isfile(os.path.join(
                staging, "database", "wordpress.sql")))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_extract_backup_wrong_password_raises(self):
        zip_path = self._make_backup_zip(password="rightpw")
        with self.assertRaises(ValueError) as ctx:
            backup_manager.extract_backup(zip_path, "wrongpw", self.logs.append)
        self.assertIn("password", str(ctx.exception).lower())

    def test_extract_backup_password_metachars_no_shell_injection(self):
        # A password full of shell metacharacters must be treated as a literal
        # value (list-args to subprocess, no shell): it must decrypt correctly
        # AND must not execute the embedded command.
        canary = os.path.join(self.tmpdir, "INJECTION_CANARY")
        evil = f"pw; touch {canary} #`id`$(id)"
        zip_path = self._make_backup_zip(password=evil)
        staging, metadata = backup_manager.extract_backup(
            zip_path, evil, self.logs.append)
        try:
            self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)
            self.assertFalse(
                os.path.exists(canary),
                "shell metacharacters in the password executed — command injection!")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_seed_onion_key_writes_vanity_keys_and_config(self):
        # An existing config so ADDRESS_PREFIX/ONIONNAME get rewritten in place.
        with open(os.path.join(self.data_dir, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=zzz\nONIONNAME=old\n")
        zip_path = self._make_backup_zip()
        staging, metadata = backup_manager.extract_backup(
            zip_path, "testpw", self.logs.append)
        try:
            addr = backup_manager.seed_onion_key_for_install(
                staging, metadata, self.logs.append, data_dir=self.data_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        self.assertEqual(addr, _FAKE_DERIVED_ADDR)
        # Key + hostname land in the launcher's pre-imported-key location.
        addr_dir = os.path.join(self.data_dir, "shared", "vanity-keys", addr)
        self.assertTrue(os.path.isfile(os.path.join(
            addr_dir, "ks_hs_id.ed25519_expanded_private")))
        with open(os.path.join(addr_dir, "hostname")) as f:
            self.assertEqual(f.read().strip(), addr)
        # Cached onion_address updated.
        with open(os.path.join(self.data_dir, "onion_address")) as f:
            self.assertEqual(f.read().strip(), addr)
        # Config: ONIONNAME from backup username; stale ADDRESS_PREFIX rewritten.
        with open(os.path.join(self.data_dir, "config")) as f:
            cfg = f.read()
        self.assertIn("ONIONNAME=admin", cfg)
        self.assertNotIn("ADDRESS_PREFIX=zzz", cfg)

    def test_prepare_install_from_backup_stages_seeds_and_marks(self):
        with open(os.path.join(self.data_dir, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=zzz\nONIONNAME=old\n")
        zip_path = self._make_backup_zip()
        staging, metadata = backup_manager.prepare_install_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)

        # Persistent staging dir (not a temp dir), still present afterward —
        # the import step removes it, not prepare.
        self.assertEqual(staging, os.path.join(self.data_dir, "restore-staging"))
        self.assertTrue(os.path.isdir(staging))
        self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)

        # Key seeded into the launcher's pre-imported-key location.
        addr_dir = os.path.join(self.data_dir, "shared", "vanity-keys",
                                _FAKE_DERIVED_ADDR)
        self.assertTrue(os.path.isfile(os.path.join(
            addr_dir, "ks_hs_id.ed25519_expanded_private")))

        # Marker written, first line points at the staging dir.
        marker = os.path.join(self.data_dir, ".install-from-backup")
        self.assertTrue(os.path.isfile(marker))
        with open(marker) as f:
            self.assertEqual(f.read().strip(), staging)

    def test_peek_backup_metadata_validates_and_returns(self):
        zip_path = self._make_backup_zip()
        meta = backup_manager.peek_backup_metadata(zip_path, "testpw")
        self.assertEqual(meta["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertEqual(meta["username"], "admin")

    def test_peek_backup_metadata_wrong_password_raises(self):
        zip_path = self._make_backup_zip(password="rightpw")
        with self.assertRaises(ValueError) as ctx:
            backup_manager.peek_backup_metadata(zip_path, "wrongpw")
        self.assertIn("password", str(ctx.exception).lower())

    def test_restore_wrong_password_raises_valueerror(self):
        zip_path = self._make_backup_zip(password="rightpw")
        with self.assertRaises(ValueError) as ctx:
            backup_manager.restore_from_backup(
                zip_path, "wrongpw", self.logs.append, data_dir=self.data_dir)
        self.assertIn("password", str(ctx.exception).lower())

    def test_garbage_plaintext_on_wrong_password_raises_valueerror(self):
        """A wrong password that slips past ZipCrypto's one-byte header check
        surfaces as zlib.error (or a CRC mismatch) from the encrypted read
        rather than "Bad password". Every restore path must still report it
        as a wrong password instead of crashing."""
        zip_path = self._make_backup_zip(password="rightpw")
        staging = os.path.join(self.tmpdir, "restore-staging")
        cases = [
            ("extract_backup", "extractall",
             lambda: backup_manager.extract_backup(
                 zip_path, "wrongpw", self.logs.append, staging=staging)),
            ("restore_from_backup", "extractall",
             lambda: backup_manager.restore_from_backup(
                 zip_path, "wrongpw", self.logs.append, data_dir=self.data_dir)),
            ("peek_backup_metadata", "extract",
             lambda: backup_manager.peek_backup_metadata(zip_path, "wrongpw")),
        ]
        for label, zipfile_method, call in cases:
            with self.subTest(label):
                with mock.patch.object(
                        zipfile.ZipFile, zipfile_method,
                        side_effect=zlib.error("Error -3 while decompressing data")):
                    with self.assertRaises(ValueError) as ctx:
                        call()
                self.assertIn("password", str(ctx.exception).lower())

    def test_seed_onion_key_mismatch_guard(self):
        stale = "stalemd1stalemd1stalemd1stalemd1stalemd1stalemd1stale12.onion"
        zip_path = self._make_backup_zip(metadata_address=stale)
        staging, metadata = backup_manager.extract_backup(
            zip_path, "testpw", self.logs.append)
        try:
            addr = backup_manager.seed_onion_key_for_install(
                staging, metadata, self.logs.append, data_dir=self.data_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.assertEqual(addr, _FAKE_DERIVED_ADDR)
        self.assertNotEqual(addr, stale)
        self.assertTrue(any("KEY-MISMATCH" in m for m in self.logs))

    def test_restore_renames_old_vanity_keys(self):
        """Restore renames the entire vanity-keys dir to vanity-keys.old<ts>.

        This ensures no stale keys from a prior install cause the launcher's
        head -1 to pick the wrong address. Old keys are kept for recovery.
        """
        sibling_addr = "siblingaddress.onion"
        sibling_dir = os.path.join(
            self.data_dir, "shared", "vanity-keys", sibling_addr)
        os.makedirs(sibling_dir)
        with open(os.path.join(sibling_dir, "ks_hs_id.ed25519_expanded_private"), "wb") as f:
            f.write(b"SIBLING-KEY")

        zip_path = self._make_backup_zip()
        backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)

        vanity_dir = os.path.join(self.data_dir, "shared", "vanity-keys")

        # Old keys moved to a .old<timestamp> dir, not deleted.
        parent = os.path.join(self.data_dir, "shared")
        old_dirs = [d for d in os.listdir(parent) if d.startswith("vanity-keys.old")]
        self.assertEqual(len(old_dirs), 1, "Expected exactly one vanity-keys.old* dir")
        old_sibling = os.path.join(parent, old_dirs[0], sibling_addr,
                                   "ks_hs_id.ed25519_expanded_private")
        with open(old_sibling, "rb") as f:
            self.assertEqual(f.read(), b"SIBLING-KEY")

        # New vanity-keys dir has only the restored address.
        self.assertEqual(os.listdir(vanity_dir), [_FAKE_DERIVED_ADDR])

    def test_restore_logs_progress(self):
        zip_path = self._make_backup_zip()
        backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        log_text = " ".join(self.logs)
        self.assertIn("extracting", log_text)
        self.assertIn("Tor keys", log_text)
        self.assertIn("database", log_text)
        self.assertIn("wp-content", log_text)

    def test_restore_staging_cleaned_up(self):
        zip_path = self._make_backup_zip()
        before = set(os.listdir(tempfile.gettempdir()))
        backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        after = set(os.listdir(tempfile.gettempdir()))
        new_dirs = [d for d in (after - before) if d.startswith("onionpress-restore-")]
        self.assertEqual(len(new_dirs), 0, "Staging directory was not cleaned up")


class TestRestoreRoundTripFormat2(TestRestoreRoundTrip):
    """Every TestRestoreRoundTrip test again, on a format-2 backup written by
    pack_backup (what create_backup and the Linux launcher now write): the
    restore paths must not care which format they are handed. The inherited
    test_restore_staging_cleaned_up also covers the decrypted temp archive,
    which shares the onionpress-restore- prefix."""

    def setUp(self):
        super().setUp()
        _cheap_scrypt(self)

    def _make_backup_zip(self, password="testpw", metadata_address=None):
        staging = self._make_staging(metadata_address)
        zip_path = os.path.join(self.tmpdir, "backup.zip")
        backup_manager.pack_backup(staging, zip_path, password, self.logs.append)
        shutil.rmtree(staging)
        return zip_path

    def test_backup_is_format_2(self):
        zip_path = self._make_backup_zip()
        self.assertTrue(backup_crypto.is_encrypted_backup(zip_path))
        meta = backup_manager.peek_backup_metadata(zip_path, "testpw")
        self.assertEqual(meta["backup_format"], 2)

    def test_garbage_plaintext_on_wrong_password_raises_valueerror(self):
        """Format 1's one-byte password check has no format-2 counterpart:
        a wrong password fails the 256-bit header tag in every entry point,
        before anything is extracted."""
        zip_path = self._make_backup_zip(password="rightpw")
        staging = os.path.join(self.tmpdir, "restore-staging")
        os.makedirs(staging)
        cases = [
            ("read_backup_metadata", lambda: backup_manager.read_backup_metadata(
                zip_path, "wrongpw")),
            ("peek_backup_metadata", lambda: backup_manager.peek_backup_metadata(
                zip_path, "wrongpw")),
            ("extract_backup", lambda: backup_manager.extract_backup(
                zip_path, "wrongpw", self.logs.append, staging=staging)),
            ("restore_from_backup", lambda: backup_manager.restore_from_backup(
                zip_path, "wrongpw", self.logs.append, data_dir=self.data_dir)),
        ]
        for label, call in cases:
            with self.subTest(label):
                with self.assertRaises(backup_crypto.WrongPasswordError) as ctx:
                    call()
                self.assertIn("password", str(ctx.exception).lower())
        self.assertEqual(os.listdir(staging), [])
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "onion_address")))

    def test_damaged_backup_is_not_called_a_wrong_password(self):
        zip_path = self._make_backup_zip()
        with zipfile.ZipFile(zip_path) as zf:
            payload = bytearray(zf.read(backup_crypto.PAYLOAD_NAME))
        payload[-50] ^= 0x01  # inside the last archive record's ciphertext
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(backup_crypto.README_NAME, "readme")
            zf.writestr(backup_crypto.PAYLOAD_NAME, bytes(payload))
        staging = os.path.join(self.tmpdir, "restore-staging")
        os.makedirs(staging)
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(backup_crypto.DamagedBackupError) as ctx:
            backup_manager.extract_backup(
                zip_path, "testpw", self.logs.append, staging=staging)
        self.assertIn("damaged", str(ctx.exception))
        # The partly decrypted archive is gone and nothing was extracted.
        leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
                  if n.startswith("onionpress-restore-")]
        self.assertEqual(leaked, [])
        self.assertEqual(os.listdir(staging), [])
        # The password was right, so the metadata still reads.
        self.assertEqual(backup_manager.peek_backup_metadata(zip_path, "testpw")
                         ["onion_address"], _FAKE_DERIVED_ADDR)

    def test_backup_from_a_newer_format_is_refused(self):
        """A backup whose metadata names a format this version doesn't know
        is refused up front, with a message that says what to do."""
        staging = self._make_staging()
        meta_path = os.path.join(staging, "metadata.json")
        with open(meta_path) as f:
            meta = json.load(f)
        meta["backup_format"] = backup_crypto.BACKUP_FORMAT + 1
        meta_bytes = json.dumps(meta).encode()
        with open(meta_path, "wb") as f:
            f.write(meta_bytes)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            for dirpath, _, files in os.walk(staging):
                for name in files:
                    path = os.path.join(dirpath, name)
                    zf.write(path, os.path.relpath(path, staging))
        zip_path = os.path.join(self.tmpdir, "newer.zip")
        with backup_crypto.create(zip_path, "testpw", meta_bytes, "readme") as w:
            w.write(inner.getvalue())
        staging2 = os.path.join(self.tmpdir, "restore-staging")
        for call in (lambda: backup_manager.read_backup_metadata(zip_path, "testpw"),
                     lambda: backup_manager.peek_backup_metadata(zip_path, "testpw"),
                     lambda: backup_manager.extract_backup(
                         zip_path, "testpw", self.logs.append, staging=staging2)):
            with self.assertRaises(backup_crypto.NewerBackupError) as ctx:
                call()
            self.assertIn("newer version of OnionPress", str(ctx.exception))


def _spec_decode(payload, password):
    """Decode onionpress-backup.enc from the layout written down in
    backup_crypto's docstring, sharing no code with the module — so the
    description stays true and a backup stays readable from it alone."""
    header, header_tag = payload[:56], payload[56:88]
    assert header[:16] == b"OnionPressBackup"
    version, kdf, log2_n, r, p, suite, log2_chunk, reserved = header[16:24]
    assert (version, kdf, suite, reserved) == (1, 1, 1, 0)
    salt, nonce = header[24:40], header[40:56]
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** log2_n,
                         r=r, p=p, maxmem=2 ** 30, dklen=64)
    enc_key, mac_key = key[:32], key[32:]
    assert hmac.compare_digest(
        header_tag, hmac.new(mac_key, b"OPBK-header" + header, "sha256").digest())
    streams, flags_seen, pos = {0: [], 1: []}, {0: [], 1: []}, 88
    while pos < len(payload):
        stream, flags = payload[pos], payload[pos + 1]
        length = int.from_bytes(payload[pos + 2:pos + 6], "big")
        assert length <= 2 ** log2_chunk
        ciphertext = payload[pos + 6:pos + 6 + length]
        tag = payload[pos + 6 + length:pos + 6 + length + 32]
        index = len(streams[stream]).to_bytes(8, "big")
        assert hmac.compare_digest(tag, hmac.new(
            mac_key, b"OPBK-record" + header_tag + payload[pos:pos + 6] + index
            + ciphertext, "sha256").digest())
        pad = hashlib.shake_256(enc_key + nonce + bytes([stream]) + index).digest(length)
        streams[stream].append(bytes(a ^ b for a, b in zip(ciphertext, pad)))
        flags_seen[stream].append(flags)
        pos += 6 + length + 32
    for stream in (0, 1):  # only each stream's last record carries the flag
        assert flags_seen[stream] == [0] * (len(flags_seen[stream]) - 1) + [1]
    assert len(streams[0]) == 1
    return streams[0][0], b"".join(streams[1])


class TestEncryptedContainer(unittest.TestCase):
    """backup_crypto's container on its own: records, tags, and the ways a
    file can be damaged or altered. 1 KiB records, so archives span several."""

    PW = "correct horse"

    # Written with scrypt N=2^10, salt 00..0f, nonce 10..1f, password PW,
    # metadata {"backup_format": 2} and archive b"OnionPress backup format 2
    # test vector\n". Every later OnionPress must still read these bytes.
    KNOWN_ANSWER = bytes.fromhex(
        "4f6e696f6e50726573734261636b757001010a08010114000001020304050607"
        "08090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f11db34fb92d3c3f2"
        "65df6e6aef528cc694a8042d7e8df493e44d22fde148b33900010000001408c1"
        "3a3be0ef5107b368945904620f6985e3e2ffb41280dfcc62f9e800154108d7b5"
        "7e4315584493adcd2cd5430f93835e6b652101010000002718080518c4f5edfb"
        "470c3f79a93256c8eb94f6f9f37d6c98bf14dab92e6d8e6d2bb5c938789de23b"
        "b31f1db87f8202e7cdb9e31e4c923fe22fc28d207aeb45cca11158aa23ad1a")
    KNOWN_METADATA = b'{"backup_format": 2}'
    KNOWN_ARCHIVE = b"OnionPress backup format 2 test vector\n"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        _cheap_scrypt(self)
        patcher = mock.patch.object(backup_crypto, "CHUNK_LOG2", 10)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.metadata = b'{"backup_format": 2, "onion_address": "x.onion"}'
        self.archive = os.urandom(4 * 1024 + 300)  # five archive records

    def _write(self, archive=None, name="b.zip"):
        path = os.path.join(self.tmpdir, name)
        with backup_crypto.create(path, self.PW, self.metadata, "readme") as w:
            w.write(self.archive if archive is None else archive)
        return path

    @staticmethod
    def _payload(path):
        with zipfile.ZipFile(path) as zf:
            return zf.read(backup_crypto.PAYLOAD_NAME)

    def _with_payload(self, payload):
        """A backup holding *payload*, in a fresh outer zip — zipfile's CRC
        matches, so only the container's own checks are left to catch it."""
        path = os.path.join(self.tmpdir, "altered.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(backup_crypto.README_NAME, "readme")
            zf.writestr(backup_crypto.PAYLOAD_NAME, payload)
        return path

    @staticmethod
    def _records(payload):
        """(header + header tag, [each record's bytes])"""
        pos, records = 88, []
        while pos < len(payload):
            end = pos + 6 + int.from_bytes(payload[pos + 2:pos + 6], "big") + 32
            records.append(payload[pos:end])
            pos = end
        return payload[:88], records

    def _decrypt(self, path, password=PW):
        out = os.path.join(self.tmpdir, "inner.bin")
        metadata = backup_crypto.decrypt_archive(path, password, out)
        with open(out, "rb") as f:
            return metadata, f.read()

    def test_round_trip_spans_records(self):
        path = self._write()
        _, records = self._records(self._payload(path))
        self.assertEqual(len(records), 1 + 5)  # metadata + five archive records
        self.assertEqual(self._decrypt(path), (self.metadata, self.archive))
        self.assertEqual(backup_crypto.read_metadata(path, self.PW), self.metadata)

    def test_record_boundaries(self):
        for size in (0, 1, 1023, 1024, 1025, 2048, 3072):
            with self.subTest(size=size):
                data = os.urandom(size)
                self.assertEqual(self._decrypt(self._write(data)), (self.metadata, data))

    def test_wrong_password_writes_nothing(self):
        path = self._write()
        out = os.path.join(self.tmpdir, "inner.bin")
        for call in (lambda: backup_crypto.read_metadata(path, "wrong"),
                     lambda: backup_crypto.decrypt_archive(path, "wrong", out),
                     lambda: backup_crypto.read_metadata(path, self.PW + " ")):
            with self.assertRaises(backup_crypto.WrongPasswordError):
                call()
        self.assertFalse(os.path.exists(out))

    def test_every_alteration_is_detected(self):
        path = self._write()
        head, records = self._records(self._payload(path))
        meta, arc = records[0], records[1:]
        _, other_arc = self._records(self._payload(self._write(name="other.zip")))

        def flip(record, i):
            b = bytearray(record)
            b[i] ^= 0x01
            return bytes(b)

        def flag_last(record):
            b = bytearray(record)
            b[1] |= backup_crypto.FLAG_LAST
            return bytes(b)

        rest = b"".join(arc[2:])
        alterations = {
            "archive ciphertext bit": head + meta + arc[0] + flip(arc[1], 10) + rest,
            "metadata ciphertext bit": head + flip(meta, 8) + b"".join(arc),
            "record tag bit": head + meta + flip(arc[0], -1) + b"".join(arc[1:]),
            "length field": head + meta + flip(arc[0], 5) + b"".join(arc[1:]),
            "records swapped": head + meta + arc[1] + arc[0] + rest,
            "middle record dropped": head + meta + arc[0] + rest,
            "last record dropped": head + meta + b"".join(arc[:-1]),
            "cut mid-record": head + meta + b"".join(arc)[:-40],
            "trailing bytes": head + meta + b"".join(arc) + b"\x00",
            "early last flag": head + meta + flag_last(arc[0]) + b"".join(arc[1:]),
            "metadata record missing": head + b"".join(arc),
            # Same password, same contents — but another backup's keys.
            "record from another backup": head + meta + other_arc[1] + b"".join(arc[1:]),
        }
        for label, payload in alterations.items():
            with self.subTest(label):
                with self.assertRaises(backup_crypto.DamagedBackupError):
                    self._decrypt(self._with_payload(payload))

    def test_header_changes_fail_the_password_check(self):
        """The header tag is the password check, so an altered salt, nonce or
        tag reads as a wrong password — never as a working backup."""
        payload = self._payload(self._write())
        for offset in (24, 40, 60):  # salt, nonce, header tag
            b = bytearray(payload)
            b[offset] ^= 0x01
            with self.subTest(offset=offset):
                with self.assertRaises(backup_crypto.WrongPasswordError):
                    self._decrypt(self._with_payload(bytes(b)))

    def test_corrupted_backup_file_reports_damage(self):
        """Bytes flipped in the backup file itself (zipfile's CRC of the
        payload no longer matches) still come out as DamagedBackupError."""
        path = self._write()
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(backup_crypto.PAYLOAD_NAME)
        with open(path, "r+b") as f:
            f.seek(info.header_offset + 26)
            name_len, extra_len = (int.from_bytes(f.read(2), "little"),
                                   int.from_bytes(f.read(2), "little"))
            spot = info.header_offset + 30 + name_len + extra_len + info.compress_size - 5
            f.seek(spot)
            byte = f.read(1)[0]
            f.seek(spot)
            f.write(bytes([byte ^ 0x01]))
        with self.assertRaises(backup_crypto.DamagedBackupError):
            self._decrypt(path)

    def test_unknown_format_ids_ask_for_a_newer_onionpress(self):
        payload = self._payload(self._write())
        for offset, field in ((16, "version"), (17, "kdf"), (21, "suite"),
                              (23, "reserved")):
            b = bytearray(payload)
            b[offset] = 99
            with self.subTest(field):
                with self.assertRaises(backup_crypto.NewerBackupError):
                    backup_crypto.read_metadata(self._with_payload(bytes(b)), self.PW)

    def test_header_cannot_demand_unbounded_scrypt(self):
        """The KDF parameters are read before the password can be checked;
        a header asking for absurd memory, time or record size is refused
        without running scrypt at all."""
        payload = self._payload(self._write())
        for offset, value, field in ((18, 30, "N=2^30"), (18, 0, "N=1"),
                                     (19, 0, "r=0"), (20, 0, "p=0"),
                                     (20, 200, "p=200"), (22, 5, "32-byte records"),
                                     (22, 40, "1 TiB records")):
            b = bytearray(payload)
            b[offset] = value
            with self.subTest(field):
                with mock.patch.object(backup_crypto, "_derive_keys") as derive:
                    with self.assertRaises(backup_crypto.DamagedBackupError):
                        backup_crypto.read_metadata(self._with_payload(bytes(b)), self.PW)
                derive.assert_not_called()

    def test_known_answer_vector(self):
        """Pins the format in both directions: this version writes exactly
        these bytes, and reads them back."""
        randoms = iter([bytes(range(16)), bytes(range(16, 32))])
        buf = io.BytesIO()
        with mock.patch.object(backup_crypto, "CHUNK_LOG2", 20), \
                mock.patch.object(backup_crypto, "_random_bytes",
                                  side_effect=lambda n: next(randoms)):
            writer = backup_crypto.EncryptedWriter(buf, self.PW, self.KNOWN_METADATA)
            writer.write(self.KNOWN_ARCHIVE)
            writer.close()
        self.assertEqual(buf.getvalue(), self.KNOWN_ANSWER)
        self.assertEqual(self._decrypt(self._with_payload(self.KNOWN_ANSWER)),
                         (self.KNOWN_METADATA, self.KNOWN_ARCHIVE))

    def test_independent_decoder_reads_it(self):
        path = self._write()
        self.assertEqual(_spec_decode(self._payload(path), self.PW),
                         (self.metadata, self.archive))
        self.assertEqual(_spec_decode(self.KNOWN_ANSWER, self.PW),
                         (self.KNOWN_METADATA, self.KNOWN_ARCHIVE))

    def test_unfinished_backup_never_appears(self):
        path = os.path.join(self.tmpdir, "b.zip")
        with self.assertRaises(RuntimeError):
            with backup_crypto.create(path, self.PW, self.metadata, "readme") as w:
                w.write(self.archive)
                raise RuntimeError("zip died")
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_format_detection(self):
        self.assertTrue(backup_crypto.is_encrypted_backup(self._write()))
        legacy = os.path.join(self.tmpdir, "legacy.zip")
        with zipfile.ZipFile(legacy, "w") as zf:
            zf.writestr("metadata.json", "{}")
        self.assertFalse(backup_crypto.is_encrypted_backup(legacy))
        junk = os.path.join(self.tmpdir, "junk.zip")
        with open(junk, "wb") as f:
            f.write(b"not a zip")
        self.assertFalse(backup_crypto.is_encrypted_backup(junk))
        self.assertFalse(backup_crypto.is_encrypted_backup(
            os.path.join(self.tmpdir, "missing.zip")))


class TestEncryptedBackupDefaults(unittest.TestCase):
    """The parameters new backups ship with (every other test lowers scrypt's
    cost for speed)."""

    def test_new_backups_use_the_full_scrypt_cost(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, "b.zip")
        with backup_crypto.create(path, "pw", b"{}", "readme") as w:
            w.write(b"x")
        with zipfile.ZipFile(path) as zf:
            header = zf.read(backup_crypto.PAYLOAD_NAME)[:56]
        self.assertEqual(header[:16], b"OnionPressBackup")
        # version 1, scrypt, N=2^17, r=8, p=1 (128 MiB), suite 1, 1 MiB records
        self.assertEqual(tuple(header[16:24]), (1, 1, 17, 8, 1, 1, 20, 0))
        self.assertEqual(backup_crypto.read_metadata(path, "pw"), b"{}")


class TestBashBackupHelpers(unittest.TestCase):
    """`onionpress.cli pack-backup` / `extract-backup`: what the bash
    launchers now run instead of `zip -P` / `unzip -P`. extract-backup must
    read both formats; the password always comes on stdin, never argv."""

    LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        _cheap_scrypt(self)
        from onionpress.cli import OnionPressCLI
        # The helpers use nothing set up by __init__ (config, ports, docker).
        self.cli = object.__new__(OnionPressCLI)
        # They log to stderr (the launcher appends it to its log); keep the
        # test output clean.
        patcher = mock.patch("sys.stderr", io.StringIO())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(os.path.join(self.staging, "tor-keys"))
        with open(os.path.join(self.staging, "metadata.json"), "w") as f:
            json.dump({"onion_address": "x.onion", "onionpress_version": "2.5.2",
                       "platform": "linux"}, f)
        self.pem = _fake_arti_pem()
        with open(os.path.join(self.staging, "tor-keys",
                               "ks_hs_id.ed25519_expanded_private"), "wb") as f:
            f.write(self.pem)

    @staticmethod
    def _read(rel):
        with open(os.path.join(_REPO, rel)) as f:
            return f.read()

    def _extracted_key(self, dest):
        with open(os.path.join(dest, "tor-keys",
                               "ks_hs_id.ed25519_expanded_private"), "rb") as f:
            return f.read()

    def test_pack_then_extract(self):
        out = os.path.join(self.tmpdir, "b.zip")
        self.assertEqual(self.cli.cmd_pack_backup(self.staging, out, "pw"), 0)
        self.assertTrue(backup_crypto.is_encrypted_backup(out))
        dest = os.path.join(self.tmpdir, "dest")
        os.makedirs(dest)
        self.assertEqual(self.cli.cmd_extract_backup(out, dest, "wrong"), 1)
        self.assertEqual(os.listdir(dest), [])
        self.assertEqual(self.cli.cmd_extract_backup(out, dest, "pw"), 0)
        self.assertEqual(self._extracted_key(dest), self.pem)
        with open(os.path.join(dest, "metadata.json")) as f:
            self.assertEqual(json.load(f)["backup_format"], 2)

    def test_extract_reads_a_format_1_backup(self):
        legacy = os.path.join(self.tmpdir, "legacy.zip")
        subprocess.run(["zip", "-r", "-P", "pw", legacy, "."],
                       cwd=self.staging, capture_output=True, check=True)
        dest = os.path.join(self.tmpdir, "dest")
        os.makedirs(dest)
        self.assertEqual(self.cli.cmd_extract_backup(legacy, dest, "pw"), 0)
        self.assertEqual(self._extracted_key(dest), self.pem)

    def test_launchers_no_longer_use_zip_encryption(self):
        for rel in self.LAUNCHERS:
            code = "\n".join(line for line in self._read(rel).splitlines()
                             if not line.lstrip().startswith("#"))
            with self.subTest(rel):
                self.assertNotRegex(code, r"\bzip\b[^\n]*\s-P\s")
                self.assertNotRegex(code, r"\bunzip\b[^\n]*\s-P\s")
                self.assertIn("extract-backup", code)

    def test_launchers_pass_the_password_on_stdin(self):
        for rel in self.LAUNCHERS:
            for line in self._read(rel).splitlines():
                if re.search(r"\b(pack|extract)-backup\b", line) \
                        and not line.lstrip().startswith("#"):
                    with self.subTest(rel, line=line.strip()):
                        self.assertRegex(line, r"""printf '%s' "\$\w+" \| """)
        # create_backup embeds a python3 -c snippet with a bare "}" line, so
        # take everything up to the next function instead.
        linux = self._read("linux/onionpress")
        start = linux.index("\ncreate_backup() {")
        body = linux[start:linux.index("\nrestore_backup() {", start)]
        self.assertIn('op_py pack-backup --staging "$staging" --output "$output_path"', body)

    def test_python_writer_does_not_use_zip_encryption(self):
        src = self._read("src/onionpress/backup.py")
        pack = src[src.index("\ndef pack_backup("):src.index("\n_WRONG_PASSWORD_MSG")]
        self.assertIn("['zip', '-r'", pack)
        self.assertNotIn("'-P'", pack)
        self.assertNotIn("'-e'", pack)

    def test_py2app_bundles_the_module(self):
        self.assertIn("'onionpress.backup_crypto'", self._read("setup.py"))


class TestEnsureMultisiteConstants(unittest.TestCase):
    """Test _ensure_multisite_constants() behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logs = []
        self.docker_calls = []

        # Create a fake docker that records calls and simulates responses
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        self.call_log = os.path.join(self.tmpdir, "docker_calls.log")
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            f.write(f'echo "$*" >> "{self.call_log}"\n')
            # Return wp_blogs table when checking for multisite
            f.write('if [[ "$*" == *"SHOW TABLES"* ]]; then\n')
            f.write('    echo "wp_blogs"; exit 0\n')
            f.write('fi\n')
            f.write('exit 0\n')
        os.chmod(fake_docker, 0o755)

        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.fake_bin + ":" + self.orig_path

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_adds_constants_for_multisite(self):
        """Should call wp config set for each multisite constant."""
        backup_manager._ensure_multisite_constants(self.logs.append)

        with open(self.call_log) as f:
            calls = f.read()

        # Should have called wp config set for each constant
        for name in backup_manager._MULTISITE_CONSTANTS:
            self.assertIn(name, calls, f"Missing wp config set for {name}")

    def test_logs_message(self):
        backup_manager._ensure_multisite_constants(self.logs.append)
        self.assertTrue(any("multisite" in msg.lower() for msg in self.logs))

    def test_skips_when_not_multisite(self):
        """Should not add constants if wp_blogs table doesn't exist."""
        # Override fake docker to not return wp_blogs
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            f.write(f'echo "$*" >> "{self.call_log}"\n')
            f.write('if [[ "$*" == *"SHOW TABLES"* ]]; then\n')
            f.write('    echo ""; exit 0\n')
            f.write('fi\n')
            f.write('exit 0\n')
        os.chmod(fake_docker, 0o755)

        backup_manager._ensure_multisite_constants(self.logs.append)

        with open(self.call_log) as f:
            calls = f.read()

        # Should NOT have called wp config set
        self.assertNotIn("config set", calls)


class TestVerifyWpAdminPasswordAny(unittest.TestCase):
    """verify_wp_admin_password_any() loops admins under WP-CLI."""

    def _make_result(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def test_match_returns_username(self):
        with mock.patch(
            "onionpress.backup.subprocess.run",
            return_value=self._make_result(stdout="alice\n"),
        ):
            ok, info = backup_manager.verify_wp_admin_password_any("hunter2")
        self.assertTrue(ok)
        self.assertEqual(info, "alice")

    def test_no_match_returns_failure(self):
        # WP-CLI exits 1 with no stdout when no admin password matches.
        with mock.patch(
            "onionpress.backup.subprocess.run",
            return_value=self._make_result(returncode=1, stdout=""),
        ):
            ok, info = backup_manager.verify_wp_admin_password_any("wrong")
        self.assertFalse(ok)
        self.assertIn("does not match", info)

    def test_timeout_returns_failure(self):
        with mock.patch(
            "onionpress.backup.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="wp eval", timeout=15),
        ):
            ok, info = backup_manager.verify_wp_admin_password_any("anything")
        self.assertFalse(ok)
        self.assertIn("Timed out", info)

    def test_generic_exception_returns_failure(self):
        # Anything not TimeoutExpired (e.g. FileNotFoundError if docker
        # isn't installed) should surface as an error message rather
        # than propagating.
        with mock.patch(
            "onionpress.backup.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            ok, info = backup_manager.verify_wp_admin_password_any("anything")
        self.assertFalse(ok)
        self.assertIn("Error verifying password", info)

    def test_empty_stdout_returns_failure(self):
        # Defensive: exit 0 but no username on stdout should not be
        # treated as a successful verify.
        with mock.patch(
            "onionpress.backup.subprocess.run",
            return_value=self._make_result(returncode=0, stdout=""),
        ):
            ok, info = backup_manager.verify_wp_admin_password_any("anything")
        self.assertFalse(ok)
        self.assertIn("does not match", info)


if __name__ == "__main__":
    unittest.main()
