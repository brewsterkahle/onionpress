"""Cross-platform launcher operations shared between Mac and Linux.

Primitives used by the Linux bash launcher (`linux/onionpress` via
`python3 -m onionpress.cli`), the Linux tray app (`linux/onionpress-tray`,
direct import), and — eventually — the Mac menubar app.

Each function takes explicit paths and doesn't read environment or globals.
Platform-aware glue (Tor Browser detection, which container runtime) lives
in callers.

Operations:
- open_in_browser(url, prefer_tor)         — launch via xdg-open / open / torbrowser-launcher
- login_target(base_url) / open_login(...) — auto-login without the token in any argv
- tor_image_has_mkp224o(image)             — verify the tor image bundles mkp224o
- generate_vanity_in_container(...)        — docker run mkp224o, write to vanity-keys/
- get_admin_password(data_dir)             — read ~/.onionpress/wp-admin-password
- run_launcher_restore(...)                — `onionpress restore`, and why it failed
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Optional

from . import auto_login, key_manager


# Pinned to digest. The literal below is propagated from build/image-pins.env
# by build/refresh-image-digests.sh, which writes every consumer at once;
# tests/test_image_pins.py fails if any of them drift apart.
#
# ONIONPRESS_TOR_IMAGE overrides it, so vanity-key generation runs against the
# same image the rest of a locally built stack uses.
DEFAULT_TOR_IMAGE = os.environ.get(
    "ONIONPRESS_TOR_IMAGE",
    "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:79b18a6190e3ba8d3c261fd2782ee0a5871cbfebc663cbe12e6ade918e10aa4f",
)

# How the menubar, the tray, scrub and the launchers hand a backup password
# to `onionpress backup|restore` and `python -m onionpress.cli backup|restore`:
# in the environment, never in argv. Other local users can read any
# process's command line (ps, /proc/<pid>/cmdline), but not its environment.
# The bash launchers and linux/onionpress-tray spell the name out; a test
# keeps them in step.
BACKUP_PASSWORD_ENV = "ONIONPRESS_BACKUP_PASSWORD"


def _tor_browser_lock_paths() -> list:
    """Return the standard torbrowser-launcher lock-file paths under the
    user's home, regardless of which actually exist right now."""
    base = os.path.expanduser(
        "~/.local/share/torbrowser/tbb/x86_64/tor-browser/Browser")
    return [
        os.path.join(base, "TorBrowser/Data/Browser/profile.default/lock"),
        os.path.join(base, "TorBrowser/Data/Browser/profile.default/.parentlock"),
        os.path.join(base, "TorBrowser/Data/Tor/lock"),
        os.path.join(base, "TorBrowser/UpdateInfo/.parentlock"),
    ]


def cleanup_stale_tor_browser_locks() -> bool:
    """If lock files exist but no Tor Browser is running, remove them.

    Firefox's `lock` symlink encodes the holding PID as `host:+PID`. If
    that PID is gone (browser crashed / was force-killed) the lock is
    stale, and torbrowser-launcher's next run will refuse to start with
    a blocking "Tor Browser is already running" dialog. We probe the
    lock target's PID; if it's dead, all locks under the profile are
    removed so the next launch is clean.

    Returns True if we removed anything (caller can log it).
    """
    # If a live Tor Browser is running, locks are valid — leave them.
    if _tor_browser_running():
        return False

    # Check the profile.default/lock symlink target — that's the
    # authoritative "is the previous process still alive?" signal.
    base = os.path.expanduser(
        "~/.local/share/torbrowser/tbb/x86_64/tor-browser/Browser")
    lock_path = os.path.join(
        base, "TorBrowser/Data/Browser/profile.default/lock")
    if os.path.islink(lock_path):
        try:
            target = os.readlink(lock_path)  # e.g. "127.0.0.1:+1789690"
            pid_str = target.rsplit("+", 1)[-1]
            pid = int(pid_str)
            if os.path.exists(f"/proc/{pid}"):
                # Process is still alive — odd that _tor_browser_running()
                # didn't see it, but defer to that check and don't clean.
                return False
        except (OSError, ValueError, IndexError):
            pass

    removed = False
    for path in _tor_browser_lock_paths():
        try:
            os.unlink(path)
            removed = True
        except OSError:
            pass
    return removed


