"""Cache Enabler and the Internet Archive Link Fixer: the pinned releases
put in place, then configured and activated (issue #442.3; until then the
launchers' identical bash functions install_wp_org_plugin,
install_ia_plugin, install_cache_enabler and setup_bundled_plugins).

  install_bundled_plugins   the pinned release of each wanted plugin, in
                            the WordPress container (install_plugin.sh
                            downloads it over Tor and checks it against its
                            pinned version and zip SHA256; a copy of any
                            other version is replaced)
  setup_bundled_plugins     configure-cache-enabler -> activate_plugin.sh
                            … --network -> apply-cache-policy, and
                            configure-ia-plugin -> activate_plugin.sh …
                            --each-site, each step only after the one
                            before it succeeded

A start runs the first before provision-post-install (which installs the
policy mu-plugins the configure-* steps refuse to run without) and the
second after it; `onionpress setup-bundled-plugins`, which
setup_logic.install_fresh_wordpress starts once setup has made the user's
site, runs the second. INSTALL_IA_PLUGIN / INSTALL_CACHE_ENABLER=no leave a
plugin out (an active one stays as it is). Best effort throughout: what
fails is logged and the start goes on. wp-cli and every look into the
plugins folder run as www-data (wp_files), never --allow-root.
"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

from .config import read_value
from .wp_files import WP_CONTAINER, WP_USER, wp_cli_args

PLUGINS_DIR = "/var/www/html/wp-content/plugins"
CACHE_ENABLER = "cache-enabler"
LINK_FIXER = "internet-archive-wayback-machine-link-fixer"
NAMES = {CACHE_ENABLER: "Cache Enabler",
         LINK_FIXER: "Internet Archive Wayback Machine Link Fixer"}
WANTED_KEY = {CACHE_ENABLER: "INSTALL_CACHE_ENABLER", LINK_FIXER: "INSTALL_IA_PLUGIN"}

# A plugin header's Version:, kept to version characters for the log (the
# bash `tr -cd '0-9A-Za-z.-'`).
_VERSION_CHARS = re.compile(r"[^0-9A-Za-z.-]")


@dataclass
class PluginHost:
    """What the steps need: the docker to run in the WordPress container
    with, the config the INSTALL_* keys are read from, the folder holding
    install_plugin.sh / activate_plugin.sh, the log file their output and
    the onionpress steps' goes to, the launcher-format log function, and
    how an onionpress step is run (start.run_cli_step: its exit status)."""
    docker: object
    config_file: str
    scripts_dir: str
    log_file: str
    log: Callable[[str], None]
    run_step: Callable[[list[str]], int]


def plugin_wanted(config_file: str, key: str) -> bool:
    """Unset or empty, or `yes`: wanted. Anything else leaves it out."""
    return read_value(config_file, key, "") in ("", "yes")


def _main_file(slug: str) -> str:
    return f"{slug}/{slug}.php"


def plugin_present(host: PluginHost, slug: str) -> bool:
    """Whether the plugin's main file is in the plugins folder, looked at
    as www-data, whose folder it is."""
    return host.docker.run(
        ["exec", "-u", WP_USER, WP_CONTAINER, "test", "-f",
         f"{PLUGINS_DIR}/{_main_file(slug)}"], timeout=30, quiet=True).ok


def plugin_version(host: PluginHost, slug: str) -> str:
    """The installed plugin's Version: header ("" when none). Compared
    with the pin only."""
    result = host.docker.run(
        ["exec", "-u", WP_USER, WP_CONTAINER, "sed", "-n",
         "s/^[[:space:]*]*Version:[[:space:]]*//p", f"{PLUGINS_DIR}/{_main_file(slug)}"],
        timeout=30, quiet=True)
    first = result.stdout.splitlines()[0] if result.ok and result.stdout else ""
    return _VERSION_CHARS.sub("", first)


def _script(host: PluginHost, name: str) -> str:
    return os.path.join(host.scripts_dir, name)


def _run_script(host: PluginHost, argv: list[str], *, to_log: bool = True,
                timeout: int | None = None) -> subprocess.CompletedProcess | None:
    """One of the plugin scripts, in docker's environment (the app's docker
    first on PATH, DOCKER_HOST): its output to the log, or captured."""
    env = host.docker.env if hasattr(host.docker, "env") else None
    try:
        if not to_log:
            return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout)
        with open(host.log_file, "a", encoding="utf-8") as log:
            return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def install_wp_org_plugin(host: PluginHost, slug: str) -> None:
    """Put the pinned release of a WordPress.org plugin in the WordPress
    container: downloaded over Tor and checked against its pin
    (install_plugin.sh), copied in as www-data (`onionpress put-wp-tree`,
    never docker cp + chown as root). Activation is setup_bundled_plugins'."""
    name = NAMES[slug]
    install = _script(host, "install_plugin.sh")
    if not os.path.isdir(host.scripts_dir) or not os.path.isfile(install):
        host.log(f"Plugin installation scripts not available, skipping {name}")
        return
    pinned = _run_script(host, [install, "--pinned-version", slug], to_log=False, timeout=60)
    if pinned is None or pinned.returncode != 0:
        host.log(f"No pinned release of {name} — not installing it")
        return
    pinned_version = pinned.stdout.strip()

    installed = plugin_version(host, slug)
    if installed == pinned_version:
        host.log(f"{name} {installed} already installed")
        return
    if installed:
        host.log(f"Replacing {name} {installed} with the pinned release {pinned_version}...")
    else:
        host.log(f"Installing {name} {pinned_version}...")

    temp_dir = tempfile.mkdtemp()
    try:
        fetched = _run_script(host, [install, slug, temp_dir])
        if fetched is not None and fetched.returncode == 0:
            if host.run_step(["put-wp-tree", "--src", os.path.join(temp_dir, slug),
                              "--dest", f"{PLUGINS_DIR}/{slug}"]) == 0:
                host.log(f"✓ {name} {pinned_version} installed with verified checksum")
            else:
                host.log(f"Failed to copy {name} to container")
        else:
            host.log(f"Failed to download and verify {name} - check log for details")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def install_bundled_plugins(host: PluginHost) -> None:
    """The Link Fixer, then Cache Enabler, each while wanted."""
    if plugin_wanted(host.config_file, WANTED_KEY[LINK_FIXER]):
        install_wp_org_plugin(host, LINK_FIXER)
    else:
        host.log("Internet Archive plugin installation disabled in config")
    if plugin_wanted(host.config_file, WANTED_KEY[CACHE_ENABLER]):
        install_wp_org_plugin(host, CACHE_ENABLER)
    else:
        host.log("Cache Enabler plugin installation disabled in config")


