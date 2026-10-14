"""WordPress multisite + theme/plugin post-install steps.

Ported from the duplicated bash implementations in app/MacOS/onionpress
and linux/onionpress. The `provision-post-install` subcommand on both
platforms now delegates to this module so the two stay in sync
automatically.

Step order matters — see `provision_post_install` for the rationale.
Each individual step is also exposed as a top-level function so callers
that already have part of the state (e.g. `start_containers` after a
restore) can invoke just the steps they need.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Optional

from . import wp_files


# Files installed into the WordPress container by install_multisite_domain_map.
# Kept here (not in the launcher) so Mac and Linux see the same set.
MU_PLUGINS = (
    "onionpress-domain-map.php",
    "onionpress-wayback-archive.php",
    "onionpress-login-fix.php",
    "onionpress-auto-login.php",
    "onionpress-favicon.php",
    "onionpress-status.php",
    "onionpress-settings.php",
    "onionpress-offline-publish.php",
    "onionpress-tor-proxy.php",
    "onionpress-name-sync.php",
    "onionpress-directory.php",
    "onionpress-root-redirect.php",
    "onionpress-user-path.php",
    "onionpress-avatar.php",
    "onionpress-blogroll.php",
    "onionpress-status-hint.php",
    "onionpress-onboarding.php",
    "onionpress-social-archive.php",
    "onionpress-social-archive-twitter.php",
    "onionpress-social-archive-mastodon.php",
    "onionpress-social-archive-bluesky.php",
    "onionpress-image-fallback.php",
    # What Cache Enabler and the Link Fixer may do here
    # (configure_cache_enabler and configure_ia_plugin refuse to set them
    # up without these).
    "onionpress-cache-policy.php",
    "onionpress-link-fixer-policy.php",
)

# Icon assets co-located with the mu-plugins.
MU_PLUGIN_ASSETS = (
    "onionpress-sidebar-icon.png",
    "onionpress-follow-icon.png",
    "onionpress-avatar-default.png",
)

# Multisite constants written to wp-config.php in ensure_multisite. The
# values are wp-cli `--raw` literals (already-quoted strings stay quoted).
MULTISITE_CONSTANTS = (
    ("MULTISITE", "true"),
    ("SUBDOMAIN_INSTALL", "false"),
    ("DOMAIN_CURRENT_SITE", "'localhost'"),
    ("PATH_CURRENT_SITE", "'/'"),
    ("SITE_ID_CURRENT_SITE", "1"),
    ("BLOG_ID_CURRENT_SITE", "1"),
    ("SUNRISE", "true"),
)

# Apache .htaccess rules for multisite — the same content used to live in
# two bash heredocs (one in ensure_multisite, one in install_multisite_
# domain_map). The latter is the canonical version (includes the privacy
# Referrer-Policy header); ensure_multisite was a near-duplicate left over
# from when the two paths were separate. They're consolidated here.
#
# The My Creations block refuses anything PHP could run under
# wp-content/creations/ (the bind-mounted host folder), on every site path.
# The WordPress image's onionpress-creations.conf does the same and more;
# this copy reaches existing installs at their next start, before that image
# does. The extension list is shared with that conf and with the upload check
# in onionpress-creations.php (tests/test_creations_no_php.py). A second rule
# refuses hidden paths there (tests/test_creations_hidden_files.py).
#
# The uploads block refuses the same extensions under wp-content/uploads/
# (every site's media), as the image's onionpress-uploads.conf does
# (tests/test_uploads_no_php.py).
HTACCESS_BODY = """\
# Privacy: prevent onion address leaking in Referer headers
<IfModule mod_headers.c>
Header set Referrer-Policy "no-referrer"
</IfModule>

# My Creations is a publishing folder: serve its files, never run them
RewriteEngine On
RewriteCond %{REQUEST_URI} /wp-content/creations/ [NC]
RewriteCond %{REQUEST_URI} \\.(php[0-9]*|pht(ml)?|phar|phps|cgi|shtml)(/|$) [NC]
RewriteRule ^ - [F]
# ...and never its hidden files and folders (.git/, .DS_Store, .thumbs/)
RewriteCond %{REQUEST_URI} /wp-content/creations/(.*/)?\\. [NC]
RewriteRule ^ - [F]

# Blog importer snapshots (drafts, private posts, author emails) went to
# blog-archives/ through 2.5.2. They live in the hidden .blog-archives/ now;
# until the importer has moved the old folder there, refuse it as well.
RewriteCond %{REQUEST_URI} /wp-content/creations/blog-archives(/|$) [NC]
RewriteRule ^ - [F]

