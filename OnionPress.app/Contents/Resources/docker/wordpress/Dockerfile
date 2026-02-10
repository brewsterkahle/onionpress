FROM wordpress:6.7

# Install wp-cli for WordPress management (setup guard, plugin activation, etc.)
RUN curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp
