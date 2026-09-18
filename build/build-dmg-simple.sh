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

# strip_pycache <dir> — remove __pycache__/ trees and stray *.pyc files from a
# tree copied out of the source checkout. `make test-unit` leaves __pycache__
# under src/onionpress/ and app/Resources/docker/tor/ (tests/test_onionnames.py
# and tests/test_onionheaven_integration.py import from there), and `cp -R`
# ships them: a DMG built right after the tests carried 31 .pyc files that a
# clean-tree build did not. Same fix as build/build-linux.sh's collect_files.
# Only point this at our own copies — never at the py2app output, which ships
# a byte-compiled site.pyc next to __boot__.py on purpose.
strip_pycache() {
    find "$1" -type d -name __pycache__ -prune -exec rm -rf {} +
    find "$1" -type f -name '*.pyc' -delete
}

# Assemble OnionPress.app from app/ source directory
echo "Assembling OnionPress.app from app/ source..."
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

cp "$PROJECT_DIR/app/Info.plist" "$APP_PATH/Contents/"
cp "$PROJECT_DIR/app/MacOS/onionpress" "$APP_PATH/Contents/MacOS/"
cp "$PROJECT_DIR/app/MacOS/launcher.sh" "$APP_PATH/Contents/MacOS/"
cp "$PROJECT_DIR/app/MacOS/torcurl" "$APP_PATH/Contents/MacOS/"
chmod +x "$APP_PATH/Contents/MacOS/onionpress" "$APP_PATH/Contents/MacOS/launcher.sh" \
         "$APP_PATH/Contents/MacOS/torcurl"

# Compile Swift launcher from source
echo "  Compiling launcher from launcher-wrapper.swift..."
swiftc -O \
    -target arm64-apple-macos13 \
    "$PROJECT_DIR/app/MacOS/launcher-wrapper.swift" \
    -o "$APP_PATH/Contents/MacOS/launcher-arm64"
swiftc -O \
    -target x86_64-apple-macos13 \
    "$PROJECT_DIR/app/MacOS/launcher-wrapper.swift" \
    -o "$APP_PATH/Contents/MacOS/launcher-x86_64"
lipo -create \
    "$APP_PATH/Contents/MacOS/launcher-arm64" \
    "$APP_PATH/Contents/MacOS/launcher-x86_64" \
    -output "$APP_PATH/Contents/MacOS/launcher"
rm "$APP_PATH/Contents/MacOS/launcher-arm64" "$APP_PATH/Contents/MacOS/launcher-x86_64"
echo "  launcher compiled"

cp -R "$PROJECT_DIR/app/Resources/docker" "$APP_PATH/Contents/Resources/docker"
cp -R "$PROJECT_DIR/app/Resources/plugins" "$APP_PATH/Contents/Resources/plugins"
cp -R "$PROJECT_DIR/app/Resources/themes" "$APP_PATH/Contents/Resources/themes"
cp -R "$PROJECT_DIR/app/Resources/scripts" "$APP_PATH/Contents/Resources/scripts"
strip_pycache "$APP_PATH/Contents/Resources"
cp "$PROJECT_DIR/app/Resources/"*.png "$APP_PATH/Contents/Resources/"
cp "$PROJECT_DIR/app/Resources/AppIcon.icns" "$APP_PATH/Contents/Resources/"
cp "$PROJECT_DIR/app/Resources/config-template.txt" "$APP_PATH/Contents/Resources/"
cp "$PROJECT_DIR/app/Resources/settings.html" "$APP_PATH/Contents/Resources/"
cp "$PROJECT_DIR/app/Resources/logo.png" "$APP_PATH/Contents/Resources/"

# Copy the menubar entry point into the bundle's scripts dir (unused at
# runtime per the note below, kept for hot-patch / debugging).
mkdir -p "$APP_PATH/Contents/Resources/scripts"
cp "$PROJECT_DIR/src/menubar.py" "$APP_PATH/Contents/Resources/scripts/"
cp "$PROJECT_DIR/src/onionpress/key_manager.py" "$APP_PATH/Contents/Resources/scripts/"

