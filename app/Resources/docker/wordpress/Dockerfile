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
# The digest must be the multi-arch INDEX digest — a per-platform manifest
# digest resolves on amd64 and 404s on the arm64 builder.
ARG WORDPRESS_IMAGE=wordpress:latest@sha256:74103f21d460e96abdee0dc304852a00823950bef3cb343ba855e19b6983c31d
FROM ${WORDPRESS_IMAGE}

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
    && chmod +x /usr/local/bin/wp \
    && wp --info --allow-root >/dev/null

# Raise PHP upload limits for large Creations files (videos, HEIC, etc.)
COPY onionpress-uploads.ini /usr/local/etc/php/conf.d/

# Apache listens on :80 only; onion visitors arrive there too (Tor -> socat
# in onionpress-tor -> wordpress:80). There is no PROXY-protocol listener:
# the :81 one C Tor fed until the switch to socat let any container on
# onionpress-network name its own REMOTE_ADDR (127.0.0.1, the gateway).
RUN a2enmod rewrite headers
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
# their <IfModule>.
RUN apache2ctl configtest \
    && apache2ctl -M 2>/dev/null | grep -q ' php_module '

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
