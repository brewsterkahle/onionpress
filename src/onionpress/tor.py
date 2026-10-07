"""Tor control port wrapper for OnionPress.

Provides a clean Python interface for Tor control protocol operations,
replacing the fragile xxd/nc/shell patterns used in bash scripts.

Two transports:
- TorControl: sends commands via `docker exec` (works everywhere)
- DirectTorControl: connects to the control port directly from the host
  when the port is forwarded through Colima (faster, no docker exec overhead)
"""

import re
import socket
import time
from dataclasses import dataclass
from typing import Callable

from .docker import Docker, DockerResult


@dataclass
class OnionService:
    """Represents a Tor onion service."""
    service_id: str  # Without .onion suffix
    private_key: str = ""  # ED25519-V3 base64 key (from ADD_ONION response)

    @property
    def address(self) -> str:
        """Full .onion address."""
        return f"{self.service_id}.onion"


class TorControlError(Exception):
    """Raised when a Tor control port command fails."""


class TorControl:
    """Tor control port client that operates via docker exec.

    The control port (9051) is inside the container — not forwarded to
    the host. All commands are sent by exec-ing into the container and
    using Python sockets or the nc/xxd fallback.
    """

    def __init__(
        self,
        docker: Docker,
        container: str = "onionpress-tor",
        control_port: int = 9051,
        cookie_path: str = "/var/lib/tor/control_auth_cookie",
        log_func: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.container = container
        self.control_port = control_port
        self.cookie_path = cookie_path
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def send_command(self, cmd: str, timeout: int = 15) -> list[str]:
        """Send a command to the Tor control port and return response lines.

        Uses a Python one-liner inside the container to avoid xxd/nc
        dependency issues. Falls back to xxd+nc if python3 isn't available.

        Args:
            cmd: The control protocol command (e.g. 'GETINFO version').
            timeout: Timeout in seconds.

        Returns:
            List of response lines (without \\r\\n).

        Raises:
            TorControlError: If the command fails or times out.
        """
        # Python-based approach (most reliable, no xxd/nc dependency)
        script = (
            "import socket\n"
            f"cookie = open('{self.cookie_path}', 'rb').read().hex()\n"
            f"s = socket.create_connection(('127.0.0.1', {self.control_port}), timeout=10)\n"
            f"s.sendall(f'AUTHENTICATE {{cookie}}\\r\\n{cmd}\\r\\nQUIT\\r\\n'.encode())\n"
            "data = b''\n"
            "while True:\n"
            "    chunk = s.recv(4096)\n"
            "    if not chunk: break\n"
            "    data += chunk\n"
            "s.close()\n"
            "print(data.decode('utf-8', errors='replace'), end='')\n"
        )
        result = self.docker.exec(
            self.container,
            ["python3", "-c", script],
            timeout=timeout,
        )

        if not result.ok:
            # Fallback to xxd+nc pattern
            return self._send_command_nc(cmd, timeout)

        return self._parse_response(result.stdout)

    def _send_command_nc(self, cmd: str, timeout: int = 15) -> list[str]:
        """Fallback: send command using xxd+nc (available in Tor container)."""
        shell_cmd = (
            f"cookie=$(xxd -p {self.cookie_path} | tr -d '\\n') && "
            f"printf 'AUTHENTICATE %s\\r\\n{cmd}\\r\\nQUIT\\r\\n' \"$cookie\" | "
            f"nc -w 5 127.0.0.1 {self.control_port}"
        )
        result = self.docker.exec(self.container, shell_cmd, timeout=timeout)
        if not result.ok:
            raise TorControlError(
                f"Control port command failed: {cmd}\n{result.stderr.rstrip()}"
            )
        return self._parse_response(result.stdout)

    @staticmethod
    def _parse_response(raw: str) -> list[str]:
        """Parse control port response into lines."""
        lines = []
        for line in raw.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    # -- High-level operations --

    def get_bootstrap_phase(self) -> tuple[int, str]:
        """Get Tor bootstrap progress.

        Returns:
            (progress_percent, summary_string) e.g. (100, 'Done')
        """
        lines = self.send_command("GETINFO status/bootstrap-phase")
        for line in lines:
            m = re.search(r"PROGRESS=(\d+)", line)
            if m:
                progress = int(m.group(1))
                summary_m = re.search(r'SUMMARY="([^"]*)"', line)
                summary = summary_m.group(1) if summary_m else ""
                return progress, summary
        return 0, "unknown"

    def is_bootstrapped(self) -> bool:
        """Check if Tor has reached 100% bootstrap."""
        try:
            progress, _ = self.get_bootstrap_phase()
            return progress >= 100
        except TorControlError:
            return False

    def add_onion(
        self,
        key: str | None = None,
        port_mapping: str = "80,127.0.0.1:80",
        detach: bool = True,
    ) -> OnionService:
        """Create an ephemeral onion service.

        Args:
            key: ED25519-V3 private key in base64. If None, generates new.
            port_mapping: Port mapping string (e.g. '80,127.0.0.1:80').
            detach: If True, service survives control connection close.

        Returns:
            OnionService with service_id and (if new) private_key.

        Raises:
            TorControlError on failure.
        """
        flags = "Flags=Detach" if detach else ""
        if key:
            cmd = f"ADD_ONION ED25519-V3:{key} {flags} Port={port_mapping}"
        else:
            cmd = f"ADD_ONION NEW:ED25519-V3 {flags} Port={port_mapping}"

        lines = self.send_command(cmd.strip())

        service_id = ""
        private_key = ""
        for line in lines:
            if line.startswith("250-ServiceID="):
                service_id = line.split("=", 1)[1]
            elif line.startswith("250-PrivateKey=ED25519-V3:"):
                # Format: 250-PrivateKey=ED25519-V3:<base64key>
                private_key = line.split("ED25519-V3:", 1)[1] if "ED25519-V3:" in line else ""
            elif "Onion address collision" in line:
                raise TorControlError(
                    "Onion address collision — service already active on another Tor instance"
                )
            elif line.startswith("5"):
                raise TorControlError(f"ADD_ONION failed: {line}")

        if not service_id:
            raise TorControlError(f"ADD_ONION: no ServiceID in response: {lines}")

        self._log(f"ADD_ONION: {service_id}.onion")
        return OnionService(service_id=service_id, private_key=private_key)

    def del_onion(self, service_id: str) -> None:
        """Remove an ephemeral onion service.

        Args:
            service_id: The service ID (without .onion suffix).

        Raises:
            TorControlError on failure.
        """
        # Strip .onion suffix if present
        service_id = service_id.replace(".onion", "")

        lines = self.send_command(f"DEL_ONION {service_id}")
        for line in lines:
            if line.startswith("5"):
                raise TorControlError(f"DEL_ONION failed: {line}")

        self._log(f"DEL_ONION: {service_id}")

    def signal_newnym(self) -> None:
        """Send SIGNAL NEWNYM to clear descriptor cache.

        Rate-limited by Tor to once per 10 seconds.
        """
        lines = self.send_command("SIGNAL NEWNYM")
        for line in lines:
            if line.startswith("5"):
                raise TorControlError(f"SIGNAL NEWNYM failed: {line}")
        self._log("SIGNAL NEWNYM sent")

    def hsfetch(self, service_id: str) -> None:
        """Force Tor to fetch a onion service descriptor.

        Args:
            service_id: The service ID (without .onion suffix).
        """
        service_id = service_id.replace(".onion", "")
        self.send_command(f"HSFETCH {service_id}")

    def flush_descriptor_cache(self, service_ids: list[str]) -> None:
        """Clear cached descriptors and re-fetch fresh ones.

        Sends NEWNYM first (clears cache), waits 3s, then issues HSFETCH
        for each service ID. Uses two separate control connections because
        you can't sleep inside a single nc pipe.

        Args:
            service_ids: List of service IDs (without .onion suffix).
        """
        self.signal_newnym()
        time.sleep(3)

        # Send all HSFETCHes in a single connection for efficiency
        cmds = "\r\n".join(f"HSFETCH {sid.replace('.onion', '')}" for sid in service_ids)
        self.send_command(cmds)

    def get_detached_services(self) -> list[str]:
        """List detached onion service IDs."""
        lines = self.send_command("GETINFO onions/detached")
        services = []
        for line in lines:
            if line.startswith("250+onions/detached="):
                continue
            if line == "." or line.startswith("250 ") or line.startswith("650 "):
                continue
            # Lines between 250+onions/detached= and . are service IDs
            if re.match(r'^[a-z2-7]{56}$', line):
                services.append(line)
        return services

    def get_version(self) -> str:
        """Get the Tor version string."""
        lines = self.send_command("GETINFO version")
        for line in lines:
            if line.startswith("250-version="):
                return line.split("=", 1)[1]
        return ""


class DirectTorControl(TorControl):
    """Tor control port client using direct host-side sockets.

    When the Tor control port is forwarded from the Colima VM to the host
    (e.g. via docker-compose ports: "127.0.0.1:9051:9051"), this class
    connects directly from the host — no docker exec overhead.

    Requires reading the auth cookie once via docker exec on init.
    """

    def __init__(
        self,
        docker: Docker,
        container: str = "onionpress-tor",
        host: str = "127.0.0.1",
        control_port: int = 9051,
        cookie_path: str = "/var/lib/tor/control_auth_cookie",
        log_func: Callable[[str], None] | None = None,
    ):
        super().__init__(docker, container, control_port, cookie_path, log_func)
        self.host = host
        self._cookie_hex: str | None = None

    def _read_cookie(self) -> str:
        """Read the auth cookie from the container (cached)."""
        if self._cookie_hex is not None:
            return self._cookie_hex

        # Read raw cookie bytes via docker exec, output as hex
        result = self.docker.exec(
            self.container,
            ["python3", "-c",
             f"print(open('{self.cookie_path}', 'rb').read().hex(), end='')"],
            timeout=10,
        )
        if result.ok:
            self._cookie_hex = result.output.strip()
            return self._cookie_hex

        # Fallback: xxd
        result = self.docker.exec(
            self.container,
            f"xxd -p {self.cookie_path} | tr -d '\\n'",
            timeout=10,
        )
        if result.ok:
            self._cookie_hex = result.output.strip()
            return self._cookie_hex

        raise TorControlError("Cannot read control auth cookie from container")

    def invalidate_cookie(self) -> None:
        """Clear the cached cookie (e.g. after container restart)."""
        self._cookie_hex = None

    def send_command(self, cmd: str, timeout: int = 15) -> list[str]:
        """Send a command via direct socket connection to the host port.

        Falls back to docker exec if the direct connection fails.
        """
        try:
            cookie = self._read_cookie()
            s = socket.create_connection((self.host, self.control_port), timeout=timeout)
            try:
                s.sendall(f"AUTHENTICATE {cookie}\r\n{cmd}\r\nQUIT\r\n".encode())
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                return self._parse_response(data.decode("utf-8", errors="replace"))
            finally:
                s.close()
        except (OSError, TorControlError) as e:
            self._log(f"Direct control port failed ({e}), falling back to docker exec")
            # Fall back to parent's docker exec approach
            return super().send_command(cmd, timeout=timeout)


def create_tor_control(
    docker: Docker,
    container: str = "onionpress-tor",
    control_port: int = 9051,
    cookie_path: str = "/var/lib/tor/control_auth_cookie",
    log_func: Callable[[str], None] | None = None,
    prefer_direct: bool = True,
) -> TorControl:
    """Factory: create the best available TorControl instance.

    If prefer_direct is True and the control port is reachable from the
    host, returns DirectTorControl. Otherwise returns TorControl (docker exec).
    """
    if prefer_direct:
        try:
            s = socket.create_connection(("127.0.0.1", control_port), timeout=2)
            s.close()
            if log_func:
                log_func(f"Control port {control_port} reachable — using direct connection")
            return DirectTorControl(
                docker, container, "127.0.0.1", control_port, cookie_path, log_func,
            )
        except OSError:
            if log_func:
                log_func(f"Control port {control_port} not reachable — using docker exec")

    return TorControl(docker, container, control_port, cookie_path, log_func)
