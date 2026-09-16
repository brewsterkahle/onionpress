"""
Backup and Restore for OnionPress

Creates password-protected zip archives containing Tor keys, WordPress database,
and wp-content (themes, plugins, uploads).
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

from onionpress import key_manager
from onionpress.config import DEFAULTS, read_config, read_value, write_value


def _default_data_dir() -> str:
    """Resolve the default OnionPress data directory at call time.

    Deliberately not cached: callers (and tests) may mutate $HOME between
    imports and the first use of backup functions.
    """
    return os.path.expanduser("~/.onionpress")


def _default_documents_dir() -> str:
    """Resolve the default ~/OnionPress/ directory at call time (see
    _default_data_dir — same not-cached rationale)."""
    from onionpress.platform import default_documents_dir
    return default_documents_dir()


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
        ['docker', 'exec', 'onionpress-db',
         'mariadb', '-B', '-N',
         '-u', db_creds['user'],
         '-p' + db_creds['password'],
         '-D', db_creds['name'],
         '-e', sql],
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
        onionheaven_bytes = _du_bytes(
            'onionpress-wordpress', '/var/lib/onionpress/onionheaven',
        )
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
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'user', 'get', username, '--field=roles', '--allow-root'],
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
            ['docker', 'exec', '-i', 'onionpress-wordpress',
             'wp', 'eval', php_code, '--allow-root'],
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

    Mirrors the bash launcher's pre-flight check in create_backup
    (linux/onionpress) so password-only UI flows (e.g. the Linux tray's
    backup dialog) can give inline feedback without asking the user to
    remember their admin username. Uses wp_check_password directly so
    it bypasses login rate-limiting and session cookies.
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
            ['docker', 'exec', '-i', 'onionpress-wordpress',
             'wp', 'eval', php_code, '--allow-root'],
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
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'user', 'list', '--role=administrator',
             '--field=user_login', '--allow-root'],
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


def _extract_tor_keys_to_staging(staging, onion_address, log_func):
    """Extract Tor keys into staging/tor-keys/; return the key-derived address.

    Shared by both WordPress and static backups — Tor identity is
    content-agnostic. See create_backup's docstring for why the derived
    address (not the caller-supplied one) is authoritative.
    """
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
    return derived_address


def create_backup(onion_address, username, password, output_path, version, log_func, *,
                   data_dir=None, site_type="wordpress", documents_dir=None):
    """Create a full OnionPress backup zip.

    Args:
        onion_address: Current .onion address
        username: WP admin username (stored in metadata). Ignored for
            static-site installs.
        password: Zip encryption password
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.
        output_path: Where to write the .zip file
        version: OnionPress version string
        log_func: Callable for progress logging
        site_type: "wordpress" (default) or "static". Static installs skip
            the DB dump/wp-content tar entirely and instead tar
            ~/OnionPress/Site/ directly off the host filesystem.
        documents_dir: ~/OnionPress/ directory (static mode only). Tests
            pass a sandbox path.
    """
    backup_start = time.monotonic()
    staging = tempfile.mkdtemp(prefix='onionpress-backup-')
    try:
        # 1. Extract Tor keys (Arti OpenSSH keystore format) — shared by
        # both site types, content-agnostic.
        with _phase_timer(log_func, 'BACKUP', 'tor_keys'):
            onion_address = _extract_tor_keys_to_staging(staging, onion_address, log_func)

        if site_type == "static":
            is_onionheaven = False
            is_onionhome = False
            with _phase_timer(log_func, 'BACKUP', 'site_copy'):
                log_func("Backup: copying static site content...")
                _docs_dir = documents_dir if documents_dir is not None else _default_documents_dir()
                site_src = os.path.join(_docs_dir, 'Site')
                site_dst = os.path.join(staging, 'site')
                if os.path.isdir(site_src):
                    shutil.copytree(site_src, site_dst)
                    site_bytes = _dir_size_bytes(site_dst)
                else:
                    os.makedirs(site_dst, exist_ok=True)
                    site_bytes = 0
                log_func(f"BACKUP_STATS: site_bytes={site_bytes}")
        else:
            # 0. Workload stats — log once up front (issue #214). Read DB
            #    creds here so we can reuse them for the dump phase below
            #    without paying for two round trips through wp-cli.
            db_creds = _get_db_credentials()
            _log_workload_stats(log_func, 'BACKUP_STATS', db_creds)

            # 2. Dump WordPress database via mariadb-dump in the db container
            # (wp db export uses mysqldump which isn't in the WordPress container)
            with _phase_timer(log_func, 'BACKUP', 'db_dump'):
                log_func("Backup: exporting database...")
                db_dir = os.path.join(staging, 'database')
                os.makedirs(db_dir)

                result = subprocess.run(
                    ['docker', 'exec', 'onionpress-db',
                     'mariadb-dump',
                     '-u', db_creds['user'],
                     '-p' + db_creds['password'],
                     db_creds['name']],
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
            #    Stream via `docker exec tar -cf - | tar -xf -` instead of
            #    docker cp so the excludes apply at read time (no wasted IO
            #    pulling Creations across the VM boundary).
            with _phase_timer(log_func, 'BACKUP', 'wpcontent_tar'):
                log_func("Backup: copying wp-content (excluding Creations, thumbs, .DS_Store)...")
                wpcontent_dir = os.path.join(staging, 'wp-content')
                os.makedirs(wpcontent_dir, exist_ok=True)
                docker_tar = subprocess.Popen(
                    ['docker', 'exec', 'onionpress-wordpress',
                     'tar', '-cf', '-',
                     '--exclude=./creations',
                     '--exclude=.thumbs',
                     '--exclude=.DS_Store',
                     '-C', '/var/www/html/wp-content', '.'],
                    stdout=subprocess.PIPE
                )
                host_tar = subprocess.run(
                    ['tar', '-xf', '-', '-C', wpcontent_dir],
                    stdin=docker_tar.stdout, timeout=300
                )
                docker_tar.stdout.close()
                docker_rc = docker_tar.wait(timeout=10)
                if docker_rc != 0 or host_tar.returncode != 0:
                    raise Exception(f"wp-content tar failed: docker={docker_rc} host={host_tar.returncode}")

            # 4. Backup OnionHeaven data if this is OnionHeaven instance
            #    (encrypted keys, master-key.json, registry — NOT the ephemeral unlock file)
            is_onionheaven = False
            onionheaven_check = subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'test', '-f', '/var/lib/onionpress/onionheaven/master-key.json'],
                capture_output=True, timeout=10
            )
            if onionheaven_check.returncode == 0:
                with _phase_timer(log_func, 'BACKUP', 'onionheaven_copy'):
                    log_func("Backup: copying OnionHeaven data (encrypted keys, registry)...")
                    is_onionheaven = True
                    onionheaven_dir = os.path.join(staging, 'onionheaven')
                    subprocess.run(
                        ['docker', 'cp',
                         'onionpress-wordpress:/var/lib/onionpress/onionheaven/.',
                         onionheaven_dir],
                        capture_output=True, timeout=60, check=True
                    )
                    # Remove the ephemeral unlock file if it was copied
                    unlocked_file = os.path.join(onionheaven_dir, '.master-key-unlocked')
                    if os.path.exists(unlocked_file):
                        os.unlink(unlocked_file)

            # 4b. Backup OnionHome name registry if this is an OnionHome instance.
            #     The DB is tiny (KB) but losing it strands every inbound
            #     /api/name/lookup/NAME that ever pointed here — it's the
            #     canonical record of who's who in the onion directory.
            is_onionhome = False
            onionhome_check = subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'test', '-f', '/var/lib/onionpress/onionhome/onionnames.db'],
                capture_output=True, timeout=10
            )
            if onionhome_check.returncode == 0:
                with _phase_timer(log_func, 'BACKUP', 'onionhome_copy'):
                    log_func("Backup: copying OnionHome name registry...")
                    is_onionhome = True
                    onionhome_dir = os.path.join(staging, 'onionhome')
                    subprocess.run(
                        ['docker', 'cp',
                         'onionpress-wordpress:/var/lib/onionpress/onionhome/.',
                         onionhome_dir],
                        capture_output=True, timeout=60, check=True
                    )

        # 5. Save non-default config values
        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        config_path = os.path.join(_data_dir, 'config')
        if os.path.exists(config_path):
            current = read_config(config_path)
            overrides = {k: v for k, v in current.items()
                         if k in DEFAULTS and v != DEFAULTS[k]}
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
            'is_static': site_type == "static",
            'is_onionheaven': is_onionheaven,
            'is_onionhome': is_onionhome,
            # Creations aren't inside wp-content anymore; they're in the
            # user's ~/OnionPress/Creations directory. Flag this
            # so restore can surface a useful "also copy your Documents"
            # reminder instead of silently producing an empty Creations page.
            # Not applicable to static installs (no wp-content at all).
            'excludes_creations': site_type != "static",
        }
        with open(os.path.join(staging, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # 8. Create password-protected zip using macOS system zip
        with _phase_timer(log_func, 'BACKUP', 'zip_encrypt'):
            log_func("Backup: creating encrypted zip archive...")
            # Remove target if it already exists (zip would append otherwise)
            if os.path.exists(output_path):
                os.unlink(output_path)

            # -n skips deflate for already-compressed formats (images, video,
            # audio, PDFs, archives). Encryption + CRC still run, but we don't
            # waste CPU running deflate on bytes that won't shrink.
            result = subprocess.run(
                ['zip', '-r', '-P', password,
                 '-n', '.mov:.mp4:.m4v:.webm:.jpg:.jpeg:.png:.gif:.heic:.heif'
                       ':.pdf:.mp3:.m4a:.aac:.ogg:.webp:.zip:.gz',
                 output_path, '.'],
                cwd=staging,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600
            )
            if result.returncode != 0:
                raise Exception(f"zip failed: {result.stderr}")

        log_func(f"BACKUP_PHASE: name=total elapsed_s={time.monotonic() - backup_start:.2f}")
        zip_bytes = os.path.getsize(output_path)
        log_func(f"Backup: complete ({zip_bytes / 1_048_576:.1f} MB)")

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def read_backup_metadata(zip_path, password):
    """Read metadata.json from a backup zip.

    Returns the metadata dict.
    Raises on bad password, missing metadata, or invalid zip.
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Try to find metadata.json (may be at root or ./metadata.json)
            metadata_name = None
            for name in zf.namelist():
                if name == 'metadata.json' or name == './metadata.json':
                    metadata_name = name
                    break
            if metadata_name is None:
                raise ValueError("Not a valid OnionPress backup (no metadata.json found)")

            data = zf.read(metadata_name, pwd=password.encode())
            return json.loads(data)
    except RuntimeError as e:
        if 'password' in str(e).lower() or 'Bad password' in str(e):
            raise ValueError("Incorrect password for this backup.")
        raise
    except zipfile.BadZipFile:
        raise ValueError("Not a valid zip file.")


