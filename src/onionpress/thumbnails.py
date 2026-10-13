"""My Creations thumbnails: which to make, which to remove.

The Mac's menubar app makes a QuickLook thumbnail for every image, video and
PDF in ~/OnionPress/Creations/My Creations, at
``.thumbs/<path of the file>.png``; the Creations pages show those instead of
the full-size originals (onionpress-creations.php, ``THUMBS_PREFIX``).

:func:`scan` is one pass over the folder: the files whose thumbnail is
missing or older than the file, and the thumbnails whose file is gone
(deleted, renamed, or moved into a hidden folder). It follows the plugin's
rule for what is published: no hidden component, and not the legacy
top-level ``blog-archives`` folder (compared without case, as the Mac's disk
does).
"""

import os

THUMBS_DIRNAME = ".thumbs"
LEGACY_SNAPSHOTS_DIR = "blog-archives"

# Only media types QuickLook renders usefully. Data files (*.xml.gz, *.zip,
# *.json, ...) have no useful preview: qlmanage hangs on them for the full
# timeout, and because no thumbnail appears it would be retried forever.
THUMBNAILABLE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".webp", ".pdf",
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm",
})


def is_thumbnailable(name):
    return os.path.splitext(name)[1].lower() in THUMBNAILABLE_EXTS


def thumb_path(creations_dir, rel):
    """Where the thumbnail of *rel* (relative to *creations_dir*) goes."""
    return os.path.join(creations_dir, THUMBS_DIRNAME, rel + ".png")


def _existing_thumbs(thumbs_dir):
    """{rel of the source: mtime} for every thumbnail on disk (links too,
    never followed)."""
    found = {}
    for root, _dirs, files in os.walk(thumbs_dir):
        for fname in files:
            if not fname.endswith(".png"):
                continue
            path = os.path.join(root, fname)
            try:
                found[os.path.relpath(path, thumbs_dir)[:-4]] = os.lstat(path).st_mtime
            except OSError:
                continue
    return found


def scan(creations_dir):
    """One pass: ``(stale, orphans, complete)``.

    *stale* lists ``(src, rel, mtime)`` for each thumbnailable file whose
    thumbnail is missing or older than it; *orphans* lists the thumbnail
    paths whose file is no longer published. *complete* is False when part
    of the folder could not be read: then *orphans* is empty, since a file
    that was merely unreadable must not lose its thumbnail.
    """
    thumbs_dir = os.path.join(creations_dir, THUMBS_DIRNAME)
    thumbs = _existing_thumbs(thumbs_dir) if os.path.isdir(thumbs_dir) else {}

    errors = []
    present = set()
    stale = []
    for root, dirs, files in os.walk(creations_dir, onerror=errors.append):
        top = root == creations_dir
        # Hidden folders (.thumbs/, .git/ ...) are neither published nor
        # walked, nor is the top-level legacy snapshot folder.
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and not (top and d.lower() == LEGACY_SNAPSHOTS_DIR)]
        for fname in files:
            if fname.startswith("."):
                continue
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, creations_dir)
            present.add(rel)
            if not is_thumbnailable(fname):
                continue
            try:
                mtime = os.stat(src).st_mtime
            except OSError:
                continue
            have = thumbs.get(rel)
            if have is None or have < mtime:
                stale.append((src, rel, mtime))

    complete = not errors
    orphans = []
    if complete:
        orphans = [os.path.join(thumbs_dir, rel + ".png")
                   for rel in sorted(thumbs) if rel not in present]
    return stale, orphans, complete


def prune(creations_dir, orphans):
    """Remove *orphans* (from :func:`scan`), then any folder under .thumbs/
    they leave empty. Returns how many thumbnails were removed."""
    thumbs_dir = os.path.join(creations_dir, THUMBS_DIRNAME)
    prefix = thumbs_dir + os.sep
    removed = 0
    for path in orphans:
        if not path.startswith(prefix) or not path.endswith(".png"):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        for root, _dirs, _files in os.walk(thumbs_dir, topdown=False):
            if root == thumbs_dir:
                continue
            try:
                os.rmdir(root)  # only succeeds when empty
            except OSError:
                pass
    return removed
