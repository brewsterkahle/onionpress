"""
A backup's archive: the plain files a backup is staged from, the encrypted
archive pack_backup writes, and everything a restore checks before and
while it extracts one.

- Staging: what comes out of a container is unpacked as plain files and
  folders only (unpack_plain_tar) and swept (scrub_staged_tree) before
  `zip -r` reads it.
- Format: pack_backup writes format 2 (backup_crypto); read_backup_metadata,
  peek_backup_metadata and extract_archive read both formats.
- Checks: metadata.json's fields (validate_backup_metadata), member names
  and types (validate_archive_members), zip-bomb limits
  (check_archive_size, extract_archive) and the extracted tree
  (check_staged_tree).

Split out of backup.py, which re-exports every name here; backup.py keeps
the orchestration (create_backup, the restores).
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib
from contextlib import contextmanager

from onionpress import backup_crypto, key_manager, wp_files


# ─── Staging what the containers hand over ──────────────────────────────
# create_backup copies wp-content out of the WordPress container, where code
# running as www-data can plant anything. (A hub's OnionHeaven data and
# OnionHome's name registry come from volumes of their own now, and are
# scrubbed the same way.) A symbolic link staged on the host points at a host path:
# `zip -r` (no -y) would store the file it points to, so a link to
# ~/.ssh/id_ed25519 would put that key in a backup whose password the
# Settings page lets the site choose; prune_image_derivatives would walk
# into a linked uploads/ and write_archive_marker would write through one.
# So the container's tar stream is unpacked as plain files and folders only
# (unpack_plain_tar), and every staged tree is swept (scrub_staged_tree)
# before anything else reads it, and again by pack_backup before zip runs.

# tarfile's stream reader copies its whole buffer on every header read, so
# a larger buffer is slower: 64 KiB unpacks as fast as bsdtar here.
_STAGE_BUFSIZE = 64 << 10
# A copy out of a container gets this, plus its size at
# wp_files.COPY_BYTES_PER_S (issue #317: a fixed 300 s cut off large sites).
_STAGE_COPY_FLOOR_S = 300
# Entries left out are logged by name up to this many per tree, then counted.
_STAGE_LOG_LIMIT = 20


def _staged_name(name):
    """A tar member's name as a path relative to the staging folder ('' for
    the root entry, '.' or './'), or None unless it is a plain relative
    name: absolute, with a '..' component or a NUL. A backslash is an
    ordinary character here (the names come from a Linux filesystem); the
    restore checks the names again as zip members."""
    if '\x00' in name or name.startswith('/'):
        return None
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if '..' in parts:
        return None
    return '/'.join(parts)


def _log_left_out(log_func, where, left_out):
    """Log what staging *where* left out of the backup: (relative name,
    what it was) pairs, the first _STAGE_LOG_LIMIT by printable cut name,
    then a count. The names are the container's, so never raw."""
    for rel, kind in left_out[:_STAGE_LOG_LIMIT]:
        log_func(f"Backup: left {_member_label(rel)} in {where} out of the "
                 f"backup ({kind})")
    if len(left_out) > _STAGE_LOG_LIMIT:
        log_func(f"Backup: left {len(left_out) - _STAGE_LOG_LIMIT} more "
                 f"entries in {where} out of the backup")


def _tar_member_kind(member):
    if member.issym():
        return "a symbolic link"
    if member.islnk():
        return "a hard link"
    if member.isdev():
        return "a device or FIFO"
    return "not a plain file"


def _make_staged_dirs(dest, rel, made):
    """Create the folders of *rel* under *dest*, one component at a time.
    Return False if one exists as anything but a folder (lstat; nothing is
    followed). *made* is the set of folders (relative) already known to be
    folders here: unpack_plain_tar creates nothing else, so they stay so."""
    if not rel or rel in made:
        return True
    parts = rel.split('/')
    for i in range(1, len(parts) + 1):
        sub = '/'.join(parts[:i])
        if sub in made:
            continue
        path = os.path.join(dest, sub)
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return False
        made.add(sub)
    return True


