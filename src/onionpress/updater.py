"""
OnionPress updater: the one release client of both platforms.

The release lookup on GitHub (the latest, a chosen version, a pre-release),
the version compare, the draft and pre-release rules, the release's
SHA256SUMS and the check of a downloaded asset against it live here, for
`onionpress update` (cli.cmd_update) and `check-for-update`. What differs
by platform is the install step and where the installed version is read:
a Mac's app bundle is replaced by the staged swap below
(download_and_install); Linux's release package is fetched and checked
here (fetch_deb) and installed by the launcher's apt-get leaf
(`update-install-deb`, linux/onionpress). Until 2.6.1 linux/onionpress
carried a second client of its own in bash.

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
from dataclasses import dataclass, field

from . import __version__


def _parse_version(version_str):
    """Parse a version string like '2.10.3' into a tuple of ints."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


# A release version as the tags carry it after the "v": one to four
# groups of digits. The digits are spelled out (not 0-9, \d) so nothing
# but ASCII digits passes; fullmatch, so a trailing newline fails too.
_RELEASE_VERSION = re.compile(r"[0123456789]{1,9}(\.[0123456789]{1,9}){1,3}")


def is_release_version(text):
    """True for a release version like 2.5.3 (no "v", no suffix)."""
    return isinstance(text, str) and _RELEASE_VERSION.fullmatch(text) is not None


def version_newer(a, b):
    """True when release version *a* is newer than *b*: compared group by
    group as numbers, missing groups as 0 (2.6 is 2.6.0; 2.10.0 is newer
    than 2.9.9), never as text."""
    x, y = _parse_version(a), _parse_version(b)
    n = max(len(x), len(y))
    return x + (0,) * (n - len(x)) > y + (0,) * (n - len(y))


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
# GitHub release lookup
# ---------------------------------------------------------------------------

# One repository for releases, on both platforms.
RELEASE_REPO = "brewsterkahle/onionpress"
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases"
RELEASE_DOWNLOAD = f"https://github.com/{RELEASE_REPO}/releases/download"
# Every release carries SHA256SUMS ("<sha256>  <asset name>", sha256sum
# format, written by build/release.sh); the Linux update installs only a
# .deb matching its line, and the Mac update installs only a .dmg
# matching its line (issue #386). Nothing else checks what the DMG is:
# the bundle is ad-hoc signed, and the swap strips quarantine.
RELEASE_SUMS = "SHA256SUMS"
RELEASE_DEB = "onionpress.deb"
# Where the .deb installs; the update refuses to put it beside another tree.
RELEASE_INSTALL_DIR = "/opt/onionpress"
SUMS_MAX_BYTES = 65536
DEB_MAX_BYTES = 268435456
# Linux: the file the .deb (and install-tree.sh) write the version to.
VERSION_FILE = "VERSION"


def _curl_argv(*args):
    """curl for GitHub: HTTPS only, redirects included (release downloads
    redirect to GitHub's asset host) and held to HTTPS, failing on any
    HTTP error, with the CA bundle rule of _curl_tls_args. *args* end
    with the URL."""
    return ["curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
            "--connect-timeout", "20", *_curl_tls_args(),
            "-H", "User-Agent: onionpress", *args]