echo "OnionPress.app assembled from app/ source"

# Download and bundle Colima dependencies
echo "Downloading container runtime binaries..."
TEMP_BIN_DIR=$(mktemp -d)

# Version configuration
COLIMA_VERSION="v0.8.1"
LIMA_VERSION="2.0.3"
DOCKER_VERSION="27.5.1"
DOCKER_COMPOSE_VERSION="v2.40.2"  # >=2.40.2 closes CVE-2025-62725 (HIGH)
MKP224O_VERSION="v1.7.0"

# GitHub anonymously throttles release-asset downloads to ~30 KB/s, which
# turns the bundling step below into a 20+ minute affair. Authenticated
# requests average ~4-7 MB/s but GitHub's CDN sometimes pins a connection
# to a slow edge (observed 20 KB/s sustained for minutes). Reconnecting
# picks a fresh — usually faster — edge. Strategy:
#   * --speed-limit 100000 --speed-time 30: abort if throughput drops
#     below 100 KB/s for 30s (slow but usable passes; truly stuck aborts).
#   * --retry 5 --retry-all-errors --retry-delay 5: retry on any failure
#     including speed-time timeouts (plain --retry won't). Five attempts
#     at 5s delay = 25s total retry budget, plenty to cycle CDN edges.
# Authenticated via `gh` token if available, unauthenticated otherwise.
GH_DL=(--speed-limit 100000 --speed-time 30 --retry 5 --retry-all-errors --retry-delay 5)
if command -v gh >/dev/null 2>&1; then
    _gh_token=$(gh auth token 2>/dev/null || true)
    if [ -n "$_gh_token" ]; then
        GH_DL+=(-H "Authorization: Bearer $_gh_token")
    fi
fi

# Versioned binary cache. These binaries are static artifacts of pinned
# versions — once we've downloaded and lipo'd colima-v0.8.1 into a
# universal binary, it's bit-identical for every subsequent build. Cache
# keyed by version saves ~3-5 min per iteration. Gitignored; safe to
# rm -rf to force re-downloads.
CACHE_DIR="$PROJECT_DIR/build/.cache/bin"
mkdir -p "$CACHE_DIR"

# cache_get <cache-name> <dest-path> — copy from cache if present, return
# 0 on hit, 1 on miss. Caller handles miss by downloading/building.
cache_get() {
    local name="$1" dest="$2"
    if [ -f "$CACHE_DIR/$name" ]; then
        cp "$CACHE_DIR/$name" "$dest"
        return 0
    fi
    return 1
}

# cache_put <cache-name> <source-path> — copy fresh artifact into cache.
cache_put() {
    local name="$1" src="$2"
    [ -f "$src" ] && cp "$src" "$CACHE_DIR/$name"
}

# Colima universal binary (download both arches, lipo, cache result)
if cache_get "colima-${COLIMA_VERSION}-universal" "$TEMP_BIN_DIR/colima"; then
    echo "  Colima ${COLIMA_VERSION}: cache hit"
else
    echo "  Downloading Colima ${COLIMA_VERSION}..."
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/colima-darwin-amd64" \
      "https://github.com/abiosoft/colima/releases/download/$COLIMA_VERSION/colima-Darwin-x86_64"
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/colima-darwin-arm64" \
      "https://github.com/abiosoft/colima/releases/download/$COLIMA_VERSION/colima-Darwin-arm64"
    chmod +x "$TEMP_BIN_DIR"/colima-*
    lipo -create \
      "$TEMP_BIN_DIR/colima-darwin-arm64" \
      "$TEMP_BIN_DIR/colima-darwin-amd64" \
      -output "$TEMP_BIN_DIR/colima"
    cache_put "colima-${COLIMA_VERSION}-universal" "$TEMP_BIN_DIR/colima"
