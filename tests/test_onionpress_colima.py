"""Tests for src/onionpress/colima.py."""

import os
import shutil
import subprocess
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

    def test_start_keeps_colima_out_of_the_users_docker_and_ssh_config(self):
        # Colima's `docker context` calls edit the DOCKER_CONFIG it inherits,
        # and its start adds an Include to ~/.ssh/config unless told not to.
        out = os.path.join(self.tmpdir, "colima_call")
        _write_script(
            os.path.join(self.paths.bin_dir, "colima"),
            f'#!/bin/bash\nprintf "%s\\n%s\\n" "$DOCKER_CONFIG" "$*" > "{out}"\n',
        )
        with mock.patch.dict(os.environ, {"DOCKER_CONFIG": "/Users/someone/.docker"}):
            Colima(self.paths).start(memory=1, cpu=2)
        with open(out) as f:
            docker_config, args = f.read().splitlines()
        self.assertEqual(docker_config, self.paths.docker_config_dir)
        self.assertIn("--activate=false", args.split())
        self.assertIn("--ssh-config=false", args.split())


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

    def test_a_failed_stop_is_a_warning(self):
        _write_script(os.path.join(self.paths.bin_dir, "colima"),
                      '#!/bin/bash\necho "vm busy" >&2\nexit 1\n')
        logs = []
        Colima(self.paths, log_func=logs.append).stop()
        self.assertIn("Colima stop warning: vm busy", logs)

    def test_a_stop_that_times_out_is_logged_not_raised(self):
        logs = []
        c = Colima(self.paths, log_func=logs.append)
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(["colima", "stop"], 60)):
            c.stop()
        self.assertIn("Colima stop timed out", logs)

    def test_a_status_that_times_out_is_not_running(self):
        c = Colima(self.paths)
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(["colima", "status"], 10)):
            self.assertFalse(c.is_running())


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

    def _check(self, *answers, config="VM_MEMORY=2\nVM_CPU=2\n"):
        with open(self.paths.config_file, "w") as f:
            f.write(config)
        docker = mock.Mock()
        docker.exec.side_effect = list(answers)
        self.logs = []
        return Colima(self.paths, log_func=self.logs.append).check_vm_config_mismatch(docker)

    def test_cpu_mismatch(self):
        self.assertTrue(self._check(mock.Mock(ok=True, output="2097152"),
                                    mock.Mock(ok=True, output="4")))
        self.assertIn("VM CPU mismatch: config=2, running=4", self.logs)

    def test_unreadable_answers_are_no_mismatch(self):
        for answers in ([mock.Mock(ok=True, output="lots")],
                        [mock.Mock(ok=True, output="2097152"), mock.Mock(ok=False, output="")],
                        [mock.Mock(ok=True, output="2097152"), mock.Mock(ok=True, output="two")]):
            with self.subTest(answers=[a.output for a in answers]):
                self.assertFalse(self._check(*answers))

    def test_restart_with_config_takes_the_stack_down_first(self):
        order = []
        docker = mock.Mock()
        docker.compose.side_effect = lambda *a, **k: order.append("down")
        c = Colima(self.paths)
        with mock.patch.object(c, "stop", lambda: order.append("stop")), \
                mock.patch.object(c, "start", lambda: order.append("start")):
            c.restart_with_config(docker)
        docker.compose.assert_called_once_with(["down"], timeout=60)
        self.assertEqual(order, ["down", "stop", "start"])


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

    def _on_mac(self, initialized, running):
        from onionpress.platform import OS
        paths = _make_paths(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, os.path.dirname(paths.data_dir), True)
        _write_script(os.path.join(paths.bin_dir, "colima"), "#!/bin/bash\n")
        started = []
        with mock.patch("onionpress.colima.detect_os", return_value=OS.MACOS), \
                mock.patch.object(Colima, "initialized", initialized), \
                mock.patch.object(Colima, "is_running", return_value=running), \
                mock.patch.object(Colima, "start", lambda self: started.append(True)), \
                mock.patch("subprocess.run") as run:
            result = detect_container_runtime(paths)
        run.assert_not_called()  # the bundled Colima, never system docker
        return result, started

    def test_mac_running_vm_is_used_as_it_is(self):
        self.assertEqual(self._on_mac(initialized=True, running=True),
                         ("colima-bundled", []))

    def test_mac_stopped_vm_is_started(self):
        self.assertEqual(self._on_mac(initialized=True, running=False),
                         ("colima-bundled", [True]))

    def test_mac_first_run_starts_the_vm(self):
        self.assertEqual(self._on_mac(initialized=False, running=False),
                         ("colima-bundled", [True]))


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
