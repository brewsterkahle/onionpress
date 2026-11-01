"""Linux's heartbeat client unit, stopped and started around a restore.

On Linux the OnionHeaven client is the `onionpress-heartbeat.service` user
unit (an old install: a system unit), which loads the onion service key
at start and sends it with every /online. Before that key is replaced by
a restore the unit is stopped, or its next beat would put the old key
back on the hub after the /unregister; after the restore it is started
again, with the key Tor serves now. The Mac has no such unit: its daemon
re-reads the key. This is the one platform seam of the shared backup and
restore (issue #406): `paused(os_name, log)` does nothing but run the
body on macOS.

The bash launcher's `stop_heartbeat_client` / `start_heartbeat_client`
(linux/onionpress) are the same steps for its own cases (stop, restart,
reset, key import): the user unit if active, else the system unit via
sudo; started again only where enabled, and never while
`hold_heartbeat_client_until_installed`'s runtime mask holds it (issue
#464: nothing registers an address with the hub before WordPress is
installed).
"""

import subprocess
from contextlib import contextmanager

UNIT = "onionpress-heartbeat.service"
_TIMEOUT = 60


def _run(run, argv, log=None):
    """Run *argv* (systemctl); False on a non-zero exit or no systemctl."""
    try:
        return run(argv, capture_output=True, text=True, timeout=_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        if log:
            log(f"{' '.join(argv)}: {e}")
        return False


def _is(run, verb, *scope):
    """`systemctl [--user] is-active|is-enabled UNIT`: the answer's exit."""
    return _run(run, ["systemctl", *scope, verb, UNIT])


def held(run=subprocess.run):
    """Whether the user unit is masked (the hold of issue #464)."""
    try:
        r = run(["systemctl", "--user", "is-enabled", UNIT],
                capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip().startswith("masked")


def stop(log, run=subprocess.run):
    """Stop the heartbeat client where it runs: the user unit, and an old
    install's system unit (through sudo). Failures are logged, never
    raised: the restore goes on, as the launcher's did."""
    if _is(run, "is-active", "--user"):
        log("Stopping heartbeat client...")
        _run(run, ["systemctl", "--user", "stop", UNIT], log)
    if _is(run, "is-active"):
        log("Stopping heartbeat client (system unit)...")
        _run(run, ["sudo", "systemctl", "stop", UNIT], log)


def start(log, run=subprocess.run):
    """Start it again where its unit is enabled (it then loads the key Tor
    serves now), unless the install hold keeps it masked."""
    if held(run):
        log("Heartbeat client stays held: WordPress is not installed yet")
        return
    if _is(run, "is-enabled", "--user"):
        if not _run(run, ["systemctl", "--user", "start", UNIT], log):
            log("WARNING: Failed to start heartbeat client")
    elif _is(run, "is-enabled"):
        if not _run(run, ["sudo", "systemctl", "start", UNIT], log):
            log("WARNING: Failed to start heartbeat client")


@contextmanager
def paused(os_name, log, run=subprocess.run):
    """On Linux, stop the heartbeat client before the body and start it
    again after, whether the body succeeded or not (the key it holds may
    have been replaced either way). Elsewhere, just the body."""
    if os_name != "linux":
        yield
        return
    stop(log, run)
    try:
        yield
    finally:
        start(log, run)
