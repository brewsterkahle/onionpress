#!/usr/bin/env python3
"""
Move the OnionHeaven hub data off the volume WordPress shares.

The OnionHeaven registry (registry.db) and every registrant's onion service
key (keys/<address>/) used to live in onionheaven/ on
onionpress-persistent-data, mounted at /var/lib/onionpress — which the
wordpress container mounts too. They now live on their own volume,
onionpress-onionheaven-data, mounted at /var/lib/onionheaven in
onionpress-tor, onionheaven and the takeover workers, and nowhere else.

The tor and onionheaven entrypoints run this before anything opens the
registry. Every start runs it; once the data has moved it has nothing to do.

  * Only into a mount point. Without the volume (a compose file older than
    this image) the data would land in the container's writable layer and
    go with it, so nothing moves and the old copy stays where it is.
  * The old directory is walked through directory file descriptors with
    O_NOFOLLOW. WordPress can write to the volume it lives on: symlinks
    there are never followed or copied, and removal never leaves that tree.
  * An old file is removed only after the whole copy has been verified, and
    only if it still has the bytes that were copied. One that changed since
    stays, and the next start picks it up.
  * First move (no marker yet): the data goes into place. Anything already
    on the volume is kept under .pre-migration-<time>/, except empty files
    and directories (a status query's sqlite3 leaves a 0-byte registry.db).
  * After that: anything that turns up in the old directory again is kept
    under legacy-leftover-<time>/ on the volume and not used.
  * onionpress-tor and onionheaven start together; a lock on the volume
    makes the second one wait for the first.
  * SQLite's -shm index is not copied: SQLite rebuilds it from the WAL.

Exit status 0: start (moved, nothing to move, or failed with nothing at
stake: no registrant's key in the old directory, or the data in use moved
earlier and only later arrivals are left there). Exit 1: the first move
failed while keys are at stake; nothing was removed, do not start yet.

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
import stat
import sys
import time
from datetime import datetime, timezone

LEGACY_PARENT = "/var/lib/onionpress"
LEGACY_NAME = "onionheaven"
DEST = "/var/lib/onionheaven"

MARKER = ".migrated-from-persistent-data"
LOCK = ".migrate.lock"
STAGING = ".migrating"
PRE_MIGRATION_PREFIX = ".pre-migration-"
LEFTOVER_PREFIX = "legacy-leftover-"

ATTEMPTS = 5
RETRY_DELAY = 2

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] onionheaven-data-migrate: {msg}\n")
    sys.stderr.flush()


class Unstable(Exception):
    """The old data changed while it was being copied."""


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
# The old tree, through directory file descriptors
# ---------------------------------------------------------------------------

def _is_shm(name):
    return name.endswith(".db-shm")


def _hash_fd(fd):
    h = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)


def _open_regular(dir_fd, name):
    """Open name in dir_fd for reading; None unless it is a regular file."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP):
            return None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _hash_entry(dir_fd, name):
    fd = _open_regular(dir_fd, name)
    if fd is None:
        return None
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _scan(dir_fd, rel, out, others):
    """Fill out with {rel: (kind, mode, uid, gid, sha256)} for the tree.

    others collects (rel, kind) for what is never copied: symlinks, special
    files, SQLite's -shm index.
    """
    for name in sorted(os.listdir(dir_fd)):
        path = f"{rel}/{name}" if rel else name
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        mode = stat.S_IMODE(st.st_mode) & 0o777
        if stat.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    raise Unstable(f"{path} changed while being read")
                raise
            try:
                out[path] = ("dir", mode, st.st_uid, st.st_gid, None)
                _scan(child, path, out, others)
            finally:
                os.close(child)
        elif stat.S_ISREG(st.st_mode):
            if _is_shm(name):
                others.append((path, "shm"))
                continue
            digest = _hash_entry(dir_fd, name)
            if digest is None:
                raise Unstable(f"{path} changed while being read")
            out[path] = ("file", mode, st.st_uid, st.st_gid, digest)
        elif stat.S_ISLNK(st.st_mode):
            others.append((path, "symlink"))
        else:
            others.append((path, "special"))


def _chown(path_or_fd, uid, gid):
    try:
        if isinstance(path_or_fd, int):
            os.fchown(path_or_fd, uid, gid)
        else:
            os.chown(path_or_fd, uid, gid, follow_symlinks=False)
    except PermissionError:
        if os.geteuid() == 0:
            raise


