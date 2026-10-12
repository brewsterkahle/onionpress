#!/usr/bin/env python3
"""
Move the OnionHeaven hub data off the volume WordPress shares.

The OnionHeaven registry (registry.db) and every registrant's onion service
key (keys/<address>/) used to live in onionheaven/ on
onionpress-persistent-data, mounted at /var/lib/onionpress — which the
wordpress container mounts too, and www-data owns. They now live on their
own volume, onionpress-onionheaven-data, mounted at /var/lib/onionheaven in
onionpress-tor, onionheaven and the takeover workers, and nowhere else.

The tor and onionheaven entrypoints run this before anything opens the
registry. Every start runs it; once the data has moved it has little to do.

  * Only between mount points. Without the new volume (a compose file older
    than this image) the data would land in the container's writable layer
    and go with it; without the old one, "nothing there" proves nothing.
    Either way nothing moves and the volume is not marked.
  * Only what older versions wrote there moves (SCHEMA): the registry,
    keys/<address>.onion/ with its key files and hostname, the hub's logs
    and flag files. WordPress can write to the volume the old directory is
    on, and can replace that directory, so it is walked through directory
    descriptors with O_NOFOLLOW and never deeper than that layout. Symlinks
    are never followed or copied, and nothing else there (other names,
    deeper trees, special files, files over their class's LIMITS or with
    holes in them) is ever copied.
  * SQLite never opens a path at the old location: a link planted beside
    the database (its -shm) would have root's SQLite write through it. The
    database and its write-ahead log are copied by descriptor into a private
    folder on the new volume, with the log's header unchanged around the
    copy, so no writer restarted the log meanwhile and the pair is one
    state even while an older container still writes to it. SQLite makes
    one database file of that copy (Connection.backup). A registry that is
    no sound SQLite database moves as it is.
  * Copies are root's: 0700 folders, 0600 files.
  * An old file is removed only after the copy has been verified and synced
    to disk, and only if it still has the bytes that were copied (the
    database together with its log, or neither). One that changed stays at
    the old location, and the next run (the other container's, seconds
    later, or the next start) keeps it as a later arrival.
  * First move (no marker yet): the data goes into place, a log or journal
    before its database. Anything already on the volume is kept under
    .pre-migration-<time>/, except empty files and directories (a status
    query's sqlite3 leaves a 0-byte registry.db).
  * After that: what turns up in the old directory again is kept under
    legacy-leftover-<time>/ on the volume and not used, if it fits beside a
    reserve of free space; everything else there is removed. A registry
    written there after the move means an older OnionPress ran on this data
    since, and gets a loud warning (never downgrade a hub across this move:
    docs/DOCKER-SOCKET.md).
  * Kept copies hold registrants' private keys, so they do not stay: a key
    the volume holds with the same bytes is dropped from them, every
    unregistration drops its key from them (onionheaven_common), and each
    copy is removed after KEEP_DAYS days.
  * onionpress-tor and onionheaven start together; a lock on the volume
    makes the second one wait for the first.

Exit status 0: start (moved, nothing to move, or failed with nothing at
stake: no registrant's key in the old directory, or the data in use moved
earlier and only later arrivals are left there). A start after a failure
with nothing at stake marks the volume as well, so a later run never sets
aside what the hub has written to the volume meanwhile. Exit 1: the first
move failed while keys are at stake; nothing was removed, do not start yet.

Usage: onionheaven-data-migrate.py [--legacy-parent DIR] [--dest DIR]
                                   [--no-mount-check]
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

LEGACY_PARENT = "/var/lib/onionpress"
LEGACY_NAME = "onionheaven"
DEST = "/var/lib/onionheaven"

MARKER = ".migrated-from-persistent-data"
LOCK = ".migrate.lock"
STAGING = ".migrating"
PRIVATE = ".registry-as-read"   # in STAGING: the registry's files, for SQLite
PRE_MIGRATION_PREFIX = ".pre-migration-"
LEFTOVER_PREFIX = "legacy-leftover-"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

ATTEMPTS = 5
RETRY_DELAY = 2
FILE_ATTEMPTS = 5
FILE_RETRY_DELAY = 0.2

KEEP_DAYS = 30                  # kept copies are removed after this
MAX_DEPTH = 32                  # what is removed at the old location: no deeper
FIRST_MOVE_MARGIN = 64 << 20    # free space the first move leaves at least
LEFTOVER_RESERVE = 256 << 20    # later arrivals are kept only beside this much
LEFTOVER_RESERVE_SHARE = 0.05   # ... or this share of the volume, if more

DB = "registry.db"
WAL = DB + "-wal"
JOURNAL = DB + "-journal"
SHM = DB + "-shm"
REGISTRY = (DB, WAL, JOURNAL)
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_BYTES = 32

ONION_DIR_RE = re.compile(r"[a-z2-7]{56}\.onion")
LOG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.log")
KEYS_COPY_RE = re.compile(r"keys(-[0-9]+)?")        # keys/ in a kept copy

# The secret halves of a registrant's key: what puts keys "at stake".
KEY_SECRETS = ("ks_hs_id.ed25519_expanded_private", "hs_ed25519_secret_key",
               "ks_hs_id.ed25519_expanded_private.enc", "hs_ed25519_secret_key.enc")
KEY_FILES = KEY_SECRETS + ("hs_ed25519_public_key", "hs_ed25519_public_key.enc")

# What older versions wrote in onionheaven/, and nothing else: a name or a
# pattern, and either the class of a regular file or a folder's own layout.
SCHEMA = (
    (DB, "db"), (WAL, "db"), (JOURNAL, "db"), (SHM, "shm"),
    ("activate", "small"), ("client_registered", "small"),
    ("master-key.json", "small"),
    (LOG_RE, "log"),
    ("keys", (
        (ONION_DIR_RE, tuple((name, "key") for name in KEY_FILES)
                       + (("hostname", "small"),)),
    )),
)
# The most a file of each class may hold; a larger one is never copied.
LIMITS = {"db": 4 << 30, "key": 64 << 10, "small": 64 << 10, "log": 256 << 20}
# Copied in this order: what a registrant cannot send again first.
CLASS_ORDER = ("key", "small", "db", "log")

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


def log(msg):
    # One line each: names at the old location are WordPress's to choose.
    msg = re.sub(r"[\x00-\x1f\x7f]+", " ", str(msg))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] onionheaven-data-migrate: {msg}\n")
    sys.stderr.flush()


class Unstable(Exception):
    """The old data changed while it was being copied."""


class Rejected(Exception):
    """A file that is never copied: larger than its class allows, or sparse."""


def _stamp():
    return datetime.now(timezone.utc).strftime(STAMP_FORMAT)


def _unescape_mountinfo(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def is_mount_point(path, mountinfo="/proc/self/mountinfo"):
    """True if path is a mount point (a volume in a container).

    Read from /proc/self/mountinfo: comparing st_dev with the parent, as
    os.path.ismount does, says no when the volume and the image layer share
    a filesystem (Docker's vfs storage driver).
    """
    real = os.path.realpath(path)
    try:
        with open(mountinfo, encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if len(fields) > 4 and _unescape_mountinfo(fields[4]) == real:
                    return True
        return False
    except OSError:
        return os.path.ismount(real)


# ---------------------------------------------------------------------------
# Ownership: everything this script makes on the volume is root's
# ---------------------------------------------------------------------------

def _own(fd, mode):
    os.fchmod(fd, mode)
    try:
        os.fchown(fd, 0, 0)
    except PermissionError:
        if os.geteuid() == 0:
            raise


def _own_path(path, mode):
    fd = os.open(path, (DIR_FLAGS if mode == 0o700 else FILE_FLAGS))
    try:
        _own(fd, mode)
    finally:
        os.close(fd)


def _mkdir(path):
    os.mkdir(path, 0o700)
    _own_path(path, 0o700)


# ---------------------------------------------------------------------------
# The old tree, through directory file descriptors
# ---------------------------------------------------------------------------

def _lookup(layout, name):
    """What layout says name is (a file class or a folder's layout), or None."""
    for match, value in layout:
        if name == match if isinstance(match, str) else match.fullmatch(name):
            return value
    return None


def _open_regular(dir_fd, name):
    """Open name in dir_fd for reading; None unless it is a regular file."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENXIO):
            return None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _has_holes(fd, size):
    """True if the file has a hole before its end (it is sparse).

    Asked with SEEK_HOLE, not by comparing st_blocks with st_size: on a
    compressing filesystem (btrfs's default on some distributions) a file
    with no hole in it takes fewer blocks than its size too.
    """
    if size == 0 or not hasattr(os, "SEEK_HOLE"):
        return False
    try:
        return os.lseek(fd, 0, os.SEEK_HOLE) < size
    except OSError:
        return False
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass


def _read_digest(fd, limit):
    """(bytes, sha256) of what fd holds; sha256 None past limit bytes."""
    h, n = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return n, h.hexdigest()
        n += len(chunk)
        if n > limit:
            return n, None
        h.update(chunk)


def _fingerprint(dir_fd, name, limit):
    """(bytes, sha256) of the regular file name in dir_fd now, or None."""
    fd = _open_regular(dir_fd, name)
    if fd is None:
        return None
    try:
        return _read_digest(fd, limit)
    finally:
        os.close(fd)


def _hash_path(path):
    fd = os.open(path, FILE_FLAGS)
    try:
        return _read_digest(fd, float("inf"))[1]
    finally:
        os.close(fd)


class _Tree:
    """What the old directory holds, sorted by what happens to it."""

    def __init__(self):
        self.files = []       # (path, class, size): SCHEMA files to copy
        self.registry = {}    # name -> (size, mtime): registry.db, its log, journal
        self.dirs = []        # SCHEMA folders, parents first
        self.links = []       # symlinks: never followed, removed
        self.shm = []         # SQLite's -shm index: never copied, rebuilt
        self.others = []      # anything else: never copied


def _open_dir_path(root_fd, rel):
    """Open directory rel below root_fd one component at a time, no symlinks."""
    fd = os.dup(root_fd)
    for part in [p for p in rel.split("/") if p]:
        try:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nxt
    return fd


def _scan(legacy_fd):
    """Walk the old directory along SCHEMA, breadth first, no deeper."""
    tree = _Tree()
    pending = [("", SCHEMA)]
    while pending:
        rel, layout = pending.pop(0)
        try:
            dir_fd = _open_dir_path(legacy_fd, rel)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                continue        # replaced since it was listed: not copied
            raise
        try:
            for name in sorted(os.listdir(dir_fd)):
                path = f"{rel}/{name}" if rel else name
                try:
                    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = _lookup(layout, name)
                if stat.S_ISLNK(st.st_mode):
                    tree.links.append(path)
                elif isinstance(kind, tuple) and stat.S_ISDIR(st.st_mode):
                    tree.dirs.append(path)
                    pending.append((path, kind))
                elif isinstance(kind, str) and stat.S_ISREG(st.st_mode):
                    # What it takes on disk, which is what its copy takes:
                    # holes stay holes (_copy_out).
                    used = min(st.st_size, st.st_blocks * 512)
                    if kind == "shm":
                        tree.shm.append(path)
                    elif kind == "db":
                        tree.registry[name] = (st.st_size, st.st_mtime, used)
                    else:
                        tree.files.append((path, kind, st.st_size, used))
                else:
                    tree.others.append(path)
        finally:
            os.close(dir_fd)
    if DB not in tree.registry:
        # A log or journal without its database is no registry.
        tree.files += [(name, "db", size, used) for name, (size, _, used) in
                       sorted(tree.registry.items())]
        tree.registry = {}
    return tree


_ZEROS = bytes(1 << 16)


def _copy_out(src, target, limit):
    """Copy the open file src into target (new, root's, 0600), at most limit
    bytes. Returns (bytes, sha256). Runs of zeros become holes in the copy:
    a sparse file never takes more room on the volume than it did where it
    was."""
    st = os.fstat(src)
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                  0o600)
    h, n = hashlib.sha256(), 0
    try:
        while True:
            chunk = os.read(src, 1 << 16)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                raise Rejected(f"grew past {limit} bytes while being copied")
            h.update(chunk)
            if chunk == _ZEROS[:len(chunk)]:
                os.lseek(out, len(chunk), os.SEEK_CUR)
                continue
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
        os.ftruncate(out, n)
        _own(out, 0o600)
        os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        os.close(out)
        os.unlink(target)
        raise
    os.close(out)
    return n, h.hexdigest()


def _version(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _copy_file(dir_fd, name, target, limit, settle=True, empty_ok=True,
               holes_ok=False):
    """Copy the regular file name in dir_fd to target.

    Returns the (bytes, sha256) copied, or None if name is gone or no longer
    a regular file. With settle, only a copy of a file that did not change
    meanwhile counts (same inode, size, times and bytes afterwards):
    FILE_ATTEMPTS tries, then Unstable. A key is rewritten in place
    (truncated, then written), so a copy can catch it empty or half
    written; without empty_ok an empty copy never counts either.
    Raises Rejected for a file over limit, or, without holes_ok, with holes
    in it. (A database may have some: a compressing filesystem such as ZFS
    keeps all-zero blocks as holes. Its copy keeps them too.)
    """
    for attempt in range(FILE_ATTEMPTS):
        src = _open_regular(dir_fd, name)
        if src is None:
            return None
        try:
            st = os.fstat(src)
            if st.st_size > limit:
                raise Rejected(f"larger than {limit} bytes")
            if not holes_ok and _has_holes(src, st.st_size):
                raise Rejected("sparse")
            got = _copy_out(src, target, limit)
        finally:
            os.close(src)
        if not settle:
            return got
        if (got[0] or empty_ok) and _fingerprint(dir_fd, name, limit) == got:
            try:
                now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if now is not None and _version(now) == _version(st):
                return got
        os.unlink(target)
        if attempt + 1 < FILE_ATTEMPTS:
            time.sleep(FILE_RETRY_DELAY)
    raise Unstable(f"{name} kept changing while being copied")


# ---------------------------------------------------------------------------
# The registry: copied as one state, made one file by SQLite on the volume
# ---------------------------------------------------------------------------

def _identity(dir_fd, name):
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (st.st_mode, st.st_dev, st.st_ino)


def _wal_header(dir_fd):
    """(inode, first bytes) of the write-ahead log, or None. A writer that
    restarts the log rewrites this header (new salts, next checkpoint
    number), and a new log file is a new inode with random salts."""
    fd = _open_regular(dir_fd, WAL)
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino, os.pread(fd, WAL_HEADER_BYTES, 0))
    finally:
        os.close(fd)


def _registry_state(dir_fd, wal_mode):
    """What must not change while the registry is copied.

    WAL mode, with a log: the log's header and the database's inode. Pages
    a checkpoint writes into the database meanwhile are in the log too, and
    SQLite reads them from there. Otherwise (no log, or rollback mode): the
    bytes of every file, since the database itself is written then.
    """
    head = _wal_header(dir_fd)
    if wal_mode and head is not None:
        return ("wal", head, _identity(dir_fd, DB))
    state = ["bytes"]
    for name in REGISTRY:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        state.append((st and _version(st), _fingerprint(dir_fd, name, LIMITS["db"])))
    return tuple(state)


def _copy_registry(legacy_fd, private, with_journal):
    """One try at copying the registry's files into private as one state.

    Returns {name: (bytes, sha256)} copied, {} if there is no database, or
    None if a writer changed it meanwhile. Raises Rejected.
    """
    db = _open_regular(legacy_fd, DB)
    if db is None:
        return {}
    try:
        head = os.pread(db, 20, 0)
    finally:
        os.close(db)
    wal_mode = head[:16] == SQLITE_HEADER and head[18:19] == b"\x02"
    before = _registry_state(legacy_fd, wal_mode)
    copied = {}
    # A rollback journal beside a WAL-mode database is stale, and SQLite
    # would roll it back into the database: never next to the copy.
    for name in REGISTRY if with_journal or not wal_mode else (DB, WAL):
        got = _copy_file(legacy_fd, name, os.path.join(private, name),
                         LIMITS["db"], settle=False, holes_ok=True)
        if got is not None:
            copied[name] = got
    if _registry_state(legacy_fd, wal_mode) != before or DB not in copied:
        return None
    if before[0] == "wal":
        # The log copied is the one whose header was checked.
        if WAL not in copied:
            return None
        with open(os.path.join(private, WAL), "rb") as f:
            if f.read(WAL_HEADER_BYTES) != before[1][2]:
                return None
    return copied


def _open_untrusted(path):
    """SQLite on a database WordPress may have written: no triggers, views
    or schema-defined functions run, damage is reported, not trusted."""
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    if hasattr(conn, "setconfig"):
        for name, value in (("SQLITE_DBCONFIG_DEFENSIVE", True),
                            ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
                            ("SQLITE_DBCONFIG_ENABLE_TRIGGER", False),
                            ("SQLITE_DBCONFIG_ENABLE_VIEW", False)):
            op = getattr(sqlite3, name, None)
            if op is not None:
                conn.setconfig(op, value)
    for pragma in ("trusted_schema=OFF", "cell_size_check=ON", "mmap_size=0"):
        conn.execute(f"PRAGMA {pragma}")
    return conn


def _consolidate(private, target):
    """Write target as one database file from the copy in private (the
    database with its log). Returns "ok", "not-sqlite", or "damaged"."""
    with open(os.path.join(private, DB), "rb") as f:
        if f.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            return "not-sqlite"
    try:
        src = _open_untrusted(os.path.join(private, DB))
        try:
            check = src.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                log(f"SQLite finds the registry as copied damaged "
                    f"({str(check[0][0])[:120] if check else '?'})")
                return "damaged"
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        log(f"SQLite cannot read the registry as copied ({e})")
        if os.path.lexists(target):
            os.unlink(target)
        return "damaged"
    _own_path(target, 0o600)
    return "ok"


def _stage_registry(legacy_fd, staging, consolidate):
    """Copy the registry into staging. Returns ({old name: (bytes, sha256)}
    copied, {staged name: sha256}). Raises Rejected, or Unstable if the last
    try did not catch it in one state."""
    private = os.path.join(staging, PRIVATE)
    target = os.path.join(staging, DB)
    copied, made = None, "not-sqlite"
    try:
        for attempt in range(FILE_ATTEMPTS):
            if os.path.lexists(private):
                shutil.rmtree(private)
            _mkdir(private)
            copied = _copy_registry(legacy_fd, private, with_journal=not consolidate)
            if copied == {}:
                return {}, {}
            if copied is not None:
                if not consolidate:
                    break               # a later arrival: kept as it is
                made = _consolidate(private, target)
                if made != "damaged" or attempt + 1 == FILE_ATTEMPTS:
                    break
                # Damaged perhaps only as read: a writer the checks missed.
            if attempt + 1 < FILE_ATTEMPTS:
                time.sleep(FILE_RETRY_DELAY)
        if copied is None:
            raise Unstable("the registry kept changing while being copied")
        if consolidate and made == "ok":
            return copied, {DB: _hash_path(target)}
        if consolidate:
            log(f"WARNING: {DB} at the old location is not a sound SQLite "
                f"database; moved as it is, bytes unchanged")
        for name in copied:
            os.rename(os.path.join(private, name), os.path.join(staging, name))
        return copied, {name: got[1] for name, got in copied.items()}
    finally:
        if os.path.lexists(private):
            shutil.rmtree(private)


def _registry_unchanged(legacy_fd, copied):
    """True if every file of the registry at the old location is still the
    one copied (and none appeared)."""
    for name in REGISTRY:
        now = _fingerprint(legacy_fd, name, LIMITS["db"])
        if now != copied.get(name):
            return False
    return True


# ---------------------------------------------------------------------------
# Copying the tree
# ---------------------------------------------------------------------------

def _stage(legacy_fd, staging, tree, first, margin):
    """Copy what tree lists into staging, the registry first, logs only
    while margin bytes stay free beside them.

    Returns (copied, registry, staged, left, rejected): {old path: (bytes,
    sha256)} of files copied, the same for the registry's files, {staged
    path: sha256}, [(path, why)] not copied, and the paths rejected (never
    copied: too large or sparse).

    On the first move a file that cannot be read fails the move; later
    arrivals are skipped instead.
    """
    copied, staged, left, rejected = {}, {}, [], []
    registry = {}
    if tree.registry:
        try:
            registry, got = _stage_registry(legacy_fd, staging, consolidate=first)
            staged.update(got)
        except Rejected as e:
            left.append((DB, str(e)))
            rejected += sorted(tree.registry)
    for path, kind, size, _ in sorted(tree.files,
                                      key=lambda f: (CLASS_ORDER.index(f[1]), f[0])):
        if first and kind == "db":
            # SQLite would apply a log to whatever database is created beside it.
            left.append((path, "a write-ahead log or journal without its database"))
            continue
        if kind == "log" and size + margin > _free_bytes(staging):
            # Logs only while there is room; any number of them can be there.
            left.append((path, "no room for it on the volume"))
            continue
        parent, _, name = path.rpartition("/")
        target_dir = staging
        for part in [p for p in parent.split("/") if p]:
            target_dir = os.path.join(target_dir, part)
            if not os.path.isdir(target_dir):
                _mkdir(target_dir)
        try:
            dir_fd = _open_dir_path(legacy_fd, parent)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                continue
            if first:
                raise
            left.append((path, os.strerror(e.errno)))
            continue
        try:
            got = _copy_file(dir_fd, name, os.path.join(target_dir, name),
                             LIMITS[kind], settle=(kind != "log"),
                             empty_ok=(kind != "key"), holes_ok=(kind == "db"))
        except Rejected as e:
            left.append((path, str(e)))
            rejected.append(path)
            continue
        except Unstable as e:
            left.append((path, str(e)))
            continue
        except OSError as e:
            if first:
                raise
            left.append((path, os.strerror(e.errno) if e.errno else str(e)))
            continue
        finally:
            os.close(dir_fd)
        if got is not None:
            copied[path] = got
            staged[path] = got[1]
    return copied, registry, staged, left, rejected


def _prune_empty_dirs(root):
    """Remove the folders under root that hold no file (made for a copy that
    was then skipped)."""
    for r, dirs, files in os.walk(root, topdown=False):
        if r != root and not os.listdir(r):
            os.rmdir(r)


def _verify(staging, staged):
    """The staged copy holds what was read, root's, 0700/0600."""
    root = os.geteuid() == 0
    for r, dirs, files in os.walk(staging):
        for name in dirs:
            st = os.lstat(os.path.join(r, name))
            if (not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700
                    or (root and (st.st_uid, st.st_gid) != (0, 0))):
                raise RuntimeError(f"the copy of {name} is not a private directory")
    for path, digest in staged.items():
        full = os.path.join(staging, path)
        st = os.lstat(full)
        if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600
                or (root and (st.st_uid, st.st_gid) != (0, 0))
                or _hash_path(full) != digest):
            raise RuntimeError(f"the copy of {path} does not match what was read")


def _count(root):
    """(files, registrants' key directories) under root."""
    files = keys = 0
    for r, _, names in os.walk(root):
        rel = os.path.relpath(r, root)
        files += len(names)
        if names and rel.startswith("keys" + os.sep) and rel.count(os.sep) == 1:
            keys += 1
    return files, keys


# ---------------------------------------------------------------------------
# Removing at the old location
# ---------------------------------------------------------------------------

def _in_parent(legacy_fd, path, action):
    """Run action(parent_fd, name) for path; False if that fails."""
    parent, _, name = path.rpartition("/")
    try:
        pfd = _open_dir_path(legacy_fd, parent)
    except OSError:
        return False
    try:
        return action(pfd, name)
    except OSError:
        return False
    finally:
        os.close(pfd)


def _unlink_if(got):
    def action(pfd, name):
        if _fingerprint(pfd, name, got[0]) != got:
            return False
        os.unlink(name, dir_fd=pfd)
        return True
    return action


def _unlink_symlink(pfd, name):
    if not stat.S_ISLNK(os.stat(name, dir_fd=pfd, follow_symlinks=False).st_mode):
        return False
    os.unlink(name, dir_fd=pfd)
    return True


def _unlink_regular(pfd, name):
    if not stat.S_ISREG(os.stat(name, dir_fd=pfd, follow_symlinks=False).st_mode):
        return False
    os.unlink(name, dir_fd=pfd)
    return True


def _rmdir(pfd, name):
    os.rmdir(name, dir_fd=pfd)
    return True


def _remove_tree(pfd, name):
    """Remove name in pfd and everything below it: never following a link,
    never more than MAX_DEPTH folders down, no recursion. True if it is
    gone."""
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=pfd)
        return True
    def opened(parent, child):
        fd = os.open(child, DIR_FLAGS, dir_fd=parent)
        try:
            return (fd, child, sorted(os.listdir(fd)))
        except BaseException:
            os.close(fd)
            raise

    complete = True
    stack = []                  # (fd, its name in the folder above, names left)
    try:
        stack.append(opened(pfd, name))
        while stack:
            fd, dname, names = stack[-1]
            if not names:
                stack.pop()
                os.close(fd)
                try:
                    os.rmdir(dname, dir_fd=stack[-1][0] if stack else pfd)
                except OSError:
                    complete = False
                continue
            child = names.pop()
            try:
                cst = os.stat(child, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(cst.st_mode):
                try:
                    os.unlink(child, dir_fd=fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    complete = False
                continue
            if len(stack) >= MAX_DEPTH:
                complete = False
                continue
            try:
                stack.append(opened(fd, child))
            except OSError:
                complete = False
    finally:
        for fd, _, _ in stack:
            os.close(fd)
    return complete


def _remove_old(parent_fd, legacy_fd, tree, copied, registry, later, rejected):
    """Remove from the old location what was copied and is unchanged, the
    registry's files only all together, links, and (later arrivals only)
    whatever else is there. Returns the copied paths that stay."""
    kept = [path for path, got in copied.items()
            if not _in_parent(legacy_fd, path, _unlink_if(got))]
    if registry:
        if _registry_unchanged(legacy_fd, registry):
            for name in REGISTRY:
                _in_parent(legacy_fd, name, _unlink_regular)
        else:
            kept += sorted(registry)
    for path in tree.links:
        _in_parent(legacy_fd, path, _unlink_symlink)
    if later:
        for path in tree.others + rejected:
            if not _in_parent(legacy_fd, path, _remove_tree):
                log(f"could not remove all of {path} at the old location")
    if _identity(legacy_fd, DB) is None:
        # The index of a database that is gone; one still there may be in use.
        for path in tree.shm:
            _in_parent(legacy_fd, path, _unlink_regular)
    for path in sorted(tree.dirs, key=lambda p: p.count("/"), reverse=True):
        _in_parent(legacy_fd, path, _rmdir)
    try:
        os.rmdir(LEGACY_NAME, dir_fd=parent_fd)
    except OSError:
        pass
    return kept


# ---------------------------------------------------------------------------
# The volume
# ---------------------------------------------------------------------------

def _is_empty(path):
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        return not os.listdir(path)
    return stat.S_ISREG(st.st_mode) and st.st_size == 0


def _remove_path(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _total_bytes(path):
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize


def _write_marker(dest, info):
    tmp = os.path.join(dest, MARKER + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, os.path.join(dest, MARKER))
    _fsync_dir(dest)


def _moved_at(dest):
    """When the marker says the data moved (UTC), or None."""
    path = os.path.join(dest, MARKER)
    try:
        with open(path, encoding="utf-8") as f:
            stamp = json.load(f)["migrated_at"]
        return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
        except OSError:
            return None


def _unique(path):
    """path, or path-2, path-3, ... whichever does not exist yet."""
    candidate, n = path, 1
    while os.path.lexists(candidate):
        n += 1
        candidate = f"{path}-{n}"
    return candidate


def _ours(name):
    return (name in (MARKER, MARKER + ".tmp", LOCK, STAGING)
            or name.startswith(PRE_MIGRATION_PREFIX)
            or name.startswith(LEFTOVER_PREFIX))


def _placing_order(name):
    """A write-ahead log or journal goes in before its database: a reader
    that opened the database in between would make a log of its own and
    delete it, by name, on closing (by then the one just put in place)."""
    if name.endswith(("-wal", "-journal")):
        return (0, name)
    if name == DB:
        return (2, name)
    return (1, name)


def _put_in_place(dest, staging, stamp):
    """Move the staged data into the volume root; keep what was there."""
    aside = _unique(os.path.join(dest, PRE_MIGRATION_PREFIX + stamp))

    def keep(path, name):
        if not os.path.isdir(aside):
            _mkdir(aside)
        os.rename(path, _unique(os.path.join(aside, name)))

    def set_aside(name):
        path = os.path.join(dest, name)
        if _is_empty(path):
            _remove_path(path)
            return
        keep(path, name)
        log(f"kept {name}, which was already on the volume, in "
            f"{os.path.basename(aside)}/")

    for name in sorted(os.listdir(dest)):
        if not _ours(name):
            set_aside(name)
    for name in sorted(os.listdir(staging), key=_placing_order):
        source = os.path.join(staging, name)
        if _ours(name):
            # A name this script uses for itself: kept, never in the root.
            keep(source, name)
            continue
        target = os.path.join(dest, name)
        for _ in range(3):
            # Something may have created it meanwhile (a status query's
            # sqlite3, a restore): keep that too, never replace it.
            if os.path.lexists(target):
                set_aside(name)
            try:
                os.rename(source, target)
                break
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR,
                                   errno.ENOTDIR):
                    raise
        else:
            raise RuntimeError(f"could not put {name} in place")
    os.rmdir(staging)
    _fsync_dir(dest)


# ---------------------------------------------------------------------------
# Kept copies: no registrant's key stays in them for long
# ---------------------------------------------------------------------------

def _plain_dir(path):
    return os.path.isdir(path) and not os.path.islink(path)


def _same_files(mine, theirs):
    """True if every entry of mine is a regular file that theirs holds with
    the same bytes."""
    if not _plain_dir(theirs):
        return False
    names = os.listdir(mine)
    for name in names:
        a, b = os.path.join(mine, name), os.path.join(theirs, name)
        if (not stat.S_ISREG(os.lstat(a).st_mode) or not os.path.lexists(b)
                or not stat.S_ISREG(os.lstat(b).st_mode)
                or _hash_path(a) != _hash_path(b)):
            return False
    return bool(names)


def _drop_duplicate_keys(root, live):
    """Remove each keys*/<address>/ under root whose files the volume's
    keys/<address>/ holds with the same bytes. Returns how many."""
    dropped = 0
    for keys in sorted(os.listdir(root)):
        keys_dir = os.path.join(root, keys)
        if not KEYS_COPY_RE.fullmatch(keys) or not _plain_dir(keys_dir):
            continue
        for address in sorted(os.listdir(keys_dir)):
            mine = os.path.join(keys_dir, address)
            if (ONION_DIR_RE.fullmatch(address) and _plain_dir(mine)
                    and _same_files(mine, os.path.join(live, "keys", address))):
                shutil.rmtree(mine)
                dropped += 1
        if not os.listdir(keys_dir):
            os.rmdir(keys_dir)
    return dropped


def _kept_since(name):
    """When a kept copy was made, from its name (UTC), or None."""
    for prefix in (PRE_MIGRATION_PREFIX, LEFTOVER_PREFIX):
        if name.startswith(prefix):
            try:
                made = datetime.strptime(name[len(prefix):len(prefix) + 16], STAMP_FORMAT)
            except ValueError:
                return None
            return made.replace(tzinfo=timezone.utc)
    return None


def tidy_kept(dest, now=None):
    """Drop keys the volume holds the same from the kept copies, and the
    copies older than KEEP_DAYS days."""
    now = now or datetime.now(timezone.utc)
    for name in sorted(os.listdir(dest)):
        path = os.path.join(dest, name)
        if not name.startswith((PRE_MIGRATION_PREFIX, LEFTOVER_PREFIX)) or not _plain_dir(path):
            continue
        made = _kept_since(name)
        if made is not None and now - made > timedelta(days=KEEP_DAYS):
            shutil.rmtree(path)
            log(f"removed {name}/, kept since {made:%Y-%m-%d}: copies are kept "
                f"{KEEP_DAYS} days")
            continue
        dropped = _drop_duplicate_keys(path, dest)
        if dropped:
            log(f"dropped {dropped} registrants' keys from {name}/: the volume "
                f"holds the same")
        if not os.listdir(path):
            os.rmdir(path)


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------

def _legacy_dir_fd(parent_fd):
    """Open the old onionheaven/ directory, or None if there is none.

    Anything that is not a directory there is not ours: a symlink is
    removed (never followed), anything else is left alone.
    """
    try:
        return os.open(LEGACY_NAME, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
    st = os.stat(LEGACY_NAME, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        os.unlink(LEGACY_NAME, dir_fd=parent_fd)
        log(f"removed a symlink at the old location ({LEGACY_NAME}); not followed")
    else:
        log(f"the old location ({LEGACY_NAME}) is not a directory; left alone")
    return None


def _report(tree, left, kept, later):
    for path, why in left:
        log(f"did not copy {path} ({why})")
    if kept:
        log(f"{len(kept)} entries changed after they were copied and stay at the "
            f"old location for now; the next run (the other container's, or the "
            f"next start) keeps them aside (first: {kept[0]})")
    if tree.others and later:
        log(f"removed {len(tree.others)} entries at the old location that are "
            f"not OnionHeaven data (first: {tree.others[0]})")
    elif tree.others:
        log(f"left {len(tree.others)} entries at the old location that are not "
            f"OnionHeaven data (first: {tree.others[0]}); never copied, and "
            f"removed on a later start")


def _room_needed(tree, registry_copies):
    """Bytes the registry, keys and flag files take on the volume (what
    is over its limit is never copied; logs are copied only while there is
    room, _stage). The registry is copied as read, and on the first move
    SQLite also writes it out whole: holes and all."""
    need = sum(used for _, kind, size, used in tree.files
               if kind in ("key", "small") and size <= LIMITS[kind])
    for size, _, used in tree.registry.values():
        if size <= LIMITS["db"]:
            need += used + (size if registry_copies > 1 else 0)
    return need


def _first_move(parent_fd, legacy_fd, dest, staging, tree, stamp):
    need = _room_needed(tree, registry_copies=2)
    free = _free_bytes(dest)
    if need + FIRST_MOVE_MARGIN > free:
        raise OSError(errno.ENOSPC, f"the OnionHeaven data needs {need} bytes and "
                      f"{FIRST_MOVE_MARGIN} to spare on {dest}; {free} are free")
    _mkdir(staging)
    copied, registry, staged, left, _ = _stage(legacy_fd, staging, tree, True,
                                               FIRST_MOVE_MARGIN)
    _prune_empty_dirs(staging)
    _verify(staging, staged)
    if not staged:
        # Only links, special files, or what is never copied: nothing to keep.
        shutil.rmtree(staging)
        _write_marker(dest, {"migrated_at": stamp, "entries": 0})
        log("nothing to move at the old location; marked this volume as current")
    else:
        files, keys = _count(staging)
        os.sync()
        _put_in_place(dest, staging, stamp)
        _write_marker(dest, {"migrated_at": stamp, "entries": len(staged),
                             "files": files, "key_dirs": keys})
        log(f"moved the OnionHeaven data to its own volume: {files} files, "
            f"{keys} registrants' key directories (verified)")
    # Everything copied is on disk before anything at the old location goes.
    os.sync()
    kept = _remove_old(parent_fd, legacy_fd, tree, copied, registry, False, [])
    _report(tree, left, kept, later=False)


def _keep_later_arrivals(parent_fd, legacy_fd, dest, staging, tree, stamp):
    copied, registry, left, rejected = {}, {}, [], []
    need = _room_needed(tree, registry_copies=1)
    reserve = max(LEFTOVER_RESERVE, int(_total_bytes(dest) * LEFTOVER_RESERVE_SHARE))
    if (tree.files or tree.registry) and need + reserve > _free_bytes(dest):
        log(f"WARNING: {len(tree.files) + len(tree.registry)} files ({need} bytes) "
            f"turned up at the old location after the move, more than the volume "
            f"keeps beside {reserve} bytes free; left there, not used")
        kept = _remove_old(parent_fd, legacy_fd, tree, {}, {}, True, [])
        _report(tree, left, kept, later=True)
        return
    if tree.files or tree.registry:
        _mkdir(staging)
        copied, registry, staged, left, rejected = _stage(legacy_fd, staging, tree,
                                                          False, reserve)
        _verify(staging, staged)
        same = _drop_duplicate_keys(staging, dest)
        _prune_empty_dirs(staging)
        files, keys = _count(staging)
        if files:
            leftover = _unique(os.path.join(dest, LEFTOVER_PREFIX + stamp))
            os.rename(staging, leftover)
            _fsync_dir(dest)
            log(f"WARNING: {files} files turned up at the old location after the "
                f"move ({keys} registrants' key directories); kept in "
                f"{os.path.basename(leftover)}/ on the volume, not used. Was an "
                f"older OnionPress container still running?")
            moved = _moved_at(dest)
            written = max((mtime for _, mtime, _ in tree.registry.values()), default=None)
            if registry and moved and written and written > moved.timestamp():
                when = datetime.fromtimestamp(written, timezone.utc)
                log(f"WARNING: the OnionHeaven registry at the old location was "
                    f"written at {when:%Y-%m-%d %H:%M:%S} UTC, after the move "
                    f"({moved:%Y-%m-%d %H:%M:%S} UTC): an older OnionPress used "
                    f"the old location since, so registrations made meanwhile are "
                    f"only in {os.path.basename(leftover)}/{DB}, not in use. Never "
                    f"run an older OnionPress on this hub again; see "
                    f"docs/DOCKER-SOCKET.md, \"The OnionHeaven hub data\"")
        else:
            shutil.rmtree(staging)
        if same:
            log(f"{same} registrants' keys turned up at the old location again, "
                f"the same as on the volume; removed there")
    os.sync()
    kept = _remove_old(parent_fd, legacy_fd, tree, copied, registry, True, rejected)
    _report(tree, left, kept, later=True)


def _migrate_once(parent_fd, dest):
    first = not os.path.exists(os.path.join(dest, MARKER))
    staging = os.path.join(dest, STAGING)
    if os.path.lexists(staging):
        _remove_path(staging)
    legacy_fd = _legacy_dir_fd(parent_fd)
    if legacy_fd is None:
        if first:
            _write_marker(dest, {"migrated_at": _stamp(), "entries": 0})
            log("nothing at the old location; marked this volume as current")
        return
    try:
        tree = _scan(legacy_fd)
        if first:
            _first_move(parent_fd, legacy_fd, dest, staging, tree, _stamp())
        else:
            _keep_later_arrivals(parent_fd, legacy_fd, dest, staging, tree, _stamp())
    finally:
        os.close(legacy_fd)


def _keys_at_stake(legacy_parent):
    """True unless the old location certainly holds no registrant's key: a
    non-empty regular key file in keys/<address>.onion/. Names WordPress
    can make up (keys/x, a folder of the right name with nothing in it, a
    link) are no key."""
    try:
        parent_fd = os.open(legacy_parent, DIR_FLAGS)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    fds = [parent_fd]
    try:
        for name in (LEGACY_NAME, "keys"):
            try:
                fds.append(os.open(name, DIR_FLAGS, dir_fd=fds[-1]))
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    return False
                raise
        for address in os.listdir(fds[-1]):
            if not ONION_DIR_RE.fullmatch(address):
                continue
            try:
                key_fd = os.open(address, DIR_FLAGS, dir_fd=fds[-1])
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    continue
                raise
            try:
                for secret in KEY_SECRETS:
                    try:
                        st = os.stat(secret, dir_fd=key_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                        return True
            finally:
                os.close(key_fd)
        return False
    except OSError:
        return True
    finally:
        for fd in fds:
            os.close(fd)


def migrate(legacy_parent, dest, attempts=ATTEMPTS, delay=RETRY_DELAY,
            legacy_mounted=True):
    if not legacy_mounted:
        log(f"{legacy_parent} is not a volume in this container; nothing moved, "
            f"and this volume is not marked as current")
        return
    try:
        parent_fd = os.open(legacy_parent, DIR_FLAGS)
    except FileNotFoundError:
        parent_fd = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                if parent_fd is None:
                    if not os.path.exists(os.path.join(dest, MARKER)):
                        _write_marker(dest, {"migrated_at": _stamp(), "entries": 0})
                    return
                _migrate_once(parent_fd, dest)
                return
            except Unstable as e:
                staging = os.path.join(dest, STAGING)
                if os.path.lexists(staging):
                    _remove_path(staging)
                if attempt == attempts:
                    raise
                log(f"{e}; trying again ({attempt}/{attempts})")
                time.sleep(delay)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _tidy_quietly(dest):
    try:
        tidy_kept(dest)
    except Exception as e:
        log(f"could not tidy the kept copies: {type(e).__name__}: {e}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--legacy-parent", default=LEGACY_PARENT)
    parser.add_argument("--dest", default=DEST)
    parser.add_argument("--no-mount-check", action="store_true",
                        help="skip the mount point checks (tests)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.dest):
        log(f"{args.dest} does not exist; nothing moved")
        return 0
    if not args.no_mount_check and not is_mount_point(args.dest):
        log(f"WARNING: {args.dest} is not a volume (a compose file older than "
            f"this image?). The OnionHeaven data stays where it is, and what "
            f"this container writes there is lost when it is recreated.")
        return 0
    legacy_mounted = args.no_mount_check or is_mount_point(args.legacy_parent)

    lock_fd = os.open(os.path.join(args.dest, LOCK),
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _own_path(args.dest, 0o700)
        migrate(args.legacy_parent, args.dest, legacy_mounted=legacy_mounted)
        _tidy_quietly(args.dest)
        return 0
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        staging = os.path.join(args.dest, STAGING)
        try:
            if os.path.lexists(staging):
                _remove_path(staging)
        except OSError:
            pass
        if os.path.exists(os.path.join(args.dest, MARKER)):
            # The data in use moved already. What is at the old location now
            # is not used, and WordPress can write there: it must never be
            # able to keep the hub from starting.
            log("the OnionHeaven data moved earlier; what is at the old location "
                "is not used, and the next run tries again to keep it aside")
            _tidy_quietly(args.dest)
            return 0
        if _keys_at_stake(args.legacy_parent):
            log("registrants' keys are still at the old location and nothing was "
                "removed; not starting until they can be moved")
            return 1
        # Starting on this volume: mark it, or the next start's first move
        # would set aside what the hub writes here from now on and put the
        # old data in its place.
        log("no registrant's key at the old location; starting anyway on this "
            "volume, which is marked as current: what is at the old location "
            "is kept aside on a later start, not used")
        if legacy_mounted:
            try:
                _write_marker(args.dest, {"migrated_at": _stamp(), "entries": 0,
                                          "move_failed": f"{type(e).__name__}: {e}"})
            except OSError as e2:
                log(f"could not mark this volume: {e2}")
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
