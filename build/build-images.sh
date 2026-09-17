#!/bin/bash
#
# Build the OnionPress container images from the Dockerfiles in this repo.
#
# WHY THIS EXISTS
#   The tor and wordpress images only ever existed as GHCR artifacts produced
#   by .github/workflows/docker-publish.yml. The Dockerfiles were in the repo
#   but nothing invoked them, so "rebuild the tor container" meant reading the
#   workflow YAML and reconstructing the buildx invocation by hand — and the
#   arm64 half of that workflow runs on a self-hosted Mac an outside
#   contributor cannot reach at all. This script is the local path.
#
# WHAT IT PRODUCES
#   onionpress-tor:dev            from app/Resources/docker/tor
#   onionpress-wordpress:dev      from app/Resources/docker/wordpress
#   onionpress-stress-worker:dev  from tests/stress        (opt-in)
#
#   ...plus, unless --no-shadow-tag, the same image ID tagged as
#   ghcr.io/brewsterkahle/onionpress-tor:latest. See "SHADOW TAGS" below.
#
# USAGE
#   build/build-images.sh                       # tor + wordpress, native arch
#   build/build-images.sh tor                   # just one
#   build/build-images.sh all                   # adds stress-worker
#   build/build-images.sh --tag v2 wordpress    # onionpress-wordpress:v2
#   build/build-images.sh --platform linux/amd64,linux/arm64 --push ...
#
#   Then point the stack at what you built — the script prints these for you:
#     export ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
#     export ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:dev
#
# HOST REQUIREMENTS
#   docker with buildx. Any OS — these are Linux images, so unlike the .dmg
#   there is no host-OS constraint. On an Apple Silicon Mac you get arm64
#   natively; cross-building amd64 needs QEMU (see --platform below).
#
# HOW LONG
#   wordpress and stress-worker are seconds. The tor image compiles arti from
#   source (`cargo install arti`) and is tens of minutes cold, even natively.
#   Under QEMU emulation it is hours — which is exactly why CI splits the two
#   architectures across two native runners instead of cross-building.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Defaults ───────────────────────────────────────────────────────────

TAG="dev"
PLATFORM=""          # empty = buildx default = the host's native platform
PUSH=0
LOAD=1
NO_CACHE=0
PULL=0
SHADOW_TAG=1
REGISTRY_PREFIX=""   # set by --push, which needs a pushable name
CACHE_FROM=""
CACHE_TO=""
PROVENANCE="false"   # explicit: build-push-action defaults to attestations,
                     # raw buildx does not. Commit 419b53ec had to move from
                     # `docker manifest` to `buildx imagetools` over exactly
                     # this mismatch, so neither default is inherited here.
TARGETS=""

GHCR_TOR="ghcr.io/brewsterkahle/onionpress-tor"
GHCR_WP="ghcr.io/brewsterkahle/onionpress-wordpress"
GHCR_STRESS="ghcr.io/brewsterkahle/onionpress-stress-worker"

usage() {
    sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

OPTIONS
  -t, --tag TAG          Local tag to apply (default: dev)
      --platform LIST    Comma-separated, e.g. linux/amd64,linux/arm64.
                         More than one platform requires --push: Docker
                         cannot load a multi-platform result into the local
                         image store.
      --push             Push instead of loading locally. Requires
                         --registry, and a registry you are logged in to.
      --registry PREFIX  Registry prefix for --push, e.g.
                         ghcr.io/brewsterkahle
      --no-cache         Build without the layer cache.
      --pull             Re-resolve base images even if cached. Correct for
                         publish builds; skip it for iteration or every
                         change to rust:latest triggers a fresh arti compile.
      --cache-from SPEC  Passed through to buildx (CI uses type=gha,...).
      --cache-to SPEC    Passed through to buildx.
      --provenance VAL   Passed through to buildx (default: false).
      --no-shadow-tag    Skip the ghcr.io/... alias tag (see SHADOW TAGS).
  -h, --help             This text.

TARGETS
  tor, wordpress, stress-worker, all
  Default: tor wordpress. `all` adds stress-worker, which is a test-harness
  image and chains off the tor image you just built.

SHADOW TAGS
  The LINUX launcher gates vanity-address generation on
      docker image inspect ghcr.io/brewsterkahle/onionpress-tor:latest
  a deliberately tag-only presence check. If a local build is only tagged
  onionpress-tor:dev that check fails, and the install silently falls back to
  a random .onion instead of an op2… vanity address — the v2.4.101 regression.
  So a local tor build also tags the GHCR name by default. The tag points at
  your local image ID; it shadows the published image on this machine until
  you `docker pull` again. Use --no-shadow-tag to opt out.

  macOS does not consult Docker for this at all: it runs the bundled native
  $BIN_DIR/mkp224o from inside the .app, so the shadow tag is irrelevant
  there.
EOF
}

