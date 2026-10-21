# OnionPress WordPress image.
#
# Base image and wp-cli are pinned to immutable identifiers. See
# docs/BUILDING.md, "Bumping a pinned input", for how to move them.
#
# Pinning the base does NOT freeze WordPress core for users:
# onionpress-security-audit.sh runs on every container start and applies
# pending core security releases over Tor (see `$WP core update` there). The
# pin fixes which WordPress this image is *built* on; the running site patches
# itself regardless of when the pin was last refreshed.
#
# A site's WordPress lives in its volume, not in the image (the base copies
# its files in only on a first start), so a new base alone moves no existing
# site to a new branch. WORDPRESS_BRANCH below does: the audit takes a site
# on an older branch up to it, over Tor, and never past it.
#
# The digest must be the multi-arch INDEX digest — a per-platform manifest
# digest resolves on amd64 and 404s on the arm64 builder.
ARG WORDPRESS_IMAGE=wordpress:latest@sha256:18b73b2dad9c0cdced35c5200dd6562c937a8e994762b4f2b43b1ece9fb317c9
FROM ${WORDPRESS_IMAGE}

# The WordPress branch this image is built and tested on. The build fails
# when the base carries another branch, so re-pinning WORDPRESS_IMAGE to a
# new major (7.2) means raising this too, after checking the multisite setup,
# the bundled mu-plugins and the pinned plugins on it (docs/BUILDING.md,
# "Moving to a new WordPress branch"): every existing site follows it at its
# next start.
ARG WORDPRESS_BRANCH=7.1
RUN branch=$(php -r 'include "/usr/src/wordpress/wp-includes/version.php"; echo implode(".", array_slice(explode(".", $wp_version), 0, 2));') \
    && echo "WordPress $branch (base image)" \
    && if [ "$branch" != "$WORDPRESS_BRANCH" ]; then \
         echo "The base image carries WordPress $branch, not WORDPRESS_BRANCH=$WORDPRESS_BRANCH" >&2; exit 1; \
       fi \
    && mkdir -p /usr/local/share/onionpress \
    && printf '%s\n' "$WORDPRESS_BRANCH" > /usr/local/share/onionpress/wordpress-branch

# wp-cli for WordPress management (setup guard, plugin activation, etc.).
#
# This used to be an unverified `curl -o` from the gh-pages branch of
# wp-cli/builds — a moving target, no checksum, and no `-f`, so an HTTP error
# page was written to /usr/local/bin/wp and chmod +x'd. The failure then
# surfaced much later and far away, inside onionpress-multisite-init.sh or
# onionpress-security-audit.sh, rather than at build time.
#
# Now: a tagged release asset, with the published sha256 verified before the
# file is made executable. A mismatch fails the build.
ARG WP_CLI_VERSION=2.12.0
ARG WP_CLI_SHA256=ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c
RUN curl -fsSL -o /usr/local/bin/wp \
      "https://github.com/wp-cli/wp-cli/releases/download/v${WP_CLI_VERSION}/wp-cli-${WP_CLI_VERSION}.phar" \
    && echo "${WP_CLI_SHA256}  /usr/local/bin/wp" | sha256sum -c - \
    && chmod +x /usr/local/bin/wp

# PHP2Shell (CVE-2026-109618, CVE-2026-109619, 2026-10-21): unauthenticated
# RCE in PHP itself, mod_php the most exposed SAPI. Fixed in 8.2.35, 8.3.36,
# 8.4.27 and 8.5.12; a base image with an older PHP of its branch fails the
# build, so a stale WORDPRESS_IMAGE pin cannot ship it again (2.5.4 shipped
# 8.3.33). Raise these floors with the next PHP security release.
RUN php -r '$floor = ["8.2" => "8.2.35", "8.3" => "8.3.36", "8.4" => "8.4.27", "8.5" => "8.5.12"]; \
      $branch = PHP_MAJOR_VERSION . "." . PHP_MINOR_VERSION; \
      if (!isset($floor[$branch]) && version_compare(PHP_VERSION, "8.6", "<")) { fwrite(STDERR, "PHP " . PHP_VERSION . " is not a supported branch\n"); exit(1); } \
      if (isset($floor[$branch]) && version_compare(PHP_VERSION, $floor[$branch], "<")) { fwrite(STDERR, "PHP " . PHP_VERSION . " lacks the PHP2Shell fix (" . $floor[$branch] . ")\n"); exit(1); } \
      echo "PHP ", PHP_VERSION, "\n";'

