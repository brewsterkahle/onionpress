#!/usr/bin/env python3
"""onionpress.launcher_ops: vanity keys from mkp224o, Tor Browser's stale
locks and liveness, and the admin password file.

What is held:

- generate_vanity_in_container refuses an empty or invalid prefix before
  anything runs (an empty filter makes mkp224o exit 0 having made nothing,
  and the scan would then return an OLD key as new); runs the pinned tor
  image with no network, every capability dropped but DAC_OVERRIDE,
  no-new-privileges and only the vanity folder mounted; returns only a
  folder named by a whole valid address with the prefix that holds a
  secret key; a failing mkp224o is None, logged with its stderr;
- cleanup_stale_tor_browser_locks removes the profile's locks only when
  no Tor Browser runs and the lock's pid is gone; a live one keeps them;
- _tor_browser_running confirms a firefox.real by its resolved binary
  (/proc/<pid>/exe under tor-browser), never by command line;
- get_admin_password: the file's stripped text, None for none or empty.

subprocess.run, /proc and HOME are stand-ins: no docker, no pgrep.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import key_manager  # noqa: E402
from onionpress import launcher_ops  # noqa: E402


def _address(seed):
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return key_manager.derive_onion_address(key_manager.derive_public_key(bytes(h)))


class TestGenerateVanity(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-vanity-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.vanity = os.path.join(self.dir, "vanity-keys")
        self.address = _address(b"vanity")
        self.prefix = self.address[:3]
        self.lines = []
        self.calls = []

    def fake_run(self, rc=0, make=(), stdout="", stderr=""):
        """mkp224o stand-in: makes the folders in *make* (name, with key?)."""
        def run(argv, **kwargs):
            self.calls.append(list(argv))
            for name, with_key in make:
                d = os.path.join(self.vanity, name)
                os.makedirs(d, exist_ok=True)
                if with_key:
                    with open(os.path.join(d, "hs_ed25519_secret_key"), "wb") as f:
                        f.write(b"k" * 96)
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)
        return run

    def generate(self, run, prefix=None, **kw):
        with mock.patch.object(launcher_ops.subprocess, "run", run):
            return launcher_ops.generate_vanity_in_container(
                self.prefix if prefix is None else prefix, self.vanity,
                image="img@sha256:abc", log_func=self.lines.append, **kw)

    def test_the_new_key_s_address_is_returned(self):
        run = self.fake_run(make=[(self.address, True)], stdout="1 key found\n")
        self.assertEqual(self.generate(run, jobs=3), self.address)
        argv = self.calls[0]
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--network"])
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--cap-add"],
                         ["DAC_OVERRIDE"])
        self.assertIn("no-new-privileges:true", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-v"],
                         [f"{self.vanity}:/out"])
        self.assertNotIn("--user", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "/usr/local/bin/mkp224o")
        self.assertEqual(argv[argv.index("--entrypoint") + 2], "img@sha256:abc")
        self.assertEqual(argv[-7:], ["-n", "1", "-j", "3", "-d", "/out", self.prefix])
        self.assertIn("mkp224o: 1 key found", self.lines)
        self.assertTrue(any("Generated address" in l for l in self.lines))

    def test_jobs_default_to_the_cpu_count(self):
        run = self.fake_run(make=[(self.address, True)])
        with mock.patch.object(launcher_ops.os, "cpu_count", return_value=None):
            self.generate(run)
        argv = self.calls[0]
        self.assertEqual(argv[argv.index("-j") + 1], "2")

    def test_an_empty_prefix_is_refused_before_docker(self):
        run = self.fake_run()
        with self.assertRaisesRegex(ValueError, "empty"):
            self.generate(run, prefix="")
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.vanity))

    def test_an_invalid_prefix_is_refused_before_docker(self):
        for bad in ("ab0", "ABC", "abcdefghij"):
            with self.subTest(prefix=bad):
                with self.assertRaises(ValueError):
                    self.generate(self.fake_run(), prefix=bad)
        self.assertEqual(self.calls, [])

    def test_a_failing_mkp224o_is_none(self):
        run = self.fake_run(rc=1, stderr="could not create directory\n")
        self.assertIsNone(self.generate(run))
        self.assertTrue(any("exit 1" in l and "could not create directory" in l
                            for l in self.lines))

    def test_only_a_whole_address_with_a_key_counts(self):
        other = _address(b"other")
        run = self.fake_run(make=[
            (self.prefix + "notanaddress.onion", True),   # not an address
            (self.address, False),                        # no secret key
            (other, True),                                # not our prefix
        ])
        if other.startswith(self.prefix):  # pragma: no cover - seeds chosen apart
            self.skipTest("seed collision")
        self.assertIsNone(self.generate(run))
        self.assertTrue(any("no " + self.prefix in l for l in self.lines))

    def test_a_file_named_like_the_address_is_not_a_key_folder(self):
        os.makedirs(self.vanity)
        with open(os.path.join(self.vanity, self.address), "w") as f:
            f.write("x")
        self.assertIsNone(self.generate(self.fake_run()))

    def test_no_log_func_is_fine(self):
        with mock.patch.object(launcher_ops.subprocess, "run",
                               self.fake_run(rc=1, stdout="x\n")):
            self.assertIsNone(launcher_ops.generate_vanity_in_container(
                self.prefix, self.vanity, image="img", jobs=1))


class TestTorBrowserRunning(unittest.TestCase):

    def running(self, pgrep_out, exes):
        real_readlink = os.readlink

        def readlink(path):
            if path.startswith("/proc/"):
                pid = path.split("/")[2]
                if pid not in exes:
                    raise FileNotFoundError(path)
                return exes[pid]
            return real_readlink(path)

        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["pgrep"], 0, stdout=pgrep_out))
        with mock.patch.object(launcher_ops.subprocess, "run", run), \
                mock.patch.object(launcher_ops.os, "readlink", readlink):
            result = launcher_ops._tor_browser_running()
        if run.call_args:
            self.assertEqual(run.call_args.args[0], ["pgrep", "-x", "firefox.real"])
        return result

    def test_the_tor_browser_binary_is_returned(self):
        tb = "/home/u/.local/share/torbrowser/tbb/x86_64/tor-browser/Browser/firefox.real"
        self.assertEqual(self.running("11\n22\n", {"11": "/usr/lib/firefox/firefox.real",
                                                   "22": tb}), tb)

    def test_an_unrelated_firefox_real_is_not_tor_browser(self):
        self.assertIsNone(self.running("11\n", {"11": "/usr/lib/firefox/firefox.real"}))

    def test_a_vanished_pid_is_skipped(self):
        self.assertIsNone(self.running("33\n", {}))

    def test_no_pgrep_is_not_running(self):
        with mock.patch.object(launcher_ops.subprocess, "run",
                               side_effect=FileNotFoundError("pgrep")):
            self.assertIsNone(launcher_ops._tor_browser_running())
        with mock.patch.object(launcher_ops.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("pgrep", 5)):
            self.assertIsNone(launcher_ops._tor_browser_running())


class TestStaleTorBrowserLocks(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="op-tbhome-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = launcher_ops._tor_browser_lock_paths()
        for p in self.paths:
            self.assertTrue(p.startswith(self.home))
        self.lock = self.paths[0]
        for p in self.paths[1:]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("")
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        os.symlink("127.0.0.1:+4242", self.lock)

    def cleanup(self, *, running=None, live_pids=()):
        real_exists = os.path.exists

        def exists(path):
            if isinstance(path, str) and path.startswith("/proc/"):
                return path.split("/")[2] in live_pids
            return real_exists(path)
        with mock.patch.object(launcher_ops, "_tor_browser_running", return_value=running), \
                mock.patch.object(launcher_ops.os.path, "exists", exists):
            return launcher_ops.cleanup_stale_tor_browser_locks()

    def present(self):
        return [p for p in self.paths if os.path.lexists(p)]

    def test_a_dead_holders_locks_are_removed(self):
        self.assertTrue(self.cleanup())
        self.assertEqual(self.present(), [])

    def test_a_running_tor_browser_keeps_its_locks(self):
        self.assertFalse(self.cleanup(running="/x/tor-browser/Browser/firefox.real"))
        self.assertEqual(self.present(), self.paths)

    def test_a_live_lock_pid_keeps_the_locks(self):
        self.assertFalse(self.cleanup(live_pids=("4242",)))
        self.assertEqual(self.present(), self.paths)

    def test_a_garbled_lock_target_is_treated_as_stale(self):
        os.remove(self.lock)
        os.symlink("garbage", self.lock)
        self.assertTrue(self.cleanup())
        self.assertEqual(self.present(), [])

    def test_nothing_to_remove_is_false(self):
        for p in self.paths:
            os.remove(p)
        self.assertFalse(self.cleanup())


class TestGetAdminPassword(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-adminpw-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, text):
        with open(os.path.join(self.dir, "wp-admin-password"), "w") as f:
            f.write(text)

    def test_the_stripped_password(self):
        self.write("  s3cret pass\n")
        self.assertEqual(launcher_ops.get_admin_password(self.dir), "s3cret pass")

    def test_none_without_the_file(self):
        self.assertIsNone(launcher_ops.get_admin_password(self.dir))

    def test_an_empty_file_is_none(self):
        self.write("\n")
        self.assertIsNone(launcher_ops.get_admin_password(self.dir))


if __name__ == "__main__":
    unittest.main()
