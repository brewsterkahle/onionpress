"""Tests for src/onionpress/platform.py."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.platform import (
    OS, Arch, OnionPressPaths,
    detect_os, detect_arch, detect_timezone, resolve_paths, find_app_bundle, linux_docker_dir,
)


class TestDetectOS(unittest.TestCase):
    @mock.patch("platform.system", return_value="Darwin")
    def test_macos(self, _):
        self.assertEqual(detect_os(), OS.MACOS)

    @mock.patch("platform.system", return_value="Linux")
    def test_linux(self, _):
        self.assertEqual(detect_os(), OS.LINUX)

    @mock.patch("platform.system", return_value="Windows")
    def test_unsupported(self, _):
        with self.assertRaises(RuntimeError):
            detect_os()


class TestDetectArch(unittest.TestCase):
    @mock.patch("platform.system", return_value="Darwin")
    @mock.patch("subprocess.run")
    def test_macos_arm64(self, mock_run, _):
        mock_run.return_value = mock.Mock(returncode=0, stdout="1\n")
        self.assertEqual(detect_arch(), Arch.ARM64)

    @mock.patch("platform.system", return_value="Darwin")
    @mock.patch("platform.machine", return_value="x86_64")
    @mock.patch("subprocess.run")
    def test_macos_x86(self, mock_run, _machine, _system):
        mock_run.return_value = mock.Mock(returncode=1, stdout="")
        self.assertEqual(detect_arch(), Arch.X86_64)

    @mock.patch("platform.system", return_value="Linux")
    @mock.patch("platform.machine", return_value="aarch64")
    def test_linux_arm64(self, _machine, _system):
        self.assertEqual(detect_arch(), Arch.ARM64)

    @mock.patch("platform.system", return_value="Linux")
    @mock.patch("platform.machine", return_value="x86_64")
    def test_linux_x86(self, _machine, _system):
        self.assertEqual(detect_arch(), Arch.X86_64)


class TestDetectTimezone(unittest.TestCase):
    def test_returns_string(self):
        tz = detect_timezone()
        self.assertIsInstance(tz, str)
        self.assertTrue(len(tz) > 0)

    @mock.patch("os.readlink", side_effect=OSError("no symlink"))
    @mock.patch("builtins.open", side_effect=OSError("no file"))
    def test_fallback_utc(self, _open, _readlink):
        self.assertEqual(detect_timezone(), "UTC")

    @mock.patch("os.readlink", return_value="/usr/share/zoneinfo/America/New_York")
    def test_parses_symlink(self, _):
        self.assertEqual(detect_timezone(), "America/New_York")


class TestResolvePaths(unittest.TestCase):
    def _assert_platform_independent_paths(self, paths, data_dir):
        """Paths derived purely from data_dir — same on every OS."""
        self.assertEqual(paths.data_dir, data_dir)
        self.assertEqual(paths.config_file, os.path.join(data_dir, "config"))
        self.assertEqual(paths.secrets_file, os.path.join(data_dir, "secrets"))
        self.assertEqual(paths.log_file, os.path.join(data_dir, "onionpress.log"))
        self.assertEqual(paths.pid_file, os.path.join(data_dir, "onionpress.pid"))
        self.assertEqual(paths.colima_home, os.path.join(data_dir, "colima"))

    @mock.patch("onionpress.platform.detect_os", return_value=OS.MACOS)
    def test_default_paths_macos(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(
                paths.docker_socket,
                os.path.join(data_dir, "colima", "default", "docker.sock"),
            )

    @mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX)
    @mock.patch("os.path.exists", return_value=True)
    @mock.patch("os.getuid", return_value=1000)
    def test_default_paths_linux_rootless(self, _uid, _exists, _os):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(paths.docker_socket, "/run/user/1000/docker.sock")

    @mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX)
    @mock.patch("os.path.exists", return_value=False)
    def test_default_paths_linux_rootful_fallback(self, _exists, _os):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(paths.docker_socket, "/var/run/docker.sock")

    def test_with_app_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = os.path.join(tmpdir, "OnionPress.app")
            os.makedirs(os.path.join(app, "Contents", "Resources", "bin"))
            os.makedirs(os.path.join(app, "Contents", "Resources", "docker"))

            paths = resolve_paths(data_dir=tmpdir, app_bundle=app)
            self.assertEqual(paths.app_bundle, app)
            self.assertEqual(
                paths.bin_dir,
                os.path.join(app, "Contents", "Resources", "bin"),
            )
            self.assertEqual(
                paths.docker_dir,
                os.path.join(app, "Contents", "Resources", "docker"),
            )

    @mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX)
    def test_linux_takes_the_install_trees_docker_dir(self, _os):
        """#508: without an app bundle, Linux's compose files are the
        install tree's; no command depends on the current folder."""
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("onionpress.platform.linux_docker_dir",
                        return_value=os.path.join(tmpdir, "docker")):
            # app_bundle="": None would find a Mac's /Applications/OnionPress.app.
            paths = resolve_paths(data_dir=os.path.join(tmpdir, ".onionpress"), app_bundle="")
            self.assertEqual(paths.docker_dir, os.path.join(tmpdir, "docker"))

    @mock.patch("onionpress.platform.detect_os", return_value=OS.MACOS)
    def test_mac_without_a_bundle_has_none(self, _os):
        with mock.patch("onionpress.platform.linux_docker_dir") as linux:
            paths = resolve_paths(data_dir="/tmp/test", app_bundle="")
        self.assertEqual(paths.docker_dir, "")
        linux.assert_not_called()

    def test_no_app_bundle(self):
        paths = resolve_paths(data_dir="/tmp/test", app_bundle=None)
        # bin_dir and docker_dir should be empty when no bundle found
        # (find_app_bundle may find the real one, so test with explicit None)
        # Just verify paths object is created successfully
        self.assertIsInstance(paths, OnionPressPaths)

    def test_frozen_dataclass(self):
        paths = resolve_paths(data_dir="/tmp/test", app_bundle=None)
        with self.assertRaises(AttributeError):
            paths.data_dir = "/other"


