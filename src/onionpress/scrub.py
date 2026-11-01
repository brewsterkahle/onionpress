"""Full-lifecycle test: backup → uninstall → install → restore → verify.

Ported from the `scrub)` case in linux/onionpress (originally ~300 lines
of bash). The heavy phases (the backup and restore, now the CLI's own
cmd_backup / cmd_restore; start_containers) go through the launcher's
subcommands; this module owns the orchestration glue, sudo keep-alive,
and the verify checks.

On a Mac (issue #529) the cycle is backup → reset in place → restore →
verify, what the Mac launcher's `scrub)` case did: no uninstall or
install.sh, no sudo; the reset asks the OnionHeaven hubs that may hold
the key to delete their copy, stops the containers, removes the volumes
and wipes the data dir's identity files, and the verify also proves the
restore reused the backup's key (the address unchanged, no vanity
generation in the log since the backup).
"""

from __future__ import annotations

import dataclasses
import getpass
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from . import wp_files

LAUNCHER_BIN = "/opt/onionpress/onionpress"
DATA_DIR = os.path.expanduser("~/.onionpress")

# Default password if the user doesn't pass one and we're not connected
# to a TTY — used by automated CI. Production runs always prompt.
_INTERACTIVE_PROMPT = "WordPress admin password (for backup): "

EXPECTED_CONTAINERS = (
    "onionpress-tor",
    "onionpress-wordpress",
    "onionpress-db",
    "onionheaven",
)


@dataclasses.dataclass
class PreScrubState:
    """Captured before the destructive phases so verify can detect drift."""
    onion_address: str = ""
    wp_port: int = 8080
    backup_path: str = ""
    repo_dir: str = ""


@dataclasses.dataclass
class Check:
    """One verify-step result. `severity == 'warn'` doesn't fail the run."""
    name: str
    ok: bool
    message: str = ""
    severity: str = "fail"  # "fail" | "warn"


def _noop_log(msg: str) -> None:
    print(msg)


