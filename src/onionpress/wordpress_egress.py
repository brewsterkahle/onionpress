"""WordPress and MariaDB reach nothing outside Docker (issue #144).

Everything WordPress fetches goes through Tor's SOCKS port (the
onionpress-tor-proxy mu-plugin; raw curl in OnionPress's own plugins sets
a socks5h proxy), and Docker's resolver answers it for the containers only
(`dns: 127.0.0.1`). Neither stops code that opens a socket to an address
itself: until this, WordPress and MariaDB could connect anywhere the Docker
host could, the publisher's LAN and, through the Colima VM's gateway, the
Mac's own services included, and the iptables log only recorded it.

So wordpress and db sit on a network of their own, onionpress-wordpress-
network, whose bridge docker-compose.yml names `onionpress-wp0`. tor,
onionheaven and cloudflared join it as well as onionpress-network (they
need the internet; their default route stays on onionpress-network, which
sorts first, see docker-compose.yml). On the Docker host the ONIONPRESS-WP
chain takes every packet that comes in from that bridge and either leaves
it or is addressed to the host itself, and refuses it unless it belongs to
a connection made from outside: a browser through the published port,
docker-proxy's connection to WordPress. The rules name the bridge, never a
container's address, so they can be in place before the bridge exists.

Where the rules go:

* macOS: the Colima VM, through `limactl shell`. The script is installed
  at VM_SCRIPT and a docker.service drop-in (VM_DROPIN) runs it before
  dockerd starts, so after a VM boot the containers Docker restarts find
  the rules already there.
* Linux, rootless Docker: RootlessKit's network namespace, where the
  bridges are. The launcher's user owns it, so it enters it the way
  Docker's rootless documentation does (`nsenter -U --preserve-credentials
  -n -m` into the RootlessKit child). The namespace goes with the daemon,
  so the Linux launcher re-applies the rules whenever it changes.
* Linux, system Docker: the host. The launcher is a docker-group user, not
  root, and no image here carries iptables, so a one-shot helper container
  (`--privileged --pid host --network none`, the pinned tor image) runs the
  host's own iptables in PID 1's mount and network namespaces. Membership
  of the docker group grants that already; the helper joins no network,
  takes no input from any container and is removed when it exits. For the
  boot, when Docker restarts the containers before the user's service
  runs, the .deb ships RULES_SCRIPT as a file and a docker.service drop-in
  that runs it first (linux/onionpress-wordpress-egress.conf).

The script is the same everywhere: iptables, plus ip6tables where present
(before Docker 26, a container on an IPv4-only network still had an IPv6
link-local address, and so a way to the host). It also removes the
address-keyed LOG rules the macOS launcher used to add (CLEARNET-wordpress,
CLEARNET-db): once WordPress has moved networks, those addresses belong to
other containers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from .platform import OS, OnionPressPaths, detect_os

NETWORK = "onionpress-wordpress-network"
BRIDGE = "onionpress-wp0"
CHAIN = "ONIONPRESS-WP"
LOG_PREFIX = "CLEARNET-blocked: "

VM_SCRIPT = "/usr/local/sbin/onionpress-wordpress-egress"
VM_DROPIN = "/etc/systemd/system/docker.service.d/onionpress-wordpress-egress.conf"

# Run as root in the network namespace that holds the Docker bridges.
# Arguments: apply|check|remove [bridge] [chain]. Prints one status line.
RULES_SCRIPT = r"""
# OnionPress: WordPress and MariaDB reach nothing outside Docker (#144).
# Installed by the OnionPress launcher (onionpress/wordpress_egress.py).
# Usage: onionpress-wordpress-egress apply|check|remove [bridge] [chain]
set -u
action=${1:-apply}
br=${2:-onionpress-wp0}
chain=${3:-ONIONPRESS-WP}
prefix="CLEARNET-blocked: "

reject_with() {
    if [ "$1" = iptables ]; then echo icmp-admin-prohibited; else echo icmp6-adm-prohibited; fi
}

# The chain, replaced in one commit (--noflush flushes only the chains it
# names): replies to connections made from outside go on; anything else
# is logged (rate-limited) and refused at once, so code that tries gets
# an error, not a hang.
chain_rules() {
    printf '%s\n' "*filter" ":$chain - [0:0]" \
        "-A $chain -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN" \
        "-A $chain -m limit --limit 6/min --limit-burst 10 -j LOG --log-prefix \"$prefix\" --log-level 4" \
        "-A $chain -j REJECT --reject-with $(reject_with "$1")" \
        "COMMIT"
}

