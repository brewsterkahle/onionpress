"""Regenerable image derivatives: leave them out of backups, rebuild them
after a restore (issue #257).

WordPress keeps, next to every uploaded image, a set of resized copies —
`photo-300x200.jpg`, `photo-1024x683.jpg`, … for each registered image
size, plus `photo-scaled.jpg` for originals over the big-image threshold.
They are pure functions of the original, and on a measured site they were
half of wp-content/uploads (449 of 916 MB). So:

* `prune_image_derivatives()` deletes them from a backup's staging copy
  of uploads. A file counts as a derivative only when the file it was
  derived from sits in the same directory (WordPress's own rule): a true
  original that merely ends in `-123x456.jpg` has no such base file and
  is kept. Video, audio, PDFs and everything else are never touched.

* `schedule_regeneration()` writes the host-side marker
  `~/.onionpress/.regenerate-derivatives-pending` when a backup is
  restored. The backup archive also carries an in-container marker at
  `wp-content/uploads/.onionpress-regenerate-derivatives`, which a plain
  `docker cp` of wp-content delivers into the container — so even an
  OnionPress version that predates this module leaves the "rebuild me"
  signal behind when it restores a derivative-free backup.

* `regenerate_if_pending()` runs, when either marker is present,
  `wp media regenerate --only-missing --yes` for every site, `nice`d, in
  the WordPress container as www-data, and clears both markers only on
  success.
  `--only-missing` builds just the sizes absent on disk, so a run that a
  reboot, quit or OOM kill interrupts resumes where it left off at the
  next launcher start instead of starting over. The launchers spawn this
  detached in the background on every start.

While a marker is present the `onionpress-image-fallback.php` mu-plugin
serves the original (or `-scaled`) file for any size whose file is not on
disk yet, so the restored site shows heavier images rather than broken
ones during the regeneration window.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from . import wp_files

# Host-side marker in the OnionPress data dir (~/.onionpress/).
HOST_MARKER = ".regenerate-derivatives-pending"

# In-container marker. Relative to a backup's wp-content/ dir on the host,
# absolute inside the WordPress container. Lives in uploads/ so that a
# restore's `docker cp wp-content/. …` carries it across.
ARCHIVE_MARKER_RELPATH = os.path.join("uploads", ".onionpress-regenerate-derivatives")
CONTAINER_MARKER = "/var/www/html/wp-content/" + ARCHIVE_MARKER_RELPATH.replace(os.sep, "/")
CONTAINER_UPLOADS = "/var/www/html/wp-content/uploads"

# Lock held for the duration of one regeneration sweep so two launcher
# starts inside the window don't run two sweeps over the same files.
LOCK_FILE = ".regenerate-derivatives.lock"

WORDPRESS_CONTAINER = "onionpress-wordpress"

# One sweep re-encodes every image on the site; hours for a big archive on
# the 1 GB VM. Generous rather than tight: a stuck docker exec is the only
# thing this guards against, and an interrupted run is resumed anyway.
REGENERATE_TIMEOUT = 6 * 3600

# Extensions WordPress's image editors produce derivatives in. A base file
# may be any of these (a JPEG original can have WebP sizes under a
# format-converting plugin) or one of the raster formats WordPress accepts
# but never writes sizes in.
DERIVATIVE_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "avif")
ORIGINAL_EXTS = DERIVATIVE_EXTS + ("heic", "heif", "tif", "tiff", "bmp")

# `<stem>-<W>x<H>.<ext>` (a registered size) or `<stem>-scaled.<ext>`.
_DERIVATIVE_RE = re.compile(
    r"^(?P<stem>.+?)-(?P<suffix>\d+x\d+|scaled)\.(?P<ext>" + "|".join(DERIVATIVE_EXTS) + r")$",
    re.IGNORECASE,
)


def derivative_base_candidates(filename: str) -> list[str]:
    """Names which, if present beside `filename`, make it a derivative.

    `photo-300x200.jpg` -> photo.jpg, photo.jpeg, …, photo-scaled.jpg, …
    `photo-scaled.jpg`  -> photo.jpg, photo.jpeg, …
    A name that does not look like a derivative returns [].
    """
    m = _DERIVATIVE_RE.fullmatch(filename)
    if not m:
        return []
    stem = m.group("stem")
    own_ext = m.group("ext")
    exts: list[str] = [own_ext]
    for ext in ORIGINAL_EXTS:
        if ext not in exts:
            exts.append(ext)
    candidates = [f"{stem}.{ext}" for ext in exts]
    if m.group("suffix").lower() != "scaled":
        # Sizes of an over-threshold image are cut from its -scaled copy;
        # if only that copy survives, the sizes are still regenerable.
        candidates += [f"{stem}-scaled.{ext}" for ext in exts]
    return candidates


def is_regenerable_derivative(filename: str, siblings: Iterable[str]) -> bool:
    """True iff `filename` is a WordPress size/-scaled copy whose base file
    is among `siblings` (the other names in the same directory)."""
    candidates = derivative_base_candidates(filename)
    if not candidates:
        return False
    sibling_set = set(siblings)
    sibling_set.discard(filename)
    return any(c in sibling_set for c in candidates)


def prune_image_derivatives(uploads_dir: str, log_func: Optional[Callable[[str], None]] = None):
    """Delete regenerable derivatives under a staging copy of uploads/.

    Returns (files_removed, bytes_removed). Never follows symlinks and
    never touches anything outside `uploads_dir`. A missing uploads dir
    is a no-op (fresh sites have no media), and so is one that is a link:
    the staged copy came out of the container, and os.walk would follow it.
    """
    log = log_func or (lambda _m: None)
    removed = 0
    freed = 0
    if os.path.islink(uploads_dir) or not os.path.isdir(uploads_dir):
        return 0, 0
    for dirpath, _dirnames, filenames in os.walk(uploads_dir):
        names = set(filenames)
        for name in filenames:
            if not is_regenerable_derivative(name, names):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                size = os.path.getsize(path)
                os.unlink(path)
            except OSError as e:
                log(f"Backup: could not prune {path}: {e}")
                continue
            removed += 1
            freed += size
    log(f"Backup: pruned {removed} regenerable image derivative(s) "
        f"({freed / 1_048_576:.1f} MB) — the restore rebuilds them")
    return removed, freed


def write_archive_marker(wpcontent_dir: str) -> str:
    """Write the in-container marker into a backup's staged wp-content/.
    Returns the path written.

    The staged wp-content came out of the container: a link at uploads/ or
    at the marker's own path is refused (OSError) rather than written
    through.
    """
    path = os.path.join(wpcontent_dir, ARCHIVE_MARKER_RELPATH)
    uploads = os.path.dirname(path)
    os.makedirs(uploads, exist_ok=True)
    if os.path.islink(uploads):
        raise OSError(f"staged uploads folder is a link: {uploads}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write("OnionPress: this backup carries original images only; the "
                "resized copies (-WxH, -scaled) are rebuilt after a restore "
                "by `wp media regenerate --only-missing`. Removed on completion.\n")
    return path


def schedule_regeneration(data_dir: str, log_func: Optional[Callable[[str], None]] = None,
                          metadata: Optional[dict] = None) -> str:
    """Write the host-side pending marker; the next launcher start (or the
    CLI restore itself) kicks off the sweep. Returns the marker path.

    Written for every restore, not only for backups flagged
    `excludes_image_derivatives`: on a complete set the sweep is a scan that
    finds nothing to do, and it also repairs sizes that were already
    missing at the source.
    """
    log = log_func or (lambda _m: None)
    path = os.path.join(data_dir, HOST_MARKER)
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    with open(path, "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    if metadata and metadata.get("excludes_image_derivatives"):
        log("Restore: backup carries original images only — resized copies "
            "will be rebuilt in the background after start")
    else:
        log("Restore: scheduled a check for missing resized images "
            "(backup predates derivative exclusion; expected to be a no-op)")
    return path


def host_marker_present(data_dir: str) -> bool:
    return os.path.exists(os.path.join(data_dir, HOST_MARKER))


def container_marker_present(docker_bin: str = "docker",
                             container: str = WORDPRESS_CONTAINER) -> bool:
    try:
        r = subprocess.run(
            [docker_bin, "exec", container, "test", "-f", CONTAINER_MARKER],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def regeneration_pending(data_dir: str, docker_bin: str = "docker") -> bool:
    return host_marker_present(data_dir) or container_marker_present(docker_bin)


def _site_urls(docker_bin: str, container: str) -> list[Optional[str]]:
    """Every site's URL on a multisite, else [None] (run without --url)."""
    try:
        r = wp_files.wp_cli("site", "list", "--field=url",
                            docker_bin=docker_bin, timeout=60,
                            container=container)
    except (OSError, subprocess.SubprocessError):
        return [None]
    urls = [u.strip() for u in (r.stdout or "").splitlines() if u.strip()]
    if r.returncode != 0 or not urls:
        return [None]
    return urls


