#!/bin/bash
# Activate WordPress plugin using WP-CLI
# Usage: activate_plugin.sh <container-name> <plugin-path>

if [ $# -ne 2 ]; then
    echo "Usage: activate_plugin.sh <container-name> <plugin-path>"
    exit 1
fi

CONTAINER_NAME="$1"
PLUGIN_PATH="$2"

echo "Activating plugin: ${PLUGIN_PATH}"

# Set up Docker environment
export DOCKER_HOST="unix://${HOME}/.onion.press/colima/default/docker.sock"
export DOCKER_CONFIG="${HOME}/.onion.press/docker-config"

# Get docker binary path
DOCKER_BIN="/Applications/Onion.Press.app/Contents/Resources/bin/docker"
if [ ! -f "$DOCKER_BIN" ]; then
    DOCKER_BIN="docker"  # Fallback to system docker
fi

# Check if WordPress is configured (has database connection)
if ! timeout 10 "$DOCKER_BIN" exec -T "$CONTAINER_NAME" wp core is-installed --allow-root >/dev/null 2>&1; then
    echo "WordPress not yet configured, plugin will activate after setup"
    exit 1
fi

# Activate the plugin
if OUTPUT=$(timeout 10 "$DOCKER_BIN" exec -T "$CONTAINER_NAME" wp plugin activate "$PLUGIN_PATH" --allow-root 2>&1); then
    echo "✓ Plugin activated: ${PLUGIN_PATH}"
    [ -n "$OUTPUT" ] && echo "$OUTPUT"
    exit 0
else
    echo "Failed to activate plugin: $OUTPUT"
    exit 1
fi
