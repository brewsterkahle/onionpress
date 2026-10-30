#!/usr/bin/env python3
"""An OnionHeaven hub's registry and registrants' keys in its backups are
sealed to an offline recovery key (issue #60, option O7), and whatever a
restore puts on the hub data volume is checked first.

- `onionpress hub-recovery-key create` makes an X25519 pair (PyNaCl), keeps
  only the public half, in ~/.onionpress/hub-backup-recovery.pub (0600,
  outside ~/.onionpress/shared, which the VM mounts, and not in config,
  which the Settings page can write), and prints the private half once.
- A hub's backup reads the registry and keys out of the onionheaven
  container into memory, seals them (SealedBox) to the public half and
  stores onionheaven.sealed in the format-2 archive; no plaintext hub data
  is ever a file in the staging folder. Without a recovery key it is as
  before, with a warning every time.
- A restore opens it only with the private half, checked against the
  backup's key id before anything is restored; without it, or with another
  key, the rest is restored and the hub data is named as left out.
- Every key restored, sealed or not, must be its address's (the hub's own
  keypair check, the same verdicts as key-convert.py's keypair_error), and
  goes in as a fresh PEM; the registry is checked in the container
  (REGISTRY_RESTORE_SH, run here for real with the sqlite3 CLI).
- (The Settings page's refusal to back up a hub or OnionHome is in
  tests/test_backup_hub_data_off_wordpress.py.)

No docker: the container side is a stub on PATH (the onionheaven scripts
run for real on a sandbox folder) or subprocess.run mocked.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import launcher_ops  # noqa: E402
from onionpress import backup, backup_crypto, hub_backup, key_manager  # noqa: E402
from onionpress import cli as op_cli  # noqa: E402
from onionpress.cli import OnionPressCLI  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402
import test_backup_staging_links as bsl  # noqa: E402
import test_backup_hub_data_off_wordpress as off  # noqa: E402

PW = mv.PW
A = off.A
KEY = off.KEY
REGISTRANT_KEY = off.REGISTRANT_KEY
HAS_SODIUM = hub_backup._SealedBox is not None


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _load_key_convert():
    # key-convert.py imports onion_auth as a top-level module: the tor image
    # copies it next to the script at build time (src/onionpress/onion_auth.py).
    pkg = os.path.join(PROJECT_ROOT, "src", "onionpress")
    if pkg not in sys.path:
        sys.path.append(pkg)
    spec = importlib.util.spec_from_file_location(
        "key_convert_hub_backup", os.path.join(DOCKER_TOR_DIR, "key-convert.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair(seed):
    """A key pair whose scalar's top bit is clear, and its address."""
    priv = bytes([seed]) * 64
    pub = key_manager.derive_public_key(priv)
    return priv, pub, key_manager.derive_onion_address(pub)


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestRecoveryKey(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-rk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, ".onionpress")

    def test_create_keeps_only_the_public_half(self):
        private_text, kid = hub_backup.create_recovery_key(self.data_dir)
        path = hub_backup.public_key_path(self.data_dir)
        self.assertEqual(path, os.path.join(self.data_dir, "hub-backup-recovery.pub"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(private_text.startswith(hub_backup.PRIVATE_PREFIX))
        private_raw = hub_backup.parse_private_key(private_text)
        public_raw = hub_backup.load_public_key(self.data_dir)
        self.assertEqual(hub_backup.public_key_of(private_raw), public_raw)
        self.assertEqual(kid, hub_backup.key_id(public_raw))
        self.assertRegex(kid, r"\A[0-9a-f]{16}\Z")
        body = private_text[len(hub_backup.PRIVATE_PREFIX):]
        # Not on disk anywhere under the data directory.
        for dirpath, _, files in os.walk(self.data_dir):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as f:
                    data = f.read()
                self.assertNotIn(body.encode(), data)
                self.assertNotIn(private_raw, data)
        with open(path) as f:
            text = f.read()
        self.assertIn(kid, text)
        self.assertIn(hub_backup.PUBLIC_PREFIX, text)
        self.assertEqual(os.listdir(self.data_dir), ["hub-backup-recovery.pub"])

    def test_an_existing_key_is_replaced_only_with_force(self):
        _, kid = hub_backup.create_recovery_key(self.data_dir)
        with self.assertRaises(hub_backup.RecoveryKeyExistsError) as cm:
            hub_backup.create_recovery_key(self.data_dir)
        self.assertIn(kid, str(cm.exception))
        self.assertIn("--force", str(cm.exception))
        self.assertEqual(hub_backup.key_id(hub_backup.load_public_key(self.data_dir)), kid)
        _, kid2 = hub_backup.create_recovery_key(self.data_dir, force=True)
        self.assertNotEqual(kid, kid2)
        self.assertEqual(hub_backup.key_id(hub_backup.load_public_key(self.data_dir)), kid2)

    def test_a_link_at_the_name_counts_as_a_key_and_is_replaced_not_followed(self):
        os.makedirs(self.data_dir)
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write("untouched")
        os.symlink(target, hub_backup.public_key_path(self.data_dir))
        with self.assertRaises(hub_backup.RecoveryKeyExistsError):
            hub_backup.create_recovery_key(self.data_dir)
        with self.assertRaises(hub_backup.RecoveryKeyError):
            hub_backup.load_public_key(self.data_dir)
        hub_backup.create_recovery_key(self.data_dir, force=True)
        with open(target) as f:
            self.assertEqual(f.read(), "untouched")
        self.assertFalse(os.path.islink(hub_backup.public_key_path(self.data_dir)))

    def test_a_damaged_public_key_file_is_an_error_not_no_key(self):
        os.makedirs(self.data_dir)
        path = hub_backup.public_key_path(self.data_dir)
        self.assertIsNone(hub_backup.load_public_key(self.data_dir))
        for content in (b"", b"garbage\n", b"x" * 5000,
                        (hub_backup.PRIVATE_PREFIX + "a" * 52 + "\n").encode()):
            with self.subTest(content=content[:20]):
                with open(path, "wb") as f:
                    f.write(content)
                with self.assertRaises(hub_backup.RecoveryKeyError):
                    hub_backup.load_public_key(self.data_dir)

    def test_the_private_half_as_people_paste_it(self):
        private_text, _ = hub_backup.create_recovery_key(self.data_dir)
        raw = hub_backup.parse_private_key(private_text)
        body = private_text[len(hub_backup.PRIVATE_PREFIX):]
        for text in (f"  {private_text}\n", private_text.upper(),
                     hub_backup.PRIVATE_PREFIX + body[:26] + "\n  " + body[26:]):
            self.assertEqual(hub_backup.parse_private_key(text), raw)
        with self.assertRaises(hub_backup.RecoveryKeyError) as cm:
            hub_backup.parse_private_key(
                hub_backup.format_public_key(hub_backup.load_public_key(self.data_dir)))
        self.assertEqual(str(cm.exception), hub_backup.PUBLIC_NOT_PRIVATE_MSG)
        for bad in ("", "  ", "hello", private_text[:-1], private_text + "a",
                    private_text[:-1] + "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(hub_backup.RecoveryKeyError):
                    hub_backup.parse_private_key(bad)

    def test_never_where_the_vm_containers_or_wordpress_reach(self):
        """The VM mounts ~/.onionpress/shared and the Documents folder
        (Colima's --mount flags); containers bind-mount only folders of
        those; the Settings page writes config. The public half is none of
        them: replaced by code in a container, every later backup would be
        sealed to a key someone else holds."""
        path = hub_backup.public_key_path(self.data_dir)
        self.assertEqual(os.path.dirname(path), self.data_dir)
        self.assertNotEqual(os.path.basename(path), "config")
        shared = os.path.join(self.data_dir, "shared")
        self.assertFalse(os.path.commonpath([path, shared]) == shared)
        # Every Colima mount is shared/ or the Documents folder.
        for rel in ("app/MacOS/launcher.sh",):
            text = _read(rel)
            mounts = re.findall(r"--mount[\"'],?\s*f?[\"']?([^\s\"']+)", text)
            mounts += re.findall(r"--mount (\S+)", text)
            self.assertTrue(mounts, rel)
            # launcher.sh's documents_mount_flag: printf '--mount %s:w' "$DOCUMENTS_DIR"
            if "%s:w'" in mounts:
                self.assertIn("printf -- '--mount %s:w' \"$DOCUMENTS_DIR\"", text)
                mounts = [m for m in mounts if m != "%s:w'"]
            for m in mounts:
                with self.subTest(rel=rel, mount=m):
                    self.assertRegex(m, r"shared|DOCUMENTS")
        # Every host path a compose file bind-mounts is under one of them.
        for rel in ("app/Resources/docker/docker-compose.yml",
                    "app/Resources/docker/docker-compose.cloudflare.yml"):
            for line in _read(rel).splitlines():
                m = re.match(r"\s*-\s*(\$\{[^}]*\}|/|\.|~)", line)
                if m and ":" in line and not line.strip().startswith("- ONION"):
                    with self.subTest(rel=rel, line=line.strip()):
                        self.assertRegex(line, r"ONIONPRESS_SHARED_DIR|ONIONPRESS_DOCUMENTS_DIR"
                                               r"|/var/run/docker.sock|/etc/localtime")
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            self.assertIn('ONIONPRESS_SHARED_DIR="$DATA_DIR/shared"', _read(rel))
        # Not a config key, so the Settings page's allowlist cannot reach it.
        from onionpress import config
        self.assertFalse([k for k in config.SETTINGS_PAGE_KEYS if "RECOVERY" in k])


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestSealing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-seal-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.private_text, self.kid = hub_backup.create_recovery_key(self.tmp)
        self.private = hub_backup.parse_private_key(self.private_text)
        self.public = hub_backup.load_public_key(self.tmp)
        self.hub = hub_backup.HubData(b"SQLite format 3\x00rows", {A: REGISTRANT_KEY})

    def test_round_trip(self):
        blob = hub_backup.seal(hub_backup.pack(self.hub), self.public)
        self.assertNotIn(REGISTRANT_KEY, blob)
        self.assertNotIn(b"SQLite format 3", blob)
        hub = hub_backup.unpack(hub_backup.unseal(blob, self.private), lambda m: None)
        self.assertEqual(hub.registry, self.hub.registry)
        self.assertEqual(hub.keys, self.hub.keys)
        # Every seal is fresh (an ephemeral key each time).
        self.assertNotEqual(blob, hub_backup.seal(hub_backup.pack(self.hub), self.public))

    def test_another_key_or_an_altered_seal_opens_nothing(self):
        blob = hub_backup.seal(hub_backup.pack(self.hub), self.public)
        other, _ = hub_backup.create_recovery_key(os.path.join(self.tmp, "other"))
        for key, data in ((hub_backup.parse_private_key(other), blob),
                          (self.private, blob[:-1] + bytes([blob[-1] ^ 1])),
                          (self.private, blob[:40]),
                          (self.private, b"")):
            with self.assertRaises(hub_backup.WrongRecoveryKeyError):
                hub_backup.unseal(data, key)
        with mock.patch.object(hub_backup, "MAX_SEALED_BYTES", len(blob) - 1):
            with self.assertRaises(hub_backup.WrongRecoveryKeyError):
                hub_backup.unseal(blob, self.private)

    def test_the_key_id_tells_another_key_apart_first(self):
        meta = {"onionheaven_data": "sealed", "onionheaven_recovery_key_id": self.kid}
        self.assertEqual(hub_backup.check_key_for_backup(self.private_text, meta), self.private)
        other, other_id = hub_backup.create_recovery_key(os.path.join(self.tmp, "o"))
        with self.assertRaises(hub_backup.WrongRecoveryKeyError) as cm:
            hub_backup.check_key_for_backup(other, meta)
        self.assertEqual(str(cm.exception), hub_backup.OTHER_KEY_MSG.format(
            given=other_id, wanted=self.kid))
        with self.assertRaises(hub_backup.RecoveryKeyError):
            hub_backup.check_key_for_backup("not a key", meta)

    def test_a_newer_seal_is_named(self):
        hub_backup.check_sealed_format({})
        hub_backup.check_sealed_format({"onionheaven_sealed_format": 1})
        with self.assertRaises(hub_backup.RecoveryKeyError) as cm:
            hub_backup.check_sealed_format({"onionheaven_sealed_format": 2})
        self.assertEqual(str(cm.exception), hub_backup.NEWER_SEAL_MSG)

    def _tar(self, entries):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, kind, data in entries:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                elif kind == "dir":
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
                elif kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = data
                    tf.addfile(info)
        return buf.getvalue()

    def test_only_the_registry_and_keys_are_read_within_bounds(self):
        data = self._tar([
            ("registry.db", "file", b"SQLite format 3\x00x"),
            ("keys", "dir", None), (f"keys/{A}", "dir", None),
            (f"keys/{A}/{KEY}", "file", REGISTRANT_KEY),
            (f"keys/{A}/hostname", "file", b"x\n"),
            (f"keys/not-an-address/{KEY}", "file", REGISTRANT_KEY),
            (f"keys/{'b' * 56}.onion/{KEY}", "link", "/etc/passwd"),
            (f"keys/{'c' * 56}.onion/{KEY}", "file", b"k" * (hub_backup.MAX_KEY_FILE_BYTES + 1)),
            ("../escape", "file", b"x"), ("/abs", "file", b"x"),
            ("heartbeat.log", "file", b"log"),
        ])
        hub, left_out = hub_backup.read_tar(io.BytesIO(data))
        self.assertEqual(hub.registry, b"SQLite format 3\x00x")
        self.assertEqual(hub.keys, {A: REGISTRANT_KEY})
        self.assertEqual(len(left_out), 6, left_out)
        with mock.patch.object(hub_backup, "MAX_ARCHIVE_BYTES", 100):
            with self.assertRaises(hub_backup.HubDataError):
                hub_backup.read_tar(io.BytesIO(data))
        with mock.patch.object(hub_backup, "MAX_REGISTRY_BYTES", 5):
            with self.assertRaises(hub_backup.HubDataError):
                hub_backup.read_tar(io.BytesIO(data))
        with mock.patch.object(hub_backup, "MAX_KEYS", 0):
            with self.assertRaises(hub_backup.HubDataError):
                hub_backup.read_tar(io.BytesIO(data))
        with self.assertRaises(hub_backup.HubDataError):
            hub_backup.read_tar(io.BytesIO(b"not a tar at all" * 100))


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestCreateSealedBackup(unittest.TestCase):
    """backup.create_backup on a hub with a recovery key set up: the hub's
    container stub serves a real registry and a real registrant key."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-hub-sealed-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.private_text, self.kid = hub_backup.create_recovery_key(self.data_dir)
        self.logs = []
        self.container = off._Container(self, self.tmp)

    def _backup(self):
        out = os.path.join(self.tmp, "backup.zip")
        meta = backup.create_backup(mv.ADDR, "admin", PW, out, "2.5.3",
                                    self.logs.append, data_dir=self.data_dir,
                                    include_hub_data=True)
        return meta, out

    def test_the_hub_data_is_sealed_and_nothing_else_holds_it(self):
        staged = []
        real_pack = backup.pack_backup

        def spy(staging, *args, **kwargs):
            # What the staging folder holds when it is zipped: the hub data
            # only as the sealed member, never as files in the clear.
            for dirpath, dirnames, files in os.walk(staging):
                for name in dirnames + files:
                    rel = os.path.relpath(os.path.join(dirpath, name), staging)
                    staged.append(rel)
                    path = os.path.join(dirpath, name)
                    if os.path.isfile(path):
                        with open(path, "rb") as f:
                            data = f.read()
                        self.assertNotIn(REGISTRANT_KEY, data, rel)
                        if rel != "metadata.json":
                            self.assertNotIn(A.encode(), data, rel)
            return real_pack(staging, *args, **kwargs)

        with mock.patch.object(backup, "pack_backup", side_effect=spy):
            meta, out = self._backup()
        self.assertIn(hub_backup.SEALED_MEMBER, staged)
        self.assertFalse([n for n in staged if n.startswith("onionheaven/")
                          or n == "onionheaven"], staged)
        members = bsl._inner_names_and_bytes(out, PW)
        self.assertFalse([n for n in members if n.startswith("onionheaven/")])
        self.assertFalse([n for n, d in members.items() if REGISTRANT_KEY in d])
        stored = json.loads(members["metadata.json"])
        for m in (meta, stored):
            self.assertEqual(m["onionheaven_data"], "sealed")
            self.assertEqual(m["onionheaven_recovery_key_id"], self.kid)
            self.assertEqual(m["onionheaven_sealed_format"], hub_backup.SEALED_FORMAT)
            self.assertIs(m["is_onionheaven"], True)
        # The README (the one part anyone can read) says so.
        import zipfile
        with zipfile.ZipFile(out) as zf:
            readme = zf.read(backup_crypto.README_NAME).decode()
        self.assertIn(self.kid, readme)
        self.assertIn("sealed", readme)
        self.assertIn("restore the site from this backup and leave the hub data out", readme)
        # Opened with the private half: the hub's registry and keys.
        hub = hub_backup.unpack(hub_backup.unseal(
            members[hub_backup.SEALED_MEMBER],
            hub_backup.parse_private_key(self.private_text)), lambda m: None)
        self.assertEqual(hub.keys, {A: REGISTRANT_KEY})
        snapshot = os.path.join(self.tmp, "r.db")
        with open(snapshot, "wb") as f:
            f.write(hub.registry)
        conn = sqlite3.connect(snapshot)
        self.assertEqual(conn.execute("SELECT content_address FROM registry").fetchall(), [(A,)])
        conn.close()
        self.assertNotIn(backup.HUB_DATA_UNSEALED_WARNING, self.logs)
        self.assertTrue([m for m in self.logs if f"sealed to recovery key {self.kid}" in m])

    def test_a_damaged_recovery_key_file_stops_the_backup(self):
        with open(hub_backup.public_key_path(self.data_dir), "w") as f:
            f.write("garbage\n")
        with self.assertRaises(hub_backup.RecoveryKeyError):
            self._backup()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "backup.zip")))

    def test_without_a_recovery_key_the_readme_says_password_only(self):
        os.remove(hub_backup.public_key_path(self.data_dir))
        meta, out = self._backup()
        self.assertEqual(meta["onionheaven_data"], "unsealed")
        import zipfile
        with zipfile.ZipFile(out) as zf:
            readme = zf.read(backup_crypto.README_NAME).decode()
        self.assertIn("protected by the password\nonly", readme)


class _Docker:
    """subprocess.run for a restore: records calls and stdin, answers the
    WordPress side and the registry script."""

    def __init__(self, registry_answer=(0, "restored dropped=0\n")):
        self.calls = []
        self.registry_answer = registry_answer

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        rc, out = 0, ""
        if argv[-1] == backup.REGISTRY_RESTORE_SH:
            rc, out = self.registry_answer
        elif "get" in argv:
            out = "wordpress\n"
        return subprocess.CompletedProcess(
            argv, rc, stdout=out if kwargs.get("text") else out.encode(),
            stderr="" if kwargs.get("text") else b"")

    def hub_calls(self):
        return [(c, i) for c, i in self.calls if any("onionheaven" in a for a in c)]

    def restored_keys(self):
        for argv, data in self.hub_calls():
            if argv[:3] == ["docker", "cp", "-"]:
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    return {m.name.split("/")[0]: key_manager.parse_openssh_key(
                                tf.extractfile(m).read())
                            for m in tf.getmembers() if m.name.endswith(KEY)}
        return {}


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestRestoreSealed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="op-hub-restore-")
        cls.private_text, cls.kid = hub_backup.create_recovery_key(
            os.path.join(cls.tmp, "hub-data"))
        # The hub's backup, made through create_backup.
        case = unittest.TestCase()
        with mock.patch.object(backup_crypto, "OPSLIMIT", 1), \
                mock.patch.object(backup_crypto, "MEMLIMIT", 8192):
            stack = contextlib.ExitStack()
            case.addCleanup = stack.callback
            off._Container(case, os.path.join(cls.tmp, "hub"))
            try:
                os.makedirs(os.path.join(cls.tmp, "hub"), exist_ok=True)
                cls.zip = os.path.join(cls.tmp, "hub-backup.zip")
                backup.create_backup(mv.ADDR, "admin", PW, cls.zip, "2.5.3",
                                     lambda m: None,
                                     data_dir=os.path.join(cls.tmp, "hub-data"),
                                     include_hub_data=True)
            finally:
                stack.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        self.logs = []
        self.staging = tempfile.mkdtemp(prefix="op-hub-restore-staging-")
        self.addCleanup(shutil.rmtree, self.staging, True)
        backup.extract_archive(self.zip, PW, self.staging, self.logs.append)
        self.metadata = backup.read_staged_metadata(self.staging)

    def _restore(self, hub_data=None, docker=None):
        docker = docker or _Docker()
        with mock.patch.object(backup.subprocess, "run", side_effect=docker), \
                mock.patch.object(backup, "put_wp_content"), \
                mock.patch.object(backup, "_ensure_multisite_constants"):
            # data_dir: a sandbox, so the DB password is never read from
            # the live ~/.onionpress/secrets.
            backup.restore_container_artifacts(self.staging, self.metadata, self.logs.append,
                                               hub_data=hub_data, data_dir=self.staging)
        return docker

    def test_with_the_key_the_hub_data_comes_back(self):
        private = hub_backup.check_key_for_backup(self.private_text, self.metadata)
        hub = backup.open_sealed_hub_data(self.staging, self.metadata, private,
                                          self.logs.append)
        docker = self._restore(hub_data=hub)
        self.assertEqual(docker.restored_keys(), {A: (off.REG_PRIV, off.REG_PUB)})
        registry = [i for c, i in docker.hub_calls() if c[-1] == backup.REGISTRY_RESTORE_SH]
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry[0].startswith(b"SQLite format 3\x00"))
        self.assertIn(backup.HUB_DATA_RESTORED_LOG, self.logs)

    def test_without_the_key_the_rest_is_restored_and_the_hub_data_named(self):
        docker = self._restore()
        self.assertEqual(docker.hub_calls(), [])
        self.assertIn(backup.sealed_skipped_message(self.metadata), self.logs)
        self.assertIn(self.kid, backup.sealed_skipped_message(self.metadata))
        # The site itself was restored.
        self.assertTrue([c for c, i in docker.calls
                         if "onionpress-db" in c and "--sandbox" in c and i])

    def test_another_key_is_caught_before_anything_is_written(self):
        other, _ = hub_backup.create_recovery_key(os.path.join(self.staging, "x"))
        with self.assertRaises(hub_backup.WrongRecoveryKeyError):
            hub_backup.check_key_for_backup(other, self.metadata)
        # A backup whose metadata names the other key (made to look so):
        # the seal itself refuses, before any docker call.
        forged = dict(self.metadata, onionheaven_recovery_key_id=hub_backup.key_id(
            hub_backup.public_key_of(hub_backup.parse_private_key(other))))
        docker = _Docker()
        with mock.patch.object(backup.subprocess, "run", side_effect=docker):
            with self.assertRaises(hub_backup.WrongRecoveryKeyError):
                backup.open_sealed_hub_data(self.staging, forged,
                                            hub_backup.check_key_for_backup(other, forged),
                                            self.logs.append)
        self.assertEqual(docker.calls, [])

    def test_the_cli_takes_the_key_from_the_environment_or_goes_without(self):
        logs = []
        right = op_cli._hub_key_for_backup(self.metadata, self.private_text, logs.append)
        self.assertEqual(right, hub_backup.parse_private_key(self.private_text))
        other, _ = hub_backup.create_recovery_key(os.path.join(self.staging, "y"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(op_cli._hub_key_for_backup(self.metadata, other, logs.append))
        self.assertIn("is not the one this backup's hub data was sealed to", err.getvalue())
        self.assertTrue([m for m in logs if "restoring without it" in m])
        # No key and no terminal: no prompt, no hub data.
        with mock.patch.object(sys, "stdin", io.StringIO(self.private_text + "\n")):
            self.assertIsNone(op_cli._hub_key_for_backup(self.metadata, "", logs.append))
        # A backup without sealed hub data needs no key.
        self.assertIsNone(op_cli._hub_key_for_backup({}, self.private_text, logs.append))

    def test_cmd_restore_wires_it_through(self):
        """onionpress restore (the Mac's Restore... and Linux's CLI): the key
        from the environment opens the sealed data right after extraction,
        and the import writes it; another key restores the site without."""
        for key, want in ((self.private_text, {A: (off.REG_PRIV, off.REG_PUB)}),
                          ("", {})):
            with self.subTest(with_key=bool(key)):
                data_dir = tempfile.mkdtemp(prefix="op-hub-cli-restore-")
                self.addCleanup(shutil.rmtree, data_dir, True)
                cli = object.__new__(OnionPressCLI)
                cli.paths = mock.Mock(data_dir=data_dir)
                cli.log = self.logs.append
                cli.containers = mock.Mock()
                cli.docker = mock.Mock()
                cli.docker.run.return_value = mock.Mock(ok=True)
                cli.docker.wp.return_value = mock.Mock(ok=True)
                docker = _Docker()
                with mock.patch.object(backup.subprocess, "run", side_effect=docker), \
                        mock.patch.object(backup, "put_wp_content"), \
                        mock.patch.object(backup, "_ensure_multisite_constants"), \
                        mock.patch("onionpress.derivatives.spawn_regeneration"), \
                        mock.patch.object(sys, "stdin", io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    # Without a key, not at a terminal: only with the
                    # confirmation (the menubar's Restore Without).
                    confirmed = frozenset() if key else frozenset({"sealed-hub-data-left-out"})
                    self.assertEqual(cli.cmd_restore(PW, self.zip, key, confirmed=confirmed), 0)
                self.assertEqual(docker.restored_keys(), want)
                if not key:
                    self.assertTrue([m for m in self.logs if "were not restored" in m])

    def test_cmd_restore_refuses_to_leave_the_hub_data_out_unasked(self):
        """Not at a terminal, a sealed backup without the key and without
        --confirmed sealed-hub-data-left-out is refused before anything is
        extracted (onionpress.confirm)."""
        data_dir = tempfile.mkdtemp(prefix="op-hub-cli-restore-")
        self.addCleanup(shutil.rmtree, data_dir, True)
        cli = object.__new__(OnionPressCLI)
        cli.paths = mock.Mock(data_dir=data_dir)
        cli.log = self.logs.append
        cli.containers = mock.Mock()
        cli.docker = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(backup, "prepare_install_from_backup") as prepare, \
                mock.patch.object(sys, "stdin", io.StringIO()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_restore(PW, self.zip, "", json_output=True), 3)
        prepare.assert_not_called()
        cli.containers.stop.assert_not_called()
        answer = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertIn("sealed-hub-data-left-out", answer["error"])
        self.assertIn(hub_backup.RECOVERY_KEY_ENV, answer["error"])

    def test_cmd_restore_check_asks_for_the_key(self):
        """`onionpress restore --check --json` on a sealed backup: the
        question, with the facts; a wrong key is the answer's error; the
        right key is accepted; the confirmation stands in for the key."""
        cli = object.__new__(OnionPressCLI)
        cli.log = self.logs.append

        def check(key, confirmed=frozenset()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.cmd_restore_check(PW, self.zip, key, confirmed, True)
            return rc, json.loads(out.getvalue().strip().splitlines()[-1])
        rc, ans = check("")
        self.assertEqual(rc, 4)
        self.assertEqual(ans["confirm"], "sealed-hub-data-left-out")
        self.assertEqual(ans["key_id"], self.kid)
        self.assertEqual(ans["onionheaven_data"], "sealed")
        self.assertEqual(ans["onion_address"], mv.ADDR)
        rc, ans = check(hub_backup.format_private_key(b"\x11" * 32))
        self.assertEqual(rc, 1)
        self.assertIn("error", ans)
        rc, ans = check(self.private_text)
        self.assertEqual((rc, ans["ok"], ans["recovery_key"]), (0, True, "accepted"))
        rc, ans = check("", frozenset({"sealed-hub-data-left-out"}))
        self.assertEqual((rc, ans["ok"]), (0, True))


class TestRegistrantKeyChecks(unittest.TestCase):
    """What a restore lets onto the hub data volume as a registrant's key."""

    def test_the_hubs_own_verdicts(self):
        kc = _load_key_convert()
        priv, pub, addr = _pair(0x0b)
        other_priv, other_pub, other_addr = _pair(0x0d)
        top_bit = bytearray(priv)
        top_bit[31] |= 0x80
        cases = {
            "good": (addr, key_manager.build_openssh_key(priv, pub), None),
            "another address's key": (other_addr, key_manager.build_openssh_key(priv, pub),
                                      "the key belongs to another address"),
            "a scalar for another public key": (
                addr, key_manager.build_openssh_key(other_priv, pub),
                "the secret scalar does not give the public key"),
            "scalar top bit": (addr, key_manager.build_openssh_key(bytes(top_bit), pub),
                               "the secret scalar is not below 2^255"),
            "zero scalar": (addr, key_manager.build_openssh_key(b"\0" * 32 + priv[32:], pub),
                            "the secret scalar is zero or a multiple of the group order"),
            "garbage": (addr, b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n", "the key file cannot be read"),
            "not an address": ("../../etc", key_manager.build_openssh_key(priv, pub),
                               "the folder name is not a v3 onion address"),
            "too large": (addr, b"x" * (hub_backup.MAX_KEY_FILE_BYTES + 1),
                          "the key file is too large"),
        }
        for label, (address, pem, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(hub_backup.registrant_key_problem(address, pem), want)
        # keypair_problem gives key-convert.py's verdicts (libsodium), and
        # so does the arithmetic it falls back on without PyNaCl.
        order = key_manager.ED25519_ORDER.to_bytes(32, "little")
        pairs = [(priv, pub), (other_priv, pub), (bytes(top_bit), pub),
                 (b"\0" * 32 + priv[32:], pub), (order + priv[32:], pub),
                 (priv, other_pub)]
        for expanded, public in pairs:
            with self.subTest(expanded=expanded[:4].hex(), public=public[:4].hex()):
                mine = hub_backup.keypair_problem(expanded, public)
                if HAS_SODIUM:
                    self.assertEqual(mine, kc.keypair_error(expanded, public))
                with mock.patch.object(key_manager, "_sodium_ed25519", return_value=None), \
                        mock.patch.object(key_manager, "_fallback_logged", True):
                    self.assertEqual(hub_backup.keypair_problem(expanded, public), mine)

    def test_check_keeps_good_keys_as_fresh_pems_and_logs_the_rest(self):
        priv, pub, addr = _pair(0x0b)
        _, _, other = _pair(0x0d)
        junk_pem = (b"junk before the key\n" + key_manager.build_openssh_key(priv, pub)
                    + b"junk after it\n")
        hub = hub_backup.HubData(b"not sqlite", {addr: junk_pem, other: junk_pem})
        logs = []
        checked = hub_backup.check(hub, logs.append)
        self.assertEqual(list(checked.keys), [addr])
        self.assertEqual(key_manager.parse_openssh_key(checked.keys[addr]), (priv, pub))
        self.assertNotEqual(checked.keys[addr], junk_pem)
        self.assertIsNone(checked.registry)
        self.assertTrue([m for m in logs if "another address" in m])
        self.assertTrue([m for m in logs if "not an SQLite database" in m])
        self.assertIn("Restore: 1 of 2 registrants' keys passed the checks", logs)

    def test_a_plain_backups_folder_is_read_without_following_links(self):
        priv, pub, addr = _pair(0x0b)
        tmp = tempfile.mkdtemp(prefix="op-hub-dir-")
        self.addCleanup(shutil.rmtree, tmp, True)
        secret = os.path.join(tmp, "secret")
        with open(secret, "wb") as f:
            f.write(key_manager.build_openssh_key(priv, pub))
        src = os.path.join(tmp, "onionheaven")
        os.makedirs(os.path.join(src, "keys", addr))
        os.symlink(secret, os.path.join(src, "keys", addr, KEY))
        os.symlink(tmp, os.path.join(src, "keys", A))
        os.makedirs(os.path.join(src, "keys", "not-an-address"))
        os.symlink(secret, os.path.join(src, "registry.db"))
        logs = []
        hub = hub_backup.load_dir(src, logs.append)
        self.assertEqual(hub.keys, {})
        self.assertIsNone(hub.registry)
        self.assertEqual(len([m for m in logs if "left" in m]), 4, logs)


@unittest.skipUnless(shutil.which("sqlite3"), "no sqlite3 command")
class TestRegistryRestoreScript(unittest.TestCase):
    """REGISTRY_RESTORE_SH, run for real with the sqlite3 CLI on a sandbox
    standing in for /var/lib/onionheaven."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-registry-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.volume = os.path.join(self.tmp, "volume")
        os.makedirs(self.volume)
        self.live = os.path.join(self.volume, "registry.db")
        conn = sqlite3.connect(self.live)
        conn.execute("PRAGMA journal_mode=WAL")
        self._schema(conn)
        conn.execute("INSERT INTO registry (content_address, healthcheck_address) "
                     "VALUES (?, ?)", ("live" * 14 + ".onion", A))
        conn.commit()
        conn.close()

    @staticmethod
    def _schema(conn):
        conn.execute("CREATE TABLE registry (content_address TEXT NOT NULL, "
                     "healthcheck_address TEXT NOT NULL, status TEXT, "
                     "PRIMARY KEY (content_address, healthcheck_address))")
        conn.execute("CREATE TABLE takeover_containers (container_name TEXT PRIMARY KEY)")

    def _db(self, build):
        path = os.path.join(self.tmp, f"in-{len(os.listdir(self.tmp))}.db")
        conn = sqlite3.connect(path)
        build(conn)
        conn.commit()
        conn.close()
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _corrupt(data):
        page = int.from_bytes(data[16:18], "big")
        return data[:page] + b"\xff" * 12 + data[page + 12:]

    def _run(self, data):
        script = backup.REGISTRY_RESTORE_SH.replace(backup.ONIONHEAVEN_DATA_DIR, self.volume)
        r = subprocess.run(["sh", "-c", script], input=data, capture_output=True,
                           timeout=60, env={**os.environ, "TMPDIR": self.tmp})
        return r.returncode, r.stdout.decode().strip()

    def _rows(self):
        conn = sqlite3.connect(self.live)
        try:
            return conn.execute("SELECT content_address FROM registry ORDER BY 1").fetchall()
        finally:
            conn.close()

    def test_a_good_registry_is_restored_and_bad_rows_dropped(self):
        _, _, addr = _pair(0x0b)

        def build(conn):
            self._schema(conn)
            conn.executemany("INSERT INTO registry VALUES (?, ?, 'online')", [
                (addr, A), ("../../etc", A), (addr.upper(), A), (A, "x"), (A, None or "")])
            conn.execute("INSERT INTO takeover_containers VALUES ('onionheaven-takeover-1')")
        rc, out = self._run(self._db(build))
        self.assertEqual((rc, out), (0, "restored dropped=4"))
        self.assertEqual(self._rows(), [(addr,)])
        self.assertEqual(os.listdir(self.tmp).count("volume"), 1)
        self.assertFalse([n for n in os.listdir(self.tmp) if n.startswith("tmp.")])

    def test_anything_but_the_hubs_tables_is_refused(self):
        def trigger(conn):
            self._schema(conn)
            conn.execute("CREATE TRIGGER t AFTER UPDATE ON registry BEGIN "
                         "DELETE FROM registry; END")

        def other_table(conn):
            self._schema(conn)
            conn.execute("CREATE TABLE extra (x)")

        def view(conn):
            self._schema(conn)
            conn.execute("CREATE VIEW v AS SELECT 1")

        def index(conn):
            self._schema(conn)
            conn.execute("CREATE INDEX i ON registry (status)")

        def no_registry(conn):
            conn.execute("CREATE TABLE takeover_containers (container_name TEXT PRIMARY KEY)")

        def wrong_columns(conn):
            conn.execute("CREATE TABLE registry (x TEXT)")
        cases = {"trigger": self._db(trigger), "other table": self._db(other_table),
                 "view": self._db(view), "index": self._db(index),
                 "no registry": self._db(no_registry), "wrong columns": self._db(wrong_columns),
                 "not a database": b"SQLite format 3\x00" + b"\x00" * 200,
                 # The registry table's root page header (page 2) overwritten.
                 "corrupt": self._corrupt(self._db(lambda c: self._schema(c)))}
        for label, data in cases.items():
            with self.subTest(label):
                rc, out = self._run(data)
                self.assertEqual(rc, 3, out)
                self.assertTrue(out.startswith("refused: "), out)
                self.assertEqual(self._rows(), [("live" * 14 + ".onion",)])

    def test_write_hub_data_reports_a_refusal_and_keeps_the_keys(self):
        logs = []
        docker = _Docker(registry_answer=(3, "refused: it holds more than the hub's tables\n"))
        priv, pub, addr = _pair(0x0b)
        hub = hub_backup.HubData(b"SQLite format 3\x00", {addr: key_manager.build_openssh_key(priv, pub)})
        with mock.patch.object(backup.subprocess, "run", side_effect=docker):
            backup.write_hub_data(hub, logs.append)
        self.assertEqual(list(docker.restored_keys()), [addr])
        self.assertIn("Restore: the backup's OnionHeaven registry was not restored: "
                      "it holds more than the hub's tables", logs)
        docker = _Docker(registry_answer=(1, ""))
        with mock.patch.object(backup.subprocess, "run", side_effect=docker):
            with self.assertRaises(Exception):
                backup.write_hub_data(hub, logs.append)


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestCli(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _main(self, *argv, env=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = op_cli.main(["--data-dir", self.tmp, *argv])
            leaked = os.environ.get(hub_backup.RECOVERY_KEY_ENV)
        return rc, out.getvalue(), err.getvalue(), leaked

    def test_create_show_check(self):
        rc, out, _, _ = self._main("hub-recovery-key", "show")
        self.assertEqual(rc, 3)
        rc, out, _, _ = self._main("hub-recovery-key", "create")
        self.assertEqual(rc, 0)
        private = re.search(r"onionpress-hub-recovery-key-1-[a-z2-7]{52}", out).group(0)
        kid = hub_backup.key_id(hub_backup.load_public_key(self.tmp))
        self.assertIn(kid, out)
        rc, out, err, _ = self._main("hub-recovery-key", "create")
        self.assertEqual(rc, 1)
        self.assertIn("--force", err)
        rc, out, _, _ = self._main("hub-recovery-key", "show")
        self.assertEqual(rc, 0)
        self.assertIn(kid, out)
        self.assertNotIn(private, out)
        rc, out, _, leaked = self._main("hub-recovery-key", "check",
                                        env={hub_backup.RECOVERY_KEY_ENV: private})
        self.assertEqual(rc, 0, out)
        self.assertIsNone(leaked)
        rc, out, _, _ = self._main("hub-recovery-key", "check", stdin=private + "\n")
        self.assertEqual(rc, 0, out)
        other, _ = hub_backup.create_recovery_key(os.path.join(self.tmp, "o"))
        rc, out, _, _ = self._main("hub-recovery-key", "check", stdin=other + "\n")
        self.assertEqual(rc, 1)
        self.assertIn("different recovery key", out)

    def test_the_key_never_stays_in_the_environment(self):
        """Removed before anything runs, whatever the command, so no docker
        child of OnionPressCLI inherits it."""
        _, _, _, leaked = self._main("hub-recovery-key", "show",
                                     env={hub_backup.RECOVERY_KEY_ENV: "x"})
        self.assertIsNone(leaked)
        src = _read("src/onionpress/cli.py")
        pop = src.index("hub_recovery_key = os.environ.pop(RECOVERY_KEY_ENV")
        main = src.index("\ndef main(")
        self.assertLess(main, pop)
        self.assertLess(pop, src.index("return handlers[args.command](", main))

    def test_restore_gets_the_key_from_the_environment(self):
        with mock.patch("onionpress.cli.OnionPressCLI") as M, \
                mock.patch.dict(os.environ, {"ONIONPRESS_BACKUP_PASSWORD": "pw",
                                             hub_backup.RECOVERY_KEY_ENV: "the-key"}):
            M.return_value.cmd_restore.return_value = 0
            op_cli.main(["restore", "/tmp/b.zip"])
            self.assertNotIn(hub_backup.RECOVERY_KEY_ENV, os.environ)
        M.return_value.cmd_restore.assert_called_once_with(
            "pw", "/tmp/b.zip", "the-key", confirmed=frozenset(), json_output=False)

    def test_the_launchers_pass_it_through_without_argv(self):
        """Taken out of the launcher's environment before anything runs and
        handed only to the restore and hub-recovery-key commands, in their
        environment, never as an argument."""
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            text = _read(rel)
            with self.subTest(rel):
                head = text[:text.index("\nset -e\n") + 400]
                self.assertIn('_HUB_RECOVERY_KEY="${ONIONPRESS_HUB_RECOVERY_KEY:-}"\n'
                              'unset ONIONPRESS_HUB_RECOVERY_KEY\n', head)
                self.assertRegex(text, r'ONIONPRESS_HUB_RECOVERY_KEY="\$_HUB_RECOVERY_KEY" \\\n'
                                       r'\s+\S+ (-m onionpress\.cli )?--data-dir "\$DATA_DIR" '
                                       r'hub-recovery-key "\$\{@:2\}"')
                self.assertRegex(text, r'ONIONPRESS_HUB_RECOVERY_KEY="\$_HUB_RECOVERY_KEY" '
                                       r'ONIONPRESS_BACKUP_PASSWORD="\$BACKUP_PASS" \S+ '
                                       r'(-m onionpress\.cli )?--data-dir "\$DATA_DIR" restore "\$restore_path"')
                # restore --check, restore, hub-recovery-key
                uses = [ln.strip() for ln in text.splitlines()
                        if "$_HUB_RECOVERY_KEY" in ln and not ln.lstrip().startswith("#")]
                self.assertEqual(len(uses), 3, uses)
                for line in uses:
                    self.assertTrue(line.startswith(('ONIONPRESS_HUB_RECOVERY_KEY="$_HUB_RECOVERY_KEY" ',
                                                     'if ! ONIONPRESS_HUB_RECOVERY_KEY="$_HUB_RECOVERY_KEY" ')),
                                    line)

    def test_cmd_backup_warns_without_a_recovery_key(self):
        cli = object.__new__(OnionPressCLI)
        cli.paths = mock.Mock(data_dir=self.tmp)
        cli.containers = mock.Mock(get_onion_address=mock.Mock(return_value=mv.ADDR))
        cli.log = lambda m: None
        err = io.StringIO()
        with mock.patch("onionpress.backup.verify_wp_admin", return_value=(True, None)), \
                mock.patch("onionpress.backup.create_backup",
                           return_value={"onionheaven_data": "unsealed"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_backup(PW, os.path.join(self.tmp, "b.zip"), "admin"), 0)
        self.assertIn(backup.HUB_DATA_UNSEALED_NOTE, err.getvalue())


class TestMac(unittest.TestCase):
    """src/menubar.py (AppKit, so checked as source) and the bundle: the
    menubar asks `onionpress restore --check --json` what the backup
    needs, asks the question in its own dialog, and hands the key over in
    the environment."""

    def setUp(self):
        self.src = _read("src/menubar.py")

    def test_restore_asks_for_the_key_on_a_sealed_backup(self):
        body = self.src[self.src.index('@rumps.clicked("Restore...")'):]
        body = body[:body.index("def _adopt_restored_address")]
        ask = body.index("hub_recovery_key = self._ask_hub_recovery_key(facts, zip_path, restore_env)")
        self.assertLess(body.index('if facts.get("confirm") == "sealed-hub-data-left-out":'), ask)
        self.assertLess(ask, body.index("_BackupProgressWindow("))
        self.assertIn("restore_env[launcher_ops.HUB_RECOVERY_KEY_ENV] = hub_recovery_key", body)
        self.assertIn('confirmed_flags = ["--confirmed", "sealed-hub-data-left-out"]', body)
        self.assertIn("extra_env=hub_key_env,", body)
        self.assertIn('extra_args=["--json", *confirmed_flags],', body)
        self.assertIn("launcher_ops.run_launcher_restore(", body)
        self.assertIn('answer.get("hub_data") == "restored"', body)
        ask_body = self.src[self.src.index("def _ask_hub_recovery_key"):]
        ask_body = ask_body[:ask_body.index('@rumps.clicked("Restore...")')]
        self.assertIn("NSSecureTextField", ask_body)
        # A typed key is checked by the CLI, in the environment
        self.assertIn('["restore", zip_path, "--check", "--json"]', ask_body)
        self.assertIn("launcher_ops.HUB_RECOVERY_KEY_ENV: text", ask_body)
        self.assertEqual(launcher_ops.HUB_RECOVERY_KEY_ENV, hub_backup.RECOVERY_KEY_ENV)

    def test_a_hub_backup_without_a_key_says_so(self):
        """The CLI's notes (HUB_DATA_UNSEALED_NOTE among them) are shown
        with the finished backup."""
        cli_src = _read("src/onionpress/cli.py")
        self.assertIn("notes.append(HUB_DATA_UNSEALED_NOTE)", cli_src)
        self.assertIn('for note in ans.get("notes") or []:\n                msg += "\\n\\n" + note',
                      self.src)

    def test_bundled(self):
        self.assertIn("'onionpress.hub_backup'", _read("setup.py"))


if __name__ == "__main__":
    unittest.main()
