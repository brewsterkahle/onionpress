"""`onionpress uninstall` (issue #366): what the menubar's Uninstall used
to do in-process on a Mac and, since #529, what the Linux launcher's
173-line bash case did (the systemd units, the tray, the containers and
volumes, /opt/onionpress and the symlinks with sudo, the desktop entries,
~/.onionpress).

Two steps, as every destructive command (onionpress.confirm): `--check`
asks "hub-holds-key" when an OnionHeaven hub may hold a copy of the onion
service key and OnionPress is not running (only the key in the tor
container can sign the request that makes the hub delete it), then
"uninstall" itself; the run needs both confirmed. It then asks the hubs
to delete the key (release_onionheaven), stops OnionPress through the
launcher, stops and deletes the Colima VM (the Docker volumes live in
it), removes the login item, the daemon's LaunchAgent and ~/.onionpress,
and moves OnionPress.app to the Trash (trash_app) unless another account,
or another data folder of this one, still uses it: one bundle in
/Applications serves every account on a multi-user Mac. The menubar quits
after.
"""

import os
import pwd
import shutil
import subprocess
import time

from . import confirm, daemon as op_daemon, login_item

# Where an installed app lives: the bundle's own folder must be one of these
# (a build in a checkout or a copy elsewhere is left where it is).
APP_FOLDERS = ("/Applications", os.path.join(os.path.expanduser("~"), "Applications"))
# Below this a macOS account is a system one (_www, daemon, nobody...).
FIRST_USER_UID = 500

DELETES = (
    "Your onion address and private key",
    "All WordPress content and data",
    "Database and configuration",
)


def hubs_holding_key(data_dir, log, docker):
    """[(address, hub)] an OnionHeaven hub may hold a key of; [] on the hub
    itself. *docker* reads the served key."""
    from . import onionheaven
    ctx = onionheaven._hub_context(data_dir, log)
    pairs = onionheaven.registrations_left(ctx)
    served = ""
    try:
        from .backup import served_onion_address
        served = served_onion_address(docker)
    except Exception:
        pass
    if served and onionheaven.is_onionheaven_instance(served):
        return []
    return [(a, h) for a, h in pairs]


def check(paths, docker, log, confirmed):
    """The questions, in order; the facts for the dialogs. Raises
    confirm.NeedsConfirmation for the first one not confirmed."""
    running = docker.container_running("onionpress-tor")
    hubs = hubs_holding_key(paths.data_dir, log, docker)
    if hubs and not running:
        confirm.require(
            "hub-holds-key", confirmed,
            hubs=[{"content_address": a, "onionheaven_address": h} for a, h in hubs])
    confirm.require("uninstall", confirmed, deletes=list(DELETES),
                    data_dir=paths.data_dir)
    return {"running": running, "hubs": len(hubs)}


# Linux: the units an install may have (the current user units and the
# legacy system ones of before #407), the package's paths, the per-user
# desktop files.
LINUX_INSTALL_DIR = "/opt/onionpress"
LINUX_SYMLINKS = ("/usr/local/bin/onionpress", "/usr/local/bin/onionpress-tray")
USER_UNITS = ("onionpress", "onionpress-daemon", "onionpress-heartbeat", "onionpress-watcher.timer")
USER_UNIT_FILES = ("onionpress.service", "onionpress-daemon.service",
                   "onionpress-heartbeat.service", "onionpress-watcher.service",
                   "onionpress-watcher.timer")
SYSTEM_UNITS = ("onionpress", "onionpress-heartbeat", "onionpress-watcher.timer")
SYSTEM_UNIT_FILES = tuple(f"/etc/systemd/system/{f}" for f in
                          ("onionpress.service", "onionpress-heartbeat.service",
                           "onionpress-watcher.service", "onionpress-watcher.timer"))


