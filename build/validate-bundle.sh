#!/bin/bash

# Validation script for onionpress bundle (universal binaries)

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

# Check universal binaries
BIN_DIR="$APP_PATH/Contents/Resources/bin"
UNIVERSAL_BINARIES=("colima" "limactl" "docker" "docker-compose" "mkp224o")

for binary in "${UNIVERSAL_BINARIES[@]}"; do
    if [ ! -f "$BIN_DIR/$binary" ]; then
        echo "ERROR: Missing binary: $binary"
        exit 1
    fi
    if [ ! -x "$BIN_DIR/$binary" ]; then
        echo "ERROR: Binary not executable: $binary"
        exit 1
    fi
    # Verify it's a Mach-O binary, not a shell script
    if ! file "$BIN_DIR/$binary" | grep -q "Mach-O"; then
        echo "ERROR: $binary is not a Mach-O binary"
        exit 1
    fi
    # Verify it contains both architectures
    ARCHS=$(lipo -archs "$BIN_DIR/$binary" 2>/dev/null || echo "unknown")
    if [[ "$ARCHS" != *"arm64"* ]] || [[ "$ARCHS" != *"x86_64"* ]]; then
        echo "ERROR: $binary is not universal (found: $ARCHS, need arm64 + x86_64)"
        exit 1
    fi
    echo "  ✓ $binary — universal ($ARCHS)"
done

# Verify no leftover dual-binary artifacts
for artifact in intel-binaries.b64; do
    if [ -f "$BIN_DIR/$artifact" ]; then
        echo "ERROR: Leftover dual-binary artifact found: $artifact"
        exit 1
    fi
done
for binary in "${UNIVERSAL_BINARIES[@]}"; do
    for suffix in -arm64 -x86_64; do
        if [ -f "$BIN_DIR/${binary}${suffix}" ]; then
            echo "ERROR: Leftover arch-suffixed binary: ${binary}${suffix}"
            exit 1
        fi
    done
done
echo "  ✓ No leftover dual-binary artifacts"

# Scan entire bundle for x86_64-only Mach-O files (Rosetta trigger)
echo "Scanning bundle for x86_64-only Mach-O files..."
ROSETTA_TRIGGERS=0
while IFS= read -r -d '' f; do
    if file "$f" | grep -q "Mach-O.*x86_64" && ! file "$f" | grep -q "universal\|arm64"; then
        ARCHS=$(lipo -archs "$f" 2>/dev/null || echo "x86_64-only")
        if [[ "$ARCHS" == "x86_64" ]]; then
            echo "  WARNING: x86_64-only Mach-O: $f"
            ROSETTA_TRIGGERS=$((ROSETTA_TRIGGERS + 1))
        fi
    fi
done < <(find "$APP_PATH" -type f -print0)
if [ $ROSETTA_TRIGGERS -gt 0 ]; then
    echo "ERROR: Found $ROSETTA_TRIGGERS x86_64-only Mach-O file(s) that would trigger Rosetta prompt"
    exit 1
fi
echo "  ✓ No x86_64-only Mach-O files found"

# Check lima wrapper script
if [ ! -f "$BIN_DIR/lima" ] || [ ! -x "$BIN_DIR/lima" ]; then
    echo "ERROR: lima wrapper script missing or not executable"
    exit 1
fi
echo "  ✓ lima wrapper script present"

# Check Lima share files (guest agents for both architectures)
SHARE_DIR="$APP_PATH/Contents/Resources/share/lima"
if [ ! -d "$SHARE_DIR" ]; then
    echo "ERROR: Lima share directory not found"
    exit 1
fi
# Check for guest agent files for both architectures
AARCH64_AGENT=$(find "$SHARE_DIR" -name "*aarch64*" -o -name "*arm64*" | head -1)
X86_64_AGENT=$(find "$SHARE_DIR" -name "*x86_64*" -o -name "*amd64*" | head -1)
if [ -z "$AARCH64_AGENT" ]; then
    echo "ERROR: No aarch64/arm64 Lima guest agent found"
    exit 1
fi
if [ -z "$X86_64_AGENT" ]; then
    echo "ERROR: No x86_64/amd64 Lima guest agent found"
    exit 1
fi
echo "✓ Lima share files present (both architectures)"

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

echo ""
echo "✅ Bundle validation passed!"
