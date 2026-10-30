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
    if diskutil image --help >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "diskutil image" "creating the disk image"
        have=$((have + 1))
    elif command -v hdiutil >/dev/null 2>&1; then
        printf '  \033[33m!\033[0m %-14s %s\n' "diskutil image" "absent (needs macOS 26+) — the build falls back to hdiutil"
        printf '      Fine for building. hdiutil is deprecated from macOS 27 on, so the\n'
        printf '      fallback has a shelf life; DMG_TOOL=hdiutil forces it for testing.\n'
        have=$((have + 1))
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "diskutil image" "creating the disk image (no diskutil image, no hdiutil)"
        missing=$((missing + 1))
    fi
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
    printf '      It needs swiftc, lipo, codesign, diskutil and PlistBuddy.\n'
    echo ""
    echo "macOS icons — build/make-icons.sh"
    printf '  \033[33m!\033[0m %-14s %s\n' "n/a" "needs macOS sips + iconutil"
fi
echo ""

echo "Releases — build/release.sh"
report gh "creating the GitHub release" \
    "https://cli.github.com — then: gh auth login"
echo ""

# The end-to-end suite runs throwaway installs in VMs (docs/E2E-TESTING.md
# §2). `e2e/run.sh --probe` is the authority on what this host can test;
# this lists the host tools it looks for, with what each missing one needs.
echo "End-to-end suite — e2e/run.sh (docs/E2E-TESTING.md)"
report uv "running the suite (fetches pytest and PyNaCl itself)" \
    "curl -LsSf https://astral.sh/uv/install.sh | sh   (or: brew install uv)"
if [ "$OS" = "Darwin" ]; then
    if command -v limactl >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "limactl" "Linux VMs (Lima)"
        have=$((have + 1))
    elif [ -x /Applications/OnionPress.app/Contents/Resources/bin/limactl ]; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "limactl" "Linux VMs — the one bundled in /Applications/OnionPress.app"
        have=$((have + 1))
    else
        printf '  \033[33m!\033[0m %-14s %s\n' "limactl" "absent — Linux is not tested from this Mac (macOS still is)"
        printf '      brew install lima, or pass --mac-app with an OnionPress.app (it bundles Lima).\n'
    fi
else
    ARCH="$(uname -m)"
    case "$ARCH" in
        aarch64|arm64) QEMU_BIN=qemu-system-aarch64; FW_PKG="qemu-efi-aarch64";
                       FW_PATHS="/usr/share/AAVMF/AAVMF_CODE.fd /usr/share/qemu-efi-aarch64/QEMU_EFI.fd /usr/share/edk2/aarch64/QEMU_EFI.fd /usr/share/edk2/aarch64/QEMU_CODE.fd /usr/share/qemu/edk2-aarch64-code.fd" ;;
        *)             QEMU_BIN="qemu-system-$ARCH"; FW_PKG="ovmf";
                       FW_PATHS="/usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/edk2-ovmf/x64/OVMF_CODE.fd /usr/share/edk2/x64/OVMF_CODE.4m.fd /usr/share/qemu/edk2-x86_64-code.fd /usr/share/qemu/ovmf-x86_64-code.bin" ;;
    esac
    report limactl "Linux VMs (Lima 2.x, with its debian-13 and ubuntu-24.04 templates)" \
        "Pinned release tarball: docs/E2E-TESTING.md §10 (Linux host setup); no distro package yet."
    if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "/dev/kvm" "hardware virtualisation for the VMs"
        have=$((have + 1))
    elif [ -e /dev/kvm ]; then
        printf '  \033[31m✗\033[0m %-14s %s\n' "/dev/kvm" "present but not writable by you"
        printf '      sudo usermod -aG kvm "$USER", then log in again (a console login often\n'
        printf '      has it already through logind; an SSH session does not).\n'
        missing=$((missing + 1))
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "/dev/kvm" "absent — no KVM (nested virtualisation off, or no VT-x/SVM)"
        printf '      Without KVM, Lima VMs are not offered here (QEMU alone is far too slow).\n'
        missing=$((missing + 1))
    fi
    report "$QEMU_BIN" "the VM machine emulator" \
        "apt-get install qemu-system-$( [ "$ARCH" = x86_64 ] && echo x86 || echo arm )   (Fedora: qemu-system-$ARCH)"
    fw_found=""
    for f in $FW_PATHS; do
        [ -f "$f" ] && fw_found="$f" && break
    done
    if [ -n "$fw_found" ]; then
        printf '  \033[32m✓\033[0m %-14s %s\n' "UEFI firmware" "$fw_found"
        have=$((have + 1))
    else
        printf '  \033[31m✗\033[0m %-14s %s\n' "UEFI firmware" "Lima's QEMU VMs boot with it ($FW_PKG)"
        printf '      apt-get install %s\n' "$FW_PKG"
        missing=$((missing + 1))
    fi
    tmp_free_kib="$(df -Pk /tmp 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -n "$tmp_free_kib" ] && [ "$tmp_free_kib" -lt $((10 * 1024 * 1024)) ]; then
        printf '  \033[33m!\033[0m %-14s %s\n' "/tmp space" "$((tmp_free_kib / 1024 / 1024)) GiB free — a VM needs ~10 GiB"
        printf '      Pass E2E_ARGS="--scratch-root DIR" with DIR outside your home on a bigger filesystem.\n'
    fi
fi
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