def _sh(argv, *, capture=True, timeout=120):
    """Run *argv*, failures ignored (every step is best effort, as the
    bash's `|| true`); *capture* False leaves the terminal to it (sudo's
    password prompt)."""
    try:
        return subprocess.run(argv, capture_output=capture, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _remove_linux(paths, docker, log):
    """The Linux launcher's uninstall after the hub release: the user
    units stopped (the legacy system ones too), the tray and an orphaned
    systemd-inhibit killed, the containers and every onionpress-* volume
    removed (`compose down -v` skips the volumes the launcher made), the
    units disabled and their files removed, the legacy system units and
    /opt/onionpress and the symlinks with sudo, the per-user desktop
    entries, icons and autostart, then ~/.onionpress."""
    import getpass
    user = getpass.getuser()
    home = os.path.expanduser("~")
    log("Uninstall: stopping user services...")
    for unit in USER_UNITS:
        _sh(["systemctl", "--user", "stop", unit])
    for unit in SYSTEM_UNITS:
        _sh(["sudo", "systemctl", "stop", unit], capture=False)
    log("Uninstall: killing the tray...")
    _sh(["pkill", "-u", user, "-f", "onionpress-tray"])
    _sh(["pkill", "-u", user, "-f", "systemd-inhibit.*OnionPress"])
    log("Uninstall: removing containers and volumes...")
    if paths.docker_dir:
        files = [os.path.join(paths.docker_dir, "docker-compose.yml"),
                 os.path.join(paths.docker_dir, "docker-compose.cloudflare.yml")]
        try:
            docker.compose(["down", "-v"], compose_files=files, timeout=300)
        except Exception as e:
            log(f"Uninstall: compose down failed (continuing): {e}")
    try:
        r = docker.run(["volume", "ls", "-q", "--filter", "name=onionpress-"], timeout=30, quiet=True)
        for vol in (r.stdout.split() if r.ok else []):
            docker.run(["volume", "rm", vol], timeout=60, quiet=True)
    except Exception as e:
        log(f"Uninstall: volume removal failed (continuing): {e}")
    log("Uninstall: disabling user services...")
    for unit in USER_UNITS:
        _sh(["systemctl", "--user", "disable", unit])
    for name in USER_UNIT_FILES:
        try:
            os.remove(os.path.join(home, ".config", "systemd", "user", name))
        except OSError:
            pass
    _sh(["systemctl", "--user", "daemon-reload"])
    log("Uninstall: disabling and removing legacy system services...")
    for unit in SYSTEM_UNITS:
        _sh(["sudo", "systemctl", "disable", unit], capture=False)
    _sh(["sudo", "rm", "-f", *SYSTEM_UNIT_FILES], capture=False)
    _sh(["sudo", "systemctl", "daemon-reload"], capture=False)
    log(f"Uninstall: removing {LINUX_INSTALL_DIR}...")
    _sh(["sudo", "rm", "-rf", LINUX_INSTALL_DIR], capture=False)
    log("Uninstall: removing the /usr/local/bin symlinks...")
    _sh(["sudo", "rm", "-f", *LINUX_SYMLINKS], capture=False)
    log("Uninstall: removing the desktop entries, icons and autostart...")
    apps = os.path.join(home, ".local", "share", "applications")
    icons = os.path.join(home, ".local", "share", "icons", "hicolor")
    for path in (os.path.join(home, ".config", "autostart", "onionpress-tray.desktop"),
                 os.path.join(apps, "onionpress.desktop"),
                 os.path.join(apps, "onionpress-tray.desktop")):
        try:
            os.remove(path)
        except OSError:
            pass
    for root, _dirs, names in os.walk(icons):
        for name in names:
            if name.startswith("onionpress"):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
    if shutil.which("gtk-update-icon-cache"):
        _sh(["gtk-update-icon-cache", "-t", "-f", icons])
    if shutil.which("update-desktop-database"):
        _sh(["update-desktop-database", apps])
    if os.path.exists(paths.data_dir):
        shutil.rmtree(paths.data_dir)
        log("Uninstall: data directory removed")


def other_installs(data_dir, accounts=None):
    """Whether OnionPress is installed for anyone else on this Mac: every
    account's ~/.onionpress but *data_dir* (this account's own default
    counts when *data_dir* is another folder). Returns (names, unsure):
    the accounts that have one, and whether some home could not be looked
    into (then nobody can say the app is unused)."""
    accounts = pwd.getpwall() if accounts is None else accounts
    mine = os.path.realpath(data_dir)
    names, unsure = [], False
    for a in accounts:
        if a.pw_uid < FIRST_USER_UID or not a.pw_dir or a.pw_dir == "/var/empty":
            continue
        candidate = os.path.join(a.pw_dir, ".onionpress")
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            unsure = True
            continue
        if os.path.realpath(candidate) != mine:
            names.append(a.pw_name)
    return names, unsure


def _trash_item(path):
    """Move *path* to the Trash, the way Finder does where Foundation is
    there (the bundle's Python has PyObjC), so Put Back works; else a rename
    into ~/.Trash. Returns where it went. A rename, never a write into the
    bundle: the app may be running from it (the updater's rule)."""
    try:
        from Foundation import NSFileManager, NSURL
    except ImportError:
        NSFileManager = None
    if NSFileManager is not None:
        ok, where, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(path), None, None)
        if ok:
            return where.path() if where is not None else path
        raise OSError(str(err.localizedDescription()) if err is not None else "could not trash")
    trash = os.path.join(os.path.expanduser("~"), ".Trash")
    os.makedirs(trash, mode=0o700, exist_ok=True)
    name = os.path.basename(path)
    target, n = os.path.join(trash, name), 1
    while os.path.lexists(target):
        n += 1
        stem, ext = os.path.splitext(name)
        target = os.path.join(trash, f"{stem} {n}{ext}")
    os.rename(path, target)
    return target


