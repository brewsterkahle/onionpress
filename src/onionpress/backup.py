"""
Backup and Restore for OnionPress

Creates password-protected backups containing Tor keys, WordPress database,
and wp-content (themes, plugins, uploads). New backups are format 2 (a zip
inside libsodium Argon2id + XChaCha20-Poly1305 secretstream encryption, see
backup_crypto); format 1 backups (ZipCrypto zips from `zip -P`) still restore.
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
from datetime import datetime, timezone

from onionpress import backup_crypto, derivatives, key_manager, wp_files
from onionpress.config import (DEFAULTS, SECRET_CONFIG_KEYS, read_config, read_value,
                               redact_config, write_value)


def _default_data_dir() -> str:
    """Resolve the default OnionPress data directory at call time.

    Deliberately not cached: callers (and tests) may mutate $HOME between
    imports and the first use of backup functions.
    """
    return os.path.expanduser("~/.onionpress")


# OnionHome's name registry: its own volume, mounted in onionpress-tor only
# (onionnames.DATA_DIR in the tor image), and the files that are the
# registry. A backup keeps them in onionhome/.
ONIONHOME_REGISTRY_DIR = "/var/lib/onionnames"
ONIONHOME_REGISTRY_FILES = ("onionnames.db", "onionnames.db-wal")

# The OnionHeaven hub's data: its own volume, reached through the
# onionheaven container (onionheaven_common.ONIONHEAVEN_DATA_DIR in the tor
# image). A backup keeps the registry and the registrants' keys in
# onionheaven/, and nothing else from there: not the logs, nor the copies
# the move to the volume keeps aside (keys of addresses since unregistered).
ONIONHEAVEN_DATA_DIR = "/var/lib/onionheaven"

# Prints "hub" if the volume holds hub data (a key directory, or a row in
# the registry), else "none". Never makes a registry.db (sqlite3 would).
HUB_DATA_STATE_SH = f"""cd {ONIONHEAVEN_DATA_DIR} || exit 1
if [ -n "$(ls -A keys 2>/dev/null)" ]; then echo hub; exit 0; fi
if [ -s registry.db ] && [ -n "$(sqlite3 registry.db 'SELECT 1 FROM registry LIMIT 1' 2>/dev/null)" ]; then
    echo hub; exit 0
fi
echo none
"""

# What never passes through WordPress in a backup. The Settings page's backup
# is handed to the WordPress container for download, and its password is the
# admin password WordPress sees in plain text: code running there would have
# both. So that backup (create_backup(include_hub_data=False); Linux:
# create_backup --without-hub-data) leaves out a hub's registry and its
# registrants' onion service keys, and says so in metadata.json
# (excludes_onionheaven_data, with the reason code below) and to the owner.
# Backups made from the app or the CLI, written to the owner's disk, keep
# them. The Settings page's restore, whose upload and password came through
# WordPress too, refuses a backup holding registrants' keys or OnionHome's
# name registry (settings_page_restore_refusal) and never writes either
# volume.
HUB_DATA_LEFT_OUT_REASON = "settings-page"
HUB_DATA_LEFT_OUT_LOG = (
    "Backup: this is an OnionHeaven hub; its registry and registrants' onion "
    "service keys are left out of this backup, which is handed to WordPress "
    "(the Settings page's download). Make the hub's backup from the "
    "OnionPress app or with onionpress backup.")
# The Settings page shows the note with the finished backup and the refusal
# with a refused restore. Plain text, safe in JSON and HTML as it stands:
# linux/onionpress writes the same note into its result file (a test keeps
# the copies equal).
HUB_DATA_LEFT_OUT_NOTE = (
    "This is an OnionHeaven hub: its registry and its registrants' onion "
    "service keys are not in this backup, so they never pass through "
    "WordPress. Back the hub up from the OnionPress app (Backup... in its "
    "menu) or with onionpress backup.")
HUB_DATA_RESTORE_REFUSED_MSG = (
    "This backup holds an OnionHeaven hub's registry and its registrants' "
    "onion service keys, which are never restored through WordPress. Nothing "
    "was restored: restore it from the OnionPress app (Restore... in its "
    "menu) or with onionpress restore.")
ONIONHOME_RESTORE_REFUSED_MSG = (
    "This backup holds OnionHome's name registry, which is never restored "
    "through WordPress. Nothing was restored: restore it from the OnionPress "
    "app (Restore... in its menu) or with onionpress restore.")

# The host's secrets never pass through WordPress in a backup either. The
# Settings page's backup (create_backup(include_host_secrets=False); Linux:
# create_backup --without-host-secrets) carries only the config WordPress's
# status page already shows (config.redact_config, SAFE_CONFIG_KEYS): never
# the Cloudflare tunnel token, which the page's write-only field keeps out of
# WordPress, nor (Linux, whose backups carry the whole config file) image
# overrides, bind host or hub admission. Linux also leaves out the
# healthcheck onion service's key, which a start regenerates. The site's
# own onion key stays: it is what the backup restores. metadata.json says so
# (excludes_host_secrets) and names the secrets that were set
# (secret_config_left_out, names only). No restore takes a secret from a
# backup anyway (RESTORABLE_CONFIG): this install's token is kept, and on a
# new machine the owner enters it again (restore_backup_config says so).
TUNNEL_TOKEN_KEY = "CLOUDFLARE_TUNNEL_TOKEN"

# A tar of the registry, as sqlite3's .backup copies it (one consistent
# state while the hub writes; a copy of the file could miss its -wal or catch
# a checkpoint half done), and of keys/.
HUB_DATA_TAR_SH = f"""set -e
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT
cd {ONIONHEAVEN_DATA_DIR}
set --
if [ -s registry.db ]; then
    sqlite3 registry.db ".backup '$d/registry.db'"
    set -- -C "$d" registry.db
fi
if [ -d keys ]; then
    set -- "$@" -C {ONIONHEAVEN_DATA_DIR} keys
fi
[ "$#" -gt 0 ]
tar -cf - "$@"
"""


# ─── Instrumentation helpers (issue #214) ───────────────────────────────
# Phase 1: log workload inputs and per-phase wall times so we can later
# fit an ETA formula and surface estimates in the dialogs. Lines use a
# stable grep prefix and key=value format:
#   BACKUP_STATS: db_bytes=… wp_posts_rows=… …
#   BACKUP_PHASE: name=db_dump elapsed_s=12.3
# Same shape for RESTORE_STATS / RESTORE_PHASE / RESTORE_STATS_POST.


@contextmanager
def _phase_timer(log_func, prefix, name):
    """Wall-time the enclosed block and log one PHASE line on exit.

    Re-raises any exception so existing error handling still runs;
    the elapsed line is logged either way.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        log_func(f"{prefix}_PHASE: name={name} elapsed_s={elapsed:.2f}")


def _query_workload_stats(db_creds):
    """Return DB stats dict (bytes + per-table approximate row counts).

    Uses INFORMATION_SCHEMA.TABLE_ROWS — approximate for InnoDB but
    cheap and good enough for ETA fitting. Multisite has per-blog
    tables (wp_<n>_posts etc.); we sum across them so the figure
    reflects total content volume.
    """
    sql = (
        "SELECT "
        "COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0), "
        "COALESCE(SUM(IF(TABLE_NAME LIKE 'wp%posts' AND TABLE_NAME NOT LIKE '%postmeta', TABLE_ROWS, 0)), 0), "
        "COALESCE(SUM(IF(TABLE_NAME LIKE 'wp%postmeta', TABLE_ROWS, 0)), 0), "
        "COALESCE(SUM(IF(TABLE_NAME LIKE 'wp%options', TABLE_ROWS, 0)), 0), "
        "COALESCE(SUM(IF(TABLE_NAME='wp_users', TABLE_ROWS, 0)), 0), "
        "COALESCE(SUM(IF(TABLE_NAME='wp_blogs', TABLE_ROWS, 0)), 0) "
        "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE();"
    )
    result = subprocess.run(
        _db_client_argv('mariadb', '-B', '-N',
                        '-u', db_creds['user'],
                        '-D', db_creds['name'],
                        '-e', sql),
        input=db_creds['password'] + '\n',
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=10,
    )
    if result.returncode != 0:
        raise Exception(result.stderr.strip() or 'mariadb query failed')
    parts = result.stdout.strip().split('\t')
    if len(parts) != 6:
        raise Exception(f'expected 6 columns, got {len(parts)}')
    keys = ('db_bytes', 'wp_posts_rows', 'wp_postmeta_rows',
            'wp_options_rows', 'wp_users_rows', 'wp_blogs_rows')
    return {k: int(v) for k, v in zip(keys, parts)}


