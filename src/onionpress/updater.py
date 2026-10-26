"""
OnionPress updater — checks for new releases and installs them.

Multi-user aware: detects other OnionPress instances on the same Mac
and guides the user through a safe coordinated update.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile

from . import release_signing

def _parse_version(version_str):
    """Parse a version string like '2.10.3' into a tuple of ints."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


# macOS's CA bundle. Debian, Ubuntu and Raspberry Pi OS keep theirs at
# /etc/ssl/certs/ca-certificates.crt, which curl finds by itself; naming
# the Mac's path there made every check fail before connecting (curl exit
# 77, issue #355), and the tray read the failure as "Up to date".
CA_BUNDLE = "/etc/ssl/cert.pem"


def _curl_tls_args():
    """`--cacert CA_BUNDLE` where that file exists, else nothing (curl's
    own default bundle)."""
    if os.path.exists(CA_BUNDLE):
        return ["--cacert", CA_BUNDLE]
    return []


# ---------------------------------------------------------------------------
# Instance discovery
# ---------------------------------------------------------------------------

def detect_other_instances():
    """Find other users' OnionPress MenubarApp processes.

    Uses ``ps aux`` which shows processes for all users without needing
    elevated privileges.

    Returns a list of dicts: ``[{"user": "internetarchive", "pid": 1458}]``
    Only includes instances belonging to *other* users (not the caller).
    """
    current_user = os.environ.get("USER", "")
    current_pid = os.getpid()
    others = []

    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        for line in result.stdout.splitlines():
            if "MenubarApp" not in line or "OnionPress" not in line:
                continue
            parts = line.split(None, 10)
            if len(parts) < 2:
                continue
            user = parts[0]
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            if user == current_user and pid == current_pid:
                continue
            if user == current_user:
                continue  # same user, different helper process
            others.append({"user": user, "pid": pid})
    except Exception:
        pass

    return others


def detect_active_offsets():
    """Return a list of port offsets currently in use.

    Tries to bind 8080, 18080, 28080, ... up to 5 slots.
    Returns offsets where the port is already taken, e.g. ``[0, 10000]``.
    """
    active = []
    for offset in (0, 10000, 20000, 30000, 40000):
        port = 8080 + offset
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                active.append(offset)
            finally:
                s.close()
        except OSError:
            pass
    return active


def _primary_user(others):
    """Return the username that owns port-offset 0, or None."""
    for inst in others:
        # We can't directly check which offset another user has, but the
        # primary user is the one on offset 0.  Since we know our own offset,
        # if we're not offset-0 then one of the others must be.
        return inst["user"]
    return None


# ---------------------------------------------------------------------------
# GitHub release check
# ---------------------------------------------------------------------------

