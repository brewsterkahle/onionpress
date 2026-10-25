"""What the host hands to the WordPress container, written as www-data.

Anything that runs in WordPress runs as www-data, and www-data owns the
folders the host writes into: /var/lib/onionpress (the onionpress-data
volume: status, results, the onion address) and the web root (.htaccess,
wp-content and the theme, plugins and mu-plugins in it). It can put a
symbolic link at any name there. Root in the container follows such a link:
`docker cp` to a link writes the file it points to, so do `tee` and
`cat >`, and `chown` hands its target to www-data. A planted
`backup-result.json -> /usr/local/bin/docker-entrypoint.sh` had root write
the script the container runs at its next start, with text an error message
could carry; a planted `wp-content/sunrise.php` link had the chown that
followed give www-data that script.

So every file and folder goes in as www-data (`docker exec -u www-data`),
which can reach nothing it could not write anyway:

- put_file: a temporary file beside the destination, renamed over it. The
  rename replaces a link instead of following it, and replaces a file root
  wrote in an older version (www-data could not open that one for writing).
- put_tree: a tar stream of plain files and folders, unpacked by www-data.
- own: root's one remaining job, giving www-data what an older version left
  root's at a destination (mu-plugins/ was made by root's mkdir). It acts
  only on the canonical path: `cd -P` into the folder, check `pwd -P`, then
  chown the bare name, so no link in the path is followed; chown -h never
  follows a link at the name itself, and the recursive form (find
  -execdir) follows none below it and never enters My Creations.

The bash launchers' wp_put carries PUT_FILE_SCRIPT inline;
tests/test_wordpress_writes_as_www_data.py keeps the three copies equal.

wp-cli runs as www-data too (wp_cli_args). It loads WordPress, that is
wp-config.php, the mu-plugins, plugins and themes, every one of them a
file www-data can write: run as root (`--allow-root`), any code execution
in WordPress became root in the container at the next wp-cli call, and
root there reaches the shared volumes and, under rootless Docker, the
host user's files. Without --allow-root, wp-cli refuses to run as root,
so a call that slips back to root fails instead of running WordPress.
The bash launchers' wp_cli/wp_cli_in and the image's onionpress-wp carry
the same command; tests/test_wp_cli_as_www_data.py keeps them equal.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
import tempfile
import threading
from typing import Optional

WP_CONTAINER = "onionpress-wordpress"
WP_USER = "www-data"

# The shared volume, as the WordPress container mounts it.
SHARED_DIR = "/var/lib/onionpress"
ONION_ADDRESS_FILE = SHARED_DIR + "/onion_address"
TOR_HOSTNAME_FILE = "/var/lib/tor/hidden_service/wordpress/hostname"

# $1 = destination, $2 = mode (octal). stdin is the content.
PUT_FILE_SCRIPT = (
    't=$(mktemp "$1.XXXXXX") || exit 1; '
    'if cat > "$t" && chmod "$2" "$t" && mv -f "$t" "$1"; then exit 0; fi; '
    'rm -f "$t"; exit 1'
)

# $1 = destination folder, replaced whole by the tar stream on stdin: it is
# unpacked into a new folder beside it, which then takes its place.
REPLACE_TREE_SCRIPT = (
    'n=$(mktemp -d "$1.XXXXXX") || exit 1; '
    'if chmod 755 "$n" && tar -xf - -C "$n" && rm -rf "$1" && mv -f "$n" "$1"; '
    'then exit 0; fi; '
    'rm -rf "$n"; exit 1'
)

# $1 = destination folder; the tar stream on stdin is unpacked into it,
# over what is there (a restore).
MERGE_TREE_SCRIPT = 'mkdir -p "$1" && exec tar -xf - -C "$1"'

# Run as root. $1 = a folder (canonical: absolute, no link, no trailing
# slash), $2 = a name in it, $3 = -h (that entry) or -R (and all below it).
# -R never enters a folder named "creations": My Creations, the user's
# ~/OnionPress folder bind-mounted at wp-content/creations, whose files are
# the host user's (under rootless Docker a chown there hands them to a
# subordinate uid) and can be many GB. As in onionpress-web-root-owner.sh,
# find follows no link and -execdir chowns each bare name from inside the
# folder find holds, so a link swapped in meanwhile sends nothing elsewhere.
OWN_SCRIPT = (
    'cd -P "$1" && [ "$(pwd -P)" = "$1" ] || exit 1; '
    'if [ "$3" = -h ]; then exec chown -h www-data:www-data -- "$2"; fi; '
    'exec find "./$2" -xdev -name creations -prune -o '
    '-execdir chown -h www-data:www-data -- {} +'
)

# Copies into and out of the containers get a time limit sized to what they
# carry (issue #317): a fixed one cut off a large site's backup or restore.
# The rate is a floor for a slow disk (a Colima VM, a Raspberry Pi), not an
# estimate; the limit is there to end a copy that hangs.
COPY_BYTES_PER_S = 1 << 20


def sized_timeout(nbytes: int, floor_s: int,
                  bytes_per_s: int = COPY_BYTES_PER_S) -> int:
    """Seconds to allow a step that moves *nbytes*: *floor_s* (small sites,
    and whatever the step does besides copying) plus *nbytes* at
    *bytes_per_s*."""
    return int(floor_s + max(0, nbytes or 0) // bytes_per_s)


def tree_bytes(root: str) -> int:
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


def put_file_args(path: str, mode: str = "600",
                  container: str = WP_CONTAINER) -> list[str]:
    """`docker` arguments (no binary) that write stdin to *path* in the
    container as www-data, *mode* (octal string) once in place. 600 suits
    everything only PHP reads; the web root's files are 644."""
    return ["exec", "-i", "-u", WP_USER, container,
            "sh", "-c", PUT_FILE_SCRIPT, "sh", path, mode]


