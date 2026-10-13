#!/usr/bin/env python3
"""
Move OnionHome's name registry off the volume WordPress shares.

onionnames.db (which onionname points at which onion address) lived in
onionhome/ on onionpress-persistent-data, mounted at /var/lib/onionpress,
which the wordpress container mounts read-write. www-data owns that folder
(and fix_onionpress_permissions gave it onionhome/ too), so code running in
WordPress could rewrite, rename or replace anything there. The registry now
lives on onionpress-onionnames-data, mounted at /var/lib/onionnames in
onionpress-tor only. onionpress-tor's entrypoint runs this on every start,
before web-server.py; web-server.py serves no name until MARKER is on that
volume (onionnames.registry_in_place).

Everything at the old location may have been written by WordPress, so:

  * Nothing there is walked. Only onionnames.db and its -wal and -journal
    are read, through O_NOFOLLOW descriptors below the old directory (itself
    opened without following a link), and only if each is a regular file of
    at most MAX_BYTES that is not sparse (more than SPARSE_SLACK of it in
    holes, asked with SEEK_DATA/SEEK_HOLE). The database must start with
    SQLite's header.
  * SQLite never opens a path there. The files are copied into a private,
    root-only folder on the new volume, again until the copy is stable, with
    runs of zeros kept as holes: a copy takes no more room than the file. No
    legitimate writer is left by then: the old onionpress-tor is the
    container this one replaces.
  * The new registry is not a copy of the old file. It is a new database
    with onionnames.py's schema holding only the rows that pass
    onionnames.py's checks, read from the private copy with SQLite's
    defensive settings after PRAGMA quick_check, written under a temporary
    name and renamed into place after any stale -wal, -journal or -shm of
    the volume is removed. The private copy is deleted in every case.
  * What counts as a registry ("at stake"): a regular onionnames.db with
    SQLite's header and an onionnames table. Anything else there is left
    alone and never read again.
  * MARKER is written whenever this container is to start on the new
    volume: after a move; with no registry at the old location; when the
    volume already holds one (a restore, or a run interrupted after the
    rename). Only when both /var/lib/onionnames and /var/lib/onionpress are
    mount points: otherwise "nothing there" proves nothing.
  * A registry that cannot be moved (it keeps changing, or SQLite finds it
    damaged) stays where it is, nothing is removed and no marker written;
    the next start tries again. The container starts regardless: the site
    stays up and /api/name/* answers 503 meanwhile, so nobody can claim a
    name in an empty registry, and nothing WordPress plants can keep the
    container from starting.
  * After a move the copied files are removed from the old location if
    they are still the files that were copied. Nothing is kept aside, and
    once the marker exists the old location is never read again.
  * Every start makes the volume root:root 0700 and its files 0600.

Exit status 0: the registry is in place on the volume. 1: it is not (see
the log); start anyway.

Usage: onionhome-registry-migrate.py [--legacy-parent DIR] [--dest DIR]
                                     [--no-mount-check]
"""

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone

import onionnames

LEGACY_PARENT = "/var/lib/onionpress"
LEGACY_NAME = "onionhome"
DEST = "/var/lib/onionnames"

DB = "onionnames.db"
READ = (DB, DB + "-wal", DB + "-journal")       # what is read at the old location
SIDECARS = (DB + "-wal", DB + "-journal", DB + "-shm")

MARKER = ".migrated-from-persistent-data"       # onionnames.VOLUME_MARKER
LOCK = ".migrate.lock"
STAGING = ".migrating"

MAX_BYTES = 256 << 20
SPARSE_SLACK = 1 << 20          # bytes of holes a file may have and still be read
SQLITE_HEADER = b"SQLite format 3\x00"
ATTEMPTS = 5
RETRY_DELAY = 1

_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# A reserved page slug matters only if a name could equal it.
_SLUG_RE = re.compile(r"[a-z0-9._-]{1,%d}" % onionnames.MAX_NAME_LEN)

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


def log(msg):
    # One line each: SQLite's messages about an untrusted file can span lines.
    msg = re.sub(r"[\x00-\x1f\x7f]+", " ", str(msg))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] onionhome-registry-migrate: {msg}\n")
    sys.stderr.flush()


class Unstable(Exception):
    """The old registry changed while it was being copied."""


class Unusable(Exception):
    """A registry is at the old location and cannot be moved now."""


