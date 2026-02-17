#!/usr/bin/env python3
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
import zipfile
from datetime import datetime, timezone


def verify_wp_admin(username, password):
    """Verify that the given credentials belong to a WordPress administrator.

    Returns (True, None) on success, or (False, error_message) on failure.
    """
    # Verify user exists and has administrator role
    try:
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'user', 'get', username, '--field=roles', '--allow-root'],
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace').strip()
            if 'Invalid user' in stderr:
                return (False, f"User '{username}' does not exist in WordPress.")
            return (False, f"Could not look up user: {stderr}")

        roles = result.stdout.decode(errors='replace').strip()
        if 'administrator' not in roles:
            return (False, f"User '{username}' is not an administrator (roles: {roles}).")
    except subprocess.TimeoutExpired:
        return (False, "Timed out connecting to WordPress container.")
    except Exception as e:
        return (False, f"Error checking user role: {e}")

    # Verify password by piping it to wp_authenticate via stdin
    # Never pass the password as a command-line argument.
    php_code = (
        "$pw = file_get_contents('php://stdin');"
        "$pw = trim($pw);"
        "$u = wp_authenticate('" + username.replace("'", "\\'") + "', $pw);"
        "if (is_wp_error($u)) { fwrite(STDERR, $u->get_error_message()); exit(1); }"
        "echo 'ok';"
    )
    try:
        result = subprocess.run(
            ['docker', 'exec', '-i', 'onionpress-wordpress',
             'wp', 'eval', php_code, '--allow-root'],
            input=password.encode(),
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace').strip()
            return (False, f"Incorrect password for '{username}'.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out verifying password.")
    except Exception as e:
        return (False, f"Error verifying password: {e}")

    return (True, None)


def create_backup(onion_address, username, password, output_path, version, log_func):
    """Create a full OnionPress backup zip.

    Args:
        onion_address: Current .onion address
        username: WP admin username (stored in metadata)
        password: Zip encryption password
        output_path: Where to write the .zip file
        version: OnionPress version string
        log_func: Callable for progress logging
    """
    staging = tempfile.mkdtemp(prefix='onionpress-backup-')
    try:
        # 1. Extract Tor keys
        log_func("Backup: extracting Tor keys...")
        tor_dir = os.path.join(staging, 'tor-keys')
        os.makedirs(tor_dir)

        for keyfile in ('hs_ed25519_secret_key', 'hs_ed25519_public_key'):
            result = subprocess.run(
                ['docker', 'exec', 'onionpress-tor', 'cat',
                 f'/var/lib/tor/hidden_service/wordpress/{keyfile}'],
                capture_output=True, timeout=10
            )
            if result.returncode != 0:
                raise Exception(f"Could not read {keyfile} from Tor container")
            with open(os.path.join(tor_dir, keyfile), 'wb') as f:
                f.write(result.stdout)

        # 2. Dump WordPress database via mariadb-dump in the db container
        # (wp db export uses mysqldump which isn't in the WordPress container)
        log_func("Backup: exporting database...")
        db_dir = os.path.join(staging, 'database')
        os.makedirs(db_dir)

        db_creds = _get_db_credentials()
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

        # 3. Copy wp-content from container
        log_func("Backup: copying wp-content (themes, plugins, uploads)...")
        wpcontent_dir = os.path.join(staging, 'wp-content')
        subprocess.run(
            ['docker', 'cp',
             'onionpress-wordpress:/var/www/html/wp-content/.',
             wpcontent_dir],
            capture_output=True, timeout=300, check=True
        )

        # 4. Write metadata
        metadata = {
            'onion_address': onion_address,
            'backup_date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'onionpress_version': version,
            'username': username,
        }
        with open(os.path.join(staging, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # 5. Create password-protected zip using macOS system zip
        log_func("Backup: creating encrypted zip archive...")
        # Remove target if it already exists (zip would append otherwise)
        if os.path.exists(output_path):
            os.unlink(output_path)

        result = subprocess.run(
            ['zip', '-r', '-P', password, output_path, '.'],
            cwd=staging,
            capture_output=True, timeout=600
        )
        if result.returncode != 0:
            raise Exception(f"zip failed: {result.stderr.decode(errors='replace')}")

        log_func("Backup: complete")

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


def restore_from_backup(zip_path, password, log_func):
    """Restore an OnionPress site from a backup zip.

    Args:
        zip_path: Path to the backup .zip
        password: Zip password
        log_func: Callable for progress logging

    Returns:
        metadata dict from the backup
    """
    staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    try:
        # Extract zip
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

        # 1. Restore Tor keys
        log_func("Restore: writing Tor keys...")
        for keyfile in ('hs_ed25519_secret_key', 'hs_ed25519_public_key'):
            src = os.path.join(tor_dir, keyfile)
            if not os.path.exists(src):
                raise Exception(f"Backup is missing {keyfile}")

            dest = f'/var/lib/tor/hidden_service/wordpress/{keyfile}'
            subprocess.run(
                ['docker', 'cp', src, f'onionpress-tor:{dest}'],
                capture_output=True, timeout=10, check=True
            )
            subprocess.run(
                ['docker', 'exec', 'onionpress-tor', 'chmod', '600', dest],
                capture_output=True, timeout=10, check=True
            )

        # 2. Restore database via mariadb CLI in the db container
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
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"Database import failed: {result.stderr.decode(errors='replace')}")

        # Clean up SQL file in container
        subprocess.run(
            ['docker', 'exec', 'onionpress-db', 'rm', '-f', '/tmp/wordpress.sql'],
            capture_output=True, timeout=10
        )

        # 3. Restore wp-content
        if wpcontent_dir and os.path.isdir(wpcontent_dir):
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

        log_func("Restore: files restored successfully")
        return metadata

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _get_db_credentials():
    """Read WordPress database credentials from wp-config.php via WP-CLI."""
    creds = {}
    for field in ('DB_NAME', 'DB_USER', 'DB_PASSWORD'):
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'config', 'get', field, '--allow-root'],
            capture_output=True, timeout=15
        )
        if result.returncode != 0:
            raise Exception(f"Could not read {field} from WordPress config")
        creds[field] = result.stdout.decode(errors='replace').strip()
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
