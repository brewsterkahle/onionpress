#!/bin/bash
# OnionPress multisite initialization script.
# Runs as part of WordPress container startup to ensure multisite is
# configured from the very first boot. This eliminates the SUNRISE
# chicken-and-egg problem where the launcher would set SUNRISE before
# the wp_site table existed.
#
# Called from the Dockerfile entrypoint wrapper. Runs AFTER WordPress
# and the database are ready.
#
# Key detail: sunrise.php routes requests by querying wp_site WHERE
# domain='localhost'. That means the network row MUST be created with
# domain=localhost — regardless of what the site's own siteurl is
# (which, in production, is http://<hash>.onion). We achieve that by
# temporarily flipping siteurl/home to http://localhost across the
# multisite-convert call, then restoring the real values.

set -e

log() { echo "[multisite-init] $*"; }

# `wp db query` requires the mysql client binary, which is NOT present in
# the wordpress Docker image. Use mysqli via PHP instead — it's always
# available (WordPress needs it).
#
# DB credentials come from the container env (set by docker-compose).
# Reading them from wp-config.php would require parsing PHP — the env
# vars are the source of truth for this image anyway.
db_q() {
    php -r '
        $host = getenv("WORDPRESS_DB_HOST") ?: "db";
        $user = getenv("WORDPRESS_DB_USER") ?: "wordpress";
        $pass = getenv("WORDPRESS_DB_PASSWORD") ?: "";
        $name = getenv("WORDPRESS_DB_NAME") ?: "wordpress";
        [$h, $p] = array_pad(explode(":", $host, 2), 2, "3306");
        $mysqli = @new mysqli($h, $user, $pass, $name, (int)$p);
        if ($mysqli->connect_errno) {
            fwrite(STDERR, "db connect: " . $mysqli->connect_error . "\n");
            exit(1);
        }
        $result = $mysqli->query($argv[1]);
        if ($result === false) {
            fwrite(STDERR, "db query: " . $mysqli->error . "\n");
            exit(2);
        }
        if (is_object($result)) {
            while ($row = $result->fetch_array(MYSQLI_NUM)) {
                echo implode("\t", $row) . "\n";
            }
        }
    ' -- "$1"
}

wait_for_db() {
    local max_wait=60
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if db_q 'SELECT 1' >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    log "WARNING: Database not ready after ${max_wait}s"
    return 1
}

wait_for_db || exit 0

# Skip cleanly if WordPress isn't installed yet — first run will trigger
# this script again after wp core install completes.
if [ "$(db_q 'SHOW TABLES LIKE "wp_options"')" != "wp_options" ]; then
    exit 0
fi
if [ -z "$(db_q "SELECT option_value FROM wp_options WHERE option_name='siteurl'")" ]; then
    exit 0
fi

# --- Core auto-update policy -------------------------------------------
# MUST stay above the "already converted" early-exit below: existing
# installs took that exit every boot, so anything placed after it only
# ever reaches brand-new users.
#
# Builds through v2.4.107 hard-disabled core auto-updates. Combined with
# the digest-pinned image, that left users with no path to a WordPress
# security release at all — not automatic, not manual. wp2shell
# (CVE-2026-63030 + CVE-2026-60137, unauthenticated RCE, CISA KEV
# 2026-07-21) landed on 7.0.0-7.0.1 while WordPress.org was force-pushing
# 7.0.2 through the exact updater we had switched off.
#
# 'minor' rather than true is deliberate: it accepts security and point
# releases like 7.0.2, but never an unattended major upgrade that could
# break the multisite conversion or the bundled mu-plugins with nobody
# watching.
if ! grep -q "define( *'AUTOMATIC_UPDATER_DISABLED', *false *)" /var/www/html/wp-config.php 2>/dev/null \
        || ! grep -q "define( *'WP_AUTO_UPDATE_CORE', *'minor' *)" /var/www/html/wp-config.php 2>/dev/null; then
    wp config set AUTOMATIC_UPDATER_DISABLED false --raw --type=constant --allow-root
    wp config set WP_AUTO_UPDATE_CORE "'minor'" --raw --type=constant --allow-root
    log "auto-update policy applied: AUTOMATIC_UPDATER_DISABLED=false WP_AUTO_UPDATE_CORE='minor'"