# ─── Parse ──────────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        -t|--tag)        TAG="${2:?--tag needs a value}"; shift ;;
        --platform)      PLATFORM="${2:?--platform needs a value}"; shift ;;
        --push)          PUSH=1; LOAD=0 ;;
        --registry)      REGISTRY_PREFIX="${2:?--registry needs a value}"; shift ;;
        --no-cache)      NO_CACHE=1 ;;
        --pull)          PULL=1 ;;
        --cache-from)    CACHE_FROM="${2:?--cache-from needs a value}"; shift ;;
        --cache-to)      CACHE_TO="${2:?--cache-to needs a value}"; shift ;;
        --provenance)    PROVENANCE="${2:?--provenance needs a value}"; shift ;;
        --no-shadow-tag) SHADOW_TAG=0 ;;
        -h|--help)       usage; exit 0 ;;
        tor|wordpress|stress-worker|all) TARGETS="$TARGETS $1" ;;
        *) echo "ERROR: unknown argument: $1" >&2; echo "Try --help." >&2; exit 1 ;;
    esac
    shift
done

[ -n "$TARGETS" ] || TARGETS="tor wordpress"
case "$TARGETS" in *all*) TARGETS="tor wordpress stress-worker" ;; esac

# ─── Preflight ──────────────────────────────────────────────────────────

if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'EOF'
ERROR: docker not found on PATH.

  These are Linux container images, so any Docker will do — Docker Desktop,
  Colima, Podman with a docker shim, or a remote DOCKER_HOST.

  OnionPress bundles its own docker CLI + Colima VM for running the app, at
  /Applications/OnionPress.app/Contents/Resources/bin/. Those work for a
  single-platform build (the bundle ships no buildx plugin, so this script
  falls back to the classic builder), but the VM is sized for running the
  stack, not for compiling arti:
      export PATH="/Applications/OnionPress.app/Contents/Resources/bin:$PATH"
      export COLIMA_HOME="$HOME/.onionpress/colima"
      export LIMA_HOME="$COLIMA_HOME/_lima"
      export DOCKER_CONFIG="$HOME/.onionpress/docker-config"
      export DOCKER_HOST="unix://$COLIMA_HOME/default/docker.sock"
      colima start
EOF
    exit 1
fi

# buildx is only REQUIRED for the features that only it has. A plain
# single-platform build works fine with the classic builder, and that matters
# here: OnionPress bundles its own docker CLI at
# Contents/Resources/bin/docker with no buildx plugin, so demanding buildx
# unconditionally locked developers out of the very toolchain this app ships.
BUILDER="buildx"
if ! docker buildx version >/dev/null 2>&1; then
    BUILDER="classic"
    needs_buildx=""
    [ -n "$PLATFORM" ]   && needs_buildx="$needs_buildx --platform"
    [ "$PUSH" = "1" ]    && needs_buildx="$needs_buildx --push"
    [ -n "$CACHE_FROM" ] && needs_buildx="$needs_buildx --cache-from"
    [ -n "$CACHE_TO" ]   && needs_buildx="$needs_buildx --cache-to"
    if [ -n "$needs_buildx" ]; then
        cat >&2 <<EOF
ERROR: 'docker buildx' is unavailable, but you asked for:$needs_buildx

  Those options exist only in buildx. Drop them to build for this host with
  the classic builder, or install buildx:
    macOS       brew install docker-buildx
    docker-ce   apt-get install docker-buildx-plugin
  then link it where your docker CLI looks for plugins, e.g.
    mkdir -p ~/.docker/cli-plugins
    ln -sfn "\$(brew --prefix)/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
EOF
        exit 1
    fi
    echo "NOTE: docker buildx not found — using the classic builder."
    echo "      Fine for a single-platform local build; --platform/--push need buildx."
fi

if ! docker version >/dev/null 2>&1; then
    echo "ERROR: cannot reach a Docker daemon (is it running?)." >&2
    echo "  Colima users: colima start" >&2
    exit 1
fi

# Multi-platform cannot be --load'ed: the local image store holds one manifest
# per tag. Fail here with the reason rather than 20 minutes into an arti build.
PLATFORM_COUNT=1
if [ -n "$PLATFORM" ]; then
    PLATFORM_COUNT=$(echo "$PLATFORM" | awk -F, '{print NF}')
fi
if [ "$PLATFORM_COUNT" -gt 1 ] && [ "$PUSH" = "0" ]; then
    cat >&2 <<EOF
ERROR: --platform lists $PLATFORM_COUNT platforms but --push was not given.

  Docker cannot load a multi-platform build into the local image store —
  a tag there resolves to exactly one manifest. Either:
    * drop --platform to build natively for this host, or
    * add --push --registry <prefix> to publish a multi-arch manifest.

  Note that cross-building the tor image is a QEMU-emulated Rust compile and
  takes hours. CI avoids it entirely by building each architecture on its own
  native runner and merging with 'buildx imagetools create'.
EOF
    exit 1
fi

