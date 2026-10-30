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
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from onionpress import backup_crypto, derivatives, hub_backup, key_manager, wp_files
from onionpress.config import (DEFAULTS, SECRET_CONFIG_KEYS, load_secrets,
                               read_config, read_value, redact_config,
                               write_value)

# This module keeps the orchestration (create_backup, the restores, the
# Settings page's refusals, config). The archive format and its checks live
# in backup_archive, the hub data's staging and sealing in hub_backup, the
# database clients and the sandboxed import in db_import. Every name moved
# there stays importable from here, so callers (and tests' mock.patch
# targets for code that stayed here) are unchanged.
from onionpress.backup_archive import (  # noqa: F401
    _BACKUP_USERNAME_RE, check_archive_size, _check_backup_format,
    check_staged_tree, check_uploaded_backup, extract_archive, _free_bytes,
    INVALID_ARCHIVE_MSG, INVALID_METADATA_MSG, _log_left_out,
    _make_staged_dirs, _MAX_ARCHIVE_MEMBERS, _MAX_EXPANSION,
    _MAX_METADATA_BYTES, _member_label, _member_path, metadata_text,
    METADATA_TOO_BIG_MSG, _MIN_UNPACK_BUDGET, MISMATCH_MSG,
    _onion_key_readme_text, OVERSIZE_MSG, pack_backup, _parse_metadata,
    peek_backup_metadata, _PLAIN_MEMBER_TYPES, read_backup_metadata,
    _read_metadata_member, read_staged_metadata, scrub_staged_tree, _size_text,
    _STAGE_BUFSIZE, _stage_container_tar, _STAGE_COPY_FLOOR_S,
    _STAGE_LOG_LIMIT, _staged_name, _tar_member_kind, TOO_BIG_MSG,
    TOO_MANY_MEMBERS_MSG, _tree_bytes, unpack_plain_tar, _unzip_cli_extract,
    _UNZIP_POLL_S, _UNZIP_TIMEOUT_S, _validate_archive_file,
    validate_archive_members, validate_backup_metadata,
    _wrong_password_as_valueerror, _WRONG_PASSWORD_MSG, _ZIP_FLOOR_S,
    _ZIP_STORE_SUFFIXES)
from onionpress.db_import import (  # noqa: F401
    _assert_db_client_sandboxed, _db_client_argv, _DB_DUMP_FLOOR_S, db_import,
    _DB_IMPORT_BYTES_PER_S, _DB_IMPORT_FLOOR_S, _DB_IMPORT_SAFE_FLAGS,
    _DB_PASSWORD_FROM_STDIN, _DB_VALUE_FORBIDDEN, _get_db_credentials,
    _reject_unsafe_db_value, _run_db_client_to_file)
from onionpress.hub_backup import (  # noqa: F401
    HUB_DATA_NOT_RESTORED_NOTE, HUB_DATA_RESTORED_LOG,
    HUB_DATA_SEALED_SKIPPED_MSG, hub_data_state, HUB_DATA_STATE_SH,
    HUB_DATA_TAR_SH, HUB_DATA_UNSEALED_NOTE, HUB_DATA_UNSEALED_WARNING,
    _hub_readme_text, _keys_tar, ONIONHEAVEN_DATA_DIR, open_sealed_hub_data,
    read_hub_data, read_sealed_hub_member, REGISTRY_RESTORE_SH,
    restore_hub_data, sealed_skipped_message, stage_hub_backup, stage_hub_data,
    _V3_SQL, write_hub_data)


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

# What never passes through WordPress in a backup. The Settings page's backup
# is handed to the WordPress container for download, and its password is the
# admin password WordPress sees in plain text: code running there would have
# both. On an OnionHeaven hub or on OnionHome that backup is refused, and
# on a node whose checks did not answer, since that could be either
# (create_backup(include_hub_data=False) raises SettingsPageBackupRefused;
# Linux: create_backup --without-hub-data returns 3): even without the hub's
# registry and registrants' keys it carries the node's own onion service
# key, and with it code in WordPress could run a rival hub that collects
# registrants' keys from their heartbeats, or a rival OnionHome answering
# name lookups (settings_page_backup_refusal). Backups made from the app or
# the CLI, written to the owner's disk, keep the hub data, sealed to the
# hub's recovery key (hub_backup). The Settings page's restore, whose upload
# and password came through WordPress too, is refused outright on a hub or
# on OnionHome, or on a node that could not be checked
# (settings_page_restore_node_refusal: it would replace the node's own key
# and site with an upload WordPress could have made), refuses a backup
# holding registrants' keys (sealed or not) or OnionHome's name registry
# (settings_page_restore_refusal) and never writes either volume.
#
# Shown on the Settings page: plain text, safe in JSON and HTML as it stands
# (a test holds them to that).
HUB_BACKUP_REFUSED_MSG = (
    "This is an OnionHeaven hub. A backup made here would carry the hub's own "
    "onion service key through WordPress, and code running there could use it "
    "to run a rival hub that collects registrants' keys. Nothing was backed "
    "up: back the hub up from the OnionPress app (Backup... in its menu) or "
    "with onionpress backup.")
ONIONHOME_BACKUP_REFUSED_MSG = (
    "This is OnionHome. A backup made here would carry its onion service key "
    "through WordPress, and code running there could use it to answer name "
    "lookups in its place. Nothing was backed up: back it up from the "
    "OnionPress app (Backup... in its menu) or with onionpress backup.")
UNVERIFIED_BACKUP_REFUSED_MSG = (
    "OnionPress could not check whether this node is an OnionHeaven hub or "
    "OnionHome (its containers did not answer), and a backup made here goes "
    "through WordPress. Nothing was backed up: try again once OnionPress is "
    "running, or back up from the OnionPress app (Backup... in its menu) or "
    "with onionpress backup.")
HUB_DATA_RESTORE_REFUSED_MSG = (
    "This backup holds an OnionHeaven hub's registry and its registrants' "
    "onion service keys, which are never restored through WordPress. Nothing "
    "was restored: restore it from the OnionPress app (Restore... in its "
    "menu) or with onionpress restore.")
ONIONHOME_RESTORE_REFUSED_MSG = (
    "This backup holds OnionHome's name registry, which is never restored "
    "through WordPress. Nothing was restored: restore it from the OnionPress "
    "app (Restore... in its menu) or with onionpress restore.")
# The Settings page's restore on a hub or OnionHome, or on a node whose
# checks did not answer (settings_page_restore_node_refusal): refused before
# the upload is opened, whatever it holds.
HUB_RESTORE_HERE_REFUSED_MSG = (
    "This is an OnionHeaven hub. A restore made here could replace the hub's "
    "own onion service key and its site with a backup uploaded through "
    "WordPress. Nothing was restored: restore the hub from the OnionPress app "
    "(Restore... in its menu) or with onionpress restore.")
ONIONHOME_RESTORE_HERE_REFUSED_MSG = (
    "This is OnionHome. A restore made here could replace its onion service "
    "key and its site with a backup uploaded through WordPress. Nothing was "
    "restored: restore it from the OnionPress app (Restore... in its menu) or "
    "with onionpress restore.")