def unpack_plain_tar(stream, dest, log_func, where):
    """Unpack the tar stream *stream* (`tar -cf -` run in a container) into
    the folder *dest*, writing regular files and folders only; return how
    many entries were left out.

    Symbolic and hard links, devices, FIFOs, absolute or '..' names and
    entries that clash with one already unpacked are left out and logged.
    Nothing written here is a link and files are opened with O_NOFOLLOW, so
    no entry reaches outside *dest*. Files keep their times and owner
    read/write; set-id, sticky and group/other write bits are dropped.
    """
    left_out = []
    made = set()
    with tarfile.open(fileobj=stream, mode='r|', bufsize=_STAGE_BUFSIZE) as tf:
        for member in tf:
            rel = _staged_name(member.name)
            if rel is None or (member.isreg() and not rel):
                left_out.append((member.name, "an unsafe name"))
                continue
            if member.isdir():
                if not _make_staged_dirs(dest, rel, made):
                    left_out.append((rel, "a folder where a file is"))
                continue
            if not member.isreg():
                left_out.append((rel, _tar_member_kind(member)))
                continue
            if not _make_staged_dirs(dest, rel.rpartition('/')[0], made):
                left_out.append((rel, "a file inside a file"))
                continue
            try:
                fd = os.open(os.path.join(dest, rel),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                             | os.O_NOFOLLOW | os.O_CLOEXEC,
                             (member.mode & 0o755) | 0o600)
            except (IsADirectoryError, NotADirectoryError):
                left_out.append((rel, "a file where a folder is"))
                continue
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(tf.extractfile(member), out, _STAGE_BUFSIZE)
                out.flush()
                try:
                    os.utime(out.fileno(), (member.mtime, member.mtime))
                except (OSError, OverflowError, ValueError):
                    pass
    _log_left_out(log_func, where, left_out)
    return len(left_out)


def scrub_staged_tree(root, log_func, where=None):
    """Remove from the staged tree *root* everything `zip -r` must not
    read; return how many entries were removed.

    Removed: symbolic links (zip stores the file a link points to, wherever
    it is), devices, FIFOs and sockets, and regular files with another name
    outside *root* (a hard link to a host file). Each is logged by name
    (_log_left_out). Folders and files the owner cannot read are made
    readable, so that this walk, and zip, see all of it. Nothing is
    followed (lstat), and a folder that cannot be listed raises rather than
    being skipped: what was not looked at is not zipped.
    """
    where = where or os.path.basename(os.path.normpath(root))
    if os.path.islink(root) or not os.path.isdir(root):
        raise ValueError(f"Backup: the staged {where} is not a folder")

    def _fail(err):
        raise err

    left_out = []
    linked = {}  # (st_dev, st_ino) -> [st_nlink, [paths]]
    for dirpath, dirnames, filenames in os.walk(root, onerror=_fail):
        removed = set()
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            mode = st.st_mode
            if stat.S_ISDIR(mode):
                if mode & 0o700 != 0o700:
                    os.chmod(path, stat.S_IMODE(mode) | 0o700)
                continue
            if stat.S_ISREG(mode):
                if not mode & stat.S_IRUSR:
                    os.chmod(path, stat.S_IMODE(mode) | stat.S_IRUSR)
                if st.st_nlink > 1:
                    linked.setdefault((st.st_dev, st.st_ino),
                                      [st.st_nlink, []])[1].append(path)
                continue
            os.unlink(path)
            removed.add(name)
            left_out.append((os.path.relpath(path, root),
                             "a symbolic link" if stat.S_ISLNK(mode)
                             else "a device, FIFO or socket"))
        # os.walk would try to descend into a removed link to a folder.
        dirnames[:] = [d for d in dirnames if d not in removed]
    for nlink, paths in linked.values():
        if len(paths) < nlink:
            for path in paths:
                os.unlink(path)
                left_out.append((os.path.relpath(path, root),
                                 "a hard link to a file outside it"))
    _log_left_out(log_func, where, left_out)
    return len(left_out)


