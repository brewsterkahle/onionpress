#!/usr/bin/env python3
"""Tests for onionpress.derivatives (issue #257): which upload files are
regenerable image derivatives, pruning them from a backup's staging copy,
the restore-time markers, and the marker-guarded regeneration sweep.

No Docker: every `docker` invocation goes through a mocked subprocess.run.
"""

import fcntl
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import derivatives  # noqa: E402


class TestClassification(unittest.TestCase):
    """is_regenerable_derivative(name, siblings) — WordPress's own rule: a
    `-WxH` or `-scaled` file is a derivative only when the file it was cut
    from sits in the same directory."""

    def test_size_with_base_present_is_a_derivative(self):
        self.assertTrue(derivatives.is_regenerable_derivative(
            "photo-300x200.jpg", {"photo.jpg", "photo-300x200.jpg"}))

    def test_size_without_base_is_kept(self):
        """A true original that merely ends in -WxH has nothing to be
        regenerated from — dropping it would lose it."""
        self.assertFalse(derivatives.is_regenerable_derivative(
            "screenshot-1920x1080.png", {"screenshot-1920x1080.png", "other.png"}))

    def test_scaled_with_base_present_is_a_derivative(self):
        self.assertTrue(derivatives.is_regenerable_derivative(
            "photo-scaled.jpg", {"photo.jpg", "photo-scaled.jpg"}))

    def test_scaled_without_base_is_kept(self):
        self.assertFalse(derivatives.is_regenerable_derivative(
            "photo-scaled.jpg", {"photo-scaled.jpg"}))

    def test_size_whose_only_base_is_the_scaled_copy_is_a_derivative(self):
        self.assertTrue(derivatives.is_regenerable_derivative(
            "photo-300x200.jpg", {"photo-scaled.jpg", "photo-300x200.jpg"}))

    def test_video_pdf_and_originals_are_never_derivatives(self):
        siblings = {"clip.mp4", "clip-1280x720.mp4", "doc.pdf", "photo.jpg", "song.mp3"}
        for name in siblings:
            with self.subTest(name=name):
                self.assertFalse(derivatives.is_regenerable_derivative(name, siblings))

    def test_pdf_preview_sizes_are_derivatives_of_the_preview(self):
        """WordPress renders doc.pdf to doc-pdf.jpg and cuts sizes from that."""
        siblings = {"doc.pdf", "doc-pdf.jpg", "doc-pdf-300x200.jpg"}
        self.assertTrue(derivatives.is_regenerable_derivative("doc-pdf-300x200.jpg", siblings))
        self.assertFalse(derivatives.is_regenerable_derivative("doc-pdf.jpg", siblings))

    def test_cross_format_size_counts_when_base_exists(self):
        """A format-converting plugin cuts WebP sizes from a JPEG original."""
        self.assertTrue(derivatives.is_regenerable_derivative(
            "photo-300x200.webp", {"photo.jpg", "photo-300x200.webp"}))

    def test_extension_case_is_tolerated(self):
        self.assertTrue(derivatives.is_regenerable_derivative(
            "IMG_0001-300x200.JPG", {"IMG_0001.JPG", "IMG_0001-300x200.JPG"}))

    def test_base_candidates_for_non_derivative_is_empty(self):
        self.assertEqual(derivatives.derivative_base_candidates("photo.jpg"), [])
        self.assertEqual(derivatives.derivative_base_candidates("clip.mp4"), [])


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.uploads = os.path.join(self.tmp, "uploads")
        self.files = {
            "2024/01/photo.jpg": 1000,
            "2024/01/photo-300x200.jpg": 100,
            "2024/01/photo-1024x683.jpg": 300,
            "2024/01/photo-scaled.jpg": 500,
            "2024/01/orphan-640x480.jpg": 200,
            "2024/01/clip.mp4": 5000,
            "2024/02/doc.pdf": 700,
            "2024/02/doc-pdf.jpg": 50,
            "2024/02/doc-pdf-116x150.jpg": 10,
            "sites/2/2024/03/pic.png": 400,
            "sites/2/2024/03/pic-150x150.png": 40,
        }
        for rel, size in self.files.items():
            path = os.path.join(self.uploads, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"x" * size)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remaining(self):
        out = set()
        for dirpath, _d, names in os.walk(self.uploads):
            for n in names:
                out.add(os.path.relpath(os.path.join(dirpath, n), self.uploads))
        return out

    def test_prunes_only_derivatives_with_a_base(self):
        logs = []
        removed, freed = derivatives.prune_image_derivatives(self.uploads, logs.append)
        self.assertEqual(removed, 5)
        self.assertEqual(freed, 100 + 300 + 500 + 10 + 40)
        self.assertEqual(self._remaining(), {
            "2024/01/photo.jpg",
            "2024/01/orphan-640x480.jpg",
            "2024/01/clip.mp4",
            "2024/02/doc.pdf",
            "2024/02/doc-pdf.jpg",
            "sites/2/2024/03/pic.png",
        })
        self.assertTrue(any("pruned 5" in m for m in logs), logs)

    def test_missing_uploads_dir_is_a_noop(self):
        self.assertEqual(
            derivatives.prune_image_derivatives(os.path.join(self.tmp, "nope"), None),
            (0, 0))

    def test_prune_is_idempotent(self):
        derivatives.prune_image_derivatives(self.uploads)
        before = self._remaining()
        self.assertEqual(derivatives.prune_image_derivatives(self.uploads), (0, 0))
        self.assertEqual(self._remaining(), before)


class TestMarkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schedule_writes_host_marker(self):
        data_dir = os.path.join(self.tmp, "data")
        logs = []
        path = derivatives.schedule_regeneration(
            data_dir, logs.append, {"excludes_image_derivatives": True})
        self.assertEqual(path, os.path.join(data_dir, ".regenerate-derivatives-pending"))
        self.assertTrue(os.path.exists(path))
        self.assertTrue(derivatives.host_marker_present(data_dir))
        self.assertTrue(any("original images only" in m for m in logs), logs)

    def test_schedule_for_an_older_backup_still_marks_but_says_so(self):
        data_dir = os.path.join(self.tmp, "data")
        logs = []
        derivatives.schedule_regeneration(data_dir, logs.append, {"onionpress_version": "2.4.0"})
        self.assertTrue(derivatives.host_marker_present(data_dir))
        self.assertTrue(any("predates" in m for m in logs), logs)

    def test_archive_marker_lands_in_uploads(self):
        wpc = os.path.join(self.tmp, "wp-content")
        path = derivatives.write_archive_marker(wpc)
        self.assertEqual(path, os.path.join(wpc, "uploads", ".onionpress-regenerate-derivatives"))
        self.assertTrue(os.path.isfile(path))
        # The in-container path is the same file after `docker cp wp-content/.`
        self.assertEqual(
            derivatives.CONTAINER_MARKER,
            "/var/www/html/wp-content/uploads/.onionpress-regenerate-derivatives")


class _FakeDocker:
    """Scripted subprocess.run stand-in: records every argv, answers by
    pattern. `container_marker` / `installed` / `regenerate_rc` drive the
    scenarios; `sites` is what `wp site list` prints."""

    def __init__(self, *, container_marker=False, installed=True,
                 regenerate_rc=0, sites=("http://localhost/", "http://localhost/alice/"),
                 site_list_rc=0):
        self.container_marker = container_marker
        self.installed = installed
        self.regenerate_rc = regenerate_rc
        self.sites = sites
        self.site_list_rc = site_list_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        rc, out, err = 0, "", ""
        if "test -f" in joined:
            rc = 0 if self.container_marker else 1
        elif "core is-installed" in joined:
            rc = 0 if self.installed else 1
        elif "site list" in joined:
            rc, out = self.site_list_rc, "\n".join(self.sites) + "\n"
        elif "media regenerate" in joined:
            rc = self.regenerate_rc
            out = "Success: Regenerated 12 of 12 images.\n" if rc == 0 else ""
            err = "Error: boom\n" if rc else ""
        elif "rm -f" in joined:
            self.container_marker = False
        return mock.Mock(returncode=rc, stdout=out, stderr=err)

    def regenerate_calls(self):
        return [c for c in self.calls if "regenerate" in c]


class TestRegenerateIfPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mark(self):
        derivatives.schedule_regeneration(self.data_dir)

    def _run(self, fake):
        with mock.patch("onionpress.derivatives.subprocess.run", fake):
            return derivatives.regenerate_if_pending(self.data_dir, self.logs.append)

    def test_no_marker_means_no_regeneration(self):
        fake = _FakeDocker()
        self.assertTrue(self._run(fake))
        self.assertEqual(fake.regenerate_calls(), [])
        # Only the in-container marker probe ran.
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("test", fake.calls[0])

    def test_host_marker_runs_every_site_niced_and_clears_markers(self):
        self._mark()
        fake = _FakeDocker(container_marker=True)
        self.assertTrue(self._run(fake))
        regen = fake.regenerate_calls()
        self.assertEqual(len(regen), 2)
        for call in regen:
            self.assertEqual(call[:3], ["docker", "exec", "onionpress-wordpress"])
            self.assertEqual(call[3:6], ["nice", "-n", "10"])
            self.assertIn("--only-missing", call)
            self.assertIn("--yes", call)
            self.assertIn("--allow-root", call)
        self.assertIn("--url=http://localhost/", regen[0])
        self.assertIn("--url=http://localhost/alice/", regen[1])
        # Cleared on success: both markers, after a chown of the new files.
        self.assertFalse(derivatives.host_marker_present(self.data_dir))
        self.assertFalse(fake.container_marker)
        joined = [" ".join(c) for c in fake.calls]
        chown = next(i for i, c in enumerate(joined) if "chown -R www-data:www-data" in c)
        rm = next(i for i, c in enumerate(joined) if "rm -f" in c)
        self.assertLess(chown, rm)
        self.assertTrue(any("complete" in m for m in self.logs), self.logs)

    def test_container_marker_alone_triggers_the_sweep(self):
        """A backup restored by an older OnionPress still carries the
        in-archive marker; the next start on this version acts on it."""
        fake = _FakeDocker(container_marker=True)
        self.assertTrue(self._run(fake))
        self.assertEqual(len(fake.regenerate_calls()), 2)
        self.assertFalse(fake.container_marker)

    def test_failed_regenerate_keeps_markers_for_the_next_start(self):
        self._mark()
        fake = _FakeDocker(container_marker=True, regenerate_rc=1)
        self.assertFalse(self._run(fake))
        self.assertTrue(derivatives.host_marker_present(self.data_dir))
        self.assertTrue(fake.container_marker)
        self.assertEqual(len(fake.regenerate_calls()), 1)  # stopped at the first failure
        self.assertTrue(any("resume on next start" in m for m in self.logs), self.logs)

    def test_wordpress_not_installed_keeps_marker(self):
        self._mark()
        fake = _FakeDocker(installed=False)
        self.assertFalse(self._run(fake))
        self.assertTrue(derivatives.host_marker_present(self.data_dir))
        self.assertEqual(fake.regenerate_calls(), [])

    def test_single_site_runs_once_without_url(self):
        self._mark()
        fake = _FakeDocker(sites=(), site_list_rc=1)
        self.assertTrue(self._run(fake))
        regen = fake.regenerate_calls()
        self.assertEqual(len(regen), 1)
        self.assertFalse(any(a.startswith("--url=") for a in regen[0]))

    def test_concurrent_sweep_is_not_started_twice(self):
        self._mark()
        lock_path = os.path.join(self.data_dir, derivatives.LOCK_FILE)
        with open(lock_path, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fake = _FakeDocker()
            self.assertTrue(self._run(fake))
        self.assertEqual(fake.regenerate_calls(), [])
        self.assertTrue(derivatives.host_marker_present(self.data_dir))
        self.assertTrue(any("already running" in m for m in self.logs), self.logs)


class TestSpawnRegeneration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nothing_pending_spawns_nothing(self):
        with mock.patch("onionpress.derivatives.subprocess.Popen") as popen:
            self.assertIsNone(derivatives.spawn_regeneration(self.data_dir))
        popen.assert_not_called()

    def test_pending_marker_spawns_detached_cli(self):
        derivatives.schedule_regeneration(self.data_dir)
        log_file = os.path.join(self.tmp, "onionpress.log")
        with mock.patch("onionpress.derivatives.subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            pid = derivatives.spawn_regeneration(self.data_dir, log_file=log_file)
        self.assertEqual(pid, 4242)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-1], "regenerate-derivatives")
        self.assertIn("--data-dir", argv)
        self.assertEqual(argv[argv.index("--data-dir") + 1], self.data_dir)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
        # The marker is left for the child to clear on completion.
        self.assertTrue(derivatives.host_marker_present(self.data_dir))


if __name__ == "__main__":
    unittest.main()
