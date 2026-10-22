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
from .docker import Docker
from .containers import ContainerManager
from .launcher_ops import BACKUP_PASSWORD_ENV
from .hub_backup import RECOVERY_KEY_ENV


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


def read_backup_password(positional: str | None = None) -> str:
    """The password for `backup` / `restore`, or "" if none was given.

    Taken from the first of: the old positional argument (with a warning:
    argv is readable by every local user), $ONIONPRESS_BACKUP_PASSWORD (how
    the launchers, menubar and tray pass it), a line on stdin when stdin is
    not a terminal, or a no-echo prompt. The variable is removed from this
    process's environment so the docker/zip/unzip children don't inherit it.
    """
    from_env = os.environ.pop(BACKUP_PASSWORD_ENV, "")
    if positional:
        print("WARNING: a password on the command line is visible to other "
              "users of this computer. Leave it out to be prompted, or set "
              f"{BACKUP_PASSWORD_ENV}.", file=sys.stderr)
        return positional
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


def _file_log_func(log_file: str | None) -> Callable[[str], None]:
    """A log function that writes to the log file only (the terminal gets
    a summary instead)."""
    def log(msg: str) -> None:
        if not log_file:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_file, "a") as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass
    return log


def _hub_context(data_dir: str | None = None,
                 log_func: Callable[[str], None] | None = None):
    """onionheaven.HubContext for this install, with the configured hub
    and a docker environment key_manager.extract_keys (plain `docker`,
    this process's environment) can use: the launchers export
    DOCKER_HOST, a direct run gets the socket resolve_paths finds."""
    from . import onionheaven
    paths = resolve_paths(data_dir=data_dir)
    log = log_func or _file_log_func(paths.log_file)
    ctx = onionheaven.HubContext(paths, log)
    onionheaven.ONIONHEAVEN_ADDRESS = onionheaven.configured_hub_address(
        ctx.read_config_value("ONIONHEAVEN_ADDRESS", ""), log)
    os.environ.setdefault("DOCKER_HOST", f"unix://{paths.docker_socket}")
    if paths.bin_dir and os.path.isdir(paths.bin_dir):
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if paths.bin_dir not in path_dirs:
            os.environ["PATH"] = os.pathsep.join([paths.bin_dir] + path_dirs)
    return ctx


# Remaining items the user can do something about: start OnionPress (the
# key could not be read) or try again later (the hub did not answer).
_RETRYABLE = ("key-unreadable", "unanswered", "busy")


def release_onionheaven(reason: str, keep_address: str | None = None,
                        data_dir: str | None = None,
                        log_func: Callable[[str], None] | None = None,
                        out=None) -> list:
    """Before this install forgets an onion service key (reset, key
    import, restore, uninstall): ask every OnionHeaven hub that may hold
    one of its keys to delete its copy (onionheaven.release_registration:
    a signed /unregister, the hub the record names), and tell the user on
    *out* (stdout) what happened. Nothing is printed when no hub holds
    anything. Returns what may remain on a hub ([] = nothing).
    """
    from . import onionheaven
    out = out or sys.stdout
    ctx = _hub_context(data_dir, log_func)
    pairs = [p for p in onionheaven.registrations_left(ctx) if p[0] != keep_address]
    if not pairs:
        return []
    print("Asking the OnionHeaven hub to delete its copy of this site's "
          "onion service key (this can take a few minutes over Tor)...", file=out)
    remaining = onionheaven.release_registration(ctx, reason, keep_address=keep_address)
    left = {(i["content_address"], i["onionheaven_address"]) for i in remaining}
    for addr, hub in pairs:
        if (addr, hub) not in left:
            print(f"  Done: the hub {hub} no longer holds the key of {addr}.", file=out)
    if remaining:
        print("", file=out)
        print("WARNING: " + onionheaven.describe_remaining(remaining).replace(
            "\n", "\nWARNING: "), file=out)
        if any(i["why"] in _RETRYABLE for i in remaining):
            print("To have it deleted, make sure OnionPress is running and "
                  "try again before the key is gone.", file=out)
        print("OnionPress keeps a note of it in "
              f"{os.path.join(ctx.app_support, onionheaven.REGISTRATION_RECORD)}.",
              file=out)
    return remaining


class _LogRecorder:
    """A log function that remembers what it was given."""

    def __init__(self, log):
        self._log = log
        self.seen = []

    def __call__(self, msg):
        self.seen.append(msg)
        self._log(msg)


