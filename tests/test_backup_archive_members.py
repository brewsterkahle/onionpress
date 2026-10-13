#!/usr/bin/env python3
"""A restore extracts only plain files and directories under plain names.

Anyone can make a backup file and hand it over: the password proves only
that the file was made with it. The `unzip` fast path recreates symlink entries, and the
restore then docker-cp's the staged wp-content/, tor-keys/, onionheaven/
and onionhome/ trees, following a link in the source path: a member
`wp-content -> ~` would publish the home folder through the onion service.
These tests hand link-holding and oddly named archives, in both backup
formats, to every extraction path (the unzip CLI, the zipfile fallback, the
Python restore entry points, the extract-backup helper and the bash
launchers that call it) and check that each refuses before anything is
extracted or touched.

No docker: the Python paths fail the test on any docker call, and the bash
paths run the launcher functions with docker stubbed (see
test_backup_metadata_validation.TestLauncherRestorePaths).
"""

import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
import zlib
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, backup_crypto, cli  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402

PW = mv.PW
HAVE_UNZIP = shutil.which("unzip") is not None
SECRET = b"host file the backup's author wants published\n"


def _zip_bytes(entries):
    """An in-memory zip of *entries*: (name, data) or (ZipInfo, data)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf, warnings.catch_warnings():
        # Duplicate names are some tests' point.
        warnings.simplefilter("ignore", UserWarning)
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _typed(name, file_type, data=b"", create_system=3):
    info = zipfile.ZipInfo(name)
    info.create_system = create_system
    info.external_attr = (file_type | 0o644) << 16
    return info, data


def _validate(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        backup.validate_archive_members(zf)


def _staging_with_links(root, links, name="staging"):
    """A backup tree (valid metadata and key) plus symlinks: *links* maps a
    path inside the tree, which the link replaces, to its target, in which
    {home} is the folder outside the tree the links point into and
    {rel_home} the same folder relative to the link. Returns (staging, home).
    """
    home = os.path.join(root, name + ".home")
    os.makedirs(os.path.join(home, ".ssh"))
    for rel in ("secret.txt", os.path.join(".ssh", "id_ed25519")):
        with open(os.path.join(home, rel), "wb") as f:
            f.write(SECRET)
    staging = mv._make_staging(root, mv._metadata(), name=name)
    os.makedirs(os.path.join(staging, "database"))
    with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
        f.write("-- dump\n")
    for rel, target in links.items():
        path = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isdir(path):
            shutil.rmtree(path)
        os.symlink(target.format(
            home=home, rel_home=os.path.relpath(home, os.path.dirname(path))), path)
    return staging, home


def _zip_tree(staging, out, password=None):
    """`zip -r -y` (store links as links, as an attacker would) of *staging*;
    with *password*, a format-1 ZipCrypto backup."""
    pw = ["-P", password] if password else []
    subprocess.run(["zip", "-r", "-y", "-q", *pw, out, "."], cwd=staging,
                   check=True, capture_output=True)
    return out


def _format2(out, inner_zip, metadata=None):
    """Wrap the zip at *inner_zip* in a format-2 container with the
    project's own writer: what anyone can make with a password of their
    choosing."""
    meta = dict(metadata or mv._metadata(), backup_format=backup_crypto.BACKUP_FORMAT)
    with open(inner_zip, "rb") as f:
        inner = f.read()
    with backup_crypto.create(out, PW, json.dumps(meta).encode(),
                              backup_crypto.readme_text("test")) as w:
        w.write(inner)
    return out


def _crafted_backups(root, links, tag):
    """The same link-holding tree as a format-1 and a format-2 backup."""
    staging, home = _staging_with_links(root, links, name=f"{tag}.staging")
    legacy = _zip_tree(staging, os.path.join(root, f"{tag}-format1.zip"), PW)
    inner = _zip_tree(staging, os.path.join(root, f"{tag}-inner.zip"))
    encrypted = _format2(os.path.join(root, f"{tag}-format2.zip"), inner)
    shutil.rmtree(staging)
    return home, {"format 1": legacy, "format 2": encrypted}


# Links a crafted backup could hold, by where the restore would copy them.
LINK_CASES = {
    "wp-content is a link to the home folder": {"wp-content": "{home}"},
    "a file link inside uploads": {
        "wp-content/uploads/notes.txt": "{home}/secret.txt"},
    "a relative link out of the tree": {
        "wp-content/uploads/key.txt": "{rel_home}/.ssh/id_ed25519"},
    "tor-keys is a link": {"tor-keys": "{home}/.ssh"},
    "a dangling link": {"onionhome": "/nonexistent/onionpress-test"},
}


class TestMemberNames(unittest.TestCase):
    """validate_archive_members on crafted member lists."""

    def test_names_real_backups_use_pass(self):
        _validate(_zip_bytes([
            ("metadata.json", "{}"),
            ("tor-keys/", ""),
            ("tor-keys/ks_hs_id.ed25519_expanded_private", "k"),
            ("database/wordpress.sql", "--"),
            ("wp-content/uploads/2026/10/café.jpg", "x"),
            ("wp-content/uploads/été/..dots..txt", "x"),
            ("wp-content/plugins/p/a:b.txt", "x"),
            ("wp-content/plugins/p/back\\slash.txt", "x"),
            ("wp-content/plugins/p/ space ", "x"),
            ("wp-content/uploads/.onionpress-regenerate-derivatives", ""),
            ("./config-overrides.json", "{}"),
        ]))

    def test_a_real_zip_r_archive_passes(self):
        """What pack_backup's `zip -r` writes: Unix modes, UTF-8 names
        without the UTF-8 flag (zipfile reads them as cp437)."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        staging = mv._make_staging(tmp, mv._metadata())
        os.makedirs(os.path.join(staging, "wp-content", "uploads", "été"))
        for name in ("café.jpg", "back\\slash.txt", "a:b.txt"):
            with open(os.path.join(staging, "wp-content", "uploads", "été", name), "w") as f:
                f.write("x")
        out = _zip_tree(staging, os.path.join(tmp, "real.zip"))
        with zipfile.ZipFile(out) as zf:
            self.assertTrue(any(stat.S_ISDIR(i.external_attr >> 16) for i in zf.infolist()))
            backup.validate_archive_members(zf)

    def test_unsafe_names_are_refused(self):
        for name in ("/etc/passwd", "\\etc\\passwd", "../escaped", "a/../../escaped",
                     "a/..", "..", "a\\..\\..\\escaped", "wp-content/../../escaped",
                     ".", "./", "/", "//", "wp-content/./../escaped"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "unsafe file name"):
                    _validate(_zip_bytes([("metadata.json", "{}"), (name, "x")]))

    def test_an_empty_name_is_refused(self):
        # zipfile cannot write an empty name: write "Q" as the only member,
        # then cut it out of both headers and fix the lengths and offsets.
        raw = bytearray(_zip_bytes([("Q", "x")]))
        local_name = 30
        self.assertEqual(raw[local_name:local_name + 1], b"Q")
        del raw[local_name]
        struct.pack_into("<H", raw, 26, 0)
        central = raw.index(b"PK\x01\x02")
        self.assertEqual(raw[central + 46:central + 47], b"Q")
        del raw[central + 46]
        struct.pack_into("<H", raw, central + 28, 0)
        end = raw.index(b"PK\x05\x06")
        size, offset = struct.unpack_from("<II", raw, end + 12)
        struct.pack_into("<II", raw, end + 12, size - 1, offset - 1)
        with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
            self.assertEqual(zf.namelist(), [""])
        with self.assertRaisesRegex(ValueError, "unsafe file name"):
            _validate(bytes(raw))

    def test_a_nul_in_a_name_is_refused(self):
        raw = _zip_bytes([("metadata.json", "{}"),
                          ("wp-content/aQb.php", "x")]).replace(b"aQb", b"a\x00b")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # zipfile cuts the name at the NUL; unzip may not.
            self.assertEqual(zf.namelist()[1], "wp-content/a")
        with self.assertRaisesRegex(ValueError, "unsafe file name"):
            _validate(raw)

    def test_duplicate_names_are_refused(self):
        for names in (["a/b", "a/b"], ["a/b", "a//b"], ["a/b", "./a/b"],
                      ["a/", "a"], ["a", "a/"], ["a\\b", "a/b"]):
            with self.subTest(names=names):
                with self.assertRaisesRegex(ValueError, "twice"):
                    _validate(_zip_bytes([(n, "") for n in names]))

    def test_a_file_that_is_also_a_folder_is_refused(self):
        for names in (["wp-content", "wp-content/x"], ["a/b/c/d", "a/b"]):
            with self.subTest(names=names):
                with self.assertRaisesRegex(ValueError, "also a folder"):
                    _validate(_zip_bytes([(n, "x") for n in names]))

    def test_the_unicode_path_field_is_checked_too(self):
        """zipfile (and unzip built with Unicode support) extract to the
        name in a 0x7075 extra field when its CRC matches the header name:
        a harmless header name must not carry a hostile one."""
        evil = "../escaped.txt".encode()
        info = zipfile.ZipInfo("safe.txt")
        info.extra = struct.pack("<HHBL", 0x7075, 5 + len(evil), 1,
                                 zlib.crc32(b"safe.txt")) + evil
        raw = _zip_bytes([("metadata.json", "{}"), (info, "x")])
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertEqual(zf.namelist()[1], "../escaped.txt")
        with self.assertRaisesRegex(ValueError, "unsafe file name"):
            _validate(raw)

    def test_links_and_special_files_are_refused(self):
        cases = {stat.S_IFLNK: "symbolic link", stat.S_IFCHR: "special file",
                 stat.S_IFBLK: "special file", stat.S_IFIFO: "special file",
                 stat.S_IFSOCK: "special file"}
        for file_type, what in cases.items():
            for system in (3, 0):
                with self.subTest(type=oct(file_type), create_system=system):
                    with self.assertRaisesRegex(ValueError, what):
                        _validate(_zip_bytes([
                            ("metadata.json", "{}"),
                            _typed("wp-content", file_type, b"/etc", system)]))

    def test_directories_and_untyped_files_pass(self):
        _validate(_zip_bytes([_typed("wp-content/", stat.S_IFDIR),
                              _typed("wp-content/a", stat.S_IFREG, b"x"),
                              ("wp-content/b", "untyped")]))

    def test_the_message_shows_a_printable_cut_name(self):
        name = "wp-content/" + "\n‮" * 3 + "x" * 200
        with self.assertRaises(ValueError) as ctx:
            _validate(_zip_bytes([_typed(name, stat.S_IFLNK, b"/")]))
        msg = str(ctx.exception)
        self.assertTrue(msg.startswith("Not a valid OnionPress backup"), msg)
        self.assertTrue(msg.isprintable(), repr(msg))
        self.assertLess(len(msg), 200)