def check_for_update(current_version, log=None):
    """Check GitHub for a newer OnionPress release.

    Returns ``(release_data, latest_version)`` if an update is available,
    or ``None`` if already up to date (or on error).
    """
    url = "https://api.github.com/repos/brewsterkahle/onionpress/releases/latest"
    try:
        result = subprocess.run(
            ["curl", "-s", *_curl_tls_args(),
             "-H", "User-Agent: onionpress", "--max-time", "10", url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        if result.returncode != 0:
            if log:
                log(f"Update check curl failed: exit={result.returncode} "
                    f"stderr={result.stderr.strip()}")
            return None

        data = json.loads(result.stdout)
        latest_version = data.get("tag_name", "").lstrip("v")
        if log:
            log(f"Update check: current={current_version}, latest={latest_version}")

        if latest_version and _parse_version(latest_version) > _parse_version(current_version):
            return (data, latest_version)

    except Exception as e:
        if log:
            log(f"Update check failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Download & install
# ---------------------------------------------------------------------------

STAGING_PREFIX = ".onionpress-update-"

# Every release carries SHA256SUMS ("<sha256>  <asset name>", sha256sum
# format, written by build/release.sh); the Linux update installs only a
# .deb matching its line, and the Mac update installs only a .dmg
# matching its line (issue #386). Nothing else checks what the DMG is:
# the bundle is ad-hoc signed, and the swap strips quarantine.
RELEASE_SUMS = "SHA256SUMS"
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](\S+)$", re.M)


def published_sha256(sums_text, asset_name):
    """The one sha256 SHA256SUMS gives for *asset_name*, lowercase, or
    None when it gives none or more than one."""
    found = [m.group(1).lower() for m in _SUMS_LINE.finditer(sums_text)
             if m.group(2) == asset_name]
    return found[0] if len(found) == 1 else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_release_sums(release_data, tmp_dir):
    """Download the release's SHA256SUMS; its text. Raises when the
    release has none or it cannot be fetched."""
    sums_url = None
    for asset in release_data.get("assets", []):
        if asset.get("name") == RELEASE_SUMS:
            sums_url = asset["browser_download_url"]
            break
    if not sums_url:
        raise RuntimeError(f"The release carries no {RELEASE_SUMS}; not installing an unchecked DMG")
    sums_path = os.path.join(tmp_dir, RELEASE_SUMS)
    result = subprocess.run(
        ["curl", "-L", "-s", *_curl_tls_args(),
         "-H", "User-Agent: onionpress", "--max-time", "60",
         "--max-filesize", "65536", "-o", sums_path, sums_url],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not download {RELEASE_SUMS} (curl exit {result.returncode}): "
            f"{result.stderr.strip()}")
    with open(sums_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(65536)


def stage_new_bundle(source_app, install_path):
    """Copy *source_app* (inside the mounted DMG) into a hidden folder beside
    *install_path* and return the staged bundle's path.

    The copy must never land in install_path itself: the running app
    executes binaries out of that path (its bin_dir is absolute), and
    macOS 26/27's Gatekeeper revokes a process's access to a bundle it is
    writing the moment something executes out of it. A sibling folder is on
    the same volume as install_path, so the staged bundle can be renamed
    into place. Quarantine is stripped here, before the bundle is live. On
    any failure the staging folder is removed and install_path has not been
    touched.
    """
    parent = os.path.dirname(install_path)
    staging_dir = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent)
    staged_app = os.path.join(staging_dir, os.path.basename(install_path))
    try:
        result = subprocess.run(
            ["cp", "-R", source_app, staged_app],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Copy failed: {result.stderr.strip()}")
        # Gatekeeper quarantine flag, should the download carry one
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", staged_app],
            capture_output=True, timeout=10,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return staged_app


def swap_in_bundle(staged_app, install_path, log=None):
    """Rename install_path aside and the staged bundle into its place.

    The old bundle becomes install_path + ".bak-<pid>" (PID-unique, so a
    stale .bak owned by another user never blocks the swap) and is removed
    once the new one is in; if the second rename fails, the old bundle is
    put back. The staging folder goes either way, as do stale .bak and
    staging folders left by earlier updates.
    """
    staging_dir = os.path.dirname(staged_app)
    backup_path = install_path + f".bak-{os.getpid()}"
    if os.path.exists(backup_path):
        shutil.rmtree(backup_path, ignore_errors=True)
        if os.path.exists(backup_path):
            subprocess.run(["rm", "-rf", backup_path], capture_output=True, timeout=15)

    try:
        os.rename(install_path, backup_path)
        try:
            os.rename(staged_app, install_path)
        except Exception:
            if log:
                log("Auto-update: swap failed, restoring backup")
            os.rename(backup_path, install_path)
            raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    shutil.rmtree(backup_path, ignore_errors=True)
    import glob as _glob
    for stale in _glob.glob(install_path + ".bak*") + _glob.glob(
            os.path.join(os.path.dirname(install_path), STAGING_PREFIX + "*")):
        shutil.rmtree(stale, ignore_errors=True)


def _fetch_release_sig(release_data, tmp_dir):
    """The release's SHA256SUMS.sig text, or None when it carries none
    (release_signing decides what that means). A download that fails is
    a missing signature, never a pass."""
    sig_url = None
    for asset in release_data.get("assets", []):
        if asset.get("name") == release_signing.SIG_ASSET:
            sig_url = asset["browser_download_url"]
            break
    if not sig_url:
        return None
    sig_path = os.path.join(tmp_dir, release_signing.SIG_ASSET)
    result = subprocess.run(
        ["curl", "-L", "-s", *_curl_tls_args(),
         "-H", "User-Agent: onionpress", "--max-time", "60",
         "--max-filesize", "4096", "-o", sig_path, sig_url],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90,
    )
    if result.returncode != 0:
        return None
    with open(sig_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(4096)


def download_and_install(release_data, latest_version, install_path,
                         log=None, notify=None, before_swap=None, after_swap=None):
    """Download the DMG from *release_data*, replace the app at *install_path*.

    *log* — callable(str) for logging.
    *notify* — callable(title, subtitle, message) for macOS notifications.
    *before_swap* / *after_swap* — called once the new bundle is staged,
    right before and after the rename swap: `onionpress update` stops the
    daemon (it executes from the bundle being replaced) and starts it again
    from the new one (issue #367).

    Raises on failure.  Returns the installed version string on success.
    """

    # Find the DMG asset
    dmg_url = None
    dmg_name = None
    for asset in release_data.get("assets", []):
        if asset["name"].endswith(".dmg"):
            dmg_url = asset["browser_download_url"]
            dmg_name = asset["name"]
            break

    if not dmg_url:
        raise RuntimeError("No .dmg asset found in the release")

    if log:
        log(f"Auto-update: downloading {dmg_url}")
    if notify:
        notify("OnionPress", "Downloading Update...",
               f"Downloading v{latest_version}. This may take a minute.")

    tmp_dir = tempfile.mkdtemp(prefix="onionpress-update-")
    dmg_path = os.path.join(tmp_dir, "onionpress.dmg")
    mount_point = os.path.join(tmp_dir, "dmg-mount")

    try:
        # Download
        result = subprocess.run(
            ["curl", "-L", "-s", *_curl_tls_args(),
             "-H", "User-Agent: onionpress", "--max-time", "300",
             "-o", dmg_path, dmg_url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=360,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Download failed (curl exit {result.returncode}): "
                f"{result.stderr.strip()}")

        dmg_size = os.path.getsize(dmg_path)
        if log:
            log(f"Auto-update: downloaded {dmg_size / 1024 / 1024:.1f} MB")
        if dmg_size < 1_000_000:
            raise RuntimeError(
                f"Downloaded file too small ({dmg_size} bytes), "
                "likely not a valid DMG")

        # The DMG is what the release's SHA256SUMS says it is, or it is
        # not mounted (issue #386): a swapped asset, a truncated or
        # tampered download, or a release without the file all stop here.
        sums_text = _fetch_release_sums(release_data, tmp_dir)
        # The file's signature (issue #246): checked when a release key is
        # embedded; said as unchecked until one is.
        sig_text = _fetch_release_sig(release_data, tmp_dir)
        ok, note = release_signing.check_release_sums(sums_text.encode("utf-8"), sig_text)
        if log:
            log(f"Auto-update: {note}")
        if not ok:
            raise RuntimeError(note)
        want = published_sha256(sums_text, dmg_name)
        if not want:
            raise RuntimeError(
                f"{RELEASE_SUMS} of the release does not give one sha256 for {dmg_name}")
        got = sha256_of(dmg_path)
        if got != want:
            raise RuntimeError(
                f"{dmg_name} does not match the release's {RELEASE_SUMS} "
                f"(sha256 {got}, published {want}); not installing it")
        if log:
            log(f"Auto-update: {dmg_name} matches {RELEASE_SUMS} ({want[:16]}...)")

        # Mount
        os.makedirs(mount_point, exist_ok=True)
        result = subprocess.run(
            ["hdiutil", "attach", dmg_path,
             "-mountpoint", mount_point, "-nobrowse", "-quiet"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to mount DMG: {result.stderr.strip()}")

        if log:
            log(f"Auto-update: DMG mounted at {mount_point}")

        # Find OnionPress.app inside the DMG
        source_app = os.path.join(mount_point, "OnionPress.app")
        if not os.path.isdir(source_app):
            for item in os.listdir(mount_point):
                candidate = os.path.join(mount_point, item, "OnionPress.app")
                if os.path.isdir(candidate):
                    source_app = candidate
                    break
            else:
                raise RuntimeError(
                    f"OnionPress.app not found in DMG. "
                    f"Contents: {os.listdir(mount_point)}")

        if log:
            log(f"Auto-update: replacing {install_path}")

        # Stage the new bundle in a hidden sibling folder, then swap it in
        # with two renames. It used to be copied straight into install_path
        # after the old bundle was renamed away — but the running app's
        # bin_dir is that absolute path, so its next docker call (health
        # probes every few seconds) executed a binary out of the half-copied
        # bundle, and macOS 26/27's Gatekeeper answered by revoking the
        # copy's access to the file cp had open ("fcopyfile failed:
        # Input/output error", then "Operation not permitted" for the rest;
        # 2.5.2 → 2.5.3 on 2026-10-13). Nothing executes from the staging
        # path, and a sibling is on the same volume, so rename(2) works.
        staged_app = stage_new_bundle(source_app, install_path)
        if before_swap is not None:
            before_swap()
        try:
            swap_in_bundle(staged_app, install_path, log=log)
        finally:
            if after_swap is not None:
                after_swap()

        if log:
            log(f"Auto-update: installed v{latest_version} successfully")

        return latest_version

    finally:
        # Always unmount and clean up
        subprocess.run(
            ["hdiutil", "detach", mount_point, "-quiet", "-force"],
            capture_output=True, timeout=15,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
