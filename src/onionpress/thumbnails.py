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

The folder is WordPress's to write (it is mounted into the container), so
anything in it may be a link planted to reach the rest of the Mac (#460).
Nothing here follows one: ``.thumbs`` must be a real folder, the walks never
enter a linked folder, a linked file is no source, and every removal or
write goes through folder fds opened ``O_NOFOLLOW``.
"""

import os
import stat

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


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def thumbs_dir_ok(creations_dir):
    """True when ``.thumbs`` is absent or a real folder (not a link) inside
    *creations_dir*; anything else stops the pass."""
    thumbs_dir = os.path.join(creations_dir, THUMBS_DIRNAME)
    try:
        st = os.lstat(thumbs_dir)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    real = os.path.realpath(creations_dir)
    return os.path.realpath(thumbs_dir).startswith(real + os.sep)


def _existing_thumbs(thumbs_dir):
    """{rel of the source: mtime} for every thumbnail on disk (links too,
    never followed)."""
    found = {}
    for root, _dirs, files, rootfd in os.fwalk(thumbs_dir, follow_symlinks=False):
        for fname in files:
            if not fname.endswith(".png"):
                continue
            try:
                mtime = os.stat(fname, dir_fd=rootfd, follow_symlinks=False).st_mtime
            except OSError:
                continue
            found[os.path.relpath(os.path.join(root, fname), thumbs_dir)[:-4]] = mtime
    return found


def source_ok(creations_dir, src):
    """True when *src* is a regular file (not a link) whose real path is
    inside *creations_dir*. Checked again right before rendering."""
    try:
        if not stat.S_ISREG(os.lstat(src).st_mode):
            return False
    except OSError:
        return False
    real = os.path.realpath(creations_dir)
    return os.path.realpath(src).startswith(real + os.sep)


def scan(creations_dir):
    """One pass: ``(stale, orphans, complete)``.

    *stale* lists ``(src, rel, mtime)`` for each thumbnailable file whose
    thumbnail is missing or older than it; *orphans* lists the thumbnail
    paths whose file is no longer published. *complete* is False when part
    of the folder could not be read, or ``.thumbs`` is not a real folder:
    then *orphans* is empty, since a file that was merely unreadable must
    not lose its thumbnail (and *stale* too, for an unsafe ``.thumbs``).
    """
    if not thumbs_dir_ok(creations_dir):
        return [], [], False
    thumbs_dir = os.path.join(creations_dir, THUMBS_DIRNAME)
    thumbs = _existing_thumbs(thumbs_dir) if os.path.lexists(thumbs_dir) else {}

    errors = []
    present = set()
    stale = []
    # followlinks=False: a linked folder is listed but never entered.
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
            try:
                st = os.lstat(src)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # a link is no source: its thumbnail would show its target
            rel = os.path.relpath(src, creations_dir)
            present.add(rel)
            if not is_thumbnailable(fname):
                continue
            have = thumbs.get(rel)
            if have is None or have < st.st_mtime:
                stale.append((src, rel, st.st_mtime))

    complete = not errors
    orphans = []
    if complete:
        orphans = [os.path.join(thumbs_dir, rel + ".png")
                   for rel in sorted(thumbs) if rel not in present]
    return stale, orphans, complete


def prune(creations_dir, orphans):
    """Remove *orphans* (from :func:`scan`), then any folder under .thumbs/
    they leave empty. Returns how many thumbnails were removed. Removals
    go through the walk's folder fds, so a folder swapped for a link
    since the scan is never entered."""
    if not thumbs_dir_ok(creations_dir):
        return 0
    thumbs_dir = os.path.join(creations_dir, THUMBS_DIRNAME)
    prefix = thumbs_dir + os.sep
    wanted = {os.path.relpath(p, thumbs_dir) for p in orphans
              if p.startswith(prefix) and p.endswith(".png")}
    if not wanted or not os.path.lexists(thumbs_dir):
        return 0
    removed = 0
    for root, dirs, files, rootfd in os.fwalk(thumbs_dir, topdown=False,
                                              follow_symlinks=False):
        for fname in files:
            if os.path.relpath(os.path.join(root, fname), thumbs_dir) not in wanted:
                continue
            try:
                os.unlink(fname, dir_fd=rootfd)
                removed += 1
            except OSError:
                continue
        if removed:
            for d in dirs:
                try:
                    os.rmdir(d, dir_fd=rootfd)  # only succeeds when empty
                except OSError:
                    pass
    return removed


def install(creations_dir, rel, rendered):
    """Put the PNG at *rendered* (in a private folder) at ``.thumbs/<rel>.png``.

    Each folder on the way is made or opened ``O_NOFOLLOW`` from the one
    before, starting at *creations_dir*; the PNG is written to a new file
    there and renamed over the old thumbnail (a link in its place is
    replaced, never followed). Returns False when a folder on the way is
    not a real one.
    """
    parts = [THUMBS_DIRNAME] + rel.split(os.sep)[:-1]
    name = os.path.basename(rel) + ".png"
    if any(p in ("", ".", "..") for p in parts) or name.startswith("."):
        return False
    fd = os.open(creations_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                return False  # a link or a file where a folder should be
            os.close(fd)
            fd = nxt
        tmp = f".{name}.{os.getpid()}.tmp"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o644, dir_fd=fd)
        try:
            with open(rendered, "rb") as f_in, os.fdopen(out, "wb") as f_out:
                out = None
                while chunk := f_in.read(1 << 20):
                    f_out.write(chunk)
            os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            if out is not None:
                os.close(out)
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        return True
    finally:
        os.close(fd)