class NotARegistry(Exception):
    """Nothing at the old location is a name registry."""

    def __init__(self, why, absent=False):
        super().__init__(why)
        self.absent = absent


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unescape_mountinfo(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def is_mount_point(path, mountinfo="/proc/self/mountinfo"):
    """True if path is a mount point (a volume in a container).

    From /proc/self/mountinfo: os.path.ismount compares st_dev with the
    parent, which says no when the volume and the image layer share a
    filesystem (Docker's vfs storage driver).
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
# The volume
# ---------------------------------------------------------------------------

def _own(fd, mode):
    os.fchmod(fd, mode)
    if os.geteuid() == 0:
        os.fchown(fd, 0, 0)


def _normalize(dest):
    """root:root, 0700 for the volume and any folder in it, 0600 for its
    files. Top level only: nothing makes folders here but this script."""
    fd = os.open(dest, DIR_FLAGS)
    try:
        _own(fd, 0o700)
        for name in os.listdir(fd):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                flags = FILE_FLAGS
                mode = 0o600
            elif stat.S_ISDIR(st.st_mode):
                flags = DIR_FLAGS
                mode = 0o700
            else:
                continue
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                continue
            try:
                _own(child, mode)
            finally:
                os.close(child)
    finally:
        os.close(fd)


def _remove_path(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _fsync_dir(path):
    fd = os.open(path, DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_marker(dest, info):
    tmp = os.path.join(dest, MARKER + ".tmp")
    _remove_path(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(info, migrated_at=_stamp()), f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(dest, MARKER))
    _fsync_dir(dest)


def _registry_on_volume(dest):
    """True if the volume holds a registry already; drops an empty file."""
    path = os.path.join(dest, DB)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISREG(st.st_mode) and st.st_size > 0:
        return True
    _remove_path(path)
    return False


# ---------------------------------------------------------------------------
# The old location: read through descriptors, never by SQLite
# ---------------------------------------------------------------------------

def _open_old_dir(parent_fd):
    """The old onionhome/ directory, or None if there is no directory (a
    link is not one: it is left alone and not followed)."""
    try:
        return os.open(LEGACY_NAME, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
    log(f"the old location ({LEGACY_NAME}) is not a directory; left alone, not read")
    return None


def _sparse(fd, size):
    """True if more than SPARSE_SLACK of the file's size bytes are holes.

    Asked with SEEK_DATA/SEEK_HOLE, not by comparing st_blocks with
    st_size: on a compressing filesystem (ZFS, btrfs with compression) a
    file with no hole in it takes fewer blocks than its size too, and the
    real registry would be taken for a planted file and left behind. ZFS
    with compression does keep all-zero blocks as holes, hence the slack.
    Where the system cannot say
    (no SEEK_DATA, or the filesystem refuses it) the file counts as dense;
    that costs no room either, since its copy keeps runs of zeros as holes
    (_copy_fd), and MAX_BYTES still applies. Raises Unstable if the file
    shrinks meanwhile.
    """
    if size <= 0 or not (hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")):
        return False
    holes = pos = 0
    try:
        while pos < size:
            try:
                data = min(os.lseek(fd, pos, os.SEEK_DATA), size)
            except OSError as e:
                if e.errno != errno.ENXIO:
                    raise
                if os.fstat(fd).st_size < size:
                    raise Unstable("it shrank while being checked for holes")
                data = size                     # nothing but a hole from pos on
            holes += data - pos
            if holes > SPARSE_SLACK:
                return True
            if data >= size:
                break
            end = os.lseek(fd, data, os.SEEK_HOLE)
            if end <= data:
                break
            pos = end
        return False
    except OSError:
        return False
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass


def _open_member(dir_fd, name):
    """(fd, None) for a regular file name below dir_fd that may be read, or
    (None, why not). Nothing but a regular file is ever opened."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, "absent"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    if st.st_size > MAX_BYTES:
        return None, f"larger than {MAX_BYTES >> 20} MiB"
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP):
            raise Unstable(f"{name} changed while being opened")
        raise
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise Unstable(f"{name} changed while being opened")
        if fst.st_size > MAX_BYTES:
            raise Unstable(f"{name} grew past the size limit")
        why = "sparse" if _sparse(fd, fst.st_size) else None
    except BaseException:
        os.close(fd)
        raise
    if why:
        os.close(fd)
        return None, why
    return fd, None


def _signature(dir_fd):
    sig = {}
    for name in READ:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            sig[name] = None
            continue
        sig[name] = (st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return sig


_ZEROS = bytes(1 << 16)


def _copy_fd(fd, path):
    """Copy the open file fd to path (new, 0600), at most MAX_BYTES. Runs of
    zeros become holes in the copy: whatever holes the file has, or gains
    after _sparse looked, its copy takes no more room than it does."""
    out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                  0o600)
    total = 0
    try:
        while True:
            chunk = os.read(fd, len(_ZEROS))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise Unstable("it grew past the size limit while being copied")
            if chunk == _ZEROS[:len(chunk)]:
                os.lseek(out, len(chunk), os.SEEK_CUR)
                continue
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
        os.ftruncate(out, total)
        os.fsync(out)
    finally:
        os.close(out)