def _run_curl(args, timeout):
    """One curl run; (returncode, stdout, stderr), or (None, "", why) when
    curl could not run at all."""
    try:
        r = subprocess.run(_curl_argv(*args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)
    return r.returncode, r.stdout, r.stderr.strip()


def _curl_failure(rc, stderr):
    if rc is None:
        return f"curl could not run: {stderr}"
    return f"curl exit {rc}: {stderr}" if stderr else f"curl exit {rc}"


class UpdateError(Exception):
    """A lookup, download or check that stops the update; the message is
    what the person is told."""


@dataclass
class Release:
    """One GitHub release: its tag (v + a release version), the version,
    GitHub's record and its asset names."""
    tag: str
    version: str
    data: dict
    assets: list = field(default_factory=list)

    def asset_count(self, name):
        return self.assets.count(name)

    def download_url(self, name):
        """The release's asset by name, from the one host and tag (never
        a URL GitHub's record offers)."""
        return f"{RELEASE_DOWNLOAD}/{self.tag}/{name}"

    def dmg_name(self):
        """The one asset ending in .dmg, or None."""
        dmgs = [n for n in self.assets if n.endswith(".dmg")]
        return dmgs[0] if len(dmgs) == 1 else None

    def install_problem(self, os_name):
        """Why `onionpress update` on *os_name* could not install this
        release (its package, or the SHA256SUMS to check it against, is
        missing), or None when it can."""
        if os_name == "linux":
            if self.asset_count(RELEASE_DEB) != 1:
                return f"release {self.tag} has no {RELEASE_DEB}."
            what = "package"
        else:
            if self.dmg_name() is None:
                return f"release {self.tag} has no .dmg."
            what = "disk image"
        if self.asset_count(RELEASE_SUMS) != 1:
            return (f"release {self.tag} publishes no {RELEASE_SUMS}, "
                    f"so its {what} cannot be checked.")
        return None


def _release_from(doc, want, pre):
    """The Release GitHub's answer *doc* describes, held to the rules:
    one release (from a list: the newest that is not a draft), a tag that
    is v + a release version (the tag *want*ed, when one was), not a
    draft, a pre-release only with *pre*. A missing draft or prerelease
    flag counts as set."""
    if isinstance(doc, list):
        doc = next((x for x in doc if isinstance(x, dict) and x.get("draft") is False), None)
    if not isinstance(doc, dict):
        raise UpdateError("GitHub's answer did not describe a release.")
    tag = doc.get("tag_name")
    tag = re.sub(r"[^A-Za-z0-9._-]", "?", tag if isinstance(tag, str) else "")[:64]
    if not tag.startswith("v") or not is_release_version(tag[1:]):
        raise UpdateError(f"the release's tag \"{tag}\" is not a release version.")
    if want and tag != f"v{want}":
        raise UpdateError(f"GitHub answered with {tag}, not v{want}.")
    if doc.get("draft") is not False:
        raise UpdateError(f"{tag} is a draft, not a release.")
    if doc.get("prerelease") is not False and not pre:
        raise UpdateError(f"{tag} is a pre-release; add --pre to install it.")
    assets = [a.get("name") for a in (doc.get("assets") or [])
              if isinstance(a, dict) and isinstance(a.get("name"), str)]
    return Release(tag, tag[1:], doc, assets)


def find_release(version=None, pre=False, log=None):
    """Look up a release on GitHub: the latest (*version* None), or tag
    v<version>. With *pre* a pre-release may answer (and, without a
    version, the newest release that is not a draft, pre-release or not).
    Raises UpdateError: GitHub not reached, not a release version, no
    such release, a draft, or a pre-release without *pre*."""
    want = None
    if version is not None:
        want = version[1:] if version.startswith("v") else version
        if not is_release_version(want):
            raise UpdateError(f"\"{version}\" is not a release version (like 2.5.3).")
        url = f"{RELEASE_API}/tags/v{want}"
    elif pre:
        url = f"{RELEASE_API}?per_page=30"
    else:
        url = f"{RELEASE_API}/latest"
    rc, out, err = _run_curl(["--max-time", "30", "--max-filesize", "4000000",
                              "-H", "Accept: application/vnd.github+json", url], timeout=45)
    if rc != 0:
        if log:
            log(f"Update check curl failed: exit={rc} stderr={err}")
        if want:
            raise UpdateError(f"could not find release v{want} on GitHub (no such release, "
                              f"or GitHub could not be reached; {_curl_failure(rc, err)}).")
        raise UpdateError(f"could not look up the latest release on GitHub "
                          f"({_curl_failure(rc, err)}).")
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    return _release_from(doc, want, pre)


@dataclass
class UpdateCheck:
    """What `onionpress update` learnt: the release found (None with
    *error*), whether it is newer than *current* (an install whose
    version cannot be read takes any release), and why this platform's
    update could not install it (None when it could)."""
    current: str
    release: object = None
    error: str = None
    newer: bool = False
    install_problem: str = None


def check_release(current, log=None, *, version=None, pre=False, os_name="macos"):
    """The release *version* (or the latest) against the installed
    *current*: an UpdateCheck, never an exception. On any failure (network
    down, GitHub rate-limited, not a release) its `error` says why."""
    try:
        release = find_release(version, pre, log=log)
    except UpdateError as e:
        if log:
            log(f"Update check failed: {e}")
        return UpdateCheck(current, error=str(e))
    if log:
        log(f"Update check: current={current}, latest={release.version}")
    newer = not is_release_version(current) or version_newer(release.version, current)
    return UpdateCheck(current, release=release, newer=newer,
                       install_problem=release.install_problem(os_name))


def check_for_update(current_version, log=None):
    """Check GitHub for a newer OnionPress release.

    Returns ``(release_data, latest_version)`` if an update is available,
    or ``None`` if already up to date (or on error; check_release tells
    the two apart).
    """
    c = check_release(current_version, log=log)
    if c.release is not None and c.newer:
        return (c.release.data, c.release.version)
    return None


def installed_version(os_name, *, app_path=None, install_dir=None):
    """The version installed here: a Mac's from the bundle's Info.plist
    (*app_path*), Linux's from the install tree's VERSION file
    (*install_dir*, else $ONIONPRESS_INSTALL_DIR, else /opt/onionpress);
    the running package's own version where neither can be read."""
    if os_name == "macos":
        if app_path:
            import plistlib
            try:
                with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
                    return plistlib.load(f).get("CFBundleShortVersionString") or __version__
            except (OSError, ValueError):
                pass
        return __version__
    install_dir = install_dir or os.environ.get("ONIONPRESS_INSTALL_DIR") or RELEASE_INSTALL_DIR
    try:
        with open(os.path.join(install_dir, VERSION_FILE), encoding="utf-8",
                  errors="replace") as f:
            text = f.readline().strip()
    except OSError:
        text = ""
    return text or __version__


# ---------------------------------------------------------------------------
# Download & install
# ---------------------------------------------------------------------------

STAGING_PREFIX = ".onionpress-update-"

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
    rc, _, err = _run_curl(["--max-time", "60", "--max-filesize", str(SUMS_MAX_BYTES),
                            "-o", sums_path, sums_url], timeout=90)
    if rc != 0:
        raise RuntimeError(f"Could not download {RELEASE_SUMS} ({_curl_failure(rc, err)})")
    with open(sums_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(SUMS_MAX_BYTES)


def fetch_deb(release, tmp_dir, log=None):
    """Download *release*'s SHA256SUMS and onionpress.deb into *tmp_dir*
    (a folder of the user's own) and check the package: its sha256 is
    the one SHA256SUMS gives for onionpress.deb (exactly one such line),
    and it is the onionpress package of the release's version (dpkg-deb).
    Returns (path, sha256); raises UpdateError, with nothing to install."""
    sums_path = os.path.join(tmp_dir, RELEASE_SUMS)
    deb_path = os.path.join(tmp_dir, RELEASE_DEB)
    rc, _, err = _run_curl(["--max-time", "60", "--max-filesize", str(SUMS_MAX_BYTES),
                            "-o", sums_path, release.download_url(RELEASE_SUMS)], timeout=90)
    if rc != 0:
        raise UpdateError(f"could not download {RELEASE_SUMS} of {release.tag} "
                          f"({_curl_failure(rc, err)}).")
    rc, _, err = _run_curl(["--max-time", "900", "--max-filesize", str(DEB_MAX_BYTES),
                            "-o", deb_path, release.download_url(RELEASE_DEB)], timeout=960)
    if rc != 0:
        raise UpdateError(f"could not download {RELEASE_DEB} of {release.tag} "
                          f"({_curl_failure(rc, err)}).")
    with open(sums_path, "r", encoding="utf-8", errors="replace") as f:
        want = published_sha256(f.read(SUMS_MAX_BYTES), RELEASE_DEB)
    if not want:
        raise UpdateError(f"{RELEASE_SUMS} of {release.tag} does not give one sha256 "
                          f"for {RELEASE_DEB}.")
    got = sha256_of(deb_path)
    if got != want:
        raise UpdateError(f"{RELEASE_DEB} of {release.tag} does not match its {RELEASE_SUMS} "
                          f"(sha256 {got}, published {want}).")
    if _deb_field(deb_path, "Package") != "onionpress":
        raise UpdateError(f"{RELEASE_DEB} of {release.tag} is not the onionpress package.")
    deb_version = _deb_field(deb_path, "Version")
    if deb_version != release.version:
        raise UpdateError(f"{RELEASE_DEB} of {release.tag} is version "
                          f"{deb_version or 'unknown'}, not {release.version}.")
    if log:
        log(f"update: {RELEASE_DEB} of {release.tag} matches its {RELEASE_SUMS} (sha256 {got})")
    return deb_path, got


def _deb_field(deb_path, name):
    """`dpkg-deb --field <deb> <name>`'s first line, or "" when it fails."""
    try:
        r = subprocess.run(["dpkg-deb", "--field", deb_path, name], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""


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
        rc, _, err = _run_curl(["--max-time", "300", "-o", dmg_path, dmg_url], timeout=360)
        if rc != 0:
            raise RuntimeError(f"Download failed ({_curl_failure(rc, err)})")

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