def _copy_file(dir_fd, name, dest_path):
    """Copy one regular file; return (sha256 of the bytes copied, its stat)."""
    src = _open_regular(dir_fd, name)
    if src is None:
        raise Unstable(f"{name} is no longer a regular file")
    try:
        st = os.fstat(src)
        out = os.open(dest_path,
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                      0o600)
        h = hashlib.sha256()
        try:
            while True:
                chunk = os.read(src, 1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
            os.fsync(out)
            os.fchmod(out, stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
            _chown(out, st.st_uid, st.st_gid)
        finally:
            os.close(out)
        return h.hexdigest(), st
    finally:
        os.close(src)


def _copy_tree(dir_fd, dest_dir, rel, copied):
    for name in sorted(os.listdir(dir_fd)):
        path = f"{rel}/{name}" if rel else name
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        target = os.path.join(dest_dir, name)
        mode = stat.S_IMODE(st.st_mode) & 0o777
        if stat.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    raise Unstable(f"{path} changed while being copied")
                raise
            try:
                os.mkdir(target, 0o700)
                _copy_tree(child, target, path, copied)
            finally:
                os.close(child)
            os.chmod(target, mode)
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            _chown(target, st.st_uid, st.st_gid)
            copied[path] = ("dir", mode, st.st_uid, st.st_gid, None)
        elif stat.S_ISREG(st.st_mode) and not _is_shm(name):
            # Recorded from the descriptor that was read: if the name was
            # swapped since the stat above, the check against the old tree
            # in _verify sees it.
            digest, fst = _copy_file(dir_fd, name, target)
            copied[path] = ("file", stat.S_IMODE(fst.st_mode) & 0o777,
                            fst.st_uid, fst.st_gid, digest)


def _hash_path(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _verify(copied, staging, legacy_fd):
    """The staged copy holds what was read, and the old tree still holds it.

    Returns the old tree's entries that are never copied (see _scan).
    """
    for path, (kind, mode, uid, gid, digest) in copied.items():
        st = os.lstat(os.path.join(staging, path))
        if kind == "dir":
            ok = stat.S_ISDIR(st.st_mode)
        else:
            ok = (stat.S_ISREG(st.st_mode)
                  and _hash_path(os.path.join(staging, path)) == digest)
        ok = ok and stat.S_IMODE(st.st_mode) == mode
        if os.geteuid() == 0:
            ok = ok and (st.st_uid, st.st_gid) == (uid, gid)
        if not ok:
            raise RuntimeError(f"the copy of {path} does not match what was read")

    now, others = {}, []
    _scan(legacy_fd, "", now, others)
    for path, entry in copied.items():
        current = now.get(path)
        if current is None:
            raise Unstable(f"{path} disappeared while being copied")
        if current != entry and not (entry[0] == "file" and path.endswith(".log")):
            raise Unstable(f"{path} changed while being copied")
    new = sorted(set(now) - set(copied))
    if new:
        raise Unstable(f"{len(new)} entries appeared while copying (first: {new[0]})")
    return others


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


def _unlink_if(digest):
    def action(pfd, name):
        if _hash_entry(pfd, name) != digest:
            return False
        os.unlink(name, dir_fd=pfd)
        return True
    return action


def _unlink_symlink(pfd, name):
    if not stat.S_ISLNK(os.stat(name, dir_fd=pfd, follow_symlinks=False).st_mode):
        return False
    os.unlink(name, dir_fd=pfd)
    return True


def _unlink(pfd, name):
    os.unlink(name, dir_fd=pfd)
    return True


def _rmdir(pfd, name):
    os.rmdir(name, dir_fd=pfd)
    return True


def _remove_copied(legacy_fd, copied, others):
    """Remove what was copied from the old tree.

    A file goes only if it still has the copied bytes. Symlinks go (never
    followed, never data), an -shm index with its database, and then the
    directories that are empty. Returns the paths that stay.
    """
    kept, removed = [], set()
    for path, (kind, _, _, _, digest) in copied.items():
        if kind == "file":
            if _in_parent(legacy_fd, path, _unlink_if(digest)):
                removed.add(path)
            else:
                kept.append(path)
    for path, kind in others:
        if kind == "symlink":
            _in_parent(legacy_fd, path, _unlink_symlink)
        elif kind == "shm" and path[:-len("-shm")] in removed:
            _in_parent(legacy_fd, path, _unlink)
    dirs = [p for p, e in copied.items() if e[0] == "dir"]
    for path in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
        if not _in_parent(legacy_fd, path, _rmdir):
            kept.append(path)
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


def _put_in_place(dest, staging, stamp):
    """Move the staged data into the volume root; keep what was there."""
    aside = _unique(os.path.join(dest, PRE_MIGRATION_PREFIX + stamp))

    def keep(path, name):
        os.makedirs(aside, mode=0o700, exist_ok=True)
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
    for name in sorted(os.listdir(staging)):
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


def _migrate_once(parent_fd, dest):
    legacy_fd = _legacy_dir_fd(parent_fd)
    first = not os.path.exists(os.path.join(dest, MARKER))
    staging = os.path.join(dest, STAGING)
    if os.path.lexists(staging):
        _remove_path(staging)

    if legacy_fd is None:
        if first:
            _write_marker(dest, {"migrated_at": _stamp(), "entries": 0})
            log("nothing at the old location; marked this volume as current")
        return
    try:
        if not os.listdir(legacy_fd):
            try:
                os.rmdir(LEGACY_NAME, dir_fd=parent_fd)
            except OSError:
                pass
            if first:
                _write_marker(dest, {"migrated_at": _stamp(), "entries": 0})
                log("the old location was empty; marked this volume as current")
            return

        stamp = _stamp()
        os.mkdir(staging, 0o700)
        copied = {}
        _copy_tree(legacy_fd, staging, "", copied)
        others = _verify(copied, staging, legacy_fd)
        if not copied:
            # Only symlinks or special files: nothing to keep.
            os.rmdir(staging)
            _remove_copied(legacy_fd, copied, others)
            if first:
                _write_marker(dest, {"migrated_at": stamp, "entries": 0})
            for path, kind in others:
                if kind == "special":
                    log(f"left {path} at the old location (not a file or directory)")
            return

        files = sum(1 for e in copied.values() if e[0] == "file")
        keys = sum(1 for p, e in copied.items()
                   if e[0] == "dir" and p.startswith("keys/") and p.count("/") == 1)
        if first:
            _put_in_place(dest, staging, stamp)
            _write_marker(dest, {"migrated_at": stamp, "entries": len(copied),
                                 "files": files, "key_dirs": keys})
            log(f"moved the OnionHeaven data to its own volume: {files} files, "
                f"{keys} registrants' key directories (verified)")
        else:
            leftover = _unique(os.path.join(dest, LEFTOVER_PREFIX + stamp))
            os.rename(staging, leftover)
            _fsync_dir(dest)
            log(f"WARNING: {files} files turned up at the old location after the "
                f"move ({keys} key directories); kept in "
                f"{os.path.basename(leftover)}/ on the volume, not used. Was an "
                f"older OnionPress container still running?")

        kept = _remove_copied(legacy_fd, copied, others)
        try:
            os.rmdir(LEGACY_NAME, dir_fd=parent_fd)
        except OSError:
            pass
        for path, kind in others:
            if kind == "special":
                log(f"left {path} at the old location (not a file or directory)")
        if kept:
            log(f"{len(kept)} entries changed after they were copied and stay at "
                f"the old location until the next start (first: {kept[0]})")
    finally:
        os.close(legacy_fd)


def _keys_at_stake(legacy_parent):
    """True unless the old location certainly holds no registrant's key."""
    try:
        parent_fd = os.open(legacy_parent, DIR_FLAGS)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            legacy_fd = os.open(LEGACY_NAME, DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        try:
            keys_fd = os.open("keys", DIR_FLAGS, dir_fd=legacy_fd)
        except FileNotFoundError:
            return False
        finally:
            os.close(legacy_fd)
        try:
            return bool(os.listdir(keys_fd))
        finally:
            os.close(keys_fd)
    except OSError:
        return True
    finally:
        os.close(parent_fd)


def migrate(legacy_parent, dest, attempts=ATTEMPTS, delay=RETRY_DELAY):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--legacy-parent", default=LEGACY_PARENT)
    parser.add_argument("--dest", default=DEST)
    parser.add_argument("--no-mount-check", action="store_true",
                        help="move into --dest even if it is not a mount point (tests)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.dest):
        log(f"{args.dest} does not exist; nothing moved")
        return 0
    if not args.no_mount_check and not is_mount_point(args.dest):
        log(f"WARNING: {args.dest} is not a volume (a compose file older than "
            f"this image?). The OnionHeaven data stays where it is, and what "
            f"this container writes there is lost when it is recreated.")
        return 0

    lock_fd = os.open(os.path.join(args.dest, LOCK),
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        os.chmod(args.dest, 0o700)
        migrate(args.legacy_parent, args.dest)
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
                "stays there until the next start")
            return 0
        if _keys_at_stake(args.legacy_parent):
            log("registrants' keys are still at the old location and nothing was "
                "removed; not starting until they can be moved")
            return 1
        log("no registrant's key at the old location; starting anyway, the move "
            "is tried again on the next start")
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
