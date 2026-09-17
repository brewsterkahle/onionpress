"""Colima VM lifecycle management (macOS only).

Handles starting, stopping, and configuring the Colima VM that runs
Docker containers on macOS. Linux bypasses this entirely (native Docker).
"""

import os
import subprocess
from typing import Callable

from .platform import OS, Arch, OnionPressPaths, detect_os, detect_arch
from .config import read_value


class ColimaError(Exception):
    """Raised when a Colima operation fails."""


class Colima:
    """Manages the Colima VM lifecycle on macOS."""

    def __init__(
        self,
        paths: OnionPressPaths,
        log_func: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        self.log_func = log_func
        self._colima_bin = self._find_colima(paths)
        self._initialized_flag = os.path.join(paths.colima_home, ".initialized")

    @staticmethod
    def _find_colima(paths: OnionPressPaths) -> str:
        """Resolve the colima binary path."""
        if paths.bin_dir:
            bundled = os.path.join(paths.bin_dir, "colima")
            if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
                return bundled
        return "colima"

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def _run(self, args: list, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run a colima command."""
        cmd = [self._colima_bin] + args
        env = os.environ.copy()
        env["COLIMA_HOME"] = self.paths.colima_home
        env["LIMA_HOME"] = os.path.join(self.paths.colima_home, "_lima")
        env["LIMA_INSTANCE"] = "onionpress"
        if self.paths.bin_dir:
            env["PATH"] = f"{self.paths.bin_dir}:{env.get('PATH', '')}"
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )

    @property
    def initialized(self) -> bool:
        """Whether Colima has been initialized (first-time setup done)."""
        return os.path.exists(self._initialized_flag)

    def is_running(self) -> bool:
        """Check if the Colima VM is running."""
        try:
            result = self._run(["status"], timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _read_vm_config(self) -> tuple:
        """Read VM memory (GB), CPU count, and disk cap (GB) from config.

        Returns (memory, cpu, disk). Disk defaults to 20 GiB (issue #230)
        for normal nodes but auto-bumps to 100 GiB when the OnionHeaven
        vanity key is present — hub installs run c-tor takeover
        instances which accumulate at scale (one per offline site). For
        very large hubs, set VM_DISK in ~/.onionpress/config before
        first launch.
        """
        memory = int(read_value(self.paths.config_file, "VM_MEMORY", "1") or "1")
        cpu = int(read_value(self.paths.config_file, "VM_CPU", "2") or "2")
        disk = int(read_value(self.paths.config_file, "VM_DISK", "20") or "20")
        # Hub auto-bump: same vanity-key path the launcher checks.
        hub_key_dir = os.path.join(
            self.paths.shared_dir, "vanity-keys",
            "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion",
        )
        if os.path.isdir(hub_key_dir) and disk < 100:
            disk = 100
        return memory, cpu, disk

    def start(self, memory: int = None, cpu: int = None) -> None:
        """Start the Colima VM.

        If memory/cpu not provided, reads from config file.
        On first run, selects the appropriate VM backend for the architecture.
        """
        cfg_mem, cfg_cpu, cfg_disk = self._read_vm_config()
        memory = memory or cfg_mem
        cpu = cpu or cfg_cpu
        arch = detect_arch()

        args = [
            "start",
            "--mount", f"{self.paths.shared_dir}:w",
            "--cpu", str(cpu),
            "--memory", str(memory),
        ]

        if not self.initialized:
            # First-time: select VM backend based on architecture, and cap
            # diffdisk to keep Finder display reasonable (#230). Default
            # 20 GiB; hub installs auto-bump to 100 in _read_vm_config.
            args.extend(["--arch", arch.value, "--disk", str(cfg_disk)])
            if arch == Arch.ARM64:
                args.extend([
                    "--vm-type", "vz",
                    "--mount-type", "virtiofs",
                    "--vz-rosetta=false",
                ])
            else:
                args.extend([
                    "--vm-type", "qemu",
                    "--mount-type", "sshfs",
                ])

        self._log(f"Starting Colima VM ({cpu} CPUs, {memory}GB RAM)...")
        result = self._run(args, timeout=300)

        if result.returncode != 0:
            self._log(f"Colima start failed: {result.stderr.rstrip()}")
            raise ColimaError(
                f"Failed to start Colima VM.\n{result.stderr.rstrip()}"
            )

        # Mark as initialized on first successful start
        if not self.initialized:
            os.makedirs(os.path.dirname(self._initialized_flag), exist_ok=True)
            with open(self._initialized_flag, "w") as f:
                f.write("")
            self._log("Colima VM initialized successfully")
        else:
            self._log(f"Colima VM started ({cpu} CPUs, {memory}GB)")

    def stop(self) -> None:
        """Stop the Colima VM."""
        self._log("Stopping Colima VM...")
        try:
            result = self._run(["stop"], timeout=60)
            if result.returncode != 0:
                self._log(f"Colima stop warning: {result.stderr.rstrip()}")
        except subprocess.TimeoutExpired:
            self._log("Colima stop timed out")

    def check_vm_config_mismatch(self, docker) -> bool:
        """Check if running VM memory/CPU differs from config.

        Args:
            docker: Docker instance for exec-ing into containers.

        Returns:
            True if a restart is needed.
        """
        cfg_mem, cfg_cpu, _ = self._read_vm_config()

        # Get actual VM memory from /proc/meminfo inside a container
        result = docker.exec(
            "onionpress-tor",
            ["awk", "/MemTotal/{print $2}", "/proc/meminfo"],
            timeout=10,
        )
        if not result.ok:
            return False
        try:
            vm_mem_kb = int(result.output.strip())
            vm_mem_gb = (vm_mem_kb + 524288) // 1048576  # round to nearest GB
        except (ValueError, TypeError):
            return False

        # Get actual CPU count
        result = docker.exec("onionpress-tor", ["nproc"], timeout=10)
        if not result.ok:
            return False
        try:
            vm_cpus = int(result.output.strip())
        except (ValueError, TypeError):
            return False

        need_restart = False
        if cfg_mem > 0 and vm_mem_gb > 0 and cfg_mem != vm_mem_gb:
            self._log(f"VM memory mismatch: config={cfg_mem}GB, running={vm_mem_gb}GB")
            need_restart = True
        if cfg_cpu > 0 and vm_cpus > 0 and cfg_cpu != vm_cpus:
            self._log(f"VM CPU mismatch: config={cfg_cpu}, running={vm_cpus}")
            need_restart = True

        return need_restart

    def restart_with_config(self, docker) -> None:
        """Stop containers, stop VM, restart with current config."""
        self._log("Restarting VM with updated config...")
        docker.compose(["down"], timeout=60)
        self.stop()
        self.start()


def repair_foreign_socket_link(paths: OnionPressPaths, log_func=None) -> bool:
    """Remove a docker.sock symlink that escapes our own Colima home.

    Launchers up to v2.4.110 symlinked ~/.colima/default/docker.sock into
    COLIMA_HOME whenever our own Lima forward was missing. That pointed
    DOCKER_HOST at whatever *other* Colima VM the user happened to be
    running, so OnionPress would deploy wordpress/db/tor into it. The link
    can still be on disk from an earlier launch, so drop it rather than
    talk to a VM that isn't ours.

    Returns True if a link was removed.
    """
    sock = paths.docker_socket
    if not os.path.islink(sock):
        return False
    target = os.path.realpath(sock)
    home = os.path.realpath(paths.colima_home)
    if target == home or target.startswith(home + os.sep):
        return False  # points inside our own VM state
    if log_func:
        log_func(f"WARNING: removing foreign Docker socket link ({sock} -> {target})")
    try:
        os.unlink(sock)
    except OSError:
        return False
    return True


def detect_container_runtime(paths: OnionPressPaths, log_func=None) -> str:
    """Detect the available container runtime.

    Returns:
        'colima-bundled' — Colima is available and running/started
        'docker-system' — system Docker is available (Linux or dev fallback)

    Raises:
        ColimaError if no runtime is available.
    """
    current_os = detect_os()

    if current_os == OS.MACOS:
        repair_foreign_socket_link(paths, log_func=log_func)
        colima = Colima(paths, log_func=log_func)
        colima_bin = colima._colima_bin
        if os.path.isfile(colima_bin) and os.access(colima_bin, os.X_OK):
            if colima.initialized:
                if colima.is_running():
                    return "colima-bundled"
                else:
                    colima.start()
                    return "colima-bundled"
            else:
                # First-time initialization
                colima.start()
                return "colima-bundled"

    # Fallback: system Docker (Linux, or dev macOS with Docker Desktop)
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            return "docker-system"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    raise ColimaError(
        "No container runtime available. "
        "Install Docker or ensure Colima is bundled in the app."
    )