def _snapshot(dir_fd, private):
    """Copy the registry's files into private. Returns (names copied, the
    signature they had). Raises NotARegistry or Unstable."""
    before = _signature(dir_fd)
    fd, why = _open_member(dir_fd, DB)
    if fd is None:
        raise NotARegistry(f"{DB}: {why}", absent=(why == "absent"))
    try:
        if os.pread(fd, len(SQLITE_HEADER), 0) != SQLITE_HEADER:
            raise NotARegistry(f"{DB} is not an SQLite database")
        _copy_fd(fd, os.path.join(private, DB))
    finally:
        os.close(fd)
    copied = [DB]
    for name in READ[1:]:
        fd, why = _open_member(dir_fd, name)
        if fd is None:
            if why != "absent":
                log(f"left {name} at the old location out ({why})")
            continue
        try:
            _copy_fd(fd, os.path.join(private, name))
        finally:
            os.close(fd)
        copied.append(name)
    if _signature(dir_fd) != before:
        raise Unstable("the old registry changed while being copied")
    return copied, before


def _remove_old(parent_fd, dir_fd, copied, sig):
    """Remove the copied files that are still those files, the -shm index
    with them, then the directory if that leaves it empty. Returns what
    stays."""
    left = []
    for name in copied:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        now = (st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if now == sig[name] and stat.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=dir_fd)
        else:
            left.append(name)
    if DB not in left:
        try:
            st = os.stat(DB + "-shm", dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                os.unlink(DB + "-shm", dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(LEGACY_NAME, dir_fd=parent_fd)
    except OSError:
        pass
    return left


# ---------------------------------------------------------------------------
# The private copy, read as untrusted, and the new registry
# ---------------------------------------------------------------------------

def _open_untrusted(path):
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


def _read_registry(path):
    """(names, reservations, rows left out) from the private copy.

    Raises NotARegistry without an onionnames table of the expected shape,
    Unusable if SQLite cannot read it or finds it damaged."""
    now = _stamp()
    try:
        conn = _open_untrusted(path)
    except sqlite3.Error as e:
        raise Unusable(f"SQLite cannot open it ({e})")
    try:
        try:
            tables = {name: sql for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
            if "onionnames" not in tables:
                raise NotARegistry("it has no onionnames table")
            for name in ("onionnames", "dynamic_reservations"):
                sql = tables.get(name)
                if sql is not None and not (isinstance(sql, str) and re.match(
                        r"\s*CREATE\s+TABLE\b", sql, re.I)):
                    # A virtual table would run its module's code to answer.
                    raise NotARegistry(f"its {name} is not a plain table")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(onionnames)")}
            if not {"name", "onionaddress", "registered_at", "last_seen_at"} <= columns:
                raise NotARegistry("its onionnames table is not the registry's")
            check = conn.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise Unusable(f"SQLite finds it damaged ({str(check[0][0])[:120]})")
            names, left_out = [], 0
            for name, address, registered, seen in conn.execute(
                    "SELECT name, onionaddress, registered_at, last_seen_at FROM onionnames"):
                if not (isinstance(name, str) and onionnames.validate_name(name)[0]
                        and isinstance(address, str)
                        and onionnames._ONION_RE.fullmatch(address)):
                    left_out += 1
                    continue
                if not (isinstance(registered, str) and _TIME_RE.fullmatch(registered)):
                    registered = now
                if not (isinstance(seen, str) and _TIME_RE.fullmatch(seen)):
                    seen = None
                names.append((name.lower(), name, address, registered, seen))
            reservations = []
            if "dynamic_reservations" in tables:
                try:
                    for slug, source, refreshed in conn.execute(
                            "SELECT name_lower, source, refreshed_at FROM dynamic_reservations"):
                        if (isinstance(slug, str) and _SLUG_RE.fullmatch(slug)
                                and source == "wp-pages" and isinstance(refreshed, str)
                                and _TIME_RE.fullmatch(refreshed)):
                            reservations.append((slug, source, refreshed))
                except sqlite3.DatabaseError:
                    reservations = []   # web-server.py refetches them from WordPress
            return names, reservations, left_out
        except sqlite3.DatabaseError as e:
            raise Unusable(f"SQLite cannot read it ({e})")
    finally:
        conn.close()


def _write_registry(path, names, reservations):
    """A new registry at path with onionnames.py's schema; returns how many
    names it holds."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(onionnames.SCHEMA)
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT OR IGNORE INTO onionnames "
            "(name_lower, name, onionaddress, registered_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)", names)
        conn.executemany(
            "INSERT OR IGNORE INTO dynamic_reservations "
            "(name_lower, source, refreshed_at) VALUES (?, ?, ?)", reservations)
        conn.execute("COMMIT")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("the new registry failed SQLite's integrity check")
        kept = conn.execute("SELECT COUNT(*) FROM onionnames").fetchone()[0]
    finally:
        conn.close()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC)
    try:
        os.fsync(fd)
        _own(fd, 0o600)
    finally:
        os.close(fd)
    return kept


def _put_in_place(dest, new):
    """Rename new into place as the registry. A -wal, -journal or -shm left
    on the volume belongs to no database now and goes first: SQLite would
    apply it to this one."""
    for name in SIDECARS:
        _remove_path(os.path.join(dest, name))
    os.rename(new, os.path.join(dest, DB))
    _fsync_dir(dest)


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------

def migrate(legacy_parent, dest, attempts=ATTEMPTS, delay=RETRY_DELAY):
    """Put the registry in place on dest and write MARKER, or raise Unusable."""
    if os.path.exists(os.path.join(dest, MARKER)):
        return
    if _registry_on_volume(dest):
        _write_marker(dest, {"moved": False, "reason": "registry already on the volume"})
        log("the volume holds a registry already (a restore, or an earlier run); "
            "marked it current, the old location is not read")
        return
    try:
        parent_fd = os.open(legacy_parent, DIR_FLAGS)
    except FileNotFoundError:
        parent_fd = None
    dir_fd = None if parent_fd is None else _open_old_dir(parent_fd)
    private = os.path.join(dest, STAGING)
    try:
        if dir_fd is None:
            raise NotARegistry("nothing at the old location", absent=True)
        for attempt in range(1, attempts + 1):
            _remove_path(private)
            os.mkdir(private, 0o700)
            try:
                copied, sig = _snapshot(dir_fd, private)
                break
            except Unstable as e:
                if attempt == attempts:
                    raise Unusable(f"{e} ({attempts} tries)")
                log(f"{e}; trying again ({attempt}/{attempts})")
                time.sleep(delay)
        names, reservations, left_out = _read_registry(os.path.join(private, DB))
        new = os.path.join(private, "new-" + DB)
        kept = _write_registry(new, names, reservations)
        _put_in_place(dest, new)
        _write_marker(dest, {"moved": True, "names": kept,
                             "reservations": len(reservations), "left_out": left_out})
        log(f"moved the OnionHome name registry to its own volume: {kept} names, "
            f"{len(reservations)} reserved page names, {left_out} invalid rows "
            f"left out (verified)")
        stays = _remove_old(parent_fd, dir_fd, copied, sig)
        if stays:
            log(f"left {', '.join(stays)} at the old location (changed after the "
                f"copy); never read again")
    except NotARegistry as e:
        _write_marker(dest, {"moved": False, "reason": str(e)})
        if e.absent:
            log("no registry at the old location; marked this volume as current")
        else:
            log(f"what is at the old location is not a name registry ({e}); left "
                f"there, never read again; marked this volume as current")
    finally:
        _remove_path(private)
        if dir_fd is not None:
            os.close(dir_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--legacy-parent", default=LEGACY_PARENT)
    parser.add_argument("--dest", default=DEST)
    parser.add_argument("--no-mount-check", action="store_true",
                        help="skip the mount point checks (tests)")
    args = parser.parse_args(argv)
    unavailable = "/api/name/* answers 503 until a start puts the registry in place"

    if not os.path.isdir(args.dest):
        log(f"ERROR: {args.dest} does not exist; {unavailable}")
        return 1
    if not args.no_mount_check:
        for path in (args.dest, args.legacy_parent):
            if not is_mount_point(path):
                log(f"ERROR: {path} is not a volume (a compose file older than "
                    f"this image?); nothing moved, {unavailable}")
                return 1

    lock_fd = os.open(os.path.join(args.dest, LOCK),
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _normalize(args.dest)
        migrate(args.legacy_parent, args.dest)
        _normalize(args.dest)
        return 0
    except Unusable as e:
        log(f"ERROR: the name registry at the old location cannot be moved: {e}. "
            f"Nothing was removed; {unavailable}")
        return 1
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}; nothing was removed, {unavailable}")
        return 1
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
