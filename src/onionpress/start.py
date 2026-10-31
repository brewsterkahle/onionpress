"""The launchers' start, in Python (issue #442, CLI-first).

Each launcher's `start_containers` waits for Docker (the Mac's Colima VM
and its recovery stay bash) and then runs `onionpress start-containers`,
the steps here, in the order the bash bodies ran them.

The START SUMMARY contract (#442.2)
-----------------------------------
The Mac launcher ends a start with one line naming the components that
failed (`log_start_summary`, from the bash variable START_ERRORS). A Python
start step names what it could not do by appending one name per line to
the file given as `--errors-file`; the bash reads the names back into
START_ERRORS after the call (`note_start_errors`) and removes the file.
The exit status says only "stop the start" (non-zero) or "carry on" (0):
a best-effort step that failed is a name in the file, never a non-zero
exit. Names are lowercase words joined by hyphens, as the bash used
(`tor`, `provision-post-install`, `wordpress-egress`); anything else in
the file is ignored by the reader.

Linux has no START SUMMARY: its launcher passes no `--errors-file`, and
the failed steps' WARNING / ERROR lines in the log are the record.
"""

import os
import re
import shutil
import subprocess

ERROR_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class StartErrors:
    """The names of the start steps that failed, for the START SUMMARY:
    appended to *path* (the launcher's `--errors-file`) as they happen, so
    a step that fails before a later one dies is still named. Without a
    path the names are only kept here (Linux, tests)."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.names: list[str] = []

    def add(self, name: str) -> None:
        if not ERROR_NAME.fullmatch(name):
            raise ValueError(f"not a start step name: {name!r}")
        self.names.append(name)
        if not self.path:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        except OSError:
            return  # the launcher's temp file is gone: the log still says it
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(name + "\n")

    def __bool__(self) -> bool:
        return bool(self.names)


# ---- running other onionpress steps ----
#
# The bash start ran the WordPress-side steps (provision-post-install, the
# plugins' configure-*, ...) as separate `onionpress …` processes, their
# output appended to the log and their failure only an exit status; the
# Python start does the same, with this interpreter, isolated from the
# current directory (derivatives.isolated_cli_argv, issue #491) and run in
# the data dir.


def _log_handle(log_file: str | None):
    if not log_file:
        return None
    return open(log_file, "a", encoding="utf-8")


def run_cli_step(argv: list[str], *, data_dir: str, log_file: str | None = None) -> int:
    """Run `onionpress --data-dir DATA_DIR ARGV` to the end: its exit
    status (127 when it could not be started). With *log_file* its output
    is appended there, as the bash's `>> "$LOG_FILE" 2>&1` did; without, it
    goes where this process's does."""
    from .derivatives import isolated_cli_argv
    command = isolated_cli_argv("--data-dir", data_dir, *argv)
    out = _log_handle(log_file)
    try:
        return subprocess.run(command, cwd=data_dir, stdin=subprocess.DEVNULL, stdout=out,
                              stderr=subprocess.STDOUT if out else None).returncode
    except OSError:
        return 127
    finally:
        if out:
            out.close()


def run_detached_cli(argv: list[str], *, data_dir: str, log_file: str) -> None:
    """Start `onionpress --data-dir DATA_DIR ARGV` in the background, its
    output appended to *log_file*, so that it outlives this start and the
    launcher's EXIT trap (which kills its own jobs): a session of its own.
    Started by the Linux Settings-page watcher (ONIONPRESS_IN_WATCHER),
    it goes out of the watcher's cgroup through systemd-run, as the bash
    run_detached did, or the watcher's stop would end it."""
    from .derivatives import isolated_cli_argv
    command = isolated_cli_argv("--data-dir", data_dir, *argv)
    if os.environ.get("ONIONPRESS_IN_WATCHER") == "true" and shutil.which("systemd-run"):
        setenv = ["--setenv=PYTHONDONTWRITEBYTECODE=1"]
        if os.environ.get("DOCKER_HOST"):
            setenv.append(f"--setenv=DOCKER_HOST={os.environ['DOCKER_HOST']}")
        try:
            if subprocess.run(
                    ["systemd-run", "--user", "--quiet", "--collect", "--no-block",
                     f"--property=StandardOutput=append:{log_file}",
                     f"--property=StandardError=append:{log_file}",
                     *setenv, "--", *command],
                    stdin=subprocess.DEVNULL, capture_output=True).returncode == 0:
                return
        except OSError:
            pass
    out = _log_handle(log_file)
    try:
        subprocess.Popen(command, cwd=data_dir, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT if out else None,
                         start_new_session=True)
    except OSError:
        pass
    finally:
        if out:
            out.close()


