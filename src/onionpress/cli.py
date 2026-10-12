"""OnionPress CLI entry point.

Usage: python -m onionpress.cli <command> [args]

Commands: start, stop, restart, status, address, logs, get, setup, backup, restore, reset
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

    def cmd_backup(self, password: str, output_path: str = None, username: str = None) -> int:
        """Create a backup."""
        from .backup import create_backup, backup_filename, verify_wp_admin, get_admin_username
        if not username:
            username = get_admin_username(data_dir=self.paths.data_dir)
        ok, err = verify_wp_admin(username, password)
        if not ok:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        addr = self.containers.get_onion_address()
        if not output_path:
            backups_dir = os.path.expanduser("~/OnionPress/backups")
            os.makedirs(backups_dir, exist_ok=True)
            output_path = os.path.join(
                backups_dir, backup_filename(addr, username))
        try:
            create_backup(
                onion_address=addr,
                username=username,
                password=password,
                output_path=output_path,
                version=__version__,
                log_func=self.log,
            )
            print(f"Backup saved to: {output_path}")
            return 0
        except Exception as e:
            self.log(f"Backup failed: {e}")
            return 1

    def cmd_restore(self, password: str, backup_path: str) -> int:
        """Restore from a backup via install-from-backup: tear the install down
        to a clean state and rebuild it directly from the backup (key + DB +
        content), instead of overwriting a live install in place. No vanity
        generation and no onion-service key-swap churn.
        """
        import shutil
        try:
            from .backup import prepare_install_from_backup
            staging, meta = prepare_install_from_backup(
                backup_path, password, self.log, data_dir=self.paths.data_dir)
        except Exception as e:
            self.log(f"Restore failed during extract/seed: {e}")
            return 1
        try:
            # Teardown: stop + wipe data + keystore volumes so the rebuild adopts
            # the seeded key and imported backup cleanly (no stale keystore
            # overriding the restored identity, no in-place overwrite).
            self.containers.stop()
            # onionpress-arti-state is the key volume's pre-2026-09-25 name;
            # wiping it too keeps the launcher's migration from bringing the
            # replaced identity back.
            for vol in ("onionpress-onion-keys", "onionpress-arti-state",
                        "onionpress-tor-state",
                        "onionpress-db-data", "onionpress-wordpress-data",
                        "onionpress-persistent-data"):
                self.docker.run(["volume", "rm", vol], timeout=20)
                self.log(f"Removed volume: {vol}")

            # Seed the fresh key volume from the backup key so Tor serves the
            # restored identity on first start (the entrypoint converts the PEM
            # to C Tor's key files when tor-state is empty). Mirrors the launcher's
            # first-run key install. Bind-mount the host vanity-keys dir, which
            # seed_onion_key_for_install wrote under shared/ (inside the Colima
            # mount, so the bind works on macOS as well as Linux).
            addr = meta.get("onion_address", "")
            vanity_addr_dir = os.path.join(
                self.paths.data_dir, "shared", "vanity-keys", addr)
            seed = self.docker.run([
                "run", "--rm",
                "-v", "onionpress-onion-keys:/dest",
                "--mount", f"type=bind,source={vanity_addr_dir},target=/src,readonly",
                "alpine", "sh", "-c",
                "mkdir -p /dest/wordpress && "
                "cp /src/ks_hs_id.ed25519_expanded_private /dest/wordpress/ && "
                "chown -R 0:0 /dest && "
                "chmod 700 /dest /dest/wordpress && "
                "chmod 600 /dest/wordpress/*",
            ], timeout=30)
            if not seed.ok:
                self.log("Restore: WARNING — key volume seed reported a "
                         "problem; Tor may not adopt the restored identity")

            # Rebuild: fresh containers adopt the seeded key; import the backup
            # artifacts into them; then bring up Tor with the restored identity.
            self.containers.start_core()
            self.containers.wait_for_wordpress()
            if self.cmd_import_backup_artifacts(staging) != 0:
                self.log("Restore: artifact import failed — staging kept for retry")
                return 1
            self.containers.start_tor()
            self.containers.wait_for_tor()

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
            return 0
        except Exception as e:
            self.log(f"Restore failed: {e}")
            return 1

    def cmd_import_backup_artifacts(self, staging: str) -> int:
        """Import a backup's container-side artifacts (DB, wp-content,
        OnionHeaven/OnionHome data, multisite constants) into the already-running
        containers, then migrate the DB schema. Used by the launcher's
        install-from-backup marker hook. Operates on an already-extracted staging
        dir, so no password is needed here.
        """
        try:
            from .backup import restore_container_artifacts
            meta_path = os.path.join(staging, "metadata.json")
            if not os.path.exists(meta_path):
                meta_path = os.path.join(staging, ".", "metadata.json")
            metadata = {}
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    metadata = json.load(f)
            restore_container_artifacts(staging, metadata, self.log)
            # Migrate DB schema for cross-version backups (non-fatal).
            res = self.docker.run(
                ["exec", "onionpress-wordpress",
                 "wp", "core", "update-db", "--allow-root"],
                timeout=120,
            )
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
        """Check GitHub for a newer release. Always exits 0.

        On any failure (network down, GitHub rate-limited, parse error)
        the JSON output carries an ``error`` field and ``update_available``
        is false — callers render that to the user instead of crashing.
        """
        from datetime import datetime, timezone
        from . import updater

        if not current:
            current = __version__

        # ``updater.check_for_update`` conflates "up to date" and "error":
        # both return ``None``. Capture the log to recover the distinction
        # without touching updater.py.
        error_msg = None

        def capture_log(msg: str) -> None:
            nonlocal error_msg
            if "failed" in msg.lower():
                error_msg = msg
            self.log(msg)

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

        report = {
            "current": current,
            "latest": latest,
            "update_available": update_available,
            "release_url": release_url,
            "release_notes": release_notes,
            "published_at": published_at,
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": error_msg,
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
        return 0

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

    def cmd_reset(self, yes: bool = False) -> int:
        """Reset OnionPress — wipe all data and start fresh."""
        if not yes:
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

        # Wipe data files (keep colima VM)
        for name in [
            "secrets", "onionpress.log", ".last_status_state",
            "config", "config.bak", "onionheaven-registration.json",
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
        return 0

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


def main(argv: list[str] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="onionpress",
        description="OnionPress — WordPress over Tor",
    )
    parser.add_argument("--version", action="version", version=f"OnionPress {__version__}")
    parser.add_argument("--data-dir", help="Override data directory (default: ~/.onionpress/)")

    sub = parser.add_subparsers(dest="command", help="Command to run")

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

    p_restore = sub.add_parser(
        "restore", help="Restore from backup",
        epilog=f"The backup password is read from ${BACKUP_PASSWORD_ENV}, "
               f"else stdin, else a prompt.")
    p_restore.add_argument(
        "password", nargs="?",
        help="Deprecated: visible to other local users; leave it out")
    p_restore.add_argument("backup_file", help="Path to backup .zip file")

    p_reset = sub.add_parser("reset", help="Wipe all data and start fresh")
    p_reset.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

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
                   help="Configure the IA Wayback Machine Link Fixer plugin")
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

    args = parser.parse_args(argv)

    if not args.command:
        args.command = "start"

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

    # Before OnionPressCLI: its Docker wrapper snapshots os.environ for every
    # docker child, and read_backup_password takes the password out of it.
    password = None
    if args.command in ("backup", "restore"):
        password = read_backup_password(args.password)
        if not password:
            print("ERROR: a password is required.", file=sys.stderr)
            return 1

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
        return cli.cmd_backup(password, args.output_opt or args.output, args.user)
    elif args.command == "restore":
        return cli.cmd_restore(password, args.backup_file)
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
        return cli.cmd_reset(yes=args.yes)
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
