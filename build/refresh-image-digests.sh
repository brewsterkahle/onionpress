#!/bin/bash
# Keep every OnionPress container-image pin in sync from one source of truth.
#
# THE SOURCE OF TRUTH IS build/image-pins.env. This script is the only thing
# that writes it, and the only thing that writes the literals embedded in the
# consumers below. tests/test_image_pins.py fails the build if they drift.
#
# Consumers (all rewritten together — that is the whole point):
#   app/Resources/docker/docker-compose.yml   tor, wordpress, onionheaven
#                                             (+ its ONIONHEAVEN_IMAGE env),
#                                             db, autoheal, autoheal-proxy
#   app/Resources/docker/docker-compose.cloudflare.yml   cloudflared
#   app/MacOS/onionpress                      KEY_HELPER_IMAGE (one-shot key helpers)
#   linux/onionpress                          KEY_HELPER_IMAGE (one-shot key helpers)
#   src/onionpress/containers.py              ONIONHEAVEN_IMAGE
#   src/onionpress/launcher_ops.py            DEFAULT_TOR_IMAGE
#
# Six pins: the two images this project builds (tor, wordpress) and four
# third-party images the stack runs as published (mariadb, autoheal,
# cloudflared, the autoheal socket proxy). The four are pinned to OUR copies
# (issue #501): build/image-mirrors.txt names each one's upstream and its
# package under ghcr.io/brewsterkahle/. A pin carries the upstream's own
# multi-arch index digest (copying an index keeps it), so this script
# resolves the digest from the UPSTREAM and writes the mirror's name, and
# .github/workflows/mirror-images.yml makes the copy when the pins change.
# Arguments below name UPSTREAM tags. Through v2.5.3 the third-party
# ones floated on `:latest` and every macOS launch ran an unconditional
# `docker compose pull`, so a registry could swap them under every user and
# each launch did clearnet DNS and registry traffic for nothing (issue #36).
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
# WHERE THE DIGESTS COME FROM
#   By default, from the registry: build/base-image-digest.sh asks GHCR what
#   :latest is right now (multi-arch index digest, no Docker daemon needed),
#   and re-resolves the third-party tags already pinned the same way.
#   Until issue #327 the ghcr.io default was `docker images --digests`, i.e.
#   whatever this machine last pulled, and during the 2.5.3 release it wrote
#   stale digests. The local cache is still available, but only when asked
#   for (--local, the two ghcr.io images only).
#
# USAGE
#   # Normal release flow — pin what GHCR serves as :latest now, and
#   # re-resolve the third-party tags:
#   build/refresh-image-digests.sh
#
#   # Supply digests explicitly (either one; the other comes from the registry):
#   build/refresh-image-digests.sh --tor sha256:aaa… --wordpress sha256:bbb…
#
#   # Pin the ghcr.io images this machine's Docker last pulled (testing):
#   build/refresh-image-digests.sh --local
#
#   # Move a third-party image to a new tag (resolved to its multi-arch index
#   # digest; a single-platform tag is refused):
#   build/refresh-image-digests.sh --mariadb mariadb:13.0.3
#   build/refresh-image-digests.sh --cloudflared cloudflare/cloudflared:2026.11.0
#   build/refresh-image-digests.sh --socket-proxy ghcr.io/wollomatic/socket-proxy:1.14.0
#   build/refresh-image-digests.sh --autoheal willfarrell/autoheal:latest
#
#   # Re-apply build/image-pins.env to every consumer, no network needed.
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

# The third-party pins, in the order they appear in build/image-pins.env.
THIRD_PARTY_KEYS=(MARIADB_IMAGE AUTOHEAL_IMAGE CLOUDFLARED_IMAGE SOCKET_PROXY_IMAGE)

# Where each third-party image is mirrored: <KEY>_UPSTREAM is the repository
# it comes from, <KEY>_MIRROR the repository the pin names.
MIRRORS_FILE="build/image-mirrors.txt"
MIRROR_NAMESPACE="ghcr.io/brewsterkahle"