class TestFindAppBundle(unittest.TestCase):
    def test_returns_string_or_none(self):
        result = find_app_bundle()
        self.assertTrue(result is None or isinstance(result, str))


if __name__ == "__main__":
    unittest.main()


class TestLinuxDockerDir(unittest.TestCase):
    """platform.linux_docker_dir (#508): the .deb's tree, the launcher's
    override, a checkout, or nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def _compose(self, *parts):
        folder = os.path.join(self.tmp, *parts)
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "docker-compose.yml"), "w").close()
        return folder

    def test_the_installed_tree(self):
        package = os.path.join(self.tmp, "opt", "lib", "onionpress")
        os.makedirs(package)
        self.assertEqual(linux_docker_dir(package, environ={}), "")
        docker = self._compose("opt", "docker")
        self.assertEqual(linux_docker_dir(package, environ={}), docker)

    def test_the_launchers_override_wins(self):
        package = os.path.join(self.tmp, "opt", "lib", "onionpress")
        os.makedirs(package)
        self._compose("opt", "docker")
        other = self._compose("elsewhere", "docker")
        env = {"ONIONPRESS_INSTALL_DIR": os.path.join(self.tmp, "elsewhere")}
        self.assertEqual(linux_docker_dir(package, environ=env), other)
        # An override without compose files falls through to the tree.
        env = {"ONIONPRESS_INSTALL_DIR": os.path.join(self.tmp, "nowhere")}
        self.assertEqual(linux_docker_dir(package, environ=env),
                         os.path.join(self.tmp, "opt", "docker"))

    def test_a_checkout(self):
        package = os.path.join(self.tmp, "repo", "src", "onionpress")
        os.makedirs(package)
        docker = self._compose("repo", "app", "Resources", "docker")
        self.assertEqual(linux_docker_dir(package, environ={}), docker)

    def test_this_checkout(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(linux_docker_dir(environ={}),
                         os.path.join(here, "app", "Resources", "docker"))
