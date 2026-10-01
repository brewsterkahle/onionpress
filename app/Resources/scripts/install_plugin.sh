#!/bin/bash
# Download and verify WordPress plugin from WordPress.org
# Usage: install_plugin.sh <plugin-slug> <destination-dir>

if [ $# -ne 2 ]; then
    echo "Usage: install_plugin.sh <plugin-slug> <destination-dir>"
    exit 1
fi

PLUGIN_SLUG="$1"
DEST_DIR="$2"

if [ ! -d "$DEST_DIR" ]; then
    echo "Error: Destination directory does not exist: $DEST_DIR"
    exit 1
fi

PLUGIN_URL="https://downloads.wordpress.org/plugin/${PLUGIN_SLUG}.zip"
ZIP_PATH="${DEST_DIR}/${PLUGIN_SLUG}.zip"

echo "Downloading ${PLUGIN_SLUG} from WordPress.org over Tor..."
echo "URL: ${PLUGIN_URL}"

# The download runs INSIDE the WordPress container, through Tor's SOCKS proxy.
#
# It used to be a plain `curl` here on the host, which handed the user's real
# IP to wordpress.org — the one correlation this project exists to prevent.
# It cannot simply be a host-side curl with --socks5-hostname either: the SOCKS
# port does not survive Colima's port forwarding (connections are accepted and
# then immediately closed), so anything Tor-routed has to run in a container on
# the compose network. The WordPress container is the one with curl.
CONTAINER="${ONIONPRESS_WP_CONTAINER:-onionpress-wordpress}"
REMOTE_ZIP="/tmp/onionpress-plugin-$$.zip"

if ! docker exec "$CONTAINER" true 2>/dev/null; then
    echo "Error: container ${CONTAINER} is not running — cannot download over Tor"
    exit 1
fi

# onionheaven first: onionpress-tor is also publishing the user's onion
# service, so bulk fetches belong on the other daemon. Never fall back to a
# direct connection — no Tor means no download.
#
# --speed-limit/--speed-time is what makes the fallback worth having. A Tor
# circuit can go silent without closing: the SOCKS handshake is local and
# instant, so curl holds a live connection with nothing coming down it, and
# --max-time alone cannot tell that apart from a genuinely slow download. On
# 2026-08-22 that cost five minutes of a first-run install — onionheaven
# accepted the Cache Enabler request, delivered 0 bytes for the full 300s, and
# only then did onionpress-tor fetch the same file in two seconds. Aborting
# below 1 KB/s for 30s catches the stall while leaving a slow-but-moving
# download alone; measured first-byte through either proxy is ~3s, so 30s is
# roughly 10x headroom. --max-time stays as the outer bound for a download
# that crawls the whole way.
downloaded=0
for proxy in onionheaven onionpress-tor; do
    if docker exec "$CONTAINER" curl -sSL -f --max-time 300 \
            --speed-limit 1024 --speed-time 30 \
            --socks5-hostname "${proxy}:9050" \
            -o "$REMOTE_ZIP" "$PLUGIN_URL" 2>&1; then
        downloaded=1
        echo "Fetched via ${proxy}"
        break
    fi
    echo "Download via ${proxy} failed, trying next Tor proxy..."
done

if [ "$downloaded" -ne 1 ]; then
    echo "Error downloading plugin over Tor"
    docker exec "$CONTAINER" rm -f "$REMOTE_ZIP" 2>/dev/null
    exit 1
fi

if ! docker cp "${CONTAINER}:${REMOTE_ZIP}" "$ZIP_PATH" 2>&1; then
    echo "Error copying downloaded plugin out of ${CONTAINER}"
    docker exec "$CONTAINER" rm -f "$REMOTE_ZIP" 2>/dev/null
    exit 1
fi
docker exec "$CONTAINER" rm -f "$REMOTE_ZIP" 2>/dev/null

# Get file size
SIZE=$(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH" 2>/dev/null)
echo "Downloaded ${SIZE} bytes"

# Calculate SHA256 checksum
SHA256=$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')
echo "SHA256: ${SHA256}"

# Extract plugin
echo "Extracting to ${DEST_DIR}..."
if ! unzip -q -o "$ZIP_PATH" -d "$DEST_DIR" 2>&1; then
    echo "Error: Failed to extract zip file"
    rm -f "$ZIP_PATH"
    exit 1
fi

# Remove zip file
rm -f "$ZIP_PATH"

echo "✓ Plugin ${PLUGIN_SLUG} downloaded and extracted successfully"
exit 0
