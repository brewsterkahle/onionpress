"""Host port selection for OnionPress (multi-user support).

Extra users offset host ports by +10000 each; the offset is found with a
socket bind test (never lsof, which sees only the current user's processes)
or, once our stack runs, read from the WordPress container's published port.
"""

import socket
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PortConfig:
    """Port configuration for multi-user support. Frozen: a process that
    learns a new offset builds a new one (issue #476)."""
    offset: int
    wp_port: int
    socks_port: int
    proxy_port: int

    @classmethod
    def from_offset(cls, offset: int) -> "PortConfig":
        """Build the port triple from an offset — the one place 8080/9050/9077
        get added to it, so the three ports can't drift apart across callers."""
        return cls(
            offset=offset,
            wp_port=8080 + offset,
            socks_port=9050 + offset,
            proxy_port=9077 + offset,
        )


def port_environ(pc) -> dict:
    """The ONIONPRESS_*_PORT entries for *pc*: what the launcher, compose
    and the scripts it runs read the ports from. The one place the four
    names are spelt for a child's environment."""
    return {
        "ONIONPRESS_PORT_OFFSET": str(pc.offset),
        "ONIONPRESS_WP_PORT": str(pc.wp_port),
        "ONIONPRESS_SOCKS_PORT": str(pc.socks_port),
        "ONIONPRESS_PROXY_PORT": str(pc.proxy_port),
    }


class PortAttributes:
    """port_offset / wp_port / socks_port / proxy_port as read-only views
    of ``self.ports`` (a PortConfig), for the daemon and the menubar: the
    four can only change together, by replacing ``self.ports``."""

    @property
    def port_offset(self):
        return self.ports.offset

    @property
    def wp_port(self):
        return self.ports.wp_port

    @property
    def socks_port(self):
        return self.ports.socks_port

    @property
    def proxy_port(self):
        return self.ports.proxy_port


def get_running_wp_port(container: str = "onionpress-wordpress") -> Optional[int]:
    """Read the host port our own WordPress container actually published.

    `detect_port_offset()` binds candidate ports to find one that's free —
    correct for allocation at first launch, wrong as a runtime lookup: once
    a stack is up, its ports are bound (by the stack itself), so the bind
    test always fails and reports "in use", concluding nothing about which
    port was actually chosen. Reading `docker port` instead asks the one
    source that's authoritative once a stack exists — the mapping Docker
    already made — for both the "still on our own port" case and the "came
    up on a different offset than last time" case (stop/start races where
    the old offset's forwards are still bound when the new ones come up).

    Returns None if the container isn't running or isn't ours to read
    (multi-user machines: this only ever inspects a container name, never
    another user's — Docker containers are per-user via the docker socket
    each user's Colima VM exposes, so there is no cross-user leakage here).
    """
    try:
        result = subprocess.run(
            ["docker", "port", container, "80"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    # Expected output: "0.0.0.0:8080" or "127.0.0.1:8080" (one line per
    # bound address; take the first).
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if ":" not in first_line:
        return None
    port_str = first_line.rsplit(":", 1)[-1]
    if not port_str.isdigit():
        return None
    port = int(port_str)
    # Offsets are 8080 + k*10000, so nothing we allocate is ever below
    # 8080 — a lower published port belongs to something else entirely
    # (e.g. a hand-edited port mapping).
    if port < 8080:
        return None
    return port


def detect_port_offset() -> PortConfig:
    """Detect available port offset for multi-user / port-conflict support.

    Probes all three host-exposed ports (WP 8080, SOCKS 9050, PROXY 9077);
    if ANY is in use, bumps the offset by 10000 and retries. Uses real
    socket binding so it detects ports bound by other users or system
    services (e.g. a system tor on 9050 from torbrowser-launcher's apt
    deps) — not just other OnionPress instances. lsof would only show
    the current user's processes.
    """
    offset = 0
    while True:
        pc = PortConfig.from_offset(offset)
        if max(pc.wp_port, pc.socks_port, pc.proxy_port) > 65535:
            return PortConfig.from_offset(0)  # fall back to default
        all_free = True
        for p in (pc.wp_port, pc.socks_port, pc.proxy_port):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                # Can't even create a socket — give up
                return PortConfig.from_offset(0)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                all_free = False
                s.close()
                break
            s.close()
        if all_free:
            return pc
        offset += 10000


def resolve_port_offset() -> PortConfig:
    """Return the port offset our own stack is actually using.

    detect_port_offset() allocates: it binds candidate ports and returns
    the first free one, which is correct before anything is running. Once
    our WordPress container is up, its ports are bound (by the container
    itself), so a fresh bind test just reports "in use" without saying
    which port that is — including the mundane case where the caller is
    asking about the very stack it's part of.

    Read the running container's published port first (authoritative —
    Docker already made this mapping) and only fall back to allocation
    when nothing is running yet. Callers that need the multi-user
    allocation behavior with no risk of misreading a foreign container
    (e.g. a first-launch check before anything of ours exists) should call
    detect_port_offset() directly instead.
    """
    running_port = get_running_wp_port()
    if running_port is not None:
        return PortConfig.from_offset(running_port - 8080)
    return detect_port_offset()
