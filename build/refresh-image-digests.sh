#!/bin/bash
# Keep every OnionPress container-image pin in sync from one source of truth.
#
# THE SOURCE OF TRUTH IS build/image-pins.env. This script is the only thing
# that writes it, and the only thing that writes the literals embedded in the
# consumers below. tests/test_image_pins.py fails the build if they drift.
#
# Consumers (all rewritten together — that is the whole point):
#   app/Resources/docker/docker-compose.yml   tor, wordpress, onionheaven
#   app/MacOS/onionpress                      update_images(), takeover worker
#   linux/onionpress                          update_images()
#   src/onionpress/containers.py              ONIONHEAVEN_IMAGE
#   src/onionpress/launcher_ops.py            DEFAULT_TOR_IMAGE
#
# WHY THIS SCRIPT GREW A --propagate MODE
#   The consumers held independent copies of the same digest and the release
#   process relied on someone remembering to run this. v2.4.110 (94ce1a36)
#   refreshed only docker-compose.yml, so from that release until this change
#   macOS compose ran tor@1f98ac29 while the Linux launcher, the vanity-key
#   generator and the OnionHeaven farm workers all still ran tor@ecab8ad6 —
#   two different Tor builds inside one install, and a "✓ up to date" log line
#   about an image the stack never started. A single source plus `--check`
#   (wired into tests/test_image_pins.py) makes that class of drift impossible
#   to commit rather than merely discouraged.
#
# mariadb and willfarrell/autoheal stay UNPINNED by design — we trust their
# upstream registries to ship security patches without us tracking digests.
# See docs/BUILDING.md, "Why some images stay floating".
#
# USAGE
#   # Normal release flow — read digests from freshly pulled images:
#   docker pull ghcr.io/brewsterkahle/onionpress-tor:latest
#   docker pull ghcr.io/brewsterkahle/onionpress-wordpress:latest
#   build/refresh-image-digests.sh
#
#   # Supply digests explicitly (CI, or a machine with no daemon):
#   build/refresh-image-digests.sh --tor sha256:aaa… --wordpress sha256:bbb…
#
#   # Re-apply build/image-pins.env to every consumer, no daemon needed.
#   # This is the repair mode for "someone hand-edited one file":
#   build/refresh-image-digests.sh --propagate
#
#   # Verify consumers match build/image-pins.env; exit 1 if not:
#   build/refresh-image-digests.sh --check
#
#   git diff   # always eyeball the changes

set -euo pipefail
cd "$(dirname "$0")/.."

PINS_FILE="build/image-pins.env"

TOR_IMG="ghcr.io/brewsterkahle/onionpress-tor:latest"
WP_IMG="ghcr.io/brewsterkahle/onionpress-wordpress:latest"

MODE="refresh"
TOR_DIGEST_ARG=""
WP_DIGEST_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --propagate) MODE="propagate" ;;
        --check)     MODE="check" ;;
        --tor)       TOR_DIGEST_ARG="${2:-}"; shift ;;
        --wordpress) WP_DIGEST_ARG="${2:-}";  shift ;;
        -h|--help)   sed -n '2,45p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

# ─── Work out the two pins ──────────────────────────────────────────────

digest_of() {
    # Read the digest from `docker images --digests` for an exact repo:tag
    # match. Fail loudly if the image isn't cached locally — we'd rather
    # noise out than write an empty/wrong digest.
    #
    # This reports the digest of whatever manifest was pulled. For these two
    # tags that is the multi-arch INDEX digest (which is what a pin must be,
    # so it resolves on both amd64 and arm64), because GHCR serves the index
    # for a multi-arch tag. Do NOT reuse this shortcut for a base image where
    # a single-platform manifest may have been pulled — use
    # `docker buildx imagetools inspect <tag>` there instead.
    local img="$1"
    local d
    d=$(docker images --digests --format '{{.Repository}}:{{.Tag}} {{.Digest}}' \
        | awk -v i="$img" '$1==i {print $2; exit}')
    if [ -z "$d" ] || [ "$d" = "<none>" ]; then
        echo "ERROR: no digest for $img — run 'docker pull $img' first" >&2
        exit 1
    fi
    echo "$d"
}

read_pin() {
    # Read one KEY= line out of build/image-pins.env. Piped through cut so a
    # missing key yields an empty string rather than tripping `set -e` on
    # grep's exit status.
    grep "^$1=" "$PINS_FILE" 2>/dev/null | head -1 | cut -d= -f2-
}