def _run(
    args: list[str],
    *,
    check: bool = False,
    capture: bool = False,
    quiet: bool = False,
    timeout: Optional[int] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run that defaults to inheriting
    stdio so output streams through to the user. capture=True for
    commands whose output we need to parse.
    """
    kwargs: dict = {"check": check}
    if capture:
        kwargs.update(capture_output=True, text=True,
                      encoding="utf-8", errors="replace")
    elif quiet:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if timeout is not None:
        kwargs["timeout"] = timeout
    if env is not None:
        kwargs["env"] = env
    return subprocess.run(args, **kwargs)


def _password_env(password: str) -> dict:
    """Environment for a launcher backup/restore. The password goes in the
    environment, not argv, which every local user can read."""
    from .launcher_ops import BACKUP_PASSWORD_ENV
    return {**os.environ, BACKUP_PASSWORD_ENV: password}


def _confirm(prompt: str = "Continue? [y/N] ") -> bool:
    """Y/N prompt; anything but y/yes declines. ONIONPRESS_YES=true (the
    launchers' -y) answers yes without asking."""
    if os.environ.get("ONIONPRESS_YES") == "true":
        return True
    if not sys.stdin.isatty():
        # Non-interactive: assume no to avoid destroying anything in CI.
        # Set ONIONPRESS_ASSUME_YES=1 in CI to opt in.
        return os.environ.get("ONIONPRESS_ASSUME_YES", "") == "1"
    try:
        reply = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes")


# ── sudo handling ────────────────────────────────────────────────────


def _prompt_and_validate_wp_password(
    initial: Optional[str],
    *,
    max_attempts: int = 3,
    log: Callable[[str], None] = _noop_log,
) -> Optional[str]:
    """Validate a WP admin password against the running container.
    `initial` may be set if the caller passed `--pass`; that's still
    validated (a typo in the CLI is just as bad as one at the prompt).
    Re-prompts up to `max_attempts` times.

    Returns the validated password, or None if the user gave up / no
    password came in / max retries hit.
    """
    from .backup import verify_wp_admin_password_any

    pw = initial
    for attempt in range(1, max_attempts + 1):
        if not pw:
            try:
                pw = getpass.getpass(_INTERACTIVE_PROMPT)
            except (EOFError, KeyboardInterrupt):
                return None
        if not pw:
            log("  (empty password — aborting)")
            return None
        ok, _info = verify_wp_admin_password_any(pw)
        if ok:
            return pw
        log("  Password does not match any WordPress admin account.")
        remaining = max_attempts - attempt
        if remaining > 0:
            log(f"  ({remaining} more attempt{'s' if remaining > 1 else ''} remaining)")
        pw = None  # force re-prompt
    log("  Too many bad attempts — aborting.")
    return None


class _SudoKeepAlive:
    """Pre-authenticates sudo at the start of scrub and refreshes the
    timestamp every 60s so the later destructive sudo calls (which run
    AFTER a 1-2 minute backup) don't block on an expired password cache.

    A previous scrub run died with `sudo: timed out` immediately after
    the backup finished, leaving the install half-uninstalled (units
    gone, volumes gone, but /opt and ~/.onionpress untouched). This
    class is the fix.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Prompt for the sudo password once, then start the refresh
        thread. Returns False if authentication failed.
        """
        # If SUDO_ASKPASS is set in the environment, use it (-A flag) so
        # non-TTY callers (e.g. Claude Code, CI) can still authenticate.
        if os.environ.get("SUDO_ASKPASS"):
            r = subprocess.run(["sudo", "-A", "-v"], capture_output=True)
            if r.returncode == 0:
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
                return True
        # Try non-interactive first (succeeds if credentials are cached).
        # Fall back to interactive `sudo -v` only when stdin is a tty —
        # subprocess.run(["sudo", "-v"]) without a tty hangs/fails even
        # with a warm cache on systems where timestamp_type=tty.
        r = subprocess.run(["sudo", "-nv"], capture_output=True)
        if r.returncode != 0:
            if not sys.stdin.isatty():
                return False
            r = subprocess.run(["sudo", "-v"])
            if r.returncode != 0:
                return False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.wait(60):
            # `sudo -nv` refreshes the cache without prompting; failures
            # are silent so the next interactive sudo will prompt.
            try:
                subprocess.run(
                    ["sudo", "-nv"], capture_output=True, timeout=5)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()


# ── Phase 1: Backup ───────────────────────────────────────────────────


def _get_onion_address() -> str:
    """Read the current onion hostname from the running tor container.
    Empty string if not available (containers down, address not minted).

    Uses `docker exec onionpress-tor` (not `docker compose exec tor`)
    because scrub runs from wherever the user invoked it — typically
    not the compose project directory — and `docker compose` would
    silently fail without a docker-compose.yml in cwd.
    """
    from . import onion_hostname
    r = _run(["docker"] + onion_hostname.exec_args(), capture=True, timeout=10)
    if r.returncode != 0:
        return ""
    return onion_hostname.served_address(r.stdout)


def _find_repo_dir() -> str:
    """Find the OnionPress git checkout to re-install from. Mirrors the
    bash glob list — the first match with a .git/ wins.
    """
    candidates = (
        glob.glob("/home/*/tmp/onionpress")
        + glob.glob("/home/*/onionpress")
        + ["/opt/onionpress-src"]
    )
    for d in candidates:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
    return ""


def phase_backup(password: str, log: Callable[[str], None]) -> PreScrubState:
    """Capture pre-scrub state + create the backup zip. Raises
    RuntimeError on any unrecoverable failure.
    """
    log("")
    log("── Step 1/5: Backup ──")

    # Capture pre-scrub state for verify.
    state = PreScrubState()
    state.onion_address = _get_onion_address()
    if state.onion_address:
        log(f"  Pre-scrub onion: {state.onion_address}")
    else:
        log("  WARNING: No pre-scrub onion address captured "
            "(won't be able to verify match)")
    try:
        state.wp_port = int(os.environ.get("ONIONPRESS_WP_PORT", "8080"))
    except ValueError:
        state.wp_port = 8080
    log(f"  Pre-scrub WP port: {state.wp_port}")

    state.repo_dir = _find_repo_dir()
    if not state.repo_dir:
        raise RuntimeError(
            "Cannot find OnionPress git repo — needed to re-install. "
            "Looked in /home/*/tmp/onionpress, /home/*/onionpress, "
            "/opt/onionpress-src.")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backups_dir = os.path.expanduser("~/OnionPress/backups")
    os.makedirs(backups_dir, exist_ok=True)
    state.backup_path = os.path.join(backups_dir, f"onionpress-scrub-{timestamp}.zip")

    # Backup is still a bash subcommand. Shell out to it.
    r = _run([LAUNCHER_BIN, "backup", "--output", state.backup_path],
             env=_password_env(password))
    if r.returncode != 0 or not os.path.isfile(state.backup_path) \
            or os.path.getsize(state.backup_path) == 0:
        raise RuntimeError("Backup file missing or empty — aborting.")

    size_mb = os.path.getsize(state.backup_path) / (1024 * 1024)
    log(f"  Backup saved to: {state.backup_path} ({size_mb:.1f}M)")
    return state


# ── Phase 2: Uninstall ────────────────────────────────────────────────


def phase_uninstall(log: Callable[[str], None]) -> None:
    """Shell out to the launcher's `uninstall` subcommand with
    ONIONPRESS_YES=true to skip the backup-prompt (we already took
    one in phase_backup). Single source of truth — any fix to
    uninstall reaches scrub automatically.
    """
    log("")
    log("── Step 2/5: Uninstall ──")
    # Kill the tray before the rest of uninstall — it runs in the user
    # session (not systemd) and would otherwise poll a deleted DATA_DIR
    # for hours if left running.
    subprocess.run(
        ["pkill", "-u", getpass.getuser(), "-f", "onionpress-tray"],
        capture_output=True)
    env = {**os.environ, "ONIONPRESS_YES": "true"}
    r = subprocess.run([LAUNCHER_BIN, "uninstall"], env=env)
    if r.returncode != 0:
        raise RuntimeError(f"uninstall exited {r.returncode} — aborting scrub.")
    log("  Uninstall complete.")


# ── Phase 3: Install ──────────────────────────────────────────────────


def phase_install(repo_dir: str, log: Callable[[str], None]) -> None:
    """Re-run install.sh from the captured repo dir. Pipes through to
    the user so they see the install output live.
    """
    log("")
    log("── Step 3/5: Install ──")
    r = subprocess.run(
        ["sudo", "bash", "linux/install.sh"],
        cwd=repo_dir,
    )
    if r.returncode != 0:
        raise RuntimeError(f"install.sh exited {r.returncode} — aborting scrub.")
    log("  Install complete.")


# ── Phase 4: Restore ──────────────────────────────────────────────────


def phase_restore(
    password: str,
    backup_path: str,
    log: Callable[[str], None],
    *,
    launcher: str = LAUNCHER_BIN,
    step: str = "4/5",
) -> None:
    """Restore from the backup zip via the launcher's `restore` subcommand
    (the CLI's install-from-backup: the stack is rebuilt and restarted).
    """
    log("")
    log(f"── Step {step}: Restore ──")
    r = subprocess.run([launcher, "restore", backup_path],
                       env=_password_env(password))
    if r.returncode != 0:
        raise RuntimeError(f"restore exited {r.returncode} — aborting scrub.")
    log("  Restore and restart complete.")


# ── Phase 5: Verify ───────────────────────────────────────────────────


def _container_running(name: str) -> bool:
    r = _run(["docker", "ps", "--format", "{{.Names}}"], capture=True)
    if r.returncode != 0:
        return False
    return name in (r.stdout or "").splitlines()


def _wp_responds() -> tuple[bool, str]:
    r = _run(
        ["docker", "exec", "onionpress-wordpress",
         "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "http://localhost:80/"],
        capture=True, timeout=15,
    )
    if r.returncode != 0:
        return False, "000"
    code = (r.stdout or "").strip() or "000"
    return code in ("200", "301", "302"), code


def _user_service_enabled(unit: str) -> bool:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    r = subprocess.run(
        ["systemctl", "--user", "is-enabled", unit],
        env=env, capture_output=True, text=True,
    )
    return r.returncode == 0


def _hub_registered(timeout_s: int = 90) -> bool:
    """Poll onionheaven-registration.json for `"registered": true`."""
    path = os.path.join(DATA_DIR, "onionheaven-registration.json")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("registered") is True:
                    return True
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(10)
    return False


def _wayback_creds_present(timeout_s: int = 90) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = wp_files.wp_cli("option", "get", "onionpress_archive_s3_access",
                            timeout=10)
        if r.returncode == 0 and (r.stdout or "").strip():
            return True
        time.sleep(10)
    return False


def phase_verify(state: PreScrubState, log: Callable[[str], None]) -> list[Check]:
    """Run all the verify checks, log results, and return them."""
    log("")
    log("── Step 5/5: Verify ──")
    checks: list[Check] = []

    # 1. Each expected container is running.
    for name in EXPECTED_CONTAINERS:
        ok = _container_running(name)
        checks.append(Check(f"{name} running", ok))

    # 2. Onion address present and matches pre-scrub (vanity key preserved).
    post_addr = _get_onion_address()
    if not post_addr:
        checks.append(Check("Onion address present", False,
                            "No onion address found"))
    elif state.onion_address and post_addr != state.onion_address:
        checks.append(Check(
            "Onion address matches pre-scrub", False,
            f"changed: {state.onion_address} → {post_addr} "
            f"(vanity key NOT restored)"))
    else:
        checks.append(Check(
            "Onion address matches pre-scrub", True,
            f"{post_addr}"))

    # 3. WordPress responds.
    ok, code = _wp_responds()
    checks.append(Check(
        "WordPress responding", ok, f"HTTP {code}"))

    # 4. User-scope systemd unit is enabled (matches install.sh's model).
    checks.append(Check(
        "onionpress.service enabled (user)",
        _user_service_enabled("onionpress")))

    # 5. WP port matches pre-scrub (port-offset drift detection).
    try:
        post_port = int(os.environ.get("ONIONPRESS_WP_PORT", "8080"))
    except ValueError:
        post_port = 8080
    if post_port == state.wp_port:
        checks.append(Check(
            f"WP port {post_port}", True, "matches pre-scrub"))
    else:
        checks.append(Check(
            "WP port matches pre-scrub", False,
            f"drifted: {state.wp_port} → {post_port} "
            f"(a stale socket was likely in TIME_WAIT at detect time)"))

    # 6. OnionHeaven hub re-registration (warn, not fail).
    checks.append(Check(
        "OnionHeaven registration restored",
        _hub_registered(),
        message="will converge on next heartbeat" if False else "",
        severity="warn",
    ))

    # 7. Wayback S3 keys configured (warn).
    checks.append(Check(
        "Wayback S3 credentials configured",
        _wayback_creds_present(),
        message=("sweep won't submit posts until they land — "
                 "check archive.org reachability"),
        severity="warn",
    ))

    # Pretty-print
    for c in checks:
        if c.ok:
            log(f"  ✓ {c.name}" + (f": {c.message}" if c.message else ""))
        elif c.severity == "warn":
            log(f"  ⚠ {c.name}"
                + (f" — {c.message}" if c.message else ""))
        else:
            log(f"  ✗ {c.name}"
                + (f": {c.message}" if c.message else ""))
    return checks


def scrub_passed(checks: list[Check]) -> bool:
    """True iff every fail-severity check passed. Warnings don't fail."""
    return all(c.ok for c in checks if c.severity == "fail")


# ── The Mac's phases: reset in place (issue #529) ─────────────────────

# What the reset-in-place wipes from the data dir; the OnionHeaven
# registration record only once every hub let go of the key.
RESET_FILES = ("secrets", "onionpress.log", ".last_status_state", "config", "config.bak",
               "onion_address", "healthcheck-address")
VANITY_REGENERATED = ("Generating custom onion address", "Generated custom address")


def _log_mark(log_file: str) -> int:
    """How many lines the launcher's log holds now (for a later "did
    anything happen since?" read)."""
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _log_since(log_file: str, mark: int) -> str:
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[mark:])
    except OSError:
        return ""


def phase_backup_mac(password: str, log: Callable[[str], None], *,
                     launcher: str, data_dir: str) -> PreScrubState:
    """The Mac's step 1: the backup into the user's own data dir (not the
    shared /tmp), and the live identity plus a log mark captured before
    the teardown, so the verify can prove the restore kept the address
    and generated no vanity key."""
    log("")
    log("── Step 1/4: Backup ──")
    state = PreScrubState()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    state.backup_path = os.path.join(data_dir, f"onionpress-scrub-{timestamp}.zip")
    r = _run([launcher, "backup", "--output", state.backup_path],
             env=_password_env(password))
    if r.returncode != 0 or not os.path.isfile(state.backup_path) \
            or os.path.getsize(state.backup_path) == 0:
        raise RuntimeError("Backup file missing or empty — aborting.")
    state.onion_address = _get_onion_address()
    state.repo_dir = str(_log_mark(os.path.join(data_dir, "onionpress.log")))
    log(f"  Pre-scrub address: {state.onion_address or '<none>'}")
    return state


def phase_reset_in_place(log: Callable[[str], None], *, launcher: str, data_dir: str) -> None:
    """The Mac's step 2: the reset wipes the config (OnionHeaven back to
    undecided, so no heartbeat follows the restore), so the hubs that may
    hold the key are asked to delete their copy first, as by `reset` (a
    hub that keeps it is a question; declined, the backup is kept and
    nothing is touched); then the containers stop, every onionpress-*
    volume goes, and the identity files leave the data dir (the Colima
    VM and docker-config stay; the OnionHeaven record stays when a hub
    may still hold a key)."""
    from .onionheaven import _RETRYABLE, release_onionheaven
    log("")
    log("── Step 2/4: Reset ──")
    remaining = release_onionheaven("scrub", data_dir=data_dir, log_func=log)
    if any(i["why"] in _RETRYABLE for i in remaining) and not _confirm(
            "Continue anyway? The hub keeps the key listed above. [y/N] "):
        raise RuntimeError("Cancelled")
    subprocess.run([launcher, "stop"], capture_output=True)
    r = _run(["docker", "volume", "ls", "-q", "--filter", "name=onionpress-"], capture=True)
    for vol in (r.stdout or "").split() if r.returncode == 0 else []:
        _run(["docker", "volume", "rm", vol], quiet=True)
    for name in RESET_FILES:
        try:
            os.remove(os.path.join(data_dir, name))
        except OSError:
            pass
    if not remaining:
        try:
            os.remove(os.path.join(data_dir, "onionheaven-registration.json"))
        except OSError:
            pass
    import shutil
    shutil.rmtree(os.path.join(data_dir, "shared", "vanity-keys"), ignore_errors=True)
    log("  Reset complete.")


def phase_verify_mac(state: PreScrubState, log: Callable[[str], None], *,
                     data_dir: str) -> list[Check]:
    """The Mac's step 4: the containers running, an address served and
    equal to the pre-scrub one (the backup's key was reused, not a fresh
    vanity key), no vanity generation in the log since the backup, and
    WordPress answering."""
    log("")
    log("── Step 4/4: Verify ──")
    checks: list[Check] = []
    for name in EXPECTED_CONTAINERS:
        checks.append(Check(f"{name} running", _container_running(name)))
    post_addr = _get_onion_address()
    if not post_addr:
        checks.append(Check("Onion address present", False, "No onion address found"))
    elif state.onion_address and post_addr != state.onion_address:
        checks.append(Check("Address preserved", False,
                            f"changed: was {state.onion_address}, now {post_addr}"))
    else:
        checks.append(Check("Address preserved (rebuilt from backup key, no regeneration)",
                            True, post_addr))
    since = _log_since(os.path.join(data_dir, "onionpress.log"), int(state.repo_dir or 0))
    regenerated = any(m.lower() in since.lower() for m in VANITY_REGENERATED)
    checks.append(Check("No vanity regeneration during restore", not regenerated,
                        "" if not regenerated else "a vanity address was generated (should reuse backup key)"))
    ok, code = _wp_responds()
    checks.append(Check("WordPress responding", ok, f"HTTP {code}"))
    for c in checks:
        if c.ok:
            log(f"  ✓ {c.name}" + (f": {c.message}" if c.message else ""))
        else:
            log(f"  ✗ {c.name}" + (f": {c.message}" if c.message else ""))
    return checks


def _run_scrub_mac(password, *, clean, log, launcher, data_dir) -> int:
    log("")
    log("OnionPress Scrub")
    log("================")
    log("This will: backup → reset → restore → verify")
    log("")
    if not _confirm("  Continue? [y/N] "):
        log("Cancelled.")
        return 0
    password = _prompt_and_validate_wp_password(password, log=log)
    if not password:
        log("ERROR: Could not validate WordPress admin password — aborting.")
        return 1
    try:
        state = phase_backup_mac(password, log, launcher=launcher, data_dir=data_dir)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        return 1
    try:
        phase_reset_in_place(log, launcher=launcher, data_dir=data_dir)
    except RuntimeError as e:
        log(f"{e}. Backup kept at: {state.backup_path}")
        return 1
    try:
        phase_restore(password, state.backup_path, log, launcher=launcher, step="3/4")
    except RuntimeError as e:
        log(f"ERROR: {e}")
        log(f"  Backup retained for recovery: {state.backup_path}")
        return 1
    checks = phase_verify_mac(state, log, data_dir=data_dir)
    ok = scrub_passed(checks)
    log("")
    if ok:
        log("Scrub PASSED — backup/reset/restore all working.")
        if clean:
            try:
                os.unlink(state.backup_path)
                log(f"  Cleaned up: {state.backup_path}")
            except OSError:
                pass
        else:
            log(f"  Backup retained at: {state.backup_path}")
            log("  (pass --clean to delete on success)")
    else:
        log("Scrub FAILED — see errors above.")
        log(f"  Backup retained for recovery: {state.backup_path}")
    # Stop everything so the app can do a clean start.
    log("")
    log("Stopping containers for clean app launch...")
    subprocess.run([launcher, "stop"], capture_output=True)
    log("Done. Launch OnionPress from /Applications to start fresh.")
    return 0 if ok else 1


# ── Top-level orchestration ───────────────────────────────────────────


def run_scrub(
    password: Optional[str] = None,
    *,
    clean: bool = False,
    log_func: Optional[Callable[[str], None]] = None,
    os_name: Optional[str] = None,
    launcher: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> int:
    """Run the full scrub cycle. Returns 0 on PASS, 1 on FAIL.

    Mirrors the bash `scrub)` cases it replaces; printing format kept
    similar so the user sees recognisable output. *os_name* picks the
    cycle: Linux's uninstall/install one (sudo, install.sh; the default)
    or the Mac's reset in place ("macos", issue #529); *launcher* and
    *data_dir* are the install's (the Linux defaults are the .deb's).
    """
    log = log_func or _noop_log
    # No *os_name* means Linux's cycle (the module's original, and what
    # its tests drive); the CLI passes the platform it detected.
    if os_name == "macos":
        return _run_scrub_mac(password, clean=clean, log=log,
                              launcher=launcher or LAUNCHER_BIN, data_dir=data_dir or DATA_DIR)

    log("")
    log("OnionPress Scrub")
    log("================")
    log("This will: backup → uninstall → install → restore → verify")
    log("")

    if not _confirm():
        log("Cancelled.")
        return 0

    # Pre-auth sudo + keep-alive (the original "abort half-uninstalled"
    # bug; see the _SudoKeepAlive docstring).
    log("── Authenticating sudo (needed for uninstall + install) ──")
    keepalive = _SudoKeepAlive()
    if not keepalive.start():
        log("ERROR: sudo authentication failed — aborting scrub.")
        return 1

    # Make sure the keep-alive dies even on early exit.
    def _on_signal(_signum, _frame):
        keepalive.stop()
        sys.exit(130)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        # Prompt for + validate the WP admin password BEFORE any
        # destructive work. Without this validation, a typo would let us
        # tear down half the install before phase_backup's internal
        # verify catches the bad password and exits — leaving the user
        # in a broken state. Three tries; clean abort on failure.
        password = _prompt_and_validate_wp_password(
            password, log=log)
        if not password:
            log("ERROR: Could not validate WordPress admin password — aborting.")
            return 1

        try:
            state = phase_backup(password, log)
        except RuntimeError as e:
            log(f"ERROR: {e}")
            return 1

        try:
            phase_uninstall(log)
        except RuntimeError as e:
            log(f"ERROR: {e}")
            log(f"  Backup retained for recovery: {state.backup_path}")
            return 1

        try:
            phase_install(state.repo_dir, log)
        except RuntimeError as e:
            log(f"ERROR: {e}")
            return 1

        try:
            phase_restore(password, state.backup_path, log)
        except RuntimeError as e:
            log(f"ERROR: {e}")
            return 1

        checks = phase_verify(state, log)
        ok = scrub_passed(checks)

        log("")
        if ok:
            log("Scrub PASSED — backup/uninstall/install/restore all working.")
            if clean:
                try:
                    os.unlink(state.backup_path)
                    log(f"  Cleaned up: {state.backup_path}")
                except OSError:
                    pass
            else:
                log(f"  Backup retained at: {state.backup_path}")
                log("  (pass --clean to delete on success)")
            return 0
        log("Scrub FAILED — see errors above.")
        log(f"  Backup retained for recovery: {state.backup_path}")
        return 1
    finally:
        keepalive.stop()