def _unzip_cli_extract(zip_path, password, dest, log_func):
    """Fast-path extraction via the system `unzip` CLI — C code, ~30-50x faster
    than zipfile's pure-Python ZipCrypto decryption (which runs ~2 MB/s on a
    ~900 MB encrypted backup). Returns True on success, False if `unzip` is
    absent or errors (caller falls back to zipfile).

    Injection-safe: arguments are passed as a LIST to subprocess.run with NO
    shell=True, so the password and paths are literal argv values that a shell
    never parses — a password containing ; $() `` | spaces etc. cannot inject
    commands. `-P` always consumes the next argv as the password value, so a
    password starting with '-' is taken literally too. zip_path is passed as a
    plain argv (callers use absolute paths, so it won't be mistaken for a flag).
    """
    import shutil as _shutil
    if _shutil.which('unzip') is None:
        return False
    try:
        result = subprocess.run(
            ['unzip', '-o', '-qq', '-P', password, zip_path, '-d', dest],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log_func(f"Restore: unzip CLI unavailable/timed out ({e}); using zipfile")
        return False
    if result.returncode != 0:
        # Wrong password, AES archive, or corrupt zip — let zipfile try (and
        # raise a clean error for the wrong-password case).
        log_func(f"Restore: unzip CLI rc={result.returncode}; using zipfile")
        return False
    return True


def extract_backup(zip_path, password, log_func, staging=None):
    """Extract a backup zip into a staging dir; return (staging, metadata).

    If *staging* is None a fresh temp dir is created (caller cleans it up).
    Pass an explicit *staging* (e.g. a persistent ~/.onionpress/restore-staging)
    when the extracted files must survive a container restart, as in
    install-from-backup. Validates the password as a side effect — a wrong
    password raises here (zipfile error) before any state is touched.

    Uses the system `unzip` CLI when available (much faster for password-
    protected archives), falling back to pure-Python zipfile otherwise.
    """
    if staging is None:
        staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    with _phase_timer(log_func, 'RESTORE', 'zip_extract'):
        log_func("Restore: extracting backup archive...")
        if not _unzip_cli_extract(zip_path, password, staging, log_func):
            # Fallback: pure-Python zipfile — always available, but slow for
            # ZipCrypto. Also the path that surfaces a wrong-password error.
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(staging, pwd=password.encode())
    metadata_path = os.path.join(staging, 'metadata.json')
    if not os.path.exists(metadata_path):
        metadata_path = os.path.join(staging, '.', 'metadata.json')
    with open(metadata_path) as f:
        metadata = json.load(f)
    return staging, metadata


def peek_backup_metadata(zip_path, password):
    """Validate the password and return a backup's metadata WITHOUT a full
    extract. Used by the welcome-screen restore flow to confirm the password up
    front (and show the address/username being restored) before committing.
    Raises on a wrong password or an invalid backup.
    """
    with tempfile.TemporaryDirectory(prefix='onionpress-peek-') as tmp:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            names = [n for n in zf.namelist()
                     if n.rstrip('/').endswith('metadata.json')]
            if not names:
                raise Exception("Not an OnionPress backup (no metadata.json)")
            zf.extract(names[0], tmp, pwd=password.encode())
            with open(os.path.join(tmp, names[0])) as f:
                return json.load(f)


def seed_onion_key_for_install(staging, metadata, log_func, *, data_dir=None):
    """Seed the backup's onion identity into host-side state so a fresh install
    adopts it WITHOUT generating a vanity key. Writes the key + hostname into
    ~/.onionpress/shared/vanity-keys/<addr>/ (the launcher's pre-imported-key
    path), updates the cached onion_address, and points ADDRESS_PREFIX/ONIONNAME
    at the restored identity. Returns the derived .onion address.

    Host-side only: does NOT touch a running container or the arti-state volume.
    On the next launcher start the key is copied into arti-state and (for C Tor)
    converted to the C-Tor keystore by the tor entrypoint.
    """
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    tor_dir = _find_dir(staging, 'tor-keys')
    key_path = os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private')
    if not os.path.exists(key_path):
        raise Exception("Backup is missing ks_hs_id.ed25519_expanded_private")

    with open(key_path, 'rb') as f:
        pem_data = f.read()
    priv, pub = key_manager.parse_openssh_key(pem_data)

    # Address is fully determined by the key; trust the derived value over
    # metadata.onion_address (older clients sometimes left it stale).
    onion_address = key_manager.derive_onion_address(pub)
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
    _, metadata = extract_backup(zip_path, password, log_func, staging=staging)

    # Persist the backup's site type BEFORE anything reads config again.
    # Everything downstream — containers.py's _build_env/start_core (which
    # picks COMPOSE_PROFILES and the wordpress+db vs. site service set),
    # the launchers' env exports, and the tor container's onion-service
    # nickname — keys off SITE_TYPE in config, while cmd_restore derives
    # the keystore nickname from this metadata. Without this write, a
    # static backup restored onto a fresh (default-wordpress) install
    # seeds the key into hss/site but then boots the WordPress topology
    # with nickname "wordpress" — a brand-new onion address instead of
    # the restored identity. Restore is a full teardown/rebuild, so
    # adopting the backup's type is the correct semantic in both
    # directions; old backups without is_static restore as wordpress.
    restored_site_type = 'static' if metadata.get('is_static') else 'wordpress'
    config_path = os.path.join(_data_dir, 'config')
    write_value(config_path, 'SITE_TYPE', restored_site_type)
    log_func(f"Restore: SITE_TYPE set to {restored_site_type} (from backup metadata)")

    seed_onion_key_for_install(staging, metadata, log_func, data_dir=_data_dir)
    apply_config_overrides(staging, log_func, data_dir=_data_dir)
    marker = os.path.join(_data_dir, INSTALL_FROM_BACKUP_MARKER)
    with open(marker, 'w') as f:
        f.write(staging + '\n')
    log_func(f"Install-from-backup: staged at {staging} (marker written)")
    return staging, metadata


def restore_from_backup(zip_path, password, log_func, *, data_dir=None):
    """Restore an OnionPress site from a backup zip.

    Args:
        zip_path: Path to the backup .zip
        password: Zip password
        log_func: Callable for progress logging
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.

    Returns:
        metadata dict from the backup
    """
    restore_start = time.monotonic()
    staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    try:
        # Extract zip
        with _phase_timer(log_func, 'RESTORE', 'zip_extract'):
            log_func("Restore: extracting backup archive...")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(staging, pwd=password.encode())

        # Normalize paths -- zip may have ./ prefix
        metadata_path = os.path.join(staging, 'metadata.json')
        if not os.path.exists(metadata_path):
            metadata_path = os.path.join(staging, '.', 'metadata.json')
        with open(metadata_path) as f:
            metadata = json.load(f)

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

        # 1. Restore Tor keys (Arti OpenSSH keystore format)
        with _phase_timer(log_func, 'RESTORE', 'tor_keys'):
            log_func("Restore: writing Tor keys...")
            key_path = os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private')
            if not os.path.exists(key_path):
                raise Exception("Backup is missing ks_hs_id.ed25519_expanded_private")

            with open(key_path, 'rb') as f:
                pem_data = f.read()
            priv, pub = key_manager.parse_openssh_key(pem_data)
            key_manager.write_private_key(priv, pub)

            # Address is fully determined by the key — derive it here and
            # use the derived value for every address-keyed write that
            # follows (cache file, vanity-keys directory name, hostname
            # file, ADDRESS_PREFIX). Trusting metadata.onion_address instead
            # propagated corruption when older clients produced backups
            # with metadata.onion_address pointing at the previous address
            # while tor-keys/ already held the new key. The metadata value
            # is recorded only as a diagnostic check.
            derived_address = key_manager.derive_onion_address(pub)
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

            # Remove arti-state volume so it gets recreated from vanity-keys
            # on next launch. This avoids stale key mismatches.
            log_func("Restore: removing arti-state volume for clean restart...")
            subprocess.run(
                ['docker', 'volume', 'rm', 'onionpress-arti-state'],
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
        restore_container_artifacts(staging, metadata, log_func)
        apply_config_overrides(staging, log_func, data_dir=data_dir)

        log_func(f"RESTORE_PHASE: name=total elapsed_s={time.monotonic() - restore_start:.2f}")
        log_func("Restore: files restored successfully")
        return metadata

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def restore_container_artifacts(staging, metadata, log_func, *, documents_dir=None):
    """Import a backup's artifacts into the current install: WordPress DB,
    wp-content, OnionHeaven data, OnionHome registry, and multisite
    constants — or, for a static-site backup (metadata['is_static']),
    the published site content. Shared by restore_from_backup (in-place)
    and the install-from-backup path. WordPress mode requires
    onionpress-db + onionpress-wordpress up; static mode is a pure host
    filesystem operation (no containers needed).
    """
    if metadata.get('is_static'):
        with _phase_timer(log_func, 'RESTORE', 'site_copy'):
            log_func("Restore: restoring static site content...")
            site_src = _find_dir(staging, 'site')
            _docs_dir = documents_dir if documents_dir is not None else _default_documents_dir()
            site_dst = os.path.join(_docs_dir, 'Site')
            if os.path.isdir(site_src):
                if os.path.isdir(site_dst):
                    shutil.rmtree(site_dst)
                shutil.copytree(site_src, site_dst)
                log_func(f"Restore: site content restored to {site_dst}")
            else:
                log_func("Restore: WARNING — backup has no site/ directory")
        return

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
            ['docker', 'exec', 'onionpress-db',
             'mariadb',
             '-u', db_creds['user'],
             '-p' + db_creds['password'],
             db_creds['name'],
             '-e', 'source /tmp/wordpress.sql'],
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
        log_func("Restore: reconciling DB user password with current secrets...")
        alter_sql = (
            f"ALTER USER 'wordpress'@'%' "
            f"IDENTIFIED BY '{db_creds['password']}'; "
            f"FLUSH PRIVILEGES;"
        )
        alter = subprocess.run(
            ['docker', 'exec', 'onionpress-db', 'sh', '-c',
             'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -e "' + alter_sql + '"'],
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
            subprocess.run(
                ['docker', 'cp',
                 wpcontent_dir + '/.',
                 'onionpress-wordpress:/var/www/html/wp-content/'],
                capture_output=True, timeout=300, check=True
            )
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'chown', '-R', 'www-data:www-data', '/var/www/html/wp-content/'],
                capture_output=True, timeout=60
            )

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

    # 4. Restore OnionHeaven data if present in backup
    onionheaven_dir = _find_dir(staging, 'onionheaven')
    if os.path.isdir(onionheaven_dir) and os.path.exists(os.path.join(onionheaven_dir, 'master-key.json')):
        with _phase_timer(log_func, 'RESTORE', 'onionheaven_restore'):
            log_func("Restore: restoring OnionHeaven data (encrypted keys, registry)...")
            # Remove ephemeral unlock file if it somehow exists in the backup
            unlocked_file = os.path.join(onionheaven_dir, '.master-key-unlocked')
            if os.path.exists(unlocked_file):
                os.unlink(unlocked_file)
            # Ensure OnionHeaven directory exists in container
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'mkdir', '-p', '/var/lib/onionpress/onionheaven'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['docker', 'cp',
                 onionheaven_dir + '/.',
                 'onionpress-wordpress:/var/lib/onionpress/onionheaven/'],
                capture_output=True, timeout=60, check=True
            )
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'chown', '-R', 'www-data:www-data', '/var/lib/onionpress/onionheaven/'],
                capture_output=True, timeout=30
            )
            log_func("Restore: OnionHeaven data restored (OnionHeaven will be locked until admin login)")

    # 4b. Restore OnionHome name registry if present in backup.
    #     Kept root-owned — the tor container reads/writes these files
    #     as root, same as OnionHeaven's tor-container-root files
    #     alongside the www-data restore target above.
    onionhome_dir = _find_dir(staging, 'onionhome')
    if os.path.isdir(onionhome_dir) and os.path.exists(os.path.join(onionhome_dir, 'onionnames.db')):
        with _phase_timer(log_func, 'RESTORE', 'onionhome_restore'):
            log_func("Restore: restoring OnionHome name registry...")
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'mkdir', '-p', '/var/lib/onionpress/onionhome'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['docker', 'cp',
                 onionhome_dir + '/.',
                 'onionpress-wordpress:/var/lib/onionpress/onionhome/'],
                capture_output=True, timeout=60, check=True
            )
            log_func("Restore: OnionHome name registry restored")

    # 5. Re-add multisite constants to wp-config.php
    # WordPress Docker image generates a fresh wp-config.php without multisite
    # constants when the container is recreated. Without these, wp core
    # is-installed --network fails and ensure_multisite skips conversion.
    with _phase_timer(log_func, 'RESTORE', 'multisite_constants'):
        _ensure_multisite_constants(log_func)


