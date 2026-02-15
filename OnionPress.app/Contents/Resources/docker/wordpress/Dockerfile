FROM wordpress:latest

# Install wp-cli for WordPress management (setup guard, plugin activation, etc.)
RUN curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp

# Enable mod_remoteip for PROXY protocol support and add port 81 listener
RUN a2enmod remoteip rewrite
COPY onionpress-proxy-protocol.conf /etc/apache2/conf-available/
RUN a2enconf onionpress-proxy-protocol
