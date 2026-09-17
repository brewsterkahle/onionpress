"""Tests for src/onionpress/colima.py."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.platform import OnionPressPaths
from onionpress.colima import (
    Colima,
    ColimaError,
    detect_container_runtime,
    repair_foreign_socket_link,
)


def _make_paths(tmpdir):
    data_dir = os.path.join(tmpdir, "data")
    bin_dir = os.path.join(tmpdir, "bin")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(bin_dir, exist_ok=True)
    return OnionPressPaths(
        data_dir=data_dir,
        config_file=os.path.join(data_dir, "config"),
        secrets_file=os.path.join(data_dir, "secrets"),
        log_file=os.path.join(data_dir, "onionpress.log"),
        launcher_log_file=os.path.join(data_dir, "launcher.log"),
        pid_file=os.path.join(data_dir, "onionpress.pid"),
        shared_dir=os.path.join(data_dir, "shared"),
        docker_config_dir=os.path.join(data_dir, "docker-config"),
        bin_dir=bin_dir,
        docker_dir=os.path.join(data_dir, "docker"),
        colima_home=os.path.join(data_dir, "colima"),
        docker_socket=os.path.join(data_dir, "colima", "default", "docker.sock"),
        app_bundle="",
        documents_dir=os.path.join(tmpdir, "documents"),
    )


def _write_script(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


class TestColimaInit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_find_bundled_colima(self):
        _write_script(os.path.join(self.paths.bin_dir, "colima"), "#!/bin/bash\n")
        c = Colima(self.paths)
        self.assertIn("colima", c._colima_bin)

    def test_initialized_flag(self):
        _write_script(os.path.join(self.paths.bin_dir, "colima"), "#!/bin/bash\n")
        c = Colima(self.paths)
        self.assertFalse(c.initialized)

        # Create the flag
        os.makedirs(self.paths.colima_home, exist_ok=True)
        with open(os.path.join(self.paths.colima_home, ".initialized"), "w") as f:
            f.write("")
        self.assertTrue(c.initialized)


class TestColimaIsRunning(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_running(self):
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\nif [ "$1" = "status" ]; then exit 0; fi\n',
        )
        c = Colima(self.paths)
        self.assertTrue(c.is_running())

    def test_not_running(self):
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\nif [ "$1" = "status" ]; then exit 1; fi\n',
        )
        c = Colima(self.paths)
        self.assertFalse(c.is_running())

    def test_missing_binary(self):
        c = Colima(self.paths)
        # No colima binary — should return False, not crash
        self.assertFalse(c.is_running())


class TestColimaStart(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        os.makedirs(self.paths.shared_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_creates_initialized_flag(self):
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\nexit 0\n',
        )
        c = Colima(self.paths)
        self.assertFalse(c.initialized)
        c.start(memory=1, cpu=2)
        self.assertTrue(c.initialized)

    def test_start_failure_raises(self):
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\necho "error" >&2; exit 1\n',
        )
        c = Colima(self.paths)
        with self.assertRaises(ColimaError):
            c.start(memory=1, cpu=2)

    def test_start_reads_config(self):
        with open(self.paths.config_file, "w") as f:
            f.write("VM_MEMORY=4\nVM_CPU=4\n")
        # Capture args passed to colima
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\necho "$@" > /tmp/colima_test_args; exit 0\n',
        )
        c = Colima(self.paths)
        c.start()
        with open("/tmp/colima_test_args") as f:
            args = f.read()
        self.assertIn("--memory 4", args)
        self.assertIn("--cpu 4", args)
        os.unlink("/tmp/colima_test_args")

    def test_subsequent_start_no_arch_flags(self):
        # Mark as already initialized
        os.makedirs(self.paths.colima_home, exist_ok=True)
        with open(os.path.join(self.paths.colima_home, ".initialized"), "w") as f:
            f.write("")

        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\necho "$@" > /tmp/colima_test_args2; exit 0\n',
        )
        c = Colima(self.paths)
        c.start(memory=1, cpu=2)
        with open("/tmp/colima_test_args2") as f:
            args = f.read()
        # Should NOT have --arch or --vm-type on subsequent starts
        self.assertNotIn("--arch", args)
        self.assertNotIn("--vm-type", args)
        os.unlink("/tmp/colima_test_args2")


class TestColimaStop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stop(self):
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            '#!/bin/bash\nexit 0\n',
        )
        logs = []
        c = Colima(self.paths, log_func=logs.append)
        c.stop()
        self.assertTrue(any("Stopping" in l for l in logs))


class TestColimaVMConfigMismatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_mismatch(self):
        with open(self.paths.config_file, "w") as f:
            f.write("VM_MEMORY=2\nVM_CPU=4\n")

        mock_docker = mock.Mock()
        # Memory: 2GB = 2097152 kB
        mock_docker.exec.side_effect = [
            mock.Mock(ok=True, output="2097152"),  # meminfo
            mock.Mock(ok=True, output="4"),         # nproc
        ]

        c = Colima(self.paths)
        self.assertFalse(c.check_vm_config_mismatch(mock_docker))

    def test_memory_mismatch(self):
        with open(self.paths.config_file, "w") as f:
            f.write("VM_MEMORY=4\nVM_CPU=2\n")

        mock_docker = mock.Mock()
        mock_docker.exec.side_effect = [
            mock.Mock(ok=True, output="1048576"),  # 1GB
            mock.Mock(ok=True, output="2"),
        ]

        logs = []
        c = Colima(self.paths, log_func=logs.append)
        self.assertTrue(c.check_vm_config_mismatch(mock_docker))
        self.assertTrue(any("memory mismatch" in l for l in logs))

    def test_docker_exec_failure(self):
        with open(self.paths.config_file, "w") as f:
            f.write("VM_MEMORY=2\nVM_CPU=2\n")

        mock_docker = mock.Mock()
        mock_docker.exec.return_value = mock.Mock(ok=False, output="")

        c = Colima(self.paths)
        self.assertFalse(c.check_vm_config_mismatch(mock_docker))


class TestDetectContainerRuntime(unittest.TestCase):
    @mock.patch("onionpress.colima.detect_os", return_value=mock.Mock(value="linux"))
    @mock.patch("subprocess.run")
    def test_linux_system_docker(self, mock_run, mock_os):
        from onionpress.platform import OS
        mock_os.return_value = OS.LINUX
        mock_run.return_value = mock.Mock(returncode=0)

        paths = _make_paths(tempfile.mkdtemp())
        try:
            result = detect_container_runtime(paths)
            self.assertEqual(result, "docker-system")
        finally:
            shutil.rmtree(os.path.dirname(paths.data_dir), ignore_errors=True)

    @mock.patch("onionpress.colima.detect_os")
    @mock.patch("subprocess.run")
    def test_no_runtime_raises(self, mock_run, mock_os):
        from onionpress.platform import OS
        mock_os.return_value = OS.LINUX
        mock_run.side_effect = FileNotFoundError("no docker")

        paths = _make_paths(tempfile.mkdtemp())
        try:
            with self.assertRaises(ColimaError):
                detect_container_runtime(paths)
        finally:
            shutil.rmtree(os.path.dirname(paths.data_dir), ignore_errors=True)


class TestRepairForeignSocketLink(unittest.TestCase):
    """A docker.sock link must never point outside our own Colima home.

    Launchers up to v2.4.110 bridged ~/.colima/default/docker.sock into
    COLIMA_HOME when our own Lima forward was missing, which silently
    handed OnionPress the user's personal Colima VM.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        os.makedirs(os.path.dirname(self.paths.docker_socket), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _link_to(self, target):
        open(target, "w").close()
        os.symlink(target, self.paths.docker_socket)

    def test_removes_link_to_another_colima(self):
        foreign = os.path.join(self.tmpdir, "dot-colima-docker.sock")
        self._link_to(foreign)

        self.assertTrue(repair_foreign_socket_link(self.paths))
        self.assertFalse(os.path.lexists(self.paths.docker_socket))
        self.assertTrue(os.path.exists(foreign))  # not ours to delete

    def test_removes_dangling_foreign_link(self):
        os.symlink(os.path.join(self.tmpdir, "gone.sock"), self.paths.docker_socket)

        self.assertTrue(repair_foreign_socket_link(self.paths))
        self.assertFalse(os.path.lexists(self.paths.docker_socket))

    def test_keeps_link_inside_colima_home(self):
        inner = os.path.join(self.paths.colima_home, "_lima", "docker.sock")
        os.makedirs(os.path.dirname(inner), exist_ok=True)
        self._link_to(inner)

        self.assertFalse(repair_foreign_socket_link(self.paths))
        self.assertTrue(os.path.lexists(self.paths.docker_socket))

    def test_keeps_real_socket(self):
        open(self.paths.docker_socket, "w").close()

        self.assertFalse(repair_foreign_socket_link(self.paths))
        self.assertTrue(os.path.exists(self.paths.docker_socket))

    def test_absent_socket_is_not_an_error(self):
        self.assertFalse(repair_foreign_socket_link(self.paths))

    def test_logs_what_it_removed(self):
        self._link_to(os.path.join(self.tmpdir, "foreign.sock"))
        lines = []

        repair_foreign_socket_link(self.paths, log_func=lines.append)

        self.assertEqual(len(lines), 1)
        self.assertIn("foreign.sock", lines[0])


if __name__ == "__main__":
    unittest.main()