UNVERIFIED_RESTORE_REFUSED_MSG = (
    "OnionPress could not check whether this node is an OnionHeaven hub or "
    "OnionHome (its containers did not answer), and a restore made here comes "
    "through WordPress. Nothing was restored: try again once OnionPress is "
    "running, or restore from the OnionPress app (Restore... in its menu) or "
    "with onionpress restore.")

# The site's own onion service key never changes through WordPress without
# the owner at the host. Anything running in WordPress can write the
# Settings page's requests, so:
# - The Mac's Settings page backup leaves the key out
#   (create_backup(include_onion_key=False), metadata excludes_onion_key,
#   ONION_KEY_LEFT_OUT_NOTE with the result and in README.txt): the backup
#   and its password are both WordPress's to read. The app's Backup... and
#   `onionpress backup` keep it. Linux's Settings page backup keeps it for
#   now (headless installs back up from the page).
# - A restore of a backup without a key keeps the address of the install it
#   is restored onto (staged_onion_key_address is '': no key written, no
#   volume removed, no hub asked to let go), or is refused when that address
#   cannot be read (NO_KEY_TO_KEEP_MSG): it never mints a new one.
# - The Settings page's restore of a backup of another address needs the
#   person at the Mac (restore_from_backup(confirm_address_change=), a
#   native dialog; ADDRESS_CHANGE_NOT_CONFIRMED_MSG otherwise). Linux, which
#   has no one to ask, refuses it (ADDRESS_CHANGE_REFUSED_MSG, restore_backup
#   --from-settings-page) and refuses the page's key import outright.
ONION_KEY_LEFT_OUT_NOTE = (
    "This backup does not contain your onion address's key; to move your "
    "address to another machine, use Backup... in the OnionPress menu.")
ADDRESS_CHANGE_NOT_CONFIRMED_MSG = (
    "Not restored: this backup is of another onion address, and restoring it "
    "replaces this site's address only when the person at the Mac running "
    "OnionPress clicks Restore in the dialog OnionPress shows there.")
ADDRESS_CHANGE_REFUSED_MSG = (
    "Not restored: this backup is of another onion address, and a restore "
    "from this page never replaces this site's address. Restore it on the "
    "computer running OnionPress: onionpress restore <backup file>")
NO_KEY_TO_KEEP_MSG = (
    "This backup holds no onion service key (it was made from the Settings "
    "page in WordPress), so restoring it keeps the onion address of the "
    "install it is restored onto, and OnionPress could not read that address "
    "(OnionPress is not running, or this install has none yet). Nothing was "
    "restored: start OnionPress, or on a new machine set up a new site first, "
    "then restore the backup with Restore... in the OnionPress menu.")
KEY_IN_KEYLESS_BACKUP_MSG = (
    "Not a valid OnionPress backup: its metadata.json says it holds no onion "
    "service key, but it does.")

# The host's secrets never pass through WordPress in a backup either. The
# Settings page's backup (create_backup(include_host_secrets=False); Linux:
# create_backup --without-host-secrets) carries only the config WordPress's
# status page already shows (config.redact_config, SAFE_CONFIG_KEYS): never
# the Cloudflare tunnel token, which the page's write-only field keeps out of
# WordPress, nor (Linux, whose backups carry the whole config file) image
# overrides, bind host or hub admission. Linux also leaves out the
# healthcheck onion service's key, which a start regenerates. The site's
# own onion key stays on Linux; the Mac leaves it out too
# (ONION_KEY_LEFT_OUT_NOTE above). metadata.json says so
# (excludes_host_secrets) and names the secrets that were set
# (secret_config_left_out, names only). No restore takes a secret from a
# backup anyway (RESTORABLE_CONFIG): this install's token is kept, and on a
# new machine the owner enters it again (restore_backup_config says so).
TUNNEL_TOKEN_KEY = "CLOUDFLARE_TUNNEL_TOKEN"

# The two nodes whose own onion service key is the service, as the tor image
# knows them (onionheaven_common.ONIONHEAVEN_HUB_ADDRESS, web-server.py's
# _ONIONHOME_ADDRESS; a test keeps the copies equal). Any node with
# ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes is a hub too.
ONIONHEAVEN_HUB_ADDRESS = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
ONIONHOME_ADDRESS = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
# An owner's backup whose hub or OnionHome check did not answer (a container
# stopped or restarting, Docker busy, a registry sqlite3 could not read):
# never taken for "not a hub" or "not OnionHome". The backup is made without
# that data, and says so: the log (the WARNINGs), metadata.json
# (onionheaven_data_unchecked, onionhome_registry_unchecked) and, with the
# finished backup, the NOTEs (unchecked_notes: the Mac's finished-backup
# message, `onionpress backup`; Linux create_backup prints the hub's).
HUB_DATA_UNCHECKED_WARNING = (
    "Backup: WARNING: the onionheaven container did not say whether it holds "
    "OnionHeaven data; this backup does not")
ONIONHOME_REGISTRY_UNCHECKED_WARNING = (
    "Backup: WARNING: the onionpress-tor container did not say whether "
    "OnionHome's name registry is on its volume; this backup does not hold it")
HUB_DATA_UNCHECKED_NOTE = (
    "OnionPress could not check whether this node is an OnionHeaven hub (its "
    "onionheaven container did not answer, or could not read the hub "
    "registry), so this backup holds no hub registry or registrants' keys. "
    "If this is a hub, make another backup once OnionPress is running.")