restore() {
    chain_rules "$1" | "$1-restore" -w 10 --noflush 2>/dev/null \
        || chain_rules "$1" | "$1-restore" --noflush
}

apply_family() {
    ipt=$1
    command -v "$ipt" >/dev/null 2>&1 || return 2
    # Before dockerd has started (the VM's drop-in) there is no DOCKER-USER
    # yet. Docker keeps the chain's rules and moves its own jump to the top
    # of FORWARD when it starts.
    "$ipt" -w 10 -N DOCKER-USER 2>/dev/null || true
    "$ipt" -w 10 -C FORWARD -j DOCKER-USER 2>/dev/null \
        || "$ipt" -w 10 -I FORWARD 1 -j DOCKER-USER || return 1
    restore "$ipt" || return 1
    # From the bridge to anywhere else (FORWARD) and to the host itself
    # (INPUT). Between containers on the bridge nothing changes.
    "$ipt" -w 10 -C DOCKER-USER -i "$br" ! -o "$br" -j "$chain" 2>/dev/null \
        || "$ipt" -w 10 -I DOCKER-USER 1 -i "$br" ! -o "$br" -j "$chain" || return 1
    "$ipt" -w 10 -C INPUT -i "$br" -j "$chain" 2>/dev/null \
        || "$ipt" -w 10 -I INPUT 1 -i "$br" -j "$chain" || return 1
}

check_family() {
    ipt=$1
    "$ipt" -w 10 -C FORWARD -j DOCKER-USER 2>/dev/null \
        && "$ipt" -w 10 -C DOCKER-USER -i "$br" ! -o "$br" -j "$chain" 2>/dev/null \
        && "$ipt" -w 10 -C INPUT -i "$br" -j "$chain" 2>/dev/null \
        && "$ipt" -w 10 -C "$chain" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN 2>/dev/null \
        && "$ipt" -w 10 -C "$chain" -j REJECT --reject-with "$(reject_with "$ipt")" 2>/dev/null \
        && [ "$("$ipt" -w 10 -S "$chain" 2>/dev/null | wc -l | tr -d ' ')" = 4 ]
}

remove_family() {
    ipt=$1
    command -v "$ipt" >/dev/null 2>&1 || return 0
    while "$ipt" -w 10 -D DOCKER-USER -i "$br" ! -o "$br" -j "$chain" 2>/dev/null; do :; done
    while "$ipt" -w 10 -D INPUT -i "$br" -j "$chain" 2>/dev/null; do :; done
    "$ipt" -w 10 -F "$chain" 2>/dev/null
    "$ipt" -w 10 -X "$chain" 2>/dev/null
    return 0
}

# The macOS launcher's old clearnet logging: LOG rules keyed by the
# addresses wordpress and db had on onionpress-network.
remove_address_logging() {
    old=$(iptables -w 10 -S DOCKER-USER 2>/dev/null \
        | grep -e '--log-prefix "CLEARNET-wordpress: "' -e '--log-prefix "CLEARNET-db: "' \
        | sed 's/^-A /-D /')
    [ -n "$old" ] || return 0
    printf '%s\n' "*filter" "$old" "COMMIT" | iptables-restore --noflush
}

case "$action" in
    apply)
        remove_address_logging || echo "warning: the old address-keyed LOG rules are still there" >&2
        rc=0; apply_family iptables || rc=$?
        if [ "$rc" -ne 0 ]; then echo "error: iptables rules for $br not installed (rc=$rc)"; exit 1; fi
        v6=applied; rc=0; apply_family ip6tables || rc=$?
        case "$rc" in 0) ;; 2) v6=absent ;; *) v6=failed ;; esac
        if check_family iptables; then echo "ok bridge=$br ipv6=$v6"; exit 0; fi
        echo "error: iptables rules for $br did not verify"; exit 1 ;;
    check)
        if check_family iptables; then echo "ok bridge=$br"; exit 0; fi
        echo "missing bridge=$br"; exit 1 ;;
    remove)
        remove_family iptables; remove_family ip6tables
        echo "removed bridge=$br"; exit 0 ;;
    *)
        echo "usage: onionpress-wordpress-egress apply|check|remove [bridge] [chain]" >&2; exit 2 ;;
