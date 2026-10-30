"""`onionpress uninstall` on a Mac (issue #366): what the menubar's
Uninstall used to do in-process. Linux's launcher keeps its bash
uninstall (sudo, systemd units, /opt/onionpress).

Two steps, as every destructive command (onionpress.confirm): `--check`
asks "hub-holds-key" when an OnionHeaven hub may hold a copy of the onion
service key and OnionPress is not running (only the key in the tor
container can sign the request that makes the hub delete it), then
"uninstall" itself; the run needs both confirmed. It then asks the hubs
to delete the key (release_onionheaven), stops OnionPress through the
launcher, stops and deletes the Colima VM (the Docker volumes live in
it), removes the login item, the daemon's LaunchAgent and ~/.onionpress. The app bundle is the
person's to move to the Trash; the menubar quits after.
"""

import os
import shutil
import subprocess
import time

from . import confirm, daemon as op_daemon, login_item

DELETES = (
    "Your onion address and private key",
    "All WordPress content and data",
    "Database and configuration",
)


def hubs_holding_key(data_dir, log):
    """[(address, hub)] an OnionHeaven hub may hold a key of; [] on the hub
    itself."""
    from . import onionheaven
    ctx = onionheaven._hub_context(data_dir, log)
    pairs = onionheaven.registrations_left(ctx)
    served = ""
    try:
        from .backup import served_onion_address
        served = served_onion_address()
    except Exception:
        pass
    if served and onionheaven.is_onionheaven_instance(served):
        return []
    return [(a, h) for a, h in pairs]


def check(paths, docker, log, confirmed):
    """The questions, in order; the facts for the dialogs. Raises
    confirm.NeedsConfirmation for the first one not confirmed."""
    running = docker.container_running("onionpress-tor")
    hubs = hubs_holding_key(paths.data_dir, log)
    if hubs and not running:
        confirm.require(
            "hub-holds-key", confirmed,
            hubs=[{"content_address": a, "onionheaven_address": h} for a, h in hubs])
    confirm.require("uninstall", confirmed, deletes=list(DELETES),
                    data_dir=paths.data_dir)
    return {"running": running, "hubs": len(hubs)}


def run(paths, docker, log, launcher, out=None):
    """Uninstall. Returns what may remain on a hub (release_onionheaven's
    list: [] when nothing)."""
    from .onionheaven import release_onionheaven
    # The daemon first: its heartbeat carries the key, so it must not put
    # the key back on a hub after the /unregister below. Its launchd agent
    # is removed with the login item.
    log("Uninstall: stopping the OnionPress daemon...")
    try:
        op_daemon.stop_agent(paths.data_dir, log, offline=False)
    except Exception as e:
        log(f"Uninstall: daemon stop failed (continuing): {e}")
    # Take the key back off every OnionHeaven hub that may hold it, before
    # stopping (the key in the tor container signs the request).
    remaining = []
    try:
        if hubs_holding_key(paths.data_dir, log):
            log("Uninstall: asking OnionHeaven to delete its copy of the key...")
            remaining = release_onionheaven("uninstall", data_dir=paths.data_dir,
                                            log_func=log, out=out)
    except Exception as e:
        log(f"Uninstall: OnionHeaven unregister failed (continuing): {e}")

    log("Uninstall: stopping services...")
    try:
        subprocess.run([launcher, "stop"], capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Uninstall: stop failed (continuing): {e}")

    # Stop and delete the Colima VM: only OnionPress's instance, never a
    # system Colima. The Docker volumes lived inside it.
    colima_bin = os.path.join(paths.bin_dir, "colima") if paths.bin_dir else "colima"
    env = {**os.environ, "COLIMA_HOME": paths.colima_home,
           "LIMA_HOME": os.path.join(paths.colima_home, "_lima"),
           "LIMA_INSTANCE": "onionpress"}
    for step, args in (("Stopping", ["stop", "-f"]), ("Deleting", ["delete", "-f"])):
        log(f"Uninstall: {step} Colima VM...")
        try:
            subprocess.run([colima_bin, *args], capture_output=True, timeout=60, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"Uninstall: colima {args[0]} failed (continuing): {e}")
    # Wait for Colima to fully shut down, then kill any orphaned
    # colima/lima processes as a fallback.
    time.sleep(3)
    try:
        subprocess.run(["pkill", "-f", paths.colima_home], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    time.sleep(2)

    login_item.remove(log)
    try:
        op_daemon.remove_agent(log)
    except Exception as e:
        log(f"Uninstall: could not remove the daemon's LaunchAgent: {e}")

    if os.path.exists(paths.data_dir):
        shutil.rmtree(paths.data_dir)
        log("Uninstall: data directory removed")
    return remaining
