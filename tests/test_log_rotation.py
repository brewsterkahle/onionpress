#!/usr/bin/env python3
"""Tests for onionpress.log_rotation.RotatingLog — focused on the gzip
compression behavior that ships rolled logs as .log.gz."""

import gzip
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.log_rotation import RotatingLog


class TestRotatingLogCompression(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _wait_for(self, predicate, timeout=5.0):
        """Wait up to *timeout* seconds for *predicate()* to return true."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_size_rotation_produces_gz(self):
        """When a file exceeds max_size, the old file is gzipped."""
        log = RotatingLog(self.tmpdir, "testlog", max_size=200)

        # Write enough to exceed max_size
        for i in range(50):
            log.write(f"line {i:04d} " + ("x" * 20) + "\n")

        # Expect at least one .log.gz file to appear (compression runs in
        # a background thread, so wait briefly).
        def has_gz():
            return any(
                f.endswith(".log.gz")
                for f in os.listdir(self.tmpdir)
            )
        self.assertTrue(self._wait_for(has_gz),
            f"no .log.gz appeared in {os.listdir(self.tmpdir)}")

        # Gzipped file should be readable with gzip.open() and contain
        # recognizable content.
        gz_files = [f for f in os.listdir(self.tmpdir) if f.endswith(".log.gz")]
        self.assertGreater(len(gz_files), 0)
        with gzip.open(os.path.join(self.tmpdir, gz_files[0]), "rt") as gf:
            contents = gf.read()
        self.assertIn("line 00", contents)

        # The uncompressed originals should have been removed. 50 writes
        # at max_size=200 triggers several rotations; gzip runs on a
        # background thread, so settle-wait rather than sample once.
        def uncompressed_count():
            return sum(
                1 for f in os.listdir(self.tmpdir)
                if f.endswith(".log") and not f.endswith(".log.gz")
            )
        self.assertTrue(
            self._wait_for(lambda: uncompressed_count() == 1),
            f"expected 1 active .log, got "
            f"{[f for f in os.listdir(self.tmpdir) if f.endswith('.log')]}",
        )
        uncompressed = [
            f for f in os.listdir(self.tmpdir)
            if f.endswith(".log") and not f.endswith(".log.gz")
        ]
        self.assertEqual(
            uncompressed[0], os.path.basename(log.current_path())
        )

    def test_completed_files_includes_gz(self):
        """completed_files() should return rolled .log.gz entries."""
        log = RotatingLog(self.tmpdir, "testlog", max_size=200)
        for i in range(50):
            log.write(f"line {i:04d} " + ("x" * 20) + "\n")

        # Wait for compression
        self._wait_for(lambda: any(
            f.endswith(".log.gz") for f in os.listdir(self.tmpdir)
        ))

        files = log.completed_files()
        gz_entries = [f for f in files if f["name"].endswith(".log.gz")]
        self.assertGreater(len(gz_entries), 0,
            f"completed_files didn't include any .log.gz: {files}")

    def test_active_file_not_compressed(self):
        """The currently-active file must NEVER be in .gz form."""
        log = RotatingLog(self.tmpdir, "testlog", max_size=200)
        log.write("short message\n")
        current = log.current_path()
        self.assertFalse(current.endswith(".gz"),
            f"current file should not be .gz: {current}")

    def test_next_seq_skips_over_compressed(self):
        """_find_next_seq must count .log.gz files when picking the next
        sequence number. We simulate a previous run's leftover compressed
        files and verify a fresh RotatingLog picks up after them."""
        today = time.strftime("%Y-%m-%d", time.gmtime())

        # Seed: one compressed roll + one active .log at seq=003.
        for seq in (1, 2):
            gz = os.path.join(self.tmpdir, f"testlog-{today}-{seq:03d}.log.gz")
            with gzip.open(gz, "wb") as gf:
                gf.write(b"old entries\n")
        active = os.path.join(self.tmpdir, f"testlog-{today}-003.log")
        with open(active, "w") as f:
            f.write("current\n")

        log = RotatingLog(self.tmpdir, "testlog", max_size=150)
        # _find_next_seq should see 001.gz, 002.gz, 003.log → pick 003 as
        # the highest existing. Constructor then sets _seq=003, current
        # path = testlog-<today>-003.log.
        current_name = os.path.basename(log.current_path())
        self.assertEqual(current_name, f"testlog-{today}-003.log",
            f"expected to continue at seq 003, got {current_name}")

    def test_enforce_total_size_counts_gz(self):
        """Total-size cap should consider .log.gz file sizes.

        New behaviour: the soft cap only trims files that were
        successfully shipped to OnionHome, so this test marks all
        rolled files as shipped before triggering enforcement — that's
        the common case for instances with analytics sharing enabled.
        """
        from onionpress import log_rotation as lr

        log = RotatingLog(
            self.tmpdir, "testlog",
            max_size=150,
            max_total_size=500,
        )
        # Force several rotations + compressions
        for i in range(200):
            log.write(f"line {i:04d} " + ("x" * 80) + "\n")
        # Let background compression catch up. Slow CI runners need
        # noticeably more than the 2s that's plenty on a Mac.
        time.sleep(5.0)
        # Artificially backdate files so enforce_total_size's 60s guard
        # doesn't protect them all.
        for f in os.listdir(self.tmpdir):
            p = os.path.join(self.tmpdir, f)
            old = time.time() - 120
            os.utime(p, (old, old))
        # Mark the highest rolled file as shipped so every preceding
        # roll falls under the watermark and is eligible for soft-cap
        # cleanup. Without this, the unshipped-retention policy would
        # keep all rolls up to the 5× hard ceiling.
        rolled = sorted(
            f for f in os.listdir(self.tmpdir)
            if f.endswith(".log") or f.endswith(".log.gz")
        )
        if rolled:
            lr.mark_shipped(self.tmpdir, "testlog", rolled[-1])
        # Drive enforcement directly. The original test piggy-backed on
        # log.write() (which only calls _enforce_total_size when a roll
        # happens), but whether the trigger-write happens to push the
        # active file over max_size is racy and was failing on Linux CI.
        log._enforce_total_size()
        total = sum(
            os.path.getsize(os.path.join(self.tmpdir, f))
            for f in os.listdir(self.tmpdir)
            if f.endswith(".log") or f.endswith(".log.gz")
        )
        # Active file may push us slightly over the cap — allow 2×.
        self.assertLess(total, 500 * 2,
            f"total size {total} far over cap; enforcement didn't prune .gz")


if __name__ == "__main__":
    unittest.main()