fi

site_count=$(db_q 'SELECT COUNT(*) FROM wp_site' || echo 0)
[ -z "$site_count" ] && site_count=0

# Detect half-converted state: wp-config has MULTISITE set but wp_site is
# empty. Strip the stale constants so wp-cli can load as single-site and
# the normal convert path below can run cleanly.
if grep -q "^define( *'MULTISITE'" /var/www/html/wp-config.php 2>/dev/null \
        && [ "$site_count" = "0" ]; then
    log "Detected stale MULTISITE in wp-config with empty wp_site — unwinding"
    for name in MULTISITE SUBDOMAIN_INSTALL DOMAIN_CURRENT_SITE \
                PATH_CURRENT_SITE SITE_ID_CURRENT_SITE BLOG_ID_CURRENT_SITE \
                SUNRISE; do
        wp config delete "$name" --type=constant --allow-root >/dev/null 2>&1 || true
    done
fi

# After any unwind, re-check: if multisite is now both wp-config-active
# AND has a real wp_site row, we're done.
if wp core is-installed --network --allow-root >/dev/null 2>&1 \
        && [ "$(db_q 'SELECT COUNT(*) FROM wp_site')" != "0" ]; then
    exit 0
fi

log "Converting to multisite..."

# Capture the real siteurl/home so we can restore after conversion. These
# are almost always http://<hash>.onion in production; we avoid hardcoding
# the value so dev/test installs with a different siteurl still work.
real_siteurl=$(db_q "SELECT option_value FROM wp_options WHERE option_name='siteurl'")
real_home=$(db_q "SELECT option_value FROM wp_options WHERE option_name='home'")

if [ -z "$real_siteurl" ]; then
    log "ERROR: could not read siteurl from wp_options; aborting"
    exit 1
fi

log "current siteurl=$real_siteurl home=$real_home"

# Flip to localhost so multisite-convert:
#   (a) accepts --url=http://localhost (it validates --url matches siteurl), and
#   (b) writes wp_site with domain=localhost (matching sunrise.php's lookup).
db_q "UPDATE wp_options SET option_value='http://localhost' WHERE option_name='siteurl'" >/dev/null
db_q "UPDATE wp_options SET option_value='http://localhost' WHERE option_name='home'" >/dev/null

restore_urls() {
    db_q "UPDATE wp_options SET option_value='$real_siteurl' WHERE option_name='siteurl'" >/dev/null 2>&1 || true
    db_q "UPDATE wp_options SET option_value='$real_home'    WHERE option_name='home'"    >/dev/null 2>&1 || true
}
trap restore_urls EXIT

# Errors here MUST fail the script — silent failure is exactly what broke
# previous builds.
wp core multisite-convert --url=http://localhost --allow-root
log "multisite-convert done"

# Set constants in wp-config.php. Multisite constants must match the wp_site
# row we just created (domain=localhost, path=/). Auto-update constants live
# here too (not in WORDPRESS_CONFIG_EXTRA) to avoid duplicate-define warnings
# on every request — CONFIG_EXTRA is eval'd and any overlap with wp-config.php
# lines triggers PHP warnings.
for const_val in \
    "MULTISITE:true" \
    "SUBDOMAIN_INSTALL:false" \
    "DOMAIN_CURRENT_SITE:'localhost'" \
    "PATH_CURRENT_SITE:'/'" \
    "SITE_ID_CURRENT_SITE:1" \
    "BLOG_ID_CURRENT_SITE:1" \
    "SUNRISE:true"; do
    name="${const_val%%:*}"
    value="${const_val#*:}"
    wp config set "$name" "$value" --raw --type=constant --allow-root
done

log "Multisite conversion complete"
