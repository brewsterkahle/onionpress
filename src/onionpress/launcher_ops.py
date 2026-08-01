"""Cross-platform launcher operations shared between Mac and Linux.

Primitives used by the Linux bash launcher (`linux/onionpress` via
`python3 -m onionpress.cli`), the Linux tray app (`linux/onionpress-tray`,
direct import), and — eventually — the Mac menubar app.

Each function takes explicit paths and doesn't read environment or globals.
Platform-aware glue (Tor Browser detection, which container runtime) lives
in callers.

Operations:
- open_in_browser(url, prefer_tor)         — launch via xdg-open / open / torbrowser-launcher
- tor_image_has_mkp224o(image)             — verify the tor image bundles mkp224o
- generate_vanity_in_container(...)        — docker run mkp224o, write to vanity-keys/
- get_admin_password(data_dir)             — read ~/.onionpress/wp-admin-password
"""

import os
import shutil
import subprocess
import sys
from typing import Optional


# Pinned to digest — must match docker-compose.yml and linux/onionpress.
# Refresh all three together via build/refresh-image-digests.sh.
DEFAULT_TOR_IMAGE = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:ecab8ad6c9a196b308441f1eac787504d8c43fb6ad7638363edb14a41e784e2b"


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


def open_in_browser(url: str, *, prefer_tor: bool = False) -> None:
    """Spawn a browser at `url`. Detached — does not wait."""
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
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "test", image,
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
    if not (2 <= len(prefix) <= 6):
        raise ValueError(f"prefix must be 2-6 chars (got {prefix!r})")

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
    # of start_containers fixes that with a chown.
    cmd = [
        "docker", "run", "--rm",
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

    # mkp224o writes <addr>.onion/ directory under /out
    candidates = [
        d for d in os.listdir(vanity_dir)
        if d.startswith(prefix) and d.endswith(".onion")
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


def mint_op_login_token(container: str = "onionpress-wordpress",
                        ttl_seconds: int = 120) -> Optional[str]:
    """Mint a one-shot magic-link token for auto-login.

    Mirrors `onionpress dashboard` (linux/onionpress:2390) in Python so
    both Mac and Linux clients can build any localhost-or-.onion URL with
    `?op_login=<tok>` appended. The `onionpress-auto-login.php` mu-plugin
    validates the token, sets the auth cookie, deletes the transient,
    and redirects to the same URL minus the query param.

    Returns the 32-char hex token, or None on any failure (caller should
    fall back to opening the URL without auto-login).
    """
    try:
        # Find the first admin user
        proc = subprocess.run(
            ["docker", "exec", container, "wp", "user", "list",
             "--role=administrator", "--field=ID", "--allow-root"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        admin_uid = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
        if not admin_uid.isdigit():
            return None
        token = secrets.token_hex(16)  # 32 hex chars
        # Set transient
        proc = subprocess.run(
            ["docker", "exec", container, "wp", "transient", "set",
             f"op_login_{token}", admin_uid, str(ttl_seconds), "--allow-root"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return token
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


# Lazy import — only needed by mint_op_login_token
import secrets  # noqa: E402


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
