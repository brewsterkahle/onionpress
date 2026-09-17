#!/bin/bash
#
# Bring up the OnionPress stack from locally built images.
#
# WHY THIS EXISTS
#   Building the images is only half of a local build path — the running app
#   fights you. Every start ran `docker compose pull`, and `start-tor` ran
#   `docker compose up --pull always`, so a locally built tag was overwritten
#   by the registry's copy before it ever started. You would build an image,
#   launch the app, and silently test someone else's build. Those pulls are
#   now gated on ONIONPRESS_TOR_IMAGE / ONIONPRESS_WORDPRESS_IMAGE pointing at
#   a non-ghcr.io reference; this script sets that up and starts the stack.
#
# USAGE
#   build/build-images.sh          # produce onionpress-{tor,wordpress}:dev
#   build/dev-up.sh                # start the stack on them
#   build/dev-up.sh --down         # stop it
#   build/dev-up.sh --tag v2       # use a different local tag
#
# WHAT IT TOUCHES
#   The same Docker volumes and container names the real app uses —
#   docker-compose.yml hardcodes both (container_name:, volumes: name:), so a
#   dev stack CANNOT run alongside an installed OnionPress. This script
#   refuses to start if the app is already running rather than fighting it.
#
#   Database passwords come from ~/.onionpress/secrets, generated the same way
#   the launchers generate them if the file does not exist yet. Using
#   different passwords against an existing db-data volume would fail to
#   authenticate, so reusing that file is deliberate.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_DIR="$PROJECT_DIR/app/Resources/docker"
DATA_DIR="$HOME/.onionpress"
SECRETS_FILE="$DATA_DIR/secrets"

TAG="dev"
ACTION="up"
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --down)  ACTION="down" ;;
        --logs)  ACTION="logs" ;;
        -t|--tag) TAG="${2:?--tag needs a value}"; shift ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found on PATH." >&2; exit 1; }
docker version >/dev/null 2>&1 || { echo "ERROR: cannot reach a Docker daemon." >&2; exit 1; }

export ONIONPRESS_TOR_IMAGE="onionpress-tor:${TAG}"
export ONIONPRESS_WORDPRESS_IMAGE="onionpress-wordpress:${TAG}"

cd "$DOCKER_DIR"

# ─── down / logs ────────────────────────────────────────────────────────

if [ "$ACTION" = "down" ]; then
    echo "Stopping the OnionPress stack..."
    # No -v: the volumes hold the user's site. Removing them is never
    # something a "stop the dev stack" command should do silently.
    docker compose down
    echo "Stopped. Volumes kept (they hold your site data)."
    exit 0
fi

if [ "$ACTION" = "logs" ]; then
    exec docker compose logs -f
fi

# ─── Preflight ──────────────────────────────────────────────────────────

missing=""
for image in "$ONIONPRESS_TOR_IMAGE" "$ONIONPRESS_WORDPRESS_IMAGE"; do
    docker image inspect "$image" >/dev/null 2>&1 || missing="$missing $image"
done
if [ -n "$missing" ]; then
    cat >&2 <<EOF
ERROR: these locally built images do not exist:$missing

  Build them first:
      build/build-images.sh --tag ${TAG}

  Or point at a published image instead by exporting ONIONPRESS_TOR_IMAGE /
  ONIONPRESS_WORDPRESS_IMAGE yourself and running docker compose directly.
EOF
    exit 1
fi

# docker-compose.yml hardcodes container_name: and volume name:, so there is
# exactly one OnionPress stack per Docker daemon. Starting a second one does
# not isolate anything — it takes the first one's containers over.
if [ "$FORCE" = "0" ]; then
    running=$(docker ps --format '{{.Names}}' 2>/dev/null \
              | grep -E '^(onionpress-|onionheaven)' || true)
    if [ -n "$running" ]; then
        cat >&2 <<EOF
ERROR: an OnionPress stack is already running:

$(echo "$running" | sed 's/^/  /')

  docker-compose.yml hardcodes container names and volume names, so a dev
  stack cannot run alongside an installed OnionPress — it would take these
  containers over. Quit the OnionPress app (or run build/dev-up.sh --down),
  then try again.

  --force starts anyway, replacing the containers above.
EOF
        exit 1
    fi
fi

# ─── Secrets ────────────────────────────────────────────────────────────
#
# Same generation the launchers do on first run, and the same file, because
# the db-data volume is keyed to whatever password created it.
mkdir -p "$DATA_DIR"
if [ ! -f "$SECRETS_FILE" ]; then
    echo "Generating database passwords -> $SECRETS_FILE"
    wordpress_pass=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    root_pass=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    touch "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
    cat > "$SECRETS_FILE" <<EOF
# Database passwords - generated on $(date)
# DO NOT SHARE THESE PASSWORDS
WORDPRESS_DB_PASSWORD='$wordpress_pass'
MYSQL_PASSWORD='$wordpress_pass'
MYSQL_ROOT_PASSWORD='$root_pass'
EOF
    unset wordpress_pass root_pass
fi
# shellcheck source=/dev/null
. "$SECRETS_FILE"
export WORDPRESS_DB_PASSWORD MYSQL_PASSWORD MYSQL_ROOT_PASSWORD

# Compose interpolates these; without them the Creations bind-mount and the
# analytics mount land in /tmp. Matches what the launchers export.
export ONIONPRESS_SHARED_DIR="$DATA_DIR/shared"
export ONIONPRESS_DOCUMENTS_DIR="$HOME/OnionPress"
mkdir -p "$ONIONPRESS_SHARED_DIR/analytics" "$ONIONPRESS_DOCUMENTS_DIR/Creations/My Creations"
export ONIONPRESS_VERSION="dev"

# ─── Up ─────────────────────────────────────────────────────────────────

echo "Starting the OnionPress stack on locally built images"
echo "  tor:       $ONIONPRESS_TOR_IMAGE"
echo "  wordpress: $ONIONPRESS_WORDPRESS_IMAGE"
echo ""
docker compose config | grep -E "^    image:" | sed 's/^/  resolved /'
echo ""

# No --pull: these images exist only on this machine.
docker compose up -d

echo ""
echo "✅ Up."
echo ""
echo "  WordPress:  http://localhost:8080"
echo "  Logs:       build/dev-up.sh --logs"
echo "  Stop:       build/dev-up.sh --down"
echo ""
echo "The onion address takes a minute or two to publish. Watch for it with:"
echo "  docker exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname"
echo ""
echo "To make an installed OnionPress.app use these images too, add to"
echo "$DATA_DIR/config:"
echo "  ONIONPRESS_TOR_IMAGE=$ONIONPRESS_TOR_IMAGE"
echo "  ONIONPRESS_WORDPRESS_IMAGE=$ONIONPRESS_WORDPRESS_IMAGE"
