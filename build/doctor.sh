#!/bin/bash
#
# Report which build tools this machine has, and what each missing one costs.
#
# WHY THIS EXISTS
#   Prerequisite failures used to land late and cryptically. A missing swiftc,
#   lipo, codesign, hdiutil or pkg-config blew up mid-assembly with a raw tool
#   error; a missing git let the DMG build run for minutes before the mkp224o
#   guard aborted it. Only python3.14, uv and gh produced a usable message.
#
# Exits 0 always — this is a report, not a gate. Nothing here is required for
# every build: which tools you need depends on which artifact you want.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s)"

have=0
missing=0

# report <tool> <needed-for> [note]
report() {
    local tool="$1" needed="$2" note="${3:-}"
    if command -v "$tool" >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "$tool" "$needed"
        have=$((have + 1))
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "$tool" "$needed"
        [ -n "$note" ] && printf '      %s\n' "$note"
        missing=$((missing + 1))
    fi
}

echo "OnionPress build prerequisites  (host: $OS)"
echo ""

echo "Container images — build/build-images.sh, build/dev-up.sh"
report docker "the images and the dev stack" \
    "Install Docker Desktop, Colima, or any docker-compatible daemon."
if command -v docker >/dev/null 2>&1; then
    if docker buildx version >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "docker buildx" "multi-stage image builds"
        have=$((have + 1))
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "docker buildx" "multi-stage image builds"
        printf '      On a bare docker-ce install: apt-get install docker-buildx-plugin\n'
        missing=$((missing + 1))
    fi
    if docker version >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "docker daemon" "reachable"
    else
        printf '  \033[33m!\033[0m %-14s %s\n' "docker daemon" "not reachable (is it running?)"
    fi
fi
echo ""

echo "Linux package — build/build-linux.sh"
report python3 "assembling the .deb (and every build script)"
if command -v dpkg-deb >/dev/null 2>&1; then
    printf '  \033[32m✓\033[0m %-14s %s\n' "dpkg-deb" "native .deb assembly"
    have=$((have + 1))
else
    printf '  \033[33m!\033[0m %-14s %s\n' "dpkg-deb" "absent — falls back to a pure-python ar writer"
    printf '      Not a problem: build-linux.sh works on macOS this way.\n'
fi
echo ""

echo "Browser extensions — build/build-extension.sh"
report zip "packaging the .xpi and .zip"
echo ""

if [ "$OS" = "Darwin" ]; then
    echo "macOS installer — build/build-dmg-simple.sh"
    report swiftc "compiling the launcher wrapper" \
        "Install the Xcode Command Line Tools: xcode-select --install"
    report lipo "universal (arm64 + x86_64) binaries" \
        "Xcode Command Line Tools."
    report codesign "ad-hoc signing the bundle" \
        "Xcode Command Line Tools."
    report hdiutil "creating the disk image"
    report git "cloning mkp224o — a missing mkp224o ABORTS the DMG build" \
        "Without it every fresh install silently gets a RANDOM .onion."
    report pkg-config "cross-compiling libsodium for mkp224o"
    report brew "installing libsodium/autoconf/automake for mkp224o" \
        "https://brew.sh — or install those three yourself."

    UNIVERSAL_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
    if [ -x "$UNIVERSAL_PYTHON" ]; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "python3.14" "universal2 — release-grade builds"
        have=$((have + 1))
    elif command -v uv >/dev/null 2>&1; then
        printf '  \033[33m!\033[0m %-14s %s\n' "python3.14" "uv-managed only — single-arch, local dev builds"
        printf '      Release builds need python.org universal2 3.14, or the .dmg\n'
        printf '      will not run on Intel Macs: https://www.python.org/downloads/\n'
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "python3.14" "REQUIRED for the .dmg (py2app freezes against it)"
        printf '      Install python.org universal2 3.14, or uv for dev builds.\n'
        printf '      /usr/bin/python3 is 3.9 and cannot import src/onionpress.\n'
        missing=$((missing + 1))
    fi

    echo ""
    echo "macOS icons — build/make-icons.sh"
    report sips "resizing the icon layers"
    report iconutil "assembling AppIcon.icns"
    if command -v magick >/dev/null 2>&1 || command -v convert >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "imagemagick" "regenerating the menubar icons"
        have=$((have + 1))
    else
        printf '  \033[33m!\033[0m %-14s %s\n' "imagemagick" "absent — menubar icons cannot be regenerated"
        printf '      brew install imagemagick. The committed icons stay valid.\n'
    fi
else
    echo "macOS installer — build/build-dmg-simple.sh"
    printf '  \033[33m!\033[0m %-14s %s\n' "n/a" "the .dmg can only be built on macOS"
    printf '      It needs swiftc, lipo, codesign, hdiutil and PlistBuddy.\n'
    echo ""
    echo "macOS icons — build/make-icons.sh"
    printf '  \033[33m!\033[0m %-14s %s\n' "n/a" "needs macOS sips + iconutil"
fi
echo ""

echo "Releases — build/release.sh"
report gh "creating the GitHub release" \
    "https://cli.github.com — then: gh auth login"
echo ""

echo "Consistency"
if "$PROJECT_DIR/build/refresh-image-digests.sh" --check >/dev/null 2>&1; then
    printf '  \033[32m✓\033[0m %-14s %s\n' "image pins" "every consumer matches build/image-pins.env"
else
    printf '  \033[31m✗\033[0m %-14s %s\n' "image pins" "DRIFTED"
    printf '      Fix with: build/refresh-image-digests.sh --propagate\n'
    missing=$((missing + 1))
fi
echo ""

echo "$have present, $missing missing."
echo "Nothing here is needed for every build — see docs/BUILDING.md for which"
echo "artifact needs what."
exit 0
