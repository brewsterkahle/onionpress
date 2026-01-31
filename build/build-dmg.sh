#!/bin/bash

# Build script for onion.press DMG installer

set -e

echo "Building onion.press DMG installer..."

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_PATH="$PROJECT_DIR/onion.press.app"
DMG_NAME="onion.press.dmg"
DMG_PATH="$BUILD_DIR/$DMG_NAME"
TEMP_DMG="$BUILD_DIR/temp.dmg"
VOLUME_NAME="onion.press"

echo "Project directory: $PROJECT_DIR"
echo "App path: $APP_PATH"

# Check if app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: onion.press.app not found at $APP_PATH"
    exit 1
fi

# Clean up old builds
echo "Cleaning up old builds..."
rm -f "$DMG_PATH" "$TEMP_DMG"

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

For more information, visit: https://github.com/yourusername/onion.press
EOF

# Calculate size needed for DMG
echo "Calculating DMG size..."
SIZE=$(du -sk "$TEMP_DIR" | cut -f1)
SIZE=$((SIZE / 1024 + 50))  # Convert to MB and add 50MB padding

echo "Creating DMG (${SIZE}MB)..."

# Create DMG using hdiutil
hdiutil create \
    -srcfolder "$TEMP_DIR" \
    -volname "$VOLUME_NAME" \
    -fs HFS+ \
    -fsargs "-c c=64,a=16,e=16" \
    -format UDRW \
    -size "${SIZE}m" \
    "$TEMP_DMG"

# Mount the DMG
echo "Mounting DMG for customization..."
MOUNT_DIR=$(hdiutil attach "$TEMP_DMG" | grep -o '/Volumes/.*$')

# Wait for mount
sleep 2

# Set custom icon positions and window properties
echo "Setting DMG window properties..."
osascript <<EOF
tell application "Finder"
    tell disk "$VOLUME_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {100, 100, 700, 500}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 96
        set background picture of viewOptions to file ".background:background.png"

        -- Set icon positions
        set position of item "onion.press.app" of container window to {150, 200}
        set position of item "Applications" of container window to {450, 200}

        close
        open
        update without registering applications
        delay 2
    end tell
end tell
EOF

# Sync filesystem
sync

# Unmount
echo "Unmounting DMG..."
hdiutil detach "$MOUNT_DIR"

# Convert to compressed read-only DMG
echo "Compressing DMG..."
hdiutil convert "$TEMP_DMG" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_PATH"

# Clean up
rm -f "$TEMP_DMG"
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
