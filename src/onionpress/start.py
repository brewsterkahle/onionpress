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
        if self.manager.run_key_seed(key_dir, image).ok:
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
        self.log(f"Using address prefix: '{prefix}'")

        key_dir = vanity_key_dir(self.paths.data_dir)
        if key_dir:
            self.log(f"Using pre-imported key: {os.path.basename(key_dir)}")
        else:
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
            if self.manager.run_tor_state_seed(key_dir, image).ok:
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
    start, pulled when missing only (never for a locally built stack;
    UPDATE_ON_LAUNCH's refresh is the launcher's update_images, before
    this), then ContainerManager.start_core (egress, retire, network move,
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
