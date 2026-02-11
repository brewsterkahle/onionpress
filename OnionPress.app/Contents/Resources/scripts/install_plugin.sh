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

echo "Downloading ${PLUGIN_SLUG} from WordPress.org..."
echo "URL: ${PLUGIN_URL}"

# Clear any inherited SSL overrides (e.g. py2app's __boot__.py) so curl
# uses the macOS system CA bundle from the keychain.
unset SSL_CERT_FILE SSL_CERT_DIR

# Download plugin zip
if ! curl -L -f --max-time 60 -o "$ZIP_PATH" "$PLUGIN_URL" 2>&1; then
    echo "Error downloading plugin"
    exit 1
fi

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
