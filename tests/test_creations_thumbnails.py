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


class TestThumbnailsFollowNoLink(unittest.TestCase):
    """WordPress can plant links in My Creations (it is mounted rw into the
    container); the host's walk follows none of them (#460)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="onionpress-thumbs-link-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dir = os.path.join(self.root, "My Creations")
        self.outside = os.path.join(self.root, "Work")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.outside, "deep"))
        for rel in ("a.png", "deep/b.png"):
            with open(os.path.join(self.outside, rel), "w") as f:
                f.write("host file")

    def _outside_files(self):
        return sorted(os.path.relpath(os.path.join(r, f), self.outside)
                      for r, _d, fs in os.walk(self.outside) for f in fs)

    def _png(self):
        path = os.path.join(self.root, "rendered.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG thumb")
        return path

    def test_linked_thumbs_folder_stops_the_pass(self):
        os.symlink(self.outside, os.path.join(self.dir, ".thumbs"))
        with open(os.path.join(self.dir, "x.jpg"), "w") as f:
            f.write("x")
        self.assertFalse(thumbnails.thumbs_dir_ok(self.dir))
        self.assertEqual(thumbnails.scan(self.dir), ([], [], False))
        orphans = [os.path.join(self.dir, ".thumbs", "a.png"),
                   os.path.join(self.dir, ".thumbs", "deep", "b.png")]
        self.assertEqual(thumbnails.prune(self.dir, orphans), 0)
        self.assertFalse(thumbnails.install(self.dir, "x.jpg", self._png()))
        self.assertEqual(self._outside_files(), ["a.png", "deep/b.png"])

    def test_thumbs_file_is_not_a_folder(self):
        with open(os.path.join(self.dir, ".thumbs"), "w") as f:
            f.write("")
        self.assertFalse(thumbnails.thumbs_dir_ok(self.dir))

    def test_linked_source_is_no_source(self):
        os.symlink(os.path.join(self.outside, "a.png"), os.path.join(self.dir, "x.png"))
        os.symlink(self.outside, os.path.join(self.dir, "sub"))
        stale, _orphans, complete = thumbnails.scan(self.dir)
        self.assertTrue(complete)
        self.assertEqual(stale, [])
        self.assertFalse(thumbnails.source_ok(self.dir, os.path.join(self.dir, "x.png")))
        self.assertFalse(thumbnails.source_ok(
            self.dir, os.path.join(self.dir, "sub", "a.png")))

    def test_thumbnail_of_a_link_is_pruned_not_followed(self):
        os.symlink(os.path.join(self.outside, "a.png"), os.path.join(self.dir, "x.png"))
        os.makedirs(os.path.join(self.dir, ".thumbs"))
        # A thumbnail that is itself a link: removed as a link only.
        os.symlink(os.path.join(self.outside, "a.png"),
                   os.path.join(self.dir, ".thumbs", "x.png.png"))
        _stale, orphans, _complete = thumbnails.scan(self.dir)
        self.assertEqual(thumbnails.prune(self.dir, orphans), 1)
        self.assertEqual(self._outside_files(), ["a.png", "deep/b.png"])

    def test_linked_thumbs_subfolder_is_neither_walked_nor_written(self):
        os.makedirs(os.path.join(self.dir, ".thumbs"))
        os.symlink(self.outside, os.path.join(self.dir, ".thumbs", "sub"))
        os.makedirs(os.path.join(self.dir, "sub"))
        with open(os.path.join(self.dir, "sub", "foo.jpg"), "w") as f:
            f.write("x")
        stale, orphans, complete = thumbnails.scan(self.dir)
        self.assertTrue(complete)
        self.assertEqual(orphans, [])  # the link's target is not listed
        self.assertEqual([rel for _s, rel, _m in stale], ["sub/foo.jpg"])
        # Orphans naming paths through the link remove nothing.
        self.assertEqual(thumbnails.prune(self.dir, [
            os.path.join(self.dir, ".thumbs", "sub", "a.png")]), 0)
        self.assertFalse(thumbnails.install(self.dir, "sub/foo.jpg", self._png()))
        self.assertEqual(self._outside_files(), ["a.png", "deep/b.png"])

    def test_install_writes_inside_and_replaces_a_link(self):
        self.assertTrue(thumbnails.install(self.dir, "s/t/x.jpg", self._png()))
        out = os.path.join(self.dir, ".thumbs", "s", "t", "x.jpg.png")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG thumb")
        os.remove(out)
        os.symlink(os.path.join(self.outside, "a.png"), out)
        self.assertTrue(thumbnails.install(self.dir, "s/t/x.jpg", self._png()))
        self.assertFalse(os.path.islink(out))
        with open(os.path.join(self.outside, "a.png")) as f:
            self.assertEqual(f.read(), "host file")

    def test_daemon_renders_into_a_private_folder(self):
        src = _read("src/onionpress/daemon.py")
        gen = src[src.index("    def start_thumbnail_generator(self):"):]
        gen = gen[:gen.index("threading.Thread(target=generator")]
        self.assertIn("thumbnails.thumbs_dir_ok(creations_dir)", gen)
        self.assertIn("thumbnails.source_ok(creations_dir, src)", gen)
        self.assertIn("tempfile.TemporaryDirectory(", gen)
        self.assertIn("thumbnails.install(creations_dir, rel, ql_output)", gen)
        self.assertNotIn("makedirs", gen)
        self.assertNotIn("os.rename", gen)


if __name__ == "__main__":
    unittest.main()
