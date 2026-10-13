#!/bin/bash
# Activate a WordPress plugin with wp-cli
# Usage: activate_plugin.sh <container-name> <plugin-path> [--network | --each-site]
#
#   (no option)  activate it on the main site
#   --network    network-activate it (Cache Enabler: one advanced-cache.php
#                drop-in and one page cache serve the whole network)
#   --each-site  activate it on every site of the network, one by one (the
#                IA Link Fixer creates its link table only in the site it is
#                activated on, so a network activation left the others
#                without one)
# A single-site WordPress gets a plain activation either way.
#
# Exit status: 0 the plugin is active, 1 it could not be activated (on a
# site, for --each-site), 2 WordPress is not installed yet.
#
# Runs in the caller's environment: the launchers have put their docker on
# PATH and pointed DOCKER_HOST at their daemon (the Colima VM's socket on
# macOS, the user's rootless or the system daemon on Linux). This script
# used to set DOCKER_HOST to the Colima socket itself, which on Linux is
# nothing, and wrapped wp-cli in timeout(1), which macOS does not have
# (and, before that, passed docker exec a flag (-T) it does not take): it
# never activated anything, on either platform.

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
    echo "Usage: activate_plugin.sh <container-name> <plugin-path> [--network | --each-site]"
    exit 1
fi

CONTAINER_NAME="$1"
PLUGIN_PATH="$2"
SCOPE="${3:-}"

case "$SCOPE" in
    ""|--network|--each-site) ;;
    *)
        echo "Unknown option: $SCOPE (expected --network or --each-site)"
        exit 1
        ;;
esac
# <plugin folder>/<main file>.php, as `wp plugin activate` takes it.
if ! [[ "$PLUGIN_PATH" =~ ^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*\.php$ ]]; then
    echo "Not a plugin path: $PLUGIN_PATH"
    exit 1
fi

DOCKER_BIN=docker

# wp-cli runs as www-data, never as root (onionpress.wp_files.wp_cli_args):
# it loads WordPress, whose plugins, themes and wp-config.php www-data can
# write, so as root a plugin bug became root in the container.
WP_CLI=("$DOCKER_BIN" exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache "$CONTAINER_NAME" wp)

if ! "${WP_CLI[@]}" core is-installed >/dev/null 2>&1; then
    echo "WordPress not yet installed; ${PLUGIN_PATH} will be activated after setup"
    exit 2
fi

# activate [--network | --url=<site>]: `wp plugin activate` succeeds for a
# plugin that is already active (or network-active) too.
activate() {
    local output
    if output=$("${WP_CLI[@]}" plugin activate "$PLUGIN_PATH" "$@" 2>&1); then
        echo "✓ Plugin active: ${PLUGIN_PATH}${1:+ ($1)}"
        [ -n "$output" ] && echo "$output"
        return 0
    fi
    echo "Failed to activate ${PLUGIN_PATH}${1:+ ($1)}: $output"
    return 1
}

if [ -z "$SCOPE" ] || ! "${WP_CLI[@]}" core is-installed --network >/dev/null 2>&1; then
    activate
    exit $?
fi

if [ "$SCOPE" = "--network" ]; then
    activate --network
    exit $?
fi

if ! SITES=$("${WP_CLI[@]}" site list --field=url 2>/dev/null) || [ -z "$SITES" ]; then
    echo "Could not list the network's sites; ${PLUGIN_PATH} not activated"
    exit 1
fi
STATUS=0
while IFS= read -r SITE_URL; do
    [[ "$SITE_URL" =~ ^https?://[^[:space:]]+$ ]] || continue
    activate "--url=$SITE_URL" || STATUS=1
done <<< "$SITES"
exit $STATUS