class TestStagedTree(unittest.TestCase):
    """check_staged_tree: the check after extraction, and before the copy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = mv._make_staging(self.tmp, mv._metadata())
        os.makedirs(os.path.join(self.root, "wp-content", "uploads"))

    def test_a_plain_tree_passes(self):
        backup.check_staged_tree(self.root)

    def test_links_anywhere_are_refused(self):
        for rel, target in (("wp-content/uploads/x.txt", "/etc/hosts"),
                            ("onionhome", self.tmp),
                            ("tor-keys/dangling", "/nonexistent")):
            path = os.path.join(self.root, rel)
            os.symlink(target, path)
            with self.subTest(rel):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    backup.check_staged_tree(self.root)
            os.unlink(path)

    def test_a_fifo_is_refused(self):
        os.mkfifo(os.path.join(self.root, "database"))
        with self.assertRaisesRegex(ValueError, "special file"):
            backup.check_staged_tree(self.root)

    def test_the_root_must_be_a_real_folder(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.root, link)
        with self.assertRaisesRegex(ValueError, "not a folder"):
            backup.check_staged_tree(link)

    def test_an_unreadable_folder_is_refused_not_skipped(self):
        hidden = os.path.join(self.root, "wp-content", "hidden")
        os.makedirs(hidden)
        os.symlink("/etc", os.path.join(hidden, "link"))
        os.chmod(hidden, 0)
        self.addCleanup(os.chmod, hidden, 0o700)
        if os.access(hidden, os.R_OK):
            self.skipTest("running as a user who can read mode-000 folders")
        with self.assertRaisesRegex(ValueError, "could not read"):
            backup.check_staged_tree(self.root)


class _ExtractorsOff:
    """Spies on both extractors; the test fails if either runs."""

    def __init__(self, test):
        p = mock.patch.object(backup, "_unzip_cli_extract",
                              side_effect=AssertionError("unzip ran"))
        test.unzip = p.start()
        test.addCleanup(p.stop)
        p = mock.patch.object(zipfile.ZipFile, "extractall",
                              side_effect=AssertionError("zipfile extracted"))
        test.extractall = p.start()
        test.addCleanup(p.stop)


class TestPythonPathsRefuseLinks(unittest.TestCase):
    """Every Python entry point, both formats, refuses a link-holding backup
    before either extractor runs, and touches nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)
        # Reuse the metadata tests' sandboxed install, docker ban and
        # key-write ban; its assert_untouched checks the install.
        self.install = mv.TestPythonRestorePaths("test_format_1_backups_are_checked_too")
        self.install.setUp()
        self.addCleanup(self.install.doCleanups)
        self.data_dir = self.install.data_dir
        self.logs = []
        self.backups = {}
        for i, (label, links) in enumerate(LINK_CASES.items()):
            _, files = _crafted_backups(self.tmp, links, f"c{i}")
            for fmt, path in files.items():
                self.backups[f"{label}, {fmt}"] = path

    def assert_untouched(self):
        self.install.assert_untouched()
        for dirpath, dirs, files in os.walk(self.tmp):
            for name in files:
                if name in ("secret.txt", "id_ed25519"):
                    self.assertTrue(dirpath.endswith((".home", ".ssh")),
                                    f"{name} copied to {dirpath}")

    def test_extract_backup_refuses_before_extracting(self):
        _ExtractorsOff(self)
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        for label, path in self.backups.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "valid OnionPress backup"):
                    backup.extract_backup(path, PW, self.logs.append, staging=dest)
                self.assertEqual(os.listdir(dest), [])
        self.unzip.assert_not_called()
        self.extractall.assert_not_called()

    def test_extract_backup_cleans_up_its_own_staging(self):
        before = set(os.listdir(tempfile.gettempdir()))
        for label, path in self.backups.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    backup.extract_backup(path, PW, self.logs.append)
        leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
                  if n.startswith("onionpress-restore-")]
        self.assertEqual(leaked, [])

    def test_restore_from_backup_refuses_before_touching_anything(self):
        _ExtractorsOff(self)
        for label, path in self.backups.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    backup.restore_from_backup(path, PW, self.logs.append,
                                               data_dir=self.data_dir)
                self.assert_untouched()

    def test_install_from_backup_refuses_before_touching_anything(self):
        _ExtractorsOff(self)
        for label, path in self.backups.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    backup.prepare_install_from_backup(
                        path, PW, self.logs.append, data_dir=self.data_dir)
                self.assert_untouched()

    def test_the_extract_backup_helper_refuses(self):
        """What the bash launchers run (restore_backup, setup --from-backup)."""
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            for label, path in self.backups.items():
                with self.subTest(label):
                    self.assertEqual(
                        cli.OnionPressCLI.cmd_extract_backup(None, path, dest, PW), 1)
                    self.assertEqual(os.listdir(dest), [])
        self.assertIn("symbolic link", err.getvalue())

    def test_a_wrong_password_on_format_2_still_says_so(self):
        path = self.backups["wp-content is a link to the home folder, format 2"]
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        with self.assertRaises(backup_crypto.WrongPasswordError):
            backup.extract_backup(path, "wrong", self.logs.append, staging=dest)
        self.assertEqual(os.listdir(dest), [])

    def test_restore_container_artifacts_checks_before_any_copy(self):
        """install-from-backup's staging sits in ~/.onionpress across a
        restart before the import copies it."""
        staging, _ = _staging_with_links(self.tmp, {"wp-content": "{home}"},
                                         name="restore-staging")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            backup.restore_container_artifacts(staging, {}, self.logs.append)