def put_file(path: str, data, mode: str = "600", *, docker_bin: str = "docker",
             env: Optional[dict] = None, timeout: int = 30,
             container: str = WP_CONTAINER) -> subprocess.CompletedProcess:
    """Write *data* (str or bytes) to *path* in the container as www-data."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return subprocess.run(
        [docker_bin] + put_file_args(path, mode, container),
        input=data, capture_output=True, timeout=timeout, env=env)


def exec_args(*command: str, container: str = WP_CONTAINER) -> list[str]:
    """`docker` arguments (no binary) that run *command* as www-data: what
    the host reads from or removes in www-data's folders."""
    return ["exec", "-u", WP_USER, container, *command]


# wp-cli's cache (core and plugin downloads) goes where www-data can write:
# its home, /var/www, is root's, and wp-cli warns on every command that
# wants the cache when it cannot make ~/.wp-cli/cache there.
WP_CLI_CACHE_DIR = "/tmp/wp-cli-cache"
# Set to 1 in a wp-cli process's environment (wp_cli_args(offline=True)),
# the onionpress-tor-proxy mu-plugin makes no outbound request at all. The
# name is a literal in the mu-plugin; tests/test_wp_cli_offline.py ties
# the two together.
OFFLINE_ENV = "ONIONPRESS_WP_OFFLINE"


def wp_cli_args(*args: str, stdin: bool = False, nice: bool = False,
                offline: bool = False,
                container: str = WP_CONTAINER) -> list[str]:
    """`docker` arguments (no binary) that run wp-cli *args* as www-data.
    *stdin* adds `-i`, for the values and secrets wp-cli reads from stdin
    (`wp eval` code reading php://stdin, `wp option update <key>`); *nice*
    runs it at `nice -n 10` (long background sweeps); *offline* sets
    OFFLINE_ENV, which the onionpress-tor-proxy mu-plugin answers by
    refusing every request to a host outside Docker before it is made
    (issue #373: wp-cli loads WordPress, whose update checks went through
    Tor and, with Tor down, hung until the proxy's timeout). For calls
    that need nothing from the network: the provisioning."""
    return (["exec"] + (["-i"] if stdin else [])
            + ["-u", WP_USER, "-e", f"WP_CLI_CACHE_DIR={WP_CLI_CACHE_DIR}"]
            + (["-e", f"{OFFLINE_ENV}=1"] if offline else [])
            + [container] + (["nice", "-n", "10"] if nice else [])
            + ["wp", *args])


