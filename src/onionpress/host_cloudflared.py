"""Is a cloudflared tunnel running on the host, beside OnionPress's own?

OnionPress runs cloudflared in a container when CLOUDFLARE_TUNNEL_TOKEN is
set. A second connector for the same tunnel installed on the host (a
Homebrew or apt `cloudflared service install`, run as root) takes about
half the tunnel's requests and cannot resolve the stack's `wordpress`
name: visitors see intermittent 502s.

The daemon asks `running()` while the tunnel is on and puts the answer in
its state (`host_cloudflared`); the menubar and the tray only show it
(issue #547: the menubar ran its own `pgrep` at launch, and Linux had no
check at all).

  macOS  `pgrep -u root -f cloudflared.*tunnel.*run` (the containers run
         in the Colima VM, so the host sees only a host connector).
  Linux  /proc: a root process whose command line matches, outside a
         container's cgroup (with system Docker OnionPress's own
         cloudflared is a root process on the host too).

True / False, or None when the question could not be asked (logged by the
caller, never shown as an answer).
"""

import os
import re
import subprocess
from typing import Optional

from .platform import OS, detect_os

PATTERN = r"cloudflared.*tunnel.*run"
_PATTERN = re.compile(PATTERN)

# A process in one of these cgroups is a container's (Docker, containerd,
# Podman), OnionPress's own cloudflared among them.
CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "libpod", "podman", "/lxc")


def running(os_name: Optional[OS] = None, proc: str = "/proc", run=subprocess.run) -> Optional[bool]:
    os_name = os_name or detect_os()
    if os_name == OS.LINUX:
        return _linux_running(proc)
    try:
        r = run(["pgrep", "-u", "root", "-f", PATTERN],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return bool(r.stdout.strip())
    if r.returncode == 1:
        return False
    return None


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read(65536)
    except OSError:
        return None


def _root_uid(status: bytes) -> bool:
    for line in status.decode("utf-8", "replace").splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            return len(fields) > 1 and fields[1] == "0"
    return False


def _linux_running(proc: str) -> Optional[bool]:
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return None
    for pid in pids:
        cmdline = _read(os.path.join(proc, pid, "cmdline"))
        if not cmdline:
            continue
        command = cmdline.replace(b"\0", b" ").decode("utf-8", "replace")
        if not _PATTERN.search(command):
            continue
        status = _read(os.path.join(proc, pid, "status"))
        if status is None or not _root_uid(status):
            continue
        cgroup = (_read(os.path.join(proc, pid, "cgroup")) or b"").decode("utf-8", "replace")
        if any(marker in cgroup for marker in CONTAINER_CGROUP_MARKERS):
            continue
        return True
    return False
