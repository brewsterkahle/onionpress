"""Container lifecycle management for OnionPress.

Handles starting/stopping the core service stack (Tor, WordPress, MariaDB)
and OnionHeaven farm workers.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .docker import Docker, DockerError
from .config import (
    DEFAULTS, Secrets, ensure_secrets, load_secrets, read_value,
    PortConfig, detect_port_offset,
)
from .platform import OnionPressPaths


CORE_SERVICES = ["wordpress", "db", "onionheaven", "autoheal"]
ALL_SERVICES = ["wordpress", "db", "tor", "onionheaven", "autoheal"]
# Pinned to digest — must match docker-compose.yml and linux/onionpress.
# Refresh all three together via build/refresh-image-digests.sh.
ONIONHEAVEN_IMAGE = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:ecab8ad6c9a196b308441f1eac787504d8c43fb6ad7638363edb14a41e784e2b"


@dataclass
class ContainerStatus:
    """Status of the OnionPress container stack."""
    runtime: str = ""  # 'colima-bundled' or 'docker-system'
    onion_address: str = ""
    wp_ready: bool = False
    tor_bootstrapped: bool = False
    services: list = field(default_factory=list)


class ContainerManager:
    """Manages the OnionPress container stack."""

    def __init__(
        self,
        docker: Docker,
        paths: OnionPressPaths,
        port_config: PortConfig | None = None,
        log_func: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.paths = paths
        self.port_config = port_config or detect_port_offset()
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def _compose_files(self, include_cloudflare: bool = False) -> list | None:
        """Return list of compose files to use."""
        if not self.paths.docker_dir:
            return None
        files = [os.path.join(self.paths.docker_dir, "docker-compose.yml")]
        if include_cloudflare:
            cf_file = os.path.join(self.paths.docker_dir, "docker-compose.cloudflare.yml")
            if os.path.exists(cf_file):
                files.append(cf_file)
        return files

    def _secrets_env(self) -> dict:
        """Load secrets and return as env dict."""
        secrets = ensure_secrets(self.paths.secrets_file)
        return secrets.as_env()

    def _build_env(self) -> dict:
        """Build extra environment for docker compose."""
        env = self._secrets_env()
        env["ONIONPRESS_WP_PORT"] = str(self.port_config.wp_port)
        env["ONIONPRESS_SOCKS_PORT"] = str(self.port_config.socks_port)
        env["ONIONPRESS_PROXY_PORT"] = str(self.port_config.proxy_port)
        env["ONIONPRESS_PORT_OFFSET"] = str(self.port_config.offset)

        # Read config-driven env vars
        tor_impl = read_value(self.paths.config_file, "TOR_IMPL", "tor")
        env["TOR_IMPL"] = tor_impl

        cf_token = read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", "")
        if cf_token:
            env["CLOUDFLARE_TUNNEL_TOKEN"] = cf_token

        return env

    # -- Core lifecycle --

    def pull_images(self, timeout: int = 300) -> bool:
        """Pull latest container images. Returns True on success."""
        self._log("Pulling container images...")
        result = self.docker.compose(
            ["pull"],
            compose_files=self._compose_files(),
            timeout=timeout,
            extra_env=self._build_env(),
        )
        if not result.ok:
            self._log(f"Image pull warning: {result.stderr.rstrip()}")
        return result.ok

    def start_core(self, retries: int = 3) -> bool:
        """Start core services (WordPress, DB, OnionHeaven). Not Tor yet.

        Returns True on success. Retries on failure.
        """
        self._log("Starting core services...")
        compose_files = self._compose_files()

        env = self._build_env()
        for attempt in range(1, retries + 1):
            result = self.docker.compose(
                ["up", "-d"] + CORE_SERVICES,
                compose_files=compose_files,
                timeout=120,
                extra_env=env,
            )
            if result.ok:
                self._log("Core services started")
                return True
            self._log(f"Start attempt {attempt}/{retries} failed: {result.stderr.rstrip()}")
            if attempt < retries:
                time.sleep(5)

        self._log("ERROR: Failed to start core services after retries")
        return False

    def start_tor(self) -> bool:
        """Start the Tor service."""
        self._log("Starting Tor service...")
        result = self.docker.compose(
            ["up", "-d", "tor"],
            compose_files=self._compose_files(),
            timeout=120,
            extra_env=self._build_env(),
        )
        if result.ok:
            self._log("Tor service started")
        else:
            self._log(f"Tor start failed: {result.stderr.rstrip()}")
        return result.ok

    def stop(self, include_onionheaven: bool = True) -> bool:
        """Stop all containers.

        Args:
            include_onionheaven: If True, also stop farm workers.
        """
        if include_onionheaven:
            self.stop_farm()

        self._log("Stopping all containers...")
        args = ["down"]

        cf_token = read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", "")
        compose_files = self._compose_files(include_cloudflare=bool(cf_token))

        result = self.docker.compose(args, compose_files=compose_files, timeout=120, extra_env=self._build_env())
        if result.ok:
            self._log("All containers stopped")
        else:
            self._log(f"Stop warning: {result.stderr.rstrip()}")
        return result.ok

    # -- Readiness checks --

    def wait_for_wordpress(self, timeout: int = 60, interval: int = 2) -> bool:
        """Wait for WordPress to respond to HTTP requests.

        Returns True if WordPress becomes ready within timeout.
        """
        self._log("Waiting for WordPress...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.docker.exec(
                "onionpress-wordpress",
                ["curl", "-sf", "--max-time", "3", "http://localhost:80/"],
                timeout=10,
            )
            if result.ok:
                self._log("WordPress is ready")
                return True
            time.sleep(interval)

        self._log("WordPress did not become ready in time")
        return False

    def wp_is_installed(self) -> bool:
        """Check if WordPress core is installed."""
        result = self.docker.exec(
            "onionpress-wordpress",
            ["wp", "--allow-root", "core", "is-installed"],
            timeout=15,
        )
        return result.ok

    def wait_for_tor(self, timeout: int = 120, interval: int = 2) -> bool:
        """Wait for Tor to bootstrap (100% or sufficiently bootstrapped).

        Returns True if Tor bootstraps within timeout.
        """
        self._log("Waiting for Tor to bootstrap...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.docker.compose(
                ["logs", "--tail", "100", "tor"],
                compose_files=self._compose_files(),
                timeout=15,
                extra_env=self._build_env(),
            )
            if result.ok:
                output = result.stdout
                if "Bootstrapped 100%" in output or "Sufficiently bootstrapped" in output:
                    self._log("Tor bootstrapped")
                    return True
            time.sleep(interval)

        self._log("Tor did not bootstrap in time")
        return False

    def get_onion_address(self) -> str:
        """Fetch the current onion address from the Tor container."""
        result = self.docker.exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        if result.ok:
            return result.output.strip()
        return ""

    def get_status(self) -> ContainerStatus:
        """Get the current container stack status."""
        status = ContainerStatus()
        status.services = self.docker.compose_ps(
            compose_files=self._compose_files(),
            extra_env=self._build_env(),
        )
        status.onion_address = self.get_onion_address()
        status.wp_ready = self.docker.container_running("onionpress-wordpress")
        status.tor_bootstrapped = self._is_tor_bootstrapped()
        return status

    def _is_tor_bootstrapped(self) -> bool:
        """Quick check if Tor is bootstrapped from recent logs."""
        result = self.docker.compose(
            ["logs", "--tail", "50", "tor"],
            compose_files=self._compose_files(),
            timeout=10,
            extra_env=self._build_env(),
        )
        if not result.ok:
            return False
        output = result.stdout
        return "Bootstrapped 100%" in output or "Sufficiently bootstrapped" in output

    def is_stack_running(self) -> bool:
        """Quick check: are all core OnionPress containers running?"""
        result = self.docker.run(
            ["ps", "--filter", "name=onionpress-", "--format", "{{.State}}"],
            timeout=10,
        )
        if not result.ok or not result.output.strip():
            return False
        states = [s.strip().lower() for s in result.output.splitlines() if s.strip()]
        return len(states) > 0 and all(s == "running" for s in states)

    # -- OnionHeaven farm --

    def start_farm_worker(self, idx: int, image: str = ONIONHEAVEN_IMAGE) -> bool:
        """Start a single OnionHeaven takeover worker container.

        Args:
            idx: Worker index (0, 1, 2, ...).
            image: Docker image to use.

        Returns True on success.
        """
        name = f"onionheaven-takeover-{idx}"
        self._log(f"Starting farm worker {name}...")

        tor_impl = read_value(self.paths.config_file, "TOR_IMPL", "tor")
        max_services = read_value(
            self.paths.config_file, "ONIONHEAVEN_MAX_SERVICES",
            DEFAULTS["ONIONHEAVEN_MAX_SERVICES"],
        )

        result = self.docker.run([
            "run", "-d",
            "--name", name,
            "--network", "onionpress-network",
            "--ulimit", "nofile=10000:10000",
            "--log-opt", "max-size=10m",
            "--log-opt", "max-file=3",
            "-e", f"TZ={os.environ.get('TZ', 'UTC')}",
            "-e", f"TOR_IMPL={tor_impl}",
            "-e", "TAKEOVER_WORKER=1",
            "-e", f"CONTAINER_NAME={name}",
            "-e", f"MAX_TAKEOVER_SERVICES={max_services}",
            "-v", f"onionpress-arti-state-takeover-{idx}:/var/lib/arti/",
            "-v", "onionpress-persistent-data:/var/lib/onionpress",
            "--restart", "unless-stopped",
            image,
        ], timeout=60)

        if result.ok:
            self._log(f"Farm worker {name} started")
        else:
            self._log(f"Farm worker {name} failed: {result.stderr.rstrip()}")
        return result.ok

    def stop_farm(self) -> None:
        """Stop and remove all OnionHeaven farm worker containers."""
        self._log("Stopping farm workers...")

        # List farm containers
        result = self.docker.run(
            ["ps", "-a", "--format", "{{.Names}}"],
            timeout=15,
        )
        if not result.ok:
            return

        workers = [
            name.strip() for name in result.output.splitlines()
            if name.strip().startswith(("onionheaven-takeover-", "onionheaven-poll-"))
        ]

        if not workers:
            return

        # Stop and remove in parallel
        def _stop_worker(name: str):
            self.docker.run(["stop", name], timeout=30)
            self.docker.run(["rm", name], timeout=15)

        with ThreadPoolExecutor(max_workers=min(len(workers), 10)) as pool:
            futures = {pool.submit(_stop_worker, w): w for w in workers}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    self._log(f"Stopped {name}")
                except Exception as e:
                    self._log(f"Warning stopping {name}: {e}")

    def list_farm_workers(self) -> list[str]:
        """List running farm worker container names."""
        result = self.docker.run(
            ["ps", "--format", "{{.Names}}"],
            timeout=15,
        )
        if not result.ok:
            return []
        return [
            name.strip() for name in result.output.splitlines()
            if name.strip().startswith(("onionheaven-takeover-", "onionheaven-poll-"))
        ]
