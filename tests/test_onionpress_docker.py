"""Tests for src/onionpress/docker.py."""

import json
import os
import shutil
import stat
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

    def test_compose_injects_default_profile(self):
        # wordpress/db carry `profiles:` tags — a compose call with no
        # active profile silently skips them on pull/down. The wrapper
        # must guarantee COMPOSE_PROFILES on every compose invocation.
        self._write_fake_docker('#!/bin/bash\necho "COMPOSE_PROFILES=$COMPOSE_PROFILES"\n')
        d = Docker(self.paths)
        result = d.compose(["pull"])
        self.assertIn("COMPOSE_PROFILES=wordpress", result.output)

    def test_compose_profile_follows_config_site_type(self):
        self._write_fake_docker('#!/bin/bash\necho "COMPOSE_PROFILES=$COMPOSE_PROFILES"\n')
        with open(self.paths.config_file, "w") as f:
            f.write("SITE_TYPE=static\n")
        d = Docker(self.paths)
        result = d.compose(["down"])
        self.assertIn("COMPOSE_PROFILES=static", result.output)

    def test_compose_profile_resolved_per_call_not_cached(self):
        # SITE_TYPE is written mid-session by first-run setup — the
        # profile must track the config file, not construction time.
        self._write_fake_docker('#!/bin/bash\necho "COMPOSE_PROFILES=$COMPOSE_PROFILES"\n')
        d = Docker(self.paths)
        self.assertIn("COMPOSE_PROFILES=wordpress", d.compose(["ps"]).output)
        with open(self.paths.config_file, "w") as f:
            f.write("SITE_TYPE=static\n")
        self.assertIn("COMPOSE_PROFILES=static", d.compose(["ps"]).output)

    def test_compose_caller_profile_wins(self):
        self._write_fake_docker('#!/bin/bash\necho "COMPOSE_PROFILES=$COMPOSE_PROFILES"\n')
        d = Docker(self.paths)
        result = d.compose(["up"], extra_env={"COMPOSE_PROFILES": "static"})
        self.assertIn("COMPOSE_PROFILES=static", result.output)


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