def check_for_update_report(current: str, log, json_output: bool = False) -> int:
    """Check GitHub for a newer release. Always exits 0.

    On any failure (network down, GitHub rate-limited, parse error) the
    JSON output carries an ``error`` field and ``update_available`` is
    false — callers render that to the user instead of crashing. The
    report also names the other macOS users running OnionPress
    (``other_instances``, ``active_offsets``, this install's
    ``port_offset``): the shared bundle is replaced for everyone.
    """
    from datetime import datetime, timezone
    from . import updater

    # ``updater.check_for_update`` conflates "up to date" and "error":
    # both return ``None``. Capture the log to recover the distinction
    # without touching updater.py.
    error_msg = None

    def capture_log(msg: str) -> None:
        nonlocal error_msg
        if "failed" in msg.lower():
            error_msg = msg
        log(msg)

    result = updater.check_for_update(current, log=capture_log)

    if result is not None:
        release_data, latest = result
        update_available = True
        release_url = f"https://github.com/brewsterkahle/onionpress/releases/tag/v{latest}"
        release_notes = (release_data.get("body") or "").strip()
        published_at = release_data.get("published_at")
    else:
        update_available = False
        release_url = None
        release_notes = None
        published_at = None
        latest = None if error_msg else current

    others = updater.detect_other_instances()
    try:
        port_offset = int(os.environ.get("ONIONPRESS_PORT_OFFSET", "0") or 0)
    except ValueError:
        port_offset = 0
    report = {
        "current": current,
        "latest": latest,
        "update_available": update_available,
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
    else:
        print("Already up to date.")
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


def cmd_update(paths, log, *, check: bool = False, yes: bool = False,
               json_output: bool = False, app_path: str = None) -> int:
    """`onionpress update` on a Mac: a new release from GitHub, installed
    by the staged bundle swap (updater.download_and_install: the new
    bundle is copied beside the old one and renamed into place last, since
    this very process runs out of the bundle being replaced). The menubar
    relaunches after. --check only reports (check_for_update_report, with
    the other users running OnionPress); without --yes a terminal is asked
    and a non-terminal refused. The answer: {"ok": true, "version"} or
    {"error", "permission_denied"} (another account owns the bundle)."""
    import plistlib
    from . import confirm as _confirm, updater
    app_path = app_path or os.environ.get("ONIONPRESS_APP_PATH") or paths.app_bundle
    current = __version__
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            current = plistlib.load(f).get("CFBundleShortVersionString", current)
    except (OSError, ValueError):
        pass
    if check:
        return check_for_update_report(current, log, json_output)

    if not json_output:
        print(f"Installed: {current}")
        print("Checking GitHub for updates...")
    result = updater.check_for_update(current, log=log)
    if not result:
        return _confirm.answer_ok(json_output, text="Already up to date.",
                                  update_available=False, current=current)
    release_data, latest = result
    if not json_output:
        print(f"Update available: {latest}  (published {release_data.get('published_at', '?')})")
        notes = (release_data.get("body") or "").strip()
        if notes:
            print()
            print(notes if len(notes) <= 800 else notes[:800] + "\n... (truncated)")
            print()
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

    log(f"Update: downloading and installing v{latest}...")
    if not json_output:
        print(f"Downloading and installing v{latest}...")
    try:
        updater.download_and_install(release_data, latest, app_path, log=log)
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
        os.makedirs(self.paths.data_dir, exist_ok=True)
        self.log = _make_log_func(self.paths.log_file)

        # Ensure config exists
        ensure_config(self.paths)

        # Detect ports
        self.port_config = detect_port_offset()

        # Build env for Docker
        secrets = ensure_secrets(self.paths.secrets_file)
        extra_env = secrets.as_env()
        extra_env["ONIONPRESS_WP_PORT"] = str(self.port_config.wp_port)
        extra_env["ONIONPRESS_SOCKS_PORT"] = str(self.port_config.socks_port)
        extra_env["ONIONPRESS_PROXY_PORT"] = str(self.port_config.proxy_port)
        extra_env["ONIONPRESS_PORT_OFFSET"] = str(self.port_config.offset)
        extra_env["TZ"] = detect_timezone()
        extra_env["ONIONPRESS_VERSION"] = __version__

        self.docker = Docker(self.paths, log_func=self.log, extra_env=extra_env)
        self.containers = ContainerManager(
            self.docker, self.paths, self.port_config, log_func=self.log,
        )

    def cmd_start(self) -> int:
        """Start OnionPress containers."""
        # Check PID lock
        if self._check_pid_lock():
            print("OnionPress is already running.", file=sys.stderr)
            return 1
        self._write_pid_lock()

        try:
            # Detect container runtime (may start Colima on macOS)
            if detect_os() == OS.MACOS:
                from .colima import detect_container_runtime
                runtime = detect_container_runtime(self.paths, log_func=self.log)
                self.log(f"Container runtime: {runtime}")

            # This start has no key volume migration (the launcher's
            # migrate_key_volume): refuse while one is due, before anything
            # starts.
            why = self.containers.key_volume_not_ready()
            if why:
                self.log(f"ERROR: not starting: {why}")
                return 1

            # Pull images if configured
            if read_value(self.paths.config_file, "UPDATE_ON_LAUNCH", "yes") == "yes":
                self.containers.pull_images()

            # Start core services
            if not self.containers.start_core():
                return 1

            # Wait for WordPress
            if not self.containers.wait_for_wordpress():
                self.log("WARNING: WordPress not ready, continuing anyway")

            # Start Tor if WordPress is installed
            if self.containers.wp_is_installed():
                # A restore may have left image sizes to rebuild (#257);
                # runs detached, resumable, no-op without the marker.
                from . import derivatives
                derivatives.spawn_regeneration(
                    self.paths.data_dir, self.log, self.paths.log_file)
                self.containers.start_tor()

                # Wait for Tor
                if self.containers.wait_for_tor():
                    addr = self.containers.get_onion_address()
                    if addr:
                        self.log(f"Onion address: {addr}")
                        print(f"  Onion address: {addr}")
            else:
                self.log("WordPress not installed — skipping Tor startup")
                print("  WordPress not installed. Run: onionpress setup")

            wp_url = f"http://localhost:{self.port_config.wp_port}"
            self.log(f"OnionPress is running! Local: {wp_url}")
            print(f"  Local access: {wp_url}")
            return 0

        except Exception as e:
            self.log(f"ERROR: {e}")
            return 1

    def cmd_stop(self) -> int:
        """Stop OnionPress containers."""
        self.containers.stop()
        self._remove_pid_lock()
        self.log("OnionPress stopped")
        return 0

    def cmd_restart(self) -> int:
        """Restart OnionPress containers."""
        self.containers.stop()
        return self.cmd_start()

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

    def cmd_address(self) -> int:
        """Print the onion address."""
        addr = self.containers.get_onion_address()
        if addr:
            print(addr)
            return 0
        print("No onion address available", file=sys.stderr)
        return 1

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
            username = get_admin_username(data_dir=self.paths.data_dir)
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
        try:
            # Teardown: stop + wipe data + keystore volumes so the rebuild adopts
            # the seeded key and imported backup cleanly (no stale keystore
            # overriding the restored identity, no in-place overwrite).
            self.containers.stop()
            # onionpress-arti-state is the key volume's pre-2026-09-25 name;
            # wiping it too keeps the launcher's migration from bringing the
            # replaced identity back. The key volumes stay when the backup
            # has no key: they hold the identity this install keeps.
            key_volumes = () if keeps_key else (
                "onionpress-onion-keys", "onionpress-arti-state",
                "onionpress-tor-state")
            for vol in key_volumes + ("onionpress-db-data", "onionpress-wordpress-data",
                                      "onionpress-persistent-data"):
                self.docker.run(["volume", "rm", vol], timeout=20)
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
                seed = self.docker.run([
                    "run", "--rm",
                    # The key needs no network, and gets none.
                    "--network", "none",
                    # Root reads the key out of the user's folder, whose
                    # permissions Linux enforces: DAC_OVERRIDE, nothing else.
                    "--cap-drop", "ALL",
                    "--cap-add", "DAC_OVERRIDE",
                    "--security-opt", "no-new-privileges:true",
                    "-v", "onionpress-onion-keys:/dest",
                    "--mount", f"type=bind,source={vanity_addr_dir},target=/src,readonly",
                    "--entrypoint", "sh",
                    image, "-c",
                    "mkdir -p /dest/wordpress && "
                    "cp /src/ks_hs_id.ed25519_expanded_private /dest/wordpress/ && "
                    "chown -R 0:0 /dest && "
                    "chmod 700 /dest /dest/wordpress && "
                    "chmod 600 /dest/wordpress/*",
                ], timeout=30)
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
        ok = derivatives.regenerate_if_pending(
            self.paths.data_dir, self.log, docker_bin=self.docker.docker_bin)
        return 0 if ok else 1

    def cmd_prune_image_derivatives(self, uploads_dir: str) -> int:
        """Delete regenerable -WxH/-scaled image copies from a backup's
        staged uploads dir and print `<files> <bytes>`. Used by the Linux
        launcher's bash create_backup so both platforms share one
        classification (#257)."""
        from . import derivatives
        if not os.path.isdir(uploads_dir):
            print("0 0")
            return 0
        removed, freed = derivatives.prune_image_derivatives(uploads_dir, self.log)
        print(f"{removed} {freed}")
        return 0

    @staticmethod
    def cmd_scrub_staging(path: str) -> int:
        """Remove links and special files from a tree the Linux launcher's
        bash create_backup just docker-cp'd out of a container (docker cp
        copies a link as a link, and zip would store the host file it points
        to). Messages go to stderr. A missing tree is nothing to do."""
        from .backup import scrub_staged_tree
        log = _make_log_func()
        if not os.path.lexists(path):
            return 0
        try:
            scrub_staged_tree(path, log)
            return 0
        except Exception as e:
            log(f"Backup: could not check the staged files for links: {e}")
            return 1

    @staticmethod
    def cmd_stage_wp_content(dest: str) -> int:
        """Copy wp-content out of the WordPress container into *dest*, read
        there as www-data and unpacked as plain files and folders
        (backup.stage_wp_content): the Linux launcher's create_backup.
        Messages go to stderr."""
        from .backup import stage_wp_content
        log = _make_log_func()
        try:
            stage_wp_content(dest, log)
            return 0
        except Exception as e:
            log(f"Backup: could not copy wp-content: {e}")
            return 1

    @staticmethod
    def cmd_stage_hub_data(dest: str) -> int:
        """Copy a hub's OnionHeaven registry and registrants' keys out of
        the onionheaven container into *dest* (backup.stage_hub_data): the
        Linux launcher's create_backup. Exit 0 when copied, 3 when the
        volume holds no hub data, 4 when the container did not answer, 1
        when the copy failed. Messages go to stderr."""
        from .backup import hub_data_state, stage_hub_data
        log = _make_log_func()
        state = hub_data_state()
        if state is None:
            log("Backup: the onionheaven container did not say whether it holds "
                "OnionHeaven data")
            return 4
        if state != "hub":
            return 3
        try:
            stage_hub_data(dest, log)
            return 0
        except Exception as e:
            log(f"Backup: could not copy the OnionHeaven data: {e}")
            return 1

    @staticmethod
    def cmd_settings_page_restore_check(staging: str) -> int:
        """For the Linux launcher's restore_backup --from-settings-page:
        exit 0 when the extracted backup in *staging* may be restored
        through the Settings page; exit 3, printing why on stdout, when it
        must not (backup.settings_page_restore_refusal: an OnionHeaven hub's
        registrants' keys, or OnionHome's name registry)."""
        from .backup import settings_page_restore_refusal
        refusal = settings_page_restore_refusal(staging)
        if refusal:
            print(refusal)
            return 3
        return 0

    @staticmethod
    def cmd_wordpress_safe_config(config_path: str, dest: str) -> int:
        """Write the config file *config_path*, cut to what a backup
        handed to WordPress may carry (backup.write_wordpress_safe_config:
        no Cloudflare tunnel token), to *dest*; print the names of the
        secrets that were set and left out, one per line. For the Linux
        launcher's create_backup --without-host-secrets. Messages go to
        stderr."""
        from .backup import write_wordpress_safe_config
        log = _make_log_func()
        try:
            for name in write_wordpress_safe_config(config_path, dest, log):
                print(name)
            return 0
        except Exception as e:
            log(f"Backup: could not write the config for WordPress: {e}")
            return 1

    @staticmethod
    def cmd_restore_hub_data(src: str) -> int:
        """Put a backup's onionheaven/ (*src*) onto the hub data volume
        (backup.restore_hub_data): the Linux launcher's restore_backup.
        Messages go to stderr."""
        from .backup import restore_hub_data
        log = _make_log_func()
        try:
            restore_hub_data(src, log)
            return 0
        except Exception as e:
            log(f"Restore: could not restore the OnionHeaven data: {e}")
            return 1

    @staticmethod
    def cmd_stage_hub_backup(staging: str, data_dir: str) -> int:
        """Put a hub's registry and registrants' keys into the staged backup
        *staging* (backup.stage_hub_backup: sealed to the recovery key if
        one is set up, else a plain onionheaven/ folder with a warning) and
        print the metadata.json fields that say how, as JSON. Exit 3 when
        the volume holds no hub data, 4 when the onionheaven container did
        not answer, 1 when it failed. For the Linux launcher's
        create_backup; messages go to stderr."""
        from .backup import hub_data_state, stage_hub_backup
        log = _make_log_func()
        state = hub_data_state()
        if state is None:
            log("Backup: the onionheaven container did not say whether it holds "
                "OnionHeaven data")
            return 4
        if state != "hub":
            return 3
        try:
            fields = stage_hub_backup(staging, log, data_dir=data_dir)
        except Exception as e:
            log(f"Backup: could not back up the OnionHeaven data: {e}")
            return 1
        print(json.dumps(fields))
        return 0

    @staticmethod
    def cmd_settings_page_backup_check(address: str, data_dir: str) -> int:
        """For the Linux launcher's create_backup --without-hub-data (the
        Settings page's backup): exit 0 when this node may be backed up
        there; exit 3, printing why on stdout, on an OnionHeaven hub or
        OnionHome, or when that could not be checked
        (backup.settings_page_backup_refusal: fails closed). *address* is
        the node's onion address, from its tor container (empty: refused)."""
        from .backup import hub_data_state, settings_page_backup_refusal
        refusal = settings_page_backup_refusal(address, hub_data_state(),
                                               data_dir=data_dir)
        if refusal:
            print(refusal)
            return 3
        return 0

    @staticmethod
    def cmd_settings_page_restore_node_check(address: str, data_dir: str) -> int:
        """For the Linux launcher's restore_backup --from-settings-page:
        exit 0 when the Settings page may restore on this node; exit 3,
        printing why on stdout, on an OnionHeaven hub or OnionHome, or when
        that could not be checked (backup.settings_page_restore_node_refusal:
        fails closed). *address* is the node's onion address, from its tor
        container (empty: refused)."""
        from .backup import hub_data_state, settings_page_restore_node_refusal
        refusal = settings_page_restore_node_refusal(address, hub_data_state(),
                                                     data_dir=data_dir)
        if refusal:
            print(refusal)
            return 3
        return 0

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

    def cmd_pack_backup(self, staging: str, output_path: str, password: str) -> int:
        """Zip and encrypt a staged backup tree into *output_path* (backup
        format 2). Used by the Linux launcher's bash create_backup so both
        platforms write the one format. Messages go to stderr only: the
        launcher appends them to its own log."""
        from .backup import pack_backup
        log = _make_log_func()
        try:
            pack_backup(staging, output_path, password, log)
            return 0
        except Exception as e:
            log(f"Backup: encrypting the archive failed: {e}")
            return 1

    def cmd_extract_backup(self, archive: str, dest: str, password: str) -> int:
        """Decrypt and extract a backup of either format into *dest*. Used by
        the bash launchers' restore and `setup --from-backup` paths, which
        used to run `unzip -P` and cannot read format 2."""
        from .backup import extract_backup
        log = _make_log_func()
        try:
            extract_backup(archive, password, log, staging=dest)
            return 0
        except Exception as e:
            log(f"Restore: could not extract the backup: {e}")
            return 1

    @staticmethod
    def cmd_backup_address(staging: str) -> int:
        """Print the onion address an extracted backup's key belongs to
        (nothing if it has no key); exit 1, printing nothing, on invalid
        metadata or key files. Used by the bash launchers' restore and
        `setup --from-backup` paths, which name a directory after the
        address: they never read it from metadata.json themselves."""
        from .backup import staged_backup_address
        log = _make_log_func()
        try:
            address = staged_backup_address(staging, log)
        except Exception as e:
            log(f"Restore: refusing the backup: {str(e)[:300]}")
            return 1
        if address:
            print(address)
        return 0

    @staticmethod
    def cmd_healthcheck_key_address(hc_dir: str) -> int:
        """Print the onion address of a backup's healthcheck key folder
        (*hc_dir*); exit 1, printing nothing, unless its key files parse and
        agree and its hostname names that address
        (backup.staged_healthcheck_address). For the Linux launcher's
        restore_backup, which writes the hostname from this."""
        from .backup import staged_healthcheck_address
        log = _make_log_func()
        try:
            address = staged_healthcheck_address(hc_dir)
        except Exception as e:
            log(f"Restore: the backup's healthcheck key is left out: {str(e)[:300]}")
            return 1
        print(address)
        return 0

    @staticmethod
    def cmd_restore_config(staging: str, config_path: str, address: str = "") -> int:
        """Merge an extracted backup's user preferences into *config_path*
        (backup.restore_backup_config: an allowlist; every other key keeps
        its value). *address* is the restored, key-derived address, which
        ADDRESS_PREFIX must match. Used by the bash launchers instead of
        copying the backup's config over the install's."""
        from .backup import restore_backup_config
        from .key_manager import is_valid_onion_address
        log = _make_log_func()
        try:
            restore_backup_config(
                staging, config_path, log,
                address if is_valid_onion_address(address) else "")
        except Exception as e:
            log(f"Restore: could not restore settings: {str(e)[:300]}")
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

    def cmd_update_images(self, json_output: bool = False) -> int:
        """`onionpress update-images`: pull the stack's pinned images (the
        menubar's Check for Updates; a launch pulls only what is missing).
        Never for locally built images (containers.using_local_images). A
        fresh download is followed by a prune of dangling images (#230)."""
        from . import confirm as _confirm
        from .containers import using_local_images
        if using_local_images(self.paths.config_file):
            self.log("Skipping image update: running locally built images")
            return _confirm.answer_ok(
                json_output, text="Skipping image update: running locally built images",
                pulled=False, local_images=True)
        result = self.containers.pull_images_result(timeout=600)
        if not result.ok:
            return _confirm.answer_error(
                f"image pull failed: {(result.stderr or '').strip()[-400:]}", json_output)
        pulled = "Downloaded" in result.stdout or "Pulled" in result.stdout
        reclaimed = ""
        if pulled:
            # Issue #230: prune dangling images right after a fresh pull,
            # exactly when old tags become <none>. Dangling-only (no -a),
            # so in-use and tagged images are untouched.
            r = self.docker.run(["image", "prune", "-f"], timeout=60, quiet=True)
            if r.ok:
                for line in (r.stdout or "").splitlines():
                    if line.strip().startswith("Total reclaimed space:"):
                        reclaimed = line.split(":", 1)[1].strip()
                self.log(f"Pruned dangling images: {reclaimed}" if reclaimed
                         else "Pruned dangling images")
        self.log("Docker images updated successfully" if pulled
                 else "Docker images are up to date")
        return _confirm.answer_ok(
            json_output, text=("Images updated." if pulled else "Images are up to date."),
            pulled=pulled, local_images=False, reclaimed=reclaimed)

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

    def _check_pid_lock(self) -> bool:
        """Check if another instance is running."""
        if not os.path.exists(self.paths.pid_file):
            return False
        try:
            with open(self.paths.pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check if process exists
            return True
        except (ValueError, OSError):
            # Stale PID file
            os.remove(self.paths.pid_file)
            return False

    def _write_pid_lock(self) -> None:
        with open(self.paths.pid_file, "w") as f:
            f.write(str(os.getpid()))

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
    """`onionpress settings-page-backup <zip>` (a Mac; Linux's launcher
    runs its bash create_backup --without-hub-data --without-host-secrets).
    Handed to WordPress by handle-action, and the password came from
    there: refused on an OnionHeaven hub or OnionHome, or a node that
    could not be checked (exit 3, SettingsPageBackupRefused's message), and
    never a hub's registry or registrants' keys, nor the Cloudflare tunnel
    token or any other config WordPress is not shown
    (backup.TUNNEL_TOKEN_KEY), nor the site's own onion service key
    (backup.ONION_KEY_LEFT_OUT_NOTE, the answer's "note": the app's
    Backup... has it)."""
    from . import confirm as _confirm
    from .backup import (create_backup, get_admin_username, served_onion_address,
                         SettingsPageBackupRefused, ONION_KEY_LEFT_OUT_NOTE)
    log = _settings_page_log(data_dir)
    try:
        metadata = create_backup(
            served_onion_address() or "unknown", get_admin_username(data_dir=data_dir),
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
    """`onionpress settings-page-restore <zip>` (a Mac; Linux's launcher
    runs its bash restore_backup --from-settings-page): the in-place
    backup.restore_from_backup(through_wordpress=True), never the
    install-from-backup `restore` (issue #341). The file and its password
    came through WordPress: refused on an OnionHeaven hub or OnionHome (or
    a node that could not be checked), and for a backup holding a hub's
    keys or OnionHome's name registry; neither volume is written. A backup
    of another address replaces this one only with --confirmed
    address-change (the handler asks the person at the Mac first: exit 4,
    {"confirm": "address-change", "current", "new"}); one without a key
    keeps it. Before the key changes, the OnionHeaven hubs that may hold
    the current one are asked to delete it, while it can still sign."""
    from . import confirm as _confirm
    from .backup import restore_from_backup, SettingsPageRestoreRefused
    log = _settings_page_log(data_dir)

    def confirm_address_change(current, new):
        _confirm.require("address-change", confirmed, current=current, new=new)
        return True

    def before_key_change(restored_address):
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
        return _confirm.answer_confirm(e, json_output)
    except SettingsPageRestoreRefused as e:
        return _confirm.answer_error(e, json_output, code=_confirm.EXIT_REFUSED)
    except Exception as e:
        log(f"Settings page restore failed: {e}")
        return _confirm.answer_error(e, json_output)
    return _confirm.answer_ok(json_output)


def main(argv: list[str] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="onionpress",
        description="OnionPress — WordPress over Tor",
    )
    parser.add_argument("--version", action="version", version=f"OnionPress {__version__}")
    parser.add_argument("--data-dir", help="Override data directory (default: ~/.onionpress/)")

    sub = parser.add_subparsers(dest="command", help="Command to run")
    from . import confirm as _confirm

    sub.add_parser("start", help="Start OnionPress")
    sub.add_parser("stop", help="Stop OnionPress")
    sub.add_parser("restart", help="Restart OnionPress")
    sub.add_parser("status", help="Show container status (JSON)")
    sub.add_parser("address", help="Print onion address")
    sub.add_parser("logs", help="Follow container logs")

    p_get = sub.add_parser(
        "get",
        help="Fetch a URL over Tor (curl inside the tor container) and print the body",
    )
    p_get.add_argument(
        "url",
        help="http(s) URL, or a bare host such as abc...xyz.onion (http:// assumed)")
    p_get.add_argument(
        "-i", "--headers", action="store_true",
        help="Include the response headers in the output (curl --include)")
    p_get.add_argument(
        "--max-time", type=int, default=GET_DEFAULT_MAX_TIME, metavar="SECONDS",
        help=f"Give up after this many seconds (default {GET_DEFAULT_MAX_TIME})")

    # The password is read by read_backup_password (environment, stdin or a
    # prompt). The positional is the old interface, kept so existing scripts
    # still work; it puts the password where other users can see it.
    p_backup = sub.add_parser(
        "backup", help="Create a backup",
        epilog=f"The WP admin password (also used to encrypt the zip) is read "
               f"from ${BACKUP_PASSWORD_ENV}, else stdin, else a prompt.")
    p_backup.add_argument(
        "password", nargs="?",
        help="Deprecated: visible to other local users; leave it out")
    p_backup.add_argument("output", nargs="?", help="Output path (default: ~/OnionPress/backups/)")
    p_backup.add_argument(
        "-o", "--output", dest="output_opt", metavar="OUTPUT",
        help="Output path, for use without the positional password")
    p_backup.add_argument("--user", help="WP admin username (default: auto-resolve)")
    _confirm.add_arguments(p_backup)

    p_restore = sub.add_parser(
        "restore", help="Restore from backup",
        epilog=f"The backup password is read from ${BACKUP_PASSWORD_ENV}, "
               f"else stdin, else a prompt.")
    p_restore.add_argument(
        "password", nargs="?",
        help="Deprecated: visible to other local users; leave it out")
    p_restore.add_argument("backup_file", help="Path to backup .zip file")
    _confirm.add_arguments(p_restore)

    p_reset = sub.add_parser("reset", help="Wipe all data and start fresh")
    p_reset.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    _confirm.add_arguments(p_reset)

    p_update = sub.add_parser(
        "update",
        help="(macOS) Check GitHub for a new release and install it, replacing "
             "the app bundle (--check to only check, --yes to skip the prompt)")
    p_update.add_argument("--check", action="store_true", help="Only check; install nothing")
    p_update.add_argument("--yes", "-y", action="store_true",
                          help="Install without prompting (scripts, the menubar)")
    p_update.add_argument("--json", action="store_true",
                          help="Answer with one JSON object on the last line")
    p_upimg = sub.add_parser(
        "update-images",
        help="Pull the stack's pinned container images (the opt-in refresh)")
    p_upimg.add_argument("--json", action="store_true",
                         help="Answer with one JSON object on the last line")
    p_uninst = sub.add_parser(
        "uninstall",
        help="(macOS) Remove OnionPress: the onion address and key, the site, "
             "the container VM and ~/.onionpress (asks first)")
    _confirm.add_arguments(p_uninst)

    p_ohu = sub.add_parser(
        "onionheaven-unregister",
        help="Ask every OnionHeaven hub that may hold one of this install's "
             "onion service keys to delete its copy (before a reset, key "
             "import, restore or uninstall; the tor container must be "
             "running). Exit 0 when no hub holds anything any more")
    p_ohu.add_argument("--reason", default="unregister",
                       help="Why, for the record and the log (reset, ...)")
    p_ohu.add_argument("--keep-address", default="",
                       help="An address whose key stays (a restore of it): "
                            "its registrations are left alone")
    sub.add_parser(
        "onionheaven-registrations",
        help="List what the registration record says OnionHeaven hubs may "
             "hold of this install's keys (no network)")

    p_cfu = sub.add_parser(
        "check-for-update",
        help="Check GitHub for a newer release (always exits 0)",
    )
    p_cfu.add_argument("--json", action="store_true",
                       help="Emit canonical JSON report on stdout")
    p_cfu.add_argument("--current", help="Override detected current version")

    sub.add_parser(
        "smoke-test-wayback",
        help="Publish a throwaway post and verify it got archived to the Wayback Machine",
    )

    sub.add_parser("generate-vanity",
                   help="Generate a vanity .onion via mkp224o in the tor container")

    sub.add_parser("admin-password",
                   help="Print the recovery admin password (if any)")

    p_ppi = sub.add_parser(
        "provision-post-install",
        help="Run the WordPress post-install steps (multisite + theme + mu-plugins)",
    )
    p_ppi.add_argument(
        "--themes-dir", required=True,
        help="Source directory containing the onionpress theme dir")
    p_ppi.add_argument(
        "--plugins-dir", required=True,
        help="Source directory containing mu-plugins, sunrise.php, icons")

    # Individual post-start provisioning steps. These are the helpers
    # start_containers used to inline as bash function calls; the bash
    # launchers now delegate each one through here.
    sub.add_parser(
        "verify-admin-password",
        help="Validate a WP admin password (reads from stdin). Exit 0 = match.",
    )
    sub.add_parser("configure-ia-plugin",
                   help="Configure the IA Wayback Machine Link Fixer in every site "
                        "(before it is activated)")
    sub.add_parser("configure-cache-enabler",
                   help="Prepare Cache Enabler's activation (policy mu-plugin, FS_METHOD)")
    sub.add_parser("apply-cache-policy",
                   help="Check Cache Enabler's settings against OnionPress's cache "
                        "policy; clear its stored pages (after activation)")
    sub.add_parser("deactivate-wp-statistics",
                   help="Remove WP-Statistics if present (clearnet leak)")
    sub.add_parser("ensure-archive-s3-keys",
                   help="Fetch shared archive.org S3 keys for Wayback archiving")

    p_scrub = sub.add_parser(
        "scrub",
        help="Full lifecycle test: backup → uninstall → install → restore → verify",
    )
    p_scrub.add_argument(
        "password", nargs="?",
        help="WordPress admin password (prompts if omitted)")
    p_scrub.add_argument(
        "--clean", action="store_true",
        help="Delete the backup zip after a successful scrub")

    p_iba = sub.add_parser(
        "import-backup-artifacts",
        help="Import a backup's container-side artifacts into the running "
             "containers (install-from-backup; no password needed)")
    p_iba.add_argument(
        "--staging", required=True,
        help="Path to the already-extracted backup staging directory")

    sub.add_parser(
        "regenerate-derivatives",
        help="Rebuild the resized image copies a restored backup left out "
             "(no-op unless a restore left the pending marker; resumable)")
    p_prune = sub.add_parser(
        "prune-image-derivatives",
        help="Delete regenerable -WxH/-scaled image copies from a staged "
             "uploads dir (backup helper); prints '<files> <bytes>'")
    p_prune.add_argument(
        "--uploads-dir", required=True,
        help="Path to the staged wp-content/uploads directory")
    p_scrub = sub.add_parser(
        "scrub-staging",
        help="Remove symbolic links, hard links to outside files and "
             "special files from a staged backup tree (backup helper)")
    p_scrub.add_argument(
        "--dir", required=True,
        help="Staged tree copied out of a container (e.g. wp-content)")
    p_stage = sub.add_parser(
        "stage-wp-content",
        help="Copy wp-content out of the WordPress container, read as "
             "www-data, as plain files and folders (backup helper)")
    p_stage.add_argument("--dest", required=True, help="Folder to fill")
    p_stage_hub = sub.add_parser(
        "stage-hub-data",
        help="Copy a hub's OnionHeaven registry and keys out of the "
             "onionheaven container (backup helper; exit 3: not a hub)")
    p_stage_hub.add_argument("--dest", required=True, help="Folder to fill")
    p_sp_check = sub.add_parser(
        "settings-page-restore-check",
        help="Exit 3 and say why if an extracted backup must not be restored "
             "through the WordPress Settings page (restore helper)")
    p_sp_check.add_argument(
        "--staging", required=True,
        help="Directory the backup was extracted into")
    p_wp_conf = sub.add_parser(
        "wordpress-safe-config",
        help="Write a config file cut to what a backup handed to WordPress "
             "may carry (backup helper); prints the secrets left out, by name")
    p_wp_conf.add_argument("--config", required=True, help="Config file to read")
    p_wp_conf.add_argument("--dest", required=True, help="File to write (new)")
    p_restore_hub = sub.add_parser(
        "restore-hub-data",
        help="Put a backup's OnionHeaven registry and keys onto the hub "
             "data volume (restore helper)")
    p_restore_hub.add_argument("--src", required=True,
                               help="The backup's onionheaven/ folder")
    p_stage_hub_backup = sub.add_parser(
        "stage-hub-backup",
        help="Put a hub's registry and keys into a staged backup, sealed to "
             "its recovery key if one is set up; print the metadata fields "
             "(backup helper; exit 3: not a hub, 4: no answer)")
    p_stage_hub_backup.add_argument("--staging", required=True,
                                    help="The staged backup folder")
    p_sp_backup = sub.add_parser(
        "settings-page-backup-check",
        help="Exit 3 and say why if this node must not be backed up through "
             "the WordPress Settings page (an OnionHeaven hub or OnionHome, "
             "or one that could not be checked)")
    p_sp_backup.add_argument("--address", default="",
                             help="This node's onion address")
    p_sp_restore_node = sub.add_parser(
        "settings-page-restore-node-check",
        help="Exit 3 and say why if nothing may be restored on this node "
             "through the WordPress Settings page (an OnionHeaven hub or "
             "OnionHome, or one that could not be checked)")
    p_sp_restore_node.add_argument("--address", default="",
                                   help="This node's onion address")

    # An OnionHeaven hub's backup recovery key. Its private half is printed
    # once by create and otherwise read from $ONIONPRESS_HUB_RECOVERY_KEY,
    # stdin or a prompt, never from argv.
    p_hrk = sub.add_parser(
        "hub-recovery-key",
        help="An OnionHeaven hub's backup recovery key: the hub's registry and "
             "registrants' keys in its backups are sealed to it",
        epilog="The private half is read from $ONIONPRESS_HUB_RECOVERY_KEY, "
               "else stdin, else a prompt; never give it as an argument.")
    hrk = p_hrk.add_subparsers(dest="hrk_command")
    p_hrk_create = hrk.add_parser(
        "create", help="Make the key; keep the public half here and print the "
                       "private half once, to keep off this machine")
    p_hrk_create.add_argument("--force", action="store_true",
                              help="Replace a key already set up (backups sealed "
                                   "to it still need its private half)")
    hrk.add_parser("show", help="Say whether a key is set up, and its id")
    hrk.add_parser("check", help="Check that a private half you kept is the key "
                                 "set up here")
    p_hrk_restore = hrk.add_parser(
        "restore", help="Restore only the hub's sealed registry and registrants' "
                        "keys from a backup (OnionPress must be running; the "
                        "backup password, then the key)")
    p_hrk_restore.add_argument("backup_file", help="Path to the backup .zip file")
    p_put = sub.add_parser(
        "put-wp-tree",
        help="Copy a host folder into the WordPress container, written as "
             "www-data (restore and plugin-install helper)")
    p_put.add_argument("--src", required=True, help="Host folder")
    p_put.add_argument("--dest", required=True,
                       help="Folder in the container (replaced whole unless --merge)")
    p_put.add_argument("--merge", action="store_true",
                       help="Unpack over what is at --dest instead of replacing it")

    p_open_login = sub.add_parser(
        "open-login",
        help="Open a localhost or .onion URL of this site in the default "
             "browser, logged in as the admin (one-time token, never in argv)")
    p_open_login.add_argument("url", help="e.g. http://localhost:8080/wp-admin/")

    sub.add_parser(
        "safe-config",
        help="Print the allowlisted (non-secret) config keys as JSON — "
             "what the WordPress container may see")
    p_wp_logs = sub.add_parser(
        "wordpress-logs",
        help="Print the end of a log, scrubbed for the WordPress container "
             "(the status page's recent-logs.txt)")
    p_wp_logs.add_argument("--log-file", required=True, help="Log to read")
    sub.add_parser(
        "wordpress-status",
        help="Read status JSON on stdin and print the fields the WordPress "
             "container may see")
    p_egress = sub.add_parser(
        "wordpress-egress",
        help="Shut WordPress's network (onionpress-wp0) off from everything "
             "but replies to connections made from outside, the Tor side's "
             "(onionpress-tor0) off from the host and the LAN, and "
             "WordPress's published port off from every container, wherever "
             "this platform's Docker keeps its bridges (issue #144); prints "
             "one status line")
    p_egress.add_argument(
        "--check", action="store_true",
        help="Only say whether the rules are in place")
    p_egress.add_argument(
        "--wp-port", type=int, default=None,
        help="WordPress's published port (default: $ONIONPRESS_WP_PORT, "
             "which both launchers export; without either the published-"
             "port rule is left as it is)")
    p_egress.add_argument(
        "--bin-dir", default=None,
        help="Where the app's limactl is (macOS; default: the app bundle's)")
    sub.add_parser(
        "apply-settings-updates",
        help="Merge the settings page's config-updates.json (on stdin) into "
             "the config — allowlisted keys with valid values only; prints "
             "'changed|unchanged|rejected KEY' lines, never values")

    # Backup helpers for the bash launchers. The password comes on stdin,
    # never in argv.
    p_pack = sub.add_parser(
        "pack-backup",
        help="Zip and encrypt a staged backup directory (backup helper; "
             "reads the password from stdin)")
    p_pack.add_argument(
        "--staging", required=True,
        help="Staged backup tree (metadata.json, tor-keys/, database/, ...)")
    p_pack.add_argument(
        "--output", required=True, help="Backup file to write")
    p_extract = sub.add_parser(
        "extract-backup",
        help="Decrypt and extract a backup, either format, into a directory "
             "(restore helper; reads the password from stdin)")
    p_extract.add_argument(
        "--archive", required=True, help="Backup file to read")
    p_extract.add_argument(
        "--dest", required=True, help="Directory to extract into")

    p_baddr = sub.add_parser(
        "backup-address",
        help="Validate an extracted backup and print the onion address its "
             "key belongs to (restore helper; exit 1 if invalid)")
    p_baddr.add_argument(
        "--staging", required=True,
        help="Directory the backup was extracted into")

    p_hcaddr = sub.add_parser(
        "healthcheck-key-address",
        help="Check a backup's healthcheck key folder and print its onion "
             "address (restore helper; exit 1 if it does not check out)")
    p_hcaddr.add_argument(
        "--dir", required=True,
        help="The backup's tor-keys/healthcheck/ folder")

    p_config = sub.add_parser(
        "config",
        help="Read and change settings in ~/.onionpress/config "
             "(get KEY | set KEY VALUE | unset KEY | list [--json])",
        description="Settings: every key `config list` shows, with the check "
                    "its value gets on the Settings page. Image overrides, "
                    "ports, paths (ONIONPRESS_*) and ONIONHEAVEN_ACCEPT_"
                    "REGISTRATIONS are hand-edit only. A secret "
                    "(CLOUDFLARE_TUNNEL_TOKEN) is set with VALUE `-` and the "
                    "value on stdin, never on the command line.")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_cget = config_sub.add_parser("get", help="Print one setting's value")
    p_cget.add_argument("key", metavar="KEY")
    p_cset = config_sub.add_parser(
        "set", help="Change one setting (VALUE `-` reads the value from stdin)")
    p_cset.add_argument("key", metavar="KEY")
    p_cset.add_argument("value", metavar="VALUE")
    # ADDRESS_PREFIX the live address does not start with asks
    # "prefix-change" (onionpress.confirm).
    _confirm.add_arguments(p_cset)
    p_cunset = config_sub.add_parser(
        "unset", help="Remove one setting's line, so its default applies")
    p_cunset.add_argument("key", metavar="KEY")
    p_clist = config_sub.add_parser(
        "list", help="Print every setting (a secret only as <set>/<empty>)",
        epilog="\n".join(f"  {k}: {v}" for k, v in sorted(_config_key_help().items())),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_clist.add_argument("--json", action="store_true",
                         help="As JSON: {\"settings\": {...}, \"hand_edit_only\": [...]}")

    p_oh = sub.add_parser(
        "onionheaven",
        help="Keep the site reachable through OnionHeaven: enable | disable | status [--json]")
    p_oh.add_argument("onionheaven_command", nargs="?", default="status",
                      metavar="{enable,disable,status}")
    p_oh.add_argument("--json", action="store_true",
                      help="status as JSON (enabled, choice, hub, registered, may_hold_key)")

    p_rconf = sub.add_parser(
        "restore-config",
        help="Merge an extracted backup's user preferences into a config "
             "file (restore helper; an allowlist, never a copy)")
    p_rconf.add_argument(
        "--staging", required=True,
        help="Directory the backup was extracted into")
    p_rconf.add_argument("--config", required=True, help="Config file to update")
    p_rconf.add_argument(
        "--address", default="",
        help="The restored onion address (ADDRESS_PREFIX must match it)")

    # The WordPress Settings page's requests (issue #366): one handler for
    # both platforms, run by onionpress-watcher.timer on Linux and by the
    # menubar's status loop on a Mac.
    p_ha = sub.add_parser(
        "handle-action",
        help="Handle the WordPress Settings page's request (requested-action "
             "in the shared volume), if there is one")
    p_ha.add_argument(
        "--launcher", default="",
        help="The launcher script the actions run through (default: this "
             "install's)")
    p_ha.add_argument(
        "--cancel", action="store_true",
        help="The question a previous run asked was answered Cancel")
    _confirm.add_arguments(p_ha, check=False)
    sub.add_parser(
        "apply-config",
        help="Apply the Settings page's config-updates.json (the allowlisted "
             "keys), then remove it")
    p_spb = sub.add_parser(
        "settings-page-backup",
        help="The Settings page's backup: for WordPress, so without the hub "
             "data, the host's secrets or (on a Mac) the onion service key; "
             "the password from $ONIONPRESS_BACKUP_PASSWORD or stdin")
    p_spb.add_argument("output", help="Where to write the zip")
    p_spb.add_argument("--json", action="store_true",
                       help="Answer with one JSON object on the last line")
    p_spr = sub.add_parser(
        "settings-page-restore",
        help="The Settings page's restore: in place, never on a hub or "
             "OnionHome, never a hub's keys; a backup of another address "
             "needs --confirmed address-change; the password from "
             "$ONIONPRESS_BACKUP_PASSWORD or stdin")
    p_spr.add_argument("backup_file", help="The uploaded backup zip")
    _confirm.add_arguments(p_spr, check=False)

    args = parser.parse_args(argv)

    if not args.command:
        args.command = "start"

    # A hub's backup recovery key leaves the environment at once, whatever
    # the command: OnionPressCLI's Docker wrapper hands os.environ to every
    # docker child. Used by restore and hub-recovery-key only.
    from .hub_backup import RECOVERY_KEY_ENV
    hub_recovery_key = os.environ.pop(RECOVERY_KEY_ENV, "")
    data_dir = args.data_dir or os.path.join(os.path.expanduser("~"), ".onionpress")

    if args.command == "config":
        # Only the config file, its change marker and the login item /
        # user units: no OnionPressCLI setup (no Docker, no port probing).
        from . import config as _config
        if not args.config_command:
            p_config.print_help()
            return 1
        paths = resolve_paths(data_dir=args.data_dir)
        return _config.run_config_command(
            args, data_dir, _file_log_func(paths.log_file),
            os_name=detect_os().value, app_bundle=paths.app_bundle)

    if args.command == "onionheaven":
        # The same: the config file and the registration record, no network.
        from . import onionheaven
        return onionheaven.run_onionheaven_command(
            args.onionheaven_command, _hub_context(args.data_dir),
            json_output=args.json)

    if args.command in ("handle-action", "apply-config"):
        # Run every 10 s (Linux) or every status tick (Mac): no
        # OnionPressCLI setup (config/secrets creation, port probing).
        from . import handle_action as _ha
        paths = resolve_paths(data_dir=args.data_dir)
        log = _make_log_func(paths.log_file)
        os_name = detect_os().value
        host = _ha.Host(
            launcher=(getattr(args, "launcher", "") or _ha.default_launcher(paths, os_name)),
            paths=paths, docker=Docker(paths, log_func=log), log=log, os_name=os_name,
            version=__version__)
        if args.command == "apply-config":
            return _ha.apply_config(host)
        result = _ha.handle(host, confirmed=_confirm.given(args), cancel=args.cancel)
        if args.json:
            print(_confirm.dumps(result))
        return 0

    if args.command == "update" and detect_os() == OS.MACOS:
        # Replaces the bundle this process runs from: no OnionPressCLI
        # setup, nothing but the updater.
        paths = resolve_paths(data_dir=args.data_dir)
        return cmd_update(paths, _make_log_func(paths.log_file), check=args.check,
                          yes=args.yes, json_output=args.json)

    if args.command == "uninstall":
        # About to remove the data directory: no OnionPressCLI setup (it
        # would create config and secrets there).
        from . import handle_action as _ha
        paths = resolve_paths(data_dir=args.data_dir)
        log = _make_log_func(paths.log_file)
        os_name = detect_os().value
        if os_name != "macos":
            return _confirm.answer_error(
                "onionpress uninstall is the Linux launcher's own command", args.json)
        return cmd_uninstall(paths, Docker(paths, log_func=log), log,
                             _ha.default_launcher(paths, os_name), check=args.check,
                             json_output=args.json, confirmed=_confirm.given(args))

    if args.command in ("settings-page-backup", "settings-page-restore"):
        # The page's own backup and restore, run by handle-action through
        # the launcher (on Linux the launcher's bash does these itself).
        password = read_backup_password()
        if not password:
            return _confirm.answer_error("a password is required", args.json)
        if args.command == "settings-page-backup":
            return cmd_settings_page_backup(args.output, password, data_dir, args.json)
        return cmd_settings_page_restore(
            args.backup_file, password, data_dir, args.json, _confirm.given(args))

    if args.command == "hub-recovery-key":
        # Needs only the data directory (restore: docker too): no
        # OnionPressCLI setup.
        if args.hrk_command == "create":
            return OnionPressCLI.cmd_hub_recovery_key_create(data_dir, force=args.force)
        if args.hrk_command == "show":
            return OnionPressCLI.cmd_hub_recovery_key_show(data_dir)
        if args.hrk_command == "check":
            return OnionPressCLI.cmd_hub_recovery_key_check(data_dir, hub_recovery_key)
        if args.hrk_command == "restore":
            password = read_backup_password()
            if not password:
                print("ERROR: a password is required.", file=sys.stderr)
                return 1
            return OnionPressCLI.cmd_hub_recovery_key_restore(
                args.backup_file, password, hub_recovery_key)
        p_hrk.print_help()
        return 1

    # Restore helpers, run by the launchers mid-restore: no OnionPressCLI
    # setup (config/secrets creation, port probing).
    if args.command == "backup-address":
        return OnionPressCLI.cmd_backup_address(args.staging)
    if args.command == "healthcheck-key-address":
        return OnionPressCLI.cmd_healthcheck_key_address(args.dir)
    if args.command == "restore-config":
        return OnionPressCLI.cmd_restore_config(
            args.staging, args.config, args.address)
    # Backup helper, run by the Linux launcher mid-backup: same.
    if args.command == "scrub-staging":
        return OnionPressCLI.cmd_scrub_staging(args.dir)
    if args.command == "stage-wp-content":
        return OnionPressCLI.cmd_stage_wp_content(args.dest)
    if args.command == "stage-hub-data":
        return OnionPressCLI.cmd_stage_hub_data(args.dest)
    if args.command == "settings-page-restore-check":
        return OnionPressCLI.cmd_settings_page_restore_check(args.staging)
    if args.command == "wordpress-safe-config":
        return OnionPressCLI.cmd_wordpress_safe_config(args.config, args.dest)
    if args.command == "restore-hub-data":
        return OnionPressCLI.cmd_restore_hub_data(args.src)
    if args.command == "stage-hub-backup":
        return OnionPressCLI.cmd_stage_hub_backup(args.staging, data_dir)
    if args.command == "settings-page-backup-check":
        return OnionPressCLI.cmd_settings_page_backup_check(args.address, data_dir)
    if args.command == "settings-page-restore-node-check":
        return OnionPressCLI.cmd_settings_page_restore_node_check(args.address, data_dir)
    # Restore and plugin-install helper for the bash launchers: same.
    if args.command == "put-wp-tree":
        return OnionPressCLI.cmd_put_wp_tree(args.src, args.dest, args.merge)

    if args.command == "safe-config":
        # Read-only, and run by the Linux launcher every 30 s: skip
        # OnionPressCLI's setup (config/secrets creation, port probing).
        from .config import read_config, redact_config
        data_dir = args.data_dir or os.path.join(os.path.expanduser("~"), ".onionpress")
        config = read_config(os.path.join(data_dir, "config"))
        print(json.dumps(redact_config(config), indent=2))
        return 0

    if args.command in ("wordpress-logs", "wordpress-status"):
        # Run by the Linux launcher on every status write; read-only like
        # safe-config, so it skips OnionPressCLI's setup too.
        from . import redact
        data_dir = args.data_dir or os.path.join(os.path.expanduser("~"), ".onionpress")
        if args.command == "wordpress-logs":
            try:
                sys.stdout.write(redact.wordpress_log_excerpt(args.log_file, data_dir))
            except OSError as e:
                print(f"ERROR: {e.strerror or e}", file=sys.stderr)
                return 1
            return 0
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

    if args.command == "apply-settings-updates":
        # The payload was written inside WordPress, so it is untrusted
        # (config.SETTINGS_PAGE_KEYS). Same fast path as safe-config: only
        # the config file is touched.
        from .config import apply_settings_updates
        data_dir = args.data_dir or os.path.join(os.path.expanduser("~"), ".onionpress")
        try:
            updates = json.loads(sys.stdin.read())
        except ValueError:
            print("ERROR: config-updates.json is not valid JSON", file=sys.stderr)
            return 1
        applied = apply_settings_updates(os.path.join(data_dir, "config"), updates)
        changed = {key for key, _old, _new in applied.changes}
        for key in applied.accepted:
            print(f"{'changed' if key in changed else 'unchanged'} {key}")
        for key in applied.rejected:
            print(f"rejected {key}")
        return 0

    if args.command == "wordpress-egress":
        # Run by both launchers before they start the containers: no
        # OnionPressCLI setup (config/secrets creation, port probing).
        import dataclasses
        from . import wordpress_egress
        paths = resolve_paths(data_dir=args.data_dir)
        if args.bin_dir:
            paths = dataclasses.replace(paths, bin_dir=args.bin_dir)
        wp_port = args.wp_port
        if wp_port is None and os.environ.get("ONIONPRESS_WP_PORT", "").isdigit():
            wp_port = int(os.environ["ONIONPRESS_WP_PORT"])
        result = wordpress_egress.ensure(
            paths, Docker(paths), action="check" if args.check else "apply",
            wp_port=wp_port)
        print(result.line())
        return 0 if result.ok else 1

    if args.command == "onionheaven-unregister":
        # Run by both launchers before a reset, key import, restore or
        # uninstall: no OnionPressCLI setup (config/secrets creation, port
        # probing) on a path that is about to remove them.
        from .key_manager import is_valid_onion_address
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

    if args.command == "onionheaven-registrations":
        from . import onionheaven
        ctx = _hub_context(args.data_dir)
        for addr, hub in onionheaven.registrations_left(ctx):
            print(f"{addr} {hub}")
        return 0

    if args.command == "open-login":
        # Needs only docker and a browser: skip OnionPressCLI's setup.
        from .launcher_ops import open_login
        try:
            open_login(args.url)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0

    # Before OnionPressCLI: its Docker wrapper snapshots os.environ for every
    # docker child, and read_backup_password takes the password out of it.
    password = None
    if args.command in ("backup", "restore"):
        password = read_backup_password(args.password)
        if not password:
            if args.command == "restore" and args.check:
                # The UI asks for the password next, worded with the login
                # the file's name carries (for the prompt only).
                from .backup import backup_filename_username
                return _confirm.answer_ask(
                    "password", args.json,
                    backup_user=backup_filename_username(args.backup_file))
            return _confirm.answer_error("a password is required", args.json)

    cli = OnionPressCLI(data_dir=args.data_dir)

    commands = {
        "start": cli.cmd_start,
        "stop": cli.cmd_stop,
        "restart": cli.cmd_restart,
        "status": cli.cmd_status,
        "address": cli.cmd_address,
        "logs": cli.cmd_logs,
    }

    if args.command in commands:
        return commands[args.command]()
    elif args.command == "get":
        return cli.cmd_get(args.url, headers=args.headers, max_time=args.max_time)
    elif args.command == "backup":
        return cli.cmd_backup(password, args.output_opt or args.output, args.user,
                              check=args.check, json_output=args.json)
    elif args.command == "restore":
        # A hub's recovery key, when one was given (cmd_restore asks at a
        # terminal otherwise).
        confirmed = _confirm.given(args)
        if args.check:
            return cli.cmd_restore_check(password, args.backup_file, hub_recovery_key,
                                         confirmed, args.json)
        return cli.cmd_restore(password, args.backup_file, hub_recovery_key,
                               confirmed=confirmed, json_output=args.json)
    elif args.command == "import-backup-artifacts":
        return cli.cmd_import_backup_artifacts(args.staging)
    elif args.command == "regenerate-derivatives":
        return cli.cmd_regenerate_derivatives()
    elif args.command == "prune-image-derivatives":
        return cli.cmd_prune_image_derivatives(args.uploads_dir)
    elif args.command in ("pack-backup", "extract-backup"):
        pw = sys.stdin.read().rstrip("\n")
        if not pw:
            print("ERROR: no password on stdin", file=sys.stderr)
            return 1
        if args.command == "pack-backup":
            return cli.cmd_pack_backup(args.staging, args.output, pw)
        return cli.cmd_extract_backup(args.archive, args.dest, pw)
    elif args.command == "reset":
        return cli.cmd_reset(yes=args.yes, check=args.check, json_output=args.json,
                             confirmed=_confirm.given(args))
    elif args.command == "update-images":
        return cli.cmd_update_images(json_output=args.json)
    elif args.command == "smoke-test-wayback":
        return cli.cmd_smoke_test_wayback()
    elif args.command == "check-for-update":
        return cli.cmd_check_for_update(json_output=args.json, current=args.current)
    elif args.command == "generate-vanity":
        return cli.cmd_generate_vanity()
    elif args.command == "admin-password":
        return cli.cmd_admin_password()
    elif args.command == "provision-post-install":
        from . import multisite
        return multisite.provision_post_install(
            themes_dir=args.themes_dir,
            plugins_dir=args.plugins_dir,
            log_func=print,
        )
    elif args.command == "configure-ia-plugin":
        from . import multisite
        return 0 if multisite.configure_ia_plugin(log_func=print) else 1
    elif args.command == "configure-cache-enabler":
        from . import multisite
        return 0 if multisite.configure_cache_enabler(log_func=print) else 1
    elif args.command == "apply-cache-policy":
        from . import multisite
        return 0 if multisite.apply_cache_policy(log_func=print) else 1
    elif args.command == "deactivate-wp-statistics":
        from . import multisite
        return 0 if multisite.deactivate_wp_statistics(log_func=print) else 1
    elif args.command == "ensure-archive-s3-keys":
        from . import multisite
        return 0 if multisite.ensure_archive_s3_keys(log_func=print) else 1
    elif args.command == "scrub":
        from . import scrub as _scrub
        return _scrub.run_scrub(
            password=args.password,
            clean=args.clean,
        )
    elif args.command == "verify-admin-password":
        from .backup import verify_wp_admin_password_any
        pw = sys.stdin.read().rstrip("\n")
        if not pw:
            return 1
        ok, _ = verify_wp_admin_password_any(pw)
        return 0 if ok else 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
