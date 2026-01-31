#!/bin/bash

# Validation script for onion.press bundle

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/onion.press.app"

echo "Validating onion.press bundle..."

# Check app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: App bundle not found at $APP_PATH"
    exit 1
fi

echo "✓ App bundle found"

# Check binaries exist
BIN_DIR="$APP_PATH/Contents/Resources/bin"
REQUIRED_BINARIES=("colima" "limactl" "docker")

for binary in "${REQUIRED_BINARIES[@]}"; do
    if [ ! -f "$BIN_DIR/$binary" ]; then
        echo "ERROR: Missing binary: $binary"
        exit 1
    fi

    if [ ! -x "$BIN_DIR/$binary" ]; then
        echo "ERROR: Binary not executable: $binary"
        exit 1
    fi

    # Check if universal binary
    ARCHS=$(lipo -archs "$BIN_DIR/$binary")
    if [[ ! "$ARCHS" =~ "x86_64" ]] || [[ ! "$ARCHS" =~ "arm64" ]]; then
        echo "ERROR: Binary $binary is not universal (found: $ARCHS)"
        exit 1
    fi

    echo "✓ $binary is present and universal ($ARCHS)"
done

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

if [ ! -f "$APP_PATH/Contents/MacOS/onion.press" ]; then
    echo "ERROR: onion.press script missing"
    exit 1
fi

echo "✓ Launch scripts present"

# Get bundle size
BUNDLE_SIZE=$(du -sh "$APP_PATH" | cut -f1)
echo ""
echo "Bundle size: $BUNDLE_SIZE"

# Estimate DMG size
echo "Expected DMG size: ~200-300MB (before compression)"

echo ""
echo "✅ Bundle validation passed!"