# ---- the first run's onion service key (#442.5) ----
#
# A start whose Docker confirms there is no KEY_VOLUME is a first run (an
# unanswered question stops the start: a missing key volume read from a
# Docker that is not up turned an upgrade into a first run, 2026-09-30).
# The key comes from vanity-keys/ (an imported key, or one a setup put
# there), else is generated (the Mac with the bundled mkp224o on the host,
# Linux with `onionpress generate-vanity` in the tor image), and is
# installed in KEY_VOLUME from the pinned tor image before anything starts
# Tor. A seed that fails removes the volume it made and stops the start:
# left behind, the empty volume would end the first run, and Tor would
# mint a random address. Without a key at all (generation failed) the
# start goes on and Tor makes a random address, as it always did.

DEFAULT_PREFIX = "op2"


def vanity_key_dir(data_dir: str) -> str | None:
    """The imported or generated key's folder under shared/vanity-keys/:
    the first whose name is a whole v3 onion address, a folder and not a
    link (it is bind-mounted into a container). The launchers'
    vanity_key_dir()."""
    from .key_manager import is_valid_onion_address
    base = os.path.join(data_dir, "shared", "vanity-keys")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for name in names:
        path = os.path.join(base, name)
        if (is_valid_onion_address(name)
                and os.path.isdir(path) and not os.path.islink(path)):
            return path
    return None


def configured_prefix(config_file: str) -> str:
    """ADDRESS_PREFIX, else the old VANITY_PREFIX, else op2: what the
    launchers read (a value that is set but empty counts as unset)."""
    from .config import read_value
    return (read_value(config_file, "ADDRESS_PREFIX", "")
            or read_value(config_file, "VANITY_PREFIX", "") or DEFAULT_PREFIX)


def ensure_key_formats(key_dir: str, log_file: str) -> bool:
    """Both key formats in *key_dir* (C Tor's files and the PEM the tor
    entrypoint reads), whichever is missing derived from the other
    (key_manager.ensure_key_formats); a failure's reason to the log file."""
    from .key_manager import ensure_key_formats as derive
    try:
        derive(key_dir)
        return True
    except Exception as e:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"ensure-formats {key_dir}: {e}\n")
        except OSError:
            pass
        return False


def generate_vanity_on_host(prefix: str, data_dir: str, bin_dir: str,
                            log, log_file: str) -> str | None:
    """The Mac's generation: the bundled mkp224o, run on this Mac into
    shared/vanity-keys/ (inside the VM's mount), then the PEM derived from
    its C Tor files. The key's folder, or None with the reason logged as
    an ERROR (the start then goes on with a random address). The Mac
    launcher's generate_vanity_address until #442.5."""
    output_dir = os.path.join(data_dir, "shared", "vanity-keys")
    log(f"Generating custom onion address with prefix '{prefix}'...")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError:
        log(f"ERROR: Cannot create {output_dir} (permissions?). Not falling back to random.")
        return None
    mkp224o = os.path.join(bin_dir, "mkp224o")
    if not os.path.isfile(mkp224o):
        log(f"ERROR: mkp224o not found at {mkp224o}. Not falling back to random.")
        return None
    if not os.access(mkp224o, os.X_OK):
        log(f"ERROR: {mkp224o} exists but is not executable. Not falling back to random.")
        return None
    # A quarantine attribute on the nested helper (a DMG from onionpress.org
    # whose main app alone Gatekeeper approved) has it killed on its first
    # run. Harmless when there is none.
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", mkp224o],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # A prior crashed run's empty folders must not pass for a key.
    from .key_manager import is_valid_onion_address
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name)
        if is_valid_onion_address(name) and os.path.isdir(path) and not os.path.islink(path):
            try:
                os.rmdir(path)
            except OSError:
                pass
    try:
        with open(log_file, "a", encoding="utf-8") as out:
            rc = subprocess.run([mkp224o, "-d", ".", "-n", "1", "-t", "4", prefix],
                                cwd=output_dir, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT).returncode
    except OSError:
        rc = 127
    if rc != 0:
        log(f"ERROR: mkp224o exited with status {rc} (prefix='{prefix}'). This is "
            "usually one of: quarantined nested binary (codesign/notarize issue), "
            "disk full, or platform mismatch. Not falling back to random.")
        return None
    candidates = sorted(n for n in os.listdir(output_dir)
                        if n.startswith(prefix)
                        and os.path.isdir(os.path.join(output_dir, n)))
    if not candidates:
        log(f"ERROR: mkp224o exited 0 but no {prefix}*.onion directory was produced")
        return None
    key_dir = os.path.join(output_dir, candidates[0])
    if not (os.path.isfile(os.path.join(key_dir, "hs_ed25519_secret_key"))
            and os.path.isfile(os.path.join(key_dir, "hostname"))):
        log(f"ERROR: {key_dir} exists but is missing hs_ed25519_secret_key or hostname")
        return None
    log(f"Generated custom address: {candidates[0]}")
    if not ensure_key_formats(key_dir, log_file):
        log(f"ERROR: ensure_key_formats failed for {key_dir}")
        return None
    log("Ensured both key formats for vanity key")
    return key_dir