fi

# Lima: two things to cache — the universal limactl binary AND the share/
# tree (guest agents differ per arch, copied later from both amd64 and
# arm64 extracted dirs). Cache them together as a single .tar.gz so a
# single cache hit gives us everything.
mkdir -p "$TEMP_BIN_DIR/lima-amd64" "$TEMP_BIN_DIR/lima-arm64"
if cache_get "lima-${LIMA_VERSION}-bundle.tar.gz" "$TEMP_BIN_DIR/lima-bundle.tar.gz"; then
    echo "  Lima ${LIMA_VERSION}: cache hit"
    tar xzf "$TEMP_BIN_DIR/lima-bundle.tar.gz" -C "$TEMP_BIN_DIR"
else
    echo "  Downloading Lima ${LIMA_VERSION}..."
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/lima-amd64.tar.gz" \
      "https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Darwin-x86_64.tar.gz"
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/lima-arm64.tar.gz" \
      "https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Darwin-arm64.tar.gz"
    tar xzf "$TEMP_BIN_DIR/lima-amd64.tar.gz" -C "$TEMP_BIN_DIR/lima-amd64"
    tar xzf "$TEMP_BIN_DIR/lima-arm64.tar.gz" -C "$TEMP_BIN_DIR/lima-arm64"
    lipo -create \
      "$TEMP_BIN_DIR/lima-arm64/bin/limactl" \
      "$TEMP_BIN_DIR/lima-amd64/bin/limactl" \
      -output "$TEMP_BIN_DIR/limactl"
    # Bundle for cache: limactl + minimal share trees (guest-agent binaries
    # are the only arch-specific pieces; the rest is identical).
    (cd "$TEMP_BIN_DIR" && tar czf lima-bundle.tar.gz \
        limactl \
        lima-amd64/share/lima \
        lima-arm64/share/lima)
    cache_put "lima-${LIMA_VERSION}-bundle.tar.gz" "$TEMP_BIN_DIR/lima-bundle.tar.gz"
fi

# Docker CLI universal binary
if cache_get "docker-${DOCKER_VERSION}-universal" "$TEMP_BIN_DIR/docker"; then
    echo "  Docker ${DOCKER_VERSION}: cache hit"
else
    echo "  Downloading Docker CLI ${DOCKER_VERSION}..."
    curl -L -o "$TEMP_BIN_DIR/docker-amd64.tgz" \
      "https://download.docker.com/mac/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"
    curl -L -o "$TEMP_BIN_DIR/docker-arm64.tgz" \
      "https://download.docker.com/mac/static/stable/aarch64/docker-${DOCKER_VERSION}.tgz"
    mkdir -p "$TEMP_BIN_DIR/docker-amd64" "$TEMP_BIN_DIR/docker-arm64"
    tar xzf "$TEMP_BIN_DIR/docker-amd64.tgz" -C "$TEMP_BIN_DIR/docker-amd64"
    tar xzf "$TEMP_BIN_DIR/docker-arm64.tgz" -C "$TEMP_BIN_DIR/docker-arm64"
    lipo -create \
      "$TEMP_BIN_DIR/docker-arm64/docker/docker" \
      "$TEMP_BIN_DIR/docker-amd64/docker/docker" \
      -output "$TEMP_BIN_DIR/docker"
    rm -rf "$TEMP_BIN_DIR/docker-arm64" "$TEMP_BIN_DIR/docker-amd64"
    cache_put "docker-${DOCKER_VERSION}-universal" "$TEMP_BIN_DIR/docker"
fi

# Docker Compose universal binary
if cache_get "docker-compose-${DOCKER_COMPOSE_VERSION}-universal" "$TEMP_BIN_DIR/docker-compose"; then
    echo "  Docker Compose ${DOCKER_COMPOSE_VERSION}: cache hit"