ONIONHOME_REGISTRY_UNCHECKED_NOTE = (
    "OnionPress could not check whether this node is OnionHome (its "
    "onionpress-tor container did not answer), so this backup does not hold "
    "OnionHome's name registry. If this is OnionHome, make another backup "
    "once OnionPress is running.")


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
    """Emit a single STATS line and return its figures (empty if they
    could not be read): the backup sizes its time limits from them. Never
    raises — instrumentation must not break the operation it's measuring."""
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
        return dict(stats, wpcontent_bytes=wpcontent_bytes,
                    onionheaven_bytes=onionheaven_bytes)
    except Exception as e:
        log_func(f"{label}: error={e}")
        return {}


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
        result = wp_files.wp_cli('user', 'get', username, '--field=roles',
                                 timeout=15)
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
        result = wp_files.wp_cli('eval', php_code,
                                 input=username + "\n" + password, timeout=15)
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
        result = wp_files.wp_cli('eval', php_code, input=password, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return (True, result.stdout.strip())
        return (False, "Password does not match any administrator account.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out verifying password.")
    except Exception as e:
        return (False, f"Error verifying password: {e}")


def find_admin_username(data_dir=None):
    """The WordPress admin user's login, and whether it was found in
    WordPress (so the caller may record it as ONIONNAME).

    Order: ONIONNAME in config → first admin from `wp user list
    --role=administrator` → "admin" as a last resort. Writes nothing
    (issue #474): a caller that wants the login recorded does it through
    config's checked writer (config.set_checked_value).
    """
    data_dir = data_dir or _default_data_dir()
    config_path = os.path.join(data_dir, "config")
    name = read_value(config_path, "ONIONNAME", "").strip()
    if name:
        return name, False
    try:
        result = wp_files.wp_cli('user', 'list', '--role=administrator',
                                 '--field=user_login', timeout=10)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                login = line.strip()
                if login:
                    return login, True
    except Exception:
        pass
    return "admin", False


def get_admin_username(data_dir=None, log=None):
    """find_admin_username's login. With *log*, a login found in WordPress
    is recorded as ONIONNAME through config's checked writer, so the next
    call takes the fast path."""
    data_dir = data_dir or _default_data_dir()
    login, found = find_admin_username(data_dir)
    if found and log is not None:
        from . import config as op_config
        try:
            op_config.set_checked_value(data_dir, "ONIONNAME", login, log)
        except OSError as e:
            log(f"WARNING: could not record ONIONNAME: {e}")
    return login


# ─── wp-content in and out of the WordPress container ──────────────────
# Staged as plain files only (backup_archive: unpack_plain_tar,
# scrub_staged_tree), put back as www-data (wp_files.put_tree).


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


def stage_wp_content(dest, log_func, *excludes, wpcontent_bytes=None):
    """Copy wp-content out of the WordPress container into *dest* as plain
    files and folders (wp_content_tar_argv, unpack_plain_tar), then
    scrub_staged_tree. The Linux launcher's create_backup runs this through
    `onionpress.cli stage-wp-content`. The time allowed is sized from
    *wpcontent_bytes*, measured in the container when not given."""
    if wpcontent_bytes is None:
        wpcontent_bytes = _du_bytes('onionpress-wordpress',
                                    '/var/www/html/wp-content',
                                    excludes=tuple(e.removeprefix('./') for e in excludes))
    os.makedirs(dest, exist_ok=True)
    _stage_container_tar(wp_content_tar_argv(*excludes), dest, log_func,
                         'wp-content',
                         timeout=wp_files.sized_timeout(wpcontent_bytes,
                                                        _STAGE_COPY_FLOOR_S))
    scrub_staged_tree(dest, log_func, 'wp-content')


def put_wp_content(src, log_func, timeout=None):
    """Unpack the staged *src* over wp-content in the WordPress container,
    as www-data (wp_files.put_tree). Not docker cp + chown -R as root: the
    copy followed links www-data can plant in wp-content (the destination's
    own `wp-content/` too), and so did the trailing-slash chown. *timeout*
    defaults to one sized to *src* (wp_files.put_tree)."""
    r = wp_files.put_tree(src, '/var/www/html/wp-content', merge=True,
                          timeout=timeout)
    if r.returncode != 0:
        log_func(f"Restore: wp-content copy failed: {r.stderr.strip()[-300:]}")
        raise Exception("Could not copy wp-content into the WordPress container")


def accepts_registrations(data_dir=None):
    """True if this node's config opts it in as an OnionHeaven hub
    (ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes, read as the tor image reads it:
    onionheaven_common.registrations_opted_in)."""
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    value = read_value(os.path.join(_data_dir, 'config'),
                       'ONIONHEAVEN_ACCEPT_REGISTRATIONS', '')
    return value.strip().lower() == 'yes'


def onionhome_registry_present():
    """True if OnionHome's name registry is on its volume (only OnionHome
    has one), False if it is not, None if the tor container did not say
    (a failed `docker exec` and a missing file both exit 1, so the answer
    comes on stdout)."""
    try:
        r = subprocess.run(['docker', 'exec', 'onionpress-tor', 'sh', '-c',
                            'if [ -f "$1" ]; then echo yes; else echo no; fi', 'sh',
                            f'{ONIONHOME_REGISTRY_DIR}/onionnames.db'],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = r.stdout.strip() if r.returncode == 0 else ""
    return {"yes": True, "no": False}.get(answer)


def unchecked_notes(metadata):
    """The NOTEs for what create_backup could not check and so left out
    (*metadata* is what it returned), in the order the backup asked."""
    if not isinstance(metadata, dict):
        return []
    return [note for key, note in (
        ('onionheaven_data_unchecked', HUB_DATA_UNCHECKED_NOTE),
        ('onionhome_registry_unchecked', ONIONHOME_REGISTRY_UNCHECKED_NOTE))
        if metadata.get(key) is True]


class SettingsPageBackupRefused(ValueError):
    """The Settings page's backup is refused on this node (its message says
    why and where to back it up instead)."""


def settings_page_backup_refusal(own_address, hub_state, *, data_dir=None):
    """Why the Settings page must not back this node up, or None.

    That backup is handed to WordPress, under a password WordPress saw,
    and it carries the node's own onion service key. On an OnionHeaven hub
    that key is what registrants send their keys to: code in WordPress
    holding it could run a rival hub and collect them from heartbeats. On
    OnionHome it answers name lookups. So a node is refused when it is
    either, by any of: its own address (*own_address*, derived from the key
    the backup would carry) is the canonical hub's or OnionHome's; its
    config opts it in as a hub (accepts_registrations); its hub data volume
    holds registrants' keys or registry rows (*hub_state* "hub", from
    hub_data_state); OnionHome's name registry is on its volume. Not
    status.json's onionheaven.server_active: on Linux that is true on every
    node.

    Fails closed: a node any of these could not be asked about (no own
    address, *hub_state* anything but "none", no answer about OnionHome's
    registry) is refused too (UNVERIFIED_BACKUP_REFUSED_MSG), since it
    could be either. This backup needs the containers anyway, so the cost
    is a retry, never a hub's or OnionHome's key handed to WordPress.
    """
    return {"hub": HUB_BACKUP_REFUSED_MSG,
            "onionhome": ONIONHOME_BACKUP_REFUSED_MSG,
            "unverified": UNVERIFIED_BACKUP_REFUSED_MSG,
            }.get(_settings_page_node(own_address, hub_state, data_dir))


def _settings_page_node(own_address, hub_state, data_dir):
    """"hub", "onionhome", "unverified" (could be either) or None: what the
    Settings page's backup and restore are refused on
    (settings_page_backup_refusal says how it is told, and fails closed)."""
    if (own_address == ONIONHEAVEN_HUB_ADDRESS or hub_state == "hub"
            or accepts_registrations(data_dir)):
        return "hub"
    if own_address == ONIONHOME_ADDRESS:
        return "onionhome"
    onionhome = onionhome_registry_present()
    if onionhome:
        return "onionhome"
    if not own_address or hub_state != "none" or onionhome is None:
        return "unverified"
    return None


def settings_page_restore_node_refusal(own_address, hub_state, *, data_dir=None):
    """Why the Settings page must not restore anything on this node, or None.

    The node is told as settings_page_backup_refusal tells it (*own_address*
    the address of the key the tor container holds, *hub_state* from
    hub_data_state), and fails closed the same way. On an OnionHeaven hub or
    OnionHome a restore from the page would put a backup WordPress could
    have made in place of the node's own site and onion service key, the
    one registrants send their keys to or that answers name lookups. So it
    is refused whatever the upload holds, before it is opened: those nodes
    are restored from the app or `onionpress restore`.
    """
    return {"hub": HUB_RESTORE_HERE_REFUSED_MSG,
            "onionhome": ONIONHOME_RESTORE_HERE_REFUSED_MSG,
            "unverified": UNVERIFIED_RESTORE_REFUSED_MSG,
            }.get(_settings_page_node(own_address, hub_state, data_dir))


def served_onion_address():
    """The onion address of the key the tor container holds, derived from
    the key (key_manager.extract_keys; private and public half must agree),
    or '' when it could not be read. The address a restore keeps or would
    replace."""
    try:
        return key_manager.key_pair_onion_address(*key_manager.extract_keys())
    except Exception:
        return ''


def create_backup(onion_address, username, password, output_path, version, log_func, *,
                  data_dir=None, include_hub_data=True, include_host_secrets=True,
                  include_onion_key=True):
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
            Settings page's download): refused with SettingsPageBackupRefused
            on an OnionHeaven hub or OnionHome, or on a node whose checks did
            not answer (settings_page_backup_refusal), before anything is
            copied. True for one written to the owner's
            disk: a hub's registry and registrants' keys go in, sealed to the
            hub's recovery key when one is set up (stage_hub_backup). Every
            caller passes it.
        include_host_secrets: False for that backup too: its config is cut
            to what WordPress's status page shows (wordpress_safe_config: no
            Cloudflare tunnel token), and metadata.json says so
            (excludes_host_secrets). Every caller passes it.
        include_onion_key: False for the Mac Settings page's backup: the
            site's onion service key is left out (no tor-keys/), so code in
            WordPress, which has the backup and its password, never has the
            key. metadata.json says so (excludes_onion_key), and so do
            README.txt and ONION_KEY_LEFT_OUT_NOTE; a restore of it keeps
            the address it is restored onto (staged_onion_key_address). The
            address is still derived from the key, for the hub check and
            metadata.json. Every caller passes it.
    """
    backup_start = time.monotonic()
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    staging = tempfile.mkdtemp(prefix='onionpress-backup-')
    try:
        # 0. Workload stats — log once up front (issue #214). Read DB
        #    creds here so we can reuse them for the dump phase below
        #    without paying for two round trips through wp-cli.
        db_creds = _get_db_credentials()
        stats = _log_workload_stats(log_func, 'BACKUP_STATS', db_creds)

        # 1. Extract Tor keys (OpenSSH PEM, the delivered-key format). Not
        #    into a backup handed to WordPress on the Mac
        #    (include_onion_key=False): only the address is derived.
        with _phase_timer(log_func, 'BACKUP', 'tor_keys'):
            if include_onion_key:
                log_func("Backup: extracting Tor keys...")
                tor_dir = os.path.join(staging, 'tor-keys')
                os.makedirs(tor_dir)

                priv = key_manager.extract_private_key()
                pub = key_manager.extract_public_key()
                pem_data = key_manager.build_openssh_key(priv, pub)
                with open(os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private'), 'wb') as f:
                    f.write(pem_data)
            else:
                log_func("Backup: the onion service key is left out of this "
                         "backup, which is handed to WordPress")
                pub = key_manager.extract_public_key()

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

        # 1b. Is this an OnionHeaven hub (its volume holds registrants' keys
        #     or registry rows)? A backup handed to WordPress (the Settings
        #     page's) is refused on a hub or on OnionHome, or when that could
        #     not be checked, before anything else is copied: the key just
        #     staged is the node's own.
        hub = hub_data_state()
        if not include_hub_data:
            refusal = settings_page_backup_refusal(onion_address, hub, data_dir=_data_dir)
            if refusal:
                log_func(f"Backup: refused (the Settings page's backup): {refusal}")
                raise SettingsPageBackupRefused(refusal)

        # 2. Dump WordPress database via mariadb-dump in the db container
        # (wp db export uses mysqldump which isn't in the WordPress container)
        with _phase_timer(log_func, 'BACKUP', 'db_dump'):
            log_func("Backup: exporting database...")
            db_dir = os.path.join(staging, 'database')
            os.makedirs(db_dir)

            # Streamed to the file, not held in memory, with a time limit
            # sized from the database (issue #317).
            _run_db_client_to_file(
                _db_client_argv('mariadb-dump',
                                '-u', db_creds['user'],
                                db_creds['name']),
                db_creds['password'],
                os.path.join(db_dir, 'wordpress.sql'),
                wp_files.sized_timeout(stats.get('db_bytes', 0),
                                       _DB_DUMP_FLOOR_S),
                'Database export')

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
                             './creations', '.thumbs', '.DS_Store',
                             wpcontent_bytes=stats.get('wpcontent_bytes'))

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
        #    Sealed to the hub's recovery key when one is set up, else in
        #    the clear under the password with a warning (stage_hub_backup).
        #    Never in a backup handed to WordPress (include_hub_data=False),
        #    which a hub refused at 1b. No answer (None) is never "not a
        #    hub": the backup goes ahead without the hub data and says so
        #    (the log, metadata onionheaven_data_unchecked, unchecked_notes).
        is_onionheaven = False
        hub_fields = {}
        if hub is None:
            log_func(HUB_DATA_UNCHECKED_WARNING)
        elif hub == "hub" and include_hub_data:
            with _phase_timer(log_func, 'BACKUP', 'onionheaven_copy'):
                log_func("Backup: copying the OnionHeaven registry and registrants' keys...")
                is_onionheaven = True
                hub_fields = stage_hub_backup(staging, log_func, data_dir=_data_dir)

        # 4b. Backup OnionHome name registry if this is an OnionHome instance.
        #     The DB is tiny (KB) but losing it strands every inbound
        #     /api/name/lookup/NAME that ever pointed here — it's the
        #     canonical record of who's who in the onion directory.
        #     It lives on its own volume, in onionpress-tor and not in
        #     onionpress-wordpress (docs/DOCKER-SOCKET.md); the archive
        #     keeps calling it onionhome/. Only the registry is copied, not
        #     what the volume's move keeps aside (ONIONHOME_REGISTRY_FILES).
        #     Asked as the Settings page's check asks
        #     (onionhome_registry_present): a tor container that does not
        #     answer is never "not OnionHome" (a failed `docker exec` and a
        #     missing file both exit 1). The backup goes ahead without the
        #     registry and says so (the log, metadata
        #     onionhome_registry_unchecked, unchecked_notes).
        is_onionhome = False
        onionhome = onionhome_registry_present()
        if onionhome is None:
            log_func(ONIONHOME_REGISTRY_UNCHECKED_WARNING)
        elif onionhome:
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
        # A hub's: onionheaven_data "sealed" (with the recovery key's id) or
        # "unsealed" (stage_hub_backup).
        metadata.update(hub_fields)
        # A check that did not answer: what this backup may lack
        # (unchecked_notes says it with the finished backup).
        if hub is None:
            metadata['onionheaven_data_unchecked'] = True
        if onionhome is None:
            metadata['onionhome_registry_unchecked'] = True
        if not include_host_secrets:
            # Which secrets were set and are not in here (names only): a
            # restore says what to enter again (restore_backup_config).
            metadata['excludes_host_secrets'] = True
            metadata['secret_config_left_out'] = secrets_left_out
        if not include_onion_key:
            # No tor-keys/: a restore keeps the address it is restored onto
            # (staged_onion_key_address refuses a backup that says this and
            # holds a key all the same).
            metadata['excludes_onion_key'] = True
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


def staged_onion_key_address(staging, metadata):
    """The onion address of an extracted backup's own key (its tor-keys/:
    every key file must parse and agree, key_manager.staged_key_address),
    or '' for a backup without one.

    A backup without a key (the Mac Settings page's, metadata
    excludes_onion_key) restores the site onto the install's own address:
    no key is written and none is removed. Restore dialogs say so from the
    metadata flag, so a backup whose metadata.json says it holds no key but
    which holds one is refused (ValueError), before anything is restored.
    """
    address = key_manager.staged_key_address(_find_dir(staging, 'tor-keys'))
    if address and isinstance(metadata, dict) and metadata.get('excludes_onion_key') is True:
        raise ValueError(KEY_IN_KEYLESS_BACKUP_MSG)
    return address


def keep_installed_address(metadata, log_func):
    """For a restore of a backup without an onion service key: record in
    *metadata* the address this install keeps (served_onion_address), which
    the restore's config check (ADDRESS_PREFIX) holds the backup's
    preferences to. Raises ValueError (NO_KEY_TO_KEEP_MSG) when there is no
    address to keep: a restore never mints a new one in its place."""
    address = served_onion_address()
    if not key_manager.is_valid_onion_address(address):
        log_func("Restore: refused: the backup holds no onion service key and "
                 "this install's own address could not be read")
        raise ValueError(NO_KEY_TO_KEEP_MSG)
    log_func(f"Restore: the backup holds no onion service key (made from the "
             f"Settings page); this install keeps its key and its address "
             f"{address}")
    metadata['onion_address'] = address
    return address


def staged_backup_address(staging, log_func):
    """Validate an extracted backup and return the onion address its key
    belongs to ('' if it carries no key).

    For the bash launchers' `setup --from-backup` and Linux's
    restore_backup, which name a directory after the address and write its
    prefix into config: they take the address from here, never from
    metadata.json. A metadata address that is well formed but differs from
    the key's is logged, as the Python restore paths do (older clients left
    it stale). Raises ValueError on invalid metadata or key files; nothing
    is touched.
    """
    metadata = read_staged_metadata(staging)
    address = staged_onion_key_address(staging, metadata)
    metadata_address = metadata.get('onion_address', '')
    if address and metadata_address and metadata_address != address:
        log_func(f"Restore: KEY-MISMATCH — backup metadata.onion_address="
                 f"{metadata_address} != key_derives_to={address}; "
                 f"using DERIVED address for all persisted state.")
    return address


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
    directory, as hub_data_state counts them), or hub data sealed to a
    recovery key (onionheaven.sealed). A registry alone does not
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
    # Sealed to the hub's recovery key: never opened here, but a hub
    # restored without its registrants' keys looks restored all the same.
    if os.path.lexists(os.path.join(staging, hub_backup.SEALED_MEMBER)):
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

        match_config_to_restored_address(
            os.path.join(_data_dir, 'config'), onion_address,
            metadata.get('username', '').strip(), log_func)

    return onion_address


INSTALL_FROM_BACKUP_MARKER = ".install-from-backup"


def prepare_install_from_backup(zip_path, password, log_func, *, data_dir=None,
                                after_extract=None):
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

    *after_extract(staging, metadata)*, if given, runs once the backup is
    extracted and before anything else is written (cmd_restore opens a
    hub's sealed data there, into memory); if it raises, nothing is.

    A backup without an onion service key (the Mac Settings page's) seeds
    nothing: metadata's onion_address becomes the address the tor
    container serves (keep_installed_address), which the install keeps.
    With no address to keep (a new install, OnionPress not running) it is
    refused, before anything is written, rather than mint a new one.
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
        if after_extract is not None:
            after_extract(staging, metadata)
        if staged_onion_key_address(staging, metadata):
            seed_onion_key_for_install(staging, metadata, log_func, data_dir=_data_dir)
        else:
            # No key in it (the Mac Settings page's backup): the install
            # keeps its own, and nothing under vanity-keys/ changes.
            keep_installed_address(metadata, log_func)
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
                        through_wordpress=False,
                        before_key_change=None,
                        confirm_address_change=None):
    """Restore an OnionPress site from a backup zip.

    Args:
        zip_path: Path to the backup .zip
        password: Zip password
        log_func: Callable for progress logging
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.
        through_wordpress: True for the Settings page's restore, whose file
            and password came through WordPress. Refused before anything
            is touched on an OnionHeaven hub or OnionHome, or a node that
            could not be checked (settings_page_restore_node_refusal), for
            a backup holding an OnionHeaven hub's registrants' keys or
            OnionHome's name registry (settings_page_restore_refusal), and
            for a backup of another onion address than the one the tor
            container serves unless *confirm_address_change* says yes
            (ADDRESS_CHANGE_NOT_CONFIRMED_MSG). Neither hub volume is
            written.
        before_key_change: Called with the restored address (derived from
            the backup's key) before that key replaces the one in the tor
            container: the menubar asks OnionHeaven hubs to delete their
            copy of the current key there, while it can still sign.
        confirm_address_change: For the Settings page's restore: called
            with (current address, the backup's) when the backup's key is
            another address's, before anything is touched; anything but
            True refuses the restore. The menubar asks the person at the
            Mac in a native dialog. Not called for a backup of the address
            this install serves, nor for one without a key.

    A backup without an onion service key (the Mac Settings page's) keeps
    this install's key and address: nothing is written to the tor
    container, vanity-keys/ or the cached address (keep_installed_address).

    Returns:
        metadata dict from the backup

    Raises:
        ValueError on a wrong password; SettingsPageRestoreRefused.
    """
    restore_start = time.monotonic()
    # The Settings page's restore is never made on an OnionHeaven hub or on
    # OnionHome, nor on a node that could not say: refused before the
    # upload is opened. The address its key serves is also the one a
    # backup of another address would replace.
    current_address = ''
    if through_wordpress:
        current_address = served_onion_address()
        refusal = settings_page_restore_node_refusal(
            current_address, hub_data_state(), data_dir=data_dir)
        if refusal:
            log_func(f"Restore: refused (the Settings page's restore): {refusal}")
            raise SettingsPageRestoreRefused(refusal)
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

        # The backup's own key: the address it belongs to (its key files
        # checked), or '' for a backup without one, which keeps this
        # install's.
        backup_address = staged_onion_key_address(staging, metadata)

        # A backup of another address, through WordPress: only if the
        # person at the Mac says so, before anything is touched.
        if through_wordpress and backup_address and backup_address != current_address:
            confirmed = (confirm_address_change is not None
                         and confirm_address_change(current_address, backup_address) is True)
            if not confirmed:
                log_func(f"Restore: refused (the Settings page's restore): the "
                         f"backup is of {backup_address}, and replacing "
                         f"{current_address or 'this address'} was not "
                         f"confirmed at this Mac")
                raise SettingsPageRestoreRefused(ADDRESS_CHANGE_NOT_CONFIRMED_MSG)
            log_func(f"Restore: replacing {current_address or 'this address'} "
                     f"with {backup_address}, as confirmed at this Mac")

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

        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        if not backup_address:
            # No key in the backup: this install's key, key volume,
            # vanity-keys/ and cached address stay as they are.
            if current_address:
                log_func(f"Restore: the backup holds no onion service key (made "
                         f"from the Settings page); this install keeps its key "
                         f"and its address {current_address}")
                metadata['onion_address'] = current_address
            else:
                keep_installed_address(metadata, log_func)
        else:
            _restore_backup_key(tor_dir, metadata, log_func, data_dir,
                                _data_dir, before_key_change)

        # 2-6. Container-side artifacts + config overrides — shared with the
        # install-from-backup path (restore_container_artifacts / apply_config_overrides).
        restore_container_artifacts(staging, metadata, log_func,
                                    hub_volumes=not through_wordpress,
                                    data_dir=data_dir)
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


def _restore_backup_key(tor_dir, metadata, log_func, data_dir, _data_dir,
                        before_key_change):
    """restore_from_backup's step 1, for a backup with a key: write it into
    the tor container, remove the key volume for a clean restart, and sync
    vanity-keys/, the cached address and ADDRESS_PREFIX/ONIONNAME to the
    address derived from it (metadata's onion_address becomes that)."""
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
        if before_key_change is not None:
            before_key_change(derived_address)
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
        from .containers import forget_key_volume_migration
        forget_key_volume_migration(
            data_dir if data_dir is not None else _default_data_dir())

    # Sync vanity-keys directory on host so OnionHeaven detection and
    # prefix mismatch logic can see the restored onion address.
    onion_address = metadata.get('onion_address', '')
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
            match_config_to_restored_address(
                os.path.join(_data_dir, 'config'), onion_address,
                metadata.get('username', '').strip(), log_func)


def restore_container_artifacts(staging, metadata, log_func, *, hub_volumes=True,
                                name_registry=True, hub_data=None, data_dir=None):
    """Import a backup's container-side state into the RUNNING db + wordpress
    containers: WordPress DB, wp-content, OnionHeaven data, OnionHome registry,
    and multisite constants. Shared by restore_from_backup (in-place) and the
    install-from-backup path. Requires onionpress-db + onionpress-wordpress up.

    data_dir locates the host secrets file, the source of truth for the DB
    password (defaults to ~/.onionpress).

    hub_volumes=False (the Settings page's restore) never writes the
    OnionHeaven hub data or the OnionHome name registry: what the backup
    holds for them is left out and logged.

    name_registry=False leaves the OnionHome name registry to the caller,
    who restores it with restore_staged_name_registry once onionpress-tor
    runs: `onionpress restore` imports before Tor starts (the restored
    WordPress must be in place before it is reachable), and the registry
    is rebuilt inside onionpress-tor.

    *hub_data* is a hub's sealed data, opened with its recovery key
    (open_sealed_hub_data) by a caller that had the key; a backup whose hub
    data is sealed and no *hub_data* restores everything else and says what
    it left out. Hub data from either source is checked
    (hub_backup.check) before anything is written.
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
        # The source of truth for the password is the host secrets file, not
        # wp-config.php (WordPress's to write): the Settings page's restore is
        # reachable from WordPress, and a poisoned wp-config.php password would
        # otherwise ride into the ALTER USER below.
        db_password = _restore_db_password(data_dir, db_creds['password'])

        # The one sandboxed import (db_import below; the Linux launcher's
        # db_import runs it as `onionpress db-import`): the client's command
        # parsing OFF, the dump on stdin after the password line.
        db_import(sql_path, db_password,
                  user=db_creds['user'], name=db_creds['name'])

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
        # using (the host secrets file). Idempotent — if they already
        # match, ALTER USER is effectively a no-op.
        #
        # db_password is validated (no quote/backslash/backtick/control
        # char), so splicing it into the literal cannot break out; the
        # root client runs --sandbox --binary-mode as belt and braces.
        # The statement carries the password, so it goes in on stdin; the
        # root password comes from the db container's own environment.
        log_func("Restore: reconciling DB user password with current secrets...")
        alter_sql = (
            f"ALTER USER 'wordpress'@'%' "
            f"IDENTIFIED BY '{db_password}'; "
            f"FLUSH PRIVILEGES;"
        )
        alter = subprocess.run(
            ['docker', 'exec', '-i', 'onionpress-db', 'sh', '-c',
             'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
             'exec mariadb -uroot --binary-mode --sandbox'],
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

    # A hub's backup made from the Settings page by a build that left the
    # hub data out instead of refusing (between a878f59b and the refusal):
    # no registry, no keys.
    if metadata.get('excludes_onionheaven_data') is True:
        log_func("Restore: NOTE — this backup of an OnionHeaven hub was made "
                 "from the Settings page and holds neither the hub's registry "
                 "nor its registrants' keys; this restore leaves the hub data "
                 "as it is. Restore those from a backup made in the "
                 "OnionPress app or with onionpress backup.")

    # 4. Restore OnionHeaven data if present in backup: its registry and
    #    registrants' keys onto the OnionHeaven data volume, through the
    #    onionheaven container (restore_hub_data, write_hub_data): never
    #    into onionpress-wordpress, root-owned, the registry through SQLite,
    #    each key checked to be its address's first. Sealed hub data only
    #    when the caller opened it with the recovery key (*hub_data*).
    #    Not from the Settings page's restore (hub_volumes=False).
    onionheaven_dir = _find_dir(staging, 'onionheaven')
    has_hub_data = os.path.isdir(onionheaven_dir) and (
        os.path.isfile(os.path.join(onionheaven_dir, 'registry.db'))
        or os.path.isdir(os.path.join(onionheaven_dir, 'keys')))
    sealed = os.path.lexists(os.path.join(staging, hub_backup.SEALED_MEMBER))
    if (has_hub_data or sealed) and not hub_volumes:
        log_func("Restore: the backup's OnionHeaven data left out: the "
                 "Settings page's restore never writes the hub data")
    elif hub_data is not None:
        with _phase_timer(log_func, 'RESTORE', 'onionheaven_restore'):
            log_func("Restore: restoring the OnionHeaven registry and registrants' "
                     "keys (unsealed with the recovery key)...")
            write_hub_data(hub_backup.check(hub_data, log_func), log_func)
    elif has_hub_data:
        with _phase_timer(log_func, 'RESTORE', 'onionheaven_restore'):
            log_func("Restore: restoring the OnionHeaven registry and registrants' keys...")
            restore_hub_data(onionheaven_dir, log_func)
    elif sealed:
        log_func(sealed_skipped_message(metadata))

    # 4b. Restore OnionHome name registry if present in backup.
    #     Onto its own volume, through onionpress-tor (whose web-server.py
    #     is its only reader and writer): never into onionpress-wordpress,
    #     and root-owned, like the OnionHeaven data above.
    #     Only the registry's own files: an older backup also carries
    #     SQLite's -shm index (rebuilt on open), and nothing else in the
    #     archive belongs on that volume (its move marker among them).
    #     Not from the Settings page's restore (hub_volumes=False), which
    #     refuses such a backup first (settings_page_restore_refusal).
    #     The backup's file is never put there as it is: the tor image's
    #     own checks rebuild it (restore_name_registry). One they refuse
    #     leaves the registry in use as it was, and the rest of the restore
    #     goes on (a failed install-from-backup import is retried at every
    #     start, the database with it).
    onionhome_dir = _find_dir(staging, 'onionhome')
    has_name_registry = (os.path.isdir(onionhome_dir) and os.path.exists(
        os.path.join(onionhome_dir, 'onionnames.db')))
    if has_name_registry and not hub_volumes:
        log_func("Restore: the backup's OnionHome name registry left out: the "
                 "Settings page's restore never writes it")
    elif has_name_registry and not name_registry:
        log_func("Restore: OnionHome's name registry is restored once "
                 "onionpress-tor runs")
    elif has_name_registry:
        restore_staged_name_registry(staging, log_func)

    # 5. Re-add multisite constants to wp-config.php
    # WordPress Docker image generates a fresh wp-config.php without multisite
    # constants when the container is recreated. Without these, wp core
    # is-installed --network fails and ensure_multisite skips conversion.
    with _phase_timer(log_func, 'RESTORE', 'multisite_constants'):
        _ensure_multisite_constants(log_func)


# ─── Restoring OnionHome's name registry ────────────────────────────────
# A backup's onionnames.db is whoever made the backup's, as the old
# location's was WordPress's (a backup the Settings page hands WordPress for
# download can be swapped before the owner gets it). A crafted SQLite file is
# a way into what opens it, SQLite itself or code its schema names
# (triggers, views, virtual tables), and web-server.py opens the registry as
# root in onionpress-tor, beside the onion keys. So a restore never puts the
# backup's file on the volume. It gets what the move gives the old
# location's files, from the same code: the tor image's
# onionhome-registry-migrate.py, run in onionpress-tor on a private copy,
# reads the db and its -wal by descriptor, size-capped, under SQLite's
# defensive settings after quick_check, and builds a new registry with
# onionnames.py's schema holding only the rows that pass its checks. That
# one replaces the registry in use through SQLite's backup API, under its
# locks (web-server.py opens it per request): never a copy over the file in
# use, whose -wal SQLite would then apply to the restored one.
ONIONHOME_MIGRATE_SCRIPT = "/onionhome-registry-migrate.py"
ONIONHOME_MIGRATE_MARKER = ".migrated-from-persistent-data"

# The names `mktemp -d` gives in onionpress-tor (restore_name_registry).
_CONTAINER_TMP_RE = re.compile(r"/tmp/tmp\.[A-Za-z0-9]+")

# Run in onionpress-tor: argv[1] is the rebuilt registry, argv[2] the one in
# use. Prints the names each held (null before: no registry yet).
_ONIONHOME_PUT_IN_PLACE_PY = """\
import json, os, sqlite3, sys
new, live = sys.argv[1], sys.argv[2]
before = None
if os.path.isfile(live) and os.path.getsize(live) > 0:
    try:
        conn = sqlite3.connect(live, timeout=30)
        before = conn.execute("SELECT COUNT(*) FROM onionnames").fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
src = sqlite3.connect("file:" + new + "?mode=ro", uri=True)
dst = sqlite3.connect(live, timeout=30)
src.backup(dst)
after = dst.execute("SELECT COUNT(*) FROM onionnames").fetchone()[0]
dst.close()
src.close()
os.chmod(live, 0o600)
print(json.dumps({"before": before, "after": after}))
"""


class NameRegistryNotRestored(Exception):
    """A backup's OnionHome name registry was not restored (the message says
    why); the registry in use is as it was."""


def restore_staged_name_registry(staging, log_func):
    """Restore the OnionHome name registry an extracted backup (*staging*)
    holds, if any, through restore_name_registry. A refusal is logged as an
    ERROR, the registry in use left as it was; nothing is raised."""
    onionhome_dir = _find_dir(staging, 'onionhome')
    if not os.path.lexists(os.path.join(onionhome_dir, 'onionnames.db')):
        return
    with _phase_timer(log_func, 'RESTORE', 'onionhome_restore'):
        log_func("Restore: restoring OnionHome name registry (rebuilt by the "
                 "tor image's checks)...")
        try:
            restore_name_registry(onionhome_dir, log_func)
        except NameRegistryNotRestored as e:
            log_func(f"Restore: ERROR — OnionHome's name registry was not "
                     f"restored: {e}. The registry in use is unchanged.")


def _printable(text, limit=300):
    """Text from the container (or a file it read) for a log line."""
    text = ''.join(ch if ch.isprintable() else ' ' for ch in str(text)).strip()
    return text if len(text) <= limit else text[:limit] + '…'


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def restore_name_registry(src, log_func):
    """Put a backup's OnionHome name registry (*src*, its onionhome/
    folder) onto the registry's volume, through onionpress-tor, rebuilt by
    the tor image's own checks (see above). Logs how many names the
    registry held before and after. Raises NameRegistryNotRestored, the
    registry in use unchanged, when the image refuses what the backup holds
    (not a registry, damaged, too large or sparse) or the work in the
    container fails."""
    files = []
    for name in ONIONHOME_REGISTRY_FILES:
        try:
            if stat.S_ISREG(os.lstat(os.path.join(src, name)).st_mode):
                files.append(name)
        except FileNotFoundError:
            pass
    if ONIONHOME_REGISTRY_FILES[0] not in files:
        raise NameRegistryNotRestored("the backup holds no onionnames.db file")
    try:
        tmp = subprocess.run(['docker', 'exec', 'onionpress-tor', 'mktemp', '-d'],
                             capture_output=True, text=True, timeout=30,
                             check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise NameRegistryNotRestored(f"onionpress-tor did not answer ({type(e).__name__})") from e
    if not _CONTAINER_TMP_RE.fullmatch(tmp):
        raise NameRegistryNotRestored(
            f"unexpected temporary folder name from onionpress-tor: {_printable(tmp, 80)!r}")
    try:
        subprocess.run(['docker', 'exec', 'onionpress-tor', 'mkdir', '-p',
                        f'{tmp}/backup/onionhome', f'{tmp}/rebuilt'],
                       capture_output=True, timeout=30, check=True)
        for name in files:
            subprocess.run(['docker', 'cp', os.path.join(src, name),
                            f'onionpress-tor:{tmp}/backup/onionhome/{name}'],
                           capture_output=True, timeout=120, check=True)
        check = subprocess.run(
            ['docker', 'exec', 'onionpress-tor', 'python3', ONIONHOME_MIGRATE_SCRIPT,
             '--legacy-parent', f'{tmp}/backup', '--dest', f'{tmp}/rebuilt',
             '--no-mount-check'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=300)
        if check.returncode != 0:
            lines = [line for line in check.stderr.splitlines() if line.strip()]
            raise NameRegistryNotRestored(
                "the tor image's checks refused it: "
                + _printable(lines[-1] if lines else f"exit status {check.returncode}"))
        marker = subprocess.run(
            ['docker', 'exec', 'onionpress-tor', 'cat',
             f'{tmp}/rebuilt/{ONIONHOME_MIGRATE_MARKER}'],
            capture_output=True, text=True, timeout=30, check=True).stdout
        info = json.loads(marker)
        if not isinstance(info, dict) or info.get('moved') is not True:
            why = info.get('reason') if isinstance(info, dict) else None
            raise NameRegistryNotRestored(
                "what the backup holds is not a name registry"
                + (f" ({_printable(why, 120)})" if why else ""))
        placed = subprocess.run(
            ['docker', 'exec', 'onionpress-tor', 'python3', '-c', _ONIONHOME_PUT_IN_PLACE_PY,
             f'{tmp}/rebuilt/onionnames.db', f'{ONIONHOME_REGISTRY_DIR}/onionnames.db'],
            capture_output=True, text=True, timeout=120, check=True).stdout
        counts = json.loads(placed.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as e:
        raise NameRegistryNotRestored(
            f"the work in onionpress-tor failed ({type(e).__name__})") from e
    finally:
        subprocess.run(['docker', 'exec', 'onionpress-tor', 'rm', '-rf', tmp],
                       capture_output=True, timeout=30)
    before = _count(counts.get('before')) if isinstance(counts, dict) else None
    after = _count(counts.get('after')) if isinstance(counts, dict) else None
    left_out = _count(info.get('left_out')) or 0
    log_func(f"Restore: OnionHome name registry restored: {after} names, "
             f"{left_out} rows in the backup that fail onionnames.py's checks "
             f"left out; it held "
             + (f"{before} names before" if before is not None else "no registry before"))


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


def match_config_to_restored_address(config_path, onion_address, username, log_func):
    """Point an existing config's ADDRESS_PREFIX and ONIONNAME at a restored
    address and the backup's (validated) metadata username. One of the two
    restore paths' config writes (with restore_backup_config) outside
    config.py: the prefix may be 6 characters here, which `config set`
    refuses."""
    if not os.path.exists(config_path):
        return
    addr_base = onion_address.replace('.onion', '')
    with open(config_path, 'r', encoding='utf-8') as cf:
        lines = cf.readlines()
    found_prefix = found_onionname = False
    for i, line in enumerate(lines):
        if line.strip().startswith('ADDRESS_PREFIX='):
            old_prefix = line.strip().split('=', 1)[1]
            if not addr_base.startswith(old_prefix):
                plen = min(max(len(old_prefix), 3), 6)
                lines[i] = f'ADDRESS_PREFIX={addr_base[:plen]}\n'
                log_func(f"Restore: updated ADDRESS_PREFIX to {addr_base[:plen]}")
            found_prefix = True
        elif line.strip().startswith('ONIONNAME=') and username:
            lines[i] = f'ONIONNAME={username}\n'
            log_func(f"Restore: updated ONIONNAME to {username}")
            found_onionname = True
    if not found_prefix:
        lines.append(f'ADDRESS_PREFIX={addr_base[:3]}\n')
    if not found_onionname and username:
        lines.append(f'ONIONNAME={username}\n')
        log_func(f"Restore: set ONIONNAME to {username}")
    # The file exists: it keeps its mode (0600).
    with open(config_path, 'w', encoding='utf-8') as cf:
        cf.writelines(lines)


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
        result = wp_files.wp_cli('db', 'query', "SHOW TABLES LIKE 'wp_blogs';",
                                 timeout=15)
        if result.returncode != 0 or 'wp_blogs' not in result.stdout:
            return  # Not a multisite install, nothing to do
    except (subprocess.TimeoutExpired, Exception):
        return  # Can't check, skip rather than break single-site installs

    log_func("Restore: re-adding multisite constants to wp-config.php...")
    for name, value in _MULTISITE_CONSTANTS.items():
        wp_files.wp_cli('config', 'set', name, value, '--raw',
                        '--type=constant', timeout=15)


def _restore_db_password(data_dir, fallback):
    """The password to authenticate the restore's DB client and to re-assert
    for the wordpress user.

    The host's secrets file (``~/.onionpress/secrets``) is the source of
    truth — it is what the running containers were started with, and the
    bash launchers' restore reads the same WORDPRESS_DB_PASSWORD. wp-config.php
    is WordPress's to write, so a value read from it (``fallback``) is only a
    last resort when the secrets file is missing. Either way the value is
    validated before it reaches SQL text."""
    try:
        secrets_path = os.path.join(data_dir or _default_data_dir(), 'secrets')
        if os.path.exists(secrets_path):
            pw = load_secrets(secrets_path).wordpress_db_password
            if pw:
                return _reject_unsafe_db_value('WORDPRESS_DB_PASSWORD', pw)
    except Exception:
        pass
    return _reject_unsafe_db_value('DB_PASSWORD', fallback)


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


def linux_backup_metadata(onion_address, version, *, has_onionheaven=False,
                          excludes_image_derivatives=False, hub_fields=None,
                          hub_unchecked=False, with_host_secrets=True,
                          secrets_left_out=(), now=None):
    """metadata.json's content for a backup the Linux launcher stages
    (issue #406; it was an inline python3 -c in create_backup).

    *hub_fields* are what stage_hub_backup printed (onionheaven_data and
    its sealed-format and recovery-key fields). pack_backup stamps
    backup_format afterwards.
    """
    metadata = {
        'onion_address': onion_address,
        'backup_date': (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'onionpress_version': version,
        'platform': 'linux',
        'has_onionheaven': bool(has_onionheaven),
        # uploads/ holds originals only; resized copies are rebuilt on restore (#257)
        'excludes_image_derivatives': bool(excludes_image_derivatives),
    }
    for key in ('onionheaven_data', 'onionheaven_sealed_format',
                'onionheaven_recovery_key_id'):
        if hub_fields and key in hub_fields:
            metadata[key] = hub_fields[key]
    if hub_unchecked:
        # The onionheaven container did not say whether this is a hub: the
        # hub data may be missing (unchecked_notes).
        metadata['onionheaven_data_unchecked'] = True
    if not with_host_secrets:
        # Handed to WordPress: no host secrets, and the names of those that
        # were set (TUNNEL_TOKEN_KEY).
        metadata['excludes_host_secrets'] = True
        metadata['secret_config_left_out'] = list(secrets_left_out)
    return metadata
