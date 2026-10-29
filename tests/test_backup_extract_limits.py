#!/usr/bin/env python3
"""A restore unpacks no more than the backup lists, and a backup may list
no more than its size allows.

A zip lists each member's size, but nothing makes the data match: Apple's
`unzip` writes past a member's listed size (exit 0), and deflate expands up
to ~1000x. A backup file is whoever made it's, and the Settings page's
restore takes one from the site itself. These tests hand extract_archive
archives that list too much, list too many members, or lie about a
member's size, in both backup formats, and check what lands on disk.

No docker.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, backup_archive  # noqa: E402
import test_backup_archive_members as am  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402

PW = mv.PW
HAVE_ZIP_TOOLS = shutil.which("zip") is not None and shutil.which("unzip") is not None
BIG = 8 << 20          # what the lying member really holds
LISTED = 1024          # what it lists


def _misstate_size(path, name, size):
    """Rewrite the uncompressed size *path*'s zip lists for member *name*
    (central directory, and the local header unless a data descriptor
    carries it) to *size*."""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(name)
    with open(path, "rb") as f:
        data = bytearray(f.read())
    local = info.header_offset
    assert data[local:local + 4] == b"PK\x03\x04"
    if not info.flag_bits & 0x08:
        struct.pack_into("<I", data, local + 22, size)
    pos = data.find(b"PK\x01\x02")
    while pos != -1:
        name_len = struct.unpack_from("<H", data, pos + 28)[0]
        if data[pos + 46:pos + 46 + name_len] == name.encode():
            struct.pack_into("<I", data, pos + 24, size)
            break
        pos = data.find(b"PK\x01\x02", pos + 4)
    else:
        raise AssertionError(f"{name} not in the central directory")
    with open(path, "wb") as f:
        f.write(bytes(data))


def _largest_file(root):
    sizes = [os.lstat(os.path.join(d, n)).st_size
             for d, _, files in os.walk(root) for n in files]
    return max(sizes, default=0)


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)
        self.logs = []

    def _dest(self):
        return tempfile.mkdtemp(dir=self.tmp, prefix="dest-")

    def _liar_tree(self):
        """A folder with a small file and BIG bytes of zeros."""
        tree = tempfile.mkdtemp(dir=self.tmp, prefix="tree-")
        with open(os.path.join(tree, "small.txt"), "wb") as f:
            f.write(b"hello\n")
        with open(os.path.join(tree, "big.bin"), "wb") as f:
            f.write(b"\0" * BIG)
        return tree

    def _format1(self, tree, name):
        out = os.path.join(self.tmp, name)
        subprocess.run(["zip", "-r", "-q", "-P", PW, out, "."], cwd=tree,
                       check=True, capture_output=True)
        return out

    def _format2(self, tree, name):
        inner = os.path.join(self.tmp, name + "-inner.zip")
        subprocess.run(["zip", "-r", "-q", inner, "."], cwd=tree,
                       check=True, capture_output=True)
        return inner


class TestListedLimits(_Base):
    """check_archive_size refuses before either extractor runs."""

    def _both_formats(self, tree, tag):
        f1 = self._format1(tree, f"{tag}-1.zip")
        f2 = am._format2(os.path.join(self.tmp, f"{tag}-2.zip"),
                         self._format2(tree, tag))
        return {"format 1": f1, "format 2": f2}

    def test_too_many_members(self):
        tree = tempfile.mkdtemp(dir=self.tmp)
        for i in range(6):
            with open(os.path.join(tree, f"f{i}"), "w") as f:
                f.write("x")
        am._ExtractorsOff(self)
        with mock.patch.object(backup_archive, "_MAX_ARCHIVE_MEMBERS", 5):
            for fmt, path in self._both_formats(tree, "many").items():
                with self.subTest(fmt):
                    dest = self._dest()
                    with self.assertRaisesRegex(ValueError, "more than 5 files"):
                        backup.extract_archive(path, PW, dest, self.logs.append)
                    self.assertEqual(os.listdir(dest), [])

    def test_too_much_listed_data(self):
        am._ExtractorsOff(self)
        with mock.patch.object(backup_archive, "_MIN_UNPACK_BUDGET", 1 << 20), \
                mock.patch.object(backup_archive, "_MAX_EXPANSION", 2):
            for fmt, path in self._both_formats(self._liar_tree(), "big").items():
                with self.subTest(fmt):
                    dest = self._dest()
                    with self.assertRaisesRegex(ValueError, "would unpack to 8.0 MB"):
                        backup.extract_archive(path, PW, dest, self.logs.append)
                    self.assertEqual(os.listdir(dest), [])

    def test_the_budget(self):
        def listing(*sizes):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, size in enumerate(sizes):
                    zf.writestr(f"f{i}", b"\0" * size)
            return zipfile.ZipFile(io.BytesIO(buf.getvalue()))
        with mock.patch.object(backup_archive, "_MIN_UNPACK_BUDGET", 1000):
            self.assertEqual(backup.check_archive_size(listing(400, 600), 10), (1000, 600))
            # 50x the archive's size when that is larger than the floor.
            self.assertEqual(backup.check_archive_size(listing(1500), 30), (1500, 1500))
            with self.assertRaises(ValueError):
                backup.check_archive_size(listing(1001), 10)
        self.assertEqual(backup.check_archive_size(listing(), 0), (0, 0))

    def test_the_shipped_limits_take_any_real_backup(self):
        """An 8 GiB floor, 50x the file's size, half a million files."""
        self.assertGreaterEqual(backup._MIN_UNPACK_BUDGET, 8 << 30)
        self.assertGreaterEqual(backup._MAX_EXPANSION, 50)
        self.assertGreaterEqual(backup._MAX_ARCHIVE_MEMBERS, 500_000)