else
    echo "  Downloading Docker Compose ${DOCKER_COMPOSE_VERSION}..."
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/docker-compose-arm64" \
      "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-aarch64"
    curl -L "${GH_DL[@]}" -o "$TEMP_BIN_DIR/docker-compose-x86_64" \
      "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-darwin-x86_64"
    chmod +x "$TEMP_BIN_DIR"/docker-compose-*
    lipo -create \
      "$TEMP_BIN_DIR/docker-compose-arm64" \
      "$TEMP_BIN_DIR/docker-compose-x86_64" \
      -output "$TEMP_BIN_DIR/docker-compose"
    cache_put "docker-compose-${DOCKER_COMPOSE_VERSION}-universal" "$TEMP_BIN_DIR/docker-compose"
fi

# Build mkp224o as a universal binary for custom onion address prefixes.
# Skip the full source build + cross-compile (~30s + libsodium crosscomp)
# when we already have a cached universal binary for this pinned tag.
if cache_get "mkp224o-${MKP224O_VERSION}-universal" "$TEMP_BIN_DIR/mkp224o"; then
    echo "  mkp224o ${MKP224O_VERSION}: cache hit"
elif command -v git >/dev/null 2>&1; then
    echo "  Building mkp224o ${MKP224O_VERSION} for custom onion address prefixes..."
    # Run the entire mkp224o build in a subshell so any failure is isolated
    # and does not abort the DMG build via set -e in the outer shell.
    (
    set -e
    # Clone mkp224o at the pinned tag — shallow clone, saves most of the
    # git fetch cost relative to a full clone of the master history.
    git clone --branch "${MKP224O_VERSION}" --depth 1 \
        https://github.com/cathugger/mkp224o.git "$TEMP_BIN_DIR/mkp224o-src" 2>/dev/null || \
        git clone https://github.com/cathugger/mkp224o.git "$TEMP_BIN_DIR/mkp224o-src" 2>/dev/null

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
        cache_put "mkp224o-${MKP224O_VERSION}-universal" "$TEMP_BIN_DIR/mkp224o"
    elif [ -f "$MKP_ARM64_DIR/mkp224o" ]; then
        echo "  WARNING: x86_64 build failed, using arm64-only mkp224o"
        cp "$MKP_ARM64_DIR/mkp224o" "$TEMP_BIN_DIR/mkp224o"
    else
        echo "  WARNING: mkp224o build failed" >&2
        exit 1
    fi
    ) || echo "  WARNING: mkp224o build failed — vanity address generation unavailable"

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

# Copy mkp224o into the bundle. This binary is REQUIRED: on first run,
# app/MacOS/onionpress -> generate_vanity_address() execs $BIN_DIR/mkp224o
# to mint the vanity (op2…) address. If it's missing, generate_vanity_address
# returns failure and the install SILENTLY falls back to a random .onion —
# exactly the regression shipped in 2.4.101. A missing mkp224o must abort the
# DMG, not warn, so a flaky cross-compile can never ship a vanity-less build.
if [ -f "$TEMP_BIN_DIR/mkp224o" ]; then
    cp "$TEMP_BIN_DIR/mkp224o" "$BIN_DIR/mkp224o"
    echo "  mkp224o installed successfully"
else
    echo "ERROR: mkp224o not available — refusing to build a DMG without it." >&2
    echo "  Every fresh install would silently get a RANDOM .onion instead of" >&2
    echo "  a vanity address. Fix the mkp224o build step above and rebuild." >&2
    exit 1
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
# py2app or other deps to be installed globally on the build machine).
#
# Python version selection, in order of preference:
#   1. python.org universal2 Python 3.14 — ships a universal (arm64 + x86_64)
#      binary, required for release builds that must run on Intel too.
#   2. uv-managed Python 3.14 — single-arch but reproducible; good for local
#      dev on Apple Silicon where the app only needs to run on the dev box.
#   3. HARD FAIL — do not fall back to /usr/bin/python3: on macOS 13/14 that
#      is 3.9, and src/onionpress/ uses PEP 604 `X | None` annotations that
#      fail to import on 3.9 (py2app freezes bytecode against the build
#      interpreter, so the shipped app would crash on launch with a cryptic
#      "Launch error / See the py2app website" dialog).
UNIVERSAL_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
if [ -x "$UNIVERSAL_PYTHON" ]; then
    echo "Using universal2 Python: $UNIVERSAL_PYTHON"
    "$UNIVERSAL_PYTHON" -m venv "$MENUBAR_BUILD_DIR/venv"