def apply_config_overrides(staging, log_func, *, data_dir=None):
    """Apply a backup's non-default config overrides to ~/.onionpress/config."""
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    # 6. Restore config overrides (non-default user preferences)
    overrides_path = os.path.join(staging, 'config-overrides.json')
    if not os.path.exists(overrides_path):
        overrides_path = os.path.join(staging, '.', 'config-overrides.json')
    if os.path.exists(overrides_path):
        with _phase_timer(log_func, 'RESTORE', 'config_overrides'):
            with open(overrides_path, 'r') as f:
                overrides = json.load(f)
            config_path = os.path.join(_data_dir, 'config')
            for key, value in overrides.items():
                write_value(config_path, key, value)
            log_func(f"Restore: applied {len(overrides)} config override(s)")



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
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'db', 'query', "SHOW TABLES LIKE 'wp_blogs';", '--allow-root'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0 or 'wp_blogs' not in result.stdout:
            return  # Not a multisite install, nothing to do
    except (subprocess.TimeoutExpired, Exception):
        return  # Can't check, skip rather than break single-site installs

    log_func("Restore: re-adding multisite constants to wp-config.php...")
    for name, value in _MULTISITE_CONSTANTS.items():
        subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'config', 'set', name, value,
             '--raw', '--type=constant', '--allow-root'],
            capture_output=True, timeout=15
        )


def _get_db_credentials():
    """Read WordPress database credentials from wp-config.php via WP-CLI."""
    creds = {}
    for field in ('DB_NAME', 'DB_USER', 'DB_PASSWORD'):
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'config', 'get', field, '--allow-root'],
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
