"""Tests for src/onionpress/containers.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.platform import OnionPressPaths
from onionpress.config import PortConfig
from onionpress.docker import Docker, DockerResult
from onionpress.containers import (
    ContainerManager, ContainerStatus, CORE_SERVICES, ONIONHEAVEN_IMAGE_PIN, tor_image,
)

ONION = "op2" + "x" * 53 + ".onion"


def _make_paths(tmpdir):
    data_dir = os.path.join(tmpdir, "data")
    bin_dir = os.path.join(tmpdir, "bin")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "docker"), exist_ok=True)
    # Create a minimal docker-compose.yml so compose_files() works
    with open(os.path.join(data_dir, "docker", "docker-compose.yml"), "w") as f:
        f.write("services: {}\n")
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


def _ok(stdout="", stderr=""):
    return DockerResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="error", code=1):
    return DockerResult(returncode=code, stdout="", stderr=stderr)


class TestContainerStatus(unittest.TestCase):
    def test_defaults(self):
        s = ContainerStatus()
        self.assertEqual(s.onion_address, "")
        self.assertFalse(s.wp_ready)
        self.assertFalse(s.tor_bootstrapped)
        self.assertEqual(s.services, [])


class TestContainerManagerInit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_compose_files(self):
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        files = cm._compose_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("docker-compose.yml"))

    def test_compose_files_with_cloudflare(self):
        # Create cloudflare compose file
        cf_path = os.path.join(self.paths.docker_dir, "docker-compose.cloudflare.yml")
        with open(cf_path, "w") as f:
            f.write("services: {}\n")

        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        files = cm._compose_files(include_cloudflare=True)
        self.assertEqual(len(files), 2)


class TestStartCore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_core_success(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok()
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.start_core()
        self.assertTrue(result)
        # Verify compose was called with core services
        call_args = docker.compose.call_args
        self.assertIn("up", call_args[0][0])

    def test_start_core_retry(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.side_effect = [_fail(), _fail(), _ok()]
        logs = []
        cm = ContainerManager(docker, self.paths, self.port_config, log_func=logs.append)
        result = cm.start_core(retries=3)
        self.assertTrue(result)
        self.assertEqual(docker.compose.call_count, 3)

    def test_start_core_all_retries_fail(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _fail()
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.start_core(retries=2)
        self.assertFalse(result)
        self.assertEqual(docker.compose.call_count, 2)


class TestStartTor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_tor_success(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertTrue(cm.start_tor())


class TestEnsureTorImage(unittest.TestCase):
    """The image the restore's key seed runs from: the stack's tor image,
    pulled by its pin when missing, never over a locally built one."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ONIONPRESS_TOR_IMAGE", None)
        os.environ.pop("ONIONPRESS_WORDPRESS_IMAGE", None)

    def _cm(self, results):
        docker = mock.Mock(spec=Docker)
        docker.run.side_effect = results
        return ContainerManager(docker, self.paths, self.port_config, log_func=lambda m: None), docker

    def test_the_pin_by_default(self):
        self.assertEqual(tor_image(self.paths.config_file), ONIONHEAVEN_IMAGE_PIN)
        self.assertRegex(ONIONHEAVEN_IMAGE_PIN,
                         r"^ghcr\.io/brewsterkahle/onionpress-tor:latest@sha256:[0-9a-f]{64}$")
        with open(self.paths.config_file, "w") as f:
            f.write("ONIONPRESS_TOR_IMAGE=onionpress-tor:dev\n")
        self.assertEqual(tor_image(self.paths.config_file), "onionpress-tor:dev")

    def test_present(self):
        cm, docker = self._cm([_ok("sha256:x")])
        self.assertEqual(cm.ensure_tor_image(), ONIONHEAVEN_IMAGE_PIN)
        self.assertEqual(docker.run.call_count, 1)

    def test_missing_is_pulled_by_its_pin(self):
        cm, docker = self._cm([_fail(), _ok(), _ok("sha256:x")])
        self.assertEqual(cm.ensure_tor_image(), ONIONHEAVEN_IMAGE_PIN)
        self.assertEqual(docker.run.call_args_list[1].args[0], ["pull", ONIONHEAVEN_IMAGE_PIN])

    def test_unavailable(self):
        cm, _ = self._cm([_fail(), _fail()])
        self.assertIsNone(cm.ensure_tor_image())

    def test_a_missing_local_image_is_never_pulled(self):
        with open(self.paths.config_file, "w") as f:
            f.write("ONIONPRESS_TOR_IMAGE=onionpress-tor:dev\n")
        cm, docker = self._cm([_fail()])
        self.assertIsNone(cm.ensure_tor_image())
        self.assertEqual(docker.run.call_args_list[0].args[0],
                         ["image", "inspect", "--format", "{{.Id}}", "onionpress-tor:dev"])
        self.assertEqual(docker.run.call_count, 1)