elif command -v uv >/dev/null 2>&1; then
    echo "Using uv-managed Python 3.14 (single-arch — local dev build)"
    UV_PYTHON=$(uv python find 3.14 2>/dev/null || true)
    if [ -z "$UV_PYTHON" ]; then
        echo "Installing Python 3.14 via uv..."
        uv python install 3.14
        UV_PYTHON=$(uv python find 3.14)
    fi
    "$UV_PYTHON" -m venv "$MENUBAR_BUILD_DIR/venv"
else
    echo "ERROR: no Python 3.14 found." >&2
    echo "  For release builds, install python.org universal2 Python 3.14:" >&2
    echo "    https://www.python.org/downloads/" >&2
    echo "  For local dev builds, install uv:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "" >&2
    echo "  Refusing to fall back to /usr/bin/python3 (3.9) — the shipped .app" >&2
    echo "  would crash on launch with a py2app 'Launch error' dialog because" >&2
    echo "  src/onionpress/ uses PEP 604 \`X | None\` syntax." >&2
    exit 1
fi
"$MENUBAR_BUILD_DIR/venv/bin/pip" install --upgrade pip
"$MENUBAR_BUILD_DIR/venv/bin/pip" install py2app
"$MENUBAR_BUILD_DIR/venv/bin/pip" install -r "$SCRIPTS_DIR/requirements.txt"

