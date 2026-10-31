"""Container lifecycle management for OnionPress.

Handles starting/stopping the core service stack (Tor, WordPress, MariaDB)
and OnionHeaven farm workers.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from . import onion_hostname, platform, wordpress_egress
from .docker import Docker, DockerError
from .config import (
    DEFAULTS, SETTINGS_PAGE_KEYS, Secrets, default_value, ensure_secrets, load_secrets,
    read_value,
    PortConfig, detect_port_offset,
)
from .platform import OnionPressPaths
from .ports import port_environ
# Re-exported: their home is image_overrides, which imports no docker code
# so the menubar can ask without loading it (issue #475).
from .image_overrides import (  # noqa: F401
    _default_config_file, image_override, using_local_images,
)


CORE_SERVICES = ["wordpress", "db", "onionheaven", "autoheal"]
ALL_SERVICES = ["wordpress", "db", "tor", "onionheaven", "autoheal"]
# Pinned to digest. The literal below is propagated from build/image-pins.env
# by build/refresh-image-digests.sh, which writes every consumer at once;
# tests/test_image_pins.py fails if any of them drift apart.
#
# Resolution order matches docker-compose.yml's `onionheaven` service, so the
# menubar path and the compose path always agree: the service-specific
# ONIONHEAVEN_IMAGE wins, then the stack-wide ONIONPRESS_TOR_IMAGE (what
# build/build-images.sh exports for a locally built stack), then the pin.
ONIONHEAVEN_IMAGE_PIN = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:eea0b54a04ebe60a0db66ebcff2d5cf1875485cadcee1df3ce80e36374a039dc"


def onionheaven_image(config_file: str | None = None) -> str:
    """The image to run OnionHeaven takeover workers from.

    Resolved on each call rather than at import, so it picks up an override
    written to ~/.onionpress/config without a restart — and so it sees the
    same config the launchers do (see image_override).
    """
    return (image_override("ONIONHEAVEN_IMAGE", config_file)
            or image_override("ONIONPRESS_TOR_IMAGE", config_file)
            or ONIONHEAVEN_IMAGE_PIN)


def tor_image(config_file: str | None = None) -> str:
    """The stack's tor image: ONIONPRESS_TOR_IMAGE, else the pin.

    The one-shot helpers that handle the onion service key (the restore's key
    seed here; the launchers' KEY_HELPER_IMAGE) run from it, never from a
    floating tag: the pin is one set of bytes, and the image is the one that
    will run Tor with that key anyway.
    """
    return image_override("ONIONPRESS_TOR_IMAGE", config_file) or ONIONHEAVEN_IMAGE_PIN


# The onion service key volume, its name until 2026-09-25, and the marker the
# launchers' migrate_key_volume() keeps beside an unverified copy of the
# first into the second. Starting Tor mounts the new volume, and Docker
# creates it, empty, when it is not there: every later start would then
# take it for the migrated key, and Tor would mint a new address.
KEY_VOLUME = "onionpress-onion-keys"
OLD_KEY_VOLUME = "onionpress-arti-state"
KEY_VOLUME_MIGRATION_PENDING = ".key-volume-migration-pending"
# Written by a key import or a restore of another address that could not
# wipe the old keystores (Docker was not running): the next start does it
# (ContainerManager.apply_pending_key_import) before anything else.
IMPORT_KEY_PENDING = ".import-key-pending"
# C Tor's keystore (/var/lib/tor/hidden_service/<name>/), and the volume a
# migration of long ago left behind.
TOR_STATE_VOLUME = "onionpress-tor-state"
OLD_TOR_KEYS_VOLUME = "onionpress-tor-keys"


def forget_key_volume_migration(data_dir: str) -> None:
    """Drop the migration marker. Every path that wipes the key volumes for
    a replaced identity calls this (the launchers `rm -f` it): kept beside
    an old volume that a wipe failed to remove, the marker would have the
    next start remove the new key and copy the replaced one back."""
    try:
        os.remove(os.path.join(data_dir, KEY_VOLUME_MIGRATION_PENDING))
    except FileNotFoundError:
        pass


# The first-run key seed (ContainerManager.run_key_seed): the delivered PEM
# where the tor entrypoint reads it, <name>/ks_hs_id.ed25519_expanded_private
# in KEY_VOLUME, root's, 0700/0600. The launchers' start (#442.5) and the
# restore (cli.cmd_restore) run it; the key folder is bind-mounted read-only.
KEY_SEED_SCRIPT = ("mkdir -p /dest/wordpress && "
                   "cp /src/ks_hs_id.ed25519_expanded_private /dest/wordpress/ && "
                   "chown -R 0:0 /dest && "
                   "chmod 700 /dest /dest/wordpress && "
                   "chmod 600 /dest/wordpress/*")
# Linux's start also puts C Tor's own key files in TOR_STATE_VOLUME
# (/var/lib/tor/hidden_service/wordpress/), debian-tor's: the tor image's
# user. The entrypoint re-derives them from the PEM anyway (issue #311);
# kept as the Linux launcher always did it, word for word (its indentation
# too: the start traces compare the argv).
TOR_STATE_SEED_SCRIPT = """
                    set -e
                    mkdir -p /dest/hidden_service/wordpress/authorized_clients
                    cp /src/hs_ed25519_secret_key /dest/hidden_service/wordpress/
                    cp /src/hs_ed25519_public_key /dest/hidden_service/wordpress/ 2>/dev/null || true
                    cp /src/hostname /dest/hidden_service/wordpress/
                    chown -R debian-tor:debian-tor /dest/hidden_service
                    chmod 700 /dest/hidden_service /dest/hidden_service/wordpress
                    chmod 600 /dest/hidden_service/wordpress/hs_ed25519_secret_key
                    chmod 644 /dest/hidden_service/wordpress/hostname
                """


def run_key_seed(docker, key_dir: str, image: str, *, timeout: int = 300):
    """Copy the PEM in *key_dir* into KEY_VOLUME (KEY_SEED_SCRIPT) from
    *image*, the pinned tor image (ensure_tor_image): no network, only
    DAC_OVERRIDE (root reads the key out of the user's 0700 folder, which
    Linux and a VM mount that enforces it check), no new privileges. The
    DockerResult; the caller decides what a failure means (a first run
    removes the volume and stops, a restore stops)."""
    return docker.run(
        ["run", "--rm",
         "--network", "none",
         "--cap-drop", "ALL",
         "--cap-add", "DAC_OVERRIDE",
         "--security-opt", "no-new-privileges:true",
         "-v", f"{KEY_VOLUME}:/dest",
         "--mount", f"type=bind,source={key_dir},target=/src,readonly",
         "--entrypoint", "sh",
         image, "-c", KEY_SEED_SCRIPT],
        timeout=timeout, quiet=True)


def run_tor_state_seed(docker, key_dir: str, image: str, *, timeout: int = 300):
    """C Tor's key files from *key_dir* into TOR_STATE_VOLUME
    (TOR_STATE_SEED_SCRIPT): no network; it reads the user's files and
    writes into debian-tor's folders (DAC_OVERRIDE), chowns (CHOWN) and
    chmods what is no longer root's (FOWNER). Linux's start only."""
    return docker.run(
        ["run", "--rm",
         "--network", "none",
         "--cap-drop", "ALL",
         "--cap-add", "CHOWN",
         "--cap-add", "DAC_OVERRIDE",
         "--cap-add", "FOWNER",
         "--security-opt", "no-new-privileges:true",
         "-v", f"{TOR_STATE_VOLUME}:/dest",
         "--mount", f"type=bind,source={key_dir},target=/src,readonly",
         "--entrypoint", "sh",
         image, "-c", TOR_STATE_SEED_SCRIPT],
        timeout=timeout, quiet=True)


# The copy ContainerManager.migrate_key_volume runs in the tor image
# (`sh -c SCRIPT migrate-key-volume /old /new`): arti's files are its own
# user's, in 0700 folders, so reading them takes DAC_OVERRIDE; the copy is
# root's, 0700/0600, as the key seeds make it, so nothing else is needed.
# It walks down to the keystore one listed folder at a time, so a folder
# it cannot list (without DAC_OVERRIDE, say) stops it instead of passing
# for a missing keystore, as it used to: an empty copy that the next start
# took for the migrated key. The keystore may hold folders and files only;
# every name in it has to be in the copy and every file the same bytes.
# tests/test_install_invariants.TestMigrationScriptLocally runs it under sh.
KEY_VOLUME_MIGRATION_SCRIPT = r"""
            set -eu
            dir="$1" dest="$2"
            for name in state keystore hss; do
                listing=$(ls -A "$dir")
                if ! printf "%s\n" "$listing" | grep -qxF "$name"; then
                    echo "the old key volume holds no keystore ($dir has no $name): nothing to copy"
                    chmod 700 "$dest"
                    exit 0
                fi
                dir="$dir/$name"
                if [ -L "$dir" ] || [ ! -d "$dir" ]; then
                    echo "$dir is not a folder" >&2
                    exit 1
                fi
            done
            wanted=$(cd "$dir" && find .)
            odd=$(cd "$dir" && find . ! -type d ! -type f)
            if [ -n "$odd" ]; then
                echo "the old keystore holds more than folders and files: $odd" >&2
                exit 1
            fi
            cp -R "$dir/." "$dest/"
            chmod 700 "$dest"
            find "$dest" -mindepth 1 -type d -exec chmod 700 {} +
            find "$dest" -type f -exec chmod 600 {} +
            got=$(cd "$dest" && find .)
            if [ "$(printf "%s\n" "$got" | sort)" != "$(printf "%s\n" "$wanted" | sort)" ]; then
                echo "the copy does not hold what the old keystore holds" >&2
                exit 1
            fi
            differ=$(cd "$dir" && find . -type f ! -exec cmp -s {} "$dest/{}" \; -print)
            if [ -n "$differ" ]; then
                echo "did not copy intact: $differ" >&2
                exit 1
            fi
            echo "copied the old keystore: $(find "$dest" -type f | wc -l) files, each compared with its original"
        """


# The OnionHeaven hub data volume (onionheaven_common.HUB_DATA_VOLUME and
# ONIONHEAVEN_DATA_DIR in the tor image), and the containers that use it.
HUB_DATA_VOLUME = "onionpress-onionheaven-data"
HUB_DATA_DIR = "/var/lib/onionheaven"
_HUB_CONTAINER_RE = re.compile(
    r"onionpress-tor|onionheaven|onionheaven-(poll|takeover)-[0-9]+")


# What OnionPress puts on onionpress-network: its compose services (wordpress
# and db too, before issue #144 moved them) and the takeover workers.
# migrate_tor_network removes these and only disconnects anything else (the
# launchers' migrate_tor_network runs it: `onionpress migrate-tor-network`).
_TOR_NETWORK_CONTAINER_RE = re.compile(
    r"onionpress-(tor|wordpress|db|cloudflared)|onionheaven"
    r"|onionheaven-(poll|takeover)-[0-9]+")
_BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"


def tor_network_is_old(options_json: str) -> bool:
    """True only if `docker network inspect --format '{{json .Options}}'
    onionpress-network` is a network whose bridge is not the fixed one (Docker
    named it br-<id>); no network, or output that is not that, is False."""
    try:
        options = json.loads(options_json)
    except (TypeError, ValueError):
        return False
    return (isinstance(options, dict)
            and options.get(_BRIDGE_NAME_OPTION) != wordpress_egress.TOR_BRIDGE)


def mounts_hub_data(mounts_json: str) -> bool:
    """False only if `docker inspect`'s Mounts certainly lack the hub data
    volume at HUB_DATA_DIR; anything unreadable is True (doubt keeps a
    container)."""
    try:
        mounts = json.loads(mounts_json)
    except (TypeError, ValueError):
        return True
    if not isinstance(mounts, list):
        return True
    return any(isinstance(m, dict) and m.get("Name") == HUB_DATA_VOLUME
               and m.get("Destination") == HUB_DATA_DIR for m in mounts)


@dataclass
class ContainerStatus:
    """Status of the OnionPress container stack."""
    runtime: str = ""  # 'colima-bundled' or 'docker-system'
    onion_address: str = ""
    wp_ready: bool = False
    tor_bootstrapped: bool = False
    services: list = field(default_factory=list)


class ContainerManager:
    """Manages the OnionPress container stack."""

    def __init__(
        self,
        docker: Docker,
        paths: OnionPressPaths,
        port_config: PortConfig | None = None,
        log_func: Callable[[str], None] | None = None,
        egress: Callable[[], object] | None = None,
    ):
        self.docker = docker
        self.paths = paths
        self.port_config = port_config or detect_port_offset()
        self.log_func = log_func
        # Shuts WordPress's network, the Tor side's and WordPress's published
        # port before start_core starts anything on them
        # (wordpress_egress.ensure; injectable for tests).
        self._egress = egress or (
            lambda: wordpress_egress.ensure(self.paths, self.docker, self._log,
                                            wp_port=self.port_config.wp_port))

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def _compose_files(self, include_cloudflare: bool = False) -> list | None:
        """Return list of compose files to use."""
        if not self.paths.docker_dir:
            return None
        files = [os.path.join(self.paths.docker_dir, "docker-compose.yml")]
        if include_cloudflare:
            cf_file = os.path.join(self.paths.docker_dir, "docker-compose.cloudflare.yml")
            if os.path.exists(cf_file):
                files.append(cf_file)
        return files

    def _secrets_env(self) -> dict:
        """Load secrets and return as env dict."""
        secrets = ensure_secrets(self.paths.secrets_file)
        return secrets.as_env()

    def _build_env(self) -> dict:
        """Build extra environment for docker compose."""
        env = self._secrets_env()
        env.update(port_environ(self.port_config))
        # The Creations bind mount in docker-compose.yml; unset, compose
        # falls back to a throwaway /tmp path.
        env["ONIONPRESS_DOCUMENTS_DIR"] = self.paths.documents_dir

        # Read config-driven env vars

        cf_token = read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", "")
        if cf_token:
            env["CLOUDFLARE_TUNNEL_TOKEN"] = cf_token

        # Run as an OnionHeaven hub; only an exact "yes" counts, checked in
        # the containers. Always set, as the launchers do, so a compose call
        # from here never recreates tor/onionheaven with a different value.
        env["ONIONHEAVEN_ACCEPT_REGISTRATIONS"] = read_value(
            self.paths.config_file, "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "")

        # The hub's takeover tunables, as both launchers export them (issue
        # #353): compose hands ONIONHEAVEN_PROPAGATION_DELAY to tor and
        # onionheaven as TOR_PROPAGATION_DELAY and ONIONHEAVEN_MAX_SERVICES
        # to onionheaven and onionheaven-farm. A value the config check
        # refuses (a hand edit) is left to the default.
        for key in ("ONIONHEAVEN_MAX_SERVICES", "ONIONHEAVEN_PROPAGATION_DELAY"):
            value = read_value(self.paths.config_file, key, "")
            env[key] = value if SETTINGS_PAGE_KEYS[key](value) else default_value(key)

        return env

    def ensure_tor_image(self) -> str | None:
        """The tor image (tor_image), made sure to be here before a helper
        that handles the onion service key runs from it: pulled by its
        pinned reference when missing, never for a locally built stack
        (using_local_images). None when it is not here and cannot be.
        Mirrors ensure_key_helper_image() in both launchers (the key seeds,
        Linux's backup key reads); migrate_key_volume runs it."""
        image = tor_image(self.paths.config_file)
        inspect = ["image", "inspect", "--format", "{{.Id}}", image]
        if self.docker.run(inspect, timeout=30, quiet=True).ok:
            return image
        if using_local_images(self.paths.config_file):
            self._log(f"ERROR: the locally built tor image {image} is not here "
                      "(build it with build/build-images.sh)")
            return None
        self._log(f"Pulling {image} for the onion service key helpers...")
        if not self.docker.run(["pull", image], timeout=600).ok:
            self._log(f"ERROR: could not pull {image}")
            return None
        return image if self.docker.run(inspect, timeout=30, quiet=True).ok else None

    def key_volume_not_ready(self) -> str | None:
        """Why Tor must not start yet, or None.

        The launchers move the key out of OLD_KEY_VOLUME before anything
        starts Tor (their migrate_key_volume, this manager's through
        `onionpress migrate-key-volume`); start_tor does not run that step
        and refuses instead while it is still due: the old volume is there
        and the new one is not, or holds an unverified copy (the marker). An
        unanswered `docker volume ls` refuses too."""
        pending = os.path.lexists(
            os.path.join(self.paths.data_dir, KEY_VOLUME_MIGRATION_PENDING))
        result = self.docker.run(["volume", "ls", "--format", "{{.Name}}"],
                                 timeout=30, quiet=True)
        if not result.ok:
            return "Docker did not say which volumes exist"
        names = set(result.stdout.split())
        if OLD_KEY_VOLUME in names and (KEY_VOLUME not in names or pending):
            return (f"the onion service key is still in {OLD_KEY_VOLUME}; "
                    "start OnionPress with its launcher (`onionpress start`), "
                    f"which moves it to {KEY_VOLUME} first")
        return None

    def volume_state(self, name: str) -> int:
        """Whether Docker has volume *name*: 0 yes, 1 no, 2 Docker could not
        say (the daemon is not up yet, e.g. the Colima VM is still booting).
        Callers must treat 2 as "stop", never as "no": a missing key volume
        is the first-run signal, so reading a Docker error as absence turns
        an upgrade into a first run. The launchers' docker_volume_state."""
        result = self.docker.run(["volume", "ls", "--format", "{{.Name}}"],
                                 timeout=30, quiet=True)
        if not result.ok:
            return 2
        return 0 if name in result.stdout.split() else 1

    def _log_lines(self, result) -> None:
        """A helper's output, line by line, where the launchers appended it
        to the log."""
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                self._log(line.rstrip())

    def _to_log_file(self, result) -> None:
        """A docker call's output, appended to the log file only (the bash
        `>> "$LOG_FILE" 2>&1`)."""
        text = result.stdout + result.stderr
        if not text:
            return
        try:
            with open(self.paths.log_file, "a", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def run_key_seed(self, key_dir: str, image: str, *, timeout: int = 300):
        return run_key_seed(self.docker, key_dir, image, timeout=timeout)

    def run_tor_state_seed(self, key_dir: str, image: str, *, timeout: int = 300):
        return run_tor_state_seed(self.docker, key_dir, image, timeout=timeout)

    def apply_pending_key_import(self) -> bool:
        """Finish a key import Docker was not up for: with the
        IMPORT_KEY_PENDING marker in the data dir, remove the old
        keystores (KEY_VOLUME, which holds the delivered PEM; OLD_KEY_VOLUME,
        or the migration would bring the replaced identity back;
        TOR_STATE_VOLUME, C Tor's copy, which the entrypoint would keep
        over the imported key), force-removing any container that holds
        one, then drop the leftover OLD_TOR_KEYS_VOLUME, an unfinished
        migration's marker and the import marker. False stops the start:
        only Docker's confirmed "no such volume" counts as removed, and
        the marker stays for the next start whenever a keystore survives
        or could not be checked (a dropped marker beside an old keystore
        would keep the replaced identity). Both launchers' start ran this
        as bash until #442.4 (`onionpress apply-pending-key-import`)."""
        data_dir = self.paths.data_dir
        marker = os.path.join(data_dir, IMPORT_KEY_PENDING)
        if not os.path.isfile(marker):
            return True
        dash = "—" if platform.detect_os() == platform.OS.MACOS else "--"
        self._log(f"Pending key import detected {dash} removing old key and tor-state volumes...")
        for vol in (KEY_VOLUME, OLD_KEY_VOLUME, TOR_STATE_VOLUME):
            self._to_log_file(self.docker.run(["volume", "rm", vol], timeout=60, quiet=True))
            state = self.volume_state(vol)
            if state == 0:
                self._log(f"Volume {vol} still in use {dash} force-removing containers using it...")
                holders = self.docker.run(["ps", "-aq", "--filter", f"volume={vol}"],
                                          timeout=30, quiet=True)
                self._to_log_file(self.docker.run(
                    ["rm", "-f", *(holders.stdout.split() if holders.ok else [])],
                    timeout=60, quiet=True))
                self._to_log_file(self.docker.run(["volume", "rm", vol], timeout=60, quiet=True))
                state = self.volume_state(vol)
            if state != 1:
                self._log(f"ERROR: could not remove the old {vol} volume; keeping the "
                          "pending key import and not starting")
                return False
        self._to_log_file(self.docker.run(["volume", "rm", OLD_TOR_KEYS_VOLUME],
                                          timeout=60, quiet=True))
        # No unfinished key volume migration survives a replaced identity.
        forget_key_volume_migration(data_dir)
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        return True

    def migrate_key_volume(self) -> bool:
        """One-time migration of the onion service key volume from its
        historical name; False means the start must not go on.

        Until 2026-09-25 the delivered PEM lived in OLD_KEY_VOLUME at
        state/keystore/hss/<name>/ (arti's keystore layout); it now lives in
        KEY_VOLUME at <name>/. This runs before the first-run check, which
        keys off the NEW volume's existence: without it an upgraded install
        would look like a fresh one and mint a new address. For the same
        reason it refuses to let the start continue when the copy fails,
        when the old keystore cannot be read (only a keystore the old volume
        shows to be absent counts as nothing to copy), or when Docker cannot
        say which volumes exist: the old volume is left untouched and the
        next start retries. Until the copy is verified, the marker
        (KEY_VOLUME_MIGRATION_PENDING in the data dir) says the new volume
        is not to be trusted: a start that finds it beside both volumes (an
        interrupted copy, or a failed one whose volume could not be removed)
        removes the new volume and copies again. The copy runs
        KEY_VOLUME_MIGRATION_SCRIPT in the stack's pinned tor image
        (ensure_tor_image), no network, DAC_OVERRIDE only. Both launchers'
        migrate_key_volume() run this (`onionpress migrate-key-volume`,
        Phase 4 #120) and stop the start on False.
        """
        data_dir = self.paths.data_dir
        pending = os.path.join(data_dir, KEY_VOLUME_MIGRATION_PENDING)
        state = self.volume_state(KEY_VOLUME)
        if state == 2:
            self._log(f"ERROR: Docker did not say whether {KEY_VOLUME} exists; not starting")
            return False
        if state == 0 and not os.path.lexists(pending):
            return True
        old_state = self.volume_state(OLD_KEY_VOLUME)
        if old_state == 1:
            # Nothing to copy from: a fresh install, one that never had the
            # old volume, or one whose volumes a key import or restore wiped.
            if state == 0:
                self._log(f"WARNING: a key volume migration did not finish and "
                          f"{OLD_KEY_VOLUME} is gone; keeping {KEY_VOLUME} as it is")
            forget_key_volume_migration(data_dir)
            return True
        if old_state == 2:
            self._log(f"ERROR: Docker did not say whether {OLD_KEY_VOLUME} exists; not starting")
            return False
        if state == 0:
            self._log(f"A key volume migration did not finish; removing the unverified "
                      f"{KEY_VOLUME} to copy again")
            removed = self.docker.run(["volume", "rm", KEY_VOLUME], timeout=60, quiet=True)
            self._log_lines(removed)
            if not removed.ok:
                self._log(f"ERROR: could not remove the unverified {KEY_VOLUME}; not "
                          "starting (the old key volume is untouched)")
                return False
        image = self.ensure_tor_image()
        if image is None:
            self._log("ERROR: no tor image to migrate the key volume with; not starting "
                      "(the old key volume is untouched)")
            return False
        self._log(f"Migrating the onion service key volume: {OLD_KEY_VOLUME} -> {KEY_VOLUME}")
        try:
            with open(pending, "w", encoding="utf-8"):
                pass
        except OSError as e:
            self._log(f"ERROR: could not write {pending}; not starting (the old key "
                      f"volume is untouched): {e}")
            return False
        created = self.docker.run(["volume", "create", KEY_VOLUME], timeout=60, quiet=True)
        self._log_lines(created)
        if not created.ok:
            self._log(f"ERROR: could not create {KEY_VOLUME}; not starting (the old key "
                      "volume is untouched)")
            return False
        copied = self.docker.run(
            ["run", "--rm",
             "--network", "none",
             "--cap-drop", "ALL",
             "--cap-add", "DAC_OVERRIDE",
             "--security-opt", "no-new-privileges:true",
             "-v", f"{OLD_KEY_VOLUME}:/old:ro",
             "-v", f"{KEY_VOLUME}:/new",
             "--entrypoint", "sh",
             image, "-c", KEY_VOLUME_MIGRATION_SCRIPT, "migrate-key-volume", "/old", "/new"],
            timeout=600, quiet=True)
        self._log_lines(copied)
        if not copied.ok:
            removed = self.docker.run(["volume", "rm", KEY_VOLUME], timeout=60, quiet=True)
            self._log_lines(removed)
            if removed.ok:
                forget_key_volume_migration(data_dir)
            self._log("ERROR: key volume migration failed; not starting so the old key "
                      f"volume stays intact (see {self.paths.log_file})")
            return False
        forget_key_volume_migration(data_dir)
        self._log(f"Key volume migrated; {OLD_KEY_VOLUME} is kept as a fallback until "
                  "uninstall sweeps it")
        return True

    # -- Core lifecycle --

    def run_compose_to(self, args: list, *, out: str, include_cloudflare: bool = False) -> int:
        """`docker compose ARGS` with the stack's environment, its output
        where the launchers sent it: *out* "log" appends it to the log
        file (the Mac's `>> "$LOG_FILE" 2>&1`), "stdout" streams it
        (Linux's `| tee -a "$LOG_FILE"`: the journal sees a first pull's
        progress as it happens). The exit status (Docker.run_to)."""
        files = []
        for f in self._compose_files(include_cloudflare) or []:
            files += ["-f", f]
        return self.docker.run_to(["compose", *files, *args],
                                  log_file=self.paths.log_file if out == "log" else None,
                                  extra_env=self._build_env())

    def start_core(self, retries: int = 3, *, launcher: str | None = None,
                   egress: Callable[[], object] | None = None) -> bool:
        """Start core services (WordPress, DB, OnionHeaven, autoheal). Not
        Tor yet. Before anything starts: the egress rules (issue #144),
        retiring an older OnionPress's containers, the Tor network's move.

        Returns True on success. Retries on failure (5 s apart).

        *launcher* ("mac" or "linux") is a launcher's start
        (onionpress start-core, #442.6): its words, compose's output where
        that launcher put it, and *egress* in place of this manager's (the
        launcher's verdict lines and START SUMMARY name). WordPress's
        published address on Linux comes in the environment, as the
        launcher's `compose up` always had it (issue #464).
        """
        if launcher is None:
            self._log("Starting core services...")

        # WordPress and MariaDB reach nothing outside Docker (issue #144):
        # the rules go on the Docker host before their network has a
        # container on it. Best effort, as in the launchers; ensure() logs.
        (egress or self._egress)()

        # Nothing from before the OnionHeaven data volume may run beside the
        # containers about to start (the data move must not race them).
        try:
            self.retire_old_onionheaven_containers()
        except Exception as e:
            self._log(f"Could not check for containers of an older OnionPress: {e}")

        # The Tor side's network on its fixed bridge, for its rules (once).
        try:
            self.migrate_tor_network()
        except Exception as e:
            self._log(f"Could not check onionpress-network's bridge: {e}")

        up = ["up", "-d"] + CORE_SERVICES
        shown = "docker compose " + " ".join(up)
        env = self._build_env()
        for attempt in range(1, retries + 1):
            if launcher is None:
                result = self.docker.compose(up, compose_files=self._compose_files(),
                                             timeout=120, extra_env=env)
                ok, why = result.ok, result.stderr.rstrip()
            else:
                ok = self.run_compose_to(up, out="log" if launcher == "mac" else "stdout") == 0
            if ok:
                self._log("Core services started" if launcher is None
                          else "WordPress, DB, and Tor client containers starting")
                return True
            if launcher is None:
                self._log(f"Start attempt {attempt}/{retries} failed: {why}")
            elif launcher == "mac":
                self._log(f"WARNING: '{shown}' failed (attempt {attempt}/{retries}). "
                          f"Check {self.paths.log_file} for details.")
            else:
                self._log(f"WARNING: docker compose up failed (attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(5)

        if launcher is None:
            self._log("ERROR: Failed to start core services after retries")
        elif launcher == "mac":
            self._log(f"ERROR: '{shown}' failed after {retries} attempts.")
        else:
            self._log(f"ERROR: docker compose up failed after {retries} attempts")
        return False

    def start_tor(self) -> bool:
        """Start the Tor service, never before its key volume is in place
        (key_volume_not_ready)."""
        why = self.key_volume_not_ready()
        if why:
            self._log(f"ERROR: not starting Tor: {why}")
            return False
        self._log("Starting Tor service...")
        result = self.docker.compose(
            ["up", "-d", "tor"],
            compose_files=self._compose_files(),
            timeout=120,
            extra_env=self._build_env(),
        )
        if result.ok:
            self._log("Tor service started")
        else:
            self._log(f"Tor start failed: {result.stderr.rstrip()}")
        return result.ok

    def stop(self, include_onionheaven: bool = True) -> bool:
        """Stop all containers.

        Args:
            include_onionheaven: If True, also stop farm workers.
        """
        if include_onionheaven:
            self.stop_farm()

        self._log("Stopping all containers...")
        args = ["down"]

        cf_token = read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN", "")
        compose_files = self._compose_files(include_cloudflare=bool(cf_token))

        result = self.docker.compose(args, compose_files=compose_files, timeout=120, extra_env=self._build_env())
        if result.ok:
            self._log("All containers stopped")
        else:
            self._log(f"Stop warning: {result.stderr.rstrip()}")
        return result.ok

    # -- Readiness checks --

    def wait_for_wordpress(self, timeout: int = 60, interval: int = 2) -> bool:
        """Wait for WordPress to respond to HTTP requests.

        Returns True if WordPress becomes ready within timeout.
        """
        self._log("Waiting for WordPress...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.docker.exec(
                "onionpress-wordpress",
                ["curl", "-sf", "--max-time", "3", "http://localhost:80/"],
                timeout=10,
            )
            if result.ok:
                self._log("WordPress is ready")
                return True
            time.sleep(interval)

        self._log("WordPress did not become ready in time")
        return False

    def wp_is_installed(self) -> bool:
        """Check if WordPress core is installed."""
        result = self.docker.wp("core", "is-installed", timeout=15)
        return result.ok

    def wait_for_tor(self, timeout: int = 120, interval: int = 2) -> bool:
        """Wait for Tor to bootstrap (100% or sufficiently bootstrapped).

        Returns True if Tor bootstraps within timeout.
        """
        self._log("Waiting for Tor to bootstrap...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.docker.compose(
                ["logs", "--tail", "100", "tor"],
                compose_files=self._compose_files(),
                timeout=15,
                extra_env=self._build_env(),
            )
            if result.ok:
                output = result.stdout
                if "Bootstrapped 100%" in output or "Sufficiently bootstrapped" in output:
                    self._log("Tor bootstrapped")
                    return True
            time.sleep(interval)

        self._log("Tor did not bootstrap in time")
        return False

    def get_onion_address(self) -> str:
        """Fetch the current onion address from the Tor container, or ""
        when there is none or the file holds anything but a well-formed v3
        address (it names a backup file and goes into backup metadata)."""
        return onion_hostname.read_served_address(self.docker, quiet=False)

    def get_status(self) -> ContainerStatus:
        """Get the current container stack status."""
        status = ContainerStatus()
        status.services = self.docker.compose_ps(
            compose_files=self._compose_files(),
            extra_env=self._build_env(),
        )
        status.onion_address = self.get_onion_address()
        status.wp_ready = self.docker.container_running("onionpress-wordpress")
        status.tor_bootstrapped = self._is_tor_bootstrapped()
        return status

    def _is_tor_bootstrapped(self) -> bool:
        """Quick check if Tor is bootstrapped from recent logs."""
        result = self.docker.compose(
            ["logs", "--tail", "50", "tor"],
            compose_files=self._compose_files(),
            timeout=10,
            extra_env=self._build_env(),
        )
        if not result.ok:
            return False
        output = result.stdout
        return "Bootstrapped 100%" in output or "Sufficiently bootstrapped" in output

    def is_stack_running(self) -> bool:
        """Quick check: are all core OnionPress containers running?"""
        result = self.docker.run(
            ["ps", "--filter", "name=onionpress-", "--format", "{{.State}}"],
            timeout=10,
        )
        if not result.ok or not result.output.strip():
            return False
        states = [s.strip().lower() for s in result.output.splitlines() if s.strip()]
        return len(states) > 0 and all(s == "running" for s in states)

    # -- OnionHeaven farm --

    def start_farm_worker(self, idx: int, image: str | None = None) -> bool:
        """Start a single OnionHeaven takeover worker container.

        Args:
            idx: Worker index (0, 1, 2, ...).
            image: Docker image to use. Resolved per call when omitted, so a
                locally built image set in the environment or in
                ~/.onionpress/config is honoured — a module-level default
                would have frozen the value at import time.

        Returns True on success.
        """
        if image is None:
            image = onionheaven_image(self.paths.config_file)
        name = f"onionheaven-takeover-{idx}"
        self._log(f"Starting farm worker {name}...")

        max_services = read_value(
            self.paths.config_file, "ONIONHEAVEN_MAX_SERVICES",
            DEFAULTS["ONIONHEAVEN_MAX_SERVICES"],
        )

        result = self.docker.run([
            "run", "-d",
            "--name", name,
            "--network", "onionpress-network",
            "--ulimit", "nofile=10000:10000",
            "--log-opt", "max-size=10m",
            "--log-opt", "max-file=3",
            # The same hardening and capabilities as _spawn_worker: su to
            # debian-tor, the control cookie in its 0700 /var/lib/tor.
            "--cap-drop", "ALL",
            "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "SETGID",
            "--cap-add", "SETUID",
            "--security-opt", "no-new-privileges:true",
            "-e", f"TZ={os.environ.get('TZ', 'UTC')}",
            "-e", "TAKEOVER_WORKER=1",
            "-e", f"CONTAINER_NAME={name}",
            "-e", f"MAX_TAKEOVER_SERVICES={max_services}",
            # The OnionHeaven data volume only, as onionheaven_common's
            # _spawn_worker does — never WordPress's shared volume.
            "-v", "onionpress-onionheaven-data:/var/lib/onionheaven",
            "--restart", "unless-stopped",
            image,
        ], timeout=60)

        if result.ok:
            self._log(f"Farm worker {name} started")
        else:
            self._log(f"Farm worker {name} failed: {result.stderr.rstrip()}")
        return result.ok

    def stop_farm(self) -> None:
        """Stop and remove all OnionHeaven farm worker containers."""
        self._log("Stopping farm workers...")

        # List farm containers
        result = self.docker.run(
            ["ps", "-a", "--format", "{{.Names}}"],
            timeout=15,
        )
        if not result.ok:
            return

        workers = [
            name.strip() for name in result.output.splitlines()
            if name.strip().startswith(("onionheaven-takeover-", "onionheaven-poll-"))
        ]

        if not workers:
            return

        # Stop and remove in parallel, a second's grace each: their onion
        # services are ephemeral (ADD_ONION).
        def _stop_worker(name: str):
            self.docker.run(["stop", "-t", "1", name], timeout=30)
            self.docker.run(["rm", name], timeout=15)

        with ThreadPoolExecutor(max_workers=min(len(workers), 10)) as pool:
            futures = {pool.submit(_stop_worker, w): w for w in workers}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    self._log(f"Stopped {name}")
                except Exception as e:
                    self._log(f"Warning stopping {name}: {e}")

    def retire_old_onionheaven_containers(self) -> list[str]:
        """Remove every OnionHeaven worker and Tor-side container that does
        not mount the hub data volume, all at once; returns their names.

        An OnionPress from before the volume started them, and they still
        read and write the hub's registry and keys on
        onionpress-persistent-data. The data move must not race them, and
        nothing may use that copy after it. Compose recreates onionpress-tor
        and onionheaven, the heartbeat starts new workers. The launchers'
        retire_old_onionheaven_containers runs this (`onionpress
        retire-old-onionheaven-containers`).
        """
        result = self.docker.run(["ps", "-a", "--format", "{{.Names}}"],
                                 timeout=15, quiet=True)
        if not result.ok:
            return []
        stale = []
        for name in result.output.split():
            if not _HUB_CONTAINER_RE.fullmatch(name):
                continue
            inspected = self.docker.run(["inspect", "--format", "{{json .Mounts}}", name],
                                        timeout=15, quiet=True)
            if inspected.ok and not mounts_hub_data(inspected.output):
                stale.append(name)
        if not stale:
            return []
        for name in stale:
            self._log(f"Removing {name}: it does not mount {HUB_DATA_VOLUME} "
                      f"(started by an older OnionPress)")
        with ThreadPoolExecutor(max_workers=min(len(stale), 10)) as pool:
            list(pool.map(lambda n: self.docker.run(["rm", "-f", n], timeout=30), stale))
        return stale

    def _tor_network_containers(self) -> list[str]:
        result = self.docker.run(
            ["network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}",
             wordpress_egress.TOR_NETWORK], timeout=15, quiet=True)
        return result.output.split() if result.ok else []

    def migrate_tor_network(self) -> bool:
        """Recreate an onionpress-network made before its bridge had a fixed
        name (wordpress_egress.TOR_BRIDGE), which the Tor side's rules name;
        True when one was removed for compose to make anew.

        Docker cannot rename a bridge, and compose 2.40, which would recreate
        the network itself, fails the whole start while a takeover worker
        (no compose service) is still on it. Everything OnionPress put there
        is removed, onionheaven first since it starts workers (compose
        recreates its services, the heartbeat its workers); anything else is
        only disconnected. The launchers' migrate_tor_network runs this
        (`onionpress migrate-tor-network`).
        """
        inspected = self.docker.run(
            ["network", "inspect", "--format", "{{json .Options}}",
             wordpress_egress.TOR_NETWORK], timeout=15, quiet=True)
        if not inspected.ok or not tor_network_is_old(inspected.output):
            return False
        self._log(f"Recreating {wordpress_egress.TOR_NETWORK} on its own bridge "
                  f"({wordpress_egress.TOR_BRIDGE}), so the Tor side's rules apply to it")
        names = self._tor_network_containers()
        if "onionheaven" in names:
            self.docker.run(["rm", "-f", "onionheaven"], timeout=30)
            names = self._tor_network_containers()
        ours = [n for n in names if _TOR_NETWORK_CONTAINER_RE.fullmatch(n)]
        for name in names:
            if name not in ours:
                self._log(f"WARNING: {name} is on {wordpress_egress.TOR_NETWORK} but is "
                          "not OnionPress's: disconnecting it")
                self.docker.run(["network", "disconnect", "-f",
                                 wordpress_egress.TOR_NETWORK, name], timeout=30)
        if ours:
            with ThreadPoolExecutor(max_workers=min(len(ours), 10)) as pool:
                list(pool.map(lambda n: self.docker.run(["rm", "-f", n], timeout=30), ours))
        removed = self.docker.run(["network", "rm", wordpress_egress.TOR_NETWORK],
                                  timeout=30)
        if not removed.ok:
            self._log(f"WARNING: could not recreate {wordpress_egress.TOR_NETWORK} on "
                      f"{wordpress_egress.TOR_BRIDGE}: until it is, the Tor-side "
                      f"containers can reach this machine and its network "
                      f"({removed.stderr.strip()})")
        return removed.ok

    def list_farm_workers(self) -> list[str]:
        """List running farm worker container names."""
        result = self.docker.run(
            ["ps", "--format", "{{.Names}}"],
            timeout=15,
        )
        if not result.ok:
            return []
        return [
            name.strip() for name in result.output.splitlines()
            if name.strip().startswith(("onionheaven-takeover-", "onionheaven-poll-"))
        ]
