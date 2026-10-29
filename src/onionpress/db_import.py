"""
The WordPress database's MariaDB clients in onionpress-db: the password on
stdin (never argv), credentials read from wp-config.php checked before use,
and the sandboxed import a restore runs a backup's dump through.

Split out of backup.py, which re-exports every name here and keeps the
restore that calls db_import (restore_container_artifacts).
"""

import os
import re
import subprocess
import tempfile

from onionpress import wp_files


# The MariaDB clients in onionpress-db take the password from MYSQL_PWD,
# which this reads from the first line of stdin. As `-p<password>` it would
# sit in the host's process list, where `ps` shows every user's argv.
_DB_PASSWORD_FROM_STDIN = 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec "$@"'

# A dump is restored, and the wordpress user's password re-set, with the
# client's own command parsing *off*, so nothing in a crafted dump or a
# poisoned wp-config.php can run anything but SQL in the db container:
#   --sandbox        refuses \!/system, \./source, tee and pager (a sourced
#                    dump runs them as OS root otherwise — a shell in the db
#                    container, reachable from WordPress via the Settings
#                    page's restore)
#   --binary-mode    treats the dump as data, never a `\x` client escape,
#                    and leaves binary blobs intact (mariadb-dump's own
#                    output still imports: its first line is the sandbox
#                    directive /*M!999999\- enable the sandbox mode */)
#   --local-infile=0 refuses LOAD DATA LOCAL INFILE, which would read the
#                    exec'd process's inherited MYSQL_ROOT_PASSWORD out of
#                    /proc/self/environ into a table
_DB_IMPORT_SAFE_FLAGS = ('--sandbox', '--binary-mode', '--local-infile=0')

# A value spliced into SQL text (the ALTER USER password re-set) or used as a
# client argv (DB_NAME/DB_USER as -D/positional, where a leading dash would be
# read as an option re-enabling local-infile) must carry none of this. The
# passwords ensure_secrets and both launchers generate are [A-Za-z0-9]{32};
# identifiers are always `wordpress`. This denylist still passes any sane
# hand-set value while refusing a quote, backslash, backtick or control char.
_DB_VALUE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f'\"\\`]")


def _reject_unsafe_db_value(label, value, *, as_argv=False):
    """Return *value* unchanged, or raise if it could escape a SQL string
    literal or a client argv. ``as_argv`` also refuses a leading dash."""
    if not isinstance(value, str) or value == '':
        raise Exception(f"{label} is empty or not a string")
    if _DB_VALUE_FORBIDDEN.search(value):
        raise Exception(
            f"{label} holds a quote, backslash, backtick or control "
            f"character not allowed in a database credential")
    if as_argv and value.startswith('-'):
        raise Exception(f"{label} may not begin with '-'")
    return value


def _assert_db_client_sandboxed():
    """Refuse the import outright on a MariaDB client too old for --sandbox,
    rather than fall back to an unsandboxed one."""
    check = subprocess.run(
        ['docker', 'exec', 'onionpress-db', 'sh', '-c',
         'mariadb --help 2>/dev/null | grep -q -- --sandbox'],
        capture_output=True, timeout=15,
    )
    if check.returncode != 0:
        raise Exception(
            "the onionpress-db MariaDB client is too old to import a backup "
            "safely (no --sandbox support); update the database image")


# The database dump gets this, plus the database's size at
# wp_files.COPY_BYTES_PER_S; the import this, plus the dump's size at the
# slower _DB_IMPORT_BYTES_PER_S (it rebuilds every index).
_DB_DUMP_FLOOR_S = 120
_DB_IMPORT_FLOOR_S = 120
_DB_IMPORT_BYTES_PER_S = 256 << 10


