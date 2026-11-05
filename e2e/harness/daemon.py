"""T1 (c)'s readings of the daemon and the tor-watchdog on a live node,
as plain functions where they parse, so the unit suite checks them
without a VM (tests/test_e2e_harness.py).

The rules are the product's (CLAUDE.md, "The Daemon"): the menubar and
the tray are views of daemon-state.json, which a live daemon keeps fresh
(daemon_client.STALE_AFTER_SECONDS); on Linux the daemon is the
`onionpress-daemon.service` user unit (`Restart=always`), reaching the
Docker daemon the launcher uses (#503); the watchdog runs only under
entrypoint.sh's `supervise_watchdog`.
"""

import re

STATE_FRESH = 90              # daemon_client.STALE_AFTER_SECONDS
PID_FILE = ".onionpress/daemon.pid"
DAEMON_LOGS = ".onionpress/logs"
UNIT = "onionpress-daemon.service"
TOR = "onionpress-tor"

# Daemon._setup_environment's line on Linux.
DOCKER_LINE_RE = re.compile(r"Docker: (\S+)\s*$", re.MULTILINE)
PPID_RE = re.compile(r"^PPid:\s*(\d+)", re.MULTILINE)

# The pids of tor-watchdog.py in the tor container, from /proc (the image
# promises no pgrep). The bracket keeps the shell running this from
# matching its own command line.
WATCHDOG_PIDS_SH = ('for p in /proc/[0-9]*; do '
                    'grep -qa "tor-watchdo[g].py" "$p/cmdline" 2>/dev/null && echo "${p#/proc/}"; '
                    'done; true')


# ── parsing ─────────────────────────────────────────────────────────────

def pid_of(text):
    """daemon.pid's content as a pid, or 0."""
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0


def state_age(state, now):
    """Seconds since the daemon wrote *state*; a state without
    `updated_at` is as old as the epoch."""
    try:
        return now - float(state.get("updated_at") or 0)
    except (TypeError, ValueError):
        return float(now)


def parent_pid(proc_status):
    """PPid from /proc/<pid>/status, or 0."""
    found = PPID_RE.search(proc_status or "")
    return int(found.group(1)) if found else 0


def top_level(pids, parent_of):
    """The *pids* whose parent is not one of them: the daemon among its own
    Python helpers (`Daemon.run_cli` and the like), in order."""
    members = set(pids)
    return [pid for pid in pids if parent_of(pid) not in members]


def launcher_env(text):
    """`onionpress launcher-env`'s KEY=VALUE lines as a dict."""
    return dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)


def daemon_docker_host(log_text):
    """The Docker daemon the daemon's latest start named in its log, or ""."""
    seen = DOCKER_LINE_RE.findall(log_text or "")
    return seen[-1] if seen else ""


def pids_of(text):
    return {int(p) for p in (text or "").split() if p.isdigit()}


# ── on a node ───────────────────────────────────────────────────────────

def alive(node, pid):
    return bool(pid) and node.exec(["kill", "-0", str(pid)], timeout=30).ok


def daemon_pid(node):
    return pid_of(node.read_file(PID_FILE))


def fresh_state(node):
    """The daemon's state when a live daemon wrote it within STATE_FRESH
    seconds by the node's clock, else None."""
    state = node.daemon_state()
    if state and state_age(state, node.now()) < STATE_FRESH and alive(node, state.get("pid")):
        return state
    return None


def parent_of(node, pid):
    return parent_pid(node.exec(["cat", f"/proc/{pid}/status"], timeout=30).out)


def ancestry(node, pid, depth=4):
    """*pid* and its parents, nearest first (Linux's /proc; on a Mac only
    *pid* itself)."""
    chain = [pid]
    if node.platform != "linux":
        return chain
    while len(chain) <= depth and chain[-1] > 1:
        chain.append(parent_of(node, chain[-1]))
    return chain


def unit_daemons(node):
    """The top-level Python processes in the daemon unit's cgroup."""
    r = node.exec(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", UNIT], timeout=30)
    cgroup = r.out.strip()
    if not r.ok or not cgroup:
        raise AssertionError(f"no cgroup for {UNIT}: {r.describe()}")
    procs = node.exec(["cat", f"/sys/fs/cgroup{cgroup}/cgroup.procs"], timeout=30).out.split()
    pythons = [int(pid) for pid in procs
               if node.exec(["cat", f"/proc/{pid}/comm"], timeout=30).out.strip().startswith("python")]
    return top_level(pythons, lambda pid: parent_of(node, pid))


def logged_docker_host(node):
    r = node.exec(["sh", "-c", 'cat "$1"/onionpress*.log', "sh", node.home_path(DAEMON_LOGS)], timeout=60)
    return daemon_docker_host(r.out)


def watchdog_pids(node):
    r = node.docker(["exec", TOR, "sh", "-c", WATCHDOG_PIDS_SH], timeout=30)
    if not r.ok:
        raise AssertionError(r.describe())
    return pids_of(r.out)