def _activate(host: PluginHost, slug: str, scope: str) -> bool:
    done = _run_script(host, [_script(host, "activate_plugin.sh"), WP_CONTAINER,
                              _main_file(slug), scope])
    return done is not None and done.returncode == 0


def wp_is_installed(docker) -> bool:
    return docker.run(wp_cli_args("core", "is-installed"), timeout=30, quiet=True).ok


def setup_bundled_plugins(host: PluginHost) -> None:
    """Configure, then activate, the plugins install_bundled_plugins put in
    place (each while still wanted), and for Cache Enabler then check its
    settings against OnionPress's cache policy. Configured first: Cache
    Enabler validates its settings (through onionpress-cache-policy.php)
    and writes its files when it is activated, and the Link Fixer shows its
    setup wizard unless it is completed by then. Cache Enabler is
    network-activated (one drop-in and page cache for the network; the
    mu-plugin gives every site, also one made later, its settings); the
    Link Fixer site by site, as it creates its link table only in the site
    it is activated on. The configure-* steps refuse, and nothing is
    activated, without the policy mu-plugins, so this runs after
    provision-post-install. Idempotent; activation is repeated at every
    start."""
    if not wp_is_installed(host.docker):
        host.log("WordPress not installed yet — Cache Enabler and the Link Fixer "
                 "are set up after setup")
        return

    if (plugin_wanted(host.config_file, WANTED_KEY[CACHE_ENABLER])
            and plugin_present(host, CACHE_ENABLER)):
        if (host.run_step(["configure-cache-enabler"]) == 0
                and _activate(host, CACHE_ENABLER, "--network")
                and host.run_step(["apply-cache-policy"]) == 0):
            host.log("✓ Cache Enabler active, cache policy applied")
        else:
            host.log(f"WARNING: Cache Enabler not set up — see {host.log_file}")

    if (plugin_wanted(host.config_file, WANTED_KEY[LINK_FIXER])
            and plugin_present(host, LINK_FIXER)):
        if (host.run_step(["configure-ia-plugin"]) == 0
                and _activate(host, LINK_FIXER, "--each-site")):
            host.log("✓ Internet Archive Wayback Machine Link Fixer active on every site")
        else:
            host.log(f"WARNING: Internet Archive Link Fixer not set up — see {host.log_file}")