def _stage_container_tar(argv, dest, log_func, where,
                         timeout=_STAGE_COPY_FLOOR_S):
    """Run *argv* (`docker exec … tar -cf - …`) and unpack its stdout into
    *dest* with unpack_plain_tar; raise if either side fails. The copy gets
    *timeout* seconds; then the docker client is killed, which closes the
    pipe the container's tar writes to."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE)
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()
    killer = threading.Timer(timeout, _kill)
    killer.daemon = True
    killer.start()
    try:
        unpack_plain_tar(proc.stdout, dest, log_func, where)
        # Drain the tar's end padding so the container side never blocks
        # on a full pipe.
        while proc.stdout.read(_STAGE_BUFSIZE):
            pass
    except BaseException as e:
        proc.kill()
        if timed_out.is_set():
            raise Exception(f"{where} copy did not finish within {timeout} s") from e
        if isinstance(e, tarfile.TarError):
            raise Exception(f"{where} tar failed: {e}") from e
        raise
    finally:
        proc.stdout.close()
        rc = proc.wait()
        killer.cancel()
    if timed_out.is_set():
        raise Exception(f"{where} copy did not finish within {timeout} s")
    if rc != 0:
        raise Exception(f"{where} tar failed: docker={rc}")


# Already-compressed formats that `zip -n` stores without deflating: deflate
# would only burn CPU on bytes that won't shrink.
_ZIP_STORE_SUFFIXES = ('.mov:.mp4:.m4v:.webm:.jpg:.jpeg:.png:.gif:.heic:.heif'
                       ':.pdf:.mp3:.m4a:.aac:.ogg:.webp:.zip:.gz:.sealed')
# zip + encryption get this, plus the staged tree's size at
# wp_files.COPY_BYTES_PER_S (was a fixed 600 s, issue #317).
_ZIP_FLOOR_S = 600


def _onion_key_readme_text(metadata):
    """What README.txt adds for a backup without the site's onion service
    key (the Mac Settings page's: excludes_onion_key)."""
    if metadata.get('excludes_onion_key') is not True:
        return ""
    return (
        "\n"
        "This backup does not contain the site's onion service key: it was\n"
        "made from the Settings page in WordPress. Restoring it keeps the\n"
        "onion address of the install it is restored onto. To move the\n"
        "address to another machine, use a backup made with Backup... in the\n"
        "OnionPress menu.\n")


def pack_backup(staging, output_path, password, log_func):
    """Write the staged backup tree *staging* to *output_path* as a format-2
    encrypted backup (see backup_crypto). Shared by create_backup and, via
    `onionpress.cli pack-backup`, the Linux launcher's bash create_backup.

    The system `zip` builds the same archive it always has (same member
    names, same -n list) but without -P; its output streams from stdout
    straight into the encrypting writer, so no unencrypted copy of the
    archive is written to disk. metadata.json is stamped with
    backup_format and also stored as the container's metadata record, which
    is what restore dialogs read. The file appears at *output_path* only
    once complete.

    `zip -r` without -y stores the file a symbolic link points to, so the
    staged tree is first swept of links and special files
    (scrub_staged_tree): parts of it came out of a container. Never pass
    -y instead: restores refuse archives holding links.
    """
    scrub_staged_tree(staging, log_func, 'the staged backup')
    meta_path = os.path.join(staging, 'metadata.json')
    with open(meta_path) as f:
        metadata = json.load(f)
    if metadata.get('backup_format') != backup_crypto.BACKUP_FORMAT:
        metadata['backup_format'] = backup_crypto.BACKUP_FORMAT
        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    with open(meta_path, 'rb') as f:
        metadata_bytes = f.read()
    # hub_backup imports this module; the README's hub part is its.
    from onionpress.hub_backup import _hub_readme_text
    readme = (backup_crypto.readme_text(metadata.get('onionpress_version'))
              + _hub_readme_text(metadata) + _onion_key_readme_text(metadata))

    # stderr goes to a file, not a pipe: zip can warn once per file, and a
    # full stderr pipe nobody reads while we drain stdout would deadlock.
    with tempfile.TemporaryFile() as zip_err:
        proc = subprocess.Popen(
            ['zip', '-r', '-q', '-n', _ZIP_STORE_SUFFIXES, '-', '.'],
            cwd=staging, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=zip_err,
        )
        killer = threading.Timer(
            wp_files.sized_timeout(_tree_bytes(staging), _ZIP_FLOOR_S), proc.kill)
        killer.daemon = True
        killer.start()
        try:
            with backup_crypto.create(output_path, password, metadata_bytes,
                                      readme) as out:
                for chunk in iter(lambda: proc.stdout.read(1 << 20), b''):
                    out.write(chunk)
                # The last record is written only after zip has exited 0, so
                # a zip that died midway never yields a complete-looking file.
                if proc.wait() != 0:
                    zip_err.seek(0)
                    err = zip_err.read().decode('utf-8', errors='replace').strip()
                    raise Exception(f"zip failed (rc={proc.returncode}): {err}")
        finally:
            killer.cancel()
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    log_func(f"Backup: archive encrypted (backup format {backup_crypto.BACKUP_FORMAT})")


_WRONG_PASSWORD_MSG = backup_crypto.WRONG_PASSWORD_MSG


def _check_backup_format(metadata):
    """Refuse a backup from a newer OnionPress whose format this one doesn't
    know (format 1 backups carry no backup_format), then validate the rest
    (validate_backup_metadata): every metadata reader goes through here."""
    if isinstance(metadata, dict):
        fmt = metadata.get('backup_format', 1)
        if not isinstance(fmt, int) or fmt > backup_crypto.BACKUP_FORMAT:
            raise backup_crypto.NewerBackupError(backup_crypto.NEWER_MSG)
    return validate_backup_metadata(metadata)


# A WordPress login: wp_insert_user keeps only these characters
# (sanitize_user, strict) and at most 60 of them. An onionname
# ([A-Za-z0-9._-], 5-40) fits too.
_BACKUP_USERNAME_RE = re.compile(r"[A-Za-z0-9 _.@-]{1,60}")

INVALID_METADATA_MSG = ("Not a valid OnionPress backup: its metadata.json "
                        "has an invalid {field}.")


def validate_backup_metadata(metadata):
    """Return *metadata* if the fields a restore uses are well formed; raise
    ValueError otherwise, before anything has been restored.

    metadata.json comes out of the backup file, so it is as trustworthy as
    whoever handed the file over: the password proves only that the archive
    was made with it, and anyone can make one. Fields that reach a path, a
    config line, a command or a dialog are checked here:

    - onion_address, if present, must be a well-formed v3 address. A
      restore still takes the address from the key (key_pair_onion_address
      / staged_key_address); a value like "../../x" or one with a newline
      is refused outright rather than logged or shown.
    - username, if present, must be a WordPress login. A restore writes it
      to config as ONIONNAME, a line per key, and later backups put it in
      their file name.
    """
    if not isinstance(metadata, dict):
        raise ValueError("Not a valid OnionPress backup: its metadata.json "
                         "is not an object.")
    address = metadata.get('onion_address')
    if address not in (None, '') and not key_manager.is_valid_onion_address(address):
        raise ValueError(INVALID_METADATA_MSG.format(field="onion address"))
    username = metadata.get('username')
    if username not in (None, ''):
        if (not isinstance(username, str)
                or (username.strip()
                    and not _BACKUP_USERNAME_RE.fullmatch(username.strip()))):
            raise ValueError(INVALID_METADATA_MSG.format(field="username"))
    return metadata


def metadata_text(metadata, field, default='unknown', limit=64):
    """A metadata field for a dialog: *default* unless it is a non-empty
    string, control and other non-printable characters (newlines, bidi
    overrides) dropped, cut to *limit* characters. For fields shown but not
    otherwise used (backup_date, onionpress_version), which a crafted backup
    could otherwise fill with text that rewrites the confirmation it sits in.
    """
    value = metadata.get(field) if isinstance(metadata, dict) else None
    if not isinstance(value, str):
        return default
    text = ''.join(ch for ch in value if ch.isprintable()).strip()
    if not text:
        return default
    return text if len(text) <= limit else text[:limit] + '…'


def _parse_metadata(data):
    """json.loads for metadata.json bytes, with one message for bad JSON."""
    try:
        return json.loads(data)
    except ValueError as e:
        raise ValueError("Not a valid OnionPress backup: its metadata.json "
                         "is not valid JSON.") from e


# metadata.json is a few hundred bytes. zipfile reads a member's whole listed
# size into memory, and a crafted backup can list gigabytes for it.
_MAX_METADATA_BYTES = 1 << 20
METADATA_TOO_BIG_MSG = ("Not a valid OnionPress backup: its metadata.json is "
                        "too large.")


def read_staged_metadata(staging):
    """Read and validate metadata.json from an extracted backup."""
    metadata_path = os.path.join(staging, 'metadata.json')
    if not os.path.exists(metadata_path):
        metadata_path = os.path.join(staging, '.', 'metadata.json')
    if not os.path.isfile(metadata_path):
        raise ValueError("Not a valid OnionPress backup (no metadata.json)")
    with open(metadata_path, 'rb') as f:
        data = f.read(_MAX_METADATA_BYTES + 1)
    if len(data) > _MAX_METADATA_BYTES:
        raise ValueError(METADATA_TOO_BIG_MSG)
    return _check_backup_format(_parse_metadata(data))


@contextmanager
def _wrong_password_as_valueerror(zf):
    """Report every way zipfile can fail on a wrong password as one ValueError.

    ZipCrypto (what `zip -P` writes) verifies only ONE byte of a member's
    decryption header, so about 1 wrong password in 256 slips past that check.
    zipfile then hands the garbage plaintext to zlib, which raises zlib.error
    (or, if the garbage happens to inflate, the CRC check raises BadZipFile)
    instead of the RuntimeError("Bad password ...") seen the other 255 times.
    Wrap reads of encrypted members — zf.read/extract/extractall with pwd= —
    so callers and the UI see a single "incorrect password" ValueError.

    Only a password-protected read is reinterpreted: if the archive has no
    encrypted members, a zlib/CRC failure is corruption and propagates as-is.
    """
    try:
        yield
    except RuntimeError as e:
        # "Bad password for file ..." or "... is encrypted, password required"
        if 'password' in str(e).lower():
            raise ValueError(_WRONG_PASSWORD_MSG) from e
        raise
    except (zlib.error, zipfile.BadZipFile) as e:
        if any(zi.flag_bits & 0x1 for zi in zf.infolist()):
            raise ValueError(_WRONG_PASSWORD_MSG) from e
        raise


def _read_metadata_member(zf, name, password):
    """A format-1 backup's metadata.json member, read with *password*;
    refused if it lists more than _MAX_METADATA_BYTES."""
    if zf.getinfo(name).file_size > _MAX_METADATA_BYTES:
        raise ValueError(METADATA_TOO_BIG_MSG)
    with _wrong_password_as_valueerror(zf):
        return zf.read(name, pwd=password.encode())


def read_backup_metadata(zip_path, password):
    """Read metadata.json from a backup zip.

    Returns the metadata dict.
    Raises ValueError on a wrong password, missing metadata, or invalid zip.
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if backup_crypto.PAYLOAD_NAME in zf.namelist():
                return _check_backup_format(_parse_metadata(
                    backup_crypto.read_metadata(zip_path, password)))
            # Format 1. Try to find metadata.json (may be at root or ./metadata.json)
            metadata_name = None
            for name in zf.namelist():
                if name == 'metadata.json' or name == './metadata.json':
                    metadata_name = name
                    break
            if metadata_name is None:
                raise ValueError("Not a valid OnionPress backup (no metadata.json found)")

            data = _read_metadata_member(zf, metadata_name, password)
            return _check_backup_format(_parse_metadata(data))
    except zipfile.BadZipFile:
        raise ValueError("Not a valid zip file.")


INVALID_ARCHIVE_MSG = "Not a valid OnionPress backup: it contains {what} ({name})."

# The Unix file types a backup's archive may hold. zip keeps a member's
# st_mode in the high 16 bits of external_attr; 0 means none was recorded
# (zipfile's writestr), which both extractors write as a plain file.
_PLAIN_MEMBER_TYPES = (0, stat.S_IFREG, stat.S_IFDIR)


def _member_label(name):
    """An archive member's name for an error message: the archive author
    chose it, so printable characters only, cut to 80."""
    text = ''.join(ch for ch in name if ch.isprintable())
    return repr(text if len(text) <= 80 else text[:80] + '…')


def _member_path(name):
    """*name* as a normalised relative path ('a/b'), or None if it is not a
    plain relative name: empty, absolute, with a '..' component or a NUL.
    Backslashes count as separators too (unzip turns them into '/' in
    archives made on Windows)."""
    if not name or '\x00' in name or name[0] in '/\\':
        return None
    parts = [p for p in re.split(r'[/\\]', name) if p not in ('', '.')]
    if not parts or '..' in parts:
        return None
    return '/'.join(parts)


def validate_archive_members(zf):
    """Refuse a backup archive (an open ZipFile) unless every member is a
    plain file or directory under a plain relative name, all distinct;
    raise ValueError before anything is extracted.

    The archive is whoever made the backup file's (the password proves only
    that it was made with it), and a restore copies what it extracts into
    the WordPress container, the Tor key directory and ~/.onionpress. The
    `unzip` fast path recreates symlink entries, so a member such as
    `wp-content -> /Users/<name>` would make the restore's docker cp
    publish the target's files through the onion service. OnionPress's own
    backups never hold links or special files: `zip -r` stores what a link
    points to, as a plain file.

    Both the name in the zip header and the Unicode Path extra field's name
    (which zipfile, and unzip builds with Unicode support, extract to) are
    checked, so the two extractors cannot be steered apart.
    """
    seen = set()
    files = set()
    parents = set()
    for info in zf.infolist():
        file_type = stat.S_IFMT(info.external_attr >> 16)
        if file_type == stat.S_IFLNK:
            raise ValueError(INVALID_ARCHIVE_MSG.format(
                what="a symbolic link", name=_member_label(info.filename)))
        if file_type not in _PLAIN_MEMBER_TYPES:
            raise ValueError(INVALID_ARCHIVE_MSG.format(
                what="a special file", name=_member_label(info.filename)))
        paths = set()
        for name in (info.orig_filename, info.filename):
            path = _member_path(name)
            if path is None:
                raise ValueError(INVALID_ARCHIVE_MSG.format(
                    what="an unsafe file name", name=_member_label(name)))
            paths.add(path)
        for path in paths:
            if path in seen:
                raise ValueError(INVALID_ARCHIVE_MSG.format(
                    what="a file name twice", name=_member_label(path)))
            seen.add(path)
            parts = path.split('/')
            parents.update('/'.join(parts[:i]) for i in range(1, len(parts)))
            if not info.is_dir():
                files.add(path)
    clash = files & parents
    if clash:
        raise ValueError(INVALID_ARCHIVE_MSG.format(
            what="a file that is also a folder",
            name=_member_label(min(clash))))


# ─── Zip bombs ──────────────────────────────────────────────────────────
# An archive lists each member's size, but nothing makes the data match it:
# Apple's `unzip` writes past a member's listed size (8 MiB for one that
# listed 1 KiB, exit 0), and deflate expands up to ~1000x. So a backup may
# list at most _MAX_ARCHIVE_MEMBERS members, whose sizes add up to at most
# the larger of _MIN_UNPACK_BUDGET and _MAX_EXPANSION times the archive's
# own size (a real backup is mostly already-compressed media, ~1-5x), and
# what extraction writes is held to what the archive lists (extract_archive).
_MAX_ARCHIVE_MEMBERS = 500_000
_MIN_UNPACK_BUDGET = 8 << 30
_MAX_EXPANSION = 50

TOO_MANY_MEMBERS_MSG = ("Not a valid OnionPress backup: it holds more than "
                        "{limit:,} files.")
TOO_BIG_MSG = ("Not a valid OnionPress backup: it would unpack to {size}, more "
               "than a backup file of {archive} can hold.")
OVERSIZE_MSG = ("Not a valid OnionPress backup: it unpacks to more data than "
                "it lists.")
MISMATCH_MSG = ("Not a valid OnionPress backup: a file in it does not match "
                "its listed size or checksum.")


def _size_text(n):
    """Bytes as '1.2 GB' or '340.0 MB', for a message."""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    return f"{n / (1 << 20):.1f} MB"


def check_archive_size(zf, archive_bytes):
    """Refuse (ValueError) an archive (an open ZipFile, *archive_bytes*
    long) that lists too many members or would unpack to more than its size
    allows; return (total listed size, largest member's listed size)."""
    infos = zf.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise ValueError(TOO_MANY_MEMBERS_MSG.format(limit=_MAX_ARCHIVE_MEMBERS))
    total = sum(i.file_size for i in infos)
    if total > max(_MIN_UNPACK_BUDGET, _MAX_EXPANSION * archive_bytes):
        raise ValueError(TOO_BIG_MSG.format(size=_size_text(total),
                                            archive=_size_text(archive_bytes)))
    return total, max((i.file_size for i in infos), default=0)


def _validate_archive_file(zip_path):
    """validate_archive_members and check_archive_size for the zip at
    *zip_path*; return (total listed size, largest listed size, members)."""
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            validate_archive_members(zf)
            total, largest = check_archive_size(zf, os.path.getsize(zip_path))
            return total, largest, len(zf.infolist())
    except zipfile.BadZipFile as e:
        raise ValueError("Not a valid zip file.") from e


_tree_bytes = wp_files.tree_bytes


def _free_bytes(path):
    """Bytes free to this user on the filesystem holding *path*."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def check_staged_tree(root):
    """Refuse an extracted backup at *root* that holds anything but plain
    files and directories; raise ValueError.

    validate_archive_members is the gate; this is the check after the fact,
    should an extractor (another unzip build, say) create a link the member
    list did not show. restore_container_artifacts runs it again before it
    copies anything: install-from-backup's staging sits on disk across a
    restart. Nothing is followed: links are found with lstat.
    """
    if os.path.islink(root) or not os.path.isdir(root):
        raise ValueError("Not a valid OnionPress backup: the extracted "
                         "backup is not a folder.")

    def _unreadable(err):
        raise ValueError("Not a valid OnionPress backup: could not read the "
                         f"extracted files ({err.strerror or err}).") from err

    for dirpath, dirnames, filenames in os.walk(root, onerror=_unreadable):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                what = "a symbolic link"
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                what = "a special file"
            else:
                continue
            raise ValueError(INVALID_ARCHIVE_MSG.format(
                what=what, name=_member_label(os.path.relpath(path, root))))


# unzip gets this, plus the archive's listed size at
# wp_files.COPY_BYTES_PER_S (was a fixed 900 s, issue #317).
_UNZIP_TIMEOUT_S = 900
_UNZIP_POLL_S = 0.25


def _unzip_cli_extract(zip_path, password, dest, log_func, *,
                       max_file_bytes, max_total_bytes):
    """Fast-path extraction of a format-1 backup via the system `unzip` CLI —
    C code, ~30-50x faster than zipfile's pure-Python ZipCrypto decryption
    (which runs ~2 MB/s on a ~900 MB encrypted backup). Returns True on
    success, False if `unzip` is absent, errors or times out (caller falls
    back to zipfile). `unzip` recreates symlink entries, so it is only ever
    handed an archive that passed validate_archive_members.

    `unzip` does not hold a member to its listed size, so it runs under a
    file-size limit (`ulimit -f`, from *max_file_bytes*: the largest member
    the archive lists), and it is stopped, raising ValueError, once the disk
    holding *dest* has lost more than *max_total_bytes* since it started.

    Injection-safe: arguments are passed as a LIST with NO shell=True. The
    `sh -c` wrapper's script is a constant; it gets the limit and the unzip
    command as positional parameters and never parses them, so a password
    containing ; $() `` | spaces etc. cannot inject commands. `-P` always
    consumes the next argv as the password value, so a password starting
    with '-' is taken literally too. zip_path is passed as a plain argv
    (callers use absolute paths, so it won't be mistaken for a flag).
    Nothing logged here shows the argv: it holds the password.
    """
    import shutil as _shutil
    if _shutil.which('unzip') is None:
        return False
    # POSIX sh counts ulimit -f in 512-byte blocks; a shell that counts
    # 1024-byte blocks (macOS's /bin/sh) allows twice as much, still bounded.
    blocks = max_file_bytes // 512 + 2
    argv = ['/bin/sh', '-c', 'ulimit -f "$1" || exit 125; shift; exec "$@"',
            'sh', str(blocks),
            'unzip', '-o', '-qq', '-P', password, zip_path, '-d', dest]
    # Output goes to a file: nothing reads a pipe while this polls.
    with tempfile.TemporaryFile() as out:
        try:
            free_at_start = _free_bytes(dest)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=out)
        except OSError as e:
            log_func(f"Restore: unzip CLI unavailable ({type(e).__name__}); "
                     f"using zipfile")
            return False
        deadline = time.monotonic() + wp_files.sized_timeout(
            max_total_bytes, _UNZIP_TIMEOUT_S)
        while True:
            try:
                rc = proc.wait(timeout=_UNZIP_POLL_S)
                break
            except subprocess.TimeoutExpired:
                # Never logged: str(TimeoutExpired) is the argv.
                pass
            try:
                written = free_at_start - _free_bytes(dest)
            except OSError:
                written = 0
            if written > max_total_bytes:
                proc.kill()
                proc.wait()
                log_func(f"Restore: unzip had written {_size_text(written)}, "
                         f"more than the backup lists; stopped it")
                raise ValueError(OVERSIZE_MSG)
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                log_func("Restore: unzip CLI timed out; using zipfile")
                return False
    if rc != 0:
        # Wrong password, AES archive, corrupt zip or a member past the size
        # limit — let zipfile try (and raise a clean error for the
        # wrong-password case).
        log_func(f"Restore: unzip CLI rc={rc}; using zipfile")
        return False
    return True


def extract_archive(zip_path, password, dest, log_func):
    """Extract a backup of either format into *dest*.

    Format 2: decrypt the inner zip to a temp file (every record verified
    first), then extract it with zipfile, which writes each member only up
    to its listed size, checks its CRC and never creates a link (`unzip`
    does none of these); the inner zip is not encrypted, so zipfile is fast.
    Format 1: `unzip -P` under size limits (_unzip_cli_extract), falling
    back to zipfile. Either way a wrong password raises ValueError before
    anything lands in *dest*, and so does an archive holding links, special
    files or unsafe names (validate_archive_members; a format-1 zip's member
    list is readable without the password) or listing too many members or
    too much data (check_archive_size). The extracted tree is checked again
    afterwards (check_staged_tree) and may hold no more than the archive
    lists.
    """
    before = _tree_bytes(dest)
    if backup_crypto.is_encrypted_backup(zip_path):
        log_func("Restore: decrypting backup (format 2)...")
        fd, inner = tempfile.mkstemp(prefix='onionpress-restore-', suffix='.zip')
        os.close(fd)
        try:
            backup_crypto.decrypt_archive(zip_path, password, inner)
            listed, _, _ = _validate_archive_file(inner)
            try:
                with zipfile.ZipFile(inner, 'r') as zf:
                    zf.extractall(dest)
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:
                # The inner zip passed authentication, so whoever made the
                # backup made it this way.
                raise ValueError(MISMATCH_MSG) from e
        finally:
            os.unlink(inner)
    else:
        listed, largest, members = _validate_archive_file(zip_path)
        # On top of the listed sizes: a block per file, and 1 GiB for
        # whatever else writes to the same disk meanwhile.
        if not _unzip_cli_extract(
                zip_path, password, dest, log_func,
                max_file_bytes=largest,
                max_total_bytes=listed + 4096 * members + (1 << 30)):
            if _tree_bytes(dest) - before > listed:
                raise ValueError(OVERSIZE_MSG)
            # Fallback: pure-Python zipfile — always available, but slow for
            # ZipCrypto. Also the path that surfaces a wrong-password error.
            with zipfile.ZipFile(zip_path, 'r') as zf:
                with _wrong_password_as_valueerror(zf):
                    zf.extractall(dest, pwd=password.encode())
    check_staged_tree(dest)
    if _tree_bytes(dest) - before > listed:
        raise ValueError(OVERSIZE_MSG)


def check_uploaded_backup(path):
    """Raise ValueError unless *path* is a regular file (lstat: a link is
    not followed).

    For the Settings page's restore, which docker-cp's the uploaded
    restore-upload.zip out of the WordPress container: docker cp copies a
    symbolic link as a link, so one planted there (www-data can) would have
    the restore read a host file as the backup.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise ValueError("No backup file was uploaded.") from None
    if not stat.S_ISREG(mode):
        raise ValueError("The uploaded backup is not a regular file.")


def peek_backup_metadata(zip_path, password):
    """Validate the password and return a backup's metadata WITHOUT a full
    extract. Used by the welcome-screen restore flow to confirm the password up
    front (and show the address/username being restored) before committing.
    Raises ValueError on a wrong password; raises on an invalid backup.
    """
    if backup_crypto.is_encrypted_backup(zip_path):
        return _check_backup_format(_parse_metadata(
            backup_crypto.read_metadata(zip_path, password)))
    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = [n for n in zf.namelist()
                 if n.rstrip('/').endswith('metadata.json')]
        if not names:
            raise Exception("Not an OnionPress backup (no metadata.json)")
        # Read the member straight from the zip: its name is the archive
        # author's, and joined onto a directory it could point anywhere.
        data = _read_metadata_member(zf, names[0], password)
        return _check_backup_format(_parse_metadata(data))
