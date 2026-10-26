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
# SHA256SUMS
#   Every release also carries SHA256SUMS: "<sha256>  <name>" for
#   onionpress.deb and onionpress.dmg, as sha256sum writes it. Linux's
#   `onionpress update` installs a release's onionpress.deb only when it
#   matches its line there, so a release without it cannot be installed
#   that way. The script refuses to finish without it: after uploading it
#   lists the release's assets, downloads SHA256SUMS and onionpress.deb back
#   and checks them as `onionpress update` will. A new release is created
#   as a draft and published only once that check passes, so a published
#   release never lacks it (the Docker image workflow runs on "published").
#
# THE CROSS-PLATFORM RULE THIS ENFORCES
#   The .dmg can ONLY be built on macOS (diskutil image/py2app). So:
#     - On macOS: build .dmg + .deb, create-or-update the release, upload
#       both and SHA256SUMS.
#     - On Linux: build .deb only. NEVER create a release (it would be
#       .dmg-less and become "Latest", breaking the Mac download link).
#       Only attach the .deb to an EXISTING release that already carries the
#       matching .dmg. This is exactly the failure that motivated the script.
#       SHA256SUMS is rewritten for the new .deb and the release's own .dmg
#       (downloaded to hash it).
#
# USAGE
#   build/release.sh              # release the current src/menubar.py version
#   build/release.sh --draft      # same, but leave a new GitHub release a draft
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
SUMS_PATH="$BUILD_DIR/SHA256SUMS"

OS="$(uname -s)"
echo "OnionPress release $TAG  (host: $OS)"
PUBLISH_AFTER_VERIFY=0
REQUIRED_ASSETS=""

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not found. Install it and run 'gh auth login'."
    exit 1
fi

fail() {
    echo "" >&2
    echo "ERROR: $*" >&2
    exit 1
}

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/onionpress-release.XXXXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

# "<sha256>  <name>" for file $1, as sha256sum writes it.
sha256_line() {
    local out
    if command -v sha256sum >/dev/null 2>&1; then
        out=$(sha256sum "$1")
    else
        out=$(shasum -a 256 "$1")
    fi
    printf '%s  %s\n' "${out%% *}" "$(basename "$1")"
}

# SHA256SUMS for the files given, and SHA256SUMS.sig when
# ONIONPRESS_RELEASE_SIGNING_KEY names the maintainer's secret key file
# (build/release-key.py; issue #246). Without the key the release goes out
# unsigned and this says so: an OnionPress with a public key embedded
# refuses to install it.
write_sums() {
    local f
    : > "$SUMS_PATH.tmp"
    for f in "$@"; do
        sha256_line "$f" >> "$SUMS_PATH.tmp"
    done
    mv "$SUMS_PATH.tmp" "$SUMS_PATH"
    echo "  SHA256SUMS:"
    sed 's/^/    /' "$SUMS_PATH"
    rm -f "$SUMS_PATH.sig"
    if [ -n "${ONIONPRESS_RELEASE_SIGNING_KEY:-}" ]; then
        python3 "$BUILD_DIR/release-key.py" sign "$SUMS_PATH" --key "$ONIONPRESS_RELEASE_SIGNING_KEY" \
            || fail "could not sign SHA256SUMS with $ONIONPRESS_RELEASE_SIGNING_KEY"
        SIG_ASSETS="$SUMS_PATH.sig"
    else
        echo "  WARNING: SHA256SUMS is not signed (ONIONPRESS_RELEASE_SIGNING_KEY unset);" >&2
        echo "           an OnionPress with a release key embedded will refuse this release." >&2
        SIG_ASSETS=""
    fi
}
SIG_ASSETS=""

