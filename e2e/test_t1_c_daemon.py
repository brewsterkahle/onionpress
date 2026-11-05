"""T1 (c) — the daemon and the watchdog on a live node (issue #407,
docs/MANUAL-TEST-PLAN.md §3 and §11).

The menubar and the tray are views of `onionpress daemon`'s state file;
these checks hold the daemon to what the views rely on (harness.daemon,
where the readings and their reasons are):

- `onionpress daemon status` and daemon-state.json agree with the node.
- On Linux the daemon reaches the Docker daemon the launcher uses (#503).
- A daemon killed outright is restarted by its unit (`Restart=always`),
  and only one runs afterwards (Linux).
- `daemon offline`, what the launcher's stop and restart run first, is
  acknowledged.
- A killed tor-watchdog is restarted by entrypoint.sh's
  `supervise_watchdog`.

Each check stands alone (not incremental). Nothing here stops the site:
the daemon and the watchdog are killed, never the containers, so this
module runs before the ones that change the node.
"""

import json

import pytest

from harness import daemon
from harness.proc import wait_until

pytestmark = [pytest.mark.tier("t1"), pytest.mark.platform("any")]

DAEMON_RESTART_TIMEOUT = 180  # RestartSec=10, then a first pass to write the state
WATCHDOG_RESTART_TIMEOUT = 120


def _wait_fresh(node, label, ready=None):
    ready = ready or (lambda: daemon.fresh_state(node))
    return wait_until(ready, timeout=DAEMON_RESTART_TIMEOUT, every=10,
                      label=f"{node.name}: {label}", timings=node.timings, log=node.log)


def test_daemon_status_agrees(ready_node):
    """What `onionpress daemon status` says is what the state file says."""
    node = ready_node
    state = _wait_fresh(node, "a fresh daemon state")
    r = node.launcher(["daemon", "status", "--json"], timeout=60)
    assert r.out.strip(), f"`daemon status --json` printed nothing: {r.describe()}"
    doc = json.loads(r.out)
    assert doc.get("loaded"), f"the daemon is not loaded: {doc}"
    assert doc.get("state_fresh"), f"`daemon status` calls the state stale: {doc}"
    assert state.get("pid") == daemon.daemon_pid(node), \
        f"the state file names pid {state.get('pid')}, daemon.pid {daemon.daemon_pid(node)}"
    # On a Mac the agent's pid is the daemon's; on Linux it is the unit's
    # MainPID, the launcher the daemon runs under.
    assert doc.get("pid") in daemon.ancestry(node, state["pid"]), \
        f"`daemon status` names pid {doc.get('pid')}, not the daemon {state['pid']} or a parent of it"
    assert doc.get("onion_address") == node.address == state.get("onion_address")
    assert doc.get("ports") == state.get("ports")


@pytest.mark.platform("linux")
def test_daemon_reaches_the_launchers_docker(ready_node):
    """#503: the unit sets no DOCKER_HOST; the daemon finds the same
    socket the launcher exports (`launcher-env`), and says so in its log."""
    node = ready_node
    r = node.launcher(["launcher-env"], timeout=60)
    assert r.ok, r.describe()
    wanted = daemon.launcher_env(r.out).get("DOCKER_HOST")
    assert wanted, f"launcher-env names no DOCKER_HOST: {r.out!r}"
    used = daemon.logged_docker_host(node)
    assert used, "the daemon's log names no Docker daemon"
    assert used == wanted, f"the daemon uses {used}, the launcher {wanted}"


@pytest.mark.platform("linux")
def test_daemon_restarted_after_kill(ready_node):
    """`kill -9` the daemon: systemd starts a new one, which writes a
    fresh state, and only one daemon runs afterwards."""
    node = ready_node
    old = daemon.daemon_pid(node)
    assert daemon.alive(node, old), f"no live daemon in daemon.pid ({old})"
    r = node.exec(["kill", "-9", str(old)], timeout=30)
    assert r.ok, r.describe()

    def replaced():
        state = daemon.fresh_state(node)
        return state if state and state.get("pid") not in (None, old) else None

    state = _wait_fresh(node, "a new daemon after kill -9", replaced)
    assert not daemon.alive(node, old), f"the killed daemon {old} still runs"
    assert daemon.daemon_pid(node) == state["pid"]
    running = daemon.unit_daemons(node)
    assert running == [state["pid"]], f"daemon processes in the unit: {running}, expected {[state['pid']]}"


def test_daemon_acknowledges_offline(ready_node):
    """`daemon offline` (the launcher's stop and restart run it first)
    is acknowledged within its wait. The daemon then holds its probe
    until it sees the stack down or STOP_HOLD_SECONDS pass, which leaves
    the site serving."""
    node = ready_node
    _wait_fresh(node, "a fresh daemon state")
    r = node.launcher(["daemon", "offline", "--json"], timeout=60)
    doc = json.loads(r.out or "{}")
    assert r.ok and doc.get("ok"), f"`daemon offline` was not acknowledged: {r.describe()}"


def test_watchdog_restarted_after_kill(ready_node):
    """entrypoint.sh's supervise_watchdog starts tor-watchdog.py again."""
    node = ready_node
    before = daemon.watchdog_pids(node)
    assert len(before) == 1, f"tor-watchdog processes before: {sorted(before)}"
    r = node.docker(["exec", daemon.TOR, "kill", "-9", str(*before)], timeout=30)
    assert r.ok, r.describe()

    def restarted():
        now = daemon.watchdog_pids(node)
        return now if now and not now & before else None

    after = wait_until(restarted, timeout=WATCHDOG_RESTART_TIMEOUT, every=5,
                       label=f"{node.name}: tor-watchdog restarted", timings=node.timings, log=node.log)
    assert len(after) == 1, f"tor-watchdog processes after: {sorted(after)}"
    assert node.container_states().get(daemon.TOR) == "running"
