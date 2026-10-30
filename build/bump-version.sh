#!/bin/bash
# Bump the OnionPress version across all files.
#
# Usage: build/bump-version.sh 2.4.47
#
# This updates the 2 canonical sources (src/menubar.py and the outer Info.plist)
# plus derived files (__init__.py, MenubarApp plist, the Linux AppStream
# metainfo). setup.py reads from menubar.py dynamically at build time, so it
# needs no update. Runs on macOS and Linux (no BSD-only sed, PlistBuddy only
# where it exists).

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 2.4.47"
    exit 1
fi

NEW_VERSION="$1"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# In-place regex replace that behaves the same on BSD and GNU userlands.
replace_in_file() {
    python3 - "$1" "$2" "$3" <<'PY'
import re, sys
path, pattern, repl = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
new, n = re.subn(pattern, repl, text, count=1)
if n != 1:
    sys.exit("no match for %r in %s" % (pattern, path))
open(path, "w", encoding="utf-8").write(new)
PY
}

# Set a plist version key with PlistBuddy, or plistlib where it is absent.
plist_set() {
    local plist="$1" key="$2" value="$3"
    if [ -x /usr/libexec/PlistBuddy ]; then
        /usr/libexec/PlistBuddy -c "Set :$key $value" "$plist"
    else
        python3 - "$plist" "$key" "$value" <<'PY'
import plistlib, sys
path, key, value = sys.argv[1:4]
with open(path, "rb") as f:
    data = plistlib.load(f)
data[key] = value
with open(path, "wb") as f:
    plistlib.dump(data, f)
PY
    fi
}

plist_get() {
    if [ -x /usr/libexec/PlistBuddy ]; then
        /usr/libexec/PlistBuddy -c "Print :$2" "$1"
    else
        python3 -c 'import plistlib,sys; print(plistlib.load(open(sys.argv[1],"rb"))[sys.argv[2]])' "$1" "$2"
    fi
}

# Validate version format (X.Y.Z)
if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: Version must be in X.Y.Z format (got: $NEW_VERSION)"
    exit 1
fi

echo "Bumping OnionPress to v$NEW_VERSION"
echo ""

# 1. src/menubar.py — self.version (canonical source #1)
replace_in_file "$PROJECT_DIR/src/menubar.py" \
    'self\.version = "[^"]*"' "self.version = \"$NEW_VERSION\""
echo "  Updated src/menubar.py"

# 2. app/Info.plist (canonical source #2)
plist_set "$PROJECT_DIR/app/Info.plist" CFBundleShortVersionString "$NEW_VERSION"
plist_set "$PROJECT_DIR/app/Info.plist" CFBundleVersion "$NEW_VERSION"
echo "  Updated app/Info.plist"

# 3. src/onionpress/__init__.py (derived)
replace_in_file "$PROJECT_DIR/src/onionpress/__init__.py" \
    '__version__ = "[^"]*"' "__version__ = \"$NEW_VERSION\""
echo "  Updated src/onionpress/__init__.py"

# 3b. linux/onionpress.metainfo.xml — the newest <release> (ships in the .deb)
replace_in_file "$PROJECT_DIR/linux/onionpress.metainfo.xml" \
    '<release version="[^"]*" date="[^"]*"' \
    "<release version=\"$NEW_VERSION\" date=\"$(date +%Y-%m-%d)\""
echo "  Updated linux/onionpress.metainfo.xml"

# 4. MenubarApp plist (derived — also rebuilt by py2app, but update for non-rebuild releases)
MENUBAR_PLIST="$PROJECT_DIR/OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist"
if [ -f "$MENUBAR_PLIST" ]; then
    plist_set "$MENUBAR_PLIST" CFBundleShortVersionString "$NEW_VERSION"
    plist_set "$MENUBAR_PLIST" CFBundleVersion "$NEW_VERSION"
    echo "  Updated MenubarApp/Contents/Info.plist"
fi

# Verify all locations match
echo ""
echo "Verification:"
MENUBAR_VER=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
PLIST_VER=$(plist_get "$PROJECT_DIR/app/Info.plist" CFBundleShortVersionString)
INIT_VER=$(grep '__version__' "$PROJECT_DIR/src/onionpress/__init__.py" | sed 's/.*"\(.*\)".*/\1/')
META_VER=$(grep -m1 '<release version=' "$PROJECT_DIR/linux/onionpress.metainfo.xml" | sed 's/.*version="\([^"]*\)".*/\1/')

ALL_MATCH=true
for name_ver in "src/menubar.py:$MENUBAR_VER" "Info.plist:$PLIST_VER" "__init__.py:$INIT_VER" "metainfo.xml:$META_VER"; do
    name="${name_ver%%:*}"
    ver="${name_ver##*:}"
    if [ "$ver" = "$NEW_VERSION" ]; then
        echo "  $name: $ver"
    else
        echo "  $name: $ver  *** MISMATCH ***"
        ALL_MATCH=false
    fi
done

if [ -f "$MENUBAR_PLIST" ]; then
    MB_VER=$(plist_get "$MENUBAR_PLIST" CFBundleShortVersionString)
    if [ "$MB_VER" = "$NEW_VERSION" ]; then
        echo "  MenubarApp plist: $MB_VER"
    else
        echo "  MenubarApp plist: $MB_VER  *** MISMATCH ***"
        ALL_MATCH=false
    fi
fi

echo ""
if $ALL_MATCH; then
    echo "All versions updated to $NEW_VERSION"
else
    echo "ERROR: Some versions did not update correctly!"
    exit 1
fi