class TestEachExtractorOnLegitBackups(unittest.TestCase):
    """Real backups, unusual names included, still extract on both
    extractors and both formats; and if an extractor ever creates a link
    the member list did not show, the check after extraction catches it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)
        self.staging = mv._make_staging(self.tmp, mv._metadata())
        uploads = os.path.join(self.staging, "wp-content", "uploads", "été")
        os.makedirs(uploads)
        for name in ("café.jpg", "..dots..txt", "back\\slash.txt", "a:b.txt"):
            with open(os.path.join(uploads, name), "w") as f:
                f.write(name)

    def _backups(self):
        f1 = os.path.join(self.tmp, "f1.zip")
        subprocess.run(["zip", "-r", "-q", "-P", PW, f1, "."], cwd=self.staging,
                       check=True, capture_output=True)
        f2 = os.path.join(self.tmp, "f2.zip")
        backup.pack_backup(self.staging, f2, PW, lambda m: None)
        return {"format 1": f1, "format 2": f2}

    def _extractors(self):
        yield "zipfile", mock.patch.object(backup, "_unzip_cli_extract",
                                           return_value=False)
        if HAVE_UNZIP:
            yield "unzip", mock.patch.object(
                zipfile.ZipFile, "extractall",
                side_effect=AssertionError("fell back to zipfile"))

    def test_legit_backups_extract(self):
        for fmt, path in self._backups().items():
            for extractor, patch in self._extractors():
                if fmt == "format 2" and extractor == "unzip":
                    continue  # format 2 extracts with zipfile only
                with self.subTest(fmt, extractor=extractor):
                    dest = tempfile.mkdtemp(dir=self.tmp)
                    with patch:
                        _, meta = backup.extract_backup(path, PW, lambda m: None,
                                                        staging=dest)
                    self.assertEqual(meta["onion_address"], mv.ADDR)
                    names = []
                    for _, _, files in os.walk(os.path.join(dest, "wp-content")):
                        names += files
                    self.assertIn("..dots..txt", names)
                    self.assertEqual(len(names), 4, names)

    @unittest.skipUnless(HAVE_UNZIP, "no unzip CLI")
    def test_the_check_after_extraction_catches_what_unzip_creates(self):
        staging, _ = _staging_with_links(self.tmp, {"wp-content": "{home}"},
                                         name="evil")
        legacy = _zip_tree(staging, os.path.join(self.tmp, "evil.zip"), PW)
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        with mock.patch.object(backup, "validate_archive_members"), \
                mock.patch.object(zipfile.ZipFile, "extractall",
                                  side_effect=AssertionError("fell back")):
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                backup.extract_archive(legacy, PW, dest, lambda m: None)
        # unzip did make the link; the check refused it.
        self.assertTrue(os.path.islink(os.path.join(dest, "wp-content")))


class TestLauncherPathsRefuseLinks(unittest.TestCase):
    """restore_backup (Linux) and setup --from-backup (both launchers),
    run for real (docker stubbed) against link-holding backups of both
    formats."""

    def setUp(self):
        self.h = mv.TestLauncherRestorePaths("test_failed_setup_and_restore_stop_the_launcher")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        # Valid metadata and key, so that only the link stands between this
        # backup and `docker cp "$staging/wp-content/." ...`.
        staging, _ = _staging_with_links(self.h.tmp, {"wp-content": "{home}"},
                                         name="launcher")
        legacy = _zip_tree(staging, os.path.join(self.h.tmp, "l1.zip"), PW)
        inner = _zip_tree(staging, os.path.join(self.h.tmp, "l2-inner.zip"))
        self.backups = {"format 1": legacy,
                        "format 2": _format2(os.path.join(self.h.tmp, "l2.zip"), inner)}

    def _log(self):
        """The launcher log, emptied for the next call."""
        path = os.path.join(self.h.tmp, "log")
        with open(path) as f:
            text = f.read()
        os.unlink(path)
        return text

    def test_restore_backup_refuses(self):
        for launcher in mv.TestLauncherRestorePaths.RESTORE:
            for fmt, path in self.backups.items():
                with self.subTest(launcher=launcher, fmt=fmt):
                    data = self.h._fresh_data_dir()
                    rc, out, err = self.h._run(launcher, f'restore_backup "$PW" {path!r}')
                    self.assertNotEqual(rc, 0)
                    # macOS says so on stderr, Linux in its log (on stdout here).
                    self.assertIn("valid OnionPress backup", out + err)
                    self.assertIn("symbolic link", self._log())
                    self.h._assert_untouched(data)

    def test_setup_from_backup_refuses(self):
        for launcher, func in self.h.SETUP.items():
            for fmt, path in self.backups.items():
                with self.subTest(launcher=launcher, fmt=fmt):
                    data = self.h._fresh_data_dir()
                    rc, _, err = self.h._run(
                        launcher, f'{func} --from-backup "$PW" {path!r}')
                    self.assertNotEqual(rc, 0)
                    self.assertIn("valid OnionPress backup", err)
                    self.assertIn("symbolic link", self._log())
                    self.h._assert_untouched(data)

    def test_launchers_extract_only_through_the_helper(self):
        for launcher in mv.LAUNCHERS:
            code = mv._read(launcher)
            for func in self.h._restore_functions(launcher):
                body = "\n".join(line for line in mv._bash_function(code, func).splitlines()
                                 if not line.lstrip().startswith("#"))
                with self.subTest(launcher, func=func):
                    self.assertIn("extract-backup --archive", body)
                    self.assertNotRegex(body, r"\b(unzip|ditto|bsdtar)\b|\btar\s+-?x")


if __name__ == "__main__":
    unittest.main()