if [ "$MODE" = "refresh" ]; then
    TOR_DIGEST="${TOR_DIGEST_ARG:-$(digest_of "$TOR_IMG")}"
    WP_DIGEST="${WP_DIGEST_ARG:-$(digest_of "$WP_IMG")}"
    for d in "$TOR_DIGEST" "$WP_DIGEST"; do
        case "$d" in
            sha256:*) ;;
            *) echo "ERROR: '$d' is not a sha256: digest" >&2; exit 1 ;;
        esac
    done
    TOR_PIN="${TOR_IMG}@${TOR_DIGEST}"
    WP_PIN="${WP_IMG}@${WP_DIGEST}"
    echo "tor:       $TOR_DIGEST"
    echo "wordpress: $WP_DIGEST"
else
    # --propagate / --check read whatever the pins file already says.
    [ -f "$PINS_FILE" ] || { echo "ERROR: $PINS_FILE not found" >&2; exit 1; }
    TOR_PIN="$(read_pin ONIONPRESS_TOR_IMAGE)"
    WP_PIN="$(read_pin ONIONPRESS_WORDPRESS_IMAGE)"
    if [ -z "$TOR_PIN" ] || [ -z "$WP_PIN" ]; then
        echo "ERROR: $PINS_FILE is missing ONIONPRESS_TOR_IMAGE or ONIONPRESS_WORDPRESS_IMAGE" >&2
        exit 1
    fi
    echo "tor:       $TOR_PIN"
    echo "wordpress: $WP_PIN"
fi

# ─── Rewrite the pins file (refresh mode only) ──────────────────────────
#
# Only the two value lines are touched; the explanatory header is preserved,
# so the file stays documentation as well as data.
if [ "$MODE" = "refresh" ]; then
    TOR_PIN="$TOR_PIN" WP_PIN="$WP_PIN" PINS_FILE="$PINS_FILE" python3 - <<'PY'
import os, pathlib, re
p = pathlib.Path(os.environ["PINS_FILE"])
s = p.read_text()
s = re.sub(r"(?m)^ONIONPRESS_TOR_IMAGE=.*$",
           "ONIONPRESS_TOR_IMAGE=" + os.environ["TOR_PIN"], s)
s = re.sub(r"(?m)^ONIONPRESS_WORDPRESS_IMAGE=.*$",
           "ONIONPRESS_WORDPRESS_IMAGE=" + os.environ["WP_PIN"], s)
p.write_text(s)
print(f"  updated: {p}")
PY
fi

# ─── Propagate into every consumer ──────────────────────────────────────
#
# Python rather than sed: BSD sed (macOS) needs `-i ''` while GNU sed
# (Linux/CI) rejects it, and we already require python3 for the build.
#
# The lookahead `(?=["}\n])` restricts rewrites to a *pin context* — a quoted
# string, a `${VAR:-…}` default, or end of line. It deliberately skips bare
# shell occurrences such as
#     if docker image inspect ghcr.io/…onionpress-tor:latest >/dev/null
# (linux/onionpress), which is a presence check that must stay tag-only: a
# local build tags `:latest` precisely so that check keeps passing.
#
# Not in this list, on purpose:
#   app/Resources/docker/tor/onionheaven_common.py — runs INSIDE the tor
#     container and defaults to the plain tag, because the right image there
#     is whichever one is already running.
#   tests/stress/Dockerfile — FROMs the image CI just built, so any static
#     digest is stale by construction.

TOR_PIN="$TOR_PIN" WP_PIN="$WP_PIN" MODE="$MODE" python3 - <<'PY'
import os, re, pathlib, sys

tor_pin = os.environ["TOR_PIN"]
wp_pin  = os.environ["WP_PIN"]
mode    = os.environ["MODE"]

tor_re = re.compile(r"ghcr\.io/brewsterkahle/onionpress-tor:latest(@sha256:[a-f0-9]+)?(?=[\"}\n])")
wp_re  = re.compile(r"ghcr\.io/brewsterkahle/onionpress-wordpress:latest(@sha256:[a-f0-9]+)?(?=[\"}\n])")

CONSUMERS = [
    "app/Resources/docker/docker-compose.yml",
    "app/MacOS/onionpress",
    "linux/onionpress",
    "src/onionpress/containers.py",
    "src/onionpress/launcher_ops.py",
]

stale = []
for path in CONSUMERS:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"ERROR: consumer missing: {path} — renamed? update this script.",
              file=sys.stderr)
        sys.exit(1)
    s = p.read_text()
    n = wp_re.sub(wp_pin, tor_re.sub(tor_pin, s))
    if n == s:
        continue
    if mode == "check":
        stale.append(path)
    else:
        p.write_text(n)
        print(f"  updated: {path}")

if mode == "check":
    if stale:
        print("", file=sys.stderr)
        print("ERROR: these files do not match build/image-pins.env:", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Fix with:  build/refresh-image-digests.sh --propagate", file=sys.stderr)
        sys.exit(1)
    print("  all consumers match build/image-pins.env")
PY

if [ "$MODE" != "check" ]; then
    echo
    echo "Updated. Review with: git diff"
fi