def _tor_browser_running() -> Optional[str]:
    """If Tor Browser is already running, return the path to its
    firefox.real binary. Used so we open URLs in the existing instance
    instead of letting torbrowser-launcher pop an "already running"
    dialog at the user.

    The previous implementation matched cmdline with `pgrep -af
    tor-browser/Browser/firefox.real`, but Tor Browser invokes its
    binary as `./firefox.real` (relative path) from cwd=…/tor-browser/
    Browser/. So pgrep never matched and the function always said "not
    running" — even with multiple live instances. The tray would then
    launch yet another, locks stacked up, and torbrowser-launcher
    started popping its "already running" dialog. Fix: enumerate
    processes by basename, then confirm via /proc/<pid>/exe (the
    resolved binary path) that they're actually the Tor Browser build,
    not some unrelated firefox.real.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "firefox.real"],
            capture_output=True, text=True, timeout=5,
        )
        for pid in proc.stdout.split():
            try:
                bin_path = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                continue
            if "tor-browser" in bin_path and bin_path.endswith("/firefox.real"):
                return bin_path
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def _launch_default_web_browser(uri: str) -> bool:
    """Open *uri* (a file:// page) in the default web browser via Gio, True
    on success. xdg-open would pick the handler for text/html files, which
    can be an editor; the web browser is the handler for http."""
    try:
        from gi.repository import Gio  # python3-gi: a tray dependency
        app = Gio.AppInfo.get_default_for_uri_scheme("http")
        return bool(app is not None and app.launch_uris([uri], None))
    except Exception:
        return False


def open_in_browser(url: str, *, prefer_tor: bool = False) -> None:
    """Spawn a browser at `url`. Detached — does not wait.

    `url` lands in a command line, so it must never carry an auto-login
    token: pass login_target()'s file:// URI instead.
    """
    cmd: list[str]
    if prefer_tor:
        running = _tor_browser_running()
        if running:
            # Existing Tor Browser session — open the URL as a new tab in
            # it rather than relaunching (which would trigger torbrowser-
            # launcher's "already running" dialog). Must go through the
            # `start-tor-browser` wrapper (sibling of firefox.real), not
            # firefox.real directly: the wrapper sets HOME/cwd/env so
            # Firefox's remoting finds the running profile. Without it,
            # firefox.real spawns a fresh instance, hits the profile
            # lock, and pops the "already running" dialog at the user.
            wrapper = os.path.join(os.path.dirname(running),
                                   "start-tor-browser")
            if os.path.exists(wrapper) and os.access(wrapper, os.X_OK):
                cmd = [wrapper, "-new-tab", url]
            else:
                # Fall back to direct firefox.real (older builds may not
                # ship the wrapper). Still better than torbrowser-launcher
                # since the live instance check above already passed.
                cmd = [running, "-new-tab", url]
        elif shutil.which("torbrowser-launcher"):
            cmd = ["torbrowser-launcher", url]
        elif sys.platform == "darwin":
            cmd = ["open", url]
        else:
            cmd = ["xdg-open", url]
    elif sys.platform == "darwin":
        cmd = ["open", url]
    else:
        cmd = ["xdg-open", url]

    if (cmd[0] == "xdg-open" and url.startswith("file://")
            and _launch_default_web_browser(url)):
        return
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except (OSError, FileNotFoundError) as e:
        raise RuntimeError(f"failed to launch browser ({cmd[0]}): {e}") from e


def tor_image_has_mkp224o(image: str = DEFAULT_TOR_IMAGE) -> bool:
    """Return True iff the tor container image is pulled AND contains mkp224o.

    Verifies via a `docker run --rm --entrypoint test` probe so we don't
    fall through to attempting vanity generation on an older image that
    predates the mkp224o build stage.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, timeout=15,
    )
    if inspect.returncode != 0:
        return False
    # A file test needs no capability, and no network.
    probe = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
         "--entrypoint", "test", image,
         "-x", "/usr/local/bin/mkp224o"],
        capture_output=True, timeout=30,
    )
    return probe.returncode == 0


