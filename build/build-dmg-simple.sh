#!/bin/bash

# Simple DMG build script for onion.press (without fancy customization)

set -e

echo "Building onion.press DMG installer (simple mode)..."

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_PATH="$PROJECT_DIR/onion.press.app"
DMG_NAME="onion.press.dmg"
DMG_PATH="$BUILD_DIR/$DMG_NAME"

echo "Project directory: $PROJECT_DIR"
echo "App path: $APP_PATH"

# Check if app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: onion.press.app not found at $APP_PATH"
    exit 1
fi

# Clean up old builds
echo "Cleaning up old builds..."
rm -f "$DMG_PATH"

# Create temporary directory for DMG contents
TEMP_DIR=$(mktemp -d)
echo "Using temp directory: $TEMP_DIR"

# Copy app to temp directory
echo "Copying application bundle..."
cp -R "$APP_PATH" "$TEMP_DIR/"

# Create Applications symlink
echo "Creating Applications folder symlink..."
ln -s /Applications "$TEMP_DIR/Applications"

# Create a README for the DMG
cat > "$TEMP_DIR/README.txt" <<EOF
onion.press - WordPress + Tor Hidden Service

INSTALLATION:
1. Drag onion.press.app to the Applications folder
2. Open onion.press from Applications
3. Follow the on-screen setup instructions

REQUIREMENTS:
- macOS 11.0 or later
- Python 3 (built into macOS 12.3+)
- Internet connection for first-time setup

FIRST LAUNCH:
- If prompted, allow installation of OrbStack (container runtime)
- Download of WordPress containers (~1GB) will begin automatically
- Setup takes 3-5 minutes on first run
- Subsequent launches are instant

USAGE:
- onion.press appears in your menu bar (🧅 icon)
- Click the icon to copy your onion address
- Use Tor Browser to access your WordPress site

For more information and troubleshooting:
https://github.com/yourusername/onion.press
EOF

echo "Creating DMG..."

# Create compressed DMG directly
hdiutil create \
    -volname "onion.press" \
    -srcfolder "$TEMP_DIR" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"

# Clean up
rm -rf "$TEMP_DIR"

# Get final size
FINAL_SIZE=$(du -h "$DMG_PATH" | cut -f1)

echo ""
echo "✅ DMG created successfully!"
echo "   Location: $DMG_PATH"
echo "   Size: $FINAL_SIZE"
echo ""
echo "To test the DMG:"
echo "   1. Open the DMG: open '$DMG_PATH'"
echo "   2. Drag onion.press.app to Applications"
echo "   3. Launch from Applications folder"
echo ""