def _onion_address_line(key_dir: str) -> str:
    try:
        with open(os.path.join(key_dir, "hostname"), encoding="utf-8") as f:
            return f.read().strip() or os.path.basename(key_dir)
    except OSError:
        return os.path.basename(key_dir)


class KeySeed:
    """The first run's key, as each launcher's start did it. *mac* picks
    the platform's flow; *bin_dir* is where the Mac's mkp224o is."""

    def __init__(self, manager, *, mac: bool, bin_dir: str = "", log=None):
        self.manager = manager
        self.paths = manager.paths
        self.docker = manager.docker
        self.mac = mac
        self.bin_dir = bin_dir
        self.log = log or manager._log

    def _to_log_file(self, result):
        self.manager._to_log_file(result)

    def run(self) -> bool:
        """False stops the start."""
        from .containers import KEY_VOLUME
        state = self.manager.volume_state(KEY_VOLUME)
        if state == 2:
            self.log(f"ERROR: Docker did not say whether {KEY_VOLUME} exists; not starting")
            return False
        first_run = state == 1
        return self._mac(first_run) if self.mac else self._linux(first_run)

    # The volume made for the key, removed when the key did not reach it.
    def _seed_or_roll_back(self, key_dir: str, image: str, *, first_run: bool,
                           tor_starts_without: str) -> bool:
        from .containers import KEY_VOLUME
        # The helper's own words (a cp error) go to the log file, as the
        # bash's `>> "$LOG_FILE" 2>&1` put them.
        seeded = self.manager.run_key_seed(key_dir, image)
        self.manager._to_log_file(seeded)
        if seeded.ok:
            self.log(f"Address key installed to the {KEY_VOLUME} volume")
            return True
        if not first_run:
            # The volume holds the key from an earlier start (Linux re-syncs).
            self.log(f"WARNING: Failed to copy address key to the {KEY_VOLUME} volume "
                     "(keeping the one installed earlier)")
            return True
        removed = self.docker.run(["volume", "rm", KEY_VOLUME], timeout=60, quiet=True)
        self._to_log_file(removed)
        if not removed.ok:
            self.log(f"ERROR: could not remove the {KEY_VOLUME} volume the key did not "
                     f"reach; remove it (docker volume rm {KEY_VOLUME}) before the next "
                     f"start{tor_starts_without}")
        self.log(f"ERROR: Failed to copy address key to the {KEY_VOLUME} volume; not starting")
        return False

    def _create_volume(self, name: str) -> bool:
        created = self.docker.run(["volume", "create", name], timeout=60, quiet=True)
        self._to_log_file(created)
        return created.ok

    def _mac(self, first_run: bool) -> bool:
        from .config import validate_address_prefix
        from .containers import KEY_VOLUME
        if not first_run:
            # The prefix is fixed at install (#256 phase 4b): nothing to do.
            return True
        self.log("First run detected - generating custom onion address...")
        prefix = configured_prefix(self.paths.config_file)
        ok, error, suggestion = validate_address_prefix(prefix)
        if not ok:
            # Not fatal: a bad prefix must not stop the app. The real guard
            # is at the point of entry (setup_window.py); this says why the
            # address is an op2… one.
            self.log(f"ERROR: ADDRESS_PREFIX '{prefix}' is invalid: {error.splitlines()[0]}")
            if len(suggestion) >= 2:
                self.log("       Falling back to 'op2'. To use the closest valid prefix, set")
                self.log(f"       ADDRESS_PREFIX={suggestion} in {self.paths.config_file} "
                         "before first launch.")
            else:
                self.log(f"       Falling back to 'op2'. Edit ADDRESS_PREFIX in "
                         f"{self.paths.config_file}.")
            prefix = DEFAULT_PREFIX

        key_dir = vanity_key_dir(self.paths.data_dir)
        if key_dir:
            # A hand-placed or imported key: the prefix plays no part (#507:
            # the prefix line came first and read as if it had).
            self.log(f"Using pre-imported key: {os.path.basename(key_dir)}")
        else:
            self.log(f"Using address prefix: '{prefix}'")
            key_dir = generate_vanity_on_host(prefix, self.paths.data_dir, self.bin_dir,
                                              self.log, self.paths.log_file)
            if key_dir:
                self.log("Custom onion address generated successfully")
        if not key_dir:
            # Tor makes a random address; the ERROR lines above say why the
            # generation failed (often a quarantined nested mkp224o).
            self.log("WARNING: vanity generation failed — continuing with random onion "
                     "address (see ERROR lines above)")
            return True

        image = self.manager.ensure_tor_image()
        if image is None:
            self.log("ERROR: no tor image to install the address key with; not starting "
                     f"(the key stays in {key_dir} for the next start)")
            return False
        if not self._create_volume(KEY_VOLUME):
            self.log(f"ERROR: Failed to create the {KEY_VOLUME} volume")
            return False
        if not self._seed_or_roll_back(key_dir, image, first_run=True,
                                       tor_starts_without=", or Tor will start without the key"):
            return False
        self.log(f"Onion address: {_onion_address_line(key_dir)}")
        return True

    def _linux(self, first_run: bool) -> bool:
        from .containers import KEY_VOLUME, TOR_STATE_VOLUME
        if first_run:
            self.log("First run detected")
        prefix = configured_prefix(self.paths.config_file)
        if first_run and not vanity_key_dir(self.paths.data_dir):
            # mkp224o is in the tor image (multi-arch, so the .deb stays
            # Architecture: all); generate-vanity checks the prefix and the
            # image, and failing leaves a random address for this install.
            self.log(f"First run with ADDRESS_PREFIX={prefix} -- checking tor image "
                     "for mkp224o...")
            if run_cli_step(["generate-vanity"], data_dir=self.paths.data_dir,
                            log_file=self.paths.log_file) == 0:
                self.log("Vanity address generated; will be installed below")
            else:
                self.log("Vanity generation skipped or failed; falling back to random address")

        key_dir = vanity_key_dir(self.paths.data_dir)
        if not key_dir:
            if first_run:
                self.log("No imported key found -- using random onion address")
            return True
        self.log(f"Using pre-imported key: {os.path.basename(key_dir)}")
        if ensure_key_formats(key_dir, self.paths.log_file):
            self.log("Key formats synchronized")
        else:
            self.log("WARNING: Failed to synchronize key formats")

        # The seeds run from the stack's tor image, which a first run has
        # not pulled yet. Without it Tor cannot start either.
        image = self.manager.ensure_tor_image()
        if image is None:
            self.log("ERROR: no tor image to install the onion service key with; not "
                     f"starting (the key stays in {key_dir} for the next start)")
            return False
        # Linux syncs the key into the volume at every start. On a first
        # run a volume Docker would not create stops the start, as the
        # Mac's always did; later the volume is there already.
        if not self._create_volume(KEY_VOLUME) and first_run:
            self.log(f"ERROR: Failed to create the {KEY_VOLUME} volume")
            return False
        if not self._seed_or_roll_back(key_dir, image, first_run=first_run,
                                       tor_starts_without=""):
            return False

        if os.path.isfile(os.path.join(key_dir, "hs_ed25519_secret_key")):
            self._create_volume(TOR_STATE_VOLUME)
            seeded = self.manager.run_tor_state_seed(key_dir, image)
            self.manager._to_log_file(seeded)
            if seeded.ok:
                self.log("Address key installed to tor-state volume (C Tor)")
            else:
                self.log("WARNING: Failed to copy address key to tor-state volume")
        else:
            self.log("WARNING: No C Tor key file (hs_ed25519_secret_key) -- C Tor will "
                     "generate a random address")
        self.log(f"Onion address: {_onion_address_line(key_dir)}")
        return True