if [ "$PUSH" = "1" ] && [ -z "$REGISTRY_PREFIX" ]; then
    echo "ERROR: --push requires --registry (e.g. --registry ghcr.io/brewsterkahle)." >&2
    exit 1
fi

# ─── Build one image ────────────────────────────────────────────────────

# image_ref <target> — the primary name:tag this build produces.
image_ref() {
    if [ -n "$REGISTRY_PREFIX" ]; then
        echo "${REGISTRY_PREFIX}/onionpress-$1:${TAG}"
    else
        echo "onionpress-$1:${TAG}"
    fi
}

# ghcr_name <target> — the published name, used for the shadow tag.
ghcr_name() {
    case "$1" in
        tor)           echo "$GHCR_TOR" ;;
        wordpress)     echo "$GHCR_WP" ;;
        stress-worker) echo "$GHCR_STRESS" ;;
    esac
}

context_for() {
    case "$1" in
        tor)           echo "$PROJECT_DIR/app/Resources/docker/tor" ;;
        wordpress)     echo "$PROJECT_DIR/app/Resources/docker/wordpress" ;;
        stress-worker) echo "$PROJECT_DIR/tests/stress" ;;
    esac
}

build_one() {
    local target="$1"
    local context ref
    context="$(context_for "$target")"
    ref="$(image_ref "$target")"

    [ -d "$context" ] || { echo "ERROR: build context missing: $context" >&2; exit 1; }
    [ -f "$context/Dockerfile" ] || { echo "ERROR: no Dockerfile in $context" >&2; exit 1; }

    echo ""
    echo "── Building $target ─────────────────────────────────────────────────"
    echo "   context: ${context#$PROJECT_DIR/}"
    echo "   tag:     $ref"
    [ -n "$PLATFORM" ] && echo "   platform: $PLATFORM"

    # bash 3.2: no arrays-of-arrays, so accumulate into a positional list.
    set -- build
    [ -n "$PLATFORM" ] && set -- "$@" --platform "$PLATFORM"
    set -- "$@" -t "$ref"


    # Shadow tag, so the launchers' tag-only presence check keeps passing.
    if [ "$SHADOW_TAG" = "1" ] && [ "$PUSH" = "0" ]; then
        set -- "$@" -t "$(ghcr_name "$target"):latest"
    fi

    # stress-worker extends the tor image; chain it off the local build so
    # `build-images.sh all` tests your tor changes rather than published ones.
    if [ "$target" = "stress-worker" ]; then
        local base
        if [ "$SHADOW_TAG" = "1" ] && [ "$PUSH" = "0" ]; then
            base="$(ghcr_name tor):latest"
        else
            base="$(image_ref tor)"
        fi
        echo "   base:    $base"
        set -- "$@" --build-arg "TOR_IMAGE=$base"
    fi

    [ "$NO_CACHE" = "1" ] && set -- "$@" --no-cache
    [ "$PULL" = "1" ] && set -- "$@" --pull

    if [ "$BUILDER" = "buildx" ]; then
        [ -n "$CACHE_FROM" ] && set -- "$@" --cache-from "$CACHE_FROM"
        [ -n "$CACHE_TO" ] && set -- "$@" --cache-to "$CACHE_TO"
        set -- "$@" --provenance "$PROVENANCE"
        [ "$PUSH" = "1" ] && set -- "$@" --push
        [ "$LOAD" = "1" ] && set -- "$@" --load
        set -- "$@" "$context"
        docker buildx "$@"
    else
        # Classic builder: no --provenance/--load/--push/--cache-* to pass.
        # The image lands in the local store directly, which is what --load
        # accomplishes on the buildx path.
        set -- "$@" "$context"
        docker "$@"
    fi

    if [ "$LOAD" = "1" ]; then
        local id
        id=$(docker image inspect "$ref" --format '{{.Id}}' 2>/dev/null || echo "?")
        echo "   built:   $id"
    fi
}

# ─── Go ─────────────────────────────────────────────────────────────────

echo "OnionPress image build"
echo "  targets: $(echo $TARGETS)"

# Order matters: stress-worker FROMs the tor image, so tor must exist first.
for target in tor wordpress stress-worker; do
    case " $TARGETS " in *" $target "*) build_one "$target" ;; esac
done

echo ""
echo "✅ Done."

if [ "$PUSH" = "0" ]; then
    cat <<EOF

Point the stack at what you just built:

  export ONIONPRESS_TOR_IMAGE=$(image_ref tor)
  export ONIONPRESS_WORDPRESS_IMAGE=$(image_ref wordpress)

Every consumer reads those — compose's tor/wordpress/onionheaven services,
both launchers, the OnionHeaven farm workers and vanity-key generation.
Check the result without starting anything:

  cd app/Resources/docker && docker compose config | grep image:

Note: the running app pulls on start, which would clobber a local tag. Use
build/dev-up.sh, which sets these and disables the pulls for you.
EOF
fi