# Copy the `onionpress` package into the build venv's site-packages so
# py2app can resolve the `onionpress.*` entries in setup.py's `includes`.
# All shared code lives inside the package now — no flat-module cp dance.
SITE_PACKAGES=$("$MENUBAR_BUILD_DIR/venv/bin/python3" -c "import site; print(site.getsitepackages()[0])")
cp -r "$SCRIPTS_DIR/onionpress" "$SITE_PACKAGES/"
# py2app copies this tree into the bundle verbatim, __pycache__ included.
strip_pycache "$SITE_PACKAGES/onionpress"

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
# py2app names the .app from CFBundleName (OnionPress.app) or script name (menubar.app)
if [ -d "$MENUBAR_BUILD_DIR/dist/OnionPress.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_APP_DIR"
elif [ -d "$MENUBAR_BUILD_DIR/dist/menubar.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/menubar.app" "$MENUBAR_APP_DIR"
else
    echo "ERROR: py2app output not found in $MENUBAR_BUILD_DIR/dist/"
    ls "$MENUBAR_BUILD_DIR/dist/"
    exit 1
fi

# Remove broken .pyo symlinks — py2app creates these but .pyo files
# haven't existed since Python 3.5. They break xattr/gatekeeper stripping.
find "$MENUBAR_APP_DIR" -name '*.pyo' -type l ! -exec test -e {} \; -delete

# No __pycache__/ anywhere in the bundle, whatever copied it in. A bytecode
# cache is only ever read next to its .py source, so dropping one cannot break
# an import — Python just recompiles on first use. Runs before signing so the
# seal covers the swept tree. Stray *.pyc are deliberately NOT swept here: the
# py2app output legitimately contains site.pyc (see strip_pycache above).
find "$APP_PATH" -type d -name __pycache__ -prune -exec rm -rf {} +

# Universal binaries in MenubarApp are fine — macOS runs the arm64 slice
# natively on Apple Silicon without triggering a Rosetta prompt.

# Verify key_manager.py is present at the path the shell launcher expects —
# app/MacOS/onionpress invokes "$SCRIPTS_DIR/key_manager.py" directly, so the
# file itself must exist there (the previous check just grepped for the
# string anywhere in MenubarApp, which passed even when the script was
# missing and vanity-key import silently fell back to a random address).
if [ -f "$APP_PATH/Contents/Resources/scripts/key_manager.py" ]; then
    echo "  key_manager.py: present at Contents/Resources/scripts/"
else
    echo "  ERROR: key_manager.py missing from Contents/Resources/scripts/" >&2
    echo "  Vanity onion generation will silently fall back to a random address." >&2
    exit 1
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

# Ad-hoc sign the entire app bundle (inside-out)
# This ensures macOS treats the app consistently across multiple users.
# NOTE: No symlinks may exist at the bundle root (outside Contents/) —
# codesign rejects them as "unsealed contents" and Gatekeeper reports "damaged".
echo "Signing application bundle..."
# Sign all .so extension modules in MenubarApp
find "$MENUBAR_APP_DIR/Contents/Resources/lib" -name "*.so" -exec codesign -f -s - {} \; 2>/dev/null
# Sign dylibs and frameworks
find "$MENUBAR_APP_DIR/Contents/Frameworks" -type f \( -name "*.dylib" -o -name "Python" \) -exec codesign -f -s - {} \; 2>/dev/null
codesign -f -s - "$MENUBAR_APP_DIR/Contents/Frameworks/Python.framework" 2>/dev/null || true
# Sign MenubarApp executables and bundle
codesign -f -s - "$MENUBAR_APP_DIR/Contents/MacOS/python" 2>/dev/null || true
codesign -f -s - "$MENUBAR_APP_DIR" 2>/dev/null || true
# Sign the outer OnionPress.app bundle
codesign -f -s - --deep "$APP_PATH"
echo "Application bundle signed"

# Clean up old builds
echo "Cleaning up old builds..."
rm -f "$DMG_PATH"

# Create temporary directory for DMG contents
TEMP_DIR=$(mktemp -d)
echo "Using temp directory: $TEMP_DIR"

# Copy app to temp directory
echo "Copying application bundle..."
cp -R "$APP_PATH" "$TEMP_DIR/"

# Re-sign the copy going into the DMG — version bumps in the repo often edit
# Info.plist after the last build, which invalidates the ad-hoc signature.
# Signing here ensures the DMG always ships a valid bundle.
echo "Re-signing app bundle for DMG..."
DMG_APP="$TEMP_DIR/$(basename "$APP_PATH")"
DMG_MENUBAR="$DMG_APP/Contents/Resources/MenubarApp"
# Remove any bundle-root symlinks that would cause "unsealed contents" error
find "$DMG_APP" -maxdepth 1 -type l -delete
find "$DMG_MENUBAR/Contents/Resources/lib" -name "*.so" -exec codesign -f -s - {} \; 2>/dev/null
find "$DMG_MENUBAR/Contents/Frameworks" -type f \( -name "*.dylib" -o -name "Python" \) -exec codesign -f -s - {} \; 2>/dev/null
codesign -f -s - "$DMG_MENUBAR/Contents/Frameworks/Python.framework" 2>/dev/null || true
codesign -f -s - "$DMG_MENUBAR/Contents/MacOS/python" 2>/dev/null || true
codesign -f -s - "$DMG_MENUBAR" 2>/dev/null || true
codesign -f -s - --deep "$DMG_APP"
echo "App bundle re-signed"

# Create Applications symlink
echo "Creating Applications folder symlink..."
ln -s /Applications "$TEMP_DIR/Applications"

# DMG background and styling — use pre-baked assets from build/dmg-assets/
# These were captured from a correctly-styled DMG and avoid the need for
# Finder AppleScript automation (which requires special macOS permissions).
DMG_ASSETS_DIR="$BUILD_DIR/dmg-assets"
DMG_BG_DIR="$TEMP_DIR/.background"
if [ -f "$DMG_ASSETS_DIR/dmg-background.png" ]; then
    echo "Using pre-baked DMG background image..."
    mkdir -p "$DMG_BG_DIR"
    cp "$DMG_ASSETS_DIR/dmg-background.png" "$DMG_BG_DIR/dmg-background.png"
else
    # Fallback: generate background dynamically (requires Pillow)
    echo "Generating DMG background image..."
    mkdir -p "$DMG_BG_DIR"
    LOGO_PATH="$PROJECT_DIR/assets/branding/logo.png"
    STORY_PATH="$PROJECT_DIR/assets/branding/story.png"
    "$MENUBAR_BUILD_DIR/venv/bin/pip" install Pillow >/dev/null 2>&1
    "$MENUBAR_BUILD_DIR/venv/bin/python3" "$BUILD_DIR/create-dmg-background.py" \
        "$DMG_BG_DIR/dmg-background.png" \
        --logo "$LOGO_PATH" \
        --story "$STORY_PATH" 2>&1 || {
        echo "WARNING: Could not generate DMG background"
        echo "         Building plain DMG instead"
        rm -rf "$DMG_BG_DIR"
    }
fi

# Now clean up the py2app build venv
rm -rf "$MENUBAR_BUILD_DIR"

echo "Creating styled DMG..."

# ─── Disk image tooling ──────────────────────────────────────────────────
# `diskutil image` arrived with macOS 26, and on macOS 27 the hdiutil verbs
# this script used — create -srcfolder, attach, detach, convert — each print
# a deprecation warning naming their replacement (hdiutil(1) carries the
# full verb-to-verb table). diskutil is used when the host has it; older
# hosts fall back to hdiutil so the .dmg still builds on the macOS 13+ the
# docs promise. DMG_TOOL=hdiutil forces the fallback — that is how it stays
# testable on a current macOS, where hdiutil still works.
#
# Both paths were run on macOS 27 and their output compared: an APFS volume
# named OnionPress either way (hdiutil defaults to APFS on 27 as well),
# identical file contents and permissions, the pre-baked .DS_Store byte-for-
# byte with its background alias resolving on the mounted volume, and a
# CRC32-checksummed UDZO image that `hdiutil attach` (still used by the
# in-app updater) mounts. The one difference: diskutil exposes no zlib
# level, so its image is ~1 MB (0.8%) larger than hdiutil's zlib-level=9.
if [ -z "${DMG_TOOL:-}" ]; then
    if diskutil image --help >/dev/null 2>&1; then
        DMG_TOOL=diskutil
    else
        DMG_TOOL=hdiutil
    fi
fi
case "$DMG_TOOL" in
    diskutil|hdiutil) echo "  Disk image tool: $DMG_TOOL" ;;
    *) echo "ERROR: DMG_TOOL must be 'diskutil' or 'hdiutil', not '$DMG_TOOL'" >&2; exit 1 ;;
