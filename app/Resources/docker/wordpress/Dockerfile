FROM wordpress:latest

# Install wp-cli for WordPress management (setup guard, plugin activation, etc.)
RUN curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp

# Enable mod_remoteip for PROXY protocol support and add port 81 listener
# Raise PHP upload limits for large Creations files (videos, HEIC, etc.)
#
# This COPY only takes effect for whoever BUILDS this image. Installs that
# pull the published image by digest get the same file injected at runtime
# instead, as an overriding conf.d overlay — see
# src/onionpress/multisite.py:install_uploads_ini. The two paths read this
# one source file, so they cannot disagree.
COPY onionpress-uploads.ini /usr/local/etc/php/conf.d/

RUN a2enmod remoteip rewrite headers
COPY onionpress-proxy-protocol.conf /etc/apache2/conf-available/
COPY onionpress-xmlrpc-block.conf /etc/apache2/conf-available/
COPY onionpress-log-format.conf /etc/apache2/conf-available/
RUN a2enconf onionpress-proxy-protocol onionpress-xmlrpc-block onionpress-log-format

# NOTE: onionpress-static-site.conf (static-first serving of published
# site generations) is deliberately NOT baked in here. It is injected at
# provision time via `docker cp` + `a2enconf` — see
# src/onionpress/multisite.py:install_static_site_conf. This keeps the
# published WordPress image (which docker-compose.yml pulls by digest,
# never rebuilds) reusable unchanged: no image rebuild, no registry push.

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