esac
"""

_DELIMITER = "ONIONPRESS_WORDPRESS_EGRESS_EOF"


def vm_dropin_text(bridge: str = BRIDGE, chain: str = CHAIN,
                   script: str = VM_SCRIPT) -> str:
    """The Colima VM's docker.service drop-in. The "-" keeps a failure from
    stopping Docker; the launcher applies the rules again, and says so,
    before it starts anything."""
    return ("# OnionPress: the WordPress network's rules, in place before Docker\n"
            "# restarts any container (onionpress/wordpress_egress.py).\n"
            "[Service]\n"
            f"ExecStartPre=-{script} apply {bridge} {chain}")


def vm_installer(bridge: str = BRIDGE, chain: str = CHAIN,
                 script: str = VM_SCRIPT, dropin: str = VM_DROPIN) -> str:
    """Shell, run as root in the Colima VM: install the script and the
    drop-in where they differ from this version's, then apply the rules."""
    dropin_text = vm_dropin_text(bridge, chain, script)
    assert _DELIMITER not in RULES_SCRIPT and _DELIMITER not in dropin_text
    return f"""set -u
script={script}
dropin={dropin}
new=$(mktemp) || exit 1
cat > "$new" <<'{_DELIMITER}'
#!/bin/sh{RULES_SCRIPT}{_DELIMITER}
if ! cmp -s "$new" "$script"; then
    mkdir -p "$(dirname "$script")" && install -m 0755 "$new" "$script" \\
        || {{ rm -f "$new"; echo "error: could not install $script"; exit 1; }}
fi
rm -f "$new"
want=$(cat <<'{_DELIMITER}'
{dropin_text}
{_DELIMITER}
)
if [ "$(cat "$dropin" 2>/dev/null)" != "$want" ]; then
    mkdir -p "$(dirname "$dropin")" && printf '%s\\n' "$want" > "$dropin" \\
        && systemctl daemon-reload \\
        || {{ echo "error: could not install $dropin"; exit 1; }}
fi
exec "$script" apply {bridge} {chain}
"""


@dataclass
class Result:
    ok: bool
    how: str      # "colima-vm", "rootless", "system-docker"; "" when nothing ran
    detail: str   # the script's status line, or why it did not run

    def line(self) -> str:
        return f"{self.detail} ({self.how})" if self.how else self.detail


def _run(argv: list, timeout: int, env: dict | None = None) -> tuple[int, str]:
    """(returncode, last line of stdout, or of stderr when stdout is empty)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"error: no answer within {timeout}s"
    except OSError as e:
        return 127, f"error: {e}"
    lines = proc.stdout.strip().splitlines() or proc.stderr.strip().splitlines()
    return proc.returncode, (lines[-1] if lines else f"rc={proc.returncode}")


def apply_in_vm(limactl: str, lima_home: str, action: str = "apply",
                instance: str = "colima", timeout: int = 60,
                bridge: str = BRIDGE, chain: str = CHAIN) -> Result:
    """macOS: as root in the Colima VM. Bounded, because `limactl shell`
    hangs for good when the guest's sshd is wedged."""
    if not limactl or not os.access(limactl, os.X_OK):
        return Result(False, "", "skipped: no bundled limactl")
    if action == "apply":
        cmd = ["sudo", "sh", "-c", vm_installer(bridge, chain)]
    else:
        cmd = ["sudo", "sh", "-c", RULES_SCRIPT, "onionpress-wordpress-egress",
               action, bridge, chain]
    rc, out = _run([limactl, "shell", "--workdir", "/", instance, "--"] + cmd,
                   timeout, dict(os.environ, LIMA_HOME=lima_home))
    return Result(rc == 0 and out.startswith(("ok", "removed")), "colima-vm", out)


def rootless_child_pid(runtime_dir: str) -> int | None:
    """The RootlessKit child, whose namespaces hold the bridges, if this
    user owns it."""
    try:
        with open(os.path.join(runtime_dir, "dockerd-rootless", "child_pid"),
                  encoding="ascii") as f:
            pid = int(f.read().strip())
        if pid > 1 and os.stat(f"/proc/{pid}").st_uid == os.getuid():
            return pid
    except (OSError, ValueError):
        pass
    return None


def rootless_netns(runtime_dir: str) -> str:
    """Which network namespace rootless Docker is in now ('' if unknown).
    It is new each time the daemon starts, and holds no rules then."""
    pid = rootless_child_pid(runtime_dir)
    if pid is None:
        return ""
    try:
        return os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        return ""