# Uploads are media: serve them, never run them
RewriteCond %{REQUEST_URI} /wp-content/uploads/ [NC]
RewriteCond %{REQUEST_URI} \\.(php[0-9]*|pht(ml)?|phar|phps|cgi|shtml)(/|$) [NC]
RewriteRule ^ - [F]

# BEGIN WordPress Multisite
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\\.php$ - [L]

# add a trailing slash to /wp-admin
RewriteRule ^([_0-9a-zA-Z.-]+/)?wp-admin$ $1wp-admin/ [R=301,L]

RewriteCond %{REQUEST_FILENAME} -f [OR]
RewriteCond %{REQUEST_FILENAME} -d
RewriteRule ^ - [L]
RewriteRule ^([_0-9a-zA-Z.-]+/)?(wp-(content|admin|includes).*) $2 [L]
RewriteRule ^([_0-9a-zA-Z.-]+/)?(.*\\.php)$ $2 [L]
RewriteRule . index.php [L]
# END WordPress Multisite
"""


def _wp(
    *args,
    docker_bin: str = "docker",
    timeout: int = 60,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run wp-cli inside the onionpress-wordpress container, as www-data
    (wp_files.wp_cli_args). With *input*, `docker exec -i` and *input* on
    wp-cli's stdin."""
    return subprocess.run(
        [docker_bin] + wp_files.wp_cli_args(*args, stdin=input is not None),
        input=input,
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _exec_sh(
    command: str,
    *,
    docker_bin: str = "docker",
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a shell command inside the WordPress container."""
    return subprocess.run(
        [docker_bin, "exec", "onionpress-wordpress", "sh", "-c", command],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


# Everything below is handed to the container as www-data (wp_files), never
# by root's docker cp + chown: www-data owns the web root and wp-content, and
# root followed a link planted at the destination (sunrise.php ->
# /usr/local/bin/docker-entrypoint.sh) both to write the file and to chown
# its target to www-data.

def _put_data(
    dest: str,
    data,
    *,
    docker_bin: str = "docker",
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Write *data* (str or bytes) to *dest* in the WordPress container, as
    www-data, mode 644 (Apache and PHP run as www-data). stderr is text."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="surrogateescape")
    return subprocess.run(
        [docker_bin] + wp_files.put_file_args(dest, "644"),
        input=data, capture_output=True, text=True,
        encoding="utf-8", errors="surrogateescape", timeout=timeout,
    )


def _put_file(
    src: str,
    dest: str,
    *,
    docker_bin: str = "docker",
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Write the host file *src* to *dest* in the WordPress container, as
    www-data, mode 644."""
    with open(src, "rb") as f:
        data = f.read()
    return _put_data(dest, data, docker_bin=docker_bin, timeout=timeout)


def _own(
    path: str,
    *,
    recursive: bool = False,
    docker_bin: str = "docker",
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Give *path* (and, *recursive*, all below it) to www-data, as root,
    following no link (wp_files.own_args)."""
    return subprocess.run(
        [docker_bin] + wp_files.own_args(path, recursive),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _put_tree(
    src: str,
    dest: str,
    *,
    docker_bin: str = "docker",
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Replace the folder *dest* in the WordPress container with the host
    folder *src*, as www-data."""
    return wp_files.put_tree(src, dest, docker_bin=docker_bin, timeout=timeout)


def _exec_www_data(
    command: str,
    *,
    docker_bin: str = "docker",
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a shell command inside the WordPress container as www-data."""
    return subprocess.run(
        [docker_bin] + wp_files.exec_args("sh", "-c", command),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def wp_is_installed(docker_bin: str = "docker") -> bool:
    """True iff `wp core is-installed` succeeds."""
    return _wp("core", "is-installed", docker_bin=docker_bin).returncode == 0


def _noop_log(_msg: str) -> None:
    pass


def ensure_multisite(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Convert single-site WordPress to multisite if it isn't already.
    No-ops if WP is not yet installed or is already multisite. Returns
    True on success (or when the work was already done).
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        log("WordPress not installed yet -- skipping multisite check")
        return True

    already = _wp(
        "core", "is-installed", "--network", docker_bin=docker_bin)
    if already.returncode == 0:
        log("WordPress multisite already active")
        return True

    log("Converting single-site WordPress to multisite...")
    r = _wp(
        "core", "multisite-convert",
        "--url=http://localhost",
        docker_bin=docker_bin, timeout=120,
    )
    if r.returncode != 0:
        log(f"WARNING: wp core multisite-convert failed: {r.stderr.strip()[:200]}")

    # Write the multisite constants into wp-config.php. Errors here are
    # logged but don't fail the function — the next start will retry.
    for name, value in MULTISITE_CONSTANTS:
        _wp(
            "config", "set", name, value,
            "--raw", "--type=constant",
            docker_bin=docker_bin,
        )

    log("WordPress multisite conversion complete")
    return True


def install_multisite_domain_map(
    *,
    plugins_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Copy sunrise.php, write the multisite .htaccess, and install all
    the bundled mu-plugins + their icon assets. `plugins_dir` is the
    on-disk path that contains the .php files and PNGs (Mac:
    `$RESOURCES_DIR/plugins`; Linux: `/opt/onionpress/plugins`).
    """
    log = log_func or _noop_log

    # Gate on WordPress being installed — sunrise.php queries wp_site on
    # every WP load, and SUNRISE=true tells WP to load it. If we drop
    # sunrise.php before `wp core install` has created the multisite
    # tables (which only happens AFTER ensure_multisite), the next
    # `wp core install` itself errors out trying to load sunrise.php,
    # leaving an unrecoverable install (#284).
    if not wp_is_installed(docker_bin):
        log("WordPress not installed yet -- skipping sunrise.php install")
        return False

    # 1. sunrise.php — must run before SUNRISE constant takes effect.
    sunrise_src = os.path.join(plugins_dir, "onionpress-sunrise.php")
    if os.path.isfile(sunrise_src):
        cp = _put_file(
            sunrise_src,
            "/var/www/html/wp-content/sunrise.php",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            # Ensure SUNRISE constant is in wp-config.php (required for
            # sunrise.php to load).
            _wp(
                "config", "set", "SUNRISE", "true",
                "--raw", "--type=constant",
                docker_bin=docker_bin,
            )
            log("sunrise.php installed")
        else:
            log("WARNING: Failed to copy sunrise.php")

    # 2. .htaccess (body: HTACCESS_BODY at module scope).
    r = _put_data("/var/www/html/.htaccess", HTACCESS_BODY,
                  docker_bin=docker_bin)
    if r.returncode == 0:
        log(".htaccess multisite rewrite rules installed")
    else:
        log(f"WARNING: Failed to write .htaccess: {r.stderr.strip()[:200]}")

    # 3. mu-plugins directory + each plugin. Root's mkdir made the folder
    # root's in older versions, where www-data could not add a file.
    _own("/var/www/html/wp-content/mu-plugins", docker_bin=docker_bin)
    _exec_www_data(
        "mkdir -p /var/www/html/wp-content/mu-plugins",
        docker_bin=docker_bin,
    )
    for plugin in MU_PLUGINS:
        src = os.path.join(plugins_dir, plugin)
        if not os.path.isfile(src):
            continue
        cp = _put_file(
            src,
            f"/var/www/html/wp-content/mu-plugins/{plugin}",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            log(f"{plugin} mu-plugin installed")
        else:
            log(f"WARNING: Failed to copy {plugin}")

    # 4. Icon assets used by onionpress-settings.php, onionpress-login-fix.php,
    # onionpress-avatar.php (default avatar image).
    for asset in MU_PLUGIN_ASSETS:
        src = os.path.join(plugins_dir, asset)
        if not os.path.isfile(src):
            continue
        cp = _put_file(
            src,
            f"/var/www/html/wp-content/mu-plugins/{asset}",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            log(f"{asset} installed")
        else:
            log(f"WARNING: Failed to copy {asset}")

    return True


def install_onionpress_theme(
    *,
    themes_dir: str,
    plugins_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Install the OnionPress theme + the hit-counter and creations
    plugins. Activates the theme when the current active theme is a
    twentytwenty* default; leaves user-chosen themes alone.
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True

    # 1. Theme. _put_tree replaces the folder whole (a copy into an
    # existing folder would land inside it, as themes/onionpress/onionpress/).
    theme_src = os.path.join(themes_dir, "onionpress")
    if os.path.isdir(theme_src):
        cp = _put_tree(
            theme_src,
            "/var/www/html/wp-content/themes/onionpress",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            log("OnionPress theme installed")

            current = _wp(
                "theme", "list",
                "--status=active",
                "--field=name",
                docker_bin=docker_bin,
            )
            current_theme = (current.stdout or "").strip().split("\n")[0]
            if current_theme == "onionpress":
                log("OnionPress theme already active")
            elif current_theme.startswith("twentytwenty") or current_theme == "":
                act = _wp(
                    "theme", "activate", "onionpress",
                    docker_bin=docker_bin,
                )
                if act.returncode == 0:
                    log("OnionPress theme activated")
                else:
                    log(f"WARNING: Failed to activate OnionPress theme: "
                        f"{act.stderr.strip()[:200]}")
            else:
                log(f"User has custom theme '{current_theme}' — not overriding")

            # Network-enable so subsites can use it on multisite.
            _wp(
                "theme", "enable", "onionpress",
                "--network",
                docker_bin=docker_bin,
            )
        else:
            log("WARNING: Failed to copy OnionPress theme")

    # 2. Hit counter + creations plugins, replaced whole the same way.
    for plugin in ("onionpress-hit-counter", "onionpress-creations"):
        src = os.path.join(plugins_dir, plugin)
        if not os.path.isdir(src):
            continue
        cp = _put_tree(
            src,
            f"/var/www/html/wp-content/plugins/{plugin}",
            docker_bin=docker_bin,
        )
        if cp.returncode != 0:
            log(f"WARNING: Failed to copy {plugin}")
            continue
        act = _wp("plugin", "activate", plugin, docker_bin=docker_bin)
        if act.returncode == 0:
            log(f"{plugin} plugin installed and activated")
        else:
            log(f"WARNING: Failed to activate {plugin}")

    return True


def fix_onionpress_permissions(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Ensure the shared `/var/lib/onionpress/` volume is owned by www-data
    so the WordPress process can read state files written by the launcher.
    The `onionheaven/` subtree is excluded — it has its own owner.

    Root here follows no link www-data may have planted: /var/lib/onionpress
    is the volume's mount point (www-data cannot replace it), find names
    each entry with no trailing slash, and chown -R never follows a link,
    the named entry included.
    """
    log = log_func or _noop_log
    log("Fixing permissions for onionpress persistent data directory...")
    r = _exec_sh(
        "chmod 750 /var/lib/onionpress && "
        "find /var/lib/onionpress -maxdepth 0 -exec chown www-data:www-data {} + && "
        "find /var/lib/onionpress -mindepth 1 -maxdepth 1 ! -name onionheaven "
        "-exec chown -R www-data:www-data {} +",
        docker_bin=docker_bin,
    )
    if r.returncode == 0:
        log("Onionpress data directory permissions fixed")
        return True
    log(f"WARNING: Could not fix onionpress data directory permissions: "
        f"{r.stderr.strip()[:200]}")
    return False


def fix_wordpress_uploads_permissions(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Ensure `wp-content/uploads` is owned by www-data so the multisite
    per-blog subtree (`sites/<id>/<YYYY>/<MM>/`) can be created on demand.
    Fresh installs had it created root-owned, which broke media uploads
    with "Unable to create directory" errors, and wp-cli added root's files
    until it ran as www-data. The chown follows no link (wp_files.own).
    """
    log = log_func or _noop_log
    r = _exec_www_data(
        "mkdir -p /var/www/html/wp-content/uploads",
        docker_bin=docker_bin,
    )
    if r.returncode == 0:
        r = _own("/var/www/html/wp-content/uploads", recursive=True,
                 docker_bin=docker_bin)
    if r.returncode == 0:
        log("WordPress uploads directory permissions fixed")
        return True
    log(f"WARNING: Could not fix WordPress uploads directory permissions: "
        f"{r.stderr.strip()[:200]}")
    return False


def write_shared_onion_address(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Copy the tor container's hostname file into the shared volume so WP
    code (theme, mu-plugins, REST endpoints) can answer "what's my .onion?"
    without parsing Host headers — needed when the site is hit via
    localhost. Idempotent; safe to call from multiple places.

    Read from the tor container, written by www-data in the WordPress
    container. Not `cp` as root in the tor container: www-data can plant a
    link at onion_address in the shared volume, and that cp followed it to
    any file in the tor container (its onion service key, say).
    """
    log = log_func or _noop_log
    r = subprocess.run(
        [docker_bin, "exec", "onionpress-tor", "cat", wp_files.TOR_HOSTNAME_FILE],
        capture_output=True, text=True, timeout=15,
    )
    content = wp_files.shared_onion_address(r.stdout) if r.returncode == 0 else None
    if content is None:
        # Caller may not care — this can fail benignly when tor isn't up yet.
        return False
    r = _put_data(wp_files.ONION_ADDRESS_FILE, content,
                  docker_bin=docker_bin, timeout=15)
    if r.returncode == 0:
        log("Onion address written to shared volume")
        return True
    # Caller may not care — this can fail benignly when tor isn't up yet.
    return False


# The two WordPress.org plugins OnionPress installs (app/Resources/scripts/
# install_plugin.sh, from the launchers' install_cache_enabler and
# install_ia_plugin). The launchers' setup_bundled_plugins runs, for each:
# configure_* below, then activate_plugin.sh, then (Cache Enabler)
# apply_cache_policy.
WP_PLUGINS_DIR = "/var/www/html/wp-content/plugins"
WP_MU_PLUGINS_DIR = "/var/www/html/wp-content/mu-plugins"
CACHE_ENABLER_PLUGIN = "cache-enabler/cache-enabler.php"
IA_PLUGIN = ("internet-archive-wayback-machine-link-fixer/"
             "internet-archive-wayback-machine-link-fixer.php")
CACHE_POLICY_MU_PLUGIN = "onionpress-cache-policy.php"
IA_POLICY_MU_PLUGIN = "onionpress-link-fixer-policy.php"

# Cache Enabler's per-host settings files and its stored pages, both
# remade from the database on the next request (apply_cache_policy).
CACHE_ENABLER_SETTINGS_DIR = "/var/www/html/wp-content/settings/cache-enabler"
CACHE_ENABLER_CACHE_DIR = "/var/www/html/wp-content/cache/cache-enabler"

# What OnionPress sets in a site's Link Fixer whose setup wizard has not
# been completed, before the plugin is activated there (activation shows
# the wizard to a site that has not completed it): links checked on
# publish and, a few posts at a time (onionpress-link-fixer-policy.php),
# in existing posts; a link archive.org finds broken points at its Wayback
# copy, over https; this site's own posts are not sent to Save Page Now
# (onionpress-wayback-archive.php archives them).
IA_PLUGIN_OPTIONS = (
    ("iawmlf_process_links", "1"),
    ("iawmlf_fixer_option", "replace_link"),
    ("iawmlf_scan_existing_posts", "1"),
    ("iawmlf_cast_to_https", "1"),
    ("iawmlf_allow_own_content_submissions", "0"),
    ("iawmlf_setup_wizard_completed", "1"),
)
# Links the Link Fixer's front-end script leaves alone (fnmatch patterns,
# its iawmlf_link_exclusions option): onion services and this machine.
# archive.org is never asked about them either (onionpress-link-fixer-
# policy.php); these keep them out of the page's link data as well.
IA_LINK_EXCLUSIONS = (
    "*://*.onion", "*://*.onion/*", "*://*.onion:*", "*://*.onion?*", "*://*.onion#*",
    "*://localhost", "*://localhost/*", "*://localhost:*",
)
# Set in each site once the above is applied there. Raise the version when
# OnionPress's choices change, so sites configured earlier get them.
IA_CONFIG_MARKER = "onionpress_link_fixer_config"
IA_CONFIG_VERSION = "1"


def _in_container(path: str, *, docker_bin: str = "docker") -> bool:
    """Is *path* a file in the WordPress container? Looked at as www-data,
    whose folders these are."""
    return _exec_www_data(f"test -f '{path}'", docker_bin=docker_bin).returncode == 0


def site_urls(*, docker_bin: str = "docker") -> list:
    """Every site's URL on a multisite; [None] on a single site (or when
    the list cannot be had), meaning: run without --url."""
    r = _wp("site", "list", "--field=url", docker_bin=docker_bin)
    urls = [u.strip() for u in (r.stdout or "").splitlines() if u.strip()]
    if r.returncode != 0 or not urls:
        return [None]
    return urls


def configure_ia_plugin(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Configure the Internet Archive Wayback Machine Link Fixer in every
    site, before setup_bundled_plugins activates it there.

    The Link Fixer keeps its options and its link table per site, and the
    user's own posts live in their subsite, not the main site this used to
    configure. A site whose wizard is not completed gets IA_PLUGIN_OPTIONS;
    one whose wizard is (an older OnionPress set it on the main site, or
    the owner went through it) keeps its choices. Every site gets
    IA_LINK_EXCLUSIONS merged into its own. Once per site and
    IA_CONFIG_VERSION.

    True when there is nothing to do (WordPress or the plugin not there
    yet) or every site is configured; False when a site could not be, or
    onionpress-link-fixer-policy.php is missing, so that the launcher does
    not activate the plugin without it.
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True
    if not _in_container(f"{WP_PLUGINS_DIR}/{IA_PLUGIN}", docker_bin=docker_bin):
        return True  # Plugin not installed; nothing to configure
    if not _in_container(f"{WP_MU_PLUGINS_DIR}/{IA_POLICY_MU_PLUGIN}",
                         docker_bin=docker_bin):
        log(f"WARNING: {IA_POLICY_MU_PLUGIN} is not installed — "
            "not setting up the Internet Archive Link Fixer without it")
        return False

    ok = True
    for url in site_urls(docker_bin=docker_bin):
        ok = _configure_ia_site(url, docker_bin=docker_bin, log=log) and ok
    return ok


def _configure_ia_site(url: Optional[str], *, docker_bin: str, log) -> bool:
    where = [f"--url={url}"] if url else []
    site = url or "the site"

    def wp(*args):
        return _wp(*args, *where, docker_bin=docker_bin)

    done = wp("option", "get", IA_CONFIG_MARKER)
    if done.returncode == 0 and done.stdout.strip() == IA_CONFIG_VERSION:
        return True

    ok = True
    wizard = wp("option", "get", "iawmlf_setup_wizard_completed")
    if wizard.returncode == 0 and wizard.stdout.strip() == "1":
        log(f"Internet Archive Link Fixer already set up on {site} — keeping its settings")
    else:
        log(f"Configuring the Internet Archive Link Fixer on {site}...")
        for name, value in IA_PLUGIN_OPTIONS:
            r = wp("option", "update", name, value)
            if r.returncode != 0:
                ok = False
                log(f"WARNING: Failed to set {name} on {site}: {r.stderr.strip()[:200]}")

    current = wp("option", "get", "iawmlf_link_exclusions", "--format=json")
    try:
        existing = json.loads(current.stdout) if current.returncode == 0 else []
    except ValueError:
        existing = []
    if isinstance(existing, dict):
        existing = list(existing.values())
    if not isinstance(existing, list):
        existing = []
    existing = [e for e in existing if isinstance(e, str)]
    merged = existing + [p for p in IA_LINK_EXCLUSIONS if p not in existing]
    r = wp("option", "update", "iawmlf_link_exclusions", json.dumps(merged),
           "--format=json")
    if r.returncode != 0:
        ok = False
        log(f"WARNING: Failed to set the Link Fixer's exclusions on {site}: "
            f"{r.stderr.strip()[:200]}")

    if ok:
        wp("option", "update", IA_CONFIG_MARKER, IA_CONFIG_VERSION)
        log(f"Internet Archive Link Fixer configured on {site}")
    return ok


def configure_cache_enabler(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Prepare Cache Enabler's activation (setup_bundled_plugins runs this
    first). Its settings come from onionpress-cache-policy.php, which Cache
    Enabler consults when it validates them on activation; without that
    mu-plugin it would cache with its own defaults (any host, utm_* query
    strings served the bare page), so this refuses (False) and the launcher
    does not activate it. Activation writes its settings file through
    WP_Filesystem, which under wp-cli (the wp-cli phar is root's, wp-cli
    runs as www-data) falls back to FTP and fails unless FS_METHOD is
    'direct'; set only when it is not, so wp-config.php is not rewritten
    at every start. True when there is nothing to do.
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True
    if not _in_container(f"{WP_PLUGINS_DIR}/{CACHE_ENABLER_PLUGIN}",
                         docker_bin=docker_bin):
        return True
    if not _in_container(f"{WP_MU_PLUGINS_DIR}/{CACHE_POLICY_MU_PLUGIN}",
                         docker_bin=docker_bin):
        log(f"WARNING: {CACHE_POLICY_MU_PLUGIN} is not installed — "
            "not setting up Cache Enabler without it")
        return False
    current = _wp("config", "get", "FS_METHOD", docker_bin=docker_bin)
    if current.returncode == 0 and current.stdout.strip() == "direct":
        return True
    r = _wp("config", "set", "FS_METHOD", "direct", "--type=constant",
            docker_bin=docker_bin)
    if r.returncode != 0:
        log(f"WARNING: Failed to set FS_METHOD=direct: {r.stderr.strip()[:200]}")
        return False
    log("FS_METHOD=direct set for Cache Enabler")
    return True


def apply_cache_policy(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """After Cache Enabler is activated: validate every site's settings
    again through onionpress-cache-policy.php (`wp onionpress-cache-policy
    apply`), then remove its settings files and stored pages, as www-data
    (they are www-data's; rm follows no link). Both are snapshots of
    whatever settings were in force when they were written; the next
    request remakes them from the settings just checked. False when the
    command fails; the files are left as they are then.
    """
    log = log_func or _noop_log
    r = _wp("onionpress-cache-policy", "apply", docker_bin=docker_bin, timeout=120)
    if r.returncode != 0:
        log(f"WARNING: Cache Enabler settings not checked: "
            f"{(r.stderr or r.stdout).strip()[:200]}")
        return False
    r = _exec_www_data(
        f"rm -f {CACHE_ENABLER_SETTINGS_DIR}/*.php && rm -rf {CACHE_ENABLER_CACHE_DIR}",
        docker_bin=docker_bin, timeout=300,
    )
    if r.returncode != 0:
        log(f"WARNING: Cache Enabler's stored pages not cleared: {r.stderr.strip()[:200]}")
        return False
    log("Cache Enabler: settings checked on every site, stored pages cleared")
    return True


def deactivate_wp_statistics(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Remove the WP-Statistics plugin if it's present. It phoned home
    to connect.wp-statistics.com even with telemetry disabled — a hard
    clearnet leak on an onion site. OnionPress ships its own hit counter
    (onionpress-hit-counter) that doesn't talk to anyone.
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True
    plugin_file = "/var/www/html/wp-content/plugins/wp-statistics/wp-statistics.php"
    if _exec_sh(f"test -f {plugin_file}",
                docker_bin=docker_bin).returncode != 0:
        return True  # Not present; nothing to do

    log("Removing WP-Statistics plugin (clearnet leak — replaced by built-in hit counter)...")
    _wp("plugin", "deactivate", "wp-statistics", "--network", docker_bin=docker_bin)
    _wp("plugin", "delete", "wp-statistics", docker_bin=docker_bin)
    log("WP-Statistics plugin removed")
    return True


# Shared archive.org credentials baked into OnionPress. Used by
# ensure_archive_s3_keys to fetch per-instance S3 keys for the Wayback
# sweep. Note: these creds belong to a low-value "upload quota" account
# whose only purpose is allowing OnionPress installs to submit to SPN.
# Compromise impact is bounded by archive.org's rate limit on that
# account; rotating it is the maintainer's call, not a user concern.
_ARCHIVE_LOGIN_EMAIL = "onionpress@internetarchive.eu"
_ARCHIVE_LOGIN_PASS = "aat:aep7"


# archive.org's onion service. Every archive.org login goes here, through
# the onionheaven container's Tor (linux/onionpress's fetch_archive_s3_keys
# names the same address; a test holds them equal).
_ARCHIVE_ONION = "https://archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"


def fetch_archive_s3_keys(
    email: str,
    password: str,
    *,
    docker_bin: str = "docker",
    env: Optional[dict] = None,
    log_func: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Log in to archive.org and return its S3 keys as
    {"access": ..., "secret": ...}, or None (logged) on any failure.

    The one archive.org login on the Mac: first-run setup (onion_proxy)
    and the per-launch reconcile (ensure_archive_s3_keys) both come here,
    so they cannot drift apart again (issue #322: setup went to clearnet
    archive.org and could fail where the reconcile succeeded).

    POSTs to archive.org's onion (NOT clearnet), from inside the
    onionheaven container through its Tor SOCKS port: clearnet archive.org
    via Tor exit nodes is aggressively rate-limited / blocked by their
    Cloudflare layer (every exit we tried 2026-05-25 returned HTTP 000),
    and from the host the login would tie the publisher's IP address to
    the account. -k accepts the onion's self-issued certificate; the
    onion address itself authenticates it.

    The login form and the session cookie go to curl on stdin
    (docker exec -i, --data-binary @- / -H @-): argv is visible to every
    local user (ps), and a TimeoutExpired's message quotes it.
    """
    import urllib.parse

    log = log_func or _noop_log
    try:
        login = subprocess.run(
            [docker_bin, "exec", "-i", "onionheaven",
             "curl", "-sk", "--socks5-hostname", "127.0.0.1:9050",
             "--max-time", "60", "-X", "POST",
             "--data-binary", "@-",
             _ARCHIVE_ONION + "/services/xauthn/?op=login"],
            input=urllib.parse.urlencode({"email": email, "password": password}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=75, env=env,
        )
        if login.returncode != 0 or not login.stdout:
            log("WARNING: Could not reach archive.org onion to fetch S3 keys")
            return None
        try:
            body = json.loads(login.stdout)
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            log("WARNING: archive.org login returned non-JSON")
            return None
        values = body.get("values") or {}
        if not body.get("success"):
            log(f"WARNING: archive.org login failed: {values.get('reason', 'unknown')}")
            return None

        s3 = values.get("s3") or {}
        access = s3.get("access", "")
        secret = s3.get("secret", "")
        if access and secret:
            return {"access": access, "secret": secret}

        # No keys in the login response: fetch them with its session cookie.
        cookies = values.get("cookies") or {}
        sig = cookies.get("logged-in-sig", "")
        user = cookies.get("logged-in-user", "")
        if sig and user:
            page = subprocess.run(
                [docker_bin, "exec", "-i", "onionheaven",
                 "curl", "-sk", "--socks5-hostname", "127.0.0.1:9050",
                 "--max-time", "60",
                 "-H", "@-",
                 _ARCHIVE_ONION + "/account/s3.php?output_json=1"],
                input=f"Cookie: logged-in-sig={sig}; logged-in-user={user}\n",
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=75, env=env,
            )
            try:
                key = json.loads(page.stdout).get("key") if page.returncode == 0 else None
            except (json.JSONDecodeError, AttributeError):
                key = None
            if isinstance(key, dict) and key.get("s3accesskey") and key.get("s3secretkey"):
                return {"access": key["s3accesskey"], "secret": key["s3secretkey"]}
        log("WARNING: archive.org login succeeded but S3 keys were empty")
        return None
    except subprocess.TimeoutExpired:
        log("WARNING: archive.org login timed out")
        return None
    except (subprocess.SubprocessError, OSError) as e:
        # Not {e}: a subprocess error's message quotes the argv.
        log(f"WARNING: archive.org S3 key fetch failed: {type(e).__name__}")
        return None


def ensure_archive_s3_keys(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Fetch archive.org S3 keys for the shared OnionPress account and
    stash them in WP options so the Wayback Machine sweep can submit.
    Idempotent — no-ops if the keys are already set. Best-effort: any
    failure (no network, archive.org throttling the Tor exit, etc.)
    logs a warning but doesn't error — the user can set their own
    creds later in Settings.

    The keys are the network's one pair, in the main site's options
    (`wp option` without --url): where the Wayback sweep reads them and
    where the Settings page saves them from any site.
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True

    current = _wp("option", "get", "onionpress_archive_s3_access",
                  docker_bin=docker_bin)
    if current.returncode == 0 and current.stdout.strip():
        return True  # Already configured

    log("Fetching archive.org S3 keys for Wayback Machine archiving...")
    keys = fetch_archive_s3_keys(_ARCHIVE_LOGIN_EMAIL, _ARCHIVE_LOGIN_PASS,
                                 docker_bin=docker_bin, log_func=log)
    if keys is None:
        log("WARNING: no archive.org S3 keys — set them manually in Settings if needed.")
        return False

    # `wp option update <key>` with no value reads it from stdin, so the
    # keys stay out of argv.
    _wp("option", "update", "onionpress_archive_s3_access",
        docker_bin=docker_bin, input=keys["access"])
    _wp("option", "update", "onionpress_archive_s3_secret",
        docker_bin=docker_bin, input=keys["secret"])
    log("Archive.org S3 keys configured for Wayback archiving")
    return True


def provision_post_install(
    *,
    themes_dir: str,
    plugins_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> int:
    """Run the post-`wp core install` provisioning sequence. Called by
    setup_logic.install_fresh_wordpress (via the launcher's
    provision-post-install subcommand) and safe to re-run by hand.

    Order matters: ensure_multisite MUST run before
    install_multisite_domain_map, because the latter drops sunrise.php
    and SUNRISE=true, and sunrise.php queries wp_site on every WP load —
    if wp_site doesn't exist yet (multisite-convert hasn't run), every
    subsequent wp-cli call errors out and the theme install silently
    skips.

    Returns 0 on success — best-effort, individual steps log warnings
    without aborting the run.
    """
    log = log_func or _noop_log
    ensure_multisite(docker_bin=docker_bin, log_func=log)
    install_multisite_domain_map(
        plugins_dir=plugins_dir, docker_bin=docker_bin, log_func=log)
    install_onionpress_theme(
        themes_dir=themes_dir, plugins_dir=plugins_dir,
        docker_bin=docker_bin, log_func=log)
    fix_onionpress_permissions(docker_bin=docker_bin, log_func=log)
    fix_wordpress_uploads_permissions(docker_bin=docker_bin, log_func=log)
    # Ensure the WP container can read its own .onion via the shared
    # volume — wait_for_services bails early on fresh installs before
    # writing this; this is the post-Setup belt-and-braces.
    write_shared_onion_address(docker_bin=docker_bin, log_func=log)
    return 0
