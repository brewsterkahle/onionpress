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

import os
import subprocess
from typing import Callable, Optional


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
)

# Icon assets co-located with the mu-plugins.
MU_PLUGIN_ASSETS = (
    "onionpress-sidebar-icon.png",
    "onionpress-follow-icon.png",
    "onionpress-avatar-default.png",
)

# Where install_uploads_ini drops the PHP limits overlay. The `zz-` prefix
# is load-bearing: PHP reads conf.d in alphabetical order and the last value
# wins, so this sorts after the image's own onionpress-uploads.ini and
# overrides it without overwriting it.
UPLOADS_INI_PATH = "/usr/local/etc/php/conf.d/zz-onionpress-uploads.ini"

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
HTACCESS_BODY = """\
# Privacy: prevent onion address leaking in Referer headers
<IfModule mod_headers.c>
Header set Referrer-Policy "no-referrer"
</IfModule>

# BEGIN WordPress Multisite
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\\.php$ - [L]

# add a trailing slash to /wp-admin
RewriteRule ^([_0-9a-zA-Z-]+/)?wp-admin$ $1wp-admin/ [R=301,L]

RewriteCond %{REQUEST_FILENAME} -f [OR]
RewriteCond %{REQUEST_FILENAME} -d
RewriteRule ^ - [L]
RewriteRule ^([_0-9a-zA-Z-]+/)?(wp-(content|admin|includes).*) $2 [L]
RewriteRule ^([_0-9a-zA-Z-]+/)?(.*\\.php)$ $2 [L]
RewriteRule . index.php [L]
# END WordPress Multisite
"""