def _du_bytes(container, path, excludes=()):
    """`du -sb` inside a container; return int bytes, 0 on any failure."""
    excl = ' '.join(f'--exclude={e}' for e in excludes)
    cmd = f"du -sb {excl} {path} 2>/dev/null | cut -f1"
    try:
        result = subprocess.run(
            ['docker', 'exec', container, 'sh', '-c', cmd],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=30,
        )
        out = result.stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:
        return 0


def _dir_size_bytes(path):
    """Recursive sum of file sizes under path; tolerant of read errors."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _log_workload_stats(log_func, label, db_creds):
    """Emit a single STATS line. Never raises — instrumentation must
    not break the operation it's measuring."""
    try:
        stats = _query_workload_stats(db_creds)
        wpcontent_bytes = _du_bytes(
            'onionpress-wordpress', '/var/www/html/wp-content',
            excludes=('creations', '.thumbs', '.DS_Store'),
        )
        onionheaven_bytes = _du_bytes('onionheaven', '/var/lib/onionheaven')
        kvs = [
            f"db_bytes={stats['db_bytes']}",
            f"wp_posts_rows={stats['wp_posts_rows']}",
            f"wp_postmeta_rows={stats['wp_postmeta_rows']}",
            f"wp_options_rows={stats['wp_options_rows']}",
            f"wp_users_rows={stats['wp_users_rows']}",
            f"wp_blogs_rows={stats['wp_blogs_rows']}",
            f"wpcontent_bytes={wpcontent_bytes}",
            f"onionheaven_bytes={onionheaven_bytes}",
        ]
        log_func(f"{label}: " + ' '.join(kvs))
    except Exception as e:
        log_func(f"{label}: error={e}")


# Multisite constants that WordPress needs in wp-config.php for a network install.
# After restore, the WordPress container generates a fresh wp-config.php without
# these, causing `wp core is-installed --network` to fail and ensure_multisite to
# skip.  We re-add them after every restore so the site works immediately.
_MULTISITE_CONSTANTS = {
    'WP_ALLOW_MULTISITE': 'true',
    'MULTISITE': 'true',
    'SUBDOMAIN_INSTALL': 'false',
    'DOMAIN_CURRENT_SITE': "'localhost'",
    'PATH_CURRENT_SITE': "'/'",
    'SITE_ID_CURRENT_SITE': '1',
    'BLOG_ID_CURRENT_SITE': '1',
}


def verify_wp_admin(username, password):
    """Verify that the given credentials belong to a WordPress administrator.

    Returns (True, None) on success, or (False, error_message) on failure.
    """
    # Verify user exists and has administrator role
    try:
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('user', 'get', username,
                                              '--field=roles'),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if 'Invalid user' in stderr:
                return (False, f"User '{username}' does not exist in WordPress.")
            return (False, f"Could not look up user: {stderr}")

        roles = result.stdout.strip()
        if 'administrator' not in roles:
            return (False, f"User '{username}' is not an administrator (roles: {roles}).")
    except subprocess.TimeoutExpired:
        return (False, "Timed out connecting to WordPress container.")
    except Exception as e:
        return (False, f"Error checking user role: {e}")

    # Verify password by piping username and password via stdin.
    # Never embed user input into the PHP code string (injection risk).
    php_code = (
        "$lines = explode(\"\\n\", trim(file_get_contents('php://stdin')));"
        "$user = $lines[0];"
        "$pw = $lines[1];"
        "$u = wp_authenticate($user, $pw);"
        "if (is_wp_error($u)) { fwrite(STDERR, $u->get_error_message()); exit(1); }"
        "echo 'ok';"
    )
    try:
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('eval', php_code, stdin=True),
            input=username + "\n" + password,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            return (False, f"Incorrect password for '{username}'.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out verifying password.")
    except Exception as e:
        return (False, f"Error verifying password: {e}")

    return (True, None)


def verify_wp_admin_password_any(password):
    """Verify *password* matches any WordPress administrator's account.

    Returns (True, username) on success, or (False, error_message).

    The Linux launcher's create_backup and scrub/uninstall checks call
    this through `onionpress.cli verify-admin-password`, and password-only
    UI flows (e.g. the Linux tray's backup dialog) call it directly to give
    inline feedback without asking the user to remember their admin
    username. Uses wp_check_password directly so it bypasses login
    rate-limiting and session cookies. The password reaches PHP on stdin
    only; it is never part of the code or of any argv.
    """
    php_code = (
        "$pw = trim(file_get_contents('php://stdin'));"
        "$users = get_users(array('role' => 'administrator'));"
        "foreach ($users as $u) {"
        "  if (wp_check_password($pw, $u->user_pass)) {"
        "    echo $u->user_login;"
        "    exit;"
        "  }"
        "}"
        "exit(1);"
    )
    try:
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('eval', php_code, stdin=True),
            input=password,
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (True, result.stdout.strip())
        return (False, "Password does not match any administrator account.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out verifying password.")
    except Exception as e:
        return (False, f"Error verifying password: {e}")


def get_admin_username(data_dir=None):
    """Return the WordPress admin user's login.

    Order: ONIONNAME in config → first admin from `wp user list
    --role=administrator` (cached back to config) → "admin" as a last
    resort.
    """
    data_dir = data_dir or _default_data_dir()
    config_path = os.path.join(data_dir, "config")
    name = read_value(config_path, "ONIONNAME", "").strip()
    if name:
        return name
    try:
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('user', 'list', '--role=administrator',
                                              '--field=user_login'),
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                login = line.strip()
                if login:
                    write_value(config_path, "ONIONNAME", login)
                    return login
    except Exception:
        pass
    return "admin"


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
_STAGE_COPY_TIMEOUT_S = 300
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


def wp_content_tar_argv(*excludes):
    """`docker exec` argv that streams wp-content as a tar, read as www-data.

    Not as root: www-data can replace wp-content (or a folder in it) with a
    link, and root's tar -C followed it to any folder in the container,
    which then went into a backup the Settings page hands back to the site.
    --ignore-failed-read: a file www-data cannot read is left out, not the
    whole backup. --hard-dereference: each name of a hard-linked file is
    stored with its contents (unpack_plain_tar keeps no link entries).
    """
    return (['docker'] + wp_files.exec_args('tar', '-cf', '-')
            + ['--hard-dereference', '--ignore-failed-read']
            + [f'--exclude={e}' for e in excludes]
            + ['-C', '/var/www/html/wp-content', '.'])


def stage_wp_content(dest, log_func, *excludes):
    """Copy wp-content out of the WordPress container into *dest* as plain
    files and folders (wp_content_tar_argv, unpack_plain_tar), then
    scrub_staged_tree. The Linux launcher's create_backup runs this through
    `onionpress.cli stage-wp-content`."""
    os.makedirs(dest, exist_ok=True)
    _stage_container_tar(wp_content_tar_argv(*excludes), dest, log_func,
                         'wp-content')
    scrub_staged_tree(dest, log_func, 'wp-content')


def put_wp_content(src, log_func, timeout=300):
    """Unpack the staged *src* over wp-content in the WordPress container,
    as www-data (wp_files.put_tree). Not docker cp + chown -R as root: the
    copy followed links www-data can plant in wp-content (the destination's
    own `wp-content/` too), and so did the trailing-slash chown."""
    r = wp_files.put_tree(src, '/var/www/html/wp-content', merge=True,
                          timeout=timeout)
    if r.returncode != 0:
        log_func(f"Restore: wp-content copy failed: {r.stderr.strip()[-300:]}")
        raise Exception("Could not copy wp-content into the WordPress container")


