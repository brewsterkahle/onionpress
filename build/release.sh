#!/bin/bash
#
# Cut an OnionPress GitHub release with BOTH platform artifacts in one shot.
#
# WHY THIS EXISTS
#   Releases used to be cut by hand: build the .dmg on a Mac, then
#   `gh release create` + upload the .dmg. The Linux .deb was a separate,
#   easily-forgotten step — so it was. Releases v2.4.101–v2.4.106 shipped
#   only the .dmg, which 404'd the README's Linux download link
#   (releases/latest/download/onionpress.deb resolves to the *Latest*
#   release's asset of that name) for that whole range. This script makes
#   the .deb a mandatory part of every release.
#
# THE CROSS-PLATFORM RULE THIS ENFORCES
#   The .dmg can ONLY be built on macOS (hdiutil/py2app). So:
#     - On macOS: build .dmg + .deb, create-or-update the release, upload both.
#     - On Linux: build .deb only. NEVER create a release (it would be
#       .dmg-less and become "Latest", breaking the Mac download link).
#       Only attach the .deb to an EXISTING release that already carries the
#       matching .dmg. This is exactly the failure that motivated the script.
#
# USAGE
#   build/release.sh              # release the current src/menubar.py version
#   build/release.sh --draft      # same, but create the GitHub release as a draft
#
#   Bump the version FIRST (build/bump-version.sh X.Y.Z, macOS only) and
#   commit, then run this. This script does not bump — it releases whatever
#   version the source tree declares.
#
# REQUIREMENTS
#   - gh, authenticated (gh auth status)
#   - macOS additionally needs whatever build-dmg-simple.sh needs (py2app etc.)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build"

DRAFT=""
for arg in "$@"; do
    case "$arg" in
        --draft) DRAFT="--draft" ;;
        *) echo "ERROR: unknown argument: $arg"; exit 1 ;;
    esac
done

# ─── Version + tag ──────────────────────────────────────────────────────
VERSION=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: could not detect version from src/menubar.py"
    exit 1
fi
TAG="v$VERSION"
DEB_PATH="$BUILD_DIR/onionpress.deb"
DMG_PATH="$BUILD_DIR/onionpress.dmg"

OS="$(uname -s)"
echo "OnionPress release $TAG  (host: $OS)"

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not found. Install it and run 'gh auth login'."
    exit 1
fi

# Does the release already exist, and does it already have a .dmg?
release_exists=0
release_has_dmg=0
if gh release view "$TAG" >/dev/null 2>&1; then
    release_exists=1
    if gh release view "$TAG" --json assets --jq '.assets[].name' 2>/dev/null | grep -qx "onionpress.dmg"; then
        release_has_dmg=1
    fi
fi

# ─── Always build the .deb (works on macOS via build-linux.sh's fallback) ──
echo ""
echo "── Building Linux .deb ──────────────────────────────────────────────"
bash "$BUILD_DIR/build-linux.sh"
[ -f "$DEB_PATH" ] || { echo "ERROR: $DEB_PATH was not produced"; exit 1; }

# ─── macOS: build the .dmg, create-or-update the release, upload both ──────
if [ "$OS" = "Darwin" ]; then
    echo ""
    echo "── Building macOS .dmg ──────────────────────────────────────────────"
    bash "$BUILD_DIR/build-dmg-simple.sh"
    [ -f "$DMG_PATH" ] || { echo "ERROR: $DMG_PATH was not produced"; exit 1; }

    echo ""
    if [ "$release_exists" = "1" ]; then
        echo "── Updating existing release $TAG (uploading both assets) ───────────"
        gh release upload "$TAG" "$DMG_PATH" "$DEB_PATH" --clobber
    else
        echo "── Creating release $TAG with both assets ───────────────────────────"
        gh release create "$TAG" "$DMG_PATH" "$DEB_PATH" \
            $DRAFT \
            --title "$TAG" \
            --generate-notes
    fi

# ─── Linux: .deb only, and only onto an existing .dmg-bearing release ──────
else
    if [ "$release_exists" = "0" ]; then
        cat >&2 <<EOF

ERROR: Release $TAG does not exist, and this is not macOS.

  The .dmg can only be built on a Mac. Creating $TAG from Linux would
  publish a .dmg-less "Latest" release and 404 the macOS download link
  (releases/latest/download/onionpress.dmg).

  Do this instead:
    1. Cut $TAG from a Mac (build/release.sh), OR
    2. Have the Mac create $TAG with the .dmg first, then re-run this on
       Linux to attach the .deb.
EOF
        exit 1
    fi
    if [ "$release_has_dmg" = "0" ]; then
        echo "WARNING: release $TAG exists but has no onionpress.dmg asset." >&2
        echo "         Attaching the .deb anyway; make sure the Mac uploads the .dmg." >&2
    fi
    echo ""
    echo "── Attaching .deb to existing release $TAG ──────────────────────────"
    gh release upload "$TAG" "$DEB_PATH" --clobber
fi

# ─── Verify the download links resolve ─────────────────────────────────────
echo ""
echo "── Verifying release assets ─────────────────────────────────────────"
gh release view "$TAG" --json assets --jq '.assets[] | "  " + .name'

echo ""
echo "✅ Release $TAG published with the Linux .deb included."