MODE="refresh"
SOURCE="registry"
TOR_DIGEST_ARG=""
WP_DIGEST_ARG=""
MARIADB_REF_ARG=""
AUTOHEAL_REF_ARG=""
CLOUDFLARED_REF_ARG=""
SOCKET_PROXY_REF_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --propagate)    MODE="propagate" ;;
        --check)        MODE="check" ;;
        --tor)          TOR_DIGEST_ARG="${2:-}"; shift ;;
        --wordpress)    WP_DIGEST_ARG="${2:-}";  shift ;;
        --mariadb)      MARIADB_REF_ARG="${2:-}"; shift ;;
        --autoheal)     AUTOHEAL_REF_ARG="${2:-}"; shift ;;
        --cloudflared)  CLOUDFLARED_REF_ARG="${2:-}"; shift ;;
        --socket-proxy) SOCKET_PROXY_REF_ARG="${2:-}"; shift ;;
        --local)        SOURCE="local" ;;
        -h|--help)      sed -n '2,70p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

# ─── Work out the pins ──────────────────────────────────────────────────

registry_digest_of() {
    # Ask the registry what the tag points at now. base-image-digest.sh
    # prints tag@sha256:… and exits non-zero (with its own message) if the
    # registry cannot be reached or the tag is a single-platform manifest —
    # either way we stop rather than write an empty or one-arch pin.
    local img="$1"
    local ref
    ref=$(build/base-image-digest.sh "$img") || {
        echo "ERROR: could not resolve $img from the registry" \
             "(pass --tor/--wordpress, or --local to use this machine's cache)" >&2
        exit 1
    }
    echo "${ref##*@}"
}

local_digest_of() {
    # --local only. Read the digest from `docker images --digests` for an
    # exact repo:tag match: whatever this machine last pulled, which is not
    # necessarily what the registry serves now (issue #327). Fail loudly if
    # the image isn't cached locally — we'd rather noise out than write an
    # empty/wrong digest.
    #
    # This reports the digest of whatever manifest was pulled. For these two
    # tags that is the multi-arch INDEX digest (which is what a pin must be,
    # so it resolves on both amd64 and arm64), because GHCR serves the index
    # for a multi-arch tag. Do NOT reuse this shortcut for a third-party
    # image where a single-platform manifest may have been pulled — those go
    # through build/base-image-digest.sh, which refuses anything but an index.
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
    # Read one KEY= line out of build/image-pins.env.
    #
    # The trailing `|| :` is required. The launchers use this same
    # grep|head|cut idiom under plain `set -e`, where the pipeline's status is
    # cut's and a missing key harmlessly yields "". This script also sets
    # `pipefail`, which propagates grep's exit 1 through the pipe — so without
    # `|| :` a missing key killed the script at the assignment below, before
    # the explanatory guard could print, leaving the operator with exit 1 and
    # zero output while doctor.sh told them the pins had "DRIFTED".
    grep "^$1=" "$PINS_FILE" 2>/dev/null | head -1 | cut -d= -f2- || :
}

digest_of() {
    if [ "$SOURCE" = "local" ]; then
        local_digest_of "$1"
    else
        registry_digest_of "$1"
    fi
}

require_pin() {
    local key="$1" value="$2"
    if [ -z "$value" ]; then
        echo "ERROR: $PINS_FILE is missing $key" >&2
        exit 1
    fi
}

check_digest() {
    case "$1" in
        *@sha256:[0-9a-f]*) ;;
        *) echo "ERROR: '$1' is not pinned to a sha256: digest" >&2; exit 1 ;;
    esac
}

[ -f "$PINS_FILE" ] || { echo "ERROR: $PINS_FILE not found" >&2; exit 1; }

# Third-party pins are read from the file in every mode: a refresh starts
# from the tag already pinned unless an argument moves it. <KEY>_OLD is what
# the file says, <KEY>_PIN what will be written (macOS /bin/bash is 3.2, so
# no associative arrays: indirect expansion and eval instead).
for key in "${THIRD_PARTY_KEYS[@]}"; do
    _val="$(read_pin "$key")"
    require_pin "$key" "$_val"
    eval "${key}_OLD=\$_val; ${key}_PIN=\$_val"
done
unset _val

[ -f "$MIRRORS_FILE" ] || { echo "ERROR: $MIRRORS_FILE not found" >&2; exit 1; }
for key in "${THIRD_PARTY_KEYS[@]}"; do
    _row="$(grep -E "^$key " "$MIRRORS_FILE" | head -1 || :)"
    [ -n "$_row" ] || { echo "ERROR: $MIRRORS_FILE has no row for $key" >&2; exit 1; }
    set -- $_row
    eval "${key}_UPSTREAM=\$2; ${key}_MIRROR=\$MIRROR_NAMESPACE/\$3"