def _stage_container_tar(argv, dest, log_func, where):
    """Run *argv* (`docker exec … tar -cf - …`) and unpack its stdout into
    *dest* with unpack_plain_tar; raise if either side fails. The container
    gets _STAGE_COPY_TIMEOUT_S to finish."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE)
    killer = threading.Timer(_STAGE_COPY_TIMEOUT_S, proc.kill)
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
        if isinstance(e, tarfile.TarError):
            raise Exception(f"{where} tar failed: {e}") from e
        raise
    finally:
        proc.stdout.close()
        rc = proc.wait()
        killer.cancel()
    if rc != 0:
        raise Exception(f"{where} tar failed: docker={rc}")


def hub_data_state():
    """"hub" if the OnionHeaven volume holds a hub's data (registrants'
    keys, or a registry with a row), "none" if not, None if the onionheaven
    container did not answer."""
    try:
        r = subprocess.run(['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_STATE_SH],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    state = r.stdout.strip() if r.returncode == 0 else ""
    return state if state in ("hub", "none") else None


def stage_hub_data(dest, log_func):
    """Copy the hub's registry (a consistent copy, HUB_DATA_TAR_SH) and
    keys/ out of the onionheaven container into *dest*, as plain files and
    folders (unpack_plain_tar), then scrub_staged_tree. The Linux
    launcher's create_backup runs this through `onionpress.cli
    stage-hub-data`."""
    os.makedirs(dest, exist_ok=True)
    _stage_container_tar(['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_TAR_SH],
                         dest, log_func, 'onionheaven')
    scrub_staged_tree(dest, log_func, 'onionheaven')
    keys = os.path.join(dest, 'keys')
    count = len(os.listdir(keys)) if os.path.isdir(keys) else 0
    log_func(f"Backup: OnionHeaven registry and {count} registrants' key directories copied")


_CONTAINER_TMP_RE = re.compile(r"/tmp/tmp\.[A-Za-z0-9]+")


def restore_hub_data(src, log_func):
    """Put a backup's OnionHeaven registry and keys (*src*, its
    onionheaven/ folder) onto the hub data volume, through the onionheaven
    container.

    The registry goes in through SQLite (.restore, under its locks): a copy
    over the file in use would leave that one's -wal beside it, and SQLite
    would apply it to the restored database. Keys go in as root's. Nothing
    else from the archive goes there (an older backup's master-key files,
    logs, the move's kept copies).
    """
    keys = os.path.join(src, 'keys')
    if os.path.isdir(keys) and not os.path.islink(keys):
        subprocess.run(['docker', 'exec', 'onionheaven', 'mkdir', '-p',
                        f'{ONIONHEAVEN_DATA_DIR}/keys'],
                       capture_output=True, timeout=30, check=True)
        subprocess.run(['docker', 'cp', keys + '/.', f'onionheaven:{ONIONHEAVEN_DATA_DIR}/keys/'],
                       capture_output=True, timeout=120, check=True)
    db = os.path.join(src, 'registry.db')
    if os.path.isfile(db) and not os.path.islink(db):
        tmp = subprocess.run(['docker', 'exec', 'onionheaven', 'mktemp'],
                             capture_output=True, text=True, timeout=30,
                             check=True).stdout.strip()
        if not _CONTAINER_TMP_RE.fullmatch(tmp):
            raise Exception(f"unexpected temporary file name from the container: {tmp!r}")
        try:
            subprocess.run(['docker', 'cp', db, f'onionheaven:{tmp}'],
                           capture_output=True, timeout=120, check=True)
            subprocess.run(['docker', 'exec', 'onionheaven', 'sqlite3',
                            f'{ONIONHEAVEN_DATA_DIR}/registry.db', f".restore '{tmp}'"],
                           capture_output=True, timeout=120, check=True)
        finally:
            subprocess.run(['docker', 'exec', 'onionheaven', 'rm', '-f', tmp],
                           capture_output=True, timeout=30)
    log_func("Restore: OnionHeaven registry and keys restored")


def create_backup(onion_address, username, password, output_path, version, log_func, *,
                  data_dir=None, include_hub_data=True, include_host_secrets=True):
    """Create a full OnionPress backup zip; return its metadata.

    Args:
        onion_address: Current .onion address
        username: WP admin username (stored in metadata)
        password: Backup encryption password
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.
        output_path: Where to write the .zip file
        version: OnionPress version string
        log_func: Callable for progress logging
        include_hub_data: False for a backup that is handed to WordPress (the
            Settings page's download): an OnionHeaven hub's registry and
            registrants' keys are then left out, and metadata.json says so
            (excludes_onionheaven_data). Every caller passes it.
        include_host_secrets: False for that backup too: its config is cut
            to what WordPress's status page shows (wordpress_safe_config: no
            Cloudflare tunnel token), and metadata.json says so
            (excludes_host_secrets). Every caller passes it.
    """
    backup_start = time.monotonic()
    staging = tempfile.mkdtemp(prefix='onionpress-backup-')
    try:
        # 0. Workload stats — log once up front (issue #214). Read DB
        #    creds here so we can reuse them for the dump phase below
        #    without paying for two round trips through wp-cli.
        db_creds = _get_db_credentials()
        _log_workload_stats(log_func, 'BACKUP_STATS', db_creds)

        # 1. Extract Tor keys (OpenSSH PEM, the delivered-key format)
        with _phase_timer(log_func, 'BACKUP', 'tor_keys'):
            log_func("Backup: extracting Tor keys...")
            tor_dir = os.path.join(staging, 'tor-keys')
            os.makedirs(tor_dir)

            priv = key_manager.extract_private_key()
            pub = key_manager.extract_public_key()
            pem_data = key_manager.build_openssh_key(priv, pub)
            with open(os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private'), 'wb') as f:
                f.write(pem_data)

            # Override caller-supplied onion_address with the address derived
            # from the actual key being zipped. The caller passes
            # self.onion_address from the menubar, which is a cached value
            # updated only on the ~30s update_status poll. If a backup runs
            # in the window after a vanity rotation / restore / key import
            # but before that poll, the caller's value is stale and the
            # backup is born with metadata.onion_address pointing at the
            # PRIOR address while tor-keys/ holds the NEW key — exactly the
            # corrupted-backup shape that propagates the mismatch into
            # KEYS_DIR on the hub when this backup is later restored and
            # heartbeated. Derive from the key and the backup is internally
            # consistent regardless of the caller's view.
            derived_address = key_manager.derive_onion_address(pub)
            if onion_address and onion_address != derived_address:
                log_func(f"Backup: KEY-MISMATCH — caller-supplied "
                         f"onion_address={onion_address} does not match "
                         f"key_derives_to={derived_address}. Recording "
                         f"DERIVED in metadata (key is authoritative). The "
                         f"caller's cached onion_address was stale — likely "
                         f"a backup taken in the window after a vanity "
                         f"rotation / restore / key import but before "
                         f"update_status repolled the container.")
            onion_address = derived_address

        # 2. Dump WordPress database via mariadb-dump in the db container
        # (wp db export uses mysqldump which isn't in the WordPress container)
        with _phase_timer(log_func, 'BACKUP', 'db_dump'):
            log_func("Backup: exporting database...")
            db_dir = os.path.join(staging, 'database')
            os.makedirs(db_dir)

            result = subprocess.run(
                _db_client_argv('mariadb-dump',
                                '-u', db_creds['user'],
                                db_creds['name']),
                input=(db_creds['password'] + '\n').encode(),
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                raise Exception(f"Database export failed: {result.stderr.decode(errors='replace')}")
            with open(os.path.join(db_dir, 'wordpress.sql'), 'wb') as f:
                f.write(result.stdout)

        # 3. Copy wp-content from container, excluding:
        #    - creations/: bind-mounted from ~/OnionPress/Creations,
        #      often multi-GB of media. Lives on the host filesystem already,
        #      so docker-cp'ing it through the VM and then re-encrypting it
        #      into the zip is redundant work for a local backup.
        #    - .thumbs/: auto-generated macOS qlmanage thumbnails; regenerate
        #      on demand.
        #    - .DS_Store: Finder cruft.
        #
        #    Stream via `docker exec tar -cf -` instead of docker cp so the
        #    excludes apply at read time (no wasted IO pulling Creations
        #    across the VM boundary). The stream is unpacked here as plain
        #    files and folders only (unpack_plain_tar: links planted in the
        #    container are left out). --hard-dereference has the container's
        #    GNU tar store each name of a hard-linked file with its contents,
        #    not as a link entry, which would be left out too.
        with _phase_timer(log_func, 'BACKUP', 'wpcontent_tar'):
            log_func("Backup: copying wp-content (excluding Creations, thumbs, .DS_Store)...")
            wpcontent_dir = os.path.join(staging, 'wp-content')
            stage_wp_content(wpcontent_dir, log_func,
                             './creations', '.thumbs', '.DS_Store')

        # 3b. Drop WordPress's regenerable image derivatives (-WxH sizes and
        #     -scaled copies) from the staged uploads — about half of a
        #     media-heavy site's uploads (issue #257). Only files whose base
        #     image sits beside them count; true originals, video, audio
        #     and PDFs stay. The restore rebuilds the sizes in the
        #     background (derivatives.regenerate_if_pending); the marker
        #     written into uploads/ rides along in the archive so even an
        #     older OnionPress restoring this backup leaves the signal
        #     behind in the container.
        with _phase_timer(log_func, 'BACKUP', 'prune_derivatives'):
            derivatives.prune_image_derivatives(
                os.path.join(wpcontent_dir, 'uploads'), log_func)
            derivatives.write_archive_marker(wpcontent_dir)

        # 4. Backup OnionHeaven data if this is an OnionHeaven hub: the
        #    registry and the registrants' keys, when the volume holds any
        #    (hub_data_state). It lives on its own volume, in the onionheaven
        #    container and not in onionpress-wordpress (docs/DOCKER-SOCKET.md).
        #    This used to wait for a master-key.json that nothing has written
        #    for a long time, so no backup made here held a hub's data.
        #    Never in a backup handed to WordPress (include_hub_data=False):
        #    for that one the container is only asked whether this is a hub,
        #    to say so.
        is_onionheaven = False
        hub_data_left_out = False
        hub = hub_data_state()
        if hub is None:
            log_func("Backup: WARNING: the onionheaven container did not say whether "
                     "it holds OnionHeaven data; this backup does not")
        elif hub == "hub" and not include_hub_data:
            hub_data_left_out = True
            log_func(HUB_DATA_LEFT_OUT_LOG)
        elif hub == "hub":
            with _phase_timer(log_func, 'BACKUP', 'onionheaven_copy'):
                log_func("Backup: copying the OnionHeaven registry and registrants' keys...")
                is_onionheaven = True
                stage_hub_data(os.path.join(staging, 'onionheaven'), log_func)

        # 4b. Backup OnionHome name registry if this is an OnionHome instance.
        #     The DB is tiny (KB) but losing it strands every inbound
        #     /api/name/lookup/NAME that ever pointed here — it's the
        #     canonical record of who's who in the onion directory.
        #     It lives on its own volume, in onionpress-tor and not in
        #     onionpress-wordpress (docs/DOCKER-SOCKET.md); the archive
        #     keeps calling it onionhome/. Only the registry is copied, not
        #     what the volume's move keeps aside (ONIONHOME_REGISTRY_FILES).
        is_onionhome = False
        onionhome_check = subprocess.run(
            ['docker', 'exec', 'onionpress-tor',
             'test', '-f', f'{ONIONHOME_REGISTRY_DIR}/onionnames.db'],
            capture_output=True, timeout=10
        )
        if onionhome_check.returncode == 0:
            with _phase_timer(log_func, 'BACKUP', 'onionhome_copy'):
                log_func("Backup: copying OnionHome name registry...")
                is_onionhome = True
                onionhome_dir = os.path.join(staging, 'onionhome')
                os.makedirs(onionhome_dir)
                for name in ONIONHOME_REGISTRY_FILES:
                    # The write-ahead log exists only while SQLite has
                    # changes not yet in the db file: optional.
                    subprocess.run(
                        ['docker', 'cp',
                         f'onionpress-tor:{ONIONHOME_REGISTRY_DIR}/{name}',
                         os.path.join(onionhome_dir, name)],
                        capture_output=True, timeout=60,
                        check=(name == 'onionnames.db')
                    )
                scrub_staged_tree(onionhome_dir, log_func, 'onionhome')

        # 5. Save non-default config values. Not the host's secrets in a
        #    backup handed to WordPress (include_host_secrets=False): only
        #    what its status page shows anyway (wordpress_safe_config).
        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        config_path = os.path.join(_data_dir, 'config')
        secrets_left_out = []
        if os.path.exists(config_path):
            current = read_config(config_path)
            overrides = {k: v for k, v in current.items()
                         if k in DEFAULTS and v != DEFAULTS[k]}
            if not include_host_secrets:
                overrides, secrets_left_out = wordpress_safe_config(overrides, log_func)
            if overrides:
                with open(os.path.join(staging, 'config-overrides.json'), 'w') as f:
                    json.dump(overrides, f, indent=2)
                log_func(f"Backup: saved {len(overrides)} config override(s)")

        # 7. Write metadata
        metadata = {
            'onion_address': onion_address,
            'backup_date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'onionpress_version': version,
            'username': username,
            'is_onionheaven': is_onionheaven,
            'is_onionhome': is_onionhome,
            # Creations aren't inside wp-content anymore; they're in the
            # user's ~/OnionPress/Creations directory. Flag this
            # so restore can surface a useful "also copy your Documents"
            # reminder instead of silently producing an empty Creations page.
            'excludes_creations': True,
            # wp-content/uploads holds originals only; the -WxH/-scaled
            # copies were pruned and must be regenerated after a restore
            # (issue #257). Restore paths write the host-side marker that
            # the launchers act on; the archive's uploads/ marker covers
            # restores by versions that don't know this flag.
            'excludes_image_derivatives': True,
            # Format 2: encrypted container, not a ZipCrypto zip. Restores
            # pick the decoder from the file itself; this records it.
            'backup_format': backup_crypto.BACKUP_FORMAT,
        }
        if hub_data_left_out:
            # A hub's backup without its registry and registrants' keys, on
            # purpose and why; restores say so (restore_container_artifacts).
            metadata['excludes_onionheaven_data'] = True
            metadata['excludes_onionheaven_data_reason'] = HUB_DATA_LEFT_OUT_REASON
        if not include_host_secrets:
            # Which secrets were set and are not in here (names only): a
            # restore says what to enter again (restore_backup_config).
            metadata['excludes_host_secrets'] = True
            metadata['secret_config_left_out'] = secrets_left_out
        with open(os.path.join(staging, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # 8. Zip and encrypt (format 2). The phase keeps its old name so the
        #    BACKUP_PHASE timings stay comparable across versions (#214).
        with _phase_timer(log_func, 'BACKUP', 'zip_encrypt'):
            log_func("Backup: creating encrypted archive...")
            pack_backup(staging, output_path, password, log_func)

        log_func(f"BACKUP_PHASE: name=total elapsed_s={time.monotonic() - backup_start:.2f}")
        zip_bytes = os.path.getsize(output_path)
        log_func(f"Backup: complete ({zip_bytes / 1_048_576:.1f} MB)")
        return metadata

    finally:
        shutil.rmtree(staging, ignore_errors=True)


# Already-compressed formats that `zip -n` stores without deflating: deflate
# would only burn CPU on bytes that won't shrink.
_ZIP_STORE_SUFFIXES = ('.mov:.mp4:.m4v:.webm:.jpg:.jpeg:.png:.gif:.heic:.heif'
                       ':.pdf:.mp3:.m4a:.aac:.ogg:.webp:.zip:.gz')
_ZIP_TIMEOUT_S = 600


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
    readme = backup_crypto.readme_text(metadata.get('onionpress_version'))

    # stderr goes to a file, not a pipe: zip can warn once per file, and a
    # full stderr pipe nobody reads while we drain stdout would deadlock.
    with tempfile.TemporaryFile() as zip_err:
        proc = subprocess.Popen(
            ['zip', '-r', '-q', '-n', _ZIP_STORE_SUFFIXES, '-', '.'],
            cwd=staging, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=zip_err,
        )
        # Same 600 s budget subprocess.run(timeout=) gave the old zip call.
        killer = threading.Timer(_ZIP_TIMEOUT_S, proc.kill)
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


def staged_backup_address(staging, log_func):
    """Validate an extracted backup and return the onion address its key
    belongs to ('' if it carries no key).

    For the bash launchers' restore_backup and `setup --from-backup`, which
    name a directory after the address and write its prefix into config:
    they take the address from here, never from metadata.json. A metadata
    address that is well formed but differs from the key's is logged, as
    the Python restore paths do (older clients left it stale). Raises
    ValueError on invalid metadata or key files; nothing is touched.
    """
    metadata = read_staged_metadata(staging)
    address = key_manager.staged_key_address(_find_dir(staging, 'tor-keys'))
    metadata_address = metadata.get('onion_address', '')
    if address and metadata_address and metadata_address != address:
        log_func(f"Restore: KEY-MISMATCH — backup metadata.onion_address="
                 f"{metadata_address} != key_derives_to={address}; "
                 f"using DERIVED address for all persisted state.")
    return address


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


def _tree_bytes(root):
    """The total size of the regular files under *root* (lstat: links count
    as nothing)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


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
        deadline = time.monotonic() + _UNZIP_TIMEOUT_S
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


class SettingsPageRestoreRefused(ValueError):
    """A backup the Settings page's restore must not restore (its message
    says why and where to restore it instead)."""


def settings_page_restore_refusal(staging):
    """Why the Settings page's restore must not restore this extracted
    backup, or None.

    That restore's upload and password both came through WordPress, so
    code running there has already had them, and could have made the file
    itself. It never writes the OnionHeaven hub data or the OnionHome name
    registry (restore_container_artifacts(hub_volumes=False)): those
    volumes are kept out of WordPress's reach. A backup holding either is
    refused whole rather than restored without it: on a new machine the
    hub's (or OnionHome's) address would come up with no registrants' keys
    (or an empty registry, every name free to claim) under a "Restored"
    message, and the full restore from the app would have to follow anyway.

    Registrants' keys means any entry in onionheaven/keys/ (a key
    directory, as hub_data_state counts them). A registry alone does not
    count: backups made by Linux through 2.5.2 carry the empty one every
    node has. It is left out of the restore, and the backup goes ahead.

    OnionHome's name registry is no secret (the lookups serve it), and the
    Settings page's backups keep it. What matters there is who writes it:
    it left WordPress's volume so that code running there could not point
    a name at an onion address of its choosing, and restoring an upload
    WordPress could have made would hand that back.
    """
    keys = os.path.join(_find_dir(staging, 'onionheaven'), 'keys')
    if os.path.isdir(keys) and not os.path.islink(keys) and os.listdir(keys):
        return HUB_DATA_RESTORE_REFUSED_MSG
    if os.path.lexists(os.path.join(_find_dir(staging, 'onionhome'), 'onionnames.db')):
        return ONIONHOME_RESTORE_REFUSED_MSG
    return None


def extract_backup(zip_path, password, log_func, staging=None):
    """Extract a backup zip into a staging dir; return (staging, metadata).

    If *staging* is None a fresh temp dir is created (caller cleans it up).
    Pass an explicit *staging* (e.g. a persistent ~/.onionpress/restore-staging)
    when the extracted files must survive a container restart, as in
    install-from-backup. Validates the password as a side effect — a wrong
    password raises ValueError here before any state is touched.

    Handles both backup formats (extract_archive); also the bash launchers'
    restore paths, via `onionpress.cli extract-backup`.
    """
    own_staging = staging is None
    if own_staging:
        staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    try:
        with _phase_timer(log_func, 'RESTORE', 'zip_extract'):
            log_func("Restore: extracting backup archive...")
            extract_archive(zip_path, password, staging, log_func)
        return staging, read_staged_metadata(staging)
    except BaseException:
        # A refused backup leaves nothing behind in a dir made here; a
        # caller's dir is the caller's to remove.
        if own_staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise


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


def seed_onion_key_for_install(staging, metadata, log_func, *, data_dir=None):
    """Seed the backup's onion identity into host-side state so a fresh install
    adopts it WITHOUT generating a vanity key. Writes the key + hostname into
    ~/.onionpress/shared/vanity-keys/<addr>/ (the launcher's pre-imported-key
    path), updates the cached onion_address, and points ADDRESS_PREFIX/ONIONNAME
    at the restored identity. Returns the derived .onion address.

    Host-side only: does NOT touch a running container or the key volume.
    On the next launcher start the key is copied into onionpress-onion-keys
    and converted to C Tor's key files by the tor entrypoint.
    """
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    validate_backup_metadata(metadata)
    tor_dir = _find_dir(staging, 'tor-keys')
    key_path = os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private')
    if not os.path.exists(key_path):
        raise Exception("Backup is missing ks_hs_id.ed25519_expanded_private")

    with open(key_path, 'rb') as f:
        pem_data = f.read()
    priv, pub = key_manager.parse_openssh_key(pem_data)

    # Address is fully determined by the key; trust the derived value over
    # metadata.onion_address (older clients sometimes left it stale). The
    # key's public half must belong to its private half, or the address
    # would be whatever the backup's author wrote into the file.
    onion_address = key_manager.key_pair_onion_address(priv, pub)
    metadata_address = metadata.get('onion_address', '')
    if metadata_address and metadata_address != onion_address:
        log_func(f"Restore: KEY-MISMATCH — backup metadata.onion_address="
                 f"{metadata_address} != key_derives_to={onion_address}; "
                 f"using DERIVED address for all persisted state.")
    metadata['onion_address'] = onion_address

    cached_path = os.path.join(_data_dir, 'onion_address')
    try:
        with open(cached_path, 'w') as cf:
            cf.write(onion_address + '\n')
        log_func(f"Restore: updated cached onion_address to {onion_address}")
    except OSError as e:
        log_func(f"Restore: warning — could not update {cached_path}: {e}")

    with _phase_timer(log_func, 'RESTORE', 'vanity_sync'):
        vanity_dir = os.path.join(_data_dir, 'shared', 'vanity-keys')
        addr_dir = os.path.join(vanity_dir, onion_address)
        if os.path.isdir(vanity_dir):
            import datetime
            ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            shutil.move(vanity_dir, vanity_dir + f'.old{ts}')
        os.makedirs(addr_dir, exist_ok=True)
        shutil.copy2(key_path, os.path.join(addr_dir, 'ks_hs_id.ed25519_expanded_private'))
        with open(os.path.join(addr_dir, 'hostname'), 'w') as hf:
            hf.write(onion_address + '\n')
        log_func(f"Restore: synced vanity-keys for {onion_address}")

        addr_base = onion_address.replace('.onion', '')
        restored_username = metadata.get('username', '').strip()
        config_path = os.path.join(_data_dir, 'config')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as cf:
                clines = cf.readlines()
            found_prefix = found_onionname = False
            for i, line in enumerate(clines):
                if line.strip().startswith('ADDRESS_PREFIX='):
                    old_prefix = line.strip().split('=', 1)[1]
                    if not addr_base.startswith(old_prefix):
                        plen = min(max(len(old_prefix), 3), 6)
                        clines[i] = f'ADDRESS_PREFIX={addr_base[:plen]}\n'
                        log_func(f"Restore: updated ADDRESS_PREFIX to {addr_base[:plen]}")
                    found_prefix = True
                elif line.strip().startswith('ONIONNAME=') and restored_username:
                    clines[i] = f'ONIONNAME={restored_username}\n'
                    log_func(f"Restore: updated ONIONNAME to {restored_username}")
                    found_onionname = True
            if not found_prefix:
                clines.append(f'ADDRESS_PREFIX={addr_base[:3]}\n')
            if not found_onionname and restored_username:
                clines.append(f'ONIONNAME={restored_username}\n')
                log_func(f"Restore: set ONIONNAME to {restored_username}")
            with open(config_path, 'w', encoding='utf-8') as cf:
                cf.writelines(clines)

    return onion_address


INSTALL_FROM_BACKUP_MARKER = ".install-from-backup"


def prepare_install_from_backup(zip_path, password, log_func, *, data_dir=None):
    """Host-side prep for an install-from-backup. Extracts the backup into a
    PERSISTENT staging dir (<data_dir>/restore-staging, survives a container
    restart), seeds the onion key into vanity-keys/, applies config overrides,
    and writes the .install-from-backup marker (whose first line is the staging
    path) so a launcher start — or cmd_import_backup_artifacts — knows where to
    import from. Returns (staging, metadata).

    A wrong password raises during extraction, before any state is touched. The
    container-side import (restore_container_artifacts) runs later, against the
    freshly (re)started containers; the import step removes staging + marker on
    success, so the caller should NOT clean staging up here.
    """
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    staging = os.path.join(_data_dir, 'restore-staging')
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        os.chmod(staging, 0o700)
    except OSError:
        pass
    try:
        _, metadata = extract_backup(zip_path, password, log_func, staging=staging)
        seed_onion_key_for_install(staging, metadata, log_func, data_dir=_data_dir)
    except Exception:
        # Refused (invalid metadata or key) or failed before any marker
        # exists: nothing will import this staging, so don't leave the
        # decrypted backup behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    apply_config_overrides(staging, log_func, data_dir=_data_dir,
                           onion_address=metadata.get('onion_address', ''))
    marker = os.path.join(_data_dir, INSTALL_FROM_BACKUP_MARKER)
    with open(marker, 'w') as f:
        f.write(staging + '\n')
    log_func(f"Install-from-backup: staged at {staging} (marker written)")
    # The launcher start that imports this staging also kicks off the
    # background rebuild of the image sizes the backup left out (#257).
    derivatives.schedule_regeneration(_data_dir, log_func, metadata)
    return staging, metadata


def restore_from_backup(zip_path, password, log_func, *, data_dir=None,
                        through_wordpress=False):
    """Restore an OnionPress site from a backup zip.

    Args:
        zip_path: Path to the backup .zip
        password: Zip password
        log_func: Callable for progress logging
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.
        through_wordpress: True for the Settings page's restore, whose file
            and password came through WordPress: a backup holding an
            OnionHeaven hub's registrants' keys or OnionHome's name registry
            is refused before anything is touched
            (settings_page_restore_refusal), and neither volume is written.

    Returns:
        metadata dict from the backup

    Raises:
        ValueError on a wrong password; SettingsPageRestoreRefused.
    """
    restore_start = time.monotonic()
    staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    try:
        # Extract zip
        with _phase_timer(log_func, 'RESTORE', 'zip_extract'):
            log_func("Restore: extracting backup archive...")
            extract_archive(zip_path, password, staging, log_func)

        # Validated before anything is restored (validate_backup_metadata).
        metadata = read_staged_metadata(staging)

        # The Settings page's restore: refused before anything is touched.
        if through_wordpress:
            refusal = settings_page_restore_refusal(staging)
            if refusal:
                log_func(f"Restore: refused (the Settings page's restore): {refusal}")
                raise SettingsPageRestoreRefused(refusal)

        # Find extracted content directories
        tor_dir = _find_dir(staging, 'tor-keys')
        db_dir = _find_dir(staging, 'database')
        wpcontent_dir = _find_dir(staging, 'wp-content')

        # Workload stats — what we know up front (issue #214). The
        # post-import stats line below records DB row counts after the
        # SQL has been ingested.
        try:
            zip_bytes = os.path.getsize(zip_path)
        except OSError:
            zip_bytes = 0
        extracted_bytes = _dir_size_bytes(staging)
        sql_path_for_size = os.path.join(db_dir, 'wordpress.sql') if db_dir else ''
        sql_bytes = (os.path.getsize(sql_path_for_size)
                     if sql_path_for_size and os.path.exists(sql_path_for_size) else 0)
        log_func(f"RESTORE_STATS: zip_bytes={zip_bytes} "
                 f"extracted_bytes={extracted_bytes} sql_bytes={sql_bytes}")

        # 1. Restore Tor keys (OpenSSH PEM, the delivered-key format)
        with _phase_timer(log_func, 'RESTORE', 'tor_keys'):
            log_func("Restore: writing Tor keys...")
            key_path = os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private')
            if not os.path.exists(key_path):
                raise Exception("Backup is missing ks_hs_id.ed25519_expanded_private")

            with open(key_path, 'rb') as f:
                pem_data = f.read()
            priv, pub = key_manager.parse_openssh_key(pem_data)

            # Address is fully determined by the key — derive it here and
            # use the derived value for every address-keyed write that
            # follows (cache file, vanity-keys directory name, hostname
            # file, ADDRESS_PREFIX). Trusting metadata.onion_address instead
            # propagated corruption when older clients produced backups
            # with metadata.onion_address pointing at the previous address
            # while tor-keys/ already held the new key. The metadata value
            # is recorded only as a diagnostic check. Derived (and the key
            # checked for consistency) before the key is written anywhere.
            derived_address = key_manager.key_pair_onion_address(priv, pub)
            key_manager.write_private_key(priv, pub)
            metadata_address = metadata.get('onion_address', '')
            if metadata_address and metadata_address != derived_address:
                log_func(f"Restore: KEY-MISMATCH — backup "
                         f"metadata.onion_address={metadata_address} does "
                         f"not match key_derives_to={derived_address}. "
                         f"Using DERIVED address for all persisted state "
                         f"(cache file, vanity-keys directory, hostname, "
                         f"ADDRESS_PREFIX). The source instance that "
                         f"created this backup had a stale cached "
                         f"onion_address — before the 542cb61b server-side "
                         f"guard, it may have leaked the wrong (address, "
                         f"key) pair to OnionHeaven, clobbering some other "
                         f"address's stored takeover key.")
            metadata['onion_address'] = derived_address

            # Remove the key volume so it gets recreated from vanity-keys on
            # next launch. This avoids stale key mismatches. The pre-2026-09-25
            # name goes too, or the launcher's migration would bring it back.
            log_func("Restore: removing key volume for clean restart...")
            subprocess.run(
                ['docker', 'volume', 'rm', 'onionpress-onion-keys', 'onionpress-arti-state'],
                capture_output=True, timeout=15
            )

        # Sync vanity-keys directory on host so OnionHeaven detection and
        # prefix mismatch logic can see the restored onion address.
        onion_address = metadata.get('onion_address', '')
        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        if onion_address:
            # Overwrite the menubar's cached onion_address. The menubar reads
            # this file at startup into self.onion_address; if it still holds
            # the pre-restore address, a heartbeat that fires before the next
            # update_status poll will ship (content=OLD, key=NEW) and the
            # server clobbers KEYS_DIR/OLD/ with NEW's bytes — old address's
            # takeover key is then unrecoverable.
            cached_path = os.path.join(_data_dir, 'onion_address')
            try:
                with open(cached_path, 'w') as cf:
                    cf.write(onion_address + '\n')
                log_func(f"Restore: updated cached onion_address to {onion_address}")
            except OSError as e:
                log_func(f"Restore: warning — could not update {cached_path}: {e}")
            with _phase_timer(log_func, 'RESTORE', 'vanity_sync'):
                vanity_dir = os.path.join(_data_dir, 'shared', 'vanity-keys')
                addr_dir = os.path.join(vanity_dir, onion_address)
                # Rename existing vanity-keys dir so no stale keys cause the
                # launcher's head -1 to pick the wrong address. Kept as
                # .old<timestamp> for recovery if needed.
                if os.path.isdir(vanity_dir):
                    import datetime
                    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                    shutil.move(vanity_dir, vanity_dir + f'.old{ts}')
                os.makedirs(addr_dir, exist_ok=True)
                # Copy the key file so generate_vanity_address isn't needed
                shutil.copy2(key_path, os.path.join(addr_dir, 'ks_hs_id.ed25519_expanded_private'))
                # Write hostname file
                with open(os.path.join(addr_dir, 'hostname'), 'w') as hf:
                    hf.write(onion_address + '\n')
                log_func(f"Restore: synced vanity-keys for {onion_address}")

                # Update ADDRESS_PREFIX and ONIONNAME in config to match
                # restored address/username so the prefix mismatch detector
                # doesn't regenerate and backup filenames are correct.
                addr_base = onion_address.replace('.onion', '')
                restored_username = metadata.get('username', '').strip()
                config_path = os.path.join(_data_dir, 'config')
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as cf:
                        lines = cf.readlines()
                    found_prefix = False
                    found_onionname = False
                    for i, line in enumerate(lines):
                        if line.strip().startswith('ADDRESS_PREFIX='):
                            old_prefix = line.strip().split('=', 1)[1]
                            if not addr_base.startswith(old_prefix):
                                plen = min(max(len(old_prefix), 3), 6)
                                new_prefix = addr_base[:plen]
                                lines[i] = f'ADDRESS_PREFIX={new_prefix}\n'
                                log_func(f"Restore: updated ADDRESS_PREFIX to {new_prefix}")
                            found_prefix = True
                        elif line.strip().startswith('ONIONNAME=') and restored_username:
                            lines[i] = f'ONIONNAME={restored_username}\n'
                            log_func(f"Restore: updated ONIONNAME to {restored_username}")
                            found_onionname = True
                    if not found_prefix:
                        lines.append(f'ADDRESS_PREFIX={addr_base[:3]}\n')
                    if not found_onionname and restored_username:
                        lines.append(f'ONIONNAME={restored_username}\n')
                        log_func(f"Restore: set ONIONNAME to {restored_username}")
                    with open(config_path, 'w', encoding='utf-8') as cf:
                        cf.writelines(lines)

        # 2-6. Container-side artifacts + config overrides — shared with the
        # install-from-backup path (restore_container_artifacts / apply_config_overrides).
        restore_container_artifacts(staging, metadata, log_func,
                                    hub_volumes=not through_wordpress)
        apply_config_overrides(staging, log_func, data_dir=data_dir,
                               onion_address=metadata.get('onion_address', ''))

        # Image sizes the backup left out are rebuilt in the background by
        # the launcher's next start (the caller restarts after this) — #257.
        derivatives.schedule_regeneration(_data_dir, log_func, metadata)

        log_func(f"RESTORE_PHASE: name=total elapsed_s={time.monotonic() - restore_start:.2f}")
        log_func("Restore: files restored successfully")
        return metadata

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def restore_container_artifacts(staging, metadata, log_func, *, hub_volumes=True):
    """Import a backup's container-side state into the RUNNING db + wordpress
    containers: WordPress DB, wp-content, OnionHeaven data, OnionHome registry,
    and multisite constants. Shared by restore_from_backup (in-place) and the
    install-from-backup path. Requires onionpress-db + onionpress-wordpress up.

    hub_volumes=False (the Settings page's restore) never writes the
    OnionHeaven hub data or the OnionHome name registry: what the backup
    holds for them is left out and logged.
    """
    # docker cp follows a link in the source path (a `wp-content` link to
    # the home folder would copy that folder into the web root) and copies
    # links inside it as links, so nothing staged may be one. Extraction
    # already refused them; install-from-backup's staging has since sat in
    # ~/.onionpress across a restart.
    check_staged_tree(staging)
    db_dir = _find_dir(staging, 'database')
    wpcontent_dir = _find_dir(staging, 'wp-content')

    # 2. Restore database via mariadb CLI in the db container
    with _phase_timer(log_func, 'RESTORE', 'db_import'):
        log_func("Restore: importing database...")
        sql_path = os.path.join(db_dir, 'wordpress.sql')
        if not os.path.exists(sql_path):
            raise Exception("Backup is missing wordpress.sql")

        db_creds = _get_db_credentials()

        # Copy SQL into db container then import
        subprocess.run(
            ['docker', 'cp', sql_path, 'onionpress-db:/tmp/wordpress.sql'],
            capture_output=True, timeout=30, check=True
        )
        result = subprocess.run(
            _db_client_argv('mariadb',
                            '-u', db_creds['user'],
                            db_creds['name'],
                            '-e', 'source /tmp/wordpress.sql'),
            input=db_creds['password'] + '\n',
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"Database import failed: {result.stderr}")

        # Clean up SQL file in container
        subprocess.run(
            ['docker', 'exec', 'onionpress-db', 'rm', '-f', '/tmp/wordpress.sql'],
            capture_output=True, timeout=10
        )

        # Reconcile the wordpress mysql user's password with the
        # CURRENT install's secrets. The mariadb-dump only dumps
        # the wordpress database (not mysql.user), so the imported
        # SQL shouldn't touch grants — but if the user is restoring
        # into a DB volume that survived from a previous install
        # (docker compose down without -v, an interrupted scrub,
        # or any flow that left the data volume present), then
        # mysql.user still holds the OLD install's password hash
        # and the WP container can't connect.
        #
        # The fix is unconditional: always re-assert the wordpress
        # user's password to match what the current containers are
        # using (from env / wp-config / secrets). Idempotent — if
        # they already match, ALTER USER is effectively a no-op.
        #
        # The statement carries the password, so it goes in on stdin; the
        # root password comes from the db container's own environment.
        log_func("Restore: reconciling DB user password with current secrets...")
        alter_sql = (
            f"ALTER USER 'wordpress'@'%' "
            f"IDENTIFIED BY '{db_creds['password']}'; "
            f"FLUSH PRIVILEGES;"
        )
        alter = subprocess.run(
            ['docker', 'exec', '-i', 'onionpress-db', 'sh', '-c',
             'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mariadb -uroot'],
            input=alter_sql,
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=15,
        )
        if alter.returncode == 0:
            log_func("Restore: DB user password reconciled")
        else:
            # Don't fail the restore — the existing grant may already
            # be correct (the common case). Log so we have a breadcrumb
            # if WP can't connect after restore.
            log_func(f"Restore: WARNING — DB user password reconcile "
                     f"failed (rc={alter.returncode}): "
                     f"{alter.stderr.strip()[:200]}")

    # Post-import workload snapshot — row counts now reflect the
    # restored DB, so we can correlate ingest time with content size.
    _log_workload_stats(log_func, 'RESTORE_STATS_POST', db_creds)

    # 3. Restore wp-content
    if wpcontent_dir and os.path.isdir(wpcontent_dir):
        with _phase_timer(log_func, 'RESTORE', 'wpcontent_extract'):
            log_func("Restore: copying wp-content (themes, plugins, uploads)...")
            put_wp_content(wpcontent_dir, log_func)

    # Surface Creations-exclusion reminder. The backup intentionally
    # skips wp-content/creations (it's the host-side bind mount of
    # ~/OnionPress/Creations) — on this machine the bind
    # mount re-materializes Creations at container start, but on a
    # different machine the user has to bring their Documents folder
    # along too, or My Creations will be empty.
    if metadata.get('excludes_creations'):
        log_func("Restore: NOTE — backup does not include 'My Creations' files. "
                 "They live in ~/OnionPress/Creations/ — if restoring "
                 "on a new machine, copy that folder across separately.")

    # A hub's backup made from the Settings page: no registry, no keys.
    if metadata.get('excludes_onionheaven_data') is True:
        log_func("Restore: NOTE — this backup of an OnionHeaven hub was made "
                 "from the Settings page and holds neither the hub's registry "
                 "nor its registrants' keys; this restore leaves the hub data "
                 "as it is. Restore those from a backup made in the "
                 "OnionPress app or with onionpress backup.")

    # 4. Restore OnionHeaven data if present in backup: its registry and
    #    registrants' keys onto the OnionHeaven data volume, through the
    #    onionheaven container (restore_hub_data): never into
    #    onionpress-wordpress, root-owned, the registry through SQLite.
    #    Not from the Settings page's restore (hub_volumes=False).
    onionheaven_dir = _find_dir(staging, 'onionheaven')
    has_hub_data = os.path.isdir(onionheaven_dir) and (
        os.path.isfile(os.path.join(onionheaven_dir, 'registry.db'))
        or os.path.isdir(os.path.join(onionheaven_dir, 'keys')))
    if has_hub_data and not hub_volumes:
        log_func("Restore: the backup's OnionHeaven data left out: the "
                 "Settings page's restore never writes the hub data")
    elif has_hub_data:
        with _phase_timer(log_func, 'RESTORE', 'onionheaven_restore'):
            log_func("Restore: restoring the OnionHeaven registry and registrants' keys...")
            restore_hub_data(onionheaven_dir, log_func)

    # 4b. Restore OnionHome name registry if present in backup.
    #     Onto its own volume, through onionpress-tor (whose web-server.py
    #     is its only reader and writer): never into onionpress-wordpress,
    #     and root-owned, like the OnionHeaven data above.
    #     Only the registry's own files: an older backup also carries
    #     SQLite's -shm index (rebuilt on open), and nothing else in the
    #     archive belongs on that volume (its move marker among them).
    #     Not from the Settings page's restore (hub_volumes=False), which
    #     refuses such a backup first (settings_page_restore_refusal).
    onionhome_dir = _find_dir(staging, 'onionhome')
    has_name_registry = (os.path.isdir(onionhome_dir) and os.path.exists(
        os.path.join(onionhome_dir, 'onionnames.db')))
    if has_name_registry and not hub_volumes:
        log_func("Restore: the backup's OnionHome name registry left out: the "
                 "Settings page's restore never writes it")
    elif has_name_registry:
        with _phase_timer(log_func, 'RESTORE', 'onionhome_restore'):
            log_func("Restore: restoring OnionHome name registry...")
            for name in ONIONHOME_REGISTRY_FILES:
                path = os.path.join(onionhome_dir, name)
                if not os.path.isfile(path):
                    continue
                subprocess.run(
                    ['docker', 'cp', path,
                     f'onionpress-tor:{ONIONHOME_REGISTRY_DIR}/{name}'],
                    capture_output=True, timeout=60, check=True
                )
            log_func("Restore: OnionHome name registry restored")

    # 5. Re-add multisite constants to wp-config.php
    # WordPress Docker image generates a fresh wp-config.php without multisite
    # constants when the container is recreated. Without these, wp core
    # is-installed --network fails and ensure_multisite skips conversion.
    with _phase_timer(log_func, 'RESTORE', 'multisite_constants'):
        _ensure_multisite_constants(log_func)


def _one_of(*allowed):
    return lambda value, address: value in allowed


def _whole_number(lo, hi):
    return lambda value, address: (re.fullmatch(r"[0-9]{1,3}", value) is not None
                                   and lo <= int(value) <= hi)


def _prefix_of_restored_address(value, address):
    return (re.fullmatch(r"[a-z2-7]{2,5}", value) is not None
            and bool(address) and address.startswith(value))


# The config a restore takes from a backup: user preferences, each with the
# values it can hold. The backup's config file and config-overrides.json are
# the backup author's, like metadata.json, so nothing else is restored:
# not what runs (image overrides), where it listens (ports, bind host),
# where data goes (paths), what leaves the machine (CLOUDFLARE_TUNNEL_TOKEN,
# SHARE_ANALYTICS_WITH_ONIONHOME), the OnionHeaven hub and admission
# (REGISTER_WITH_ONIONHEAVEN, ONIONHEAVEN_*), or updates (UPDATE_ON_LAUNCH).
# Those keep this install's values; the user sets them again. ONIONNAME
# comes from the validated metadata username, and ADDRESS_PREFIX only when
# the restored address starts with it.
RESTORABLE_CONFIG = {
    "INSTALL_IA_PLUGIN": _one_of("yes", "no"),
    "LAUNCH_ON_LOGIN": _one_of("yes", "no"),
    "PREVENT_SLEEP": _one_of("normal", "on-battery", "never"),
    "VM_MEMORY": _whole_number(1, 64),
    "VM_CPU": _whole_number(1, 64),
    "ADDRESS_PREFIX": _prefix_of_restored_address,
}


def restore_backup_config(staging, config_path, log_func, onion_address=''):
    """Merge the restorable preferences (RESTORABLE_CONFIG) of a backup into
    *config_path*; every other key keeps this install's value.

    Reads the backup's config file (Linux backups carry the whole file) and
    its config-overrides.json (Python-made backups: the non-default
    values), the latter winning. *onion_address* is the restored address,
    derived from the key. Returns the keys written.
    """
    values = {}
    config_file = os.path.join(staging, 'config')
    if not os.path.isfile(config_file):
        config_file = os.path.join(staging, '.', 'config')
    if os.path.isfile(config_file):
        values.update(read_config(config_file))
    overrides_path = os.path.join(staging, 'config-overrides.json')
    if not os.path.exists(overrides_path):
        overrides_path = os.path.join(staging, '.', 'config-overrides.json')
    if os.path.isfile(overrides_path):
        try:
            with open(overrides_path, 'rb') as f:
                overrides = json.loads(f.read())
        except ValueError:
            overrides = None
        if isinstance(overrides, dict):
            values.update(overrides)
        else:
            log_func("Restore: WARNING — config-overrides.json is not a JSON "
                     "object; skipped")
    applied, kept = [], []
    for key, value in values.items():
        check = RESTORABLE_CONFIG.get(key)
        if check and isinstance(value, str) and check(value, onion_address):
            write_value(config_path, key, value)
            applied.append(key)
        else:
            kept.append(key)
    if applied:
        log_func(f"Restore: restored settings: {', '.join(applied)}")
    if kept:
        # Key names only (they are the backup author's too): no values.
        names = [k for k in kept if _CONFIG_KEY_NAME_RE.fullmatch(k)]
        log_func(f"Restore: kept this install's value for {len(kept)} "
                 f"setting(s) a restore does not take from a backup"
                 + (f": {', '.join(names[:20])}" if names else ""))
    _tunnel_token_note(staging, values, config_path, log_func)
    return applied


_CONFIG_KEY_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")


def _tunnel_token_note(staging, values, config_path, log_func):
    """Say what became of the Cloudflare tunnel when the backed-up site had
    one: its token is in the backup's config (a backup made from the app or
    the CLI) or named in metadata.json's secret_config_left_out (one made
    from the Settings page). Either way no restore takes it (a token from a
    crafted backup would put this site on the clear web through someone
    else's tunnel): this install's token stays, and without one the owner
    enters it again."""
    had_tunnel = bool(values.get(TUNNEL_TOKEN_KEY))
    if not had_tunnel:
        try:
            left_out = read_staged_metadata(staging).get('secret_config_left_out')
        except (OSError, ValueError):
            left_out = None
        had_tunnel = isinstance(left_out, list) and TUNNEL_TOKEN_KEY in left_out
    if not had_tunnel:
        return
    if read_value(config_path, TUNNEL_TOKEN_KEY, ""):
        log_func("Restore: kept this install's Cloudflare tunnel token (a "
                 "restore never takes one from a backup)")
    else:
        log_func("Restore: the backed-up site used a Cloudflare tunnel; a "
                 "restore never brings its token back. Enter it again in the "
                 "Settings page (Cloudflare Tunnel Token) to put the site back "
                 "on its tunnel.")


def wordpress_safe_config(values, log_func):
    """*values* (config key → value) cut to what a backup handed to
    WordPress may carry: config.redact_config, the keys WordPress's status
    page shows anyway. Returns (what is kept, the SECRET_CONFIG_KEYS that
    had a value and are left out, sorted). Logs the names of the keys left
    out, never a value."""
    kept = redact_config(values)
    left_out = sorted(k for k in values if k not in kept)
    secrets = [k for k in left_out if k in SECRET_CONFIG_KEYS and values[k]]
    if left_out:
        names = [k for k in left_out if _CONFIG_KEY_NAME_RE.fullmatch(k)]
        log_func(f"Backup: left {len(left_out)} setting(s) out of this backup, "
                 f"which is handed to WordPress"
                 + (f": {', '.join(names[:20])}" if names else ""))
    return kept, secrets


def write_wordpress_safe_config(config_path, dest, log_func):
    """Write the config file at *config_path*, cut by wordpress_safe_config,
    to *dest* (a new file, owner-only); return the secrets left out. For
    the Linux launcher's create_backup --without-host-secrets, whose
    backups carry the config file itself."""
    kept, secrets = wordpress_safe_config(read_config(config_path), log_func)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.writelines(f"{k}={v}\n" for k, v in kept.items())
    return secrets


def apply_config_overrides(staging, log_func, *, data_dir=None, onion_address=''):
    """Restore a backup's user preferences into ~/.onionpress/config
    (restore_backup_config)."""
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    with _phase_timer(log_func, 'RESTORE', 'config_overrides'):
        restore_backup_config(staging, os.path.join(_data_dir, 'config'),
                              log_func, onion_address)


def _ensure_multisite_constants(log_func):
    """Re-add multisite constants to wp-config.php after restore.

    This fixes the bug where WordPress Docker generates a fresh wp-config.php
    without MULTISITE, SUBDOMAIN_INSTALL, etc., causing wp core is-installed
    to fail and ensure_multisite to skip.

    Only adds constants if the database actually has multisite tables (wp_blogs).
    """
    # Check if the database has multisite tables before adding constants
    try:
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('db', 'query',
                                              "SHOW TABLES LIKE 'wp_blogs';"),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0 or 'wp_blogs' not in result.stdout:
            return  # Not a multisite install, nothing to do
    except (subprocess.TimeoutExpired, Exception):
        return  # Can't check, skip rather than break single-site installs

    log_func("Restore: re-adding multisite constants to wp-config.php...")
    for name, value in _MULTISITE_CONSTANTS.items():
        subprocess.run(
            ['docker'] + wp_files.wp_cli_args('config', 'set', name, value,
                                              '--raw', '--type=constant'),
            capture_output=True, timeout=15
        )


# The MariaDB clients in onionpress-db take the password from MYSQL_PWD,
# which this reads from the first line of stdin. As `-p<password>` it would
# sit in the host's process list, where `ps` shows every user's argv.
_DB_PASSWORD_FROM_STDIN = 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec "$@"'


def _db_client_argv(*client_argv):
    """argv that runs a MariaDB client in onionpress-db. Feed the password
    as the first line of stdin: ``input=password + '\\n'``."""
    return ['docker', 'exec', '-i', 'onionpress-db',
            'sh', '-c', _DB_PASSWORD_FROM_STDIN, 'sh', *client_argv]


def _get_db_credentials():
    """Read WordPress database credentials from wp-config.php via WP-CLI."""
    creds = {}
    for field in ('DB_NAME', 'DB_USER', 'DB_PASSWORD'):
        result = subprocess.run(
            ['docker'] + wp_files.wp_cli_args('config', 'get', field),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            raise Exception(f"Could not read {field} from WordPress config")
        creds[field] = result.stdout.strip()
    return {
        'name': creds['DB_NAME'],
        'user': creds['DB_USER'],
        'password': creds['DB_PASSWORD'],
    }


def _find_dir(staging, name):
    """Find a directory inside the staging area, handling ./ prefix from zip."""
    path = os.path.join(staging, name)
    if os.path.isdir(path):
        return path
    path = os.path.join(staging, '.', name)
    if os.path.isdir(path):
        return path
    return os.path.join(staging, name)  # return expected path even if missing


def backup_filename(onion_address, username):
    """Generate the default backup filename."""
    # Strip .onion suffix for brevity in filename
    addr_short = onion_address.replace('.onion', '') if onion_address else 'unknown'
    # Use first 8 chars of onion address
    addr_prefix = addr_short[:8]
    ts = datetime.now().strftime('%Y-%m-%d-%H-%M')
    return f"OnionPress-{addr_prefix}-{username}-{ts}.zip"


# backup_filename()'s shape. The Settings page names its backups without a
# username (OnionPress-<addr>-<ts>.zip), which this does not match.
_BACKUP_FILENAME_RE = re.compile(
    r"OnionPress-[^-]*-(?P<user>.+)-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}\.zip")


def backup_filename_username(path):
    """The username backup_filename() put in *path*'s name, or None.

    For the restore password prompt's wording only. A file name is whatever
    the file is called now, not something the backup vouches for: anyone
    can hand over "OnionPress-x-<any text>-<ts>.zip" and have the text shown
    in that dialog. So the name must look like a login (the rule metadata's
    username is held to), or there is none: no newlines, bidi overrides or
    paragraphs, at most 60 characters. Never use it in a path or a command.
    """
    if not isinstance(path, str):
        return None
    m = _BACKUP_FILENAME_RE.fullmatch(os.path.basename(path))
    if not m:
        return None
    user = m.group('user').strip()
    return user if _BACKUP_USERNAME_RE.fullmatch(user) else None