def regenerate_if_pending(data_dir: str, log_func: Optional[Callable[[str], None]] = None,
                          docker_bin: str = "docker",
                          container: str = WORDPRESS_CONTAINER) -> bool:
    """Rebuild missing image sizes if a marker says a restore left them out.

    Returns True when there is nothing (left) to do — no marker, or the
    sweep completed and both markers were cleared — and False when the
    sweep could not run or did not finish, in which case the markers stay
    so the next start tries again. A sweep already in progress (lock held)
    returns True without touching anything.
    """
    log = log_func or (lambda _m: None)
    host_pending = host_marker_present(data_dir)
    container_pending = container_marker_present(docker_bin, container)
    if not (host_pending or container_pending):
        return True

    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(data_dir, LOCK_FILE)
    lock = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("Image derivatives: a regeneration sweep is already running — not starting another")
            return True

        installed = wp_files.wp_cli("core", "is-installed",
                                    docker_bin=docker_bin, timeout=60,
                                    container=container)
        if installed.returncode != 0:
            log("Image derivatives: WordPress is not up — leaving the marker for the next start")
            return False

        # wp-cli runs as www-data, which must be able to write every folder
        # of uploads: give it what an older version's root wp-cli left
        # root's, following no link www-data may have planted
        # (wp_files.own_args).
        subprocess.run(
            [docker_bin] + wp_files.own_args(CONTAINER_UPLOADS, recursive=True,
                                             container=container),
            capture_output=True, timeout=600,
        )
        sites = _site_urls(docker_bin, container)
        log(f"Image derivatives: rebuilding missing resized images for "
            f"{len(sites)} site(s) (wp media regenerate --only-missing)")
        for url in sites:
            args = ["media", "regenerate", "--only-missing", "--yes"]
            if url:
                args.append(f"--url={url}")
            try:
                r = wp_files.wp_cli(*args, docker_bin=docker_bin, nice=True,
                                    timeout=REGENERATE_TIMEOUT,
                                    container=container)
            except (OSError, subprocess.SubprocessError) as e:
                log(f"Image derivatives: regenerate failed for {url or 'site'}: {e} — will resume on next start")
                return False
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            if r.returncode != 0:
                err = (r.stderr or "").strip().splitlines()[-1:] or [""]
                log(f"Image derivatives: regenerate rc={r.returncode} for "
                    f"{url or 'site'}: {err[0][:200]} — will resume on next start")
                return False
            log(f"Image derivatives: {url or 'site'}: {tail[0][:200]}")

        rm = subprocess.run(
            [docker_bin] + wp_files.exec_args("rm", "-f", CONTAINER_MARKER,
                                              container=container),
            capture_output=True, timeout=30,
        )
        if rm.returncode != 0:
            log("Image derivatives: could not clear the in-container marker — will retry on next start")
            return False
        try:
            os.remove(os.path.join(data_dir, HOST_MARKER))
        except FileNotFoundError:
            pass
        log("Image derivatives: regeneration complete — markers cleared")
        return True
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def spawn_regeneration(data_dir: str, log_func: Optional[Callable[[str], None]] = None,
                       log_file: Optional[str] = None) -> Optional[int]:
    """Start `onionpress.cli regenerate-derivatives` detached in the
    background (own session, so it outlives the caller). Returns its pid, or
    None when nothing is pending or the spawn failed. The bash launchers do
    the same with a `( … & )` subshell; this is for the Python CLI paths."""
    log = log_func or (lambda _m: None)
    if not host_marker_present(data_dir):
        return None
    out = subprocess.DEVNULL
    try:
        if log_file:
            out = open(log_file, "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", "onionpress.cli", "--data-dir", data_dir,
             "regenerate-derivatives"],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        log(f"Image derivatives: could not start background regeneration: {e} "
            f"— the next launcher start will")
        return None
    finally:
        if out is not subprocess.DEVNULL:
            out.close()
    log(f"Image derivatives: background regeneration started (pid {proc.pid})")
    return proc.pid