def _wp(
    *args,
    docker_bin: str = "docker",
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run wp-cli inside the onionpress-wordpress container."""
    return subprocess.run(
        [docker_bin, "exec", "onionpress-wordpress",
         "wp", "--allow-root"] + list(args),
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


def _docker_cp(
    src: str,
    dest: str,
    *,
    docker_bin: str = "docker",
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """`docker cp <src> <container:dest>`. Caller's responsibility to rm
    dest first if it's a directory — docker cp into an existing dir
    copies INTO it (creates dest/src/), not over it.
    """
    return subprocess.run(
        [docker_bin, "cp", src, dest],
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
        cp = _docker_cp(
            sunrise_src,
            "onionpress-wordpress:/var/www/html/wp-content/sunrise.php",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            _exec_sh(
                "chown www-data:www-data /var/www/html/wp-content/sunrise.php",
                docker_bin=docker_bin,
            )
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

    # 2. .htaccess. Write via a heredoc inside the container so we don't
    # need a temp file on the host. Single-quoted heredoc delimiter so the
    # shell doesn't interpret $1, %{...}, etc.
    # Body is HTACCESS_BODY at module scope.
    htaccess_cmd = (
        "cat > /var/www/html/.htaccess <<'HTEOF'\n"
        + HTACCESS_BODY
        + "HTEOF\n"
        "chown www-data:www-data /var/www/html/.htaccess"
    )
    r = _exec_sh(htaccess_cmd, docker_bin=docker_bin)
    if r.returncode == 0:
        log(".htaccess multisite rewrite rules installed")
    else:
        log(f"WARNING: Failed to write .htaccess: {r.stderr.strip()[:200]}")

    # 3. mu-plugins directory + each plugin.
    _exec_sh(
        "mkdir -p /var/www/html/wp-content/mu-plugins",
        docker_bin=docker_bin,
    )
    for plugin in MU_PLUGINS:
        src = os.path.join(plugins_dir, plugin)
        if not os.path.isfile(src):
            continue
        cp = _docker_cp(
            src,
            f"onionpress-wordpress:/var/www/html/wp-content/mu-plugins/{plugin}",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            _exec_sh(
                f"chown www-data:www-data /var/www/html/wp-content/mu-plugins/{plugin}",
                docker_bin=docker_bin,
            )
            log(f"{plugin} mu-plugin installed")
        else:
            log(f"WARNING: Failed to copy {plugin}")

    # 4. Icon assets used by onionpress-settings.php, onionpress-login-fix.php,
    # onionpress-avatar.php (default avatar image).
    for asset in MU_PLUGIN_ASSETS:
        src = os.path.join(plugins_dir, asset)
        if not os.path.isfile(src):
            continue
        cp = _docker_cp(
            src,
            f"onionpress-wordpress:/var/www/html/wp-content/mu-plugins/{asset}",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            _exec_sh(
                f"chown www-data:www-data /var/www/html/wp-content/mu-plugins/{asset}",
                docker_bin=docker_bin,
            )
            log(f"{asset} installed")
        else:
            log(f"WARNING: Failed to copy {asset}")

    return True


def install_static_site_conf(
    *,
    conf_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Inject the Apache static-first conf into the running WordPress
    container at provision time (the runtime equivalent of the Dockerfile
    COPY + a2enconf we removed).

    Why runtime, not baked-in: docker-compose.yml pulls the published
    WordPress image by digest — it is never built locally. Baking
    onionpress-static-site.conf into that image would force us to rebuild
    + host a fork image. Copying it in the same way the mu-plugins are
    (docker cp into the live container) reuses the published image
    unchanged. Mind the
    asymmetry with the mu-plugins, though: those land in /var/www/html,
    which is a Docker volume and so persists, whereas /etc/apache2 is
    container rootfs. This conf survives a restart but NOT a container
    recreate, and the provisioning path that calls this does not run when
    the launcher's `start` short-circuits on an already-running stack.
    `ensure_static_site_conf` closes that gap — see its docstring.

    `conf_dir` is the on-disk directory holding onionpress-static-site.conf
    (Mac: `$RESOURCES_DIR/docker/wordpress`; Linux: `$INSTALL_DIR/docker/
    wordpress`). Best-effort: a missing file or a not-yet-running container
    logs a warning and returns False without aborting the provision run.
    """
    log = log_func or _noop_log
    src = os.path.join(conf_dir, "onionpress-static-site.conf")
    if not os.path.isfile(src):
        log(f"WARNING: static-site Apache conf not found at {src} — "
            "static-site serving not enabled")
        return False

    cp = _docker_cp(
        src,
        "onionpress-wordpress:/etc/apache2/conf-available/"
        "onionpress-static-site.conf",
        docker_bin=docker_bin,
    )
    if cp.returncode != 0:
        log(f"WARNING: Failed to copy static-site Apache conf: "
            f"{cp.stderr.strip()[:200]}")
        return False

    # Enable mod_rewrite (the conf's InheritDownBefore rules need it),
    # enable the conf, then gracefully reload Apache so it takes effect
    # without dropping in-flight requests. a2enmod/a2enconf are idempotent
    # (no-op + exit 0 when already enabled), so this is safe every start.
    r = _exec_sh(
        "a2enmod rewrite && a2enconf onionpress-static-site && "
        "apache2ctl graceful",
        docker_bin=docker_bin,
    )
    if r.returncode != 0:
        log(f"WARNING: Failed to enable static-site Apache conf: "
            f"{r.stderr.strip()[:200]}")
        # The chain is not atomic: a2enconf can succeed and then
        # `apache2ctl graceful` fail, leaving the conf-enabled symlink in
        # place while Apache still serves the old config. That is exactly
        # the state ensure_static_site_conf's presence probe reads as
        # "healthy" — so it would short-circuit forever on a conf that
        # never took effect, recreating the silent-forever failure this
        # pair exists to end. Back the symlink out so the probe stays
        # honest and the next start retries.
        try:
            _exec_sh("a2disconf onionpress-static-site", docker_bin=docker_bin)
        except Exception:
            pass  # best-effort cleanup; the warning above is the signal
        return False

    log("Static-first Apache conf installed "
        "(static generations served ahead of WordPress)")
    return True


def install_uploads_ini(
    *,
    conf_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Inject the PHP limits ini into the running WordPress container at
    provision time, as an overlay that leaves the image's own copy alone.

    Same reasoning as install_static_site_conf: docker-compose.yml pulls the
    WordPress image by digest from a registry the fork does not own, so a
    `COPY` in our Dockerfile only reaches a container someone else built.
    Runtime injection reuses the published image unchanged.

    Why an overlay under a different name rather than overwriting: unlike
    onionpress-static-site.conf, the image ALREADY ships
    /usr/local/etc/php/conf.d/onionpress-uploads.ini — present, but frozen
    at whatever limits the image was built with. PHP reads conf.d in
    alphabetical order, last value wins, so copying our version to
    zz-onionpress-uploads.ini overrides the image's without touching it. The
    image's file stays pristine (an upstream revision of it is not silently
    clobbered), and backing the injection out is a plain `rm` of a file we
    alone own.

    The reload is not optional. mod_php reads conf.d when an Apache worker
    initialises, so `docker cp` alone leaves every live worker on the old
    limits — verified against a real container: the copy lands, the served
    values stay at the image's, and only `apache2ctl graceful` moves them.

    `conf_dir` is the on-disk directory holding onionpress-uploads.ini — the
    same directory install_static_site_conf reads its conf from.
    Best-effort: a missing file or a not-yet-running container logs a
    warning and returns False without aborting the provision run.
    """
    log = log_func or _noop_log
    src = os.path.join(conf_dir, "onionpress-uploads.ini")
    if not os.path.isfile(src):
        log(f"WARNING: PHP limits ini not found at {src} — "
            "large static-site uploads may exceed PHP's default limits")
        return False

    cp = _docker_cp(
        src,
        f"onionpress-wordpress:{UPLOADS_INI_PATH}",
        docker_bin=docker_bin,
    )
    if cp.returncode != 0:
        log(f"WARNING: Failed to copy PHP limits ini: "
            f"{cp.stderr.strip()[:200]}")
        return False

    r = _exec_sh("apache2ctl graceful", docker_bin=docker_bin)
    if r.returncode != 0:
        log(f"WARNING: Failed to reload Apache for PHP limits ini: "
            f"{r.stderr.strip()[:200]}")
        # Copy-then-reload is not atomic, and the overlay file existing is
        # exactly what ensure_uploads_ini's probe reads as "healthy" — so a
        # failed reload would short-circuit every later start on limits
        # Apache never actually loaded. Remove it so the probe stays honest
        # and the next start retries.
        try:
            _exec_sh(f"rm -f {UPLOADS_INI_PATH}", docker_bin=docker_bin)
        except Exception:
            pass  # best-effort cleanup; the warning above is the signal
        return False

    log("PHP limits ini installed "
        "(large static generations can be uploaded)")
    return True


def ensure_uploads_ini(
    *,
    conf_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Guarantee the PHP limits overlay is present, cheaply.

    The container-rootfs/volume asymmetry ensure_static_site_conf exists for
    applies here too: /usr/local/etc/php/conf.d is rootfs, so a container
    RECREATE drops the overlay and restores the image's lower limits, while
    a plain restart keeps it. provision_post_install re-injects on the next
    full start, but a launcher `start` that short-circuits on an
    already-running stack never reaches provisioning.

    The failure that leaves behind is not silent, unlike the static-site
    one — the next large upload fails outright at the image's stale
    limits. It is, though, indefinite: nothing else on the start path
    would ever put the overlay back.

    Cheap by design — one `test -e` on the happy path, no docker cp and no
    Apache reload, so it is safe on every start including the fast
    already-running path. Same deliberate tradeoff as
    ensure_static_site_conf: it tests presence, not content, so an app
    update shipping revised limits will NOT refresh an overlay already in
    the container. An app update recreates the container anyway, which
    drops the overlay entirely and lets provision_post_install install the
    new version on the very next start.

    Returns True if the overlay is present or was successfully restored.
    Best-effort like install_uploads_ini: never raises, so it can never turn
    a healthy start into a failed one.
    """
    log = log_func or _noop_log
    try:
        present = _exec_sh(
            f"test -e {UPLOADS_INI_PATH}", docker_bin=docker_bin)
        if present.returncode == 0:
            return True

        # Same rc=1 ambiguity ensure_static_site_conf splits on: `test -e`
        # reports absence with an EMPTY stderr, while the docker CLI reports
        # a dead daemon or a stopped container with a message ON stderr.
        # Without the split, a stopped container claims a recreate happened
        # and runs a copy that cannot land.
        if present.stderr.strip():
            log("WARNING: could not check PHP limits ini: "
                f"{present.stderr.strip()[:200]}")
            return False

        log("PHP limits ini missing (container recreated?) — reinstalling "
            "so large static generations can still be uploaded")
        return install_uploads_ini(
            conf_dir=conf_dir, docker_bin=docker_bin, log_func=log,
        )
    except Exception as e:  # docker missing, daemon hang, timeout
        # Spans the reinstall as well as the probe: _docker_cp and _exec_sh
        # raise TimeoutExpired/OSError rather than returning a non-zero rc,
        # and this function promises callers it never raises.
        log(f"WARNING: could not ensure PHP limits ini: {e}")
        return False


def ensure_static_site_conf(
    *,
    conf_dir: str,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Guarantee the static-first Apache conf is present, cheaply.

    Why this is separate from install_static_site_conf: /etc/apache2 is
    container rootfs, not a Docker volume, so the conf is destroyed by any
    container RECREATE (compose recreate, image change, stack update); a
    plain restart keeps it. provision_post_install normally re-injects it
    on the next start, but a launcher `start` that short-circuits on an
    already-running stack ("nothing to do") never reaches that
    provisioning.

    A recreate done behind the launcher's back therefore leaves the conf
    missing indefinitely, and the failure is silent in the worst way:
    publishes keep succeeding, and the onion just serves the WordPress
    theme instead of the published site.

    Cheap by design — one `test -e` in the container on the happy path,
    with no docker cp and no Apache reload, so it is safe to call on every
    start including the fast already-running path. The tradeoff is that it
    tests presence, not content: an app update shipping a revised
    onionpress-static-site.conf will NOT refresh a stale one already in the
    container. That is deliberate — comparing content on every start costs
    a docker cp plus an Apache reload, and an app update recreates the
    container anyway (dropping the conf entirely), so provision_post_install
    installs the new version on the very next start.

    Only a launcher whose `start` can short-circuit on an already-running
    stack needs this. The Linux launcher's `start` has no such
    short-circuit — it always runs start_containers, which already calls
    provision-post-install with --apache-conf-dir.

    Returns True if the conf is present or was successfully restored.
    Best-effort like install_static_site_conf: never raises, so it can
    never turn a healthy start into a failed one.
    """
    log = log_func or _noop_log
    try:
        present = _exec_sh(
            "test -e /etc/apache2/conf-enabled/onionpress-static-site.conf",
            docker_bin=docker_bin,
        )
        if present.returncode == 0:
            return True

        # Tell "the conf is absent" apart from "I couldn't ask". `test -e`
        # reports absence with rc=1 and an EMPTY stderr; the docker CLI
        # reports a dead daemon or a stopped/absent container with rc=1 and
        # a message ON stderr. Without this split, a stopped container logs
        # the recreate-detected line below and runs a pointless copy —
        # inverting the diagnostic value of the one line this whole
        # function exists to emit.
        if present.stderr.strip():
            log("WARNING: could not check static-site Apache conf: "
                f"{present.stderr.strip()[:200]}")
            return False

        log("Static-first Apache conf missing (container recreated?) — "
            "reinstalling so static generations are served ahead of WordPress")
        return install_static_site_conf(
            conf_dir=conf_dir, docker_bin=docker_bin, log_func=log,
        )
    except Exception as e:  # docker missing, daemon hang, timeout
        # Deliberately spans the reinstall as well as the probe: _docker_cp
        # and _exec_sh raise TimeoutExpired/OSError rather than returning a
        # non-zero rc, and this function promises callers it never raises.
        log(f"WARNING: could not ensure static-site Apache conf: {e}")
        return False


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

    # 1. Theme. Pre-delete the destination — docker cp into an existing
    # directory copies INTO it (creates dest/src/), not over it.
    theme_src = os.path.join(themes_dir, "onionpress")
    if os.path.isdir(theme_src):
        _exec_sh(
            "rm -rf /var/www/html/wp-content/themes/onionpress",
            docker_bin=docker_bin,
        )
        cp = _docker_cp(
            theme_src,
            "onionpress-wordpress:/var/www/html/wp-content/themes/onionpress",
            docker_bin=docker_bin,
        )
        if cp.returncode == 0:
            _exec_sh(
                "chown -R www-data:www-data /var/www/html/wp-content/themes/onionpress",
                docker_bin=docker_bin,
            )
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

    # 2. Hit counter + creations plugins. Pre-delete same reason.
    for plugin in ("onionpress-hit-counter", "onionpress-creations"):
        src = os.path.join(plugins_dir, plugin)
        if not os.path.isdir(src):
            continue
        _exec_sh(
            f"rm -rf /var/www/html/wp-content/plugins/{plugin}",
            docker_bin=docker_bin,
        )
        cp = _docker_cp(
            src,
            f"onionpress-wordpress:/var/www/html/wp-content/plugins/{plugin}",
            docker_bin=docker_bin,
        )
        if cp.returncode != 0:
            log(f"WARNING: Failed to copy {plugin}")
            continue
        _exec_sh(
            f"chown -R www-data:www-data /var/www/html/wp-content/plugins/{plugin}",
            docker_bin=docker_bin,
        )
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
    with "Unable to create directory" errors.
    """
    log = log_func or _noop_log
    r = _exec_sh(
        "mkdir -p /var/www/html/wp-content/uploads && "
        "chown -R www-data:www-data /var/www/html/wp-content/uploads",
        docker_bin=docker_bin,
    )
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
    """
    log = log_func or _noop_log
    r = subprocess.run(
        [docker_bin, "exec", "onionpress-tor", "sh", "-c",
         "cp /var/lib/tor/hidden_service/wordpress/hostname "
         "/var/lib/onionpress/onion_address"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        log("Onion address written to shared volume")
        return True
    # Caller may not care — this can fail benignly when tor isn't up yet.
    return False


def configure_ia_plugin(
    *,
    docker_bin: str = "docker",
    log_func: Optional[Callable[[str], None]] = None,
) -> bool:
    """Configure the Internet Archive Wayback Machine Link Fixer plugin
    if it's installed and WP is set up. Skips silently if either is not
    yet the case (the bash launcher used to run this every start; we
    preserve the no-op-when-not-ready semantics).
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True

    plugin_file = (
        "/var/www/html/wp-content/plugins/"
        "internet-archive-wayback-machine-link-fixer/"
        "internet-archive-wayback-machine-link-fixer.php"
    )
    if _exec_sh(f"test -f {plugin_file}",
                docker_bin=docker_bin).returncode != 0:
        return True  # Plugin not installed; nothing to configure

    done = _wp("option", "get", "iawmlf_setup_wizard_completed",
               docker_bin=docker_bin)
    if done.returncode == 0 and done.stdout.strip() == "1":
        log("Internet Archive plugin already configured")
        return True

    log("Configuring Internet Archive Wayback Machine Link Fixer plugin...")
    options = (
        ("iawmlf_process_links", "1"),
        ("iawmlf_fixer_option", "replace_link"),
        ("iawmlf_scan_existing_posts", "1"),
        ("iawmlf_setup_wizard_completed", "1"),
    )
    for name, value in options:
        r = _wp("option", "update", name, value, docker_bin=docker_bin)
        if r.returncode != 0:
            log(f"WARNING: Failed to set {name}: {r.stderr.strip()[:200]}")

    log("Internet Archive plugin configured (link fixer enabled, wizard completed)")
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
    """
    log = log_func or _noop_log
    if not wp_is_installed(docker_bin):
        return True

    current = _wp("option", "get", "onionpress_archive_s3_access",
                  docker_bin=docker_bin)
    if current.returncode == 0 and current.stdout.strip():
        return True  # Already configured

    log("Fetching archive.org S3 keys for Wayback Machine archiving...")
    # POST to archive.org's onion (NOT clearnet) — clearnet archive.org
    # via Tor exit nodes is aggressively rate-limited / blocked by their
    # Cloudflare layer (every exit we tried 2026-05-25 returned HTTP 000).
    # The onion service (Onion-Location header advertised on archive.org
    # itself) routes around that block entirely. -k accepts the onion's
    # self-issued certificate.
    login = subprocess.run(
        [docker_bin, "exec", "onionheaven",
         "curl", "-sk", "--socks5-hostname", "127.0.0.1:9050",
         "--max-time", "60", "-X", "POST",
         "-d", f"email={_ARCHIVE_LOGIN_EMAIL}&password={_ARCHIVE_LOGIN_PASS}",
         "https://archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/services/xauthn/?op=login"],
        capture_output=True, text=True, timeout=75,
    )
    if login.returncode != 0 or not login.stdout:
        log("WARNING: Could not reach archive.org onion to fetch S3 keys — "
            "set them manually in Settings if needed.")
        return False

    import json as _json
    try:
        body = _json.loads(login.stdout)
    except _json.JSONDecodeError:
        log("WARNING: archive.org login returned non-JSON")
        return False

    s3 = (body.get("values") or {}).get("s3") or {}
    access = s3.get("access", "")
    secret = s3.get("secret", "")
    if not access or not secret:
        log("WARNING: archive.org login succeeded but S3 keys were empty")
        return False

    _wp("option", "update", "onionpress_archive_s3_access", access,
        docker_bin=docker_bin)
    _wp("option", "update", "onionpress_archive_s3_secret", secret,
        docker_bin=docker_bin)
    log("Archive.org S3 keys configured for Wayback archiving")
    return True


def provision_post_install(
    *,
    themes_dir: str,
    plugins_dir: str,
    conf_dir: Optional[str] = None,
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
    # Runtime-inject the Apache static-first conf so static generations
    # shadow WordPress, and the PHP limits overlay so uploading one doesn't
    # exceed the image's PHP limits. Only when a conf dir is supplied —
    # callers that predate static-first serving (and don't pass one) keep
    # the earlier behavior. Both files live in that same directory.
    if conf_dir:
        install_static_site_conf(
            conf_dir=conf_dir, docker_bin=docker_bin, log_func=log)
        install_uploads_ini(
            conf_dir=conf_dir, docker_bin=docker_bin, log_func=log)
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
