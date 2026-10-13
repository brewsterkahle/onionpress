#!/bin/bash
# Download and verify WordPress plugin from WordPress.org
# Usage: install_plugin.sh <plugin-slug> <destination-dir>
#        install_plugin.sh --pinned-version <plugin-slug>
#            prints the version pinned below (the launchers replace an
#            installed copy of any other version)

if [ $# -ne 2 ]; then
    echo "Usage: install_plugin.sh <plugin-slug> <destination-dir>"
    echo "       install_plugin.sh --pinned-version <plugin-slug>"
    exit 1
fi

PLUGIN_SLUG="$1"
DEST_DIR="$2"

# The plugin releases OnionPress installs: "<version> <sha256 of its zip>".
# The zip is checked against this before anything is unpacked, and a slug
# with no pin, or a zip that does not match, installs nothing. HTTPS
# protects the download in transit; the pin is what ties it to the release
# that was looked at, so a changed zip (a plugin account taken over, a
# release nobody read) never reaches the web root, where its PHP runs.
#
# To move a plugin to a new release, read its changelog, then run
# build/pin-wp-plugin.sh <slug> <version> and paste the line it prints
# here (docs/BUILDING.md, "Bumping a pinned input").
plugin_pin() {
    case "$1" in
        cache-enabler)
            echo "1.8.17 d57e23c3c1ca84fce4b14feeaaf493ba00f2a7e443bfbb7066b234285c7b7235" ;;
        internet-archive-wayback-machine-link-fixer)
            echo "1.4.2 874d7dc369acc8a363c8f0c73e0ba65e3b12d6fe366bf85985800de6eab91112" ;;
        *) return 1 ;;
    esac
}

if [ "$1" = "--pinned-version" ]; then
    PIN=$(plugin_pin "$2") || exit 1
    echo "${PIN%% *}"
    exit 0
fi

if [ ! -d "$DEST_DIR" ]; then
    echo "Error: Destination directory does not exist: $DEST_DIR"
    exit 1
fi

if ! PIN=$(plugin_pin "$PLUGIN_SLUG"); then
    echo "Error: ${PLUGIN_SLUG} is not a plugin OnionPress installs (no pinned release)"
    exit 1
fi
PIN_VERSION=${PIN%% *}
PIN_SHA256=${PIN#* }
if ! [[ "$PIN_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Error: no SHA256 pinned for ${PLUGIN_SLUG} ${PIN_VERSION} — refusing to install it unchecked (build/pin-wp-plugin.sh)"
    exit 1
fi

PLUGIN_URL="https://downloads.wordpress.org/plugin/${PLUGIN_SLUG}.${PIN_VERSION}.zip"
ZIP_PATH="${DEST_DIR}/${PLUGIN_SLUG}.zip"

echo "Downloading ${PLUGIN_SLUG} ${PIN_VERSION} from WordPress.org over Tor..."
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

if ! docker exec "$CONTAINER" true 2>/dev/null; then
    echo "Error: container ${CONTAINER} is not running — cannot download over Tor"
    exit 1
fi

# The zip lands in a folder of root's own (mktemp -d: mode 700), not at a
# guessable name in the container's /tmp, where www-data could put a link
# for root's curl to write through or swap the file before it is copied out.
REMOTE_DIR=$(docker exec "$CONTAINER" mktemp -d /tmp/onionpress-plugin-XXXXXX 2>/dev/null | tr -d '\r\n')
case "$REMOTE_DIR" in
    /tmp/onionpress-plugin-??????) ;;
    *) echo "Error: could not make a download folder in ${CONTAINER}"; exit 1 ;;
esac
REMOTE_ZIP="${REMOTE_DIR}/plugin.zip"

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
    docker exec "$CONTAINER" rm -rf "$REMOTE_DIR" 2>/dev/null
    exit 1
fi

if ! docker cp "${CONTAINER}:${REMOTE_ZIP}" "$ZIP_PATH" 2>&1; then
    echo "Error copying downloaded plugin out of ${CONTAINER}"
    docker exec "$CONTAINER" rm -rf "$REMOTE_DIR" 2>/dev/null
    exit 1
fi
docker exec "$CONTAINER" rm -rf "$REMOTE_DIR" 2>/dev/null

# Get file size
SIZE=$(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH" 2>/dev/null)
echo "Downloaded ${SIZE} bytes"

# Check the zip against the pinned release before unpacking anything.
if command -v shasum >/dev/null 2>&1; then
    SHA256=$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')
else
    SHA256=$(sha256sum "$ZIP_PATH" | awk '{print $1}')
fi
echo "SHA256: ${SHA256}"
if [ "$SHA256" != "$PIN_SHA256" ]; then
    echo "Error: ${PLUGIN_SLUG} ${PIN_VERSION} does not match its pinned SHA256 (expected ${PIN_SHA256}) — not installed"
    rm -f "$ZIP_PATH"
    exit 1
fi
echo "Verified against the pinned SHA256 for ${PLUGIN_SLUG} ${PIN_VERSION}"

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