class TestStop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stop_calls_compose_down(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok()
        docker.run.return_value = _ok("")  # for stop_farm listing
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.stop()
        self.assertTrue(result)
        # Should call compose down with onionheaven profile
        compose_call = docker.compose.call_args
        self.assertIn("down", compose_call[0][0])


class TestWaitForWordpress(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wordpress_ready_immediately(self):
        docker = mock.Mock(spec=Docker)
        docker.exec.return_value = _ok("<html>")
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.wait_for_wordpress(timeout=5)
        self.assertTrue(result)

    def test_wordpress_timeout(self):
        docker = mock.Mock(spec=Docker)
        docker.exec.return_value = _fail()
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.wait_for_wordpress(timeout=1, interval=0.2)
        self.assertFalse(result)


class TestWpIsInstalled(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_installed(self):
        docker = mock.Mock(spec=Docker)
        docker.wp.return_value = _ok()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertTrue(cm.wp_is_installed())
        # Docker.wp: wp-cli as www-data, never root.
        docker.wp.assert_called_once_with("core", "is-installed", timeout=15)
        docker.exec.assert_not_called()

    def test_not_installed(self):
        docker = mock.Mock(spec=Docker)
        docker.wp.return_value = _fail()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertFalse(cm.wp_is_installed())


class TestWaitForTor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tor_bootstrapped(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok("Bootstrapped 100% (done): Done")
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.wait_for_tor(timeout=5)
        self.assertTrue(result)

    def test_tor_sufficiently_bootstrapped(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok("Sufficiently bootstrapped to build circuits")
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.wait_for_tor(timeout=5)
        self.assertTrue(result)

    def test_tor_timeout(self):
        docker = mock.Mock(spec=Docker)
        docker.compose.return_value = _ok("Bootstrapped 5%")
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.wait_for_tor(timeout=1, interval=0.2)
        self.assertFalse(result)


class TestGetOnionAddress(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_has_address(self):
        docker = mock.Mock(spec=Docker)
        docker.exec.return_value = _ok(ONION + "\n")
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertEqual(cm.get_onion_address(), ONION)

    def test_malformed_address_reads_as_none(self):
        # It names the backup file and goes into backup metadata.
        for content in ("op2xyz.onion", "../../" + "a" * 50 + ".onion",
                        ONION + "\nevil"):
            with self.subTest(content=content):
                docker = mock.Mock(spec=Docker)
                docker.exec.return_value = _ok(content + "\n")
                cm = ContainerManager(docker, self.paths, self.port_config)
                self.assertEqual(cm.get_onion_address(), "")

    def test_no_address(self):
        docker = mock.Mock(spec=Docker)
        docker.exec.return_value = _fail()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertEqual(cm.get_onion_address(), "")


class TestFarmWorkers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_farm_worker(self):
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = _ok("container-id")
        cm = ContainerManager(docker, self.paths, self.port_config)
        result = cm.start_farm_worker(0)
        self.assertTrue(result)
        # Verify docker run was called with correct container name
        call_args = docker.run.call_args[0][0]
        self.assertIn("onionheaven-takeover-0", call_args)

    def test_stop_farm_no_workers(self):
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = _ok("")  # no containers
        cm = ContainerManager(docker, self.paths, self.port_config)
        cm.stop_farm()
        # Should only call ps, not stop/rm
        self.assertEqual(docker.run.call_count, 1)

    def test_stop_farm_with_workers(self):
        docker = mock.Mock(spec=Docker)
        docker.run.side_effect = [
            _ok("onionheaven-takeover-0\nonionheaven-takeover-1\nonionpress-tor\n"),
            _ok(), _ok(),  # stop/rm worker 0
            _ok(), _ok(),  # stop/rm worker 1
        ]
        cm = ContainerManager(docker, self.paths, self.port_config)
        cm.stop_farm()
        # ps + 2*(stop+rm) = 5 calls
        self.assertEqual(docker.run.call_count, 5)

    def test_list_farm_workers(self):
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = _ok(
            "onionheaven-takeover-0\nonionheaven-takeover-1\nonionpress-tor\n"
        )
        cm = ContainerManager(docker, self.paths, self.port_config)
        workers = cm.list_farm_workers()
        self.assertEqual(workers, ["onionheaven-takeover-0", "onionheaven-takeover-1"])

    def test_list_farm_workers_empty(self):
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = _fail()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertEqual(cm.list_farm_workers(), [])


class TestGetStatus(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_status(self):
        docker = mock.Mock(spec=Docker)
        docker.compose_ps.return_value = [
            {"Name": "onionpress-tor", "State": "running"},
            {"Name": "onionpress-wordpress", "State": "running"},
        ]
        docker.exec.return_value = _ok(ONION + "\n")
        docker.container_running.return_value = True
        docker.compose.return_value = _ok("Bootstrapped 100%")

        cm = ContainerManager(docker, self.paths, self.port_config)
        status = cm.get_status()
        self.assertEqual(len(status.services), 2)
        self.assertEqual(status.onion_address, ONION)
        self.assertTrue(status.wp_ready)
        self.assertTrue(status.tor_bootstrapped)


class TestBuildEnv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.paths = _make_paths(self.tmpdir)
        self.port_config = PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_env_includes_ports(self):
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertEqual(env["ONIONPRESS_WP_PORT"], "8080")
        self.assertEqual(env["ONIONPRESS_SOCKS_PORT"], "9050")
        # There is one Tor implementation; no switch is exported.
        self.assertNotIn("TOR_IMPL", env)

    def test_build_env_reads_config(self):
        with open(self.paths.config_file, "w") as f:
            f.write("CLOUDFLARE_TUNNEL_TOKEN=mytoken\n")
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertEqual(env["CLOUDFLARE_TUNNEL_TOKEN"], "mytoken")


if __name__ == "__main__":
    unittest.main()
