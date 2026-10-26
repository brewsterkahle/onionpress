#!/bin/bash
# Quick rebuild of just the MenubarApp (py2app) and install into OnionPress.app.
# The full build-dmg-simple.sh calls this section too, but this script is
# faster for iterating on src/ changes during development.
#
# Usage: build/rebuild-menubar.sh
#
# After running, quit and relaunch OnionPress.app to pick up changes.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/src"
APP_PATH="/Applications/OnionPress.app"
MENUBAR_APP_DIR="$APP_PATH/Contents/Resources/MenubarApp"
MENUBAR_BUILD_DIR=$(mktemp -d)

trap 'rm -rf "$MENUBAR_BUILD_DIR"' EXIT

echo "=== Creating venv and installing deps..."
UNIVERSAL_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
if [ -x "$UNIVERSAL_PYTHON" ]; then
    echo "Using universal2 Python: $UNIVERSAL_PYTHON"
    "$UNIVERSAL_PYTHON" -m venv "$MENUBAR_BUILD_DIR/venv"
else
    echo "WARNING: universal2 Python not found, using system python3 (app may be arm64-only)"
    python3 -m venv "$MENUBAR_BUILD_DIR/venv"
fi
# Pinned by version and hash (issue #387): the locks build/refresh-python-locks.sh writes.
"$MENUBAR_BUILD_DIR/venv/bin/python3" -m pip install -q --require-hashes -r "$PROJECT_DIR/build/build-requirements-macos.lock"
"$MENUBAR_BUILD_DIR/venv/bin/pip" install -q --require-hashes -r "$PROJECT_DIR/build/requirements-macos.lock"
# PyNaCl (backup encryption) from source, optimised; cffi made universal2.
"$PROJECT_DIR/build/py2app-extensions.sh" prepare "$MENUBAR_BUILD_DIR/venv"

echo "=== Copying the onionpress package to site-packages..."
SITE_PACKAGES=$("$MENUBAR_BUILD_DIR/venv/bin/python3" -c "import site; print(site.getsitepackages()[0])")
cp -r "$SCRIPTS_DIR/onionpress" "$SITE_PACKAGES/"

echo "=== Building with py2app..."
cd "$PROJECT_DIR"
if ! "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
    --dist-dir "$MENUBAR_BUILD_DIR/dist" \
    --bdist-base "$MENUBAR_BUILD_DIR/build" \
    2>&1 | tail -5; then
    echo "py2app failed — retrying with the locked setuptools..."
    "$MENUBAR_BUILD_DIR/venv/bin/pip" install -q --require-hashes -r "$PROJECT_DIR/build/build-requirements-macos.lock"
    rm -rf "$MENUBAR_BUILD_DIR/build" "$MENUBAR_BUILD_DIR/dist"
    "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
        --dist-dir "$MENUBAR_BUILD_DIR/dist" \
        --bdist-base "$MENUBAR_BUILD_DIR/build" \
        2>&1 | tail -5
fi

# Refuse to install a MenubarApp that cannot encrypt or restore backups.
for built in "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_BUILD_DIR/dist/menubar.app"; do
    if [ -d "$built" ]; then
        echo "=== Checking backup encryption (PyNaCl) in the build..."
        "$PROJECT_DIR/build/py2app-extensions.sh" verify "$built"
        break
    fi
done

echo "=== Installing into $MENUBAR_APP_DIR..."
rm -rf "$MENUBAR_APP_DIR"
if [ -d "$MENUBAR_BUILD_DIR/dist/OnionPress.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_APP_DIR"
elif [ -d "$MENUBAR_BUILD_DIR/dist/menubar.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/menubar.app" "$MENUBAR_APP_DIR"
else
    echo "ERROR: py2app output not found in $MENUBAR_BUILD_DIR/dist/"
    ls "$MENUBAR_BUILD_DIR/dist/"
    exit 1
fi

# Remove broken .pyo symlinks
find "$MENUBAR_APP_DIR" -name '*.pyo' -type l -delete 2>/dev/null || true

# Sync the parent app's Info.plist version with the freshly-built MenubarApp.
# self.version in menubar.py reads from $APP_PATH/Contents/Info.plist (the
# parent), and that's what gets written to /var/lib/onionpress/version for
# the WP settings page. Without this sync, the parent plist stays at whatever
# version it was when the DMG was last built, even though the MenubarApp
# code is newer — so the WP page shows a stale version.
PARENT_PLIST="$APP_PATH/Contents/Info.plist"
MENUBAR_PLIST="$MENUBAR_APP_DIR/Contents/Info.plist"
if [ -f "$PARENT_PLIST" ] && [ -f "$MENUBAR_PLIST" ]; then
    NEW_VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$MENUBAR_PLIST" 2>/dev/null || true)
    OLD_VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$PARENT_PLIST" 2>/dev/null || true)
    if [ -n "$NEW_VERSION" ] && [ "$NEW_VERSION" != "$OLD_VERSION" ]; then
        /usr/libexec/PlistBuddy -c "Set CFBundleShortVersionString $NEW_VERSION" "$PARENT_PLIST"
        echo "=== Synced parent Info.plist: $OLD_VERSION -> $NEW_VERSION"
    fi
fi

echo "=== Done! Quit and relaunch OnionPress.app to pick up changes."
