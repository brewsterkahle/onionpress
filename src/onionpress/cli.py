"""OnionPress CLI entry point.

Usage: python -m onionpress.cli <command> [args]

Commands: start, stop, restart, status, address, logs, get, setup, backup, restore, reset,
hub-recovery-key, config, onionheaven
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import urlsplit

from . import __version__
from .platform import OS, detect_os, resolve_paths, detect_timezone
from .config import (
    ensure_config, ensure_secrets, read_value, detect_port_offset,
    validate_address_prefix, DEFAULTS,
)
from .ports import port_environ
from .docker import Docker
from .containers import ContainerManager
from .start import StartErrors
from .launcher_ops import BACKUP_PASSWORD_ENV
from .setup_logic import IA_PASSWORD_ENV as _IA_PASSWORD_ENV
from .setup_logic import SETUP_PASSWORD_ENV as _SETUP_PASSWORD_ENV
from .analytics_sharing import SHARING_CONFIG_KEY
from .hub_backup import RECOVERY_KEY_ENV
from . import confirm as _confirm
# Domain functions of onionheaven's (uninstall imports them from there),
# kept under their old names here for the commands and for callers.
from .onionheaven import _RETRYABLE, _file_log_func, _hub_context, release_onionheaven


# `onionpress get` (issue #70) runs curl inside this container, where Tor's
# SOCKS port is local. The name is fixed: each user has their own VM, so the
# multi-user port offset changes host port mappings only, never container names.
TOR_CONTAINER = "onionpress-tor"
TOR_SOCKS_IN_CONTAINER = "127.0.0.1:9050"
GET_DEFAULT_MAX_TIME = 60  # seconds; onion rendezvous alone can take 10-30 s


def normalize_get_url(url: str) -> str:
    """Turn what the user typed into the URL curl gets, or raise ValueError.

    Accepts a full http(s) URL or a bare host (`abc…xyz.onion`,
    `example.com/path`), which gets `http://` prepended. The result is
    validated as one printable-ASCII token with http/https and a host of
    hostname characters only. The argv is a list, so the shell never sees
    it; this check is so that what reaches `docker exec … curl` is always
    a URL argument and never a curl option (`-K`, `--config`) or garbage.
    """
    s = (url or "").strip()
    if not s:
        raise ValueError("no URL given")
    if any(not (0x21 <= ord(c) <= 0x7E) for c in s):
        raise ValueError("URL must be printable ASCII without whitespace")
    if "://" not in s:
        s = "http://" + s
    try:
        parts = urlsplit(s)
        parts.port  # raises on a non-numeric or out-of-range port
    except ValueError as e:
        raise ValueError(f"cannot parse URL: {e}") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parts.scheme!r} (use http or https)")
    host = parts.hostname or ""
    if not host:
        raise ValueError("URL has no host")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError(f"URL host {host!r} contains characters that are not valid in a hostname")
    return s


PASSWORD_ON_ARGV_MSG = ("ERROR: a password on the command line is visible to other "
                        "users of this computer, so it is refused. Leave it out to "
                        f"be prompted, or set {BACKUP_PASSWORD_ENV}.")


def read_backup_password(positional: str | None = None) -> str:
    """The password for `backup` / `restore` / `scrub`, or "" if none was given.

    Taken from the first of: $ONIONPRESS_BACKUP_PASSWORD (how the launchers,
    menubar and tray pass it), a line on stdin when stdin is not a terminal,
    or a no-echo prompt. A password given as an argument (the old positional
    form) is refused, exit 2: argv is readable by every local user, and a
    leaked child process kept one in ps until reboot (issue #504). The
    variable is removed from this process's environment so the
    docker/zip/unzip children don't inherit it.
    """
    from_env = os.environ.pop(BACKUP_PASSWORD_ENV, "")
    if positional:
        print(PASSWORD_ON_ARGV_MSG, file=sys.stderr)
        raise SystemExit(2)
    if from_env:
        return from_env
    if sys.stdin is None:
        return ""
    try:
        if not sys.stdin.isatty():
            return sys.stdin.readline().rstrip("\n")
        return getpass.getpass("Backup password: ")
    except (EOFError, KeyboardInterrupt):
        return ""


def read_hub_recovery_key(from_env: str, *, stdin_ok: bool, prompt: str) -> str:
    """An OnionHeaven hub's backup recovery key (its private half), or "".

    Taken from the first of: *from_env* (main() has already removed
    $ONIONPRESS_HUB_RECOVERY_KEY from the environment, so docker children
    never see it), a line on stdin when *stdin_ok* and stdin is not a
    terminal, or a no-echo prompt when it is. Never from argv.
    """
    if from_env:
        return from_env
    if sys.stdin is None:
        return ""
    try:
        if not sys.stdin.isatty():
            return sys.stdin.readline().strip() if stdin_ok else ""
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _hub_key_for_backup(metadata: dict, from_env: str, log) -> bytes | None:
    """For a restore: the recovery key's private half when *metadata* says
    the backup's hub data is sealed and the operator supplies its key, else
    None (said why). Checked against the backup's key id before anything
    is restored; at a terminal a key that does not fit is asked for again,
    and an empty answer restores without the hub data."""
    from . import hub_backup
    if metadata.get("onionheaven_data") != "sealed":
        if from_env:
            log("Restore: a hub recovery key was given, but this backup holds no "
                "sealed hub data")
        return None
    kid = metadata.get("onionheaven_recovery_key_id")
    kid = kid if hub_backup.is_key_id(kid) else "unknown"
    interactive = not from_env and sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        print(f"This backup holds an OnionHeaven hub's registry and registrants' "
              f"keys, sealed to recovery key {kid}. Without that key the rest "
              f"is restored and they are not: online registrants send their "
              f"keys again within a minute, offline sites cannot be taken over "
              f"until they come back.", file=sys.stderr)
    for _ in range(3 if interactive else 1):
        text = read_hub_recovery_key(
            from_env, stdin_ok=False,
            prompt="Recovery key (Enter to restore without the hub data): ")
        if not text:
            break
        try:
            return hub_backup.check_key_for_backup(text, metadata)
        except (hub_backup.RecoveryKeyError, hub_backup.SodiumUnavailableError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            log(f"Restore: the recovery key given was not used: {e}")
            if not interactive:
                break
    log(f"Restore: no usable recovery key for the hub data sealed to key {kid}; "
        f"restoring without it")
    return None


def _make_log_func(log_file: str | None = None) -> Callable[[str], None]:
    """Create a log function that writes to stderr and optionally a file."""
    def log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, file=sys.stderr)
        if log_file:
            try:
                with open(log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    return log


def _config_key_help() -> dict:
    from .config import CONFIG_KEY_HELP
    return CONFIG_KEY_HELP


def _launcher_log_func() -> Callable[[str], None]:
    """A log function in the bash launchers' `log` format on stdout, for a
    start step a launcher runs as `… 2>&1 | tee -a "$LOG_FILE"` (Phase 4,
    #120): the lines land in the log and on the launcher's terminal as the
    bash body's did."""
    def log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)
    return log


def _log_wordpress_egress_verdict(result, log: Callable[[str], None],
                                 log_file: str) -> None:
    """The launchers' block_wordpress_egress() verdict lines (Phase 4,
    #120), worded as each launcher's bash body worded them: the Mac's
    start log ticks a step that is in place and points at the log file,
    Linux's names the LAN. A failure is a WARNING: the launchers start
    anyway (the Mac notes the status in its START SUMMARY)."""
    mac = detect_os() == OS.MACOS
    if result.ok:
        log(f"{'✓ ' if mac else ''}WordPress network egress block in place: {result.line()}")
    elif mac:
        log("WARNING: WordPress network egress block not in place: "
            f"{result.line()}. WordPress and MariaDB can reach the network "
            "directly, and the Tor-side containers this Mac's own services "
            f"— see {log_file}")
    else:
        log("WARNING: WordPress network egress block not in place: "
            f"{result.line()}. WordPress and MariaDB can reach the network "
            "directly, and the Tor-side containers this machine and its LAN.")


def _start_step_manager(paths) -> ContainerManager:
    """A ContainerManager for one of the launchers' pre-`compose up` steps:
    the ports from the launcher's own ONIONPRESS_PORT_OFFSET (never the
    bind probe, which detect_port_offset would run beside a running
    stack), the launcher's log format."""
    from .config import PortConfig
    offset = os.environ.get("ONIONPRESS_PORT_OFFSET", "")
    port_config = PortConfig.from_offset(int(offset) if offset.isdigit() else 0)
    return ContainerManager(Docker(paths), paths, port_config,
                            log_func=_launcher_log_func())


class _LogRecorder:
    """A log function that remembers what it was given."""

    def __init__(self, log):
        self._log = log
        self.seen = []

    def __call__(self, msg):
        self.seen.append(msg)
        self._log(msg)


def check_for_update_report(current: str, log, json_output: bool = False, *,
                            version: str = None, pre: bool = False,
                            os_name: str = None) -> int:
    """Check GitHub for a newer release (updater.check_release: the
    latest, or *version*; *pre* allows a pre-release). Always exits 0.

    On any failure (network down, GitHub rate-limited, parse error) the
    JSON output carries an ``error`` field and ``update_available`` is
    false — callers render that to the user instead of crashing.
    ``installable`` says whether `onionpress update` on this platform
    could install the release (its package and SHA256SUMS are published;
    else ``install_problem`` says what is missing). The report also names
    the other macOS users running OnionPress (``other_instances``,
    ``active_offsets``, this install's ``port_offset``): the shared
    bundle is replaced for everyone.
    """
    from datetime import datetime, timezone
    from . import updater

    os_name = os_name or detect_os().value
    c = updater.check_release(current, log=log, version=version, pre=pre, os_name=os_name)
    error_msg = c.error
    update_available = c.release is not None and c.newer
    latest = c.release.version if c.release is not None else None
    install_problem = c.install_problem if update_available else None
    if update_available:
        release_url = f"https://github.com/{updater.RELEASE_REPO}/releases/tag/{c.release.tag}"
        release_notes = (c.release.data.get("body") or "").strip()
        published_at = c.release.data.get("published_at")
    else:
        release_url = None
        release_notes = None
        published_at = None

    others = updater.detect_other_instances()
    try:
        port_offset = int(os.environ.get("ONIONPRESS_PORT_OFFSET", "0") or 0)
    except ValueError:
        port_offset = 0
    report = {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "installable": update_available and install_problem is None,
        "install_problem": install_problem,
        "release_url": release_url,
        "release_notes": release_notes,
        "published_at": published_at,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": error_msg,
        "other_instances": others,
        "active_offsets": updater.detect_active_offsets() if others else [],
        "port_offset": port_offset,
    }

    if json_output:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Installed: {current}")
    if error_msg:
        print(f"Update check failed: {error_msg}")
        return 0
    if update_available:
        print(f"Update available: {latest}  (published {published_at or '?'})")
        if release_notes:
            snippet = (release_notes if len(release_notes) <= 800
                       else release_notes[:800] + "\n... (truncated)")
            print()
            print(snippet)
        if install_problem:
            print(f"An update is available, but 'onionpress update' cannot install it: "
                  f"{install_problem}")
        else:
            print("Run 'onionpress update' to install it.")
    else:
        print(f"Already up to date (release {c.release.tag}).")
    if others:
        print()
        print("Note: other users are running OnionPress:")
        for o in others:
            print(f"  - {o['user']} (pid {o['pid']})")
        print("Updating replaces the shared app bundle for everyone.")
    return 0


def _bundle_owner_hint(install_path: str) -> str:
    """Who owns the app bundle another account installed, and the chown
    that lets this one update it."""
    try:
        import pwd
        owner = pwd.getpwuid(os.stat(install_path).st_uid).pw_name
    except Exception:
        return ""
    hint = f"\n\nThe app is owned by the \"{owner}\" account. "
    if owner != os.environ.get("USER", ""):
        hint += ("Either update from that account, or run this in Terminal:\n\n"
                 f"sudo chown -R $(whoami):admin \"{install_path}\" && "
                 f"chmod -R g+w \"{install_path}\"")
    return hint


# What `onionpress update --from-checkout` needs and refuses.
FROM_CHECKOUT_LINUX_ONLY_MSG = ("--from-checkout installs a git checkout on Linux "
                                "(linux/install-tree.sh); a Mac's bundle is built from one.")
FROM_CHECKOUT_ALONE_MSG = "--from-checkout takes no other option."
# The install leaves the Linux launcher keeps (bash): the apt-get install
# of a checked package and a developer's checkout.
UPDATE_INSTALL_DEB_VERB = "update-install-deb"
UPDATE_FROM_CHECKOUT_VERB = "update-from-checkout"
UPDATE_TOOLS_LINUX = ("dpkg-deb", "apt-get", "sudo")


def cmd_update(paths, log, *, check: bool = False, yes: bool = False,
               json_output: bool = False, app_path: str = None, version: str = None,
               pre: bool = False, force: bool = False, from_checkout: bool = False,
               launcher: str = None, os_name: str = None) -> int:
    """`onionpress update` on both platforms: a release from GitHub (the
    latest, or --version X.Y.Z; --pre allows a pre-release), installed
    once it is newer than the installed version (--force for a reinstall
    or a downgrade) and its package matches the release's SHA256SUMS
    (updater.check_release, fetch_deb / download_and_install).

    The install step is the platform's. A Mac's bundle is replaced by the
    staged bundle swap (updater.download_and_install: the new bundle is
    copied beside the old one and renamed into place last, since this
    very process runs out of the bundle being replaced; the menubar
    relaunches after). On Linux the checked onionpress.deb goes to the
    launcher's `update-install-deb` leaf (sudo apt-get install, then the
    launcher's update_finish restarts the site); --from-checkout, for
    developers, is the launcher's `update-from-checkout` leaf alone.

    --check only reports (check_for_update_report); without --yes a
    terminal is asked and a non-terminal refused. The answer: {"ok": true,
    "version"} or {"error", "permission_denied"} (another account owns
    the Mac bundle)."""
    from . import confirm as _confirm, updater
    os_name = os_name or detect_os().value
    mac = os_name == "macos"
    if from_checkout:
        if mac:
            return _confirm.answer_error(FROM_CHECKOUT_LINUX_ONLY_MSG, json_output, code=2)
        if check or force or pre or yes or version is not None:
            return _confirm.answer_error(FROM_CHECKOUT_ALONE_MSG, json_output, code=2)
        launcher = launcher or _linux_launcher(paths)
        rc = subprocess.run([launcher, UPDATE_FROM_CHECKOUT_VERB]).returncode
        if rc != 0:
            return _confirm.answer_error(
                f"installing from the checkout failed (exit {rc}); see {paths.log_file}.",
                json_output, code=rc)
        return _confirm.answer_ok(json_output, from_checkout=True)

    if mac:
        app_path = app_path or os.environ.get("ONIONPRESS_APP_PATH") or paths.app_bundle
    current = updater.installed_version(os_name, app_path=app_path)
    if check:
        return check_for_update_report(current, log, json_output, version=version, pre=pre,
                                       os_name=os_name)

    log("Checking for OnionPress updates...")
    if not json_output:
        print(f"Installed: {current}")
        print("Checking GitHub for updates...")
    c = updater.check_release(current, log=log, version=version, pre=pre, os_name=os_name)
    if c.error:
        return _confirm.answer_error(f"{c.error} Nothing was installed.", json_output)
    release = c.release
    latest = release.version
    if not c.newer and not force:
        log(f"update: {current} is up to date (release {release.tag})")
        return _confirm.answer_ok(
            json_output,
            text=(f"Already up to date (OnionPress {current}; release {release.tag}). "
                  f"Nothing to install.\n"
                  f"To install {release.tag} anyway: onionpress update --version {latest} --force"),
            update_available=False, current=current, latest=latest)
    if c.install_problem:
        log(f"ERROR: update: {c.install_problem}")
        return _confirm.answer_error(f"{c.install_problem} Nothing was installed.", json_output)
    if not mac:
        problem = _linux_update_problem()
        if problem:
            log(f"ERROR: update: {problem}")
            return _confirm.answer_error(f"{problem} Nothing was installed.", json_output)
        launcher = launcher or _linux_launcher(paths)

    if not json_output:
        print(f"Update available: {latest}  (published {release.data.get('published_at', '?')})")
        notes = (release.data.get("body") or "").strip()
        if notes:
            print()
            print(notes if len(notes) <= 800 else notes[:800] + "\n... (truncated)")
            print()
        if mac:
            others = updater.detect_other_instances()
            if others:
                print("Note: other users are running OnionPress:")
                for o in others:
                    print(f"  - {o['user']} (pid {o['pid']})")
                print("Updating replaces the shared app bundle for everyone.")
                print()
    if not yes:
        if sys.stdin is None or not sys.stdin.isatty():
            return _confirm.answer_error(
                f"OnionPress v{latest} is available; re-run with --yes to install it.",
                json_output, code=2)
        try:
            reply = input(f"Install v{latest} now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    if not mac:
        return _update_linux(paths, log, release, current, launcher, force=force,
                             json_output=json_output)

    log(f"Update: downloading and installing v{latest}...")
    if not json_output:
        print(f"Downloading and installing v{latest}...")
    # The daemon executes from the bundle being replaced (CLAUDE.md: a
    # process running out of a half-replaced bundle has its access revoked
    # by Gatekeeper): stopped once the new bundle is staged, started again
    # from the new one right after the swap, before the menubar relaunches.
    from . import daemon as _daemon
    from . import handle_action as _ha
    launcher = _ha.default_launcher(paths, "macos")
    if app_path and not os.path.isfile(launcher):
        launcher = os.path.join(app_path, "Contents", "MacOS", "onionpress")

    def _stop_daemon():
        if _daemon.daemon_pid(paths.data_dir) or _daemon.agent_loaded_pid(paths.data_dir)[0]:
            log("Update: stopping the OnionPress daemon before the bundle swap")
            _daemon.stop_agent(paths.data_dir, log, offline=False)

    def _start_daemon():
        log("Update: starting the OnionPress daemon from the new bundle")
        _daemon.start_agent(launcher, paths.data_dir, log,
                            port_offset=os.environ.get("ONIONPRESS_PORT_OFFSET"))

    try:
        updater.download_and_install(release.data, latest, app_path, log=log,
                                     before_swap=_stop_daemon, after_swap=_start_daemon)
    except PermissionError as e:
        log(f"Auto-update failed (permission denied): {e}")
        message = (f"OnionPress v{latest} was downloaded but could not be installed "
                   f"because another user account owns the app bundle."
                   f"{_bundle_owner_hint(app_path)}")
        if json_output:
            print(_confirm.dumps({"error": message, "permission_denied": True,
                                  "version": latest}))
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return 1
    except Exception as e:
        log(f"Auto-update failed: {e}")
        return _confirm.answer_error(e, json_output)
    return _confirm.answer_ok(
        json_output,
        text=f"Installed v{latest}. Quit and relaunch OnionPress to use the new version.",
        version=latest, current=current)


def _linux_launcher(paths):
    from . import handle_action as _ha
    return _ha.default_launcher(paths, "linux")


def _linux_update_problem():
    """Why a Linux release install cannot go ahead here: this OnionPress
    runs from another tree than the package installs, or a tool the
    install needs is missing. None when it can."""
    from . import updater
    install_dir = os.environ.get("ONIONPRESS_INSTALL_DIR") or updater.RELEASE_INSTALL_DIR
    if install_dir != updater.RELEASE_INSTALL_DIR:
        return (f"this OnionPress runs from {install_dir}; the release package installs "
                f"{updater.RELEASE_INSTALL_DIR}.")
    import shutil as _shutil
    for tool in UPDATE_TOOLS_LINUX:
        if _shutil.which(tool) is None:
            return f"{tool} is not installed; it is needed to install a release."
    return None


def _update_linux(paths, log, release, current, launcher, *, force, json_output):
    """Linux's install: the release's onionpress.deb and SHA256SUMS into a
    folder of this user's own (mkdtemp: mode 700), checked
    (updater.fetch_deb), then the launcher's `update-install-deb` leaf
    (sudo apt-get install, then update_finish). The folder goes either
    way."""
    import tempfile
    from . import confirm as _confirm, updater
    log(f"Downloading {updater.RELEASE_DEB} and {updater.RELEASE_SUMS} of {release.tag}...")
    if not json_output:
        print(f"Downloading OnionPress {release.version}...")
    try:
        tmp = tempfile.mkdtemp(prefix="onionpress-update.")
    except OSError as e:
        return _confirm.answer_error(
            f"could not make a temporary folder ({e}). Nothing was installed.", json_output)
    try:
        try:
            deb_path, _ = updater.fetch_deb(release, tmp, log=log)
        except updater.UpdateError as e:
            log(f"ERROR: update: {e}")
            return _confirm.answer_error(f"{e} Nothing was installed.", json_output)
        if not json_output:
            print(f"Checked: {updater.RELEASE_DEB} matches the sha256 in the release's "
                  f"{updater.RELEASE_SUMS}.")
            print(f"Installing OnionPress {release.version} (sudo apt-get install)...")
        log(f"Installing {release.tag} with apt-get ({launcher} {UPDATE_INSTALL_DEB_VERB})...")
        argv = [launcher, UPDATE_INSTALL_DEB_VERB, deb_path] + (["--force"] if force else [])
        # The leaf's sudo may ask for a password: the terminal is its.
        rc = subprocess.run(argv).returncode
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)
    if rc != 0:
        log(f"ERROR: update: {UPDATE_INSTALL_DEB_VERB} exited {rc}")
        return _confirm.answer_error(
            f"installing the package failed. Check {paths.log_file} for details.", json_output)
    return _confirm.answer_ok(
        json_output, text=f"Installed OnionPress {release.version}.",
        version=release.version, current=current)


def cmd_uninstall(paths, docker, log, launcher, *, check: bool = False,
                  json_output: bool = False, confirmed=frozenset()) -> int:
    """`onionpress uninstall` on a Mac (onionpress.uninstall). The questions
    ("hub-holds-key" when a hub may hold the key and OnionPress is not
    running, then "uninstall") are answered with --confirmed, or at a
    terminal here; a non-terminal run without them is refused."""
    from . import confirm as _confirm, uninstall as _uninstall
    confirmed = set(confirmed)
    interactive = sys.stdin is not None and sys.stdin.isatty()
    while True:
        try:
            facts = _uninstall.check(paths, docker, log, confirmed)
            break
        except _confirm.NeedsConfirmation as e:
            if check:
                return _confirm.answer_confirm(e, json_output)
            if not interactive:
                return _confirm.answer_error(_confirm.NOT_CONFIRMED_MSG.format(what=e.what),
                                             json_output, code=_confirm.EXIT_REFUSED)
            if e.what == "hub-holds-key":
                listed = "\n".join(f"  {h['onionheaven_address']} (key of {h['content_address']})"
                                   for h in e.facts["hubs"])
                print("These OnionHeaven hubs may hold a copy of your onion service "
                      f"private key:\n{listed}\nOnionPress asks them to delete it "
                      "when you uninstall, but only while it is running.")
                try:
                    reply = input("Uninstall anyway (the hub keeps the key)? [y/N] ")
                except (EOFError, KeyboardInterrupt):
                    reply = ""
                if reply.strip().lower() not in ("y", "yes"):
                    print("Cancelled.")
                    return 1
            else:
                print("CRITICAL WARNING: uninstalling PERMANENTLY DELETES:")
                for item in e.facts["deletes"]:
                    print(f"  - {item}")
                print("Your site CANNOT BE RECOVERED unless you have a backup "
                      "(onionpress backup).")
                try:
                    reply = input("Type DELETE to confirm: ")
                except (EOFError, KeyboardInterrupt):
                    reply = ""
                if reply.strip().upper() != "DELETE":
                    print("Cancelled.")
                    return 1
            confirmed.add(e.what)
    if check:
        return _confirm.answer_ok(json_output, text="OK: nothing left to ask.", **facts)
    remaining = _uninstall.run(paths, docker, log, launcher,
                               out=sys.stderr if json_output else sys.stdout)
    if not json_output:
        print("OnionPress has been uninstalled. Move OnionPress.app to the Trash.")
        if remaining:
            from . import onionheaven
            print("Note: " + onionheaven.describe_remaining(remaining))
    return _confirm.answer_ok(json_output, remaining=remaining)


class OnionPressCLI:
    """Wires together all OnionPress modules for CLI use."""

    def __init__(self, data_dir: str = None, app_bundle: str = None):
        self.paths = resolve_paths(data_dir=data_dir, app_bundle=app_bundle)
        # This user's alone (issue #397): config, secrets and the key.
        os.makedirs(self.paths.data_dir, mode=0o700, exist_ok=True)
        self.log = _make_log_func(self.paths.log_file)

        # Ensure config exists
        ensure_config(self.paths)

        # The ports: the launcher's own offset when it says so
        # (ONIONPRESS_PORT_OFFSET, as the daemon and the start steps read
        # it), else the bind probe. Beside this install's own running stack
        # the probe found its ports taken and moved a restore to the next
        # offset (issue #507).
        from .config import PortConfig
        offset = os.environ.get("ONIONPRESS_PORT_OFFSET", "")
        self.port_config = (PortConfig.from_offset(int(offset)) if offset.isdigit()
                            else detect_port_offset())

        # Build env for Docker
        secrets = ensure_secrets(self.paths.secrets_file)
        extra_env = secrets.as_env()
        extra_env.update(port_environ(self.port_config))
        extra_env["TZ"] = detect_timezone()
        extra_env["ONIONPRESS_VERSION"] = __version__

        self.docker = Docker(self.paths, log_func=self.log, extra_env=extra_env)
        self.containers = ContainerManager(
            self.docker, self.paths, self.port_config, log_func=self.log,
        )

    def cmd_stop(self) -> int:
        """Stop OnionPress containers."""
        self.containers.stop()
        self._remove_pid_lock()
        self.log("OnionPress stopped")
        return 0

    def cmd_status(self) -> int:
        """Print container status as JSON."""
        status = self.containers.get_status()
        output = {
            "onion_address": status.onion_address,
            "wp_ready": status.wp_ready,
            "tor_bootstrapped": status.tor_bootstrapped,
            "services": status.services,
        }
        print(json.dumps(output, indent=2))
        return 0

    def cmd_logs(self) -> int:
        """Follow container logs."""
        result = self.docker.compose(
            ["logs", "-f"],
            compose_files=[os.path.join(self.paths.docker_dir, "docker-compose.yml")] if self.paths.docker_dir else None,
            timeout=0,  # Will be killed by user
        )
        return result.returncode

    def cmd_get(self, url: str, headers: bool = False,
                max_time: int = GET_DEFAULT_MAX_TIME) -> int:
        """Fetch a URL over Tor and print the response body (issue #70).

        For checking whether a .onion (yours, a friend's, the hub's
        healthcheck address) is reachable right now. `curl --socks5-hostname`
        on the host does not work through Colima's port forwarding
        (CLAUDE.md, "Colima Networking Gotcha"), so curl runs with
        `docker exec` inside the tor container, where the SOCKS port is
        local; .onion names resolve inside Tor, and clearnet URLs leave via
        a Tor exit. curl's exit code is passed through (7 = could not
        connect, 28 = timed out, ...) so scripts can tell apart "unreachable"
        from "reached, got a body". Nothing is logged to onionpress.log:
        a debugging fetch is not an app event.
        """
        try:
            target = normalize_get_url(url)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if max_time <= 0:
            print("ERROR: --max-time must be a positive number of seconds",
                  file=sys.stderr)
            return 2

        cmd = ["curl", "-sS", "--max-time", str(max_time),
               "--socks5-hostname", TOR_SOCKS_IN_CONTAINER]
        if headers:
            cmd.append("--include")
        cmd.append(target)
        # quiet=True: curl's own stderr (printed below) is the error report;
        # the generic "docker exec ... FAILED" line would only repeat it.
        result = self.docker.exec(TOR_CONTAINER, cmd,
                                  timeout=max_time + 15, quiet=True)
        if result.stdout:
            sys.stdout.write(result.stdout)
            sys.stdout.flush()
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        if result.timed_out:
            return 124
        return result.returncode

    def cmd_backup(self, password: str, output_path: str = None, username: str = None,
                   *, check: bool = False, json_output: bool = False) -> int:
        """Create a backup. `--check` verifies the credentials and says
        what the backup would be ({"ok": true, "filename", "output",
        "address", "username"}) without making one: the menubar's Backup...
        asks that before its save panel."""
        from . import confirm as _confirm
        from .backup import (create_backup, backup_filename, verify_wp_admin,
                             get_admin_username, unchecked_notes, served_onion_address)
        if not username:
            username = get_admin_username(data_dir=self.paths.data_dir, log=self.log)
        ok, err = verify_wp_admin(username, password)
        if not ok:
            return _confirm.answer_error(err, json_output)
        # The address the live key derives to names the file: the tor
        # container's hostname can lag it for a while after a key import or
        # a restore.
        addr = served_onion_address() or self.containers.get_onion_address()
        filename = backup_filename(addr, username)
        if not output_path:
            backups_dir = os.path.expanduser("~/OnionPress/backups")
            if not check:
                os.makedirs(backups_dir, exist_ok=True)
            output_path = os.path.join(backups_dir, filename)
        if check:
            return _confirm.answer_ok(
                json_output, text=f"OK: {username} verified; the backup would be {output_path}",
                filename=filename, output=output_path, address=addr, username=username)
        try:
            metadata = create_backup(
                onion_address=addr,
                username=username,
                password=password,
                output_path=output_path,
                version=__version__,
                log_func=self.log,
                # Written to the owner's disk, never handed to WordPress: an
                # OnionHeaven hub's registry and registrants' keys stay in
                # (sealed to its recovery key when one is set up), and so do
                # the whole config and the site's onion service key.
                include_hub_data=True,
                include_host_secrets=True,
                include_onion_key=True,
            )
        except Exception as e:
            self.log(f"Backup failed: {e}")
            return _confirm.answer_error(e, json_output)
        print(f"Backup saved to: {output_path}")
        notes = []
        if isinstance(metadata, dict) and metadata.get("onionheaven_data") == "unsealed":
            from .backup import HUB_DATA_UNSEALED_NOTE
            notes.append(HUB_DATA_UNSEALED_NOTE)
        # A hub or OnionHome check that did not answer: what this backup
        # may lack.
        notes.extend(unchecked_notes(metadata))
        for note in notes:
            print(f"WARNING: {note}", file=sys.stderr)
        try:
            size = os.path.getsize(output_path)
        except OSError:
            size = 0
        return _confirm.answer_ok(json_output, output=output_path, bytes=size, notes=notes)

    def cmd_restore_check(self, password: str, backup_path: str, hub_recovery_key: str,
                          confirmed, json_output: bool) -> int:
        """`onionpress restore <zip> --check`: the backup's facts for the
        UI's own summary (address, date, username, version, whether its key
        is in it), without restoring anything. A hub's backup with sealed
        hub data asks "sealed-hub-data-left-out" unless a recovery key was
        given (checked against the backup's key id: a wrong one is the
        answer's error) or that was confirmed."""
        from . import confirm as _confirm, hub_backup
        from .backup import read_backup_metadata, metadata_text
        try:
            metadata = read_backup_metadata(backup_path, password)
        except ValueError as e:
            return _confirm.answer_error(e, json_output)
        except Exception as e:
            return _confirm.answer_error(f"Could not read backup: {e}", json_output)
        # read_backup_metadata validated the address and username; the other
        # fields are only shown, so they go through metadata_text.
        hub_data = metadata.get("onionheaven_data")
        facts = {
            "onion_address": metadata.get("onion_address") or "unknown",
            "backup_date": metadata_text(metadata, "backup_date"),
            "username": metadata_text(metadata, "username"),
            "onionpress_version": metadata_text(metadata, "onionpress_version"),
            "excludes_onion_key": metadata.get("excludes_onion_key") is True,
            "onionheaven_data": hub_data if hub_data in ("sealed", "unsealed") else None,
        }
        if hub_data == "sealed":
            kid = metadata.get("onionheaven_recovery_key_id")
            facts["recovery_key_id"] = kid if hub_backup.is_key_id(kid) else "unknown"
            if hub_recovery_key:
                try:
                    hub_backup.check_key_for_backup(hub_recovery_key, metadata)
                except (hub_backup.RecoveryKeyError, hub_backup.SodiumUnavailableError) as e:
                    return _confirm.answer_error(e, json_output)
                facts["recovery_key"] = "accepted"
            elif "sealed-hub-data-left-out" not in confirmed:
                return _confirm.answer_confirm(
                    _confirm.NeedsConfirmation("sealed-hub-data-left-out",
                                               key_id=facts["recovery_key_id"], **facts),
                    json_output)
        return _confirm.answer_ok(
            json_output, text="\n".join(f"{k}: {v}" for k, v in facts.items()), **facts)

    def cmd_restore(self, password: str, backup_path: str,
                    hub_recovery_key: str = "", *, confirmed=frozenset(),
                    json_output: bool = False) -> int:
        """Restore from a backup via install-from-backup (_restore). On Linux
        the heartbeat client unit, which holds the current key, is stopped
        before the restore asks the OnionHeaven hubs to delete their copy
        and started again after it, with the restored key
        (heartbeat_unit.paused: the one platform seam, issue #406)."""
        from . import heartbeat_unit
        with heartbeat_unit.paused(detect_os().value, self.log):
            return self._restore(password, backup_path, hub_recovery_key,
                                 confirmed=confirmed, json_output=json_output)

    def _restore(self, password: str, backup_path: str,
                 hub_recovery_key: str = "", *, confirmed=frozenset(),
                 json_output: bool = False) -> int:
        """Restore from a backup via install-from-backup: tear the install down
        to a clean state and rebuild it directly from the backup (key + DB +
        content), instead of overwriting a live install in place. No vanity
        generation and no onion-service key-swap churn.

        An OnionHeaven hub's backup with sealed hub data: its registry and
        registrants' keys are restored only with the recovery key
        (*hub_recovery_key*, from $ONIONPRESS_HUB_RECOVERY_KEY, else asked
        for at a terminal). It is checked against the backup before
        anything is restored, and the sealed data is opened into memory,
        never into a file, right after extraction. Without it, or with
        another key, everything else is restored and the hub data is not.

        A backup without an onion service key (the Mac Settings page's)
        keeps this install's key and address: the key volumes stay, no hub
        is asked to let go of the key, and nothing is seeded. With no
        address to keep (OnionPress not running, a new install) it is
        refused before anything is changed (backup.NO_KEY_TO_KEEP_MSG).

        Not at a terminal, a sealed backup without a key is restored
        without its hub data only with --confirmed sealed-hub-data-left-out
        (the menubar asks first: cmd_restore_check); refused otherwise.
        With *json_output* the last line is {"ok": true, "address",
        "hub_data": "restored"|"left-out"|"plain"|"none", "left_on_hubs"}.
        """
        import shutil
        from . import confirm as _confirm
        # What was logged, for the answer (the hub data's fate).
        self.log = recorder = _LogRecorder(self.log)
        # The key seed below runs from the stack's pinned tor image; make
        # sure it is here before anything is changed.
        image = self.containers.ensure_tor_image()
        if not image:
            self.log("Restore failed: the tor image the key seed runs from is "
                     "not available; nothing was changed")
            return 1
        hub = {}
        try:
            from . import hub_backup
            from .backup import (prepare_install_from_backup, read_backup_metadata,
                                 open_sealed_hub_data, staged_onion_key_address)
            from .key_manager import is_valid_onion_address
            metadata = read_backup_metadata(backup_path, password)
            interactive = sys.stdin is not None and sys.stdin.isatty()
            if (metadata.get("onionheaven_data") == "sealed" and not hub_recovery_key
                    and not interactive and "sealed-hub-data-left-out" not in confirmed):
                from .backup import sealed_skipped_message
                self.log("Restore refused: the backup's sealed hub data needs the "
                         "recovery key or --confirmed sealed-hub-data-left-out")
                return _confirm.answer_error(
                    sealed_skipped_message(metadata) + " Give the key in "
                    f"{RECOVERY_KEY_ENV}, or run again with --confirmed "
                    "sealed-hub-data-left-out to restore everything else.",
                    json_output, code=_confirm.EXIT_REFUSED)
            hub_key = _hub_key_for_backup(metadata, hub_recovery_key, self.log)

            def open_hub_data(staging, metadata):
                if hub_key is None:
                    return
                try:
                    hub["data"] = open_sealed_hub_data(staging, metadata, hub_key, self.log)
                except (hub_backup.RecoveryKeyError, hub_backup.HubDataError,
                        hub_backup.SodiumUnavailableError) as e:
                    self.log(f"Restore: the OnionHeaven hub data will not be restored: {e}")

            staging, meta = prepare_install_from_backup(
                backup_path, password, self.log, data_dir=self.paths.data_dir,
                after_extract=open_hub_data)
            addr = meta.get("onion_address", "")
            # Derived from the backup's key by the seed step (or, for a
            # backup without a key, the address this install keeps);
            # checked again, before the teardown, because it names the
            # directory bind-mounted below.
            if not is_valid_onion_address(addr):
                raise ValueError("the restored onion address is not valid")
            # A backup without a key (the Mac Settings page's): this
            # install's key, key volumes and address stay as they are.
            keeps_key = not staged_onion_key_address(staging, meta)
        except Exception as e:
            self.log(f"Restore failed during extract/seed: {e}")
            return 1
        # A backup of another address replaces this install's key: ask the
        # OnionHeaven hubs that may hold the current one to delete their
        # copy first, while it is here to sign the request. The restored
        # address's own registration (a backup of this same site) stays.
        # Not a reason to stop: what remains is reported and recorded.
        # Nothing to ask when the key stays.
        if not keeps_key:
            try:
                release_onionheaven("restore", keep_address=addr,
                                    data_dir=self.paths.data_dir)
            except Exception as e:
                self.log(f"Restore: could not ask the OnionHeaven hub to delete "
                         f"the old key ({e}); continuing")
        torn_down = False

        def tor_back(why):
            # A failure past the teardown left the site down with its
            # containers rebuilt (#508: Linux stayed down until a restart
            # by hand). Tor comes back, best effort, so the site, however
            # far the import got, is reachable at its address; the staging
            # is kept and the next start imports it again (both platforms).
            self.log(f"Restore: {why}; starting Tor again so the site is reachable "
                     f"(the site may be incomplete: run the restore again, or start "
                     f"OnionPress, which imports the kept backup once more)")
            try:
                self.containers.start_tor()
            except Exception as e:
                self.log(f"Restore: Tor could not be started again: {e}")

        try:
            # Teardown: stop + wipe data + keystore volumes so the rebuild adopts
            # the seeded key and imported backup cleanly (no stale keystore
            # overriding the restored identity, no in-place overwrite).
            self.containers.stop()
            torn_down = True
            # onionpress-arti-state is the key volume's pre-2026-09-25 name;
            # wiping it too keeps the launcher's migration from bringing the
            # replaced identity back. The key volumes stay when the backup
            # has no key: they hold the identity this install keeps.
            key_volumes = () if keeps_key else (
                "onionpress-onion-keys", "onionpress-arti-state",
                "onionpress-tor-state")
            for vol in key_volumes + ("onionpress-db-data", "onionpress-wordpress-data",
                                      "onionpress-persistent-data"):
                # Quiet: a volume that is not there (onionpress-arti-state
                # on every install since 2026-09-25) is no failure (#507).
                if self.docker.run(["volume", "rm", vol], timeout=20, quiet=True).ok:
                    self.log(f"Removed volume: {vol}")
            if keeps_key:
                self.log(f"Restore: the backup holds no onion service key; "
                         f"this install keeps its key volumes and its address {addr}")
            else:
                # The restored key replaces whatever a key volume migration
                # was still copying.
                from .containers import forget_key_volume_migration
                forget_key_volume_migration(self.paths.data_dir)

            # Seed the fresh key volume from the backup key (a backup with
            # one) so Tor serves the restored identity on first start.
            if not keeps_key:
                # The entrypoint converts the PEM to C Tor's key files when
                # tor-state is empty. Mirrors the launcher's first-run key
                # install. Bind-mount the host vanity-keys dir, which
                # seed_onion_key_for_install wrote under shared/ (inside the
                # Colima mount, so the bind works on macOS as well as Linux).
                vanity_addr_dir = os.path.join(
                    self.paths.data_dir, "shared", "vanity-keys", addr)
                # The same seed as a first-run start's (containers.KEY_SEED_SCRIPT:
                # no network, DAC_OVERRIDE only, the pinned tor image).
                from .containers import run_key_seed
                seed = run_key_seed(self.docker, vanity_addr_dir, image, timeout=30)
                if not seed.ok:
                    # Started without it, Tor would mint a new address under the
                    # restored site. Without the volume, the next start installs
                    # the key from vanity-keys/ and finishes the import.
                    self.docker.run(["volume", "rm", "onionpress-onion-keys"], timeout=20)
                    self.log("Restore failed: the restored key could not be installed "
                             "in the onionpress-onion-keys volume; not starting Tor "
                             "under a new address (start OnionPress again to retry)")
                    return 1

            # Rebuild: fresh containers adopt the seeded key; import the backup
            # artifacts into them; then bring up Tor with the restored identity.
            # OnionHome's name registry last: it is rebuilt inside
            # onionpress-tor, which the teardown removed.
            self.containers.start_core()
            self.containers.wait_for_wordpress()
            if self.cmd_import_backup_artifacts(staging, name_registry=False,
                                                hub_data=hub.get("data")) != 0:
                self.log("Restore: artifact import failed — staging kept for retry")
                tor_back("the import did not finish")
                return 1
            self.containers.start_tor()
            self.containers.wait_for_tor()
            from .backup import restore_staged_name_registry
            restore_staged_name_registry(staging, self.log)

            # Clean up staging + marker only after a successful import.
            shutil.rmtree(staging, ignore_errors=True)
            marker = os.path.join(self.paths.data_dir,
                                  ".install-from-backup")
            if os.path.exists(marker):
                os.remove(marker)
            # Rebuild the image sizes the backup left out (#257) — detached,
            # so this command returns now; the marker survives an
            # interruption and every launcher start re-kicks it until done.
            from . import derivatives
            derivatives.spawn_regeneration(
                self.paths.data_dir, self.log, self.paths.log_file)
            print("Restore complete. OnionPress rebuilt from backup.")
            if json_output:
                from . import onionheaven
                from .backup import HUB_DATA_RESTORED_LOG
                ctx = _hub_context(self.paths.data_dir, self.log)
                left = [{"content_address": a, "onionheaven_address": h, "why": "no-key"}
                        for a, h in onionheaven.registrations_left(ctx) if a != addr]
                answer = {"ok": True, "address": addr, "left_on_hubs": left}
                if meta.get("onionheaven_data") == "sealed":
                    if any(HUB_DATA_RESTORED_LOG in m for m in recorder.seen):
                        answer["hub_data"] = "restored"
                    else:
                        from .backup import HUB_DATA_NOT_RESTORED_NOTE
                        answer["hub_data"] = "left-out"
                        answer["hub_data_note"] = HUB_DATA_NOT_RESTORED_NOTE
                else:
                    answer["hub_data"] = "plain" if meta.get("onionheaven_data") else "none"
                print(_confirm.dumps(answer))
            return 0
        except Exception as e:
            self.log(f"Restore failed: {e}")
            if torn_down:
                tor_back("the restore failed after the containers were rebuilt")
            return 1

    def cmd_import_backup_artifacts(self, staging: str, name_registry: bool = True,
                                    hub_data=None) -> int:
        """Import a backup's container-side artifacts (DB, wp-content,
        OnionHeaven/OnionHome data, multisite constants) into the already-running
        containers, then migrate the DB schema. Used by the launcher's
        install-from-backup marker hook. Operates on an already-extracted staging
        dir, so no password is needed here. name_registry=False leaves
        OnionHome's name registry to the caller (cmd_restore, before Tor runs).
        *hub_data*: a hub's sealed data, opened by cmd_restore with the
        recovery key; the launcher's hook has no key, and a sealed backup's
        hub data is left out there (said so).
        """
        try:
            from .backup import (restore_container_artifacts,
                                 validate_backup_metadata)
            meta_path = os.path.join(staging, "metadata.json")
            if not os.path.exists(meta_path):
                meta_path = os.path.join(staging, ".", "metadata.json")
            metadata = {}
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    metadata = validate_backup_metadata(json.load(f))
            restore_container_artifacts(staging, metadata, self.log,
                                        name_registry=name_registry,
                                        hub_data=hub_data,
                                        data_dir=self.paths.data_dir)
            # Migrate DB schema for cross-version backups (non-fatal).
            res = self.docker.wp("core", "update-db", timeout=120)
            if not res.ok:
                self.log("Install-from-backup: wp core update-db reported a "
                         "problem (continuing)")
            self.log("Install-from-backup: artifacts imported")
            return 0
        except Exception as e:
            self.log(f"Install-from-backup import failed: {e}")
            return 1

    def cmd_regenerate_derivatives(self) -> int:
        """Rebuild the image sizes a restored backup left out, if a marker
        says one did (#257). Synchronous — the launchers run it detached in
        the background. Exit 0 = nothing pending or sweep complete (markers
        cleared); 1 = could not finish, markers kept for the next start.
        """
        from . import derivatives
        # Detached by derivatives.spawn_regeneration with its output
        # appended to the log file: log to the file only then, or every
        # line lands twice (once from stderr, once from the logger).
        log = self.log
        if sys.stderr is None or not sys.stderr.isatty():
            log = _file_log_func(self.paths.log_file)
        ok = derivatives.regenerate_if_pending(
            self.paths.data_dir, log, docker_bin=self.docker.docker_bin)
        return 0 if ok else 1

    # ─── An OnionHeaven hub's backup recovery key ───────────────────────

    @staticmethod
    def cmd_hub_recovery_key_create(data_dir: str, force: bool = False) -> int:
        """Make the hub's backup recovery key: keep the public half here,
        print the private half once (hub_backup.create_recovery_key)."""
        from . import hub_backup
        try:
            private_text, kid = hub_backup.create_recovery_key(data_dir, force=force)
        except (hub_backup.RecoveryKeyError, hub_backup.SodiumUnavailableError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"""OnionHeaven hub backup recovery key created (key id {kid}).

From now on this hub's backups seal its registry and its registrants' onion
service keys to this key. Here is its private half. It is shown this once
and is not stored on this computer:

    {private_text}

Keep it off this machine: in a password manager, or printed and stored
safely. A restore of the hub's data needs it. Without it a restore brings
back everything else: registrants that are online send their keys again
within a minute, and sites that are offline cannot be taken over until they
come back. Nobody loses their address.

Check a copy you kept with: onionpress hub-recovery-key check
Public half: {hub_backup.public_key_path(data_dir)}""")
        return 0

    @staticmethod
    def cmd_hub_recovery_key_show(data_dir: str) -> int:
        """Say whether a recovery key is set up, and its id (the public
        half only; the private half is not here)."""
        from . import hub_backup
        try:
            public_raw = hub_backup.load_public_key(data_dir)
        except hub_backup.RecoveryKeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if public_raw is None:
            print("No hub backup recovery key is set up: a hub's backups hold its "
                  "registrants' keys under the backup password only. Set one up "
                  "with: onionpress hub-recovery-key create")
            return 3
        print(f"Hub backup recovery key: key id {hub_backup.key_id(public_raw)}")
        print(f"Public half: {hub_backup.public_key_path(data_dir)}")
        return 0

    @staticmethod
    def cmd_hub_recovery_key_check(data_dir: str, from_env: str = "") -> int:
        """Check that a recovery key kept off the hub is the one set up
        here. The key comes from $ONIONPRESS_HUB_RECOVERY_KEY, stdin or a
        prompt; exit 0 if it is, 1 if not."""
        from . import hub_backup
        try:
            public_raw = hub_backup.load_public_key(data_dir)
        except hub_backup.RecoveryKeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if public_raw is None:
            print("ERROR: no hub backup recovery key is set up here.", file=sys.stderr)
            return 1
        text = read_hub_recovery_key(from_env, stdin_ok=True, prompt="Recovery key: ")
        try:
            given = hub_backup.key_id(hub_backup.public_key_of(
                hub_backup.parse_private_key(text)))
        except (hub_backup.RecoveryKeyError, hub_backup.SodiumUnavailableError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        wanted = hub_backup.key_id(public_raw)
        if given != wanted:
            print(f"That is a different recovery key (key id {given}); the one set "
                  f"up here is key id {wanted}.")
            return 1
        print(f"That is this hub's backup recovery key (key id {wanted}).")
        return 0

    @staticmethod
    def cmd_hub_recovery_key_restore(backup_path: str, password: str,
                                     from_env: str = "") -> int:
        """Restore only a hub's sealed registry and registrants' keys from a
        backup (after a restore that did not have the recovery key, such as
        one from the welcome screen). OnionPress must be running. The
        backup password and then the key come from the environment, stdin
        or prompts. The key is checked before anything is written, and
        every key is checked to be its address's (hub_backup.check)."""
        from . import hub_backup
        from .backup import read_sealed_hub_member, write_hub_data
        log = _make_log_func()
        try:
            metadata, blob = read_sealed_hub_member(backup_path, password)
            hub_backup.check_sealed_format(metadata)
            kid = metadata.get("onionheaven_recovery_key_id")
            text = read_hub_recovery_key(
                from_env, stdin_ok=True,
                prompt=f"Recovery key (key id "
                       f"{kid if hub_backup.is_key_id(kid) else 'unknown'}): ")
            private_raw = hub_backup.check_key_for_backup(text, metadata)
            hub = hub_backup.check(hub_backup.unpack(hub_backup.unseal(blob, private_raw), log),
                                   log)
        except (ValueError, hub_backup.SodiumUnavailableError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        try:
            write_hub_data(hub, log)
        except Exception as e:
            log(f"Restore: could not restore the OnionHeaven data: {e}")
            return 1
        print("OnionHeaven registry and registrants' keys restored.")
        return 0

    @staticmethod
    def cmd_put_wp_tree(src: str, dest: str, merge: bool = False) -> int:
        """Hand the host folder *src* to the WordPress container at *dest*,
        written as www-data (wp_files.put_tree): the bash launchers' restore
        and plugin installs. Messages go to stderr."""
        from . import wp_files
        log = _make_log_func()
        try:
            r = wp_files.put_tree(src, dest, merge=merge)
        except Exception as e:
            log(f"Could not copy {os.path.basename(src)} into WordPress: {e}")
            return 1
        if r.returncode != 0:
            log(f"Could not copy {os.path.basename(src)} into WordPress: "
                f"{r.stderr.strip()[-300:]}")
            return 1
        return 0

    def cmd_smoke_test_wayback(self) -> int:
        """End-to-end smoke test of the Wayback archiving pipeline.

        Publishes a throwaway post, verifies save_post queues the right
        URLs, force-drains the queue, and polls SPN for each job_id until
        it reports success or terminal error. Always cleans up the test
        post.
        """
        from .wayback_smoke import smoke_test_wayback
        return smoke_test_wayback(self.log)

    def cmd_check_for_update(self, json_output: bool = False,
                             current: str = None) -> int:
        """Check GitHub for a newer release (check_for_update_report)."""
        return check_for_update_report(current or __version__, self.log, json_output)

    # ─── Vanity address + admin-password (shared with Mac/tray) ─────────

    def cmd_generate_vanity(self) -> int:
        """Generate a vanity .onion address using mkp224o inside the tor container.

        Reads `ADDRESS_PREFIX` from config (default `op2`). On success the
        key bundle lands in `~/.onionpress/shared/vanity-keys/<addr>.onion/`,
        ready for the existing key-install path in start_containers.
        """
        from .launcher_ops import (
            tor_image_has_mkp224o, generate_vanity_in_container,
            DEFAULT_TOR_IMAGE,
        )
        # Validated with config.validate_address_prefix(), not a local length
        # check. The old `2 <= len(prefix) <= 6` disagreed with the macOS
        # launcher's max of 5 (so the same config generated a 6-character
        # address here and silently fell back to "op2" there) and checked no
        # character set at all — a prefix containing 0, 1, 8 or 9 is not
        # base32, can never match any address, and sent mkp224o searching
        # forever.
        # read_value() returns its default only when the key is ABSENT; a line
        # `ADDRESS_PREFIX=` with nothing after it yields "". The validator
        # accepts "" (to UI callers it means "use the default"), so without
        # this substitution the empty string went to mkp224o as an empty
        # filter. Before this branch a bare length check happened to reject
        # it; keep rejecting it, by giving it the default it stands for.
        prefix = (read_value(self.paths.config_file, "ADDRESS_PREFIX",
                             DEFAULTS["ADDRESS_PREFIX"])
                  or DEFAULTS["ADDRESS_PREFIX"])
        prefix_ok, prefix_error, prefix_suggestion = validate_address_prefix(prefix)
        if not prefix_ok:
            self.log(f"ADDRESS_PREFIX is invalid, skipping vanity generation: "
                     f"{prefix_error.splitlines()[0]}")
            if prefix_suggestion:
                self.log(f"Set ADDRESS_PREFIX={prefix_suggestion} in "
                         f"{self.paths.config_file} to use the closest valid prefix.")
            return 2

        if not tor_image_has_mkp224o(DEFAULT_TOR_IMAGE):
            self.log("Tor image is missing or has no mkp224o; "
                     "skipping vanity generation")
            return 3

        vanity_dir = os.path.join(self.paths.shared_dir, "vanity-keys")
        running_marker = os.path.join(self.paths.data_dir, ".vanity-running")
        try:
            with open(running_marker, "w") as f:
                f.write(str(os.getpid()))
            addr = generate_vanity_in_container(
                prefix=prefix,
                vanity_dir=vanity_dir,
                log_func=self.log,
            )
        finally:
            try:
                os.remove(running_marker)
            except OSError:
                pass

        if not addr:
            return 1

        # Derive Arti key format from the C Tor secret key that mkp224o
        # produced. The existing helper handles this in-place.
        try:
            from . import key_manager
            key_manager.ensure_key_formats(os.path.join(vanity_dir, addr))
        except Exception as e:
            self.log(f"WARNING: ensure_key_formats failed: {e}")

        print(addr)
        return 0

    def cmd_admin_password(self) -> int:
        """Print the auto-generated admin password (recovery hatch)."""
        from .launcher_ops import get_admin_password
        pw = get_admin_password(self.paths.data_dir)
        if not pw:
            print("No admin password file found", file=sys.stderr)
            return 1
        print(pw)
        return 0

    def cmd_reset(self, yes: bool = False, *, check: bool = False,
                  json_output: bool = False, confirmed=frozenset()) -> int:
        """Reset OnionPress — wipe all data and start fresh.

        The questions ("hub-holds-key" when an OnionHeaven hub may hold the
        key and OnionPress is not running to ask it, then "reset") are
        answered with --confirmed, at a terminal here, or by --yes; a
        non-terminal run without them is refused (onionpress.confirm).
        """
        from . import confirm as _confirm, onionheaven
        interactive = sys.stdin is not None and sys.stdin.isatty()
        ctx = _hub_context(self.paths.data_dir, self.log)
        held = onionheaven.registrations_left(ctx)
        running = self.docker.container_running("onionpress-tor")
        if check:
            try:
                if held and not running:
                    _confirm.require("hub-holds-key", confirmed, hubs=[
                        {"content_address": a, "onionheaven_address": h} for a, h in held])
                _confirm.require("reset", confirmed, deletes=[
                    "WordPress, database, Tor state", "onion address keys",
                    "config and secrets"])
            except _confirm.NeedsConfirmation as e:
                return _confirm.answer_confirm(e, json_output)
            return _confirm.answer_ok(json_output, text="OK: nothing left to ask.",
                                      running=running)
        if not yes and "reset" not in confirmed:
            if not interactive:
                return _confirm.answer_error(_confirm.NOT_CONFIRMED_MSG.format(what="reset"),
                                             json_output, code=_confirm.EXIT_REFUSED)
            print()
            print("  This will ERASE everything: WordPress, database, Tor state,")
            print("  onion address keys, config, and secrets.")
            print()
            print("  To preserve your data, run 'onionpress backup' first.")
            print()
            try:
                input("  Press Enter to continue or Ctrl+C to cancel...")
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return 1

        # The key goes with the reset: ask the OnionHeaven hubs that may
        # hold a copy to delete it first, while the key is here to sign
        # the request.
        remaining = release_onionheaven("reset", data_dir=self.paths.data_dir,
                                        out=sys.stderr if json_output else sys.stdout)
        if (not yes and "hub-holds-key" not in confirmed
                and any(i["why"] in _RETRYABLE for i in remaining)):
            if not interactive:
                return _confirm.answer_error(
                    "Not reset: an OnionHeaven hub keeps the key (start OnionPress "
                    "and try again, or run again with --confirmed hub-holds-key).",
                    json_output, code=_confirm.EXIT_REFUSED)
            try:
                answer = input("  Reset anyway? The hub keeps the key listed "
                               "above. [y/N] ")
            except (KeyboardInterrupt, EOFError):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("\n  Cancelled.")
                return 1

        self.log("Resetting OnionPress (full wipe)...")

        # Stop everything
        try:
            self.containers.stop()
        except Exception:
            pass

        # Remove Docker volumes
        result = self.docker.run(
            ["volume", "ls", "-q", "--filter", "name=onionpress-"],
            timeout=15,
        )
        if result.ok:
            for vol in result.output.splitlines():
                vol = vol.strip()
                if vol:
                    self.docker.run(["volume", "rm", vol], timeout=15)
                    self.log(f"Removed volume: {vol}")

        # Wipe data files (keep colima VM). The OnionHeaven registration
        # record stays when a hub may still hold a key: it is what tells
        # the user what remains on which hub.
        from .onionheaven import REGISTRATION_RECORD
        for name in [
            "secrets", "onionpress.log", ".last_status_state",
            "config", "config.bak",
            *([] if remaining else [REGISTRATION_RECORD]),
            "onion_address", "healthcheck-address",
        ]:
            path = os.path.join(self.paths.data_dir, name)
            if os.path.exists(path):
                os.remove(path)

        import shutil
        vanity_dir = os.path.join(self.paths.shared_dir, "vanity-keys")
        if os.path.exists(vanity_dir):
            shutil.rmtree(vanity_dir)

        self.log("Removed keys, config, secrets, and logs")
        print("\n  Reset complete. Run 'onionpress start' to start fresh.")
        self._remove_pid_lock()
        return _confirm.answer_ok(json_output, remaining=remaining)

    # -- PID lock --

    def _remove_pid_lock(self) -> None:
        try:
            os.remove(self.paths.pid_file)
        except OSError:
            pass


def _settings_page_log(data_dir: str):
    paths = resolve_paths(data_dir=data_dir)
    return _make_log_func(paths.log_file)


def cmd_settings_page_backup(output_path: str, password: str, data_dir: str,
                             json_output: bool = False) -> int:
    """`onionpress settings-page-backup <zip>` (both launchers' case, issue
    #406). Handed to WordPress by handle-action, and the password came
    from there: refused on an OnionHeaven hub or OnionHome, or a node that
    could not be checked (exit 3, SettingsPageBackupRefused's message), and
    never a hub's registry or registrants' keys, nor the Cloudflare tunnel
    token or any other config WordPress is not shown
    (backup.TUNNEL_TOKEN_KEY), nor the site's own onion service key
    (backup.ONION_KEY_LEFT_OUT_NOTE, the answer's "note": the app's
    Backup... and `onionpress backup` have it)."""
    from . import confirm as _confirm
    from .backup import (create_backup, get_admin_username, served_onion_address,
                         SettingsPageBackupRefused, ONION_KEY_LEFT_OUT_NOTE)
    log = _settings_page_log(data_dir)
    try:
        metadata = create_backup(
            served_onion_address() or "unknown", get_admin_username(data_dir=data_dir, log=log),
            password, output_path, __version__, log,
            include_hub_data=False, include_host_secrets=False,
            include_onion_key=False)
    except SettingsPageBackupRefused as e:
        return _confirm.answer_error(e, json_output, code=_confirm.EXIT_REFUSED)
    except Exception as e:
        log(f"Settings page backup failed: {e}")
        return _confirm.answer_error(e, json_output)
    facts = {}
    if isinstance(metadata, dict) and metadata.get("excludes_onion_key") is True:
        facts["note"] = ONION_KEY_LEFT_OUT_NOTE
    return _confirm.answer_ok(json_output, **facts)


def cmd_settings_page_restore(backup_file: str, password: str, data_dir: str,
                              json_output: bool, confirmed) -> int:
    """`onionpress settings-page-restore <zip>` (both launchers' case, issue
    #406): the in-place backup.restore_from_backup with
    through_wordpress=True, never the install-from-backup `restore` (issue
    #341). The file and its password came through WordPress: refused on
    an OnionHeaven hub or OnionHome (or a node that could not be checked),
    and for a backup holding a hub's keys or OnionHome's name registry;
    neither volume is written. A backup of another address replaces this
    one only with --confirmed address-change on a Mac (the handler asks the
    person at the Mac first: exit 3, {"error", "confirm": "address-change",
    "current", "new"}) and never on Linux, which has no one to ask
    (backup.ADDRESS_CHANGE_REFUSED_MSG, exit 3); one without a key keeps
    it. Before the key changes, the OnionHeaven hubs that may hold the
    current one are asked to delete it, while it can still sign; on Linux
    the heartbeat client unit that holds it is stopped first and started
    again after (heartbeat_unit)."""
    from . import confirm as _confirm, heartbeat_unit
    from .backup import (restore_from_backup, SettingsPageRestoreRefused,
                         ADDRESS_CHANGE_REFUSED_MSG)
    log = _settings_page_log(data_dir)
    linux = detect_os() == OS.LINUX
    heartbeat_stopped = False

    def confirm_address_change(current, new):
        if linux:
            # No one at this host to ask (the Mac asks the person at the
            # Mac): the page never changes the address. `onionpress
            # restore` on the host does it.
            log(f"Restore: refused (the Settings page's restore): the backup "
                f"is of {new}, not of this site's {current or 'address'}")
            raise SettingsPageRestoreRefused(ADDRESS_CHANGE_REFUSED_MSG)
        _confirm.require("address-change", confirmed, current=current, new=new)
        return True

    def before_key_change(restored_address):
        nonlocal heartbeat_stopped
        if linux:
            # The heartbeat client holds the key about to be replaced: stopped
            # before the hubs are asked to let go of it, or its next beat
            # would put it back (started again below; the handler's restart
            # after a success stops and starts it once more).
            heartbeat_unit.stop(log)
            heartbeat_stopped = True
        try:
            release_onionheaven("restore", keep_address=restored_address,
                                data_dir=data_dir, log_func=log, out=sys.stderr)
        except Exception as e:
            log(f"Restore: could not ask OnionHeaven to delete the old key (continuing): {e}")

    try:
        restore_from_backup(backup_file, password, log, through_wordpress=True,
                            before_key_change=before_key_change,
                            confirm_address_change=confirm_address_change)
    except _confirm.NeedsConfirmation as e:
        # No --check on this command (the handler's first run is the
        # check): refused, exit 3, the question in the answer.
        return _confirm.answer_refused_question(e, json_output)
    except SettingsPageRestoreRefused as e:
        return _confirm.answer_error(e, json_output, code=_confirm.EXIT_REFUSED)
    except Exception as e:
        log(f"Settings page restore failed: {e}")
        return _confirm.answer_error(e, json_output)
    finally:
        if heartbeat_stopped:
            heartbeat_unit.start(log)
    return _confirm.answer_ok(json_output)


def cmd_prepare_restore(backup_file: str, password: str, data_dir: str,
                        json_output: bool = False) -> int:
    """`onionpress prepare-restore <zip>`: the welcome screen's Restore from
    backup, staged before the first start (backup.prepare_install_from_backup:
    extract, seed the onion key, apply the config overrides, write the
    .install-from-backup marker); the launcher's start then imports the
    database and content. A hub's sealed registry and keys are not opened
    here (no recovery key on this path; `onionpress hub-recovery-key
    restore` later): the answer's "note" says so."""
    from . import confirm as _confirm
    from .backup import prepare_install_from_backup, sealed_skipped_message
    log = _settings_page_log(data_dir)

    def log_both(msg):
        log(msg)
        print(msg, file=sys.stderr)
    try:
        _staging, meta = prepare_install_from_backup(backup_file, password, log_both,
                                                     data_dir=data_dir)
    except Exception as e:
        log(f"Restore from backup: prepare failed: {e}")
        return _confirm.answer_error(e, json_output)
    facts = {"onion_address": meta.get("onion_address") or "",
             "onionheaven_data": meta.get("onionheaven_data")}
    if meta.get("onionheaven_data") == "sealed":
        facts["note"] = sealed_skipped_message(meta)
    return _confirm.answer_ok(json_output, text="Staged; start OnionPress to restore.", **facts)


def cmd_prepare_restore_check(backup_file: str, password: str,
                              json_output: bool = False) -> int:
    """`onionpress prepare-restore <zip> --check`: the welcome screen's
    Restore from backup opens the backup here, before it starts, rather
    than in the menubar's process (issue #475): the password checked and
    the metadata read (backup.peek_backup_metadata), nothing written. The
    answer is the backup's onion address and whether its key is left out
    (a Settings page backup, which cannot set up a new install)."""
    from . import confirm as _confirm
    from .backup import peek_backup_metadata
    try:
        meta = peek_backup_metadata(backup_file, password)
    except Exception as e:
        return _confirm.answer_error(f"Could not open the backup: {e}", json_output)
    address = meta.get("onion_address")
    return _confirm.answer_ok(
        json_output, text="The backup opens.",
        onion_address=address if isinstance(address, str) else "",
        excludes_onion_key=meta.get("excludes_onion_key") is True)


# ---------------------------------------------------------------------------
# The subcommands (issue #483): _COMMANDS at the end is the table, one row
# per subcommand: its name, the function that adds its parser (and its
# nested ones) and the handler main() dispatches to. The table's order is
# the parsers' order, so the help's. A new subcommand is its builder, its
# handler and one row (and a row in tests/test_cli_dispatch.py).
#
# Handlers that need no OnionPressCLI never construct it: its setup creates
# config and secrets and probes ports.

class _Run:
    """What a handler gets: the parsed arguments, the data directory, the
    hub recovery key main() took out of the environment, and the parsers
    (for a grouping command's help)."""

    def __init__(self, args, parser, sub, hub_recovery_key: str):
        self.args = args
        self.data_dir = args.data_dir or os.path.join(os.path.expanduser("~"), ".onionpress")
        self.hub_recovery_key = hub_recovery_key
        self._parser = parser
        self._sub = sub

    def help(self, command: str | None = None) -> int:
        """Print *command*'s help (default: onionpress's own); exit 1."""
        (self._sub.choices[command] if command else self._parser).print_help()
        return 1


def _plain(help: str):
    """The builder of a subcommand with no arguments of its own."""
    return lambda sub, name: sub.add_parser(name, help=help)


def _cli(run: _Run) -> "OnionPressCLI":
    """OnionPressCLI, for the commands that need its setup. The
    provisioning steps, scrub and verify-admin-password construct it
    without using it: they always have, and its setup (config and secrets
    creation) runs before them."""
    return OnionPressCLI(data_dir=run.args.data_dir)


def _app_setup(run: _Run):
    """The app's functions as subcommands (issue #382, onionpress.app_commands):
    the paths and a Docker wrapper, no OnionPressCLI setup (config and
    secrets creation, port probing)."""
    paths = resolve_paths(data_dir=run.args.data_dir)
    log = _file_log_func(paths.log_file)
    return paths, log, Docker(paths, log_func=log), detect_os().value


# ---- the app's functions (onionpress.app_commands) ----

def _add_version(sub, name):
    p = sub.add_parser(name, help="Print the OnionPress version (About)")
    p.add_argument("--json", action="store_true", help="As JSON (version, platform)")


def _run_version(run):
    from . import app_commands
    return app_commands.version_command(detect_os().value, json_output=run.args.json,
                                        out=sys.stdout)


def _add_address(sub, name):
    p = sub.add_parser(
        name,
        help="Print the onion address (--url: the site's shareable URL with its "
             "/<onionname>/ path; --copy: put that URL on the clipboard)")
    p.add_argument("--url", action="store_true",
                   help="The shareable URL, as the menu's Copy Onion Address")
    p.add_argument("--copy", action="store_true",
                   help="Copy the URL to the clipboard (pbcopy, wl-copy, xclip or xsel)")


def _run_address(run):
    from . import app_commands
    paths, _log, docker, os_name = _app_setup(run)
    return app_commands.address_command(paths, docker, os_name, url=run.args.url,
                                        copy=run.args.copy, out=sys.stdout)


def _add_logs(sub, name):
    p = sub.add_parser(
        name,
        help="Follow the container logs; with --app, --launcher or --web print "
             "the end of that OnionPress log instead (-n N lines, -f to follow)")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--app", dest="which", action="store_const", const="app",
                       help="The app's log (the Mac daemon's loops; on Linux onionpress.log)")
    which.add_argument("--launcher", dest="which", action="store_const", const="launcher",
                       help="The launcher's onionpress.log (every start's steps and errors)")
    which.add_argument("--web", dest="which", action="store_const", const="web",
                       help="The web usage log: the site's visits, the app's own probes left out")
    p.add_argument("-n", "--lines", type=int, default=200, metavar="N",
                   help="How many lines from the end (default 200; 0: all)")
    p.add_argument("-f", "--follow", action="store_true",
                   help="Keep printing as the log grows")


def _run_logs(run):
    args = run.args
    if not args.which:
        return _cli(run).cmd_logs()
    from . import app_commands
    paths, _log, docker, os_name = _app_setup(run)
    return app_commands.logs_command(paths, docker, os_name, args.which,
                                     lines=args.lines, follow=args.follow, out=sys.stdout)


def _add_check_reachability(sub, name):
    p = sub.add_parser(
        name,
        help="Test the site through the independent onionheaven Tor client, as the "
             "Settings page's Test Reachability does (exit 0 reachable, 1 not, 2 no address)")
    p.add_argument("--json", action="store_true",
                   help="The result as JSON (reachable, http_code, address, tested_at)")


def _run_check_reachability(run):
    from . import app_commands
    paths, _log, docker, _os_name = _app_setup(run)
    return app_commands.check_reachability_command(paths, docker, json_output=run.args.json,
                                                   out=sys.stdout)


def _add_check_docker(sub, name):
    p = sub.add_parser(
        name,
        help="Say whether this machine's Docker Engine and Docker Compose are new enough "
             "for OnionPress (exit 0 yes, 1 too old or no `docker compose`, 2 docker did "
             "not answer)")
    p.add_argument("--json", action="store_true",
                   help="The result as JSON (ok, engine, compose, problems, min_engine, min_compose)")


def _run_check_docker(run):
    from . import docker_versions
    _paths, _log, docker, _os_name = _app_setup(run)
    return docker_versions.check_command(docker, json_output=run.args.json, out=sys.stdout)


def _add_open(sub, name):
    p = sub.add_parser(
        name,
        help="Open the site in a browser, logged in as the owner: site (local), "
             "admin (wp-admin), settings (the OnionPress Settings page) or onion "
             "(the .onion address, in Tor Browser where it is installed)")
    p.add_argument("what", choices=("site", "admin", "settings", "onion"))


def _run_open(run):
    from . import app_commands
    paths, _log, docker, _os_name = _app_setup(run)
    return app_commands.open_command(run.args.what, paths, docker, out=sys.stdout)


def _add_onionname(sub, name):
    p = sub.add_parser(
        name,
        help="The site's onionname (its OnionHome name): show | set NAME [--json]")
    onionname_sub = p.add_subparsers(dest="onionname_command")
    p_show = onionname_sub.add_parser("show", help="The recorded name, address and "
                                                   "whether it is registered")
    p_show.add_argument("--json", action="store_true", help="As JSON")
    p_set = onionname_sub.add_parser(
        "set", help="Pick (another) onionname: checked, registered with OnionHome "
                    "while the site is running, recorded in the config; a taken "
                    "name is refused with suggestions (exit 2)")
    p_set.add_argument("name", metavar="NAME")
    p_set.add_argument("--json", action="store_true", help="As JSON")


def _run_onionname(run):
    from . import app_commands
    args = run.args
    paths, log, docker, os_name = _app_setup(run)
    if not args.onionname_command:
        return run.help("onionname")
    if args.onionname_command == "show":
        return app_commands.onionname_show(paths, json_output=args.json, out=sys.stdout)
    # The registrar signs with the served key (key_manager.extract_keys:
    # plain `docker`), so the docker environment _hub_context sets up.
    _hub_context(args.data_dir, log)
    return app_commands.onionname_set(args.name, paths, docker, os_name, log,
                                      json_output=args.json, out=sys.stdout)


# ---- OnionPressCLI's commands ----

def _add_get(sub, name):
    p = sub.add_parser(
        name,
        help="Fetch a URL over Tor (curl inside the tor container) and print the body",
    )
    p.add_argument(
        "url",
        help="http(s) URL, or a bare host such as abc...xyz.onion (http:// assumed)")
    p.add_argument(
        "-i", "--headers", action="store_true",
        help="Include the response headers in the output (curl --include)")
    p.add_argument(
        "--max-time", type=int, default=GET_DEFAULT_MAX_TIME, metavar="SECONDS",
        help=f"Give up after this many seconds (default {GET_DEFAULT_MAX_TIME})")


def _run_get(run):
    args = run.args
    return _cli(run).cmd_get(args.url, headers=args.headers, max_time=args.max_time)


# The password is read by read_backup_password (environment, stdin or a
# prompt). The positional is the old interface, still parsed so that a
# script using it gets the refusal, not a mis-read argument (issue #504).
def _add_backup(sub, name):
    p = sub.add_parser(
        name, help="Create a backup",
        epilog=f"The WP admin password (also used to encrypt the zip) is read "
               f"from ${BACKUP_PASSWORD_ENV}, else stdin, else a prompt.")
    p.add_argument(
        "password", nargs="?",
        help="Deprecated: visible to other local users; leave it out")
    p.add_argument("output", nargs="?", help="Output path (default: ~/OnionPress/backups/)")
    p.add_argument(
        "-o", "--output", dest="output_opt", metavar="OUTPUT",
        help="Output path, for use without the positional password")
    p.add_argument("--user", help="WP admin username (default: auto-resolve)")
    _confirm.add_arguments(p)


def _run_backup(run):
    args = run.args
    # Before OnionPressCLI: its Docker wrapper snapshots os.environ for every
    # docker child, and read_backup_password takes the password out of it.
    password = read_backup_password(args.password)
    if not password:
        return _confirm.answer_error("a password is required", args.json)
    return _cli(run).cmd_backup(password, args.output_opt or args.output, args.user,
                                check=args.check, json_output=args.json)


def _add_restore(sub, name):
    p = sub.add_parser(
        name, help="Restore from backup",
        epilog=f"The backup password is read from ${BACKUP_PASSWORD_ENV}, "
               f"else stdin, else a prompt.")
    p.add_argument(
        "password", nargs="?",
        help="Deprecated: visible to other local users; leave it out")
    p.add_argument("backup_file", help="Path to backup .zip file")
    _confirm.add_arguments(p)


def _run_restore(run):
    args = run.args
    # Before OnionPressCLI, as backup's.
    password = read_backup_password(args.password)
    if not password:
        if args.check:
            # The UI asks for the password next, worded with the login
            # the file's name carries (for the prompt only).
            from .backup import backup_filename_username
            return _confirm.answer_ask(
                "password", args.json,
                backup_user=backup_filename_username(args.backup_file))
        return _confirm.answer_error("a password is required", args.json)
    cli = _cli(run)
    # A hub's recovery key, when one was given (cmd_restore asks at a
    # terminal otherwise).
    confirmed = _confirm.given(args)
    if args.check:
        return cli.cmd_restore_check(password, args.backup_file, run.hub_recovery_key,
                                     confirmed, args.json)
    return cli.cmd_restore(password, args.backup_file, run.hub_recovery_key,
                           confirmed=confirmed, json_output=args.json)


def _add_reset(sub, name):
    p = sub.add_parser(name, help="Wipe all data and start fresh")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    _confirm.add_arguments(p)


def _run_reset(run):
    args = run.args
    return _cli(run).cmd_reset(yes=args.yes, check=args.check, json_output=args.json,
                               confirmed=_confirm.given(args))


def _add_update(sub, name):
    p = sub.add_parser(
        name,
        help="Check GitHub for a new OnionPress release and install it: a Mac's "
             "app bundle is replaced (the staged swap); on Linux the release's "
             "onionpress.deb is installed with sudo apt-get once it matches the "
             "release's SHA256SUMS (--check to only check, --yes to skip the prompt)")
    p.add_argument("--check", action="store_true", help="Only check; install nothing")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Install without prompting (scripts, the menubar)")
    p.add_argument("--json", action="store_true",
                   help="Answer with one JSON object on the last line")
    p.add_argument("--version", metavar="X.Y.Z", default=None,
                   help="That release instead of the latest")
    p.add_argument("--pre", action="store_true",
                   help="Allow a pre-release (without --version: the newest release, "
                        "pre-release or not)")
    p.add_argument("--force", action="store_true",
                   help="Install even when the release is not newer (a reinstall or a downgrade)")
    p.add_argument("--from-checkout", action="store_true",
                   help="(Linux, for developers) install a git checkout instead of a release "
                        "($HOME/tmp/onionpress, $HOME/onionpress or /opt/onionpress-src), "
                        "unchecked beyond git; takes no other option")
    p.add_argument("--launcher", default=None,
                   help="(Linux) the launcher whose install steps run (the .deb's by default)")


def _run_update(run):
    # Replaces the bundle this process runs from (a Mac) or the package it
    # runs from (Linux): no OnionPressCLI setup, nothing but the updater.
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    return cmd_update(paths, _make_log_func(paths.log_file), check=args.check,
                      yes=args.yes, json_output=args.json, version=args.version,
                      pre=args.pre, force=args.force, from_checkout=args.from_checkout,
                      launcher=args.launcher)


def _add_uninstall(sub, name):
    p = sub.add_parser(
        name,
        help="(macOS) Remove OnionPress: the onion address and key, the site, "
             "the container VM and ~/.onionpress (asks first)")
    _confirm.add_arguments(p)


def _run_uninstall(run):
    # About to remove the data directory: no OnionPressCLI setup (it
    # would create config and secrets there).
    from . import handle_action as _ha
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    log = _make_log_func(paths.log_file)
    os_name = detect_os().value
    if os_name != "macos":
        return _confirm.answer_error(
            "onionpress uninstall is the Linux launcher's own command", args.json)
    return cmd_uninstall(paths, Docker(paths, log_func=log), log,
                         _ha.default_launcher(paths, os_name), check=args.check,
                         json_output=args.json, confirmed=_confirm.given(args))


# ---- OnionHeaven ----

def _add_onionheaven_unregister(sub, name):
    p = sub.add_parser(
        name,
        help="Ask every OnionHeaven hub that may hold one of this install's "
             "onion service keys to delete its copy (before a reset, key "
             "import, restore or uninstall; the tor container must be "
             "running). Exit 0 when no hub holds anything any more")
    p.add_argument("--reason", default="unregister",
                   help="Why, for the record and the log (reset, ...)")
    p.add_argument("--keep-address", default="",
                   help="An address whose key stays (a restore of it): "
                        "its registrations are left alone")


def _run_onionheaven_unregister(run):
    # Run by both launchers before a reset, key import, restore or
    # uninstall: no OnionPressCLI setup (config/secrets creation, port
    # probing) on a path that is about to remove them.
    from .key_manager import is_valid_onion_address
    args = run.args
    keep = (args.keep_address or "").strip()
    if keep and not is_valid_onion_address(keep):
        print("ERROR: --keep-address is not an onion address", file=sys.stderr)
        return 2
    remaining = release_onionheaven(args.reason, keep_address=keep or None,
                                    data_dir=args.data_dir)
    # 0: no hub holds anything; 1: a hub may, and starting OnionPress
    # or trying again could still take it back (the launchers ask
    # before going on); 3: a hub may, but nothing here can sign for it
    # any more (reported, recorded).
    if any(i["why"] in _RETRYABLE for i in remaining):
        return 1
    return 3 if remaining else 0


def _run_onionheaven_registrations(run):
    from . import onionheaven
    ctx = _hub_context(run.args.data_dir)
    for addr, hub in onionheaven.registrations_left(ctx):
        print(f"{addr} {hub}")
    return 0


def _add_check_for_update(sub, name):
    p = sub.add_parser(
        name,
        help="Check GitHub for a newer release (always exits 0)",
    )
    p.add_argument("--json", action="store_true",
                   help="Emit canonical JSON report on stdout")
    p.add_argument("--current", help="Override detected current version")


# ---- WordPress provisioning (the launchers' post-start steps) ----

def _add_provision_post_install(sub, name):
    p = sub.add_parser(
        name,
        help="Run the WordPress post-install steps (multisite + theme + mu-plugins)",
    )
    p.add_argument(
        "--themes-dir", required=True,
        help="Source directory containing the onionpress theme dir")
    p.add_argument(
        "--plugins-dir", required=True,
        help="Source directory containing mu-plugins, sunrise.php, icons")


def _run_provision_post_install(run):
    _cli(run)
    from . import multisite
    return multisite.provision_post_install(
        themes_dir=run.args.themes_dir,
        plugins_dir=run.args.plugins_dir,
        log_func=print,
    )


def _run_verify_admin_password(run):
    _cli(run)
    from .backup import verify_wp_admin_password_any
    pw = sys.stdin.read().rstrip("\n")
    if not pw:
        return 1
    ok, _ = verify_wp_admin_password_any(pw)
    return 0 if ok else 1


def _multisite_step(step: str):
    """The handler of a provisioning step: multisite.<step>, exit 0 when
    it succeeds."""
    def handle(run):
        _cli(run)
        from . import multisite
        return 0 if getattr(multisite, step)(log_func=print) else 1
    return handle


def _add_scrub(sub, name):
    p = sub.add_parser(
        name,
        help="Full lifecycle test: backup → uninstall → install → restore → verify",
    )
    p.add_argument(
        "password", nargs="?",
        help="Deprecated (visible to other users): WordPress admin password; "
             "set $ONIONPRESS_BACKUP_PASSWORD or be prompted instead")
    p.add_argument(
        "--clean", action="store_true",
        help="Delete the backup zip after a successful scrub")


def _run_scrub(run):
    _cli(run)
    from . import scrub as _scrub
    # As backup's password: the environment (removed at once, so no child
    # inherits it), else scrub prompts; one on argv is refused (#504).
    password = os.environ.pop(BACKUP_PASSWORD_ENV, "") or None
    if run.args.password:
        print(PASSWORD_ON_ARGV_MSG, file=sys.stderr)
        return 2
    return _scrub.run_scrub(
        password=password,
        clean=run.args.clean,
    )


# ---- backup and restore helpers for the launchers ----

def _add_import_backup_artifacts(sub, name):
    p = sub.add_parser(
        name,
        help="Import a backup's container-side artifacts into the running "
             "containers (install-from-backup; no password needed)")
    p.add_argument(
        "--staging", required=True,
        help="Path to the already-extracted backup staging directory")


def _run_regenerate_derivatives_pending(run):
    # The launchers' regenerate_derivatives_pending() (Phase 4, #120):
    # the two markers regenerate-derivatives itself acts on, asked the
    # same way (derivatives.regeneration_pending). No OnionPressCLI
    # setup; exit 0 = a rebuild is owed.
    from . import derivatives
    paths = resolve_paths(data_dir=run.args.data_dir)
    pending = derivatives.regeneration_pending(paths.data_dir, Docker(paths).docker_bin)
    return 0 if pending else 1


# An OnionHeaven hub's backup recovery key. Its private half is printed
# once by create and otherwise read from $ONIONPRESS_HUB_RECOVERY_KEY,
# stdin or a prompt, never from argv.
def _add_hub_recovery_key(sub, name):
    p = sub.add_parser(
        name,
        help="An OnionHeaven hub's backup recovery key: the hub's registry and "
             "registrants' keys in its backups are sealed to it",
        epilog="The private half is read from $ONIONPRESS_HUB_RECOVERY_KEY, "
               "else stdin, else a prompt; never give it as an argument.")
    hrk = p.add_subparsers(dest="hrk_command")
    p_create = hrk.add_parser(
        "create", help="Make the key; keep the public half here and print the "
                       "private half once, to keep off this machine")
    p_create.add_argument("--force", action="store_true",
                          help="Replace a key already set up (backups sealed "
                               "to it still need its private half)")
    hrk.add_parser("show", help="Say whether a key is set up, and its id")
    hrk.add_parser("check", help="Check that a private half you kept is the key "
                                 "set up here")
    p_restore = hrk.add_parser(
        "restore", help="Restore only the hub's sealed registry and registrants' "
                        "keys from a backup (OnionPress must be running; the "
                        "backup password, then the key)")
    p_restore.add_argument("backup_file", help="Path to the backup .zip file")


def _run_hub_recovery_key(run):
    # Needs only the data directory (restore: docker too): no
    # OnionPressCLI setup.
    args, data_dir = run.args, run.data_dir
    if args.hrk_command == "create":
        return OnionPressCLI.cmd_hub_recovery_key_create(data_dir, force=args.force)
    if args.hrk_command == "show":
        return OnionPressCLI.cmd_hub_recovery_key_show(data_dir)
    if args.hrk_command == "check":
        return OnionPressCLI.cmd_hub_recovery_key_check(data_dir, run.hub_recovery_key)
    if args.hrk_command == "restore":
        password = read_backup_password()
        if not password:
            print("ERROR: a password is required.", file=sys.stderr)
            return 1
        return OnionPressCLI.cmd_hub_recovery_key_restore(
            args.backup_file, password, run.hub_recovery_key)
    return run.help("hub-recovery-key")


def _add_put_wp_tree(sub, name):
    p = sub.add_parser(
        name,
        help="Copy a host folder into the WordPress container, written as "
             "www-data (restore and plugin-install helper)")
    p.add_argument("--src", required=True, help="Host folder")
    p.add_argument("--dest", required=True,
                   help="Folder in the container (replaced whole unless --merge)")
    p.add_argument("--merge", action="store_true",
                   help="Unpack over what is at --dest instead of replacing it")


def _add_open_login(sub, name):
    p = sub.add_parser(
        name,
        help="Open a localhost or .onion URL of this site in the default "
             "browser, logged in as the admin (one-time token, never in argv)")
    p.add_argument("url", help="e.g. http://localhost:8080/wp-admin/")


def _run_open_login(run):
    # Needs only docker and a browser: skip OnionPressCLI's setup.
    from .launcher_ops import open_login
    try:
        open_login(run.args.url)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


# ---- what the WordPress container may see (the Linux launcher's status) ----

def _run_safe_config(run):
    # Read-only, and run by the Linux launcher every 30 s: skip
    # OnionPressCLI's setup (config/secrets creation, port probing).
    from .config import read_config, redact_config
    config = read_config(os.path.join(run.data_dir, "config"))
    print(json.dumps(redact_config(config), indent=2))
    return 0


def _add_wordpress_logs(sub, name):
    p = sub.add_parser(
        name,
        help="Print the end of a log, scrubbed for the WordPress container "
             "(the status page's recent-logs.txt)")
    p.add_argument("--log-file", required=True, help="Log to read")


def _run_wordpress_logs(run):
    # Run by the Linux launcher on every status write; read-only like
    # safe-config, so it skips OnionPressCLI's setup too.
    from . import redact
    try:
        sys.stdout.write(redact.wordpress_log_excerpt(run.args.log_file, run.data_dir))
    except OSError as e:
        print(f"ERROR: {e.strerror or e}", file=sys.stderr)
        return 1
    return 0


def _run_wordpress_status(run):
    # As wordpress-logs.
    from . import redact
    try:
        status = json.load(sys.stdin)
    except ValueError as e:
        print(f"ERROR: status is not JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(status, dict):
        print("ERROR: status is not a JSON object", file=sys.stderr)
        return 1
    print(json.dumps(redact.status_for_wordpress(status), indent=2))
    return 0


def _run_write_status(run):
    # The Linux launcher's write_status() (issue #523): the daemon's two
    # status documents from one pass (onionpress.status_writer, the one
    # writer for both platforms), logged in the launcher's format. Every
    # 30 s beside a live stack, so no OnionPressCLI setup; always 0.
    from . import status_writer
    paths = resolve_paths(data_dir=run.args.data_dir)
    return status_writer.run_once(paths, _launcher_log_func())


# ---- the launchers' start steps (Phase 4, #120) ----

def _run_start_step(run):
    # The launchers' retire_old_onionheaven_containers() and
    # migrate_tor_network(): ContainerManager's own steps (start_core runs
    # them too), logged in the launcher's format; no OnionPressCLI setup.
    # Exit 0 whatever is found: neither step may stop a start.
    paths = resolve_paths(data_dir=run.args.data_dir)
    manager = _start_step_manager(paths)
    if run.args.command == "migrate-tor-network":
        manager.migrate_tor_network()
    else:
        manager.retire_old_onionheaven_containers()
    return 0


def _run_migrate_key_volume(run):
    # The launchers' migrate_key_volume(): ContainerManager's, logged in
    # the launcher's format; no OnionPressCLI setup (it runs before the
    # stack is up). Unlike the two steps above, its status matters: 1
    # stops the start, since starting tor would create an empty
    # onionpress-onion-keys that the next start took for the migrated key.
    paths = resolve_paths(data_dir=run.args.data_dir)
    return 0 if _start_step_manager(paths).migrate_key_volume() else 1


def _add_bundled_plugins(sub, name):
    what = ("Put the pinned Internet Archive Link Fixer and Cache Enabler "
            "releases in the WordPress container (downloaded over Tor, checked "
            "against their pins; each unless INSTALL_*=no)"
            if name == "install-bundled-plugins" else
            "Configure and activate Cache Enabler (network-wide, then its cache "
            "policy) and the Link Fixer (site by site), once WordPress is "
            "installed")
    p = sub.add_parser(name, help=what + "; best effort, always exits 0")
    p.add_argument("--scripts-dir", default=None,
                   help="Where install_plugin.sh and activate_plugin.sh are "
                        "(default: the app bundle's)")


def _plugin_host(paths, scripts_dir):
    from . import start
    from .bundled_plugins import PluginHost
    if not scripts_dir:
        scripts_dir = (os.path.join(os.path.dirname(paths.docker_dir), "scripts")
                       if paths.docker_dir else "")
    return PluginHost(
        docker=Docker(paths), config_file=paths.config_file, scripts_dir=scripts_dir,
        log_file=paths.log_file, log=_launcher_log_func(),
        run_step=lambda argv: start.run_cli_step(argv, data_dir=paths.data_dir,
                                                 log_file=paths.log_file))


def _run_bundled_plugins(run):
    # The launchers' install_bundled_plugins() / setup_bundled_plugins()
    # (#442.3): onionpress.bundled_plugins, logged in the launcher's format
    # (`… 2>&1 | tee -a "$LOG_FILE"`); no OnionPressCLI setup. Best
    # effort: whatever goes wrong is a logged line, never a stopped start.
    from . import bundled_plugins
    paths = resolve_paths(data_dir=run.args.data_dir)
    host = _plugin_host(paths, run.args.scripts_dir)
    try:
        if run.args.command == "install-bundled-plugins":
            bundled_plugins.install_bundled_plugins(host)
        else:
            bundled_plugins.setup_bundled_plugins(host)
    except Exception as e:
        host.log(f"WARNING: {run.args.command} failed: {e}")
    return 0


def _start_paths(args):
    """The data dir's paths, with the launcher's docker and bin folders."""
    import dataclasses
    paths = resolve_paths(data_dir=args.data_dir)
    changes = {}
    if getattr(args, "docker_dir", None):
        changes["docker_dir"] = args.docker_dir
    if getattr(args, "bin_dir", None):
        changes["bin_dir"] = args.bin_dir
    return dataclasses.replace(paths, **changes) if changes else paths


def _add_start_containers(sub, name):
    p = sub.add_parser(
        name,
        help="A launcher's start once Docker answers (onionpress.start): a "
             "pending key import, the key volume's migration, the first run's "
             "key, the core services, then Tor, the plugins and provisioning. "
             "Exit 1 when the start must stop, else 0 (WordPress installed) or "
             "3 (not yet)")
    p.add_argument("--docker-dir", default=None, help="The compose files' folder")
    p.add_argument("--bin-dir", default=None,
                   help="Where the app's mkp224o and limactl are (macOS)")
    p.add_argument("--scripts-dir", default=None,
                   help="Where install_plugin.sh and activate_plugin.sh are")
    p.add_argument("--themes-dir", default="", help="The bundled themes")
    p.add_argument("--plugins-dir", default="", help="The bundled mu-plugins")
    p.add_argument("--wp-port", type=int, default=None,
                   help="WordPress's published port (default: $ONIONPRESS_WP_PORT)")
    p.add_argument("--until-core", action="store_true",
                   help="Stop after the core services (Linux: its install-state "
                        "steps follow in the launcher, then start-post-up)")
    _add_errors_file(p)


def _run_start_containers(run):
    # The launchers' start_containers after the Docker wait (#442.8):
    # start.start_containers, logged in the launcher's format.
    from . import start
    args = run.args
    paths = _start_paths(args)
    wp_port = args.wp_port
    if wp_port is None:
        port = os.environ.get("ONIONPRESS_WP_PORT", "")
        wp_port = int(port) if port.isdigit() else 8080
    manager = _start_step_manager(paths)
    return start.start_containers(
        manager, mac=detect_os() == OS.MACOS, wp_port=wp_port,
        errors=StartErrors(args.errors_file) if args.errors_file else None,
        bin_dir=args.bin_dir or paths.bin_dir, scripts_dir=args.scripts_dir or "",
        themes_dir=args.themes_dir, plugins_dir=args.plugins_dir,
        until_core=args.until_core, log=manager.log_func)


def _add_setup(sub, name):
    p = sub.add_parser(
        name,
        help="Set up WordPress from a terminal (the launchers' `setup`): the "
             "install the setup window makes, with the answers given here or "
             "asked; --from-backup restores the backup's site instead (what "
             "`restore` does: its key, database and content; issue #509). "
             f"Passwords only in the environment (${{{_SETUP_PASSWORD_ENV}}}, "
             f"${{{_IA_PASSWORD_ENV}}}, ${{{BACKUP_PASSWORD_ENV}}} for the "
             "backup's). Exit 0 done, 2 already installed, 10 the launcher "
             "must restart the stack, 1 failed")
    p.add_argument("--user", default="", help="The admin username (an onionname)")
    p.add_argument("--title", default="", help="The site title")
    p.add_argument("--email", default="",
                   help="Accepted for compatibility and ignored (with a warning): "
                        "the admin's email is the setup window's")
    p.add_argument("--ia-account", default="",
                   help="An archive.org account to keep S3 keys from (Linux's setup)")
    p.add_argument("--from-backup", default="", metavar="BACKUP_ZIP",
                   help="Restore this backup's site (its password in the environment; "
                        "a hub's recovery key as `restore` takes it)")
    p.add_argument("--launcher", default=None,
                   help="The launcher that runs provision-post-install and "
                        "setup-bundled-plugins (default: this install's)")


def _run_setup(run):
    # The launchers' `setup)` case (#442.11): setup_logic.setup_from_terminal.
    # No OnionPressCLI setup: the launcher made config and secrets already.
    from . import setup_logic
    from .handle_action import default_launcher
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    mac = detect_os() == OS.MACOS
    log = _make_log_func(paths.log_file)
    if args.email:
        # The old bash setup made it the admin's email; install_fresh_wordpress
        # sets that the setup window's way. Say so rather than drop it unseen.
        print("WARNING: --email is ignored: the admin's email is set the way the "
              "setup window sets it", file=sys.stderr)
        log("WARNING: setup: --email given and ignored")

    def restore(password, zip_path):
        # A backup's setup is the restore (issue #509): cmd_restore's
        # install-from-backup rebuild, OnionPressCLI and all, with the hub
        # recovery key main() took out of the environment. Only here is
        # OnionPressCLI constructed for a setup.
        return _cli(run).cmd_restore(password, zip_path, run.hub_recovery_key)

    return setup_logic.setup_from_terminal(
        data_dir=paths.data_dir, mac=mac,
        launcher_bin=args.launcher or default_launcher(paths, "macos" if mac else "linux"),
        docker=Docker(paths), manager=_start_step_manager(paths), user=args.user,
        title=args.title, from_backup=args.from_backup, ia_account=args.ia_account,
        log=log, restore=restore)


def _add_start_post_up(sub, name):
    p = sub.add_parser(
        name,
        help="The end of a launcher's start, once the core services are up: "
             "Tor, the pinned plugins, provisioning, the Mac's .htaccess, "
             "backup import and site timezones, the image rebuild; best "
             "effort, always exits 0 (the Cloudflare tunnel follows in the "
             "launcher)")
    p.add_argument("--wordpress-installed", choices=("yes", "no"), required=True,
                   help="What start-core found")
    p.add_argument("--docker-dir", default=None, help="The compose files' folder")
    p.add_argument("--scripts-dir", default=None,
                   help="Where install_plugin.sh and activate_plugin.sh are")
    p.add_argument("--themes-dir", required=True, help="The bundled themes")
    p.add_argument("--plugins-dir", required=True, help="The bundled mu-plugins")
    _add_errors_file(p)


def _run_start_post_up(run):
    # The launchers' post-up block of start_containers (#442.7):
    # start.post_up, in the launcher's words; best effort, so 0.
    from . import start
    args = run.args
    paths = _start_paths(args)
    manager = _start_step_manager(paths)
    errors = StartErrors(args.errors_file) if args.errors_file else None
    try:
        start.post_up(manager, mac=detect_os() == OS.MACOS,
                      installed=args.wordpress_installed == "yes", errors=errors,
                      themes_dir=args.themes_dir, plugins_dir=args.plugins_dir,
                      scripts_dir=args.scripts_dir or "", log=manager.log_func)
    except Exception as e:
        manager.log_func(f"WARNING: the start's last steps failed: {e}")
        if errors is not None:
            errors.add("start-post-up")
    return 0


def _add_is_onionheaven_hub(sub, name):
    p = sub.add_parser(
        name,
        help="Exit 0 when the onion address given is the OnionHeaven hub's "
             "(onionheaven.is_onionheaven_instance, the Mac daemon's rule), "
             "1 otherwise; the Linux launcher's write_status asks it, so "
             "status.json's onionheaven.server_active is decided by the "
             "node's address, never by the onionheaven container, which "
             "every node runs (issue #345)")
    p.add_argument("address", nargs="?", default="",
                   help="The node's served onion address")


def _run_is_onionheaven_hub(run):
    # The Linux launcher's is_onionheaven_hub() (issue #345): the Mac
    # daemon's rule, by address. No OnionPressCLI setup (every status
    # tick, beside a live stack).
    from .onionheaven import is_onionheaven_instance
    return 0 if is_onionheaven_instance(run.args.address) else 1


def _run_using_local_images(run):
    # The launchers' using_local_images(): the one copy of the rule
    # (containers.using_local_images; the start calls it in-process). Before the stack is up, so no OnionPressCLI
    # setup (config/secrets creation, port probing).
    from .containers import using_local_images
    paths = resolve_paths(data_dir=run.args.data_dir)
    return 0 if using_local_images(paths.config_file) else 1


def _add_wordpress_egress(sub, name):
    p = sub.add_parser(
        name,
        help="Shut WordPress's network (onionpress-wp0) off from everything "
             "but replies to connections made from outside, the Tor side's "
             "(onionpress-tor0) off from the host and the LAN, and "
             "WordPress's published port off from every container, wherever "
             "this platform's Docker keeps its bridges (issue #144); prints "
             "one status line")
    p.add_argument(
        "--check", action="store_true",
        help="Only say whether the rules are in place")
    p.add_argument(
        "--wp-port", type=int, default=None,
        help="WordPress's published port (default: $ONIONPRESS_WP_PORT, "
             "which both launchers export; without either the published-"
             "port rule is left as it is)")
    p.add_argument(
        "--bin-dir", default=None,
        help="Where the app's limactl is (macOS; default: the app bundle's)")
    _add_errors_file(p)


def _add_errors_file(p):
    p.add_argument(
        "--errors-file", default=None,
        help="Append the name of each step that failed here, one per line, "
             "and exit 0 for them: the Mac launcher's START SUMMARY "
             "(onionpress.start); a non-zero exit then only means the "
             "start must stop")


def _run_wordpress_egress(run):
    # The launchers' block_wordpress_egress() (Phase 4, #120): run
    # before they start the containers, so no OnionPressCLI setup
    # (config/secrets creation, port probing). Their bash body is
    # `… 2>&1 | tee -a "$LOG_FILE"` and returns 0 whatever happens
    # (the Mac puts a non-zero status into its START SUMMARY), so the
    # verdict is logged here in their `[ts] msg` format; --check (not
    # a launcher's call) prints the bare status line. Never a
    # traceback: whatever goes wrong is the WARNING's words.
    import dataclasses
    from . import wordpress_egress
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    if args.bin_dir:
        paths = dataclasses.replace(paths, bin_dir=args.bin_dir)
    wp_port = args.wp_port
    if wp_port is None and os.environ.get("ONIONPRESS_WP_PORT", "").isdigit():
        wp_port = int(os.environ["ONIONPRESS_WP_PORT"])
    try:
        result = wordpress_egress.ensure(
            paths, Docker(paths), action="check" if args.check else "apply",
            wp_port=wp_port)
    except Exception as e:  # a start must never stop here
        result = wordpress_egress.Result(False, "", f"error: {e}")
    if args.check:
        print(result.line())
    else:
        _log_wordpress_egress_verdict(result, _launcher_log_func(), paths.log_file)
    if args.errors_file and not args.check:
        if not result.ok:
            StartErrors(args.errors_file).add("wordpress-egress")
        return 0
    return 0 if result.ok else 1


def _run_apply_settings_updates(run):
    # The payload was written inside WordPress, so it is untrusted
    # (config.SETTINGS_PAGE_KEYS). Same fast path as safe-config: only
    # the config file is touched.
    from .config import apply_settings_updates
    try:
        updates = json.loads(sys.stdin.read())
    except ValueError:
        print("ERROR: config-updates.json is not valid JSON", file=sys.stderr)
        return 1
    applied = apply_settings_updates(os.path.join(run.data_dir, "config"), updates)
    changed = {key for key, _old, _new in applied.changes}
    for key in applied.accepted:
        print(f"{'changed' if key in changed else 'unchanged'} {key}")
    for key in applied.rejected:
        print(f"rejected {key}")
    for key in applied.held:
        print(f"held {key}")
    return 0




# ---- settings ----

def _add_config(sub, name):
    p = sub.add_parser(
        name,
        help="Read and change settings in ~/.onionpress/config "
             "(get KEY | set KEY VALUE | unset KEY | list [--json])",
        description="Settings: every key `config list` shows, with the check "
                    "its value gets on the Settings page. Image overrides, "
                    "ports, paths (ONIONPRESS_*) and ONIONHEAVEN_ACCEPT_"
                    "REGISTRATIONS are hand-edit only. A secret "
                    "(CLOUDFLARE_TUNNEL_TOKEN) is set with VALUE `-` and the "
                    "value on stdin, never on the command line.")
    config_sub = p.add_subparsers(dest="config_command")
    p_get = config_sub.add_parser("get", help="Print one setting's value")
    p_get.add_argument("key", metavar="KEY")
    p_set = config_sub.add_parser(
        "set", help="Change one setting (VALUE `-` reads the value from stdin)")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    # ADDRESS_PREFIX the live address does not start with asks
    # "prefix-change" (onionpress.confirm).
    _confirm.add_arguments(p_set)
    p_unset = config_sub.add_parser(
        "unset", help="Remove one setting's line, so its default applies")
    p_unset.add_argument("key", metavar="KEY")
    p_unset.add_argument("--json", action="store_true",
                         help="Answer with one JSON object on the last line")
    p_list = config_sub.add_parser(
        "list", help="Print every setting (a secret only as <set>/<empty>)",
        epilog="\n".join(f"  {k}: {v}" for k, v in sorted(_config_key_help().items())),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_list.add_argument("--json", action="store_true",
                        help="As JSON: {\"settings\": {...}, \"hand_edit_only\": [...]}")


def _run_config(run):
    # Only the config file, its change marker and the login item /
    # user units: no OnionPressCLI setup (no Docker, no port probing).
    from . import config as _config
    args = run.args
    if not args.config_command:
        return run.help("config")
    paths = resolve_paths(data_dir=args.data_dir)
    return _config.run_config_command(
        args, run.data_dir, _file_log_func(paths.log_file),
        os_name=detect_os().value, app_bundle=paths.app_bundle)


def _add_onionheaven(sub, name):
    p = sub.add_parser(
        name,
        help="Keep the site reachable through OnionHeaven: enable | disable | "
             "status [--json] | overview [--json]")
    p.add_argument("onionheaven_command", nargs="?", default="status",
                   metavar="{enable,disable,status,overview}")
    p.add_argument("--json", action="store_true",
                   help="status as JSON (enabled, choice, hub, registered, may_hold_key); "
                        "overview as JSON (client, hub, known_hosts)")


def _run_onionheaven(run):
    # As config: the config file and the registration record, no network.
    from . import onionheaven
    return onionheaven.run_onionheaven_command(
        run.args.onionheaven_command, _hub_context(run.args.data_dir),
        json_output=run.args.json)


# ---- the WordPress Settings page's requests and the Mac daemon ----

# The WordPress Settings page's requests (issue #366): one handler for
# both platforms, run by onionpress-watcher.timer on Linux and by the
# menubar's status loop on a Mac.
def _add_handle_action(sub, name):
    p = sub.add_parser(
        name,
        help="Handle the WordPress Settings page's request (requested-action "
             "in the shared volume), if there is one")
    p.add_argument(
        "--launcher", default="",
        help="The launcher script the actions run through (default: this "
             "install's)")
    p.add_argument(
        "--cancel", action="store_true",
        help="The question a previous run asked was answered Cancel")
    _confirm.add_arguments(p, check=False)


def _run_handle_action(run):
    # Run every 10 s (Linux) or every status tick (Mac): no
    # OnionPressCLI setup (config/secrets creation, port probing).
    from . import handle_action as _ha
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    log = _make_log_func(paths.log_file)
    os_name = detect_os().value
    host = _ha.Host(
        launcher=(getattr(args, "launcher", "") or _ha.default_launcher(paths, os_name)),
        paths=paths, docker=Docker(paths, log_func=log), log=log, os_name=os_name,
        version=__version__)
    if args.command == "apply-config":
        return 1 if _ha.apply_config(host, out=sys.stdout) is None else 0
    result = _ha.handle(host, confirmed=_confirm.given(args), cancel=args.cancel)
    if args.json:
        print(_confirm.dumps(result))
    return 0


def _run_upload_analytics(run):
    # Share Now from the command line (#351): both launchers'
    # `upload-analytics` is this. It reads the config and leaves a
    # marker, so no OnionPressCLI setup (config/secrets creation, port
    # probing).
    from . import analytics_sharing
    paths = resolve_paths(data_dir=run.args.data_dir)
    return analytics_sharing.request_upload(paths, detect_os().value, out=sys.stdout)


def _add_daemon(sub, name):
    p = sub.add_parser(
        name,
        help="The Mac's long-running loops (status, probe, heartbeat, the "
             "Settings page's requests, log captures, the extension proxy), "
             "as a launchd agent: daemon start | stop | status | offline, or "
             "serve (the foreground process launchd runs)")
    p.add_argument("daemon_command", nargs="?", default="serve",
                   choices=("serve", "start", "stop", "status", "offline"))
    p.add_argument("--json", action="store_true", help="Answer in JSON")
    p.add_argument("--no-offline", action="store_true",
                   help="stop: without telling the hub /offline (an update: "
                        "the daemon comes right back)")
    p.add_argument("--launcher", default="",
                   help="The launcher script the agent runs (default: this install's)")


def _run_daemon(run):
    # The loops (onionpress.daemon, issue #367): no OnionPressCLI setup,
    # the daemon resolves its own ports and docker environment.
    from . import daemon as _daemon
    from . import handle_action as _ha
    args = run.args
    paths = resolve_paths(data_dir=args.data_dir)
    os_name = detect_os().value
    launcher = args.launcher or _ha.default_launcher(paths, os_name)
    return _daemon.run_daemon_command(
        args.daemon_command, paths, _file_log_func(paths.log_file),
        launcher_script=launcher, json_output=args.json, version=__version__,
        no_offline=args.no_offline)


# The page's own backup and restore, run by handle-action through the
# launcher (on Linux the launcher's bash does these itself), and the
# welcome screen's staging of a backup to install from.
def _add_settings_page_backup(sub, name):
    p = sub.add_parser(
        name,
        help="The Settings page's backup: for WordPress, so without the hub "
             "data, the host's secrets or (on a Mac) the onion service key; "
             "the password from $ONIONPRESS_BACKUP_PASSWORD or stdin")
    p.add_argument("output", help="Where to write the zip")
    p.add_argument("--json", action="store_true",
                   help="Answer with one JSON object on the last line")


def _add_prepare_restore(sub, name):
    p = sub.add_parser(
        name,
        help="Stage a backup for a first start that restores it (the welcome "
             "screen's Restore from backup): extract, seed the key, apply the "
             "config overrides and write the .install-from-backup marker; the "
             "password from $ONIONPRESS_BACKUP_PASSWORD or stdin")
    p.add_argument("backup_file", help="The backup zip")
    p.add_argument("--check", action="store_true",
                   help="Only open the backup and answer its onion address and "
                        "whether its key is left out; change nothing")
    p.add_argument("--json", action="store_true",
                   help="Answer with one JSON object on the last line")


def _add_settings_page_restore(sub, name):
    p = sub.add_parser(
        name,
        help="The Settings page's restore: in place, never on a hub or "
             "OnionHome, never a hub's keys; a backup of another address "
             "needs --confirmed address-change; the password from "
             "$ONIONPRESS_BACKUP_PASSWORD or stdin")
    p.add_argument("backup_file", help="The uploaded backup zip")
    _confirm.add_arguments(p, check=False)


def _run_settings_page_command(run):
    args, data_dir = run.args, run.data_dir
    password = read_backup_password()
    if args.command == "prepare-restore" and args.check:
        if not password:
            return _confirm.answer_ask("password", args.json)
        return cmd_prepare_restore_check(args.backup_file, password, args.json)
    if not password:
        return _confirm.answer_error("a password is required", args.json)
    if args.command == "prepare-restore":
        return cmd_prepare_restore(args.backup_file, password, data_dir, args.json)
    if args.command == "settings-page-backup":
        return cmd_settings_page_backup(args.output, password, data_dir, args.json)
    return cmd_settings_page_restore(
        args.backup_file, password, data_dir, args.json, _confirm.given(args))


# A helper run mid-start (put-wp-tree: the bundled plugins' installs) is
# OnionPressCLI's static method: no OnionPressCLI setup (config/secrets
# creation, port probing). The backup and restore helpers the Linux
# launcher's bash spine called went with issue #406: the backup and the
# restores are cmd_backup, cmd_restore and the settings-page commands.
# Individual post-start provisioning steps are the helpers start_containers
# used to inline as bash function calls; the bash launchers now delegate
# each one through here.
_COMMANDS = [
    # (name, adds its parser: build(sub, name), its handler: run(_Run) -> exit code)
    ("stop", _plain("Stop OnionPress"), lambda run: _cli(run).cmd_stop()),
    ("status", _plain("Show container status (JSON)"), lambda run: _cli(run).cmd_status()),
    ("version", _add_version, _run_version),
    ("address", _add_address, _run_address),
    ("logs", _add_logs, _run_logs),
    ("check-reachability", _add_check_reachability, _run_check_reachability),
    ("check-docker", _add_check_docker, _run_check_docker),
    ("open", _add_open, _run_open),
    ("onionname", _add_onionname, _run_onionname),
    ("get", _add_get, _run_get),
    ("backup", _add_backup, _run_backup),
    ("restore", _add_restore, _run_restore),
    ("reset", _add_reset, _run_reset),
    ("update", _add_update, _run_update),
    ("uninstall", _add_uninstall, _run_uninstall),
    ("onionheaven-unregister", _add_onionheaven_unregister, _run_onionheaven_unregister),
    ("onionheaven-registrations",
     _plain("List what the registration record says OnionHeaven hubs may "
            "hold of this install's keys (no network)"),
     _run_onionheaven_registrations),
    ("check-for-update", _add_check_for_update,
     lambda run: _cli(run).cmd_check_for_update(json_output=run.args.json,
                                                current=run.args.current)),
    ("smoke-test-wayback",
     _plain("Publish a throwaway post and verify it got archived to the Wayback Machine"),
     lambda run: _cli(run).cmd_smoke_test_wayback()),
    ("generate-vanity", _plain("Generate a vanity .onion via mkp224o in the tor container"),
     lambda run: _cli(run).cmd_generate_vanity()),
    ("admin-password", _plain("Print the recovery admin password (if any)"),
     lambda run: _cli(run).cmd_admin_password()),
    ("provision-post-install", _add_provision_post_install, _run_provision_post_install),
    ("verify-admin-password",
     _plain("Validate a WP admin password (reads from stdin). Exit 0 = match."),
     _run_verify_admin_password),
    ("configure-ia-plugin",
     _plain("Configure the IA Wayback Machine Link Fixer in every site "
            "(before it is activated)"),
     _multisite_step("configure_ia_plugin")),
    ("configure-cache-enabler",
     _plain("Prepare Cache Enabler's activation (policy mu-plugin, FS_METHOD)"),
     _multisite_step("configure_cache_enabler")),
    ("apply-cache-policy",
     _plain("Check Cache Enabler's settings against OnionPress's cache "
            "policy; clear its stored pages (after activation)"),
     _multisite_step("apply_cache_policy")),
    ("deactivate-wp-statistics", _plain("Remove WP-Statistics if present (clearnet leak)"),
     _multisite_step("deactivate_wp_statistics")),
    ("ensure-archive-s3-keys",
     _plain("Fetch shared archive.org S3 keys for Wayback archiving"),
     _multisite_step("ensure_archive_s3_keys")),
    ("scrub", _add_scrub, _run_scrub),
    ("import-backup-artifacts", _add_import_backup_artifacts,
     lambda run: _cli(run).cmd_import_backup_artifacts(run.args.staging)),
    ("regenerate-derivatives",
     _plain("Rebuild the resized image copies a restored backup left out "
            "(no-op unless a restore left the pending marker; resumable)"),
     lambda run: _cli(run).cmd_regenerate_derivatives()),
    ("regenerate-derivatives-pending",
     _plain("Exit 0 when a restore left resized images to rebuild (the host "
            "marker in the data dir, or the one a backup carries in the "
            "container's uploads/), 1 when not; both launchers' start gates "
            "the detached regenerate-derivatives on it"),
     _run_regenerate_derivatives_pending),
    ("hub-recovery-key", _add_hub_recovery_key, _run_hub_recovery_key),
    ("put-wp-tree", _add_put_wp_tree,
     lambda run: OnionPressCLI.cmd_put_wp_tree(run.args.src, run.args.dest,
                                               run.args.merge)),
    ("open-login", _add_open_login, _run_open_login),
    ("safe-config",
     _plain("Print the allowlisted (non-secret) config keys as JSON — "
            "what the WordPress container may see"),
     _run_safe_config),
    ("wordpress-logs", _add_wordpress_logs, _run_wordpress_logs),
    ("wordpress-status",
     _plain("Read status JSON on stdin and print the fields the WordPress "
            "container may see"),
     _run_wordpress_status),
    ("write-status",
     _plain("Write the status documents from one pass over the stack: "
            "daemon-state.json and ~/.onionpress/status.json, and, while "
            "the stack runs, status.json, version, config-current.json and "
            "recent-logs.txt in the shared volume (the Linux launcher's "
            "write_status; the Mac daemon writes them itself); always "
            "exits 0"),
     _run_write_status),
    ("retire-old-onionheaven-containers",
     _plain("Before the first `compose up`: remove every onionpress-tor, "
            "onionheaven and onionheaven-(poll|takeover)-N that does not "
            "mount onionpress-onionheaven-data (started by an older "
            "OnionPress; the hub data move must not race them); always "
            "exits 0"),
     _run_start_step),
    ("migrate-tor-network",
     _plain("Before the first `compose up`: recreate an onionpress-network "
            "whose bridge is not onionpress-tor0 (made by an older "
            "OnionPress; the Tor side's rules name the bridge), removing "
            "OnionPress's own containers on it and disconnecting anything "
            "else; always exits 0"),
     _run_start_step),
    ("setup", _add_setup, _run_setup),
    ("start-containers", _add_start_containers, _run_start_containers),
    ("start-post-up", _add_start_post_up, _run_start_post_up),
    ("migrate-key-volume",
     _plain("Before anything starts Tor: copy the onion service key out of "
            "onionpress-arti-state (its name until 2026-09-25) into "
            "onionpress-onion-keys once, verified, in the pinned tor image "
            "with no network; exit 1 (the start must stop) when Docker does "
            "not answer, the copy fails or the image is not here"),
     _run_migrate_key_volume),
    ("install-bundled-plugins", _add_bundled_plugins, _run_bundled_plugins),
    ("setup-bundled-plugins", _add_bundled_plugins, _run_bundled_plugins),
    ("is-onionheaven-hub", _add_is_onionheaven_hub, _run_is_onionheaven_hub),
    ("using-local-images",
     _plain("Exit 0 when the stack points at images built on this machine "
            "(ONIONPRESS_TOR_IMAGE / ONIONPRESS_WORDPRESS_IMAGE, environment "
            "or ~/.onionpress/config, not on ghcr.io), 1 when at published "
            "ones; both launchers gate every image pull on it"),
     _run_using_local_images),
    ("wordpress-egress", _add_wordpress_egress, _run_wordpress_egress),
    ("apply-settings-updates",
     _plain("Merge the settings page's config-updates.json (on stdin) into "
            "the config — allowlisted keys with valid values only; prints "
            "'changed|unchanged|rejected KEY' lines, never values"),
     _run_apply_settings_updates),
    ("config", _add_config, _run_config),
    ("onionheaven", _add_onionheaven, _run_onionheaven),
    ("handle-action", _add_handle_action, _run_handle_action),
    ("apply-config",
     _plain("Apply the Settings page's config-updates.json (the allowlisted "
            "keys), then remove it"),
     _run_handle_action),
    ("upload-analytics",
     _plain("Upload the diagnostic logs to OnionHome now (Share Now; needs "
            f"{SHARING_CONFIG_KEY}=yes and a running OnionPress): the daemon "
            "takes the request within a cycle"),
     _run_upload_analytics),
    ("daemon", _add_daemon, _run_daemon),
    ("settings-page-backup", _add_settings_page_backup, _run_settings_page_command),
    ("prepare-restore", _add_prepare_restore, _run_settings_page_command),
    ("settings-page-restore", _add_settings_page_restore, _run_settings_page_command),
]


def build_parser():
    """onionpress's argument parser, from _COMMANDS: (parser, subparsers)."""
    parser = argparse.ArgumentParser(
        prog="onionpress",
        description="OnionPress — WordPress over Tor",
    )
    parser.add_argument("--version", action="version", version=f"OnionPress {__version__}")
    parser.add_argument("--data-dir", help="Override data directory (default: ~/.onionpress/)")
    sub = parser.add_subparsers(dest="command", help="Command to run")
    for name, build, _handler in _COMMANDS:
        build(sub, name)
    return parser, sub


def main(argv: list[str] = None) -> int:
    """CLI entry point."""
    parser, sub = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # Starting is the launcher's (`onionpress start`); this CLI has no
        # third copy of it (issue #405).
        parser.print_help()
        return 2

    # A hub's backup recovery key leaves the environment at once, whatever
    # the command: OnionPressCLI's Docker wrapper hands os.environ to every
    # docker child. Used by restore and hub-recovery-key only.
    hub_recovery_key = os.environ.pop(RECOVERY_KEY_ENV, "")
    handlers = {name: handler for name, _build, handler in _COMMANDS}
    return handlers[args.command](_Run(args, parser, sub, hub_recovery_key))


if __name__ == "__main__":
    sys.exit(main())
