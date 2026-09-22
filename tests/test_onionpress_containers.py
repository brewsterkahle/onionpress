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
from onionpress.containers import ContainerManager, ContainerStatus, CORE_SERVICES


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
        docker.exec.return_value = _ok()
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertTrue(cm.wp_is_installed())

    def test_not_installed(self):
        docker = mock.Mock(spec=Docker)
        docker.exec.return_value = _fail()
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
        docker.exec.return_value = _ok("op2xyz.onion\n")
        cm = ContainerManager(docker, self.paths, self.port_config)
        self.assertEqual(cm.get_onion_address(), "op2xyz.onion")

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

    def test_start_farm_worker_passes_bridge_config(self):
        with open(self.paths.config_file, "w") as f:
            f.write(
                "TOR_BRIDGE_LINES=snowflake 192.0.2.1:80 FPRINT\n"
                "TOR_CLIENT_TRANSPORT_PLUGIN=snowflake\n"
                "TOR_UPSTREAM_PROXY=172.19.0.1:15235\n"
            )
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = _ok("container-id")
        cm = ContainerManager(docker, self.paths, self.port_config)
        cm.start_farm_worker(0)
        call_args = docker.run.call_args[0][0]
        self.assertIn("TOR_BRIDGE_LINES=snowflake 192.0.2.1:80 FPRINT", call_args)
        self.assertIn("TOR_CLIENT_TRANSPORT_PLUGIN=snowflake", call_args)
        self.assertIn("TOR_UPSTREAM_PROXY=172.19.0.1:15235", call_args)

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
        docker.exec.return_value = _ok("op2abc.onion\n")
        docker.container_running.return_value = True
        docker.compose.return_value = _ok("Bootstrapped 100%")

        cm = ContainerManager(docker, self.paths, self.port_config)
        status = cm.get_status()
        self.assertEqual(len(status.services), 2)
        self.assertEqual(status.onion_address, "op2abc.onion")
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
        # Default with no config is C Tor (the TOR_IMPL=arti default was a bug,
        # fixed so fresh installs / menubar-driven starts come up as C Tor).
        self.assertEqual(env["TOR_IMPL"], "tor")

    def test_build_env_reads_config(self):
        with open(self.paths.config_file, "w") as f:
            f.write("TOR_IMPL=tor\nCLOUDFLARE_TUNNEL_TOKEN=mytoken\n")
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertEqual(env["TOR_IMPL"], "tor")
        self.assertEqual(env["CLOUDFLARE_TUNNEL_TOKEN"], "mytoken")

    def test_build_env_omits_bridge_vars_by_default(self):
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertNotIn("TOR_BRIDGE_LINES", env)
        self.assertNotIn("TOR_CLIENT_TRANSPORT_PLUGIN", env)
        self.assertNotIn("TOR_UPSTREAM_PROXY", env)

    def test_build_env_reads_bridge_config(self):
        with open(self.paths.config_file, "w") as f:
            f.write(
                "TOR_BRIDGE_LINES=snowflake 192.0.2.1:80 FPRINT1;snowflake 192.0.2.2:80 FPRINT2\n"
                "TOR_CLIENT_TRANSPORT_PLUGIN=snowflake\n"
                "TOR_UPSTREAM_PROXY=172.19.0.1:15235\n"
            )
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertEqual(
            env["TOR_BRIDGE_LINES"],
            "snowflake 192.0.2.1:80 FPRINT1;snowflake 192.0.2.2:80 FPRINT2",
        )
        self.assertEqual(env["TOR_CLIENT_TRANSPORT_PLUGIN"], "snowflake")
        self.assertEqual(env["TOR_UPSTREAM_PROXY"], "172.19.0.1:15235")

    def test_build_env_proxy_needs_bridges(self):
        # Mirrors the entrypoint's guard: a proxy with no bridge would hand
        # the proxy public relay IPs, so _build_env withholds it too.
        with open(self.paths.config_file, "w") as f:
            f.write("TOR_UPSTREAM_PROXY=172.19.0.1:15235\n")
        docker = mock.Mock(spec=Docker)
        cm = ContainerManager(docker, self.paths, self.port_config)
        env = cm._build_env()
        self.assertNotIn("TOR_UPSTREAM_PROXY", env)


if __name__ == "__main__":
    unittest.main()