# ---- the core services (#442.6) ----

def egress_step(paths, docker, *, wp_port: int | None, log, errors: StartErrors | None) -> None:
    """The launchers' block_wordpress_egress: wordpress_egress.ensure
    (issue #144) before the first `compose up`, its verdict in the
    launcher's words; a block not in place is a WARNING and, on the Mac,
    `wordpress-egress` in the START SUMMARY. Never stops the start."""
    from . import cli, wordpress_egress
    try:
        result = wordpress_egress.ensure(paths, docker, action="apply", wp_port=wp_port)
    except Exception as e:  # a start must never stop here
        result = wordpress_egress.Result(False, "", f"error: {e}")
    cli._log_wordpress_egress_verdict(result, log, paths.log_file)
    if not result.ok and errors is not None:
        errors.add("wordpress-egress")


def wordpress_answers(port: int) -> bool:
    """Whether WordPress answers on this host's published port (the
    launchers' `curl -s --max-time 3 http://localhost:PORT`)."""
    try:
        return subprocess.run(["curl", "-s", "--max-time", "3", f"http://localhost:{port}"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_core(manager, *, mac: bool, wp_port: int, errors: StartErrors | None,
               log=None) -> tuple[bool, bool]:
    """The middle of the launchers' start: the images compose is about to
    start, pulled when missing only (never for a locally built stack),
    then ContainerManager.start_core (egress, retire, network move,
    `compose up` of the core services, 3 attempts 5 s apart), WordPress's
    port for up to 60 s and `wp core is-installed` 15 times 2 s apart (a
    cold MariaDB reads as "not installed" until it answers).
    (started, WordPress installed); not started stops the start."""
    import time
    from .config import read_value
    from .containers import using_local_images
    from .wp_files import wp_cli_args
    log = log or manager._log
    paths = manager.paths

    if using_local_images(paths.config_file):
        log("Skipping image pull: running locally built images")
    else:
        log("Pulling pre-built images (missing only)...")
        # Linux pulls cloudflared too when the tunnel is on (CF_COMPOSE);
        # the Mac's start never did.
        tunnel = not mac and bool(read_value(paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", ""))
        if manager.run_compose_to(["pull", "--policy", "missing"],
                                  out="log" if mac else "stdout",
                                  include_cloudflare=tunnel) != 0:
            log("WARNING: Failed to pull images (using cached)" if mac
                else "WARNING: Failed to pull images (will use cached)")

    egress = lambda: egress_step(paths, manager.docker, wp_port=wp_port, log=log,
                                 errors=errors)
    if not manager.start_core(launcher="mac" if mac else "linux", egress=egress):
        return False, False

    for _ in range(30):
        if wordpress_answers(wp_port):
            break
        time.sleep(2)
    else:
        if mac:
            log(f"WARNING: WordPress did not answer on :{wp_port} within 60s — continuing; "
                "the install check below may misreport a slow database as a fresh site")

    for _ in range(15):
        if manager.docker.run(wp_cli_args("core", "is-installed"), timeout=30, quiet=True).ok:
            return True, True
        time.sleep(2)
    return True, False


# ---- after the core is up (#442.7) ----

INSTALL_FROM_BACKUP = ".install-from-backup"


def _host_timezone() -> str:
    """This machine's IANA zone from /etc/localtime ("" when it is not a
    link into a zoneinfo folder): what the Mac launcher read."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    i = target.find("zoneinfo/")
    return target[i + len("zoneinfo/"):] if i != -1 else ""


def configure_site_timezones(docker, log, log_file: str) -> None:
    """Each site's timezone_string set to this Mac's zone the first time
    it is seen (an empty one only, never a choice someone made), so
    imported posts' times read as their authors saw them; gmt_offset
    blanked beside it. Idempotent. The Mac launcher's
    configure_site_timezones."""
    from .wp_files import wp_cli_args

    def wp(*args):
        return docker.run(wp_cli_args(*args), timeout=60, quiet=True)

    if not wp("core", "is-installed").ok:
        return
    zone = _host_timezone()
    if not zone:
        log("Skipping timezone config: host TZ undetectable")
        return
    listed = wp("site", "list", "--field=url")
    sites = listed.stdout.split() if listed.ok else []
    if not sites:
        siteurl = wp("option", "get", "siteurl")
        sites = siteurl.stdout.split() if siteurl.ok else []
    for url in sites:
        current = wp("option", "get", "timezone_string", f"--url={url}")
        if current.ok and current.stdout.strip():
            continue
        updated = wp("option", "update", "timezone_string", zone, f"--url={url}")
        _append(log_file, updated)
        log(f"✓ Set timezone_string={zone} for {url}" if updated.ok
            else f"WARNING: Failed to set timezone for {url}")
        _append(log_file, wp("option", "update", "gmt_offset", "", f"--url={url}"))


def _append(log_file: str, result) -> None:
    text = (result.stdout or "") + (result.stderr or "")
    if not text:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _stack_images(manager) -> list[str]:
    """The image references the stack runs, as compose resolves them (the
    pins, or ONIONPRESS_*_IMAGE overrides), cloudflared's included; one
    each, in compose's order. [] when compose does not answer."""
    result = manager.docker.compose(
        ["config", "--images"], compose_files=manager._compose_files(include_cloudflare=True),
        timeout=60, extra_env=manager._build_env())
    if not result.ok:
        return []
    refs = []
    for line in result.stdout.splitlines():
        ref = line.strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# The repository each mirrored image was pulled from before #502 put the
# third-party pins under ghcr.io/brewsterkahle (build/image-mirrors.txt,
# held equal by tests/test_start_post_up.py). An install that ran the old
# pins keeps those images under the old names; they are the mirror's
# predecessors, cleaned up by the same rule.
FORMER_REPOSITORIES = {
    "ghcr.io/brewsterkahle/onionpress-mariadb": "mariadb",
    "ghcr.io/brewsterkahle/onionpress-autoheal": "willfarrell/autoheal",
    "ghcr.io/brewsterkahle/onionpress-cloudflared": "cloudflare/cloudflared",
    "ghcr.io/brewsterkahle/onionpress-socket-proxy": "ghcr.io/wollomatic/socket-proxy",
}


def _repository(ref: str) -> str:
    """`mariadb:13.0.2@sha256:…` -> `mariadb`; a registry's port stays."""
    name = ref.split("@", 1)[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    return name[:colon] if colon > slash else name


def prune_superseded_images(manager, log) -> None:
    """Remove the images a release's new pins superseded (the start's
    pull fetched the new ones; the old ones stayed on the VM's fixed disk).
    For each image the stack runs, only once its pinned image is here: the
    other images of that repository, and of the repository a mirrored
    image was pulled from before (FORMER_REPOSITORIES), that have no tag
    (the same digest under the old name is the pinned image itself, kept). OnionPress pulls by
    digest, so every image it pulled has none; a tagged one (`mariadb:11`
    of the user's own on a Linux Docker, a local build) is never touched.
    Plain `docker image rm`, never forced: an image a container still uses,
    running or stopped, stays. Nothing for a locally built stack. Best
    effort: a failure is a log line, never a stopped start."""
    from .containers import using_local_images
    if using_local_images(manager.paths.config_file):
        return
    docker = manager.docker
    for ref in _stack_images(manager):
        if "@sha256:" not in ref:
            continue
        repo = _repository(ref)
        pinned = docker.run(["image", "inspect", "--format", "{{.Id}}", ref],
                            timeout=30, quiet=True)
        if not pinned.ok or not pinned.stdout.strip():
            log(f"Image cleanup: {repo} left as is (its pinned image is not here)")
            continue
        for name in (repo, FORMER_REPOSITORIES.get(repo)):
            if name:
                _remove_older_untagged(docker, name, pinned.stdout.strip(), log)


def _remove_older_untagged(docker, repo: str, pinned_id: str, log) -> None:
    listed = docker.run(["image", "ls", "--no-trunc", "--format", "{{.ID}} {{.Tag}}", repo],
                        timeout=30, quiet=True)
    if not listed.ok:
        return
    older = []
    for line in listed.stdout.splitlines():
        image_id, _, tag = line.strip().partition(" ")
        if (re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) and tag == "<none>"
                and image_id != pinned_id and image_id not in older):
            older.append(image_id)
    removed = kept = 0
    for image_id in older:
        if docker.run(["image", "rm", image_id], timeout=60, quiet=True).ok:
            removed += 1
        else:
            kept += 1
    if removed:
        log(f"Image cleanup: removed {removed} superseded {repo} image(s)")
    if kept:
        log(f"Image cleanup: kept {kept} older {repo} image(s) still in use")


def warn_if_socks_port_exposed(manager, log) -> None:
    """Linux: Tor's SOCKS port must be published on 127.0.0.1 only. A
    compose file from before that (re-running install.sh leaves it) still
    publishes it more widely; Docker's own mapping is the authority."""
    result = manager.docker.run(["port", "onionpress-tor", "9050/tcp"], timeout=30, quiet=True)
    exposed = [l for l in result.stdout.splitlines()
               if l.strip() and not l.startswith("127.0.0.1:")] if result.ok else []
    if exposed:
        compose = os.path.join(manager.paths.docker_dir, "docker-compose.yml")
        log(f"WARNING: Tor's SOCKS port is published beyond this machine "
            f"({' '.join(exposed)}): {compose} is out of date. Install the current "
            "OnionPress .deb to replace it.")


def post_up(manager, *, mac: bool, installed: bool, errors: StartErrors | None,
            themes_dir: str, plugins_dir: str, scripts_dir: str, log=None) -> None:
    """Everything after the core services are up, in the launchers' order:
    the Mac's .htaccess repair, Tor (`tor` in the START SUMMARY when it
    will not start), Linux's SOCKS port check, the pinned plugins, the
    install-from-backup import (both platforms since #508: a restore that
    failed past its import keeps the staging for it), deactivate-wp-statistics and
    provision-post-install (each a WARNING and a START SUMMARY name when it
    fails), the plugins' setup, the image-derivative rebuild and the Mac's
    archive.org login (both detached: they outlive the launcher), the Mac's
    site timezones, then the superseded images' cleanup. Best effort: nothing here stops the start. Linux's
    install-state steps (WordPress's port, the heartbeat hold, issue #464),
    its OnionHeaven activation watcher and both platforms' Cloudflare
    tunnel (start_cloudflare_tunnel, one bash function held identical in
    the two launchers, last in a start) stay in the launcher, around this
    step."""
    from . import bundled_plugins, derivatives, multisite
    from .wp_files import put_file_args
    log = log or manager._log
    paths, docker = manager.paths, manager.docker

    def name_failure(name):
        if errors is not None:
            errors.add(name)

    if mac:
        if installed:
            log("WordPress is installed")
            # The block editor's REST API needs the rewrite rules, which a
            # WordPress auto-update or a recreated volume can lose.
            if not docker.run(["exec", "onionpress-wordpress", "test", "-s",
                               "/var/www/html/.htaccess"], timeout=30, quiet=True).ok:
                log("Restoring .htaccess rewrite rules (missing or empty)")
                written = docker.run(put_file_args("/var/www/html/.htaccess", "644"),
                                     input=multisite.HTACCESS_BODY, timeout=30, quiet=True)
                log("✓ .htaccess restored" if written.ok else "WARNING: Failed to restore .htaccess")
        else:
            log("WordPress not yet installed — will be configured by setup window")
        log("Starting Tor containers...")

    # Always Tor: the address is ready by the time setup is done. Never
    # before its key volume is in place (the steps before this made sure;
    # starting tor would create an empty one that later starts take for
    # the key), as ContainerManager.start_tor. --no-deps: tor depends on
    # wordpress, and without it compose recreates WordPress under the
    # launcher's exported bind host, publishing the installer to the
    # network before setup on Linux (#464; found by the live checks).
    why = manager.key_volume_not_ready()
    rc = 1 if why else manager.run_compose_to(["up", "-d", "--no-deps", "tor", "onionheaven"],
                                              out="log")
    if why:
        log(f"ERROR: not starting Tor: {why}")
        name_failure("tor")
    elif rc != 0:
        if mac:
            log(f"ERROR: Failed to start tor/onionheaven containers (rc={rc}) — see "
                f"{paths.log_file}")
            name_failure("tor")
        else:
            log("ERROR: Failed to start tor/onionheaven containers")
    if not mac:
        warn_if_socks_port_exposed(manager, log)

    host = bundled_plugins.PluginHost(
        docker=docker, config_file=paths.config_file, scripts_dir=scripts_dir,
        log_file=paths.log_file, log=log,
        run_step=lambda argv: run_cli_step(argv, data_dir=paths.data_dir,
                                           log_file=paths.log_file))
    bundled_plugins.install_bundled_plugins(host)

    # A backup staged for this install (the welcome screen's Restore from
    # backup, or a restore that failed past its import, #508): its database
    # and content go into the running containers before the provisioning,
    # which then refreshes the theme and mu-plugins to this version's.
    staging = ""
    marker = os.path.join(paths.data_dir, INSTALL_FROM_BACKUP)
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                staging = f.readline().rstrip("\n")
        except OSError:
            staging = ""
        if staging and os.path.isdir(staging):
            log(f"Install-from-backup: importing artifacts from {staging}")
            if run_cli_step(["import-backup-artifacts", "--staging", staging],
                            data_dir=paths.data_dir) != 0:
                log("ERROR: install-from-backup import failed — leaving marker for retry")
                staging = ""
            else:
                # The backup's database is in: the site is installed from
                # here on, whatever start_core saw before the import (the
                # image rebuild below used to wait one more start, #507).
                installed = True
        else:
            log(f"Install-from-backup: marker present but staging missing ({staging}); "
                "ignoring")
            staging = ""

    step_log = paths.log_file if mac else None
    rc = run_cli_step(["deactivate-wp-statistics"], data_dir=paths.data_dir, log_file=step_log)
    if rc != 0:
        log(f"WARNING: deactivate-wp-statistics failed (rc={rc}) — continuing; see "
            f"{paths.log_file}")
        name_failure("deactivate-wp-statistics")
    rc = run_cli_step(["provision-post-install", "--themes-dir", themes_dir,
                       "--plugins-dir", plugins_dir],
                      data_dir=paths.data_dir, log_file=step_log)
    if rc != 0:
        log(f"WARNING: provision-post-install failed (rc={rc}) — themes/plugins may be "
            f"missing; see {paths.log_file}")
        name_failure("provision-post-install")
    bundled_plugins.setup_bundled_plugins(host)

    # Only after a successful import and provisioning: an interrupted run
    # imports again at the next start.
    if staging:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        log("Install-from-backup: complete (staging cleaned up)")

    # Image derivatives (#257): a restore's resized images, rebuilt in the
    # background (niced, resumable), after provision-post-install put the
    # image-fallback mu-plugin in place.
    if installed and derivatives.regeneration_pending(paths.data_dir, docker.docker_bin):
        log("Image derivatives: rebuild pending — starting in the background")
        run_detached_cli(["regenerate-derivatives"], data_dir=paths.data_dir,
                         log_file=paths.log_file)
    if mac:
        # The archive.org login over Tor can take 30 s; a no-op once the
        # keys are there.
        run_detached_cli(["ensure-archive-s3-keys"], data_dir=paths.data_dir,
                         log_file=paths.log_file)
        configure_site_timezones(docker, log, paths.log_file)
    # Last: everything this start runs is up, so what an older image still
    # serves is in use and kept.
    prune_superseded_images(manager, log)


# ---- the whole start (#442.8) ----

STOP, INSTALLED, NOT_INSTALLED = 1, 0, 3


def start_containers(manager, *, mac: bool, wp_port: int, errors: StartErrors | None,
                     bin_dir: str = "", scripts_dir: str = "", themes_dir: str = "",
                     plugins_dir: str = "", until_core: bool = False, log=None) -> int:
    """Both launchers' start once Docker answers, in their order: a key
    import Docker was not up for, the key volume's migration from its old
    name, the first run's key (all three before anything that could start
    Tor, and each stopping the start when it cannot be done), the core
    services, then everything after them (post_up). STOP, or whether
    WordPress is installed (INSTALLED / NOT_INSTALLED). *until_core* ends
    after the core services: Linux runs its install-state steps (which
    name WordPress's published address, issue #464) in the launcher and
    then `start-post-up`."""
    log = log or manager._log
    if not manager.apply_pending_key_import():
        return STOP
    # Upgraded installs: the key volume comes over from its old name
    # before the first-run check looks for the new one.
    if not manager.migrate_key_volume():
        log("ERROR: cannot start without the onion service key volume")
        return STOP
    if not KeySeed(manager, mac=mac, bin_dir=bin_dir, log=log).run():
        return STOP
    ok, installed = start_core(manager, mac=mac, wp_port=wp_port, errors=errors, log=log)
    if not ok:
        return STOP
    if not until_core:
        post_up(manager, mac=mac, installed=installed, errors=errors, themes_dir=themes_dir,
                plugins_dir=plugins_dir, scripts_dir=scripts_dir, log=log)
    return INSTALLED if installed else NOT_INSTALLED
