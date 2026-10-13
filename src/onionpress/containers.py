"""Container lifecycle management for OnionPress.

Handles starting/stopping the core service stack (Tor, WordPress, MariaDB)
and OnionHeaven farm workers.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from . import key_manager, wordpress_egress
from .docker import Docker, DockerError
from .config import (
    DEFAULTS, Secrets, ensure_secrets, load_secrets, read_value,
    PortConfig, detect_port_offset,
)
from .platform import OnionPressPaths


def _default_config_file() -> str:
    # Same path resolve_paths() uses when no data_dir override is given. Kept
    # as a plain join rather than a resolve_paths() call so importing this
    # module has no side effects and no dependency on the app bundle.
    return os.path.join(os.path.expanduser("~"), ".onionpress", "config")


def image_override(name: str, config_file: str | None = None) -> str | None:
    """An image override from the environment, or failing that from the config.

    `config_file` defaults to ~/.onionpress/config. Callers that already hold
    an OnionPressPaths should pass `paths.config_file` — resolve_paths()
    supports a data_dir override, and a hardcoded home path would silently
    read the wrong file under one.

    Reading the config file matters on macOS, and is not merely a
    convenience. The bash launcher exports these after reading
    ~/.onionpress/config, but the MenubarApp is that launcher's PARENT — it
    spawns the launcher, never the reverse — so a child's exports can never
    reach it. Without this, a developer who followed the documented route of
    putting ONIONPRESS_TOR_IMAGE in ~/.onionpress/config still had the
    menubar's own `docker compose pull` overwrite their local image on every
    launch, and be told the images were up to date.

    The config is parsed the same way the launchers parse it: first matching
    `KEY=` line, everything after the first `=`.
    """
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open(config_file or _default_config_file(),
                  "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


CORE_SERVICES = ["wordpress", "db", "onionheaven", "autoheal"]
ALL_SERVICES = ["wordpress", "db", "tor", "onionheaven", "autoheal"]
# Pinned to digest. The literal below is propagated from build/image-pins.env
# by build/refresh-image-digests.sh, which writes every consumer at once;
# tests/test_image_pins.py fails if any of them drift apart.
#
# Resolution order matches docker-compose.yml's `onionheaven` service, so the
# menubar path and the compose path always agree: the service-specific
# ONIONHEAVEN_IMAGE wins, then the stack-wide ONIONPRESS_TOR_IMAGE (what
# build/build-images.sh exports for a locally built stack), then the pin.
ONIONHEAVEN_IMAGE_PIN = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:2cc73a26b378279a7417cf5539607c8d15df303bd7d792db7783552be2f237ca"


def onionheaven_image(config_file: str | None = None) -> str:
    """The image to run OnionHeaven takeover workers from.

    Resolved on each call rather than at import, so it picks up an override
    written to ~/.onionpress/config without a restart — and so it sees the
    same config the launchers do (see image_override).
    """
    return (image_override("ONIONHEAVEN_IMAGE", config_file)
            or image_override("ONIONPRESS_TOR_IMAGE", config_file)
            or ONIONHEAVEN_IMAGE_PIN)


def tor_image(config_file: str | None = None) -> str:
    """The stack's tor image: ONIONPRESS_TOR_IMAGE, else the pin.

    The one-shot helpers that handle the onion service key (the restore's key
    seed here; the launchers' KEY_HELPER_IMAGE) run from it, never from a
    floating tag: the pin is one set of bytes, and the image is the one that
    will run Tor with that key anyway.
    """
    return image_override("ONIONPRESS_TOR_IMAGE", config_file) or ONIONHEAVEN_IMAGE_PIN


# The onion service key volume, its name until 2026-09-25, and the marker the
# launchers' migrate_key_volume() keeps beside an unverified copy of the
# first into the second. Starting Tor mounts the new volume, and Docker
# creates it, empty, when it is not there: every later start would then
# take it for the migrated key, and Tor would mint a new address.
KEY_VOLUME = "onionpress-onion-keys"
OLD_KEY_VOLUME = "onionpress-arti-state"
KEY_VOLUME_MIGRATION_PENDING = ".key-volume-migration-pending"


def forget_key_volume_migration(data_dir: str) -> None:
    """Drop the migration marker. Every path that wipes the key volumes for
    a replaced identity calls this (the launchers `rm -f` it): kept beside
    an old volume that a wipe failed to remove, the marker would have the
    next start remove the new key and copy the replaced one back."""
    try:
        os.remove(os.path.join(data_dir, KEY_VOLUME_MIGRATION_PENDING))
    except FileNotFoundError:
        pass


# The OnionHeaven hub data volume (onionheaven_common.HUB_DATA_VOLUME and
# ONIONHEAVEN_DATA_DIR in the tor image), and the containers that use it.
HUB_DATA_VOLUME = "onionpress-onionheaven-data"
HUB_DATA_DIR = "/var/lib/onionheaven"
_HUB_CONTAINER_RE = re.compile(
    r"onionpress-tor|onionheaven|onionheaven-(poll|takeover)-[0-9]+")


# What OnionPress puts on onionpress-network: its compose services (wordpress
# and db too, before issue #144 moved them) and the takeover workers.
# migrate_tor_network removes these and only disconnects anything else; the
# launchers' migrate_tor_network matches the same names.
_TOR_NETWORK_CONTAINER_RE = re.compile(
    r"onionpress-(tor|wordpress|db|cloudflared)|onionheaven"
    r"|onionheaven-(poll|takeover)-[0-9]+")
_BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"


def tor_network_is_old(options_json: str) -> bool:
    """True only if `docker network inspect --format '{{json .Options}}'
    onionpress-network` is a network whose bridge is not the fixed one (Docker
    named it br-<id>); no network, or output that is not that, is False."""
    try:
        options = json.loads(options_json)
    except (TypeError, ValueError):
        return False
    return (isinstance(options, dict)
            and options.get(_BRIDGE_NAME_OPTION) != wordpress_egress.TOR_BRIDGE)


def mounts_hub_data(mounts_json: str) -> bool:
    """False only if `docker inspect`'s Mounts certainly lack the hub data
    volume at HUB_DATA_DIR; anything unreadable is True (doubt keeps a
    container)."""
    try:
        mounts = json.loads(mounts_json)
    except (TypeError, ValueError):
        return True
    if not isinstance(mounts, list):
        return True
    return any(isinstance(m, dict) and m.get("Name") == HUB_DATA_VOLUME
               and m.get("Destination") == HUB_DATA_DIR for m in mounts)



def using_local_images(config_file: str | None = None) -> bool:
    """True when the stack points at images built on this machine.

    build/build-images.sh prints ONIONPRESS_TOR_IMAGE / ONIONPRESS_WORDPRESS_IMAGE
    for a locally built stack. Every image pull is gated on this: without the
    gate a pull overwrites the local tag with the registry's copy, so a
    developer builds an image, starts the app, and silently tests someone
    else's build.

    A reference that still points at ghcr.io is a deliberate pin, not a local
    build, so it does not count. Mirrors using_local_images() in both bash
    launchers — change all three together.
    """
    for name in ("ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE"):
        ref = image_override(name, config_file)
        if ref and not ref.startswith("ghcr.io/"):
            return True
    return False


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
        egress: Callable[[], object] | None = None,
    ):
        self.docker = docker
        self.paths = paths
        self.port_config = port_config or detect_port_offset()
        self.log_func = log_func
        # Shuts WordPress's network, the Tor side's and WordPress's published
        # port before start_core starts anything on them
        # (wordpress_egress.ensure; injectable for tests).
        self._egress = egress or (
            lambda: wordpress_egress.ensure(self.paths, self.docker, self._log,
                                            wp_port=self.port_config.wp_port))

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
        # The Creations bind mount in docker-compose.yml; unset, compose
        # falls back to a throwaway /tmp path.
        env["ONIONPRESS_DOCUMENTS_DIR"] = self.paths.documents_dir

        # Read config-driven env vars

        cf_token = read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", "")
        if cf_token:
            env["CLOUDFLARE_TUNNEL_TOKEN"] = cf_token

        # Run as an OnionHeaven hub; only an exact "yes" counts, checked in
        # the containers. Always set, as the launchers do, so a compose call
        # from here never recreates tor/onionheaven with a different value.
        env["ONIONHEAVEN_ACCEPT_REGISTRATIONS"] = read_value(
            self.paths.config_file, "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "")

        return env

    def ensure_tor_image(self) -> str | None:
        """The tor image (tor_image), made sure to be here before a helper
        that handles the onion service key runs from it: pulled by its
        pinned reference when missing, never for a locally built stack
        (using_local_images). None when it is not here and cannot be.
        Mirrors ensure_key_helper_image() in both launchers."""
        image = tor_image(self.paths.config_file)
        inspect = ["image", "inspect", "--format", "{{.Id}}", image]
        if self.docker.run(inspect, timeout=30, quiet=True).ok:
            return image
        if using_local_images(self.paths.config_file):
            self._log(f"ERROR: the locally built tor image {image} is not here "
                      "(build it with build/build-images.sh)")
            return None
        self._log(f"Pulling {image}...")
        if not self.docker.run(["pull", image], timeout=600).ok:
            self._log(f"ERROR: could not pull {image}")
            return None
        return image if self.docker.run(inspect, timeout=30, quiet=True).ok else None

    def key_volume_not_ready(self) -> str | None:
        """Why Tor must not start yet, or None.

        The launchers move the key out of OLD_KEY_VOLUME before anything
        starts Tor (migrate_key_volume); this has no copy of that step and
        refuses instead while it is still due: the old volume is there and
        the new one is not, or holds an unverified copy (the marker). An
        unanswered `docker volume ls` refuses too."""
        pending = os.path.lexists(
            os.path.join(self.paths.data_dir, KEY_VOLUME_MIGRATION_PENDING))
        result = self.docker.run(["volume", "ls", "--format", "{{.Name}}"],
                                 timeout=30, quiet=True)
        if not result.ok:
            return "Docker did not say which volumes exist"
        names = set(result.stdout.split())
        if OLD_KEY_VOLUME in names and (KEY_VOLUME not in names or pending):
            return (f"the onion service key is still in {OLD_KEY_VOLUME}; "
                    "start OnionPress with its launcher (`onionpress start`), "
                    f"which moves it to {KEY_VOLUME} first")
        return None

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

        # WordPress and MariaDB reach nothing outside Docker (issue #144):
        # the rules go on the Docker host before their network has a
        # container on it. Best effort, as in the launchers; ensure() logs.
        self._egress()

        # Nothing from before the OnionHeaven data volume may run beside the
        # containers about to start (the data move must not race them).
        try:
            self.retire_old_onionheaven_containers()
        except Exception as e:
            self._log(f"Could not check for containers of an older OnionPress: {e}")

        # The Tor side's network on its fixed bridge, for its rules (once).
        try:
            self.migrate_tor_network()
        except Exception as e:
            self._log(f"Could not check onionpress-network's bridge: {e}")

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
        """Start the Tor service, never before its key volume is in place
        (key_volume_not_ready)."""
        why = self.key_volume_not_ready()
        if why:
            self._log(f"ERROR: not starting Tor: {why}")
            return False
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
        result = self.docker.wp("core", "is-installed", timeout=15)
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
        """Fetch the current onion address from the Tor container, or ""
        when there is none or the file holds anything but a well-formed v3
        address (it names a backup file and goes into backup metadata)."""
        result = self.docker.exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        if result.ok:
            addr = result.output.strip()
            if key_manager.is_valid_onion_address(addr):
                return addr
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

    def start_farm_worker(self, idx: int, image: str | None = None) -> bool:
        """Start a single OnionHeaven takeover worker container.

        Args:
            idx: Worker index (0, 1, 2, ...).
            image: Docker image to use. Resolved per call when omitted, so a
                locally built image set in the environment or in
                ~/.onionpress/config is honoured — a module-level default
                would have frozen the value at import time.

        Returns True on success.
        """
        if image is None:
            image = onionheaven_image(self.paths.config_file)
        name = f"onionheaven-takeover-{idx}"
        self._log(f"Starting farm worker {name}...")

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
            # The same hardening and capabilities as _spawn_worker: su to
            # debian-tor, the control cookie in its 0700 /var/lib/tor.
            "--cap-drop", "ALL",
            "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "SETGID",
            "--cap-add", "SETUID",
            "--security-opt", "no-new-privileges:true",
            "-e", f"TZ={os.environ.get('TZ', 'UTC')}",
            "-e", "TAKEOVER_WORKER=1",
            "-e", f"CONTAINER_NAME={name}",
            "-e", f"MAX_TAKEOVER_SERVICES={max_services}",
            # The OnionHeaven data volume only, as onionheaven_common's
            # _spawn_worker does — never WordPress's shared volume.
            "-v", "onionpress-onionheaven-data:/var/lib/onionheaven",
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

        # Stop and remove in parallel, a second's grace each: their onion
        # services are ephemeral (ADD_ONION).
        def _stop_worker(name: str):
            self.docker.run(["stop", "-t", "1", name], timeout=30)
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

    def retire_old_onionheaven_containers(self) -> list[str]:
        """Remove every OnionHeaven worker and Tor-side container that does
        not mount the hub data volume, all at once; returns their names.

        An OnionPress from before the volume started them, and they still
        read and write the hub's registry and keys on
        onionpress-persistent-data. The data move must not race them, and
        nothing may use that copy after it. Compose recreates onionpress-tor
        and onionheaven, the heartbeat starts new workers. The launchers'
        retire_old_onionheaven_containers does the same.
        """
        result = self.docker.run(["ps", "-a", "--format", "{{.Names}}"],
                                 timeout=15, quiet=True)
        if not result.ok:
            return []
        stale = []
        for name in result.output.split():
            if not _HUB_CONTAINER_RE.fullmatch(name):
                continue
            inspected = self.docker.run(["inspect", "--format", "{{json .Mounts}}", name],
                                        timeout=15, quiet=True)
            if inspected.ok and not mounts_hub_data(inspected.output):
                stale.append(name)
        if not stale:
            return []
        for name in stale:
            self._log(f"Removing {name}: it does not mount {HUB_DATA_VOLUME} "
                      f"(started by an older OnionPress)")
        with ThreadPoolExecutor(max_workers=min(len(stale), 10)) as pool:
            list(pool.map(lambda n: self.docker.run(["rm", "-f", n], timeout=30), stale))
        return stale

    def _tor_network_containers(self) -> list[str]:
        result = self.docker.run(
            ["network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}",
             wordpress_egress.TOR_NETWORK], timeout=15, quiet=True)
        return result.output.split() if result.ok else []

    def migrate_tor_network(self) -> bool:
        """Recreate an onionpress-network made before its bridge had a fixed
        name (wordpress_egress.TOR_BRIDGE), which the Tor side's rules name;
        True when one was removed for compose to make anew.

        Docker cannot rename a bridge, and compose 2.40, which would recreate
        the network itself, fails the whole start while a takeover worker
        (no compose service) is still on it. Everything OnionPress put there
        is removed, onionheaven first since it starts workers (compose
        recreates its services, the heartbeat its workers); anything else is
        only disconnected. The launchers' migrate_tor_network does the same.
        """
        inspected = self.docker.run(
            ["network", "inspect", "--format", "{{json .Options}}",
             wordpress_egress.TOR_NETWORK], timeout=15, quiet=True)
        if not inspected.ok or not tor_network_is_old(inspected.output):
            return False
        self._log(f"Recreating {wordpress_egress.TOR_NETWORK} on its own bridge "
                  f"({wordpress_egress.TOR_BRIDGE}), so the Tor side's rules apply to it")
        names = self._tor_network_containers()
        if "onionheaven" in names:
            self.docker.run(["rm", "-f", "onionheaven"], timeout=30)
            names = self._tor_network_containers()
        ours = [n for n in names if _TOR_NETWORK_CONTAINER_RE.fullmatch(n)]
        for name in names:
            if name not in ours:
                self._log(f"WARNING: {name} is on {wordpress_egress.TOR_NETWORK} but is "
                          "not OnionPress's: disconnecting it")
                self.docker.run(["network", "disconnect", "-f",
                                 wordpress_egress.TOR_NETWORK, name], timeout=30)
        if ours:
            with ThreadPoolExecutor(max_workers=min(len(ours), 10)) as pool:
                list(pool.map(lambda n: self.docker.run(["rm", "-f", n], timeout=30), ours))
        removed = self.docker.run(["network", "rm", wordpress_egress.TOR_NETWORK],
                                  timeout=30)
        if not removed.ok:
            self._log(f"WARNING: could not recreate {wordpress_egress.TOR_NETWORK} on "
                      f"{wordpress_egress.TOR_BRIDGE}: until it is, the Tor-side "
                      f"containers can reach this machine and its network "
                      f"({removed.stderr.strip()})")
        return removed.ok

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
