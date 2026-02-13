#!/bin/bash

# Simple DMG build script for onionpress (without fancy customization)

set -e

echo "Building onionpress DMG installer (simple mode)..."

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_PATH="$PROJECT_DIR/OnionPress.app"
DMG_NAME="onionpress.dmg"
DMG_PATH="$BUILD_DIR/$DMG_NAME"

echo "Project directory: $PROJECT_DIR"
echo "App path: $APP_PATH"

# Check if app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: OnionPress.app not found at $APP_PATH"
    exit 1
fi

# Download and bundle Colima dependencies
echo "Downloading container runtime binaries..."
TEMP_BIN_DIR=$(mktemp -d)

# Version configuration
COLIMA_VERSION="v0.8.1"
LIMA_VERSION="2.0.3"
DOCKER_VERSION="27.5.1"
DOCKER_COMPOSE_VERSION="v2.32.4"
MKP224O_VERSION="master"  # Using master for latest version

# Download Colima for both architectures
echo "  Downloading Colima binaries..."
curl -L -o "$TEMP_BIN_DIR/colima-darwin-amd64" \
  "https://github.com/abiosoft/colima/releases/download/$COLIMA_VERSION/colima-Darwin-x86_64"
curl -L -o "$TEMP_BIN_DIR/colima-darwin-arm64" \
  "https://github.com/abiosoft/colima/releases/download/$COLIMA_VERSION/colima-Darwin-arm64"

chmod +x "$TEMP_BIN_DIR"/colima-*

# Create universal Colima binary
echo "  Creating universal Colima binary..."
lipo -create \
  "$TEMP_BIN_DIR/colima-darwin-arm64" \
  "$TEMP_BIN_DIR/colima-darwin-amd64" \
  -output "$TEMP_BIN_DIR/colima"

# Download Lima binaries
echo "  Downloading Lima binaries..."
curl -L -o "$TEMP_BIN_DIR/lima-amd64.tar.gz" \
  "https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Darwin-x86_64.tar.gz"
curl -L -o "$TEMP_BIN_DIR/lima-arm64.tar.gz" \
  "https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Darwin-arm64.tar.gz"

mkdir -p "$TEMP_BIN_DIR/lima-amd64" "$TEMP_BIN_DIR/lima-arm64"
tar xzf "$TEMP_BIN_DIR/lima-amd64.tar.gz" -C "$TEMP_BIN_DIR/lima-amd64"
tar xzf "$TEMP_BIN_DIR/lima-arm64.tar.gz" -C "$TEMP_BIN_DIR/lima-arm64"

# Create universal Lima binary
echo "  Creating universal limactl binary..."
lipo -create \
  "$TEMP_BIN_DIR/lima-arm64/bin/limactl" \
  "$TEMP_BIN_DIR/lima-amd64/bin/limactl" \
  -output "$TEMP_BIN_DIR/limactl"

# Download Docker CLI
echo "  Downloading Docker CLI binaries..."
curl -L -o "$TEMP_BIN_DIR/docker-amd64.tgz" \
  "https://download.docker.com/mac/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"
curl -L -o "$TEMP_BIN_DIR/docker-arm64.tgz" \
  "https://download.docker.com/mac/static/stable/aarch64/docker-${DOCKER_VERSION}.tgz"

mkdir -p "$TEMP_BIN_DIR/docker-amd64" "$TEMP_BIN_DIR/docker-arm64"
tar xzf "$TEMP_BIN_DIR/docker-amd64.tgz" -C "$TEMP_BIN_DIR/docker-amd64"
tar xzf "$TEMP_BIN_DIR/docker-arm64.tgz" -C "$TEMP_BIN_DIR/docker-arm64"

# Create universal Docker CLI binary
echo "  Creating universal Docker CLI binary..."
lipo -create \
  "$TEMP_BIN_DIR/docker-arm64/docker/docker" \
  "$TEMP_BIN_DIR/docker-amd64/docker/docker" \
  -output "$TEMP_BIN_DIR/docker"
rm -rf "$TEMP_BIN_DIR/docker-arm64" "$TEMP_BIN_DIR/docker-amd64"

# Download Docker Compose plugin for both architectures
echo "  Downloading Docker Compose plugin..."
curl -L -o "$TEMP_BIN_DIR/docker-compose-arm64" \
  "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-aarch64"
curl -L -o "$TEMP_BIN_DIR/docker-compose-x86_64" \
  "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-x86_64"

