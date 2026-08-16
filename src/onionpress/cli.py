"""OnionPress CLI entry point.

Usage: python -m onionpress.cli <command> [args]

Commands: start, stop, restart, status, address, logs, setup, backup, restore, reset
"""

import argparse
import json
import os
import sys
import time
from typing import Callable

from . import __version__
from .platform import OS, detect_os, resolve_paths, detect_timezone
from .config import (
    ensure_config, ensure_secrets, read_value, detect_port_offset,
)
from .docker import Docker
from .containers import ContainerManager


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
        extra_env["TOR_IMPL"] = read_value(
            self.paths.config_file, "TOR_IMPL", "tor"
        )
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
            for vol in ("onionpress-arti-state", "onionpress-tor-state",
                        "onionpress-db-data", "onionpress-wordpress-data",
                        "onionpress-persistent-data"):
                self.docker.run(["volume", "rm", vol], timeout=20)
                self.log(f"Removed volume: {vol}")

            # Seed the fresh arti-state keystore from the backup key so Tor serves
            # the restored identity on first start (the C-Tor entrypoint converts
            # arti->ctor when tor-state is empty). Mirrors the launcher's
            # first-run key install. Bind-mount the host vanity-keys dir, which
            # seed_onion_key_for_install wrote under shared/ (inside the Colima
            # mount, so the bind works on macOS as well as Linux).
            addr = meta.get("onion_address", "")
            vanity_addr_dir = os.path.join(
                self.paths.data_dir, "shared", "vanity-keys", addr)
            seed = self.docker.run([
                "run", "--rm",
                "-v", "onionpress-arti-state:/dest",
                "--mount", f"type=bind,source={vanity_addr_dir},target=/src,readonly",
                "alpine", "sh", "-c",
                "mkdir -p /dest/state/keystore/hss/wordpress && "
                "cp /src/ks_hs_id.ed25519_expanded_private "
                "/dest/state/keystore/hss/wordpress/ && "
                "chown -R 100:100 /dest/state && "
                "chmod 700 /dest/state /dest/state/keystore "
                "/dest/state/keystore/hss /dest/state/keystore/hss/wordpress && "
                "chmod 600 /dest/state/keystore/hss/wordpress/*",
            ], timeout=30)
            if not seed.ok:
                self.log("Restore: WARNING — arti-state key seed reported a "
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
        prefix = read_value(self.paths.config_file, "ADDRESS_PREFIX", "op2")
        if not (2 <= len(prefix) <= 6):
            self.log(f"ADDRESS_PREFIX must be 2-6 chars (got {prefix!r}); skipping")
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

    # ─── Headless onionname registry (for driving by an external app) ────
    #
    # An external app drives these programmatically and parses a single
    # JSON line from stdout. All diagnostics go through self.log
    # (stderr), so stdout carries exactly one JSON object per command.

    def _registrar(self):
        """Build a Registrar whose curl runs through the CLI's docker env.

        We inject a runner backed by self.docker.exec so the OnionHome
        request uses the same DOCKER_HOST / Colima socket the rest of the
        CLI does — working whether the CLI was invoked via the launcher
        (docker env exported) or directly. SOCKS over Colima port-forwarding
        is broken, so docker exec into the tor container is the only
        reliable path.
        """
        from .onionnames_registrar import Registrar, DEFAULT_CONTAINER

        def runner(curl_args, timeout):
            result = self.docker.exec(
                DEFAULT_CONTAINER, ["curl", *curl_args],
                timeout=timeout, quiet=True,
            )
            return result.returncode, result.stdout or ""

        return Registrar(log=self.log, runner=runner)

    def cmd_onionname_suggest(self) -> int:
        """Print one onionname suggestion as a single JSON line: {"name": …}.

        Asks the registry (authority on availability); falls back to a local
        wordlist suggestion if the registry is unreachable, so the caller
        always gets a plausible, validation-passing default.
        """
        from .onionnames_client import suggest_name_local

        name = ""
        try:
            result = self._registrar().suggest()
            if result.ok and isinstance(result.body, dict):
                name = (result.body.get("onionname") or "").strip()
        except Exception as e:
            self.log(f"onionname suggest: registry error: {e}")
        if not name:
            name = suggest_name_local() or ""
        print(json.dumps({"name": name}))
        return 0

    def cmd_onionname_check(self, name: str) -> int:
        """Print onionname availability as a single JSON line:
        {"available": bool, "reason": str, "suggestions": [...]}.

        Rejects locally-invalid names before hitting the registry (5–40
        chars, the shared charset, not all-numeric — mirrors OnionHome).
        """
        from .onionnames_client import validate_name

        ok, reason = validate_name(name)
        if not ok:
            print(json.dumps(
                {"available": False, "reason": reason, "suggestions": []}))
            return 0

        result = self._registrar().check(name)
        if result.ok and isinstance(result.body, dict):
            available = bool(result.body.get("available"))
            body_reason = result.body.get("reason")
            reason_str = body_reason or ("" if available else "taken")
            suggestions = result.body.get("suggestions") or []
            print(json.dumps({
                "available": available,
                "reason": reason_str,
                "suggestions": suggestions,
            }))
            return 0

        # Unreachable / server error: report not-available with the
        # diagnostic so the picker can surface "registry unreachable".
        print(json.dumps({
            "available": False,
            "reason": result.reason or result.status,
            "suggestions": result.suggestions or [],
        }))
        return 0

    def cmd_onionname_register(self, name: str) -> int:
        """Register `name` for this site's onion address and print a single
        JSON line: {"ok": true, "name", "address", "url"} on success, or
        {"ok": false, "error", "suggestions": [...]} on failure.

        Policy: the registered name maps to the onion address ROOT
        (http://<addr>.onion/). OnionPress's WP-admin/subsite-path coupling
        is intentionally not applied here — a headless registration
        addresses the whole service, not a WP subsite; the onionname is a
        memorable handle that maps to the address.
        """
        from .onionnames_client import validate_name

        ok, reason = validate_name(name)
        if not ok:
            print(json.dumps(
                {"ok": False, "error": reason, "suggestions": []}))
            return 0

        # Resolve the onion address the same way `address` does.
        onionaddress = self.containers.get_onion_address()
        if not onionaddress:
            print(json.dumps(
                {"ok": False, "error": "no_onion_address", "suggestions": []}))
            return 0

        result = self._registrar().register(name, onionaddress)
        if result.ok:
            print(json.dumps({
                "ok": True,
                "name": name,
                "address": onionaddress,
                "url": f"http://{onionaddress}/",
            }))
            return 0

        print(json.dumps({
            "ok": False,
            "error": result.reason or result.status,
            "suggestions": result.suggestions or [],
        }))
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

    p_backup = sub.add_parser("backup", help="Create a backup")
    p_backup.add_argument("password", help="WP admin password (also used to encrypt the zip)")
    p_backup.add_argument("output", nargs="?", help="Output path (default: ~/OnionPress/backups/)")
    p_backup.add_argument("--user", help="WP admin username (default: auto-resolve)")

    p_restore = sub.add_parser("restore", help="Restore from backup")
    p_restore.add_argument("password", help="Backup encryption password")
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

    # Headless onionname registry — an external app drives these and
    # parses one JSON line from stdout. See OnionPressCLI.cmd_onionname_*.
    p_name = sub.add_parser(
        "onionname",
        help="Onionname registry ops for driving by an external app "
             "(one JSON line on stdout)",
    )
    name_sub = p_name.add_subparsers(dest="name_command")
    name_sub.add_parser(
        "suggest", help='Print an available onionname: {"name": "…"}')
    p_name_check = name_sub.add_parser(
        "check",
        help='Check availability: {"available", "reason", "suggestions"}')
    p_name_check.add_argument("name", help="Onionname to check")
    p_name_register = name_sub.add_parser(
        "register",
        help='Register for this site: {"ok", "name", "address", "url"}')
    p_name_register.add_argument("name", help="Onionname to register")

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
    p_ppi.add_argument(
        "--apache-conf-dir",
        help="Source directory containing onionpress-static-site.conf, "
             "injected at runtime for static-first serving "
             "(optional; skipped when omitted)")

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

    p_essc = sub.add_parser(
        "ensure-static-site-conf",
        help="Restore the static-first Apache conf if a container recreate "
             "dropped it (cheap no-op when already present)",
    )
    p_essc.add_argument(
        "--apache-conf-dir", required=True,
        help="Source directory containing onionpress-static-site.conf")

    p_eui = sub.add_parser(
        "ensure-uploads-ini",
        help="Restore the PHP limits overlay if a container recreate "
             "dropped it (cheap no-op when already present)",
    )
    p_eui.add_argument(
        "--conf-dir", required=True,
        help="Source directory containing onionpress-uploads.ini")

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

    args = parser.parse_args(argv)

    if not args.command:
        args.command = "start"

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
    elif args.command == "backup":
        return cli.cmd_backup(args.password, args.output, args.user)
    elif args.command == "restore":
        return cli.cmd_restore(args.password, args.backup_file)
    elif args.command == "import-backup-artifacts":
        return cli.cmd_import_backup_artifacts(args.staging)
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
    elif args.command == "onionname":
        nc = getattr(args, "name_command", None)
        if nc == "suggest":
            return cli.cmd_onionname_suggest()
        elif nc == "check":
            return cli.cmd_onionname_check(args.name)
        elif nc == "register":
            return cli.cmd_onionname_register(args.name)
        else:
            p_name.print_help()
            return 1
    elif args.command == "provision-post-install":
        from . import multisite
        return multisite.provision_post_install(
            themes_dir=args.themes_dir,
            plugins_dir=args.plugins_dir,
            conf_dir=args.apache_conf_dir,
            log_func=print,
        )
    elif args.command == "ensure-static-site-conf":
        from . import multisite
        # Deliberately always 0: this runs on the launcher's fast
        # already-running path, where a missing container or a docker
        # hiccup must not turn a healthy `start` into a reported failure.
        multisite.ensure_static_site_conf(
            conf_dir=args.apache_conf_dir,
            log_func=print,
        )
        return 0
    elif args.command == "ensure-uploads-ini":
        from . import multisite
        # Deliberately always 0, for the same reason as
        # ensure-static-site-conf above: this runs on the launcher's fast
        # already-running path.
        multisite.ensure_uploads_ini(
            conf_dir=args.conf_dir,
            log_func=print,
        )
        return 0
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
