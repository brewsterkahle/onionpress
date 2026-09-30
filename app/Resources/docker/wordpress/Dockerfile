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

# Enable mod_remoteip for PROXY protocol support and add port 81 listener
# Raise PHP upload limits for large Creations files (videos, HEIC, etc.)
COPY onionpress-uploads.ini /usr/local/etc/php/conf.d/

RUN a2enmod remoteip rewrite headers
COPY onionpress-proxy-protocol.conf /etc/apache2/conf-available/
COPY onionpress-xmlrpc-block.conf /etc/apache2/conf-available/
COPY onionpress-log-format.conf /etc/apache2/conf-available/
RUN a2enconf onionpress-proxy-protocol onionpress-xmlrpc-block onionpress-log-format

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
