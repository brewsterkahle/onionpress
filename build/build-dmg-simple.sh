#!/bin/bash

# Simple DMG build script for onion.press (without fancy customization)

set -e

echo "Building onion.press DMG installer (simple mode)..."

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_PATH="$PROJECT_DIR/Onion.Press.app"
DMG_NAME="onion.press.dmg"
DMG_PATH="$BUILD_DIR/$DMG_NAME"

echo "Project directory: $PROJECT_DIR"
echo "App path: $APP_PATH"

# Check if app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: Onion.Press.app not found at $APP_PATH"
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

# Create universal binary
echo "  Creating universal Colima binary..."
lipo -create \
  "$TEMP_BIN_DIR/colima-darwin-amd64" \
  "$TEMP_BIN_DIR/colima-darwin-arm64" \
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

# Create universal binaries for Lima components
echo "  Creating universal Lima binaries..."
for binary in limactl; do
  if [ -f "$TEMP_BIN_DIR/lima-amd64/bin/$binary" ]; then
    lipo -create \
      "$TEMP_BIN_DIR/lima-amd64/bin/$binary" \
      "$TEMP_BIN_DIR/lima-arm64/bin/$binary" \
      -output "$TEMP_BIN_DIR/$binary"
  fi
done

# Copy lima shell script (not a binary, just copy from arm64 version)
if [ -f "$TEMP_BIN_DIR/lima-arm64/bin/lima" ]; then
  cp "$TEMP_BIN_DIR/lima-arm64/bin/lima" "$TEMP_BIN_DIR/lima"
fi

# Download Docker CLI
echo "  Downloading Docker CLI binaries..."
curl -L -o "$TEMP_BIN_DIR/docker-amd64.tgz" \
  "https://download.docker.com/mac/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"
curl -L -o "$TEMP_BIN_DIR/docker-arm64.tgz" \
  "https://download.docker.com/mac/static/stable/aarch64/docker-${DOCKER_VERSION}.tgz"

mkdir -p "$TEMP_BIN_DIR/docker-amd64" "$TEMP_BIN_DIR/docker-arm64"
tar xzf "$TEMP_BIN_DIR/docker-amd64.tgz" -C "$TEMP_BIN_DIR/docker-amd64"
tar xzf "$TEMP_BIN_DIR/docker-arm64.tgz" -C "$TEMP_BIN_DIR/docker-arm64"

# Create universal binary for Docker CLI
echo "  Creating universal Docker CLI binary..."
lipo -create \
  "$TEMP_BIN_DIR/docker-amd64/docker/docker" \
  "$TEMP_BIN_DIR/docker-arm64/docker/docker" \
  -output "$TEMP_BIN_DIR/docker"

# Download Docker Compose plugin for both architectures
echo "  Downloading Docker Compose plugin..."
curl -L -o "$TEMP_BIN_DIR/docker-compose-amd64" \
  "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-x86_64"
curl -L -o "$TEMP_BIN_DIR/docker-compose-arm64" \
  "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-aarch64"

chmod +x "$TEMP_BIN_DIR"/docker-compose-*

# Create universal binary for Docker Compose
echo "  Creating universal Docker Compose binary..."
lipo -create \
  "$TEMP_BIN_DIR/docker-compose-amd64" \
  "$TEMP_BIN_DIR/docker-compose-arm64" \
  -output "$TEMP_BIN_DIR/docker-compose"

# Build mkp224o for vanity onion addresses
echo "  Building mkp224o for vanity onion addresses..."
if command -v git >/dev/null 2>&1; then
    # Clone mkp224o
    git clone https://github.com/cathugger/mkp224o.git "$TEMP_BIN_DIR/mkp224o-src" 2>/dev/null || true
    cd "$TEMP_BIN_DIR/mkp224o-src"

    # Check for required dependencies
    if command -v brew >/dev/null 2>&1; then
        # Ensure libsodium is installed
        brew list libsodium >/dev/null 2>&1 || brew install libsodium
        brew list autoconf >/dev/null 2>&1 || brew install autoconf
        brew list automake >/dev/null 2>&1 || brew install automake
    fi

    # Build mkp224o (use ref10 for ARM64 compatibility)
    # Statically link libsodium so the binary works without Homebrew installed
    SODIUM_PREFIX=$(brew --prefix libsodium 2>/dev/null)
    SODIUM_STATIC="$SODIUM_PREFIX/lib/libsodium.a"

    # Configure and build, forcing static libsodium linking
    if ./autogen.sh && \
       CFLAGS="-I$SODIUM_PREFIX/include" \
       ./configure --enable-ref10; then

        # Modify the Makefile to force static linking
        # Replace -lsodium with the full path to libsodium.a
        # Use word boundary to avoid replacing libsodium in paths
        sed -i.bak "s/ -lsodium/ ${SODIUM_STATIC////\\/}/g" GNUmakefile

        if make; then
            # Copy binary
            cp mkp224o "$TEMP_BIN_DIR/mkp224o"

            # Verify static linking worked
            echo "  Checking mkp224o dependencies:"
            if otool -L "$TEMP_BIN_DIR/mkp224o" | grep -q libsodium; then
                echo "  ⚠️  WARNING: mkp224o still has dynamic libsodium dependency"
                otool -L "$TEMP_BIN_DIR/mkp224o"
            else
                echo "  ✓ mkp224o successfully statically linked (no libsodium dependency)"
            fi
        else
            echo "  WARNING: mkp224o make failed"
        fi
    else
        echo "  WARNING: mkp224o configure failed. Vanity onion address generation will not be available."
        echo "  Users will get a random .onion address instead."
    fi
    cd "$TEMP_BIN_DIR"
