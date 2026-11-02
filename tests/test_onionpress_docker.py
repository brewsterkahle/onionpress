"""Tests for src/onionpress/docker.py."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.platform import resolve_paths
from onionpress.docker import Docker, DockerResult, DockerError


class TestDockerResult(unittest.TestCase):
    def test_ok_success(self):
        r = DockerResult(returncode=0, stdout="hello\n", stderr="")
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "hello")

    def test_ok_failure(self):
        r = DockerResult(returncode=1, stdout="", stderr="error")
        self.assertFalse(r.ok)

    def test_ok_timeout(self):
        r = DockerResult(returncode=-1, stdout="", stderr="", timed_out=True)
        self.assertFalse(r.ok)

    def test_output_strips_trailing(self):
        r = DockerResult(returncode=0, stdout="  data  \n\n", stderr="")
        self.assertEqual(r.output, "  data")


class FakeDockerTestCase(unittest.TestCase):
    """Base class that sets up a fake docker binary in a temp dir."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fake_bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.fake_bin)
        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.data_dir)

        # Create fake docker
        self.fake_docker = os.path.join(self.fake_bin, "docker")
        self._write_fake_docker('#!/bin/bash\necho "fake-docker $@"\n')

        self.paths = resolve_paths(data_dir=self.data_dir, app_bundle=None)
        # Override bin_dir to point to our fake
        # We need to make a new paths with the right bin_dir
        self.paths = resolve_paths.__wrapped__(self.data_dir, None) if hasattr(resolve_paths, '__wrapped__') else self._make_paths()

        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.fake_bin}:{self.orig_path}"

    def _make_paths(self):
        """Create paths with our fake bin dir."""
        from onionpress.platform import OnionPressPaths
        return OnionPressPaths(
            data_dir=self.data_dir,
            documents_dir=os.path.join(self.data_dir, "documents"),
            config_file=os.path.join(self.data_dir, "config"),
            secrets_file=os.path.join(self.data_dir, "secrets"),
            log_file=os.path.join(self.data_dir, "onionpress.log"),
            launcher_log_file=os.path.join(self.data_dir, "launcher.log"),
            pid_file=os.path.join(self.data_dir, "onionpress.pid"),
            shared_dir=os.path.join(self.data_dir, "shared"),
            docker_config_dir=os.path.join(self.data_dir, "docker-config"),
            bin_dir=self.fake_bin,
            docker_dir=os.path.join(self.data_dir, "docker"),
            colima_home=os.path.join(self.data_dir, "colima"),
            docker_socket=os.path.join(self.data_dir, "colima", "default", "docker.sock"),
            app_bundle="",
        )

    def _write_fake_docker(self, script):
        with open(self.fake_docker, "w") as f:
            f.write(script)
        os.chmod(self.fake_docker, 0o755)

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestDockerRun(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_basic_run(self):
        self._write_fake_docker('#!/bin/bash\necho "container-list"\n')
        d = Docker(self.paths)
        result = d.run(["ps"])
        self.assertTrue(result.ok)
        self.assertIn("container-list", result.output)

    def test_run_failure(self):
        self._write_fake_docker('#!/bin/bash\necho "error" >&2; exit 1\n')
        d = Docker(self.paths)
        result = d.run(["ps"])
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 1)

    def test_run_check_raises(self):
        self._write_fake_docker('#!/bin/bash\nexit 1\n')
        d = Docker(self.paths)
        with self.assertRaises(DockerError):
            d.run(["ps"], check=True)

    def test_run_timeout(self):
        self._write_fake_docker('#!/bin/bash\nsleep 10\n')
        d = Docker(self.paths)
        result = d.run(["ps"], timeout=1)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_logging(self):
        # Docker.run only logs on failure now (unless quiet=True), to keep
        # happy-path container operations from spamming launcher.log. The
        # test verifies log_func is wired through by triggering a failure.
        self._write_fake_docker('#!/bin/bash\nexit 3\n')
        logs = []
        d = Docker(self.paths, log_func=logs.append)
        d.run(["ps", "-a"])
        self.assertEqual(len(logs), 1)
        self.assertIn("ps -a", logs[0])
        self.assertIn("FAILED", logs[0])

    def test_logging_quiet_suppresses_failure_log(self):
        self._write_fake_docker('#!/bin/bash\nexit 3\n')
        logs = []
        d = Docker(self.paths, log_func=logs.append)
        d.run(["ps", "-a"], quiet=True)
        self.assertEqual(logs, [],
                         "quiet=True must suppress the failure log")

    def test_logging_success_is_silent(self):
        self._write_fake_docker('#!/bin/bash\nexit 0\n')
        logs = []
        d = Docker(self.paths, log_func=logs.append)
        d.run(["ps", "-a"])
        self.assertEqual(logs, [],
                         "successful docker runs must not log (would "
                         "flood launcher.log during normal operation)")


