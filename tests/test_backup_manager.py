#!/usr/bin/env python3
"""Tests for onionpress.backup module (formerly backup_manager)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

# Add src/ to path so we can import both onionpress.backup and key_manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from onionpress import backup as backup_manager
from onionpress import key_manager


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
            f.write('    mkdir -p "$fakedir/themes" "$fakedir/plugins" "$fakedir/uploads"\n')
            f.write('    echo "theme data" > "$fakedir/themes/flavor.css"\n')
            f.write('    echo "plugin data" > "$fakedir/plugins/hello.php"\n')
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

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            names = zf.namelist()

        # Normalize ./ prefixes
        names_normalized = [n.lstrip("./") for n in names if n.lstrip("./")]

        self.assertIn("metadata.json", names_normalized)
        self.assertTrue(any("tor-keys/ks_hs_id.ed25519_expanded_private" in n for n in names_normalized))
        self.assertTrue(any("database/wordpress.sql" in n for n in names_normalized))
        self.assertTrue(any("wp-content/themes/" in n for n in names_normalized))
        self.assertTrue(any("wp-content/plugins/" in n for n in names_normalized))

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

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    data = json.loads(zf.read(name, pwd=b"testpass"))
                    break

        self.assertEqual(data["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertEqual(data["username"], "admin")
        self.assertEqual(data["onionpress_version"], "2.2.84")
        self.assertIn("backup_date", data)
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

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    data = json.loads(zf.read(name, pwd=b"testpass"))
                    break

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

        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    # Reading without password should fail
                    with self.assertRaises(RuntimeError):
                        zf.read(name)
                    # Reading with correct password should succeed
                    data = zf.read(name, pwd=b"secret")
                    self.assertIn(_FAKE_DERIVED_ADDR.encode(), data)
                    break

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


class TestCreateBackupStaticSite(unittest.TestCase):
    """create_backup(site_type="static"): skips DB/wp-content entirely,
    tars ~/OnionPress/Site/ instead, no docker calls beyond the Tor-key
    extraction (which is content-agnostic and shared with WordPress mode).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_zip = os.path.join(self.tmpdir, "backup.zip")
        self.logs = []

        self.pem_path = os.path.join(self.tmpdir, "arti.pem")
        with open(self.pem_path, "wb") as f:
            f.write(_fake_arti_pem())

        # Fake docker only needs to answer the Tor-key extraction exec —
        # a static backup makes no other docker calls at all. Anything
        # else hitting this script is a bug (would indicate a WP-only
        # code path firing for a static backup).
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w") as f:
            f.write('#!/bin/bash\n')
            f.write(f'if [[ "$1" == "exec" && "$*" == *"ks_hs_id.ed25519_expanded_private"* ]]; then\n')
            f.write(f'    cat "{self.pem_path}"; exit 0\n')
            f.write('fi\n')
            f.write('echo "unexpected docker call: $*" >&2\n')
            f.write('exit 1\n')
        os.chmod(fake_docker, 0o755)

        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.fake_bin + ":" + self.orig_path

        self.data_dir = os.path.join(self.tmpdir, "onionpress-data")
        os.makedirs(self.data_dir)
        self.documents_dir = os.path.join(self.tmpdir, "OnionPress")
        self.site_dir = os.path.join(self.documents_dir, "Site")
        os.makedirs(self.site_dir)
        with open(os.path.join(self.site_dir, "index.html"), "w") as f:
            f.write("<h1>hello</h1>")

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create(self, **kwargs):
        defaults = dict(
            onion_address=_FAKE_DERIVED_ADDR,
            username="site",
            password="testpass",
            output_path=self.output_zip,
            version="2.2.84",
            log_func=self.logs.append,
            data_dir=self.data_dir,
            site_type="static",
            documents_dir=self.documents_dir,
        )
        defaults.update(kwargs)
        backup_manager.create_backup(**defaults)

    def test_zip_contains_site_not_wp_content(self):
        self._create()
        with zipfile.ZipFile(self.output_zip, "r") as zf:
            names = [n.lstrip("./") for n in zf.namelist() if n.lstrip("./")]
        self.assertTrue(any("site/index.html" in n for n in names))
        self.assertTrue(any("tor-keys/ks_hs_id.ed25519_expanded_private" in n for n in names))
        self.assertFalse(any("wp-content" in n for n in names))
        self.assertFalse(any("database" in n for n in names))

    def test_metadata_marks_is_static(self):
        self._create()
        with zipfile.ZipFile(self.output_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("metadata.json"):
                    data = json.loads(zf.read(name, pwd=b"testpass"))
                    break
        self.assertTrue(data["is_static"])
        self.assertFalse(data["is_onionheaven"])
        self.assertFalse(data["is_onionhome"])
        self.assertFalse(data["excludes_creations"])

    def test_missing_site_dir_produces_empty_but_valid_backup(self):
        shutil.rmtree(self.site_dir)
        self._create()
        self.assertTrue(os.path.exists(self.output_zip))
        with zipfile.ZipFile(self.output_zip, "r") as zf:
            names = [n.lstrip("./") for n in zf.namelist() if n.lstrip("./")]
        self.assertTrue(any("tor-keys/ks_hs_id.ed25519_expanded_private" in n for n in names))


class TestRestoreContainerArtifactsStaticSite(unittest.TestCase):
    """restore_container_artifacts branches on metadata['is_static'] — pure
    host filesystem copy, no docker calls, no WP-only steps."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(os.path.join(self.staging, "site"))
        with open(os.path.join(self.staging, "site", "index.html"), "w") as f:
            f.write("restored content")
        self.documents_dir = os.path.join(self.tmpdir, "OnionPress")
        self.logs = []

    def test_restores_site_dir(self):
        backup_manager.restore_container_artifacts(
            self.staging, {"is_static": True}, self.logs.append,
            documents_dir=self.documents_dir,
        )
        restored = os.path.join(self.documents_dir, "Site", "index.html")
        with open(restored) as f:
            self.assertEqual(f.read(), "restored content")

    def test_replaces_existing_site_dir(self):
        site_dir = os.path.join(self.documents_dir, "Site")
        os.makedirs(site_dir)
        with open(os.path.join(site_dir, "stale.html"), "w") as f:
            f.write("old")
        backup_manager.restore_container_artifacts(
            self.staging, {"is_static": True}, self.logs.append,
            documents_dir=self.documents_dir,
        )
        self.assertFalse(os.path.exists(os.path.join(site_dir, "stale.html")))
        self.assertTrue(os.path.exists(os.path.join(site_dir, "index.html")))

    def test_warns_when_backup_has_no_site_dir(self):
        shutil.rmtree(os.path.join(self.staging, "site"))
        backup_manager.restore_container_artifacts(
            self.staging, {"is_static": True}, self.logs.append,
            documents_dir=self.documents_dir,
        )
        self.assertTrue(any("no site/ directory" in m for m in self.logs))


class TestRestoreRoundTrip(unittest.TestCase):
    """Test that a backup zip can be read back by restore_from_backup.

    Uses a manually-created zip (no Docker needed for read/extract).
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
            zip_path, "testpw", self.logs.append, data_dir=self.data_dir)
        self.assertEqual(metadata["onion_address"], _FAKE_DERIVED_ADDR)
        self.assertEqual(metadata["username"], "admin")
        # No KEY-MISMATCH when backup metadata and key agree.
        self.assertFalse(any("KEY-MISMATCH" in m for m in self.logs),
                         f"unexpected KEY-MISMATCH log: {self.logs}")

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
        with self.assertRaises(Exception):
            backup_manager.extract_backup(zip_path, "wrongpw", self.logs.append)

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
        with self.assertRaises(Exception):
            backup_manager.peek_backup_metadata(zip_path, "wrongpw")

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