def db_import(sql_path, password, *, user='wordpress', name='wordpress'):
    """Import the dump at *sql_path* into database *name* of the running
    onionpress-db as *user*, with *password*. Raises on any failure.

    The one implementation (Phase 4, #120): restore_container_artifacts
    runs it, and the Linux launcher's db_import() is `onionpress db-import`
    (backup.py's caller of this, the password on stdin's first line).

    The client's own command parsing is OFF so a crafted dump cannot run
    \\! / system / \\. / tee / pager (a shell in the db container as OS
    root) or LOAD DATA LOCAL (read MYSQL_ROOT_PASSWORD out of
    /proc/self/environ): `--sandbox --binary-mode --local-infile=0`, and a
    client too old for --sandbox is refused rather than fallen back from
    (_assert_db_client_sandboxed). The dump goes in on stdin after the
    password line (read into MYSQL_PWD, never argv: the process list shows
    every user's command lines) — never docker cp'd to a fixed /tmp path
    and sourced. The DB dump is the database only (media rides
    wp-content), so its bytes fit in memory the way create_backup once held
    them. The timeout is sized from the dump (issue #317: a fixed limit
    failed large sites); importing rebuilds every index.
    """
    if not password or '\n' in password or '\r' in password:
        # The password is stdin's first line: empty or with a line break it
        # would not reach the client whole, and the rest would be read as
        # SQL.
        raise Exception("Database import refused: the database password is "
                        "empty or holds a line break")
    _assert_db_client_sandboxed()
    import_argv = _db_client_argv(
        'mariadb', *_DB_IMPORT_SAFE_FLAGS, '-u', user, name)
    sql_bytes = os.path.getsize(sql_path)
    with open(sql_path, 'rb') as f:
        dump_bytes = f.read()
    result = subprocess.run(
        import_argv,
        # First stdin line is the password (read into MYSQL_PWD); the
        # dump bytes follow for the client to read as SQL.
        input=password.encode() + b'\n' + dump_bytes,
        capture_output=True,
        timeout=wp_files.sized_timeout(sql_bytes, _DB_IMPORT_FLOOR_S,
                                       _DB_IMPORT_BYTES_PER_S),
    )
    if result.returncode != 0:
        raise Exception("Database import failed: "
                        f"{result.stderr.decode('utf-8', 'replace').strip()}")


def _run_db_client_to_file(argv, password, out_path, timeout, what):
    """Run the MariaDB client *argv* (_db_client_argv) with its stdout
    written to *out_path*; raise if it fails or takes more than *timeout*
    seconds, in which case it is killed. *what* names it in the error."""
    with open(out_path, 'wb') as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out,
                                stderr=err)
        try:
            try:
                proc.stdin.write((password + '\n').encode())
                proc.stdin.close()
            except BrokenPipeError:
                pass  # it exited already; its status says why
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if rc is None:
            raise Exception(f"{what} did not finish within {timeout} s")
        if rc != 0:
            err.seek(0)
            raise Exception(f"{what} failed: "
                            f"{err.read().decode(errors='replace').strip()[-500:]}")


def _db_client_argv(*client_argv):
    """argv that runs a MariaDB client in onionpress-db. Feed the password
    as the first line of stdin: ``input=password + '\\n'``."""
    return ['docker', 'exec', '-i', 'onionpress-db',
            'sh', '-c', _DB_PASSWORD_FROM_STDIN, 'sh', *client_argv]


def _get_db_credentials():
    """Read WordPress database credentials from wp-config.php via WP-CLI.

    wp-config.php is WordPress's to write, so the values are validated before
    any caller uses them as a client argv or splices them into SQL: a
    DB_NAME/DB_USER starting with '-' would be read as a client option
    (e.g. re-enabling --local-infile), and a quote or backslash in any field
    would break out of a string literal.
    """
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
        'name': _reject_unsafe_db_value('DB_NAME', creds['DB_NAME'], as_argv=True),
        'user': _reject_unsafe_db_value('DB_USER', creds['DB_USER'], as_argv=True),
        'password': _reject_unsafe_db_value('DB_PASSWORD', creds['DB_PASSWORD']),
    }