@unittest.skipUnless(HAVE_ZIP_TOOLS, "no zip/unzip CLI")
class TestLyingSizes(_Base):
    """A member holding BIG bytes while it lists LISTED."""

    def test_format_2_writes_no_more_than_listed_and_never_runs_unzip(self):
        inner = self._format2(self._liar_tree(), "liar")
        _misstate_size(inner, "big.bin", LISTED)
        path = am._format2(os.path.join(self.tmp, "liar-2.zip"), inner)
        dest = self._dest()
        with mock.patch.object(backup_archive, "_unzip_cli_extract",
                               side_effect=AssertionError("unzip ran")):
            with self.assertRaisesRegex(ValueError, "does not match its listed size"):
                backup.extract_archive(path, PW, dest, self.logs.append)
        self.assertLessEqual(_largest_file(dest), LISTED)

    def test_format_1_unzip_is_held_to_the_largest_listed_size(self):
        path = self._format1(self._liar_tree(), "liar-1.zip")
        _misstate_size(path, "big.bin", LISTED)
        dest = self._dest()
        with mock.patch.object(zipfile.ZipFile, "extractall",
                               side_effect=AssertionError("fell back to zipfile")):
            with self.assertRaisesRegex(ValueError, "more data than it lists"):
                backup.extract_archive(path, PW, dest, self.logs.append)
        # ulimit -f: LISTED rounded up to 512-byte blocks, at most doubled.
        self.assertLessEqual(_largest_file(dest), 4 * LISTED)

    def test_format_1_zipfile_fallback_writes_no_more_than_listed(self):
        path = self._format1(self._liar_tree(), "liar-1z.zip")
        _misstate_size(path, "big.bin", LISTED)
        dest = self._dest()
        with mock.patch.object(backup_archive, "_unzip_cli_extract", return_value=False):
            with self.assertRaises(ValueError):
                backup.extract_archive(path, PW, dest, self.logs.append)
        self.assertLessEqual(_largest_file(dest), LISTED)

    def test_extract_backup_leaves_nothing_behind(self):
        path = self._format1(self._liar_tree(), "liar-1c.zip")
        _misstate_size(path, "big.bin", LISTED)
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(ValueError):
            backup.extract_backup(path, PW, self.logs.append)
        leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
                  if n.startswith("onionpress-restore-")]
        self.assertEqual(leaked, [])

    def test_honest_backups_still_use_unzip_for_format_1(self):
        tree = self._liar_tree()
        path = self._format1(tree, "honest-1.zip")
        dest = self._dest()
        with mock.patch.object(zipfile.ZipFile, "extractall",
                               side_effect=AssertionError("fell back to zipfile")):
            backup.extract_archive(path, PW, dest, self.logs.append)
        self.assertEqual(os.path.getsize(os.path.join(dest, "big.bin")), BIG)


class TestUnzipWatchdog(_Base):
    """_unzip_cli_extract stops unzip once the disk has lost more than the
    archive lists, whatever each file's size limit allowed."""

    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "unzip"), "w") as f:
            f.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(os.path.join(bin_dir, "unzip"), 0o755)
        p = mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        p.start()
        self.addCleanup(p.stop)

    def test_unzip_is_stopped_past_the_total(self):
        free = iter([10 << 30] + [(10 << 30) - 5000] * 1000)
        start = time.monotonic()
        with mock.patch.object(backup_archive, "_free_bytes", side_effect=lambda _p: next(free)):
            with self.assertRaisesRegex(ValueError, "more data than it lists"):
                backup._unzip_cli_extract("/nonexistent.zip", PW, self._dest(),
                                          self.logs.append, max_file_bytes=10,
                                          max_total_bytes=4999)
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(any("stopped it" in m for m in self.logs), self.logs)
        for line in self.logs:
            self.assertNotIn(PW, line)

    def test_the_file_size_limit_reaches_unzip(self):
        """The ulimit wrapper passes the limit and the argv through intact."""
        with open(os.path.join(self.tmp, "bin", "unzip"), "w") as f:
            f.write('#!/bin/sh\n{ ulimit -f; printf "%s\\n" "$@"; } > "$OUT"\n')
        out = os.path.join(self.tmp, "out")
        dest = self._dest()
        with mock.patch.dict(os.environ, {"OUT": out}):
            self.assertTrue(backup._unzip_cli_extract(
                "/b.zip", "p'w; $(x)", dest, self.logs.append,
                max_file_bytes=1 << 20, max_total_bytes=1 << 40))
        with open(out) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], str((1 << 20) // 512 + 2))
        self.assertEqual(lines[1:], ["-o", "-qq", "-P", "p'w; $(x)", "/b.zip", "-d", dest])


class TestMetadataSize(_Base):
    """metadata.json is read whole into memory: never one listing more than
    _MAX_METADATA_BYTES."""

    def _bloated(self):
        big = json.dumps(dict(mv._metadata(), pad="x" * (2 << 20)))
        tree = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(tree, "metadata.json"), "w") as f:
            f.write(big)
        return tree

    @unittest.skipUnless(HAVE_ZIP_TOOLS, "no zip CLI")
    def test_format_1_readers_refuse(self):
        path = self._format1(self._bloated(), "meta.zip")
        with mock.patch.object(zipfile.ZipFile, "read",
                               side_effect=AssertionError("read it")):
            for reader in (backup.read_backup_metadata, backup.peek_backup_metadata):
                with self.subTest(reader.__name__):
                    with self.assertRaisesRegex(ValueError, "metadata.json is too large"):
                        reader(path, PW)

    def test_the_staged_reader_refuses(self):
        with self.assertRaisesRegex(ValueError, "metadata.json is too large"):
            backup.read_staged_metadata(self._bloated())


if __name__ == "__main__":
    unittest.main()