class TestDockerExec(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_exec_with_list(self):
        self._write_fake_docker('#!/bin/bash\necho "$@"\n')
        d = Docker(self.paths)
        result = d.exec("mycontainer", ["cat", "/etc/hostname"])
        self.assertTrue(result.ok)
        self.assertIn("exec mycontainer cat /etc/hostname", result.output)

    def test_exec_with_string(self):
        self._write_fake_docker('#!/bin/bash\necho "$@"\n')
        d = Docker(self.paths)
        result = d.exec("mycontainer", "echo hello")
        self.assertTrue(result.ok)
        self.assertIn("exec mycontainer sh -c echo hello", result.output)


class TestDockerCompose(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_compose_up(self):
        self._write_fake_docker('#!/bin/bash\necho "$@"\n')
        d = Docker(self.paths)
        result = d.compose(["up", "-d"])
        self.assertTrue(result.ok)
        self.assertIn("compose up -d", result.output)

    def test_compose_with_files(self):
        self._write_fake_docker('#!/bin/bash\necho "$@"\n')
        d = Docker(self.paths)
        result = d.compose(["up"], compose_files=["/path/to/docker-compose.yml"])
        self.assertTrue(result.ok)
        self.assertIn("-f /path/to/docker-compose.yml", result.output)


class TestContainerRunning(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_running(self):
        self._write_fake_docker('#!/bin/bash\necho "true"\n')
        d = Docker(self.paths)
        self.assertTrue(d.container_running("mycontainer"))

    def test_not_running(self):
        self._write_fake_docker('#!/bin/bash\necho "false"\n')
        d = Docker(self.paths)
        self.assertFalse(d.container_running("mycontainer"))

    def test_error(self):
        self._write_fake_docker('#!/bin/bash\nexit 1\n')
        d = Docker(self.paths)
        self.assertFalse(d.container_running("mycontainer"))


class TestComposePs(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_parse_json(self):
        svc = json.dumps({"Name": "onionpress-tor", "State": "running"})
        self._write_fake_docker(f'#!/bin/bash\necho \'{svc}\'\n')
        d = Docker(self.paths)
        services = d.compose_ps()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["Name"], "onionpress-tor")

    def test_empty_output(self):
        self._write_fake_docker('#!/bin/bash\nexit 1\n')
        d = Docker(self.paths)
        self.assertEqual(d.compose_ps(), [])


class TestDockerEnv(FakeDockerTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_env_contains_docker_host(self):
        # Write a fake docker that prints DOCKER_HOST
        self._write_fake_docker('#!/bin/bash\necho "$DOCKER_HOST"\n')
        d = Docker(self.paths)
        result = d.run(["info"])
        self.assertIn("docker.sock", result.output)

    def test_env_contains_colima_home(self):
        self._write_fake_docker('#!/bin/bash\necho "$COLIMA_HOME"\n')
        d = Docker(self.paths)
        result = d.run(["info"])
        self.assertIn("colima", result.output)

    def test_extra_env(self):
        self._write_fake_docker('#!/bin/bash\necho "$MY_CUSTOM_VAR"\n')
        d = Docker(self.paths, extra_env={"MY_CUSTOM_VAR": "hello123"})
        result = d.run(["info"])
        self.assertIn("hello123", result.output)


class TestDockerPopenAndBytes(FakeDockerTestCase):
    """The runner's two other shapes (issue #477): a process the caller
    drives (popen: `logs -f`, a tar into `exec -i`) and bytes in or out
    (a key file, a file handed to a container)."""

    def setUp(self):
        super().setUp()
        self.paths = self._make_paths()

    def test_popen_runs_the_binary_in_the_runners_environment(self):
        self._write_fake_docker('#!/bin/bash\necho "$MY_CUSTOM_VAR $*"; cat\n')
        d = Docker(self.paths, extra_env={"MY_CUSTOM_VAR": "hello123"})
        proc = d.popen(["logs", "-f", "x"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True)
        out, _ = proc.communicate("fed\n")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out, "hello123 logs -f x\nfed\n")

    def test_popen_extra_env_and_a_missing_binary(self):
        self._write_fake_docker('#!/bin/bash\necho "$EXTRA"\n')
        d = Docker(self.paths)
        proc = d.popen(["ps"], extra_env={"EXTRA": "yes"}, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.communicate()[0], "yes\n")
        d._docker_bin = os.path.join(self.tmpdir, "no-such-docker")
        with self.assertRaises(OSError):
            d.popen(["ps"])

    def test_bytes_in_text_out(self):
        self._write_fake_docker('#!/bin/bash\nwc -c | tr -d " "\n')
        d = Docker(self.paths)
        r = d.run(["exec", "-i", "c", "sh"], input=b"\x00\xff\x01")
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "3")
        self.assertIsInstance(r.stdout, str)

    def test_binary_reads_bytes(self):
        self._write_fake_docker('#!/bin/bash\nprintf "\\x00\\xff\\x01"; printf "err" >&2\n')
        d = Docker(self.paths)
        r = d.run(["exec", "c", "cat", "/k"], binary=True)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, b"\x00\xff\x01")
        self.assertEqual(r.stderr, b"err")
        self.assertEqual(r.output, b"\x00\xff\x01")

    def test_binary_failures_are_bytes_too(self):
        self._write_fake_docker('#!/bin/bash\nsleep 10\n')
        d = Docker(self.paths)
        r = d.run(["exec", "c", "cat", "/k"], binary=True, timeout=1)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.stdout, b"")
        self.assertIn(b"timed out", r.stderr)
        d._docker_bin = os.path.join(self.tmpdir, "no-such-docker")
        r = d.run(["ps"], binary=True)
        self.assertEqual(r.returncode, 127)
        self.assertIn(b"not found", r.stderr)

    def test_no_timeout_when_asked(self):
        self._write_fake_docker('#!/bin/bash\necho done\n')
        d = Docker(self.paths)
        self.assertEqual(d.run(["run", "x"], timeout=None).output, "done")


class TestDockerBinaryNotFound(unittest.TestCase):
    def test_missing_binary(self):
        from onionpress.platform import OnionPressPaths
        paths = OnionPressPaths(
            data_dir="/nonexistent",
            config_file="/nonexistent/config",
            secrets_file="/nonexistent/secrets",
            log_file="/nonexistent/log",
            launcher_log_file="/nonexistent/launcher.log",
            pid_file="/nonexistent/pid",
            shared_dir="/nonexistent/shared",
            docker_config_dir="/nonexistent/docker-config",
            bin_dir="/nonexistent/bin",
            docker_dir="/nonexistent/docker",
            colima_home="/nonexistent/colima",
            docker_socket="/nonexistent/colima/default/docker.sock",
            app_bundle="",
            documents_dir="/nonexistent/documents",
        )
        d = Docker(paths)
        # Should not crash, just return error result
        result = d.run(["ps"])
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