else
    echo "  WARNING: git not found, skipping mkp224o build"
fi

# Copy to app bundle
BIN_DIR="$APP_PATH/Contents/Resources/bin"
mkdir -p "$BIN_DIR"

echo "Installing binaries to app bundle..."
cp "$TEMP_BIN_DIR/colima" "$BIN_DIR/colima"
cp "$TEMP_BIN_DIR/limactl" "$BIN_DIR/limactl"
cp "$TEMP_BIN_DIR/docker" "$BIN_DIR/docker"
cp "$TEMP_BIN_DIR/docker-compose" "$BIN_DIR/docker-compose"

# Copy mkp224o if it was built
if [ -f "$TEMP_BIN_DIR/mkp224o" ]; then
    cp "$TEMP_BIN_DIR/mkp224o" "$BIN_DIR/mkp224o"
    echo "  mkp224o installed successfully"
else
    echo "  WARNING: mkp224o not available"
fi

chmod +x "$BIN_DIR"/*

# Extract ARM64-only slices to prevent Rosetta emulation issues
# Note: We keep universal binaries in source but extract ARM64 for the app
# This prevents Lima/Colima from accidentally running under Rosetta
echo "Extracting ARM64 slices from universal binaries..."
cd "$BIN_DIR"
for binary in colima docker docker-compose limactl; do
    if file "$binary" | grep -q "universal"; then
        echo "  Extracting ARM64 slice for $binary"
        lipo "$binary" -thin arm64 -output "${binary}.arm64"
        mv "$binary" "${binary}.universal"
        mv "${binary}.arm64" "$binary"
        # Delete the universal binary to save space in DMG
        rm "${binary}.universal"
    fi
done

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

# Copy Lima share files
echo "Copying Lima support files..."
SHARE_DIR="$APP_PATH/Contents/Resources/share/lima"
mkdir -p "$SHARE_DIR"
cp -R "$TEMP_BIN_DIR/lima-arm64/share/lima"/* "$SHARE_DIR/"

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
python3 -m venv "$MENUBAR_BUILD_DIR/venv"
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

# Verify key_manager was included
if grep -rq "key_manager" "$MENUBAR_APP_DIR/Contents/Resources/" 2>/dev/null; then
    echo "  key_manager: included"
else
    echo "  WARNING: key_manager may not be included in MenubarApp bundle!"
    echo "  Check setup.py 'includes' list."
fi

cd "$PROJECT_DIR"
rm -rf "$MENUBAR_BUILD_DIR"
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

# Create a README for the DMG
cat > "$TEMP_DIR/README.txt" <<EOF
onion.press - WordPress + Tor Hidden Service

INSTALLATION:
1. Drag Onion.Press.app to the Applications folder
2. Open Onion.Press from Applications
3. Follow the on-screen setup instructions

REQUIREMENTS:
- macOS 13.0 (Ventura) or later
- Internet connection for first-time setup

WHAT'S INCLUDED:
- Colima container runtime (bundled)
- Docker CLI tools (bundled)
- All dependencies included - no external Docker installation needed!

FIRST LAUNCH:
- First launch will initialize the container runtime (~2-3 minutes)
- Download of WordPress containers (~1GB) will begin automatically
- Setup takes 3-5 minutes on first run
- Subsequent launches are instant

USAGE:
- onion.press appears in your menu bar (OP icon)
- Click the icon to view your onion address
- Use Tor Browser to access your WordPress site

NOTES:
- This app uses Apple's native virtualization framework (VZ)
- All container data is isolated and stored in ~/.onion.press/
- No need to install Docker Desktop separately

For more information and troubleshooting:
https://github.com/brewsterkahle/onion.press
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
echo "   2. Drag Onion.Press.app to Applications"
echo "   3. Launch from Applications folder"
echo ""