def own_args(path: str, recursive: bool = False,
             container: str = WP_CONTAINER) -> list[str]:
    """`docker` arguments (no binary) that give *path* (and, *recursive*,
    everything below it) to www-data, as root, following no link."""
    parent, name = os.path.split(path)
    if (not parent.startswith("/") or parent != os.path.normpath(parent)
            or name in ("", ".", "..")):
        raise ValueError(f"not a canonical container path: {path!r}")
    return ["exec", container, "sh", "-c", OWN_SCRIPT, "sh",
            parent, name, "-R" if recursive else "-h"]


def own(path: str, recursive: bool = False, *, docker_bin: str = "docker",
        env: Optional[dict] = None, timeout: int = 600,
        container: str = WP_CONTAINER) -> subprocess.CompletedProcess:
    """Run own_args. Fails harmlessly when *path* does not exist yet."""
    return subprocess.run(
        [docker_bin] + own_args(path, recursive, container),
        capture_output=True, timeout=timeout, env=env)


def write_tar(src_dir: str, out) -> None:
    """Write *src_dir*'s contents to the binary stream *out* as a tar of
    plain folders and files, names relative to *src_dir*. Anything else
    (a link, a device) raises ValueError: the trees handed over are the
    app's own files and backups already checked for links."""
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tf:
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            rel_root = os.path.relpath(root, src_dir)
            for name in dirs + sorted(files):
                path = os.path.join(root, name)
                arcname = name if rel_root == "." else f"{rel_root}/{name}"
                st = os.lstat(path)
                info = tarfile.TarInfo(arcname)
                info.mtime = int(st.st_mtime)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if stat.S_ISDIR(st.st_mode):
                    info.type = tarfile.DIRTYPE
                    info.mode = stat.S_IMODE(st.st_mode) | 0o700
                    tf.addfile(info)
                elif stat.S_ISREG(st.st_mode):
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as f:
                        fst = os.fstat(f.fileno())
                        if not stat.S_ISREG(fst.st_mode):
                            raise ValueError(f"not a regular file: {arcname}")
                        info.type = tarfile.REGTYPE
                        info.mode = stat.S_IMODE(fst.st_mode) | 0o600
                        info.size = fst.st_size
                        tf.addfile(info, f)
                else:
                    raise ValueError(f"not a plain file or folder: {arcname}")


PUT_TREE_FLOOR_S = 300


def put_tree(src_dir: str, dest: str, *, merge: bool = False,
             docker_bin: str = "docker", env: Optional[dict] = None,
             timeout: Optional[int] = None,
             container: str = WP_CONTAINER) -> subprocess.CompletedProcess:
    """Hand *src_dir*'s contents to the container at *dest*, as www-data.

    *merge* unpacks over what is at *dest* (a restore into wp-content);
    otherwise *dest* is replaced whole (the theme, a plugin). What an older
    version left root's at *dest* is given to www-data first (own), so
    www-data can replace it. Returns the unpacking exec's result; raises
    ValueError for a link or special file in *src_dir*. *timeout* (each of
    the two steps) defaults to one sized to *src_dir* (sized_timeout).
    """
    if timeout is None:
        timeout = sized_timeout(tree_bytes(src_dir), PUT_TREE_FLOOR_S)
    own(dest, recursive=True, docker_bin=docker_bin, env=env,
        timeout=timeout, container=container)
    script = MERGE_TREE_SCRIPT if merge else REPLACE_TREE_SCRIPT
    argv = [docker_bin, "exec", "-i", "-u", WP_USER, container,
            "sh", "-c", script, "sh", dest]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=err, env=env)
        killer = threading.Timer(timeout, proc.kill)
        killer.daemon = True
        killer.start()
        try:
            try:
                write_tar(src_dir, proc.stdin)
            except BrokenPipeError:
                pass  # the container side gave up; its exit status says why
            except BaseException:
                # The container side sees a cut-off archive: tar fails, and
                # a replacement leaves the old folder in place.
                proc.kill()
                proc.wait()
                raise
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            rc = proc.wait()
        finally:
            killer.cancel()
        err.seek(0)
        stderr = err.read().decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(argv, rc, "", stderr)


def shared_onion_address(hostname: str) -> Optional[str]:
    """The onion_address file's content for the tor container's *hostname*
    output, or None unless it is one well-formed v3 address."""
    from .key_manager import is_valid_onion_address
    addr = (hostname or "").strip()
    return addr + "\n" if is_valid_onion_address(addr) else None