chmod +x "$TEMP_BIN_DIR"/docker-compose-*

# Create universal Docker Compose binary
echo "  Creating universal Docker Compose binary..."
lipo -create \
  "$TEMP_BIN_DIR/docker-compose-arm64" \
  "$TEMP_BIN_DIR/docker-compose-x86_64" \
  -output "$TEMP_BIN_DIR/docker-compose"

# Build mkp224o as a universal binary for vanity onion addresses
echo "  Building mkp224o for vanity onion addresses..."
if command -v git >/dev/null 2>&1; then
    # Clone mkp224o
    git clone https://github.com/cathugger/mkp224o.git "$TEMP_BIN_DIR/mkp224o-src" 2>/dev/null || true

    # Check for required dependencies
    if command -v brew >/dev/null 2>&1; then
        brew list libsodium >/dev/null 2>&1 || brew install libsodium
        brew list autoconf >/dev/null 2>&1 || brew install autoconf
        brew list automake >/dev/null 2>&1 || brew install automake
    fi

    SODIUM_PREFIX=$(brew --prefix libsodium 2>/dev/null)

    # Build libsodium from source for x86_64 (Homebrew only has native arch)
    echo "  Building libsodium for x86_64 cross-compilation..."
    SODIUM_X86_DIR="$TEMP_BIN_DIR/libsodium-x86_64"
    SODIUM_X86_SRC="$TEMP_BIN_DIR/libsodium-src"
    SODIUM_VERSION=$(pkg-config --modversion libsodium 2>/dev/null || echo "1.0.20")
    curl -L -o "$TEMP_BIN_DIR/libsodium.tar.gz" \
      "https://download.libsodium.org/libsodium/releases/libsodium-${SODIUM_VERSION}.tar.gz" 2>/dev/null || \
    curl -L -o "$TEMP_BIN_DIR/libsodium.tar.gz" \
      "https://download.libsodium.org/libsodium/releases/libsodium-1.0.20-stable.tar.gz" 2>/dev/null
    mkdir -p "$SODIUM_X86_SRC"
    tar xzf "$TEMP_BIN_DIR/libsodium.tar.gz" -C "$SODIUM_X86_SRC" --strip-components=1
    cd "$SODIUM_X86_SRC"
    ./configure --host=x86_64-apple-darwin --prefix="$SODIUM_X86_DIR" \
        --disable-shared --enable-static \
        CC="clang -arch x86_64" \
        CFLAGS="-arch x86_64 -mmacosx-version-min=13.0" \
        LDFLAGS="-arch x86_64" > /dev/null 2>&1
    make -j"$(sysctl -n hw.ncpu)" > /dev/null 2>&1
    make install > /dev/null 2>&1
    echo "  ✓ libsodium x86_64 built"

    # Run autogen once in the mkp224o source
    cd "$TEMP_BIN_DIR/mkp224o-src"
    ./autogen.sh > /dev/null 2>&1

    # Build mkp224o for arm64 (native)
    echo "  Building mkp224o for arm64..."
    MKP_ARM64_DIR="$TEMP_BIN_DIR/mkp224o-arm64"
    mkdir -p "$MKP_ARM64_DIR"
    cp -R "$TEMP_BIN_DIR/mkp224o-src"/* "$MKP_ARM64_DIR/"
    cd "$MKP_ARM64_DIR"
    CFLAGS="-arch arm64 -mmacosx-version-min=13.0 -I$SODIUM_PREFIX/include" \
        LDFLAGS="-arch arm64" \
        ./configure --host=aarch64-apple-darwin --enable-ref10 > /dev/null 2>&1
    sed -i.bak "s| -lsodium| ${SODIUM_PREFIX}/lib/libsodium.a|g" GNUmakefile
    make -j"$(sysctl -n hw.ncpu)" > /dev/null 2>&1
    echo "  ✓ mkp224o arm64 built"

    # Build mkp224o for x86_64 (cross-compile)
    echo "  Building mkp224o for x86_64..."
    MKP_X86_DIR="$TEMP_BIN_DIR/mkp224o-x86_64"
    mkdir -p "$MKP_X86_DIR"
    cp -R "$TEMP_BIN_DIR/mkp224o-src"/* "$MKP_X86_DIR/"
    cd "$MKP_X86_DIR"
    CFLAGS="-arch x86_64 -mmacosx-version-min=13.0 -I$SODIUM_X86_DIR/include" \
        LDFLAGS="-arch x86_64" \
        CC="clang -arch x86_64" \
        ./configure --host=x86_64-apple-darwin --enable-ref10 > /dev/null 2>&1
    sed -i.bak "s| -lsodium| ${SODIUM_X86_DIR}/lib/libsodium.a|g" GNUmakefile
    make -j"$(sysctl -n hw.ncpu)" > /dev/null 2>&1
    echo "  ✓ mkp224o x86_64 built"

    # Create universal binary
    if [ -f "$MKP_ARM64_DIR/mkp224o" ] && [ -f "$MKP_X86_DIR/mkp224o" ]; then
        lipo -create \
            "$MKP_ARM64_DIR/mkp224o" \
            "$MKP_X86_DIR/mkp224o" \
            -output "$TEMP_BIN_DIR/mkp224o"
        echo "  ✓ mkp224o universal binary created ($(lipo -archs "$TEMP_BIN_DIR/mkp224o"))"

        # Verify static linking
        if otool -L "$TEMP_BIN_DIR/mkp224o" | grep -q libsodium; then
            echo "  ⚠️  WARNING: mkp224o still has dynamic libsodium dependency"
        else
            echo "  ✓ mkp224o statically linked (no libsodium dependency)"
        fi
    elif [ -f "$MKP_ARM64_DIR/mkp224o" ]; then
        echo "  WARNING: x86_64 build failed, using arm64-only mkp224o"
        cp "$MKP_ARM64_DIR/mkp224o" "$TEMP_BIN_DIR/mkp224o"
    else
        echo "  WARNING: mkp224o build failed"
    fi

    cd "$TEMP_BIN_DIR"
else
    echo "  WARNING: git not found, skipping mkp224o build"
fi

# Copy to app bundle
BIN_DIR="$APP_PATH/Contents/Resources/bin"
mkdir -p "$BIN_DIR"

# Remove any leftover binaries from previous builds
rm -f "$BIN_DIR"/*-arm64 "$BIN_DIR"/*-x86_64 "$BIN_DIR"/x86_64-binaries.tar.gz "$BIN_DIR"/intel-binaries.b64 2>/dev/null || true

echo "Installing universal binaries to app bundle..."
for binary in colima limactl docker docker-compose; do
    cp "$TEMP_BIN_DIR/$binary" "$BIN_DIR/$binary"
    echo "  $binary installed ($(lipo -archs "$BIN_DIR/$binary"))"
done

# Copy mkp224o if it was built (native to build machine only)
if [ -f "$TEMP_BIN_DIR/mkp224o" ]; then
    cp "$TEMP_BIN_DIR/mkp224o" "$BIN_DIR/mkp224o"
    echo "  mkp224o installed successfully"
else
    echo "  WARNING: mkp224o not available"
fi

chmod +x "$BIN_DIR"/*

# Ad-hoc sign universal binaries
echo "Signing binaries..."
for binary in colima limactl docker docker-compose; do
    codesign -s - --force "$BIN_DIR/$binary"
done
if [ -f "$BIN_DIR/mkp224o" ]; then
    codesign -s - --force "$BIN_DIR/mkp224o"
fi

# Re-sign limactl with virtualization entitlement — required for Apple VZ framework
echo "Adding virtualization entitlement to limactl..."
VZ_ENTITLEMENTS=$(mktemp)
cat > "$VZ_ENTITLEMENTS" <<'VZEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.virtualization</key>
    <true/>
</dict>
</plist>
VZEOF
codesign -s - --entitlements "$VZ_ENTITLEMENTS" --force "$BIN_DIR/limactl"
rm "$VZ_ENTITLEMENTS"

# Create lima wrapper script
echo "Creating lima wrapper script..."
cat > "$BIN_DIR/lima" <<'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LIMACTL="$SCRIPT_DIR/limactl"
INSTANCE="${LIMA_INSTANCE:-colima}"
exec "$LIMACTL" shell "$INSTANCE" -- "$@"
EOF
chmod +x "$BIN_DIR/lima"

cd "$PROJECT_DIR"

# Copy Lima share files from both architectures (guest agent differs per arch)
echo "Copying Lima support files..."
SHARE_DIR="$APP_PATH/Contents/Resources/share/lima"
mkdir -p "$SHARE_DIR"
cp -R "$TEMP_BIN_DIR/lima-arm64/share/lima"/* "$SHARE_DIR/"
cp -R "$TEMP_BIN_DIR/lima-amd64/share/lima"/* "$SHARE_DIR/"

# Clean up temp directory
rm -rf "$TEMP_BIN_DIR"

echo "Container runtime binaries installed successfully"

# Build standalone MenubarApp using py2app
# This bundles Python + all dependencies into a self-contained .app so
# end users don't need Python/pip installed.
echo ""
echo "Building standalone MenubarApp with py2app..."
SCRIPTS_DIR="$PROJECT_DIR/src"
MENUBAR_BUILD_DIR=$(mktemp -d)

# Create a temporary venv for the py2app build (so we don't require
# py2app or other deps to be installed globally on the build machine)
# Use python.org universal2 Python so the built app runs on both arm64 and Intel.
# Falls back to system python3 if the universal build isn't installed.
UNIVERSAL_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
if [ -x "$UNIVERSAL_PYTHON" ]; then
    echo "Using universal2 Python: $UNIVERSAL_PYTHON"
    "$UNIVERSAL_PYTHON" -m venv "$MENUBAR_BUILD_DIR/venv"
else
    echo "WARNING: universal2 Python not found, using system python3 (app may be arm64-only)"
    python3 -m venv "$MENUBAR_BUILD_DIR/venv"
fi
"$MENUBAR_BUILD_DIR/venv/bin/pip" install --upgrade pip
"$MENUBAR_BUILD_DIR/venv/bin/pip" install py2app
"$MENUBAR_BUILD_DIR/venv/bin/pip" install -r "$SCRIPTS_DIR/requirements.txt"

# Copy local modules into the build venv's site-packages so py2app can find them.
# IMPORTANT: py2app does not reliably auto-detect local modules imported via
# runtime sys.path manipulation. We copy them here AND list them in the
# 'includes' option in setup.py as a belt-and-suspenders approach.
# If you add a new local .py module imported by menubar.py, you must:
#   1. Add it to the 'includes' list in setup.py
#   2. Add a cp line here
SITE_PACKAGES=$("$MENUBAR_BUILD_DIR/venv/bin/python3" -c "import site; print(site.getsitepackages()[0])")
cp "$SCRIPTS_DIR/key_manager.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/bip39_words.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/setup_window.py" "$SITE_PACKAGES/"

# Run py2app build using the root setup.py
cd "$PROJECT_DIR"
if ! "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
    --dist-dir "$MENUBAR_BUILD_DIR/dist" \
    --bdist-base "$MENUBAR_BUILD_DIR/build" \
    2>&1; then
    # py2app uses distutils.spawn(dry_run=...) which setuptools 81+ removed.
    # Retry with older setuptools until py2app ships a fix.
    echo "py2app failed — retrying with setuptools<81..."
    "$MENUBAR_BUILD_DIR/venv/bin/pip" install 'setuptools<81'
    rm -rf "$MENUBAR_BUILD_DIR/build" "$MENUBAR_BUILD_DIR/dist"
    "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
        --dist-dir "$MENUBAR_BUILD_DIR/dist" \
        --bdist-base "$MENUBAR_BUILD_DIR/build" \
        2>&1
fi

# Install the built MenubarApp into the app bundle
MENUBAR_APP_DIR="$APP_PATH/Contents/Resources/MenubarApp"
rm -rf "$MENUBAR_APP_DIR"
mv "$MENUBAR_BUILD_DIR/dist/menubar.app" "$MENUBAR_APP_DIR"

# Universal binaries in MenubarApp are fine — macOS runs the arm64 slice
# natively on Apple Silicon without triggering a Rosetta prompt.

# Verify key_manager was included
if grep -rq "key_manager" "$MENUBAR_APP_DIR/Contents/Resources/" 2>/dev/null; then
    echo "  key_manager: included"
else
    echo "  WARNING: key_manager may not be included in MenubarApp bundle!"
    echo "  Check setup.py 'includes' list."
fi

# Verify the built MenubarApp version matches src/menubar.py
EXPECTED_VERSION=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
BUILT_VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$MENUBAR_APP_DIR/Contents/Info.plist" 2>/dev/null)
if [ "$EXPECTED_VERSION" != "$BUILT_VERSION" ]; then
    echo "ERROR: Version mismatch! src/menubar.py has $EXPECTED_VERSION but built MenubarApp has $BUILT_VERSION"
    echo "The py2app build may have used stale source. Aborting."
    exit 1
fi
echo "  Version verified: $BUILT_VERSION"

cd "$PROJECT_DIR"
echo "Standalone MenubarApp built successfully"
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

# Generate DMG background image using the py2app build venv (still alive)
echo "Generating DMG background image..."
DMG_BG_DIR="$TEMP_DIR/.background"
mkdir -p "$DMG_BG_DIR"
LOGO_PATH="$PROJECT_DIR/assets/branding/logo.png"
"$MENUBAR_BUILD_DIR/venv/bin/pip" install Pillow >/dev/null 2>&1
"$MENUBAR_BUILD_DIR/venv/bin/python3" "$BUILD_DIR/create-dmg-background.py" \
    "$DMG_BG_DIR/dmg-background.png" \
    --logo "$LOGO_PATH" 2>&1 || {
    echo "WARNING: Could not generate DMG background"
    echo "         Building plain DMG instead"
    rm -rf "$DMG_BG_DIR"
}

# Now clean up the py2app build venv
rm -rf "$MENUBAR_BUILD_DIR"

echo "Creating styled DMG..."

# Calculate DMG size (app size + 20MB headroom)
APP_SIZE_KB=$(du -sk "$TEMP_DIR" | cut -f1)
DMG_SIZE_KB=$((APP_SIZE_KB + 20480))

# Step 1: Create read-write DMG
RW_DMG_PATH="$BUILD_DIR/onionpress-rw.dmg"
rm -f "$RW_DMG_PATH"
hdiutil create \
    -volname "OnionPress" \
    -srcfolder "$TEMP_DIR" \
    -ov \
    -format UDRW \
    -size "${DMG_SIZE_KB}k" \
    "$RW_DMG_PATH"

# Clean up source temp dir (contents are now in the DMG)
rm -rf "$TEMP_DIR"

# Step 2: Mount the read-write DMG
# Eject any existing volume with the same name to avoid collisions
echo "Mounting DMG for styling..."
hdiutil detach "/Volumes/OnionPress" -quiet 2>/dev/null || true
MOUNT_OUTPUT=$(hdiutil attach -readwrite -noverify -noautoopen "$RW_DMG_PATH")
DEVICE=$(echo "$MOUNT_OUTPUT" | grep '/dev/' | head -1 | awk '{print $1}')
MOUNT_POINT=$(echo "$MOUNT_OUTPUT" | grep '/Volumes/' | sed 's/.*\/Volumes/\/Volumes/')
# Extract just the volume name (basename of mount point)
VOL_NAME=$(basename "$MOUNT_POINT")

echo "  Mounted at: $MOUNT_POINT (volume: $VOL_NAME)"

# Step 3: Apply Finder styling via AppleScript (if background exists)
if [ -f "$MOUNT_POINT/.background/dmg-background.png" ]; then
    echo "Applying Finder window styling..."

    # Give Finder a moment to index the volume
    sleep 2

    osascript <<APPLESCRIPT
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {100, 100, 740, 580}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 128
        set background picture of viewOptions to file ".background:dmg-background.png"
        set position of item "OnionPress.app" of container window to {160, 300}
        set position of item "Applications" of container window to {480, 300}
        close
        open
        delay 2
        close
    end tell
end tell
APPLESCRIPT
    echo "  Finder styling applied"
else
    echo "  No background image found, skipping Finder styling"
fi

# Step 4: Finalize — set permissions and unmount
echo "Finalizing DMG..."
chmod -Rf go-w "$MOUNT_POINT" 2>/dev/null || true
sync
hdiutil detach "$MOUNT_POINT" -quiet

# Step 5: Convert to compressed read-only DMG
echo "Compressing DMG..."
rm -f "$DMG_PATH"
hdiutil convert "$RW_DMG_PATH" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_PATH"

# Clean up read-write DMG
rm -f "$RW_DMG_PATH"

# Get final size
FINAL_SIZE=$(du -h "$DMG_PATH" | cut -f1)

echo ""
echo "✅ DMG created successfully!"
echo "   Location: $DMG_PATH"
echo "   Size: $FINAL_SIZE"
echo ""
echo "To test the DMG:"
echo "   1. Open the DMG: open '$DMG_PATH'"
echo "   2. Drag OnionPress.app to Applications"
echo "   3. Launch from Applications folder"
echo ""