def trash_app(app_bundle, data_dir, log, *, accounts=None, trash=_trash_item,
              running_from=__file__):
    """Move the installed OnionPress.app to the Trash once nobody on this
    Mac uses it. Returns {"trashed": where} or {"kept": path, "reason": …},
    or {} with no bundle at all. Only the bundle this code runs from: a
    checkout's CLI resolves /Applications/OnionPress.app as a fallback
    (platform.find_app_bundle), and must not trash the installed app."""
    if not app_bundle:
        return {}
    app = os.path.realpath(app_bundle)
    if (not app.endswith(".app")
            or os.path.dirname(app) not in {os.path.realpath(f) for f in APP_FOLDERS}
            or not os.path.isfile(os.path.join(app, "Contents", "Info.plist"))
            or not os.path.realpath(running_from).startswith(app + os.sep)):
        return {"kept": app, "reason": "not-installed"}
    names, unsure = other_installs(data_dir, accounts)
    if names:
        log(f"Uninstall: {app} stays: OnionPress is still installed for {', '.join(names)}")
        return {"kept": app, "reason": "other-accounts", "accounts": names}
    if unsure:
        log(f"Uninstall: {app} stays: could not tell whether another account uses it")
        return {"kept": app, "reason": "unsure"}
    try:
        where = trash(app)
    except OSError as e:
        log(f"Uninstall: could not move {app} to the Trash: {e}")
        return {"kept": app, "reason": "trash-failed"}
    log(f"Uninstall: {app} moved to the Trash")
    return {"trashed": where}


def run(paths, docker, log, launcher, out=None, os_name="macos", app_out=None):
    """Uninstall. Returns what may remain on a hub (release_onionheaven's
    list: [] when nothing). *os_name* picks the platform's removal. On a
    Mac, *app_out* (a dict) gets trash_app's answer."""
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
        if hubs_holding_key(paths.data_dir, log, docker):
            log("Uninstall: asking OnionHeaven to delete its copy of the key...")
            remaining = release_onionheaven("uninstall", data_dir=paths.data_dir,
                                            log_func=log, out=out)
    except Exception as e:
        log(f"Uninstall: OnionHeaven unregister failed (continuing): {e}")

    if os_name == "linux":
        _remove_linux(paths, docker, log)
        return remaining

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

    login_item.remove(log, data_dir=paths.data_dir)
    try:
        op_daemon.remove_agent(log, data_dir=paths.data_dir)
    except Exception as e:
        log(f"Uninstall: could not remove the daemon's LaunchAgent: {e}")

    if os.path.exists(paths.data_dir):
        shutil.rmtree(paths.data_dir)
        log("Uninstall: data directory removed")
    # Last: everything else is gone, so only other installs keep the app.
    app = trash_app(paths.app_bundle, paths.data_dir, log)
    if app_out is not None:
        app_out.update(app)
    return remaining