def generate_vanity_in_container(
    prefix: str,
    vanity_dir: str,
    *,
    image: str = DEFAULT_TOR_IMAGE,
    jobs: Optional[int] = None,
    log_func=None,
) -> Optional[str]:
    """Run mkp224o inside the tor container; return the generated .onion address.

    Writes the key bundle into `vanity_dir/<addr>.onion/`. Returns the
    address (basename of the dir, including `.onion`) on success, None on
    failure. Caller is responsible for ensuring the dir is empty first if
    a fresh key is required.
    """
    # Single source of truth — see config.validate_address_prefix(). This
    # used to be a bare `2 <= len(prefix) <= 6`, which allowed a 6-character
    # prefix the macOS launcher rejects and let a non-base32 prefix (0/1/8/9)
    # through to mkp224o, where no address can ever match and the search never
    # terminates.
    # Empty means "use the default" to UI callers, and validate_address_prefix()
    # accepts it for that reason. A library function about to invoke mkp224o
    # has no default to fall back on. With an empty filter mkp224o reports
    # "0 filters" and exits 0 having generated nothing — and the
    # `startswith(prefix)` scan below then matches EVERY existing key directory,
    # returning an old address as if freshly minted.
    if not prefix:
        raise ValueError("prefix must not be empty")
    # Imported here, not at module scope: config imports this module for
    # get_running_wp_port(), so a top-level import would be circular.
    from .config import validate_address_prefix

    prefix_ok, prefix_error, _ = validate_address_prefix(prefix)
    if not prefix_ok:
        raise ValueError(prefix_error.splitlines()[0])

    os.makedirs(vanity_dir, exist_ok=True)
    if jobs is None:
        jobs = os.cpu_count() or 2

    if log_func:
        log_func(f"[STAGE] vanity Starting mkp224o for prefix '{prefix}' "
                 f"with {jobs} threads...")

    # Override the image's entrypoint so we don't trigger the tor service's
    # setup steps (which require root inside the container). We don't pass
    # --user: under rootless Docker, in-container UID 1000 maps to a subuid
    # that doesn't own the bind mount, whereas in-container root maps to
    # the host user — exactly what we want for file ownership of the keys.
    # Under rootful Docker, files end up root-owned but the next pass
    # of start_containers fixes that with a chown. There root writes into
    # the user's folder, which takes DAC_OVERRIDE (mkp224o: "could not
    # create directory ... for key output" without it); nothing else. The
    # new key needs no network, and gets none.
    cmd = [
        "docker", "run", "--rm", "--network", "none",
        "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
        "--security-opt", "no-new-privileges:true",
        "-v", f"{vanity_dir}:/out",
        "--entrypoint", "/usr/local/bin/mkp224o",
        image,
        "-n", "1", "-j", str(jobs), "-d", "/out", prefix,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if log_func and proc.stdout:
        for line in proc.stdout.splitlines():
            log_func(f"mkp224o: {line}")
    if proc.returncode != 0:
        if log_func:
            log_func(f"mkp224o failed (exit {proc.returncode}): {proc.stderr.strip()}")
        return None

    # mkp224o writes <addr>.onion/ directory under /out. Only a directory
    # named by a whole, well-formed address counts: the name is returned
    # as the new address.
    candidates = [
        d for d in os.listdir(vanity_dir)
        if d.startswith(prefix) and key_manager.is_valid_onion_address(d)
        and os.path.isdir(os.path.join(vanity_dir, d))
        and os.path.exists(os.path.join(vanity_dir, d, "hs_ed25519_secret_key"))
    ]
    if not candidates:
        if log_func:
            log_func(f"mkp224o exit 0 but no {prefix}*.onion directory in {vanity_dir}")
        return None
    addr = candidates[0]
    if log_func:
        log_func(f"[STAGE] vanity Generated address: {addr}")
    return addr


def login_url(base_url: str) -> str:
    """*base_url* with a one-time auto-login token, or *base_url* when none
    could be minted. Only for a URL that reaches the browser off any command
    line (the tray's copy-and-paste dialog); see login_target otherwise."""
    return auto_login.login_url(base_url) or base_url


def login_target(base_url: str, *, page_dir: Optional[str] = None) -> str:
    """What to put on a browser's command line to open *base_url* logged in.

    A browser on Linux takes URLs only in its argv, which every local user
    can read (/proc/<pid>/cmdline), and the token in an auto-login URL is a
    one-time admin login. So the browser gets the file:// URI of a 0600 page
    that redirects to the URL (auto_login.write_redirect_page), deleted once
    the token has expired. *base_url* itself when no token could be minted.
    """
    url = auto_login.login_url(base_url)
    if not url:
        return base_url
    try:
        path = auto_login.write_redirect_page(
            url, page_dir or auto_login.REDIRECT_DIR)
    except OSError:
        return base_url
    auto_login.remove_later(path)
    return pathlib.Path(path).as_uri()


def open_login(base_url: str) -> None:
    """Open *base_url* logged in as the admin (`onionpress dashboard`)."""
    open_in_browser(login_target(base_url))


def signal_watchdog(docker, container: str, sig: str) -> bool:
    """Send a Unix signal to the tor-watchdog inside a container.

    Used on both Mac and Linux around sleep/wake (USR1 = DEL_ONION,
    USR2 = ADD_ONION). The [t]or-watchdog bracket prevents pgrep from
    matching its own sh -c invocation.

    `docker` is an onionpress.docker.Docker instance (or anything with
    a compatible .exec(container, argv, timeout=...) method whose
    result has a truthy `.ok` on success).
    """
    result = docker.exec(
        container,
        ["sh", "-c",
         f"kill -{sig} $(pgrep -f '[t]or-watchdog') 2>/dev/null"],
        timeout=10,
    )
    return bool(getattr(result, "ok", False))


# Where the tor-watchdog looks for a host-requested descriptor republish.
# Inside the tor container's own filesystem (not the shared onionpress-data
# volume) so only something with docker exec on that container — the app —
# can request one. Level-triggered: the file sits there until the watchdog
# acts on it or discards it, so a request made while the container is
# frozen, the watchdog is in its cooldown or is being restarted by its
# supervisor is coalesced, never lost or duplicated. Must match
# REPUBLISH_REQUEST_PATH in app/Resources/docker/tor/tor-watchdog.py (a
# test enforces it).
WATCHDOG_REPUBLISH_REQUEST_PATH = "/run/tor-watchdog/republish-requested"

# The file the watchdog touches every pass of its loops (issue #313); must
# match ALIVE_PATH in tor-watchdog.py (test-enforced). Older than
# WATCHDOG_ALIVE_MAX_AGE, the watchdog is hung (its supervisor kills it at
# 300 s) and a request written now is not being read.
WATCHDOG_ALIVE_PATH = "/run/tor-watchdog/alive"
WATCHDOG_ALIVE_MAX_AGE = 120

# request_watchdog_republish() results.
REPUBLISH_REQUESTED = "requested"      # written, and a live watchdog reads it
REPUBLISH_NO_WATCHDOG = "no-watchdog"  # written, but no live watchdog to read it
REPUBLISH_FAILED = "failed"            # not written (docker exec failed)

# Exit status of the request script when the file was written but no
# watchdog is there to act on it.
_NO_WATCHDOG_EXIT = 3

# $1 reason, $2 request path, $3 alive path, $4 max alive age. The pattern
# is "python3 /[t]or-watchdog.py", not the bare name: this shell's own argv
# holds the request path, which contains "tor-watchdog". An image older
# than the alive file has none; then the process check is all there is.
_REQUEST_SCRIPT = (
    'mkdir -p "$(dirname "$2")" && printf \'%s\\n\' "$1" > "$2" || exit 1\n'
    "pgrep -f 'python3 /[t]or-watchdog.py' >/dev/null || exit 3\n"
    '[ -f "$3" ] || exit 0\n'
    'mtime=$(stat -c %Y "$3") || exit 0\n'
    '[ $(( $(date +%s) - mtime )) -le "$4" ] || exit 3\n'
)


def request_watchdog_republish(docker, container: str, reason: str) -> str:
    """Ask the tor-watchdog in `container` to force a descriptor republish.

    Writes the request file the watchdog polls (see
    WATCHDOG_REPUBLISH_REQUEST_PATH); the watchdog answers with
    DEL_ONION + ADD_ONION for every service it manages, which mints fresh
    intro points and uploads a new descriptor immediately instead of at
    Tor's next scheduled upload (up to two hours away). This is the
    outcome-verified recovery loop from issue #266: the caller has watched
    the service be unreachable through the Tor network for
    health.REPUBLISH_AFTER_UNREACHABLE_SECONDS while Tor itself looked
    healthy.

    `reason` is free text for the watchdog's log line. Returns
    REPUBLISH_REQUESTED when the file was written and a watchdog is alive
    to read it; REPUBLISH_NO_WATCHDOG when the file was written but no
    watchdog process runs or its alive file is stale (issue #313: the
    entrypoint's supervisor restarts it, and the restarted watchdog acts
    on the waiting file); REPUBLISH_FAILED when the file could not be
    written. A tor image whose watchdog predates the request file simply
    never reads it — harmless.
    """
    reason = " ".join(str(reason or "").split())[:200] or "unspecified"
    result = docker.exec(
        container,
        ["sh", "-c", _REQUEST_SCRIPT, "_", reason,
         WATCHDOG_REPUBLISH_REQUEST_PATH, WATCHDOG_ALIVE_PATH,
         str(WATCHDOG_ALIVE_MAX_AGE)],
        timeout=10,
    )
    if getattr(result, "ok", False):
        return REPUBLISH_REQUESTED
    if (getattr(result, "returncode", None) == _NO_WATCHDOG_EXIT
            and not getattr(result, "timed_out", False)):
        return REPUBLISH_NO_WATCHDOG
    return REPUBLISH_FAILED


def describe_republish_request(status: str) -> str:
    """The log text for a request_watchdog_republish() result (both hosts)."""
    if status == REPUBLISH_REQUESTED:
        return "requested descriptor republish from tor-watchdog"
    if status == REPUBLISH_NO_WATCHDOG:
        return ("republish request written, but tor-watchdog is not running "
                "or is hung; it waits for the watchdog's restart by the tor "
                "container's supervisor")
    return "republish request to tor-watchdog FAILED (docker exec)"


def get_running_wp_port(container: str = "onionpress-wordpress") -> Optional[int]:
    """Read the host port our own WordPress container actually published.

    `detect_port_offset()` binds candidate ports to find one that's free —
    correct for allocation at first launch, wrong as a runtime lookup: once
    a stack is up, its ports are bound (by the stack itself), so the bind
    test always fails and reports "in use", concluding nothing about which
    port was actually chosen. Reading `docker port` instead asks the one
    source that's authoritative once a stack exists — the mapping Docker
    already made — for both the "still on our own port" case and the "came
    up on a different offset than last time" case (stop/start races where
    the old offset's forwards are still bound when the new ones come up).

    Returns None if the container isn't running or isn't ours to read
    (multi-user machines: this only ever inspects a container name, never
    another user's — Docker containers are per-user via the docker socket
    each user's Colima VM exposes, so there is no cross-user leakage here).
    """
    try:
        result = subprocess.run(
            ["docker", "port", container, "80"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    # Expected output: "0.0.0.0:8080" or "127.0.0.1:8080" (one line per
    # bound address; take the first).
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if ":" not in first_line:
        return None
    port_str = first_line.rsplit(":", 1)[-1]
    if not port_str.isdigit():
        return None
    port = int(port_str)
    # Offsets are 8080 + k*10000, so nothing we allocate is ever below
    # 8080 — a lower published port belongs to something else entirely
    # (e.g. a hand-edited port mapping).
    if port < 8080:
        return None
    return port


def get_admin_password(data_dir: str) -> Optional[str]:
    """Read the auto-generated WP admin password (`~/.onionpress/wp-admin-password`).

    None if the file doesn't exist (user has done their own setup, or
    headless install hasn't run yet).
    """
    path = os.path.join(data_dir, "wp-admin-password")
    try:
        with open(path) as f:
            value = f.read().strip()
            return value or None
    except OSError:
        return None


# The launcher's `restore` passes on the Python CLI's log lines on stderr
# ("[2026-10-12 10:30:00] Restore failed during extract/seed: ...") and ends
# with "ERROR: Restore failed — see <log>". The dialog shows the last few.
_LOG_STAMP_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def restore_failure_message(output: str, data_dir: str, log_path: str,
                            *, max_lines: int = 6) -> str:
    """What a failed `onionpress restore` says to the user (#323).

    *output* is the launcher's stdout then stderr. Its last *max_lines*
    non-empty lines, timestamps dropped, control and other non-printable
    characters removed (they can carry names from the backup file, which
    is its maker's), each cut to 200 characters. Then the staging folder
    kept for the next start's import, if the .install-from-backup marker
    names one, and the log's path.
    """
    lines = []
    for raw in (output or "").splitlines():
        text = "".join(ch for ch in _LOG_STAMP_PREFIX.sub("", raw.strip())
                       if ch.isprintable()).strip()
        if text and not text.startswith("ERROR: Restore failed — see "):
            lines.append(text if len(text) <= 200 else text[:200] + "…")
    parts = ["Restore failed — the backup was refused or could not be restored."]
    if lines:
        parts.append("\n".join(lines[-max_lines:]))
    try:
        with open(os.path.join(data_dir, ".install-from-backup")) as f:
            staging = f.readline().strip()
    except OSError:
        staging = ""
    if staging and os.path.isdir(staging):
        parts.append(f"The unpacked backup is kept in {staging}; OnionPress "
                     f"imports it again at its next start.")
    parts.append(f"Full details: {log_path}")
    return "\n\n".join(parts)


def run_launcher_restore(launcher_script: str, zip_path: str, password: str,
                         *, data_dir: str, log_path: str,
                         extra_env: dict | None = None,
                         timeout: int | None = 1800) -> tuple:
    """`<launcher> restore <zip>` with the password in the environment
    (BACKUP_PASSWORD_ENV: argv is readable by every local user, the
    environment only by this one), and extra_env too (a hub's recovery
    key). (True, "", stderr) on success, else (False,
    restore_failure_message(...), stderr). The exit status is the only
    sign of a refused or failed restore. timeout=None waits as long as
    the launcher runs.
    """
    try:
        r = subprocess.run(
            [launcher_script, "restore", zip_path],
            env={**os.environ, BACKUP_PASSWORD_ENV: password} | (extra_env or {}),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, restore_failure_message(
            f"The restore did not finish within {timeout // 60} minutes.",
            data_dir, log_path), ""
    if r.returncode == 0:
        return True, "", r.stderr or ""
    return False, restore_failure_message(
        (r.stdout or "") + "\n" + (r.stderr or ""), data_dir, log_path), r.stderr or ""
