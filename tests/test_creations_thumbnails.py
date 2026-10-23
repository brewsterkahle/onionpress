#!/usr/bin/env python3
"""My Creations thumbnails on the Mac (#319).

The generator started only from Backup... (a leftover of keeping macOS's
Documents prompt away while Creations lived in ~/Documents), so most
sessions made no thumbnails and every Creations page loaded the full-size
originals. It starts at launch now. It also never removed the thumbnail of
a deleted file; onionpress.thumbnails.scan() finds those (never when part of
the folder could not be read) and prune() removes them.
"""

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                      "onionpress-creations", "onionpress-creations.php")
MENUBAR = os.path.join(PROJECT_ROOT, "src", "menubar.py")
SETUP = os.path.join(PROJECT_ROOT, "setup.py")

sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
from onionpress import thumbnails  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestMacThumbnailsStartAtLaunch(unittest.TestCase):
    def test_started_in_init_before_auto_start(self):
        # The generator runs in the daemon (onionpress.daemon, #367), from
        # its start, beside the status loop.
        src = _read("src/onionpress/daemon.py")
        run = src[src.index("    def run(self):"):src.index("    def _main_loop_once(")]
        self.assertIn("self.start_thumbnail_generator()", run)
        self.assertNotIn("start_thumbnail_generator", _read(MENUBAR))

    def test_generator_prunes_and_module_is_bundled(self):
        src = _read("src/onionpress/daemon.py")
        self.assertIn("thumbnails.prune(creations_dir, orphans)", src)
        self.assertIn("'onionpress.thumbnails'", _read(SETUP))


class TestThumbnailScan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="onionpress-thumbs-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dir = os.path.join(self.root, "My Creations")
        os.makedirs(self.dir)

    def _file(self, rel, mtime=1_000_000):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_stale_and_orphans(self):
        self._file("a.jpg", 2_000_000)
        self._file("sub/b.mov", 2_000_000)
        self._file("fresh.png", 1_000_000)
        self._file("notes.txt")
        self._file(".git/c.jpg")
        self._file("Blog-Archives/d.jpg")
        self._file("sub/blog-archives/e.jpg")
        # Thumbnails: one fresh, one outdated, three of files that are gone.
        self._file(".thumbs/fresh.png.png", 1_500_000)
        self._file(".thumbs/a.jpg.png", 1_500_000)
        self._file(".thumbs/gone.jpg.png")
        self._file(".thumbs/old/deep/x.mov.png")
        self._file(".thumbs/.git/c.jpg.png")
        self._file(".thumbs/notes.txt.png")  # file still there: kept

        stale, orphans, complete = thumbnails.scan(self.dir)
        self.assertTrue(complete)
        self.assertEqual(sorted(rel for _src, rel, _m in stale),
                         ["a.jpg", "sub/b.mov", "sub/blog-archives/e.jpg"])
        t = os.path.join(self.dir, ".thumbs")
        self.assertEqual(sorted(orphans), sorted([
            os.path.join(t, ".git", "c.jpg.png"),
            os.path.join(t, "gone.jpg.png"),
            os.path.join(t, "old", "deep", "x.mov.png"),
        ]))

        self.assertEqual(thumbnails.prune(self.dir, orphans), 3)
        self.assertFalse(os.path.exists(os.path.join(t, "old")))
        self.assertFalse(os.path.exists(os.path.join(t, ".git")))
        self.assertTrue(os.path.exists(os.path.join(t, "fresh.png.png")))
        self.assertTrue(os.path.exists(os.path.join(t, "notes.txt.png")))
        self.assertTrue(os.path.isdir(t))

    def test_unreadable_folder_prunes_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("root reads every folder")
        self._file("locked/a.jpg")
        self._file(".thumbs/locked/a.jpg.png")
        locked = os.path.join(self.dir, "locked")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        _stale, orphans, complete = thumbnails.scan(self.dir)
        self.assertFalse(complete)
        self.assertEqual(orphans, [])

    def test_prune_stays_inside_thumbs(self):
        outside = self._file("keep.jpg")
        self.assertEqual(thumbnails.prune(self.dir, [outside, outside + ".png"]), 0)
        self.assertTrue(os.path.exists(outside))

    def test_thumb_path_matches_the_plugin(self):
        self.assertEqual(thumbnails.thumb_path("/c", "sub/a.mov"), "/c/.thumbs/sub/a.mov.png")
        self.assertIn("const THUMBS_PREFIX = '.thumbs/';", _read(PLUGIN))


if __name__ == "__main__":
    unittest.main()
