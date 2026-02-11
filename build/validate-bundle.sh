#!/bin/bash

# Validation script for onionpress bundle

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/OnionPress.app"

echo "Validating OnionPress bundle..."

# Check app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: App bundle not found at $APP_PATH"
    exit 1
fi

echo "✓ App bundle found"

# Check arch-suffixed binaries and wrapper scripts
BIN_DIR="$APP_PATH/Contents/Resources/bin"
WRAPPED_BINARIES=("colima" "limactl" "docker" "docker-compose")

for binary in "${WRAPPED_BINARIES[@]}"; do
    # Check wrapper script exists and is executable
    if [ ! -f "$BIN_DIR/$binary" ]; then
        echo "ERROR: Missing wrapper script: $binary"
        exit 1
    fi
    if [ ! -x "$BIN_DIR/$binary" ]; then
        echo "ERROR: Wrapper script not executable: $binary"
        exit 1
    fi
    # Verify it's a shell script, not a Mach-O binary
    if file "$BIN_DIR/$binary" | grep -q "Mach-O"; then
        echo "ERROR: $binary should be a wrapper script, but is a Mach-O binary"
        exit 1
    fi
    echo "  ✓ $binary wrapper script present"

    # Check arm64 binary
    if [ ! -f "$BIN_DIR/${binary}-arm64" ]; then
        echo "ERROR: Missing binary: ${binary}-arm64"
        exit 1
    fi
    if [ ! -x "$BIN_DIR/${binary}-arm64" ]; then
        echo "ERROR: Binary not executable: ${binary}-arm64"
        exit 1
    fi
    ARCH=$(lipo -archs "$BIN_DIR/${binary}-arm64" 2>/dev/null || echo "unknown")
    if [[ "$ARCH" != *"arm64"* ]]; then
        echo "ERROR: ${binary}-arm64 does not contain arm64 (found: $ARCH)"
        exit 1
    fi
    echo "  ✓ ${binary}-arm64 present ($ARCH)"

done

# Check x86_64 binaries archive (base64-encoded to hide from Gatekeeper)
if [ ! -f "$BIN_DIR/intel-binaries.b64" ]; then
    echo "ERROR: Missing intel-binaries.b64 archive"
    exit 1
fi
# Verify archive contains expected binaries
X86_CONTENTS=$(base64 -d < "$BIN_DIR/intel-binaries.b64" | tar tzf - | sort)
for binary in "${WRAPPED_BINARIES[@]}"; do
    if ! echo "$X86_CONTENTS" | grep -q "${binary}-x86_64"; then
        echo "ERROR: intel-binaries.b64 missing ${binary}-x86_64"
        exit 1
    fi
done
echo "  ✓ intel-binaries.b64 present (contains all binaries)"

# Verify no bare x86_64 Mach-O binaries in bundle (would trigger Rosetta prompt)
for binary in "${WRAPPED_BINARIES[@]}"; do
    if [ -f "$BIN_DIR/${binary}-x86_64" ]; then
        echo "ERROR: Bare x86_64 binary found: ${binary}-x86_64 (should be in archive only)"
        exit 1
    fi
done
echo "  ✓ No bare x86_64 Mach-O binaries in bundle"

# Check lima wrapper script
if [ ! -f "$BIN_DIR/lima" ] || [ ! -x "$BIN_DIR/lima" ]; then
    echo "ERROR: lima wrapper script missing or not executable"
    exit 1
fi
echo "  ✓ lima wrapper script present"

# Check Lima share files
SHARE_DIR="$APP_PATH/Contents/Resources/share/lima"
if [ ! -d "$SHARE_DIR" ]; then
    echo "ERROR: Lima share directory not found"
    exit 1
fi

echo "✓ Lima share files present"

# Check Info.plist minimum version
MIN_VERSION=$(defaults read "$APP_PATH/Contents/Info.plist" LSMinimumSystemVersion)
if [ "$MIN_VERSION" != "13.0" ]; then
    echo "ERROR: LSMinimumSystemVersion is $MIN_VERSION, should be 13.0"
    exit 1
fi

echo "✓ Info.plist minimum version is 13.0"

# Check scripts exist
if [ ! -f "$APP_PATH/Contents/MacOS/launcher" ]; then
    echo "ERROR: launcher script missing"
    exit 1
fi

if [ ! -f "$APP_PATH/Contents/MacOS/onionpress" ]; then
    echo "ERROR: onionpress script missing"
    exit 1
fi

echo "✓ Launch scripts present"

# Get bundle size
BUNDLE_SIZE=$(du -sh "$APP_PATH" | cut -f1)
echo ""
echo "Bundle size: $BUNDLE_SIZE"

# Estimate DMG size
echo "Expected DMG size: ~300-450MB (before compression)"

echo ""
echo "✅ Bundle validation passed!"
