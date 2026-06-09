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
    "onionpress-research-vault.php",
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
