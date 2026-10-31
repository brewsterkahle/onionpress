#!/usr/bin/env python3
"""The first run's onion service key (issue #442.5): start.KeySeed, which
both launchers' start runs through `onionpress seed-onion-key`, and the
Mac's host-side generator start.generate_vanity_on_host.

A mistake here mints a new onion address, so every branch is here: only
Docker's confirmed "no key volume" is a first run; the key goes in from the
pinned tor image with no network before anything starts Tor; a seed that
fails removes the volume it made and stops the start."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import containers, start  # noqa: E402
from onionpress.config import PortConfig  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

ADDRESS = "op2" + "a" * 53 + ".onion"
PIN = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:" + "0" * 64


class FakeDocker:
    """`volume ls` lists *volumes* (fails when *down*); a command starting
    with one of *fail* fails; `volume create`/`rm` keep the list."""

    def __init__(self, volumes=(), down=False, fail=()):
        self.volumes, self.down, self.fail = list(volumes), down, tuple(fail)
        self.calls = []

    def run(self, args, **_kw):
        args = [str(a) for a in args]
        self.calls.append(args)
        cmd = " ".join(args)
        if self.down:
            return DockerResult(1, "", "Cannot connect to the Docker daemon")
        if any(cmd.startswith(p) for p in self.fail):
            return DockerResult(1, "", "stub: fails")
        if args[:2] == ["volume", "ls"]:
            return DockerResult(0, "".join(v + "\n" for v in self.volumes), "")
        if args[:2] == ["volume", "create"] and args[2] not in self.volumes:
            self.volumes.append(args[2])
        if args[:2] == ["volume", "rm"] and args[2] in self.volumes:
            self.volumes.remove(args[2])
        if args[:2] == ["image", "inspect"]:
            return DockerResult(0, "sha256:x", "")
        return DockerResult(0, "", "")

    def runs(self):
        return [c for c in self.calls if c[:1] == ["run"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.lines = []
        self.config = os.path.join(self.data, "config")
        with open(self.config, "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        patcher = mock.patch.object(containers, "tor_image", return_value=PIN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def key(self, address=ADDRESS, ctor=True):
        folder = os.path.join(self.data, "shared", "vanity-keys", address)
        os.makedirs(folder)
        names = ["hostname", "ks_hs_id.ed25519_expanded_private"]
        if ctor:
            names += ["hs_ed25519_secret_key", "hs_ed25519_public_key"]
        for name in names:
            with open(os.path.join(folder, name), "w") as f:
                f.write(address + "\n" if name == "hostname" else "k\n")
        return folder

    def seed(self, docker, mac, **kw):
        paths = resolve_paths(data_dir=self.data, app_bundle="")
        manager = containers.ContainerManager(docker, paths, PortConfig.from_offset(0),
                                              log_func=self.lines.append, egress=lambda: None)
        return start.KeySeed(manager, mac=mac, bin_dir=kw.pop("bin_dir", "/nonexistent"),
                             **kw).run()


class TestBothPlatforms(Base):

    def test_an_unanswered_docker_is_never_a_first_run(self):
        for mac in (True, False):
            with self.subTest(mac=mac):
                self.key() if mac else None
                docker = FakeDocker(down=True)
                self.assertFalse(self.seed(docker, mac))
                self.assertEqual(self.lines[-1], "ERROR: Docker did not say whether "
                                 "onionpress-onion-keys exists; not starting")
                self.assertEqual(docker.calls, [["volume", "ls", "--format", "{{.Name}}"]])
                shutil.rmtree(os.path.join(self.data, "shared"), True)

    def test_the_seed_is_the_pinned_image_without_a_network(self):
        for mac in (True, False):
            with self.subTest(mac=mac):
                folder = self.key()
                docker = FakeDocker()
                with mock.patch.object(start, "ensure_key_formats", return_value=True):
                    self.assertTrue(self.seed(docker, mac))
                run = docker.runs()[0]
                self.assertEqual(run[:12], [
                    "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                    "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges:true",
                    "-v", "onionpress-onion-keys:/dest"])
                self.assertIn(f"type=bind,source={folder},target=/src,readonly", run)
                self.assertEqual(run[run.index("--entrypoint") + 2], PIN)
                self.assertEqual(run[-1], containers.KEY_SEED_SCRIPT)
                self.assertIn("onionpress-onion-keys", docker.volumes)
                self.assertIn(f"Onion address: {ADDRESS}", self.lines)
                shutil.rmtree(os.path.join(self.data, "shared"), True)

    def test_a_failed_seed_removes_the_volume_and_stops(self):
        for mac in (True, False):
            with self.subTest(mac=mac):
                self.key()
                docker = FakeDocker(fail=("run ",))
                with mock.patch.object(start, "ensure_key_formats", return_value=True):
                    self.assertFalse(self.seed(docker, mac))
                self.assertNotIn("onionpress-onion-keys", docker.volumes,
                                 "left behind it would end the first run with no key")
                self.assertEqual(self.lines[-1], "ERROR: Failed to copy address key to the "
                                 "onionpress-onion-keys volume; not starting")
                # Why it failed is in the log file, as the bash's
                # `>> "$LOG_FILE" 2>&1` left it (found by the live checks).
                with open(os.path.join(self.data, "onionpress.log"), encoding="utf-8") as f:
                    self.assertIn("stub: fails", f.read())
                os.unlink(os.path.join(self.data, "onionpress.log"))
                shutil.rmtree(os.path.join(self.data, "shared"), True)

    def test_a_volume_that_will_not_go_says_how_to_remove_it(self):
        self.key()
        docker = FakeDocker(fail=("run ", "volume rm"))
        self.assertFalse(self.seed(docker, True))
        self.assertIn("ERROR: could not remove the onionpress-onion-keys volume the key did "
                      "not reach; remove it (docker volume rm onionpress-onion-keys) before "
                      "the next start, or Tor will start without the key", self.lines)

    def test_no_tor_image_keeps_the_key_and_creates_nothing(self):
        for mac in (True, False):
            with self.subTest(mac=mac):
                folder = self.key()
                docker = FakeDocker()
                with mock.patch.object(containers.ContainerManager, "ensure_tor_image",
                                       return_value=None), \
                     mock.patch.object(start, "ensure_key_formats", return_value=True):
                    self.assertFalse(self.seed(docker, mac))
                self.assertIn(f"the key stays in {folder} for the next start", self.lines[-1])
                self.assertEqual(docker.volumes, [])
                self.assertTrue(os.path.isdir(folder))
                shutil.rmtree(os.path.join(self.data, "shared"), True)

    def test_a_link_or_a_bad_name_is_no_key(self):
        os.makedirs(os.path.join(self.data, "shared", "vanity-keys", "notanaddress.onion"))
        target = self.key("op2" + "b" * 53 + ".onion")
        os.symlink(target, os.path.join(self.data, "shared", "vanity-keys", ADDRESS))
        self.assertEqual(start.vanity_key_dir(self.data), target)
        shutil.rmtree(target)
        self.assertIsNone(start.vanity_key_dir(self.data))


class TestMac(Base):

    def test_not_a_first_run_does_nothing(self):
        self.key()
        docker = FakeDocker(volumes=["onionpress-onion-keys"])
        self.assertTrue(self.seed(docker, True))
        self.assertEqual(docker.runs(), [])
        self.assertEqual(self.lines, [])

    def test_a_failed_create_stops(self):
        self.key()
        docker = FakeDocker(fail=("volume create",))
        self.assertFalse(self.seed(docker, True))
        self.assertEqual(self.lines[-1], "ERROR: Failed to create the onionpress-onion-keys volume")
        self.assertEqual(docker.runs(), [])

    def test_generation_failing_goes_on_with_a_random_address(self):
        docker = FakeDocker()
        with mock.patch.object(start, "generate_vanity_on_host", return_value=None) as gen:
            self.assertTrue(self.seed(docker, True, bin_dir="/app/bin"))
        self.assertEqual(gen.call_args.args[:3], ("op2", self.data, "/app/bin"))
        self.assertEqual(self.lines[-1], "WARNING: vanity generation failed — continuing with "
                         "random onion address (see ERROR lines above)")
        self.assertEqual(docker.volumes, [])

    def test_an_imported_key_is_used_before_generating(self):
        self.key()
        with mock.patch.object(start, "generate_vanity_on_host") as gen:
            self.assertTrue(self.seed(FakeDocker(), True))
        gen.assert_not_called()
        self.assertIn(f"Using pre-imported key: {ADDRESS}", self.lines)


class TestLinux(Base):

    def test_a_first_run_without_a_key_generates_in_the_tor_image(self):
        docker = FakeDocker()
        with mock.patch.object(start, "run_cli_step", return_value=1) as step:
            self.assertTrue(self.seed(docker, False))
        self.assertEqual(step.call_args.args[0], ["generate-vanity"])
        self.assertEqual(self.lines, [
            "First run detected",
            "First run with ADDRESS_PREFIX=op2 -- checking tor image for mkp224o...",
            "Vanity generation skipped or failed; falling back to random address",
            "No imported key found -- using random onion address"])
        self.assertEqual(docker.volumes, [])

    def test_the_key_is_resynced_every_start_and_c_tor_gets_its_files(self):
        self.key()
        docker = FakeDocker(volumes=["onionpress-onion-keys"])
        with mock.patch.object(start, "ensure_key_formats", return_value=True), \
             mock.patch.object(start, "run_cli_step") as step:
            self.assertTrue(self.seed(docker, False))
        step.assert_not_called()
        runs = docker.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1][-1], containers.TOR_STATE_SEED_SCRIPT)
        self.assertEqual([runs[1][i + 1] for i, a in enumerate(runs[1]) if a == "--cap-add"],
                         ["CHOWN", "DAC_OVERRIDE", "FOWNER"])
        self.assertIn("Address key installed to tor-state volume (C Tor)", self.lines)

    def test_a_failed_resync_keeps_the_installed_key(self):
        self.key(ctor=False)
        docker = FakeDocker(volumes=["onionpress-onion-keys"], fail=("run ",))
        with mock.patch.object(start, "ensure_key_formats", return_value=False):
            self.assertTrue(self.seed(docker, False))
        self.assertIn("onionpress-onion-keys", docker.volumes)
        self.assertIn("WARNING: Failed to synchronize key formats", self.lines)
        self.assertIn("WARNING: Failed to copy address key to the onionpress-onion-keys "
                      "volume (keeping the one installed earlier)", self.lines)
        self.assertIn("WARNING: No C Tor key file (hs_ed25519_secret_key) -- C Tor will "
                      "generate a random address", self.lines)

    def test_a_failed_create_on_a_first_run_stops(self):
        self.key()
        docker = FakeDocker(fail=("volume create",))
        with mock.patch.object(start, "ensure_key_formats", return_value=True):
            self.assertFalse(self.seed(docker, False))
        self.assertEqual(docker.runs(), [])


MKP224O = """#!/bin/sh
echo "mkp224o $*"
[ -z "$FAIL" ] || exit 3
d="op2$(printf 'c%.0s' $(seq 53)).onion"
mkdir -p "$d" && echo "$d" > "$d/hostname" && echo k > "$d/hs_ed25519_secret_key"
"""


class TestGenerateOnHost(Base):

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.data, "bin")
        os.makedirs(self.bin)
        path = os.path.join(self.bin, "mkp224o")
        with open(path, "w") as f:
            f.write(MKP224O)
        os.chmod(path, stat.S_IRWXU)
        self.log_file = os.path.join(self.data, "onionpress.log")

    def generate(self):
        return start.generate_vanity_on_host("op2", self.data, self.bin, self.lines.append,
                                             self.log_file)

    def test_generates_and_derives_the_pem(self):
        stale = os.path.join(self.data, "shared", "vanity-keys", "op2" + "d" * 53 + ".onion")
        os.makedirs(stale)
        with mock.patch.object(start, "ensure_key_formats", return_value=True) as formats:
            got = self.generate()
        self.assertEqual(os.path.basename(got), "op2" + "c" * 53 + ".onion")
        formats.assert_called_once_with(got, self.log_file)
        self.assertFalse(os.path.exists(stale), "an empty folder of a crashed run goes")
        with open(self.log_file) as f:
            self.assertIn("mkp224o -d . -n 1 -t 4 op2", f.read())
        self.assertEqual(self.lines[-1], "Ensured both key formats for vanity key")

    def test_failures_are_errors_and_none(self):
        with mock.patch.dict(os.environ, {"FAIL": "1"}):
            self.assertIsNone(self.generate())
        self.assertIn("ERROR: mkp224o exited with status 3", self.lines[-1])
        os.unlink(os.path.join(self.bin, "mkp224o"))
        self.assertIsNone(self.generate())
        self.assertIn("ERROR: mkp224o not found at", self.lines[-1])

    def test_a_pem_that_cannot_be_derived_is_no_key(self):
        with mock.patch.object(start, "ensure_key_formats", return_value=False):
            self.assertIsNone(self.generate())
        self.assertIn("ERROR: ensure_key_formats failed for", self.lines[-1])


if __name__ == "__main__":
    unittest.main()