done
unset _row

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
    echo "tor:          $TOR_DIGEST"
    echo "wordpress:    $WP_DIGEST"

    # Third-party images: resolve the tag (the requested one, else the one
    # already pinned) to its multi-arch index digest over the registry API.
    # base-image-digest.sh exits 1 for a single-platform manifest, which is
    # exactly the mistake a hand-pinned digest invites.
    resolve_third_party() {
        local key="$1" arg="$2" old_var up_var mirror_var old tag ref resolved pin
        old_var="${key}_OLD"; up_var="${key}_UPSTREAM"; mirror_var="${key}_MIRROR"
        old="${!old_var%%@*}"
        # The tag already pinned, on the upstream, unless an argument moves it.
        ref="${arg:-${!up_var}:${old##*:}}"
        resolved="$(build/base-image-digest.sh "$ref")"
        check_digest "$resolved"
        # The tag is what follows the last colon of the reference proper.
        tag="${resolved%%@*}"; tag="${tag##*:}"
        pin="${!mirror_var}:${tag}@${resolved##*@}"
        eval "${key}_PIN=\$pin"
    }
    resolve_third_party MARIADB_IMAGE "$MARIADB_REF_ARG"
    resolve_third_party AUTOHEAL_IMAGE "$AUTOHEAL_REF_ARG"
    resolve_third_party CLOUDFLARED_IMAGE "$CLOUDFLARED_REF_ARG"
    resolve_third_party SOCKET_PROXY_IMAGE "$SOCKET_PROXY_REF_ARG"
else
    # --propagate / --check read whatever the pins file already says.
    TOR_PIN="$(read_pin ONIONPRESS_TOR_IMAGE)"
    WP_PIN="$(read_pin ONIONPRESS_WORDPRESS_IMAGE)"
    require_pin ONIONPRESS_TOR_IMAGE "$TOR_PIN"
    require_pin ONIONPRESS_WORDPRESS_IMAGE "$WP_PIN"
    echo "tor:          $TOR_PIN"
    echo "wordpress:    $WP_PIN"
fi
for key in "${THIRD_PARTY_KEYS[@]}"; do
    _pin_var="${key}_PIN"
    check_digest "${!_pin_var}"
    printf '%-13s %s\n' "$(echo "${key%_IMAGE}" | tr 'A-Z_' 'a-z-'):" "${!_pin_var}"
done
unset _pin_var

# ─── Rewrite the pins file (refresh mode only) ──────────────────────────
#
# Only the value lines are touched; the explanatory header is preserved,
# so the file stays documentation as well as data.
if [ "$MODE" = "refresh" ]; then
    TOR_PIN="$TOR_PIN" WP_PIN="$WP_PIN" PINS_FILE="$PINS_FILE" \
    MARIADB_PIN="$MARIADB_IMAGE_PIN" MARIADB_OLD="$MARIADB_IMAGE_OLD" \
    AUTOHEAL_PIN="$AUTOHEAL_IMAGE_PIN" \
    CLOUDFLARED_PIN="$CLOUDFLARED_IMAGE_PIN" \
    SOCKET_PROXY_PIN="$SOCKET_PROXY_IMAGE_PIN" \
    python3 - <<'PY'
import os, pathlib, re, sys

def version(ref):
    m = re.search(r":(\d+)\.(\d+)\.(\d+)(?:@|$)", ref)
    return tuple(int(x) for x in m.groups()) if m else None

# mariadb pins move forward only: the data directory follows whatever
# version has run on it (MARIADB_AUTO_UPGRADE=1), and an older server
# refuses a newer directory. See build/image-pins.env.
old, new = version(os.environ["MARIADB_OLD"]), version(os.environ["MARIADB_PIN"])
if old and new and new < old:
    sys.exit(f"ERROR: mariadb pin {os.environ['MARIADB_PIN']} is older than "
             f"the current {os.environ['MARIADB_OLD']}; an install that ran "
             "the newer one would refuse to start. Pin forward only.")

