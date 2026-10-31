#!/usr/bin/env python3
"""A key import Docker was not up for is finished at the next start
(issue #442.4): ContainerManager.apply_pending_key_import, which both
launchers' start runs through `onionpress apply-pending-key-import`.

The rule (CLAUDE.md, "A key import never loses the current key"): every
old keystore goes, each confirmed absent by Docker, before the marker is
dropped; a keystore that survives, or a Docker that does not answer,
keeps the marker and stops the start."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import cli, containers  # noqa: E402
from onionpress.config import PortConfig  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402

KEYSTORES = ["onionpress-onion-keys", "onionpress-arti-state", "onionpress-tor-state"]


class FakeDocker:
    """`volume ls` lists *volumes*; `volume rm` fails while a volume is in
    *in_use* (its holders' ids), `rm -f` of them frees it unless *stuck*;
    *ls_fails* `volume ls` calls fail first (-1: all)."""

    def __init__(self, volumes, in_use=None, stuck=(), ls_fails=0):
        self.volumes = list(volumes)
        self.in_use = dict(in_use or {})
        self.stuck, self.ls_fails = set(stuck), ls_fails
        self.calls = []

    def run(self, args, **_kw):
        args = [str(a) for a in args]
        self.calls.append(" ".join(args))
        if args[:2] == ["volume", "ls"]:
            if self.ls_fails:
                self.ls_fails -= 1 if self.ls_fails > 0 else 0
                return DockerResult(1, "", "Cannot connect to the Docker daemon")
            return DockerResult(0, "".join(v + "\n" for v in self.volumes), "")
        if args[:2] == ["volume", "rm"]:
            name = args[2]
            if self.in_use.get(name):
                return DockerResult(1, "", f"Error: volume is in use - [{name}]")
            if name in self.volumes:
                self.volumes.remove(name)
                return DockerResult(0, name + "\n", "")
            return DockerResult(1, "", f"Error: no such volume: {name}")
        if args[:2] == ["ps", "-aq"]:
            return DockerResult(0, "\n".join(self.in_use.get(args[-1][len("volume="):], [])), "")
        if args[:2] == ["rm", "-f"]:
            for vol, ids in self.in_use.items():
                if vol not in self.stuck:
                    self.in_use[vol] = [i for i in ids if i not in args[2:]]
            return DockerResult(0, "", "")
        return DockerResult(0, "", "")


class TestApplyPendingKeyImport(unittest.TestCase):

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.marker = os.path.join(self.data, ".import-key-pending")
        self.migration = os.path.join(self.data, ".key-volume-migration-pending")
        open(self.marker, "w").close()
        open(self.migration, "w").close()
        self.lines = []
        patcher = mock.patch.object(containers.platform, "detect_os", return_value=OS.MACOS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self, docker):
        paths = resolve_paths(data_dir=self.data, app_bundle="")
        manager = containers.ContainerManager(docker, paths, PortConfig.from_offset(0),
                                              log_func=self.lines.append,
                                              egress=lambda: None)
        return manager.apply_pending_key_import()

    def log_file(self):
        try:
            with open(os.path.join(self.data, "onionpress.log"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_nothing_pending_nothing_done(self):
        os.unlink(self.marker)
        docker = FakeDocker(KEYSTORES)
        self.assertTrue(self.apply(docker))
        self.assertEqual(docker.calls, [])
        self.assertTrue(os.path.exists(self.migration))

    def test_removed(self):
        docker = FakeDocker(KEYSTORES + ["onionpress-tor-keys", "other"])
        self.assertTrue(self.apply(docker))
        self.assertEqual(docker.volumes, ["other"])
        self.assertEqual([c for c in docker.calls if c.startswith("volume rm")],
                         [f"volume rm {v}" for v in KEYSTORES + ["onionpress-tor-keys"]])
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.migration),
                         "no unfinished migration survives a replaced identity")
        self.assertEqual(self.lines, ["Pending key import detected — removing old key "
                                      "and tor-state volumes..."])
        # Docker's own words go to the log file only, as the bash's did.
        self.assertIn("onionpress-onion-keys\n", self.log_file())

    def test_the_old_volume_name_is_wiped_too(self):
        """Left behind, migrate_key_volume would copy the replaced identity
        from onionpress-arti-state into the new volume."""
        docker = FakeDocker(["onionpress-arti-state"])
        self.assertTrue(self.apply(docker))
        self.assertIn("volume rm onionpress-arti-state", docker.calls)
        self.assertEqual(docker.volumes, [])

    def test_in_use_then_removed(self):
        docker = FakeDocker(KEYSTORES, in_use={"onionpress-tor-state": ["c1", "c2"]})
        self.assertTrue(self.apply(docker))
        i = docker.calls.index("ps -aq --filter volume=onionpress-tor-state")
        self.assertEqual(docker.calls[i + 1:i + 3],
                         ["rm -f c1 c2", "volume rm onionpress-tor-state"])
        self.assertIn("Volume onionpress-tor-state still in use — force-removing "
                      "containers using it...", self.lines)
        self.assertFalse(os.path.exists(self.marker))

    def test_still_there_keeps_the_marker_and_stops(self):
        docker = FakeDocker(KEYSTORES, in_use={"onionpress-tor-state": ["c1"]},
                            stuck={"onionpress-tor-state"})
        self.assertFalse(self.apply(docker))
        self.assertTrue(os.path.exists(self.marker))
        self.assertTrue(os.path.exists(self.migration))
        self.assertEqual(self.lines[-1], "ERROR: could not remove the old "
                         "onionpress-tor-state volume; keeping the pending key import "
                         "and not starting")
        self.assertNotIn("volume rm onionpress-tor-keys", docker.calls)

    def test_unanswered_keeps_the_marker_and_stops(self):
        for fails in (1, -1):
            with self.subTest(ls_fails=fails):
                docker = FakeDocker(KEYSTORES, ls_fails=fails)
                self.assertFalse(self.apply(docker))
                self.assertTrue(os.path.exists(self.marker))
                self.assertIn("ERROR: could not remove the old onionpress-onion-keys",
                              self.lines[-1])
                # It stopped at the first keystore it could not confirm.
                self.assertNotIn("volume rm onionpress-arti-state", docker.calls)

    def test_linux_words(self):
        with mock.patch.object(containers.platform, "detect_os", return_value=OS.LINUX):
            docker = FakeDocker(KEYSTORES, in_use={"onionpress-onion-keys": ["c1"]})
            self.assertTrue(self.apply(docker))
        self.assertEqual(self.lines, [
            "Pending key import detected -- removing old key and tor-state volumes...",
            "Volume onionpress-onion-keys still in use -- force-removing containers using it..."])

    def test_a_link_at_the_marker_is_not_a_pending_import(self):
        os.unlink(self.marker)
        os.symlink(os.path.join(self.data, "nowhere"), self.marker)
        docker = FakeDocker(KEYSTORES)
        self.assertTrue(self.apply(docker))
        self.assertEqual(docker.calls, [])


class TestLaunchers(unittest.TestCase):
    def test_no_launcher_keeps_a_copy(self):
        """Both launchers' start runs it inside start-containers (#442.8)."""
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(rel):
                self.assertNotIn('rm -f "$DATA_DIR/.import-key-pending"', text)
                self.assertNotIn("\napply_pending_key_import() {", text)
                self.assertIn("start-containers", text)


if __name__ == "__main__":
    unittest.main()