# Raise PHP upload limits for large Creations files (videos, HEIC, etc.)
COPY onionpress-uploads.ini /usr/local/etc/php/conf.d/
# PHP2Shell mitigation that costs WordPress nothing (see the file).
COPY onionpress-php-hardening.ini /usr/local/etc/php/conf.d/

# REMOTE_ADDR is the TCP peer, never an address a client names: the
# auto-login plugin and /__op_proxy.php admit requests by it.
#
# Apache listens on :80 only; onion visitors arrive there too (Tor -> socat
# in onionpress-tor -> wordpress:80). There is no PROXY-protocol listener:
# the :81 one C Tor fed until the switch to socat let any container on
# onionpress-network name its own REMOTE_ADDR (127.0.0.1, the gateway).
#
# The base image loads mod_remoteip and trusts X-Forwarded-For from every
# private address (its remoteip.conf), and every peer WordPress has is one:
# the tor container, cloudflared, the onionpress-network gateway. Any onion
# visitor could set REMOTE_ADDR with that header, so the conf and the
# module are off (the check below fails the build if the module returns).
# Cloudflare Tunnel visitors now show as cloudflared, as onion visitors
# show as the tor container.
RUN a2disconf remoteip && a2dismod remoteip && a2enmod rewrite headers
COPY onionpress-xmlrpc-block.conf /etc/apache2/conf-available/
COPY onionpress-log-format.conf /etc/apache2/conf-available/
# Static reachability target for the app's Tor-network probe (issue #266):
# served without PHP so WordPress latency can't masquerade as a Tor failure.
COPY onionpress-health.conf /etc/apache2/conf-available/
COPY onionpress-health.txt /usr/local/share/onionpress/health.txt
# My Creations is bind-mounted under DocumentRoot: serve it, never run it.
COPY onionpress-creations.conf /etc/apache2/conf-available/
# The same for wp-content/uploads: media, never code.
COPY onionpress-uploads.conf /etc/apache2/conf-available/
RUN a2enconf onionpress-xmlrpc-block onionpress-log-format onionpress-health onionpress-creations onionpress-uploads
# A conf Apache rejects would take every site down at the next image pull;
# fail the build instead. The php_module check keeps the creations and
# uploads confs' `php_admin_flag engine off` from being skipped silently by
# their <IfModule>. The remoteip_module check keeps REMOTE_ADDR the TCP
# peer (above) whatever a new base image enables.
RUN apache2ctl configtest \
    && apache2ctl -M 2>/dev/null | grep -q ' php_module ' \
    && [ "$(php -r 'echo var_export((bool) ini_get("session.upload_progress.enabled"), true);')" = false ] \
    && ! apache2ctl -M 2>/dev/null | grep -q ' remoteip_module '

# wp-cli for the scripts below, run as www-data: root never loads
# WordPress's PHP, which www-data can write. Checked here, so an image
# where runuser cannot drop to www-data fails the build instead of every
# wp-cli call at runtime.
COPY onionpress-wp /usr/local/bin/
RUN chmod +x /usr/local/bin/onionpress-wp \
    && onionpress-wp --info >/dev/null \
    && [ "$(runuser -u www-data -- id -un)" = www-data ]

# Gives www-data back the files root's wp-cli left in the web root, at
# every start, before the two scripts below.
COPY onionpress-web-root-owner.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/onionpress-web-root-owner.sh

# Multisite init script — runs on every container start to ensure
# multisite is configured (safe to re-run, checks if already done)
COPY onionpress-multisite-init.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/onionpress-multisite-init.sh

# Security audit — applies pending core security releases and scans for
# wp2shell (CVE-2026-63030) compromise artifacts on every start.
# The helper does all of the audit's network work through Tor; wp-cli's own
# core commands bypass the WordPress HTTP API and would go clearnet.
COPY onionpress-security-audit.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/onionpress-security-audit.sh
COPY onionpress-core-update.php /usr/local/lib/

# Custom entrypoint: run multisite init in background after WordPress starts,
# then hand off to the default WordPress entrypoint
COPY onionpress-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/onionpress-entrypoint.sh
ENTRYPOINT ["onionpress-entrypoint.sh"]
CMD ["apache2-foreground"]