esac

# dmg_create_rw <source-folder> <image-path> — a read-write image holding
# the folder's contents, its volume named OnionPress. The volume name is
# load-bearing: the alias inside build/dmg-assets/DS_Store points at
# /Volumes/OnionPress/.background/dmg-background.png. diskutil's RAW is the
# format hdiutil calls UDRW (imageinfo reports it as such). diskutil takes
# no size and leaves ~5% free (18 MB on a 380 MB payload) — ample for the
# 10 KB .DS_Store written after mounting; hdiutil needs the size spelled
# out, so that path keeps the old app-size-plus-80 MB headroom.
dmg_create_rw() {
    if [ "$DMG_TOOL" = diskutil ]; then
        diskutil image create from --format RAW --volumeName "OnionPress" "$1" "$2"
    else
        hdiutil create -volname "OnionPress" -srcfolder "$1" -ov -format UDRW \
            -size "$(( $(du -sk "$1" | cut -f1) + 81920 ))k" "$2"
    fi
}

# dmg_attach <image-path> — mount read-write and print the mount point.
# diskutil prints one tab-separated line per entity: device, content hint,
# and — on the line for a mounted filesystem — the mount point. A RAW image
# mounts read-write by default and has no checksum to verify, and Finder
# only auto-opens volumes blessed for it, so hdiutil's -readwrite -noverify
# -noautoopen need no counterpart.
dmg_attach() {
    local out
    if [ "$DMG_TOOL" = diskutil ]; then
        out=$(diskutil image attach "$1")
        printf '%s\n' "$out" | awk -F'\t' '$NF ~ /^\/Volumes\// { print $NF; exit }'
    else
        out=$(hdiutil attach -readwrite -noverify -noautoopen "$1")
        printf '%s\n' "$out" | grep '/Volumes/' | sed 's/.*\/Volumes/\/Volumes/'
    fi
}