def apply_rootless(runtime_dir: str, action: str = "apply", timeout: int = 30,
                   bridge: str = BRIDGE, chain: str = CHAIN) -> Result:
    """Linux, rootless Docker: in RootlessKit's namespaces, as their owner."""
    pid = rootless_child_pid(runtime_dir)
    if pid is None:
        return Result(False, "rootless", "error: no RootlessKit child_pid of this "
                      f"user's in {os.path.join(runtime_dir, 'dockerd-rootless')}")
    rc, out = _run(["nsenter", "-U", "--preserve-credentials", "-n", "-m",
                    "-t", str(pid), "--", "sh", "-c", RULES_SCRIPT,
                    "onionpress-wordpress-egress", action, bridge, chain],
                   timeout)
    return Result(rc == 0 and out.startswith(("ok", "removed")), "rootless", out)


def helper_argv(image: str, action: str = "apply",
                bridge: str = BRIDGE, chain: str = CHAIN) -> list:
    """`docker` arguments for the system-Docker helper: the host's own
    iptables in PID 1's mount and network namespaces. No network of its own.
    The one privileged container OnionPress runs; tests/test_container_capabilities.py
    holds it to this shape. no-new-privileges as everywhere else: nsenter
    and iptables run as root and need no setuid binary."""
    return ["run", "--rm", "--privileged", "--pid", "host", "--network", "none",
            "--security-opt", "no-new-privileges:true",
            "--log-driver", "none", "--label", "onionpress.helper=wordpress-egress",
            "--entrypoint", "nsenter", image,
            "-t", "1", "-m", "-n", "--", "sh", "-c", RULES_SCRIPT,
            "onionpress-wordpress-egress", action, bridge, chain]


def apply_system_docker(docker, image: str, action: str = "apply",
                        timeout: int = 60) -> Result:
    """Linux, system Docker: the host, through the helper container."""
    result = docker.run(helper_argv(image, action), timeout=timeout, quiet=True)
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    lines = stdout.strip().splitlines() or stderr.strip().splitlines()
    out = lines[-1] if lines else f"rc={result.returncode}"
    return Result(result.ok is True and out.startswith(("ok", "removed")),
                  "system-docker", out)


def docker_is_rootless(docker) -> bool | None:
    """From `docker info`; None when Docker did not answer."""
    result = docker.run(["info", "--format", "{{json .SecurityOptions}}"],
                        timeout=20, quiet=True)
    if result.ok is not True or not isinstance(result.stdout, str):
        return None
    return "rootless" in result.stdout


def ensure(paths: OnionPressPaths, docker,
           log: Callable[[str], None] | None = None, *,
           action: str = "apply", image: str | None = None,
           runtime_dir: str | None = None) -> Result:
    """Install (action="check": verify) the rules wherever this platform's
    Docker keeps its bridges. Never raises. The caller decides what a
    failure means; the launchers start anyway and log it as a WARNING.

    On macOS only the app's own limactl and Colima home are used (from
    `paths`), never whatever is on PATH or in the environment."""
    try:
        if detect_os() == OS.MACOS:
            limactl = os.path.join(paths.bin_dir, "limactl") if paths.bin_dir else ""
            result = apply_in_vm(limactl, os.path.join(paths.colima_home, "_lima"),
                                 action)
        else:
            rootless = docker_is_rootless(docker)
            if rootless is None:
                result = Result(False, "", "error: Docker did not answer `docker info`")
            elif rootless:
                result = apply_rootless(
                    runtime_dir or os.environ.get("XDG_RUNTIME_DIR")
                    or f"/run/user/{os.getuid()}", action)
            else:
                if image is None:
                    from .containers import ONIONHEAVEN_IMAGE_PIN, image_override
                    image = (image_override("ONIONPRESS_TOR_IMAGE", paths.config_file)
                             or ONIONHEAVEN_IMAGE_PIN)
                result = apply_system_docker(docker, image, action)
    except Exception as e:  # a start must never stop here
        result = Result(False, "", f"error: {e}")
    if log:
        if result.ok:
            log(f"WordPress network egress block: {result.line()}")
        else:
            log("WARNING: WordPress network egress block not in place: "
                f"{result.line()}. WordPress and MariaDB can reach the network "
                "directly until it is.")
    return result