# The release carries every asset named in $@, its SHA256SUMS is the one
# written here, and its onionpress.deb matches it: the check Linux's
# `onionpress update` makes before installing. Exits on any failure.
verify_release() {
    local names need line
    names=$(gh release view "$TAG" --json assets --jq '.assets[].name') \
        || fail "could not list the assets of release $TAG."
    printf '%s\n' "$names" | sed 's/^/  /'
    for need in "$@"; do
        grep -qxF "$need" <<< "$names" || fail "release $TAG has no $need."
    done
    rm -rf "$WORK_DIR/published"
    mkdir "$WORK_DIR/published"
    gh release download "$TAG" --pattern SHA256SUMS --pattern onionpress.deb \
            --dir "$WORK_DIR/published" \
        || fail "could not download SHA256SUMS and onionpress.deb of $TAG to check them."
    cmp -s "$WORK_DIR/published/SHA256SUMS" "$SUMS_PATH" \
        || fail "the SHA256SUMS published with $TAG is not the one written here ($SUMS_PATH)."
    line=$(sha256_line "$WORK_DIR/published/onionpress.deb")
    grep -qxF "$line" "$SUMS_PATH" \
        || fail "the onionpress.deb published with $TAG does not match SHA256SUMS."
    echo "  SHA256SUMS and onionpress.deb check out."
}

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
    # A release must run on Intel Macs too (the README asks only for macOS
    # 13). Without python.org's universal2 Python, build-dmg-simple.sh falls
    # back to uv's single-arch one — fine for a dev build, and how 2.5.2 and
    # 2.5.3 shipped an Apple-Silicon-only MenubarApp: the fallback printed
    # "local dev build" and nothing here looked. ONIONPRESS_RELEASE_BUILD
    # makes the build refuse that fallback up front; the scan below checks
    # what was actually built.
    ONIONPRESS_RELEASE_BUILD=1 bash "$BUILD_DIR/build-dmg-simple.sh"
    [ -f "$DMG_PATH" ] || { echo "ERROR: $DMG_PATH was not produced"; exit 1; }

    echo ""
    echo "── Checking every Mach-O in the bundle is universal ────────────────"
    # build-dmg-simple.sh leaves the bundle it copied into the .dmg here.
    # lipo -archs fails on anything that is not Mach-O; those are skipped.
    APP_PATH="$PROJECT_DIR/OnionPress.app"
    MENUBAR_PYTHON="$APP_PATH/Contents/Resources/MenubarApp/Contents/MacOS/python"
    [ -f "$MENUBAR_PYTHON" ] || { echo "ERROR: $MENUBAR_PYTHON not found"; exit 1; }
    single_arch=()
    machos=0
    while IFS= read -r -d '' f; do
        archs=" $(lipo -archs "$f" 2>/dev/null) " || continue
        machos=$((machos + 1))
        if [[ "$archs" != *" arm64 "* || "$archs" != *" x86_64 "* ]]; then
            single_arch+=("${f#"$APP_PATH/"} ($(echo $archs))")
        fi
    done < <(find "$APP_PATH" -type f -print0)
    if [ "${#single_arch[@]}" -gt 0 ]; then
        echo "ERROR: not universal (arm64 + x86_64); this release would not run on every Mac:" >&2
        printf '  %s\n' "${single_arch[@]}" >&2
        exit 1
    fi
    echo "  $machos Mach-O files, all arm64 + x86_64 (MenubarApp python: $(lipo -archs "$MENUBAR_PYTHON"))"

    write_sums "$DEB_PATH" "$DMG_PATH"
    REQUIRED_ASSETS="onionpress.dmg onionpress.deb SHA256SUMS${SIG_ASSETS:+ SHA256SUMS.sig}"

    echo ""
    if [ "$release_exists" = "1" ]; then
        echo "── Updating existing release $TAG (both assets + SHA256SUMS) ────────"
        gh release upload "$TAG" "$DMG_PATH" "$DEB_PATH" "$SUMS_PATH" $SIG_ASSETS --clobber
    else
        # A draft until verify_release passes; published below unless
        # --draft was asked for.
        echo "── Creating release $TAG with both assets + SHA256SUMS ──────────────"
        gh release create "$TAG" "$DMG_PATH" "$DEB_PATH" "$SUMS_PATH" $SIG_ASSETS \
            --draft \
            --title "$TAG" \
            --generate-notes
        if [ -z "$DRAFT" ]; then
            PUBLISH_AFTER_VERIFY=1
        fi
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
    # SHA256SUMS for the new .deb, and for the release's own .dmg.
    echo ""
    REQUIRED_ASSETS="onionpress.deb SHA256SUMS"
    if [ "$release_has_dmg" = "1" ]; then
        mkdir "$WORK_DIR/dmg"
        gh release download "$TAG" --pattern onionpress.dmg --dir "$WORK_DIR/dmg" \
            || fail "could not download the onionpress.dmg of $TAG to hash it for SHA256SUMS."
        write_sums "$DEB_PATH" "$WORK_DIR/dmg/onionpress.dmg"
        REQUIRED_ASSETS="onionpress.dmg $REQUIRED_ASSETS"
    else
        write_sums "$DEB_PATH"
    fi
    echo ""
    echo "── Attaching .deb + SHA256SUMS to existing release $TAG ─────────────"
    gh release upload "$TAG" "$DEB_PATH" "$SUMS_PATH" $SIG_ASSETS --clobber
fi

# ─── Verify the assets, SHA256SUMS included, then publish ──────────────────
echo ""
echo "── Verifying release assets ─────────────────────────────────────────"
# shellcheck disable=SC2086  # a list of plain asset names
verify_release $REQUIRED_ASSETS

if [ "$PUBLISH_AFTER_VERIFY" = "1" ]; then
    echo ""
    echo "── Publishing $TAG ───────────────────────────────────────────────────"
    gh release edit "$TAG" --draft=false
fi

echo ""
if [ "$(gh release view "$TAG" --json isDraft --jq .isDraft 2>/dev/null)" = "true" ]; then
    echo "✅ Draft release $TAG ready with the Linux .deb and SHA256SUMS (not published)."
    echo "   Publish it with: gh release edit $TAG --draft=false"
else
    echo "✅ Release $TAG published with the Linux .deb and SHA256SUMS included."
fi