# dmg_eject <mount-point>
dmg_eject() {
    if [ "$DMG_TOOL" = diskutil ]; then
        diskutil eject "$1" >/dev/null
    else
        hdiutil detach "$1" -quiet
    fi
}

# dmg_compress <rw-image> <output> — read-only, zlib-compressed UDZO.
dmg_compress() {
    if [ "$DMG_TOOL" = diskutil ]; then
        diskutil image create from --format UDZO "$1" "$2"
    else
        hdiutil convert "$1" -format UDZO -imagekey zlib-level=9 -o "$2"
    fi
}

# Step 1: Create read-write DMG from the staged folder
RW_DMG_PATH="$BUILD_DIR/onionpress-rw.dmg"
rm -f "$RW_DMG_PATH"
dmg_create_rw "$TEMP_DIR" "$RW_DMG_PATH"

# Clean up source temp dir (contents are now in the DMG)
rm -rf "$TEMP_DIR"

# Step 2: Mount the read-write DMG
# Eject any existing volume with the same name to avoid collisions
echo "Mounting DMG for styling..."
dmg_eject "/Volumes/OnionPress" 2>/dev/null || true
MOUNT_POINT=$(dmg_attach "$RW_DMG_PATH")
if [ -z "$MOUNT_POINT" ] || [ ! -d "$MOUNT_POINT" ]; then
    echo "ERROR: $DMG_TOOL attached $RW_DMG_PATH but reported no /Volumes mount point" >&2
    exit 1
fi
# Extract just the volume name (basename of mount point)
VOL_NAME=$(basename "$MOUNT_POINT")

echo "  Mounted at: $MOUNT_POINT (volume: $VOL_NAME)"

# Step 3: Apply DMG styling — use pre-baked .DS_Store if available,
# fall back to AppleScript (requires Finder automation permission).
if [ -f "$DMG_ASSETS_DIR/DS_Store" ]; then
    echo "Applying pre-baked DMG styling..."
    cp "$DMG_ASSETS_DIR/DS_Store" "$MOUNT_POINT/.DS_Store"
    echo "  Styling applied from build/dmg-assets/DS_Store"
elif [ -f "$MOUNT_POINT/.background/dmg-background.png" ]; then
    echo "Applying Finder window styling via AppleScript..."
    sleep 2
    osascript <<APPLESCRIPT || echo "  WARNING: AppleScript failed — DMG will have no styling"
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {100, 100, 740, 720}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 128
        set background picture of viewOptions to file ".background:dmg-background.png"
        set position of item "OnionPress.app" of container window to {160, 245}
        set position of item "Applications" of container window to {480, 245}
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
dmg_eject "$MOUNT_POINT"

# Step 5: Convert to compressed read-only DMG
echo "Compressing DMG..."
rm -f "$DMG_PATH"
dmg_compress "$RW_DMG_PATH" "$DMG_PATH"

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