p = pathlib.Path(os.environ["PINS_FILE"])
s = p.read_text()
for key, env in (("ONIONPRESS_TOR_IMAGE", "TOR_PIN"),
                 ("ONIONPRESS_WORDPRESS_IMAGE", "WP_PIN"),
                 ("MARIADB_IMAGE", "MARIADB_PIN"),
                 ("AUTOHEAL_IMAGE", "AUTOHEAL_PIN"),
                 ("CLOUDFLARED_IMAGE", "CLOUDFLARED_PIN"),
                 ("SOCKET_PROXY_IMAGE", "SOCKET_PROXY_PIN")):
    s, n = re.subn(rf"(?m)^{key}=.*$", key + "=" + os.environ[env], s)
    if n != 1:
        sys.exit(f"ERROR: expected exactly one {key}= line in {p}, found {n}")
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
# string, a `${VAR:-…}` default, or end of line.
#
# That is not sufficient on its own. `docker image inspect` lines are presence
# checks that MUST stay tag-only — digest-pinning one makes a locally built
# image fail it and the install silently falls back to a random .onion (the
# v2.4.101 regression). They used to be skipped incidentally, because the
# reference was bare and followed by a space. Once linux/onionpress grew an
# override, `"${ONIONPRESS_TOR_IMAGE:-ghcr.io/…:latest}"` put the very same
# check *into* pin context and this script would have pinned it. So they are
# now excluded explicitly, by what the line does rather than by how it is
# punctuated.
#
# A third-party reference is matched by repository with ANY tag, digest or
# not: `mariadb:latest` in a consumer is drift from `mariadb:13.0.2@sha256:…`
# and gets rewritten (so --check catches a floating tag creeping back in).
# The lookbehind keeps `mariadb` from matching inside another path.
#
# Not in this list, on purpose:
#   app/Resources/docker/tor/onionheaven_common.py — runs INSIDE the tor
#     container, where the right image is whichever one is already running:
#     it takes ONIONHEAVEN_IMAGE from the compose file above, else asks
#     Docker for its own container's image ID. It embeds no reference.
#   tests/stress/Dockerfile — FROMs the image CI just built, so any static
#     digest is stale by construction.

TOR_PIN="$TOR_PIN" WP_PIN="$WP_PIN" MODE="$MODE" \
MARIADB_PIN="$MARIADB_IMAGE_PIN" \
AUTOHEAL_PIN="$AUTOHEAL_IMAGE_PIN" \
CLOUDFLARED_PIN="$CLOUDFLARED_IMAGE_PIN" \
SOCKET_PROXY_PIN="$SOCKET_PROXY_IMAGE_PIN" \
python3 - <<'PY'
import os, re, pathlib, sys

mode = os.environ["MODE"]

def tagged(repo_tag):
    """Pattern for a project image: exact repo:tag, digest optional."""
    return re.compile(re.escape(repo_tag) + r"(@sha256:[a-f0-9]+)?(?=[\"}\n])")

UPSTREAMS = {}
for row in pathlib.Path("build/image-mirrors.txt").read_text().splitlines():
    if row.strip() and not row.lstrip().startswith("#"):
        key, upstream, _ = row.split()
        UPSTREAMS[key] = upstream

def any_tag(key, pin):
    """Pattern for a third-party image: the mirror's repository (the pin's)
    or the upstream's it was copied from, with any tag. An upstream name in
    a consumer is a pin from before the mirror and is moved to it."""
    mirror = pin.split("@", 1)[0].rsplit(":", 1)[0]
    repos = "|".join(re.escape(r) for r in (mirror, UPSTREAMS[key]))
    return re.compile(r"(?<![\w./-])(?:" + repos
                      + r"):[\w][\w.-]*(@sha256:[a-f0-9]+)?(?=[\"}\n])")

REWRITES = [
    (tagged("ghcr.io/brewsterkahle/onionpress-tor:latest"), os.environ["TOR_PIN"]),
    (tagged("ghcr.io/brewsterkahle/onionpress-wordpress:latest"), os.environ["WP_PIN"]),
] + [(any_tag(key, os.environ[env]), os.environ[env])
     for key, env in (("MARIADB_IMAGE", "MARIADB_PIN"),
                      ("AUTOHEAL_IMAGE", "AUTOHEAL_PIN"),
                      ("CLOUDFLARED_IMAGE", "CLOUDFLARED_PIN"),
                      ("SOCKET_PROXY_IMAGE", "SOCKET_PROXY_PIN"))]

CONSUMERS = [
    "app/Resources/docker/docker-compose.yml",
    "app/Resources/docker/docker-compose.cloudflare.yml",
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
    # Rewrite line by line so presence checks can be held back.
    out = []
    for line in s.splitlines(keepends=True):
        if "image inspect" in line:
            out.append(line)
            continue
        for pattern, pin in REWRITES:
            line = pattern.sub(pin, line)
        out.append(line)
    n = "".join(out)
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
