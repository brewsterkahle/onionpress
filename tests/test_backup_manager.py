#!/usr/bin/env python3
"""Tests for backup_manager module."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

# Add src/ to path so we can import backup_manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import backup_manager


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
    """Test read_backup_metadata() with real zip files."""

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

        # Create a fake docker script that returns test data
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            # Route based on subcommand + args
            f.write('if [[ "$1" == "exec" && "$*" == *"hs_ed25519_secret_key"* ]]; then\n')
            f.write('    printf "fake-secret-key-data-32-bytes-xx"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"hs_ed25519_public_key"* ]]; then\n')
            f.write('    printf "fake-public-key-data-32-bytes-xx"; exit 0\n')
            f.write('elif [[ "$1" == "exec" && "$*" == *"wp db export"* ]]; then\n')
            f.write('    echo "CREATE TABLE wp_posts; INSERT INTO wp_posts VALUES (1);"; exit 0\n')
            f.write('elif [[ "$1" == "cp" ]]; then\n')
            # For `docker cp container:/path dest`, create the dest with sample content
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
        )
        self.assertTrue(os.path.exists(self.output_zip))

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            names = zf.namelist()

        # Normalize ./ prefixes
        names_normalized = [n.lstrip("./") for n in names if n.lstrip("./")]

        self.assertIn("metadata.json", names_normalized)
        self.assertTrue(any("tor-keys/hs_ed25519_secret_key" in n for n in names_normalized))
        self.assertTrue(any("tor-keys/hs_ed25519_public_key" in n for n in names_normalized))
        self.assertTrue(any("database/wordpress.sql" in n for n in names_normalized))
        self.assertTrue(any("wp-content/themes/" in n for n in names_normalized))
        self.assertTrue(any("wp-content/plugins/" in n for n in names_normalized))

    def test_metadata_content(self):
        backup_manager.create_backup(
            onion_address="testaddr.onion",
            username="admin",
            password="testpass",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
        )

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    data = json.loads(zf.read(name, pwd=b"testpass"))
                    break

        self.assertEqual(data["onion_address"], "testaddr.onion")
        self.assertEqual(data["username"], "admin")
        self.assertEqual(data["onionpress_version"], "2.2.84")
        self.assertIn("backup_date", data)

    def test_password_protection(self):
        backup_manager.create_backup(
            onion_address="testaddr.onion",
            username="admin",
            password="secret",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
        )

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    # Reading without password should fail
                    with self.assertRaises(RuntimeError):
                        zf.read(name)
                    # Reading with correct password should succeed
                    data = zf.read(name, pwd=b"secret")
                    self.assertIn(b"testaddr.onion", data)
                    break

    def test_log_messages(self):
        backup_manager.create_backup(
            onion_address="test.onion",
            username="admin",
            password="pw",
            output_path=self.output_zip,
            version="1.0",
            log_func=self.logs.append,
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
        )
        after = set(os.listdir(tempfile.gettempdir()))
        new_dirs = [d for d in (after - before) if d.startswith("onionpress-backup-")]
        self.assertEqual(len(new_dirs), 0, "Staging directory was not cleaned up")


class TestRestoreRoundTrip(unittest.TestCase):
    """Test that a backup zip can be read back by restore_from_backup.

    Uses a manually-created zip (no Docker needed for read/extract).
    Docker calls in restore are mocked.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logs = []

        # Create a fake docker that succeeds for all commands
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(fake_docker, 0o755)

        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.fake_bin + ":" + self.orig_path

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_backup_zip(self, password="testpw"):
        """Create a realistic backup zip manually."""
        staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(staging)

        # metadata
        metadata = {
            "onion_address": "restored123.onion",
            "backup_date": "2026-02-01T12:00:00Z",
            "onionpress_version": "2.2.84",
            "username": "admin",
        }
        with open(os.path.join(staging, "metadata.json"), "w") as f:
            json.dump(metadata, f)

        # tor-keys
        tor_dir = os.path.join(staging, "tor-keys")
        os.makedirs(tor_dir)
        with open(os.path.join(tor_dir, "hs_ed25519_secret_key"), "wb") as f:
            f.write(b"secret-key-bytes")
        with open(os.path.join(tor_dir, "hs_ed25519_public_key"), "wb") as f:
            f.write(b"public-key-bytes")

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

        zip_path = os.path.join(self.tmpdir, "backup.zip")
        subprocess.run(
            ["zip", "-r", "-P", password, zip_path, "."],
            cwd=staging, capture_output=True, check=True
        )
        shutil.rmtree(staging)
        return zip_path

    def test_restore_returns_metadata(self):
        zip_path = self._make_backup_zip()
        metadata = backup_manager.restore_from_backup(
            zip_path, "testpw", self.logs.append)
        self.assertEqual(metadata["onion_address"], "restored123.onion")
        self.assertEqual(metadata["username"], "admin")

    def test_restore_logs_progress(self):
        zip_path = self._make_backup_zip()
        backup_manager.restore_from_backup(zip_path, "testpw", self.logs.append)
        log_text = " ".join(self.logs)
        self.assertIn("extracting", log_text)
        self.assertIn("Tor keys", log_text)
        self.assertIn("database", log_text)
        self.assertIn("wp-content", log_text)

    def test_restore_staging_cleaned_up(self):
        zip_path = self._make_backup_zip()
        before = set(os.listdir(tempfile.gettempdir()))
        backup_manager.restore_from_backup(zip_path, "testpw", self.logs.append)
        after = set(os.listdir(tempfile.gettempdir()))
        new_dirs = [d for d in (after - before) if d.startswith("onionpress-restore-")]
        self.assertEqual(len(new_dirs), 0, "Staging directory was not cleaned up")


if __name__ == "__main__":
    unittest.main()
