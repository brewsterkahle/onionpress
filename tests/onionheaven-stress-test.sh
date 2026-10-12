#!/bin/bash
# OBSOLETE — use onionheaven-stress-test.py instead
# OnionHeaven Stress Test — Real Arti Onion Services
#
# Architecture:
#   - Site containers: each runs Arti with N real onion services + a single
#     Python HTTP server handling all ports (replaces per-site socat processes).
#   - Each site self-registers with OnionHeaven over Tor, just like a real
#     OnionPress instance.
#   - OnionHeaven Tor container: never modified by this script.
#   - This script: orchestrates containers, monitors dashboard, controls failures.
#
# Scaling:
#   - Each container handles --per-ctr sites (default 50).
#   - For 1000 sites: 20 containers × 50 sites each.
#   - Only 1 docker exec -d per container (vs 2 per site in old architecture).
#   - macOS process limit is no longer a bottleneck.
#
# Usage:
#   # Quick test — 5 sites in 1 container
#   ./onionheaven-stress-test.sh --total 5
#
#   # Scale test — 100 sites across 2 containers
#   ./onionheaven-stress-test.sh --total 100 --per-ctr 50
#
#   # Big test — 1000 sites across 20 containers, start 5 at a time
#   ./onionheaven-stress-test.sh --total 1000 --per-ctr 50 --batch-size 5
#
#   # Monitor dashboard
#   ./onionheaven-stress-test.sh --mode coordinator
#
#   # Clean up (all stress test artifacts)
#   ./onionheaven-stress-test.sh --cleanup
#
#   # Test against a specific OnionHeaven node (e.g. a Pi)
#   ./onionheaven-stress-test.sh --total 5 --onionheaven-addr op2pie...ad.onion
#
#   # Fast bootstrap — skip healthcheck onion services per site (halves circuit load)
#   ./onionheaven-stress-test.sh --total 5 --no-healthcheck
#
#   # Clean up only stale stress tests (no activity in 2+ hours)
#   ./onionheaven-stress-test.sh --cleanup-stale
#   ./onionheaven-stress-test.sh --cleanup-stale --stale-hours 1

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
MODE="worker"
TOTAL=5           # default 5 sites
HEALTHY=""        # auto: half of total
FAILING=""        # auto: half of total
ONIONHEAVEN_ADDR=""    # auto-detect from local tor container
OUTPUT_DIR="./onionheaven-stress-results"
CLEANUP=false
CLEANUP_STALE=false
STALE_HOURS=2
PER_CTR=20        # sites per container
BATCH_SIZE=0      # 0 = start all containers at once
NUM_POLL_CLIENTS=0  # auto-scaled: 1 per 10 sites (set after TOTAL is parsed)
STRESS_VERSION="stress-test-$(date +%Y%m%d-%H%M%S)-$$"
BASE_PORT=9100    # port range start inside each container
IS_ONIONHEAVEN_HOST=false  # auto-detected in preflight
NO_HEALTHCHECK=false  # skip healthcheck onion services (halves circuit load)
NO_TIMEOUT=false      # disable timeouts (wait indefinitely for takeover/recovery)

DATA_DIR="$HOME/.onionpress"
DOCKER_HOST_SOCK=""
if [ -S "${DATA_DIR}/colima/default/docker.sock" ]; then
    DOCKER_HOST_SOCK="unix://${DATA_DIR}/colima/default/docker.sock"
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --total)       TOTAL="$2"; shift 2 ;;
        --healthy)     HEALTHY="$2"; shift 2 ;;
        --failing)     FAILING="$2"; shift 2 ;;
        --per-ctr)     PER_CTR="$2"; shift 2 ;;
        --onionheaven-addr) ONIONHEAVEN_ADDR="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --batch-size)  BATCH_SIZE="$2"; shift 2 ;;
        --no-healthcheck) NO_HEALTHCHECK=true; shift ;;
        --no-timeout)  NO_TIMEOUT=true; shift ;;
        --cleanup)     CLEANUP=true; shift ;;
        --cleanup-stale) CLEANUP_STALE=true; shift ;;
        --stale-hours)   STALE_HOURS="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Timeout values (--no-timeout sets to 24 hours)
if [ "$NO_TIMEOUT" = true ]; then
    BOOTSTRAP_TIMEOUT=86400
    TAKEOVER_TIMEOUT=86400
    RECOVERY_TIMEOUT=86400

    HEALTHY_TIMEOUT=86400
    REDIRECT_VERIFY_TIMEOUT=86400
else
    BOOTSTRAP_TIMEOUT=900
    TAKEOVER_TIMEOUT=600
    RECOVERY_TIMEOUT=600

    HEALTHY_TIMEOUT=600
    REDIRECT_VERIFY_TIMEOUT=300
fi

# Compute healthy/failing split
if [ -z "$HEALTHY" ] && [ -z "$FAILING" ]; then
    FAILING=$((TOTAL / 2))
    HEALTHY=$((TOTAL - FAILING))
elif [ -z "$HEALTHY" ]; then
    HEALTHY=$((TOTAL - FAILING))
elif [ -z "$FAILING" ]; then
    FAILING=$((TOTAL - HEALTHY))
else
    TOTAL=$((HEALTHY + FAILING))
fi

# Compute number of containers
NUM_CONTAINERS=$(( (TOTAL + PER_CTR - 1) / PER_CTR ))

# Auto-scale poll clients: 1 per 3 failing sites (each Tor client handles ~4
# concurrent onion service lookups reliably, so 2-3 per client leaves headroom).
# At 100 sites (50 failing): ~17 poll clients. At 200: ~34.
if [ "$NUM_POLL_CLIENTS" -eq 0 ]; then
    NUM_POLL_CLIENTS=$(( (TOTAL / 2 + 2) / 3 ))  # 1 per 3 failing sites (half of total)
    [ "$NUM_POLL_CLIENTS" -lt 3 ] && NUM_POLL_CLIENTS=3
    [ "$NUM_POLL_CLIENTS" -gt 20 ] && NUM_POLL_CLIENTS=20  # cap for VM resources (50 caused 84% VM memory at 400 sites)
fi

if [ "$TOTAL" -lt 1 ]; then
    echo "ERROR: --total must be at least 1"
    exit 1
fi

# ── Docker helper ─────────────────────────────────────────────────────────────
DOCKER_BIN="docker"
if [ -x "/Applications/OnionPress.app/Contents/Resources/bin/docker" ]; then
    DOCKER_BIN="/Applications/OnionPress.app/Contents/Resources/bin/docker"
fi

docker_cmd() {
    if [ -n "$DOCKER_HOST_SOCK" ]; then
        DOCKER_HOST="$DOCKER_HOST_SOCK" "$DOCKER_BIN" "$@"
    else
        "$DOCKER_BIN" "$@"
    fi
}

# ── Logging ───────────────────────────────────────────────────────────────────
log() {
    echo "[$(date '+%H:%M:%S')] $*"
    [ -n "${LOG_FILE:-}" ] && echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE" || true
    [ -n "$PHASE_LOG" ] && echo "[$(date '+%H:%M:%S')] $*" >> "$PHASE_LOG" || true
}

log_json() {
    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    echo "{\"ts\":\"$ts\",$1}" >> "$OUTPUT_DIR/metrics.jsonl"
}

# Phase log — written by run_worker, read by run_coordinator
PHASE_LOG=""  # set after OUTPUT_DIR is known

phase_start() {
    local phase="$1"
    local desc="$2"
    [ -z "$PHASE_LOG" ] && return
    echo "[$(date '+%H:%M:%S')] PHASE ${phase}: ${desc}" >> "$PHASE_LOG"
}

phase_result() {
    local phase="$1"
    local result="$2"
    [ -z "$PHASE_LOG" ] && return
    echo "[$(date '+%H:%M:%S')]   -> ${result}" >> "$PHASE_LOG"
}

# Format seconds as "Xm:XXs" (e.g., 135 -> "2m:15s", 45 -> "0m:45s")
fmt_duration() {
    local secs="$1"
    printf "%dm:%02ds" $((secs / 60)) $((secs % 60))
}

# Get OnionHeaven server version from /status API
get_onionheaven_version() {
    local status
    status=$(query_onionheaven_status)
    if echo "$status" | grep -q '"version"'; then
        echo "$status" | sed 's/.*"version"[[:space:]]*:[[:space:]]*"//' | sed 's/".*//' | tr -d '\n\r'
    else
        echo "pre-2.4.22"
    fi
}

# Write phase.log header with test parameters and versions
write_phase_header() {
    local oh_version
    oh_version=$(get_onionheaven_version)
    cat >> "$PHASE_LOG" << EOF
====================================================================
  OnionHeaven Stress Test
  $(date '+%Y-%m-%d %H:%M:%S')
--------------------------------------------------------------------
  Sites: ${TOTAL} total (${HEALTHY} healthy, ${FAILING} failing)
  Containers: ${NUM_CONTAINERS} x ${PER_CTR} sites/container
  OnionHeaven: ${ONIONHEAVEN_ADDR}
  OnionHeaven server version: ${oh_version:-unknown}
  Stress test version: ${STRESS_VERSION}
====================================================================

EOF
}

# Open a Terminal.app window tailing the phase log
open_phase_log_window() {
    [ -z "$PHASE_LOG" ] && return
    local abs_path
    abs_path=$(cd "$(dirname "$PHASE_LOG")" && pwd)/$(basename "$PHASE_LOG")
    osascript -e "
        tell application \"Terminal\"
            activate
            do script \"tail -f '${abs_path}'\"
        end tell
    " 2>/dev/null &
}

# ── Inject code into onionheaven + takeover containers ────────────────────────
_inject_onionheaven_code() {
    # No-op: the onionheaven containers are built from the local image overlay
    # which already has the latest code. Injecting and restarting processes
    # mid-run caused race conditions (killed workers, lost ADD_ONION work,
    # stale MAX_TAKEOVER_SERVICES).
    if ! docker_cmd exec onionheaven sqlite3 --version >/dev/null 2>&1; then
        log "  Installing sqlite3 in onionheaven..."
        docker_cmd exec onionheaven sh -c "apt-get update -qq && apt-get install -y -qq sqlite3 >/dev/null 2>&1"
    fi
    log "  sqlite3 available in onionheaven"
    log "  Using production heartbeat timing and existing container scripts"
}

# ── Wait for lazy OnionHeaven activation ──────────────────────────────────────
_wait_for_lazy_activation() {
    log "Waiting for lazy OnionHeaven activation (onionheaven container to start)..."
    for i in $(seq 1 60); do
        if docker_cmd inspect --format='{{.State.Running}}' onionheaven 2>/dev/null | grep -q true; then
            log "  onionheaven container is running (took ~${i}0s)"
            sleep 5  # give it a moment to settle
            _inject_onionheaven_code
            LAZY_ACTIVATION=false
            return 0
        fi
        sleep 10
    done
    log "WARNING: onionheaven container did not start within 10 minutes"
    return 1
}

# ── Preflight checks ─────────────────────────────────────────────────────────
preflight() {
    # Ensure output directory and log file exist early (before run_worker creates the timestamped subdir)
    mkdir -p "$OUTPUT_DIR"
    LOG_FILE="${OUTPUT_DIR}/stress-test.log"
    log "Preflight checks..."

    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker"
        if [ -n "$DOCKER_HOST_SOCK" ]; then
            echo "  Using socket: $DOCKER_HOST_SOCK (is Colima running?)"
        else
            echo "  No OnionPress Colima socket found — is Docker available?"
        fi
        exit 1
    fi

    for ctr in onionpress-tor onionpress-wordpress; do
        if ! docker_cmd inspect --format='{{.State.Running}}' "$ctr" 2>/dev/null | grep -q true; then
            echo "ERROR: Container $ctr is not running"
            exit 1
        fi
    done

    # Detect OnionHeaven host by checking if local onion address matches the target
    detect_onionheaven_addr
    LOCAL_ONION_ADDR=$(docker_cmd exec onionpress-tor \
        cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null) || true
    if [ -n "$LOCAL_ONION_ADDR" ] && [ "$LOCAL_ONION_ADDR" = "$ONIONHEAVEN_ADDR" ]; then
        IS_ONIONHEAVEN_HOST=true
        log "  Detected OnionHeaven host (content address matches)"
    else
        IS_ONIONHEAVEN_HOST=false
        log "  Not OnionHeaven host — sites will register over Tor"
        if [ -n "$LOCAL_ONION_ADDR" ]; then
            log "  Local address: $LOCAL_ONION_ADDR"
        fi
    fi

    # Get the Arti image from the running tor container (used for poll clients)
    ARTI_IMAGE=$(docker_cmd inspect --format='{{.Config.Image}}' onionpress-tor 2>/dev/null)
    if [ -z "$ARTI_IMAGE" ]; then
        echo "ERROR: Cannot determine Arti image from onionpress-tor container"
        exit 1
    fi
    log "  Arti image: $ARTI_IMAGE"

    # Stress worker image — pre-built with scripts, no apt-get at runtime
    STRESS_IMAGE="ghcr.io/brewsterkahle/onionpress-stress-worker:latest"
    if ! docker_cmd image inspect "$STRESS_IMAGE" >/dev/null 2>&1; then
        log "  Building stress worker image locally..."
        docker_cmd build -t "$STRESS_IMAGE" "${SCRIPT_DIR}/stress/" >/dev/null 2>&1 || true
    fi
    if docker_cmd image inspect "$STRESS_IMAGE" >/dev/null 2>&1; then
        log "  Stress worker image: $STRESS_IMAGE"
    else
        STRESS_IMAGE="$ARTI_IMAGE"
        log "  WARNING: Stress worker image not available, falling back to $ARTI_IMAGE"
    fi

    # OnionHeaven-host-only checks: registration API, onionheaven container
    LAZY_ACTIVATION=false
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        # API server runs in onionpress-tor (v2.4.31+), fallback to onionheaven for older images
        local status_check
        status_check=$(docker_cmd exec onionpress-tor curl -s --max-time 5 http://localhost:8083/status 2>/dev/null || echo "")
        if [ -z "$status_check" ]; then
            status_check=$(docker_cmd exec onionheaven curl -s --max-time 5 http://localhost:8083/status 2>/dev/null || echo "")
        fi
        if [ -z "$status_check" ]; then
            echo "ERROR: OnionHeaven registration API is not responding"
            echo "  Check onionpress-tor or onionheaven container logs"
            exit 1
        fi
        log "  OnionHeaven registration API is ready"

        if docker_cmd inspect --format='{{.State.Running}}' onionheaven 2>/dev/null | grep -q true; then
            log "  onionheaven is running (dedicated polling Tor)"
            _inject_onionheaven_code
        else
            log "  onionheaven container not yet running — lazy activation will bootstrap it"
            LAZY_ACTIVATION=true
        fi
    else
        log "  Skipping file injections (not OnionHeaven host)"
        log "  Using production heartbeat timing and existing container scripts"
    fi

    log "Preflight OK"
}

# ── Auto-detect onionheaven address ────────────────────────────────────────────────
DEFAULT_ONIONHEAVEN_ADDR="oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"

detect_onionheaven_addr() {
    if [ -n "$ONIONHEAVEN_ADDR" ]; then
        log "OnionHeaven address (user-specified): $ONIONHEAVEN_ADDR"
        return
    fi
    ONIONHEAVEN_ADDR="$DEFAULT_ONIONHEAVEN_ADDR"
    log "OnionHeaven address: $ONIONHEAVEN_ADDR"
}

# ── Docker network ────────────────────────────────────────────────────────────
get_onionpress_network() {
    docker_cmd inspect onionpress-tor --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null
}

# ── Site container management ─────────────────────────────────────────────────

# Start a single site container with Arti + Python HTTP server.
# Each container handles $workers_in_ctr onion service pairs (sites).
start_worker_container() {
    local idx="$1"
    local workers_in_ctr="$2"
    local ctr_name="stress-worker-${idx}"
    local network="$3"

    log "  Starting container ${ctr_name} (${workers_in_ctr} sites)..."

    # Remove leftover
    docker_cmd rm -f "$ctr_name" 2>/dev/null || true

    if [ "$TOR_IMPL" = "tor" ]; then
        # ── C Tor: generate torrc (SOCKS + control port only) ──
        # Onion services are created via ADD_ONION after bootstrap,
        # so DEL_ONION can remove them without SIGHUP.
        local torrc="${OUTPUT_DIR}/${ctr_name}-torrc"
        cat > "$torrc" << 'TORRC_HEAD'
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
TORRC_HEAD
    else
        # ── Arti: generate arti.toml ──
        local arti_conf="${OUTPUT_DIR}/${ctr_name}-arti.toml"
        cat > "$arti_conf" << 'TOML_HEAD'
[proxy]
socks_listen = "127.0.0.1:9050"

[path_rules]
# Restrict to IPv4 only — Colima/Docker has no IPv6 routes,
# so IPv6 relay connections fail with "Network unreachable"
reachable_addrs = ["0.0.0.0/0:*"]

[storage]
cache_dir = "/var/lib/arti/cache"
state_dir = "/var/lib/arti/state"

[storage.keystore]
enabled = true

[vanguards]
# Disable vanguards on stress containers — reduces circuit exhaustion cascades
# when hosting many onion services per Arti instance.
mode = "disabled"

[[logging.files]]
path = "/var/lib/arti/arti.log"
filter = "info,tor_hsservice=debug,tor_circmgr=debug,arti=debug"
TOML_HEAD

        for i in $(seq 0 $((workers_in_ctr - 1))); do
            local cp=$((BASE_PORT + i * 2))
            local hp=$((BASE_PORT + i * 2 + 1))
            cat >> "$arti_conf" << EOF

[onion_services."w${idx}_${i}_content"]
enabled = true
proxy_ports = [["80", "127.0.0.1:${cp}"]]
EOF
            if [ "$NO_HEALTHCHECK" != true ]; then
                cat >> "$arti_conf" << EOF

[onion_services."w${idx}_${i}_hc"]
enabled = true
proxy_ports = [["80", "127.0.0.1:${hp}"]]
EOF
            fi
        done
    fi

    # Start container with sleep (we'll exec the real startup after copying files)
    docker_cmd run -d \
        --name "$ctr_name" \
        --network "$network" \
        --ulimit nofile=10000:10000 \
        --entrypoint sh \
        "$STRESS_IMAGE" \
        -c "sleep infinity" >/dev/null 2>&1

    # Always copy latest scripts into container (image may be stale)
    docker_cmd cp "${SCRIPT_DIR}/stress/worker-server.py" "${ctr_name}:/worker-server.py"
    docker_cmd cp "${SCRIPT_DIR}/stress/worker-bootstrap.py" "${ctr_name}:/worker-bootstrap.py"
    docker_cmd cp "${SCRIPT_DIR}/../src/onionpress/onion_auth.py" "${ctr_name}:/onion_auth.py"
    docker_cmd cp "${SCRIPT_DIR}/stress/tor-watchdog.sh" "${ctr_name}:/tor-watchdog.sh"
    if [ "$TOR_IMPL" = "tor" ]; then
        docker_cmd cp "$torrc" "${ctr_name}:/etc/tor/torrc"
    else
        docker_cmd cp "$arti_conf" "${ctr_name}:/etc/arti/arti.toml"
    fi

    # Generate startup script
    local startup="${OUTPUT_DIR}/${ctr_name}-start.sh"
    if [ "$TOR_IMPL" = "tor" ]; then
        cat > "$startup" << STARTEOF
#!/bin/sh
# No set -e — individual failures should not kill the container

# Install deps only if not using pre-built stress image
if ! python3 --version >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-minimal curl netcat-openbsd xxd >/dev/null 2>&1
fi

# Prepare C Tor data dir (no HiddenServiceDir — services created via ADD_ONION)
mkdir -p /var/lib/tor /worker-data
chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
chmod 700 /var/lib/tor

# Start Python HTTP server
python3 /worker-server.py ${BASE_PORT} ${workers_in_ctr} &

# Start C Tor with bootstrap watchdog (retries on bad guard selection)
MAX_TOR_RETRIES=3
TOR_ATTEMPT=0
while [ "\$TOR_ATTEMPT" -lt "\$MAX_TOR_RETRIES" ]; do
    TOR_ATTEMPT=\$((TOR_ATTEMPT + 1))
    rm -f /var/lib/tor/state /var/lib/tor/lock
    su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
    TOR_PID=\$!
    sh /tor-watchdog.sh 120 &
    WATCHDOG_PID=\$!
    wait \$WATCHDOG_PID
    WATCHDOG_EXIT=\$?
    if [ "\$WATCHDOG_EXIT" -eq 0 ]; then
        break
    fi
    echo "tor-watchdog: retry \$TOR_ATTEMPT/\$MAX_TOR_RETRIES" >&2
    wait \$TOR_PID 2>/dev/null
done

# Wait for bootstrap, create services via ADD_ONION, then register with OnionHeaven
STRESS_VERSION="${STRESS_VERSION}" NO_HEALTHCHECK="${NO_HEALTHCHECK}" TOR_IMPL=tor python3 -u /worker-bootstrap.py "${ONIONHEAVEN_ADDR}" ${idx} ${workers_in_ctr} ${BASE_PORT} > /bootstrap.log 2>&1 &

wait \$TOR_PID
STARTEOF
    else
        cat > "$startup" << STARTEOF
#!/bin/sh
# No set -e — individual failures should not kill the container

# Install Python + curl (Arti image is Debian trixie-slim)
apt-get update -qq && apt-get install -y -qq python3-minimal curl >/dev/null 2>&1

# Fix ownership on config file (docker cp sets host UID)
chown root:root /etc/arti/arti.toml
chmod 644 /etc/arti/arti.toml

# Prepare Arti state dirs
mkdir -p /var/lib/arti/cache /var/lib/arti/state /worker-data
chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Start Python HTTP server (single process handles all ${workers_in_ctr} sites)
python3 /worker-server.py ${BASE_PORT} ${workers_in_ctr} &

# Start Arti
su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" &
ARTI_PID=\$!

# Wait for Arti keys, then self-register with OnionHeaven over Tor
STRESS_VERSION="${STRESS_VERSION}" NO_HEALTHCHECK="${NO_HEALTHCHECK}" python3 -u /worker-bootstrap.py "${ONIONHEAVEN_ADDR}" ${idx} ${workers_in_ctr} ${BASE_PORT} > /bootstrap.log 2>&1 &

wait \$ARTI_PID
STARTEOF
    fi
    chmod +x "$startup"
    docker_cmd cp "$startup" "${ctr_name}:/start.sh"

    # Launch the startup script — use foreground exec backgrounded from bash
    # (docker exec -d is unreliable under qemu: processes die silently as zombies)
    docker_cmd exec "$ctr_name" sh /start.sh </dev/null >/dev/null 2>&1 &
}

# Start all site containers, optionally in batches.
start_all_workers() {
    local network
    network=$(get_onionpress_network)
    if [ -z "$network" ]; then
        log "ERROR: Could not determine OnionPress Docker network"
        exit 1
    fi
    log "Docker network: $network"

    local remaining=$TOTAL
    local idx=0

    while [ "$remaining" -gt 0 ]; do
        local workers_in_ctr=$PER_CTR
        if [ "$workers_in_ctr" -gt "$remaining" ]; then
            workers_in_ctr=$remaining
        fi

        start_worker_container "$idx" "$workers_in_ctr" "$network"

        remaining=$((remaining - workers_in_ctr))
        idx=$((idx + 1))

        # Batch staggering: wait between batches of containers
        if [ "$BATCH_SIZE" -gt 0 ] && [ $((idx % BATCH_SIZE)) -eq 0 ] && [ "$remaining" -gt 0 ]; then
            log "  Batch of ${BATCH_SIZE} containers started, waiting 30s before next batch..."
            sleep 30
        fi
    done

    log "Started ${NUM_CONTAINERS} stress containers"
}

# ── Polling client farm ───────────────────────────────────────────────────────
# Dedicated Tor SOCKS containers for polling reachability. These are separate
# from the stress workers (which publish services) and from onionpress-tor
# (which may have stale circuits). Each polling client has fresh circuits
# optimized for client-side descriptor lookups.

start_poll_clients() {
    local network
    network=$(get_onionpress_network)
    log "Starting ${NUM_POLL_CLIENTS} polling clients..."
    for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local name="stress-poll-client-${i}"
        docker_cmd rm -f "$name" 2>/dev/null || true
        docker_cmd run -d \
            --name "$name" \
            --network "$network" \
            --ulimit nofile=10000:10000 \
            --entrypoint sh \
            "$STRESS_IMAGE" \
            -c "
                mkdir -p /var/lib/tor
                chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
                chmod 700 /var/lib/tor
                cat > /etc/tor/torrc << EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
EOF
                su -s /bin/sh debian-tor -c 'tor -f /etc/tor/torrc'
            " >/dev/null 2>&1
    done

    # Wait for all polling clients to bootstrap
    log "Waiting for ${NUM_POLL_CLIENTS} polling clients to bootstrap..."
    local all_ready=false
    local prev_ready=0
    for attempt in $(seq 1 30); do
        local ready=0
        for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
            if docker_cmd exec "stress-poll-client-${i}" \
                curl -s -o /dev/null --socks5-hostname 127.0.0.1:9050 --max-time 5 \
                "http://example.com/" 2>/dev/null; then
                ready=$((ready + 1))
            fi
        done
        # Print dots for newly bootstrapped clients
        if [ -n "$PHASE_LOG" ] && [ "$ready" -gt "$prev_ready" ]; then
            printf '%0.s.' $(seq 1 $((ready - prev_ready))) >> "$PHASE_LOG"
            prev_ready=$ready
        fi
        if [ "$ready" -ge "$NUM_POLL_CLIENTS" ]; then
            all_ready=true
            [ -n "$PHASE_LOG" ] && printf ' %s/%s\n' "$NUM_POLL_CLIENTS" "$NUM_POLL_CLIENTS" >> "$PHASE_LOG"
            log "  All ${NUM_POLL_CLIENTS} polling clients ready (${attempt}0s)"
            break
        fi
        sleep 10
    done
    if [ "$all_ready" = false ]; then
        [ -n "$PHASE_LOG" ] && printf ' %s/%s\n' "$prev_ready" "$NUM_POLL_CLIENTS" >> "$PHASE_LOG"
        log "WARNING: Not all polling clients bootstrapped (${prev_ready}/${NUM_POLL_CLIENTS}), using what's available"
    fi

    # Copy latest verify-worker.py into poll clients (image may be stale)
    for ci in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        docker_cmd cp "${SCRIPT_DIR}/stress/verify-worker.py" "stress-poll-client-${ci}:/verify-worker.py" 2>/dev/null || true
    done
    log "  Poll clients ready (verify-worker.py updated from repo)"
}

stop_poll_clients() {
    for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        docker_cmd rm -f "stress-poll-client-${i}" 2>/dev/null &
    done
    wait
}

# Wait for all sites to bootstrap (register with OnionHeaven over Tor).
WAIT_RESULT=""  # human-readable result from last wait_for_* call

wait_for_bootstrap() {
    local timeout_secs="${1:-900}"
    log "Waiting for all sites to bootstrap and register (timeout: ${timeout_secs}s)..."

    local start_ts
    start_ts=$(date +%s)
    local deadline=$((start_ts + timeout_secs))
    local last_status=0
    local registered_count=0
    local prev_registered=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local all_ready=true
        local ready_count=0
        registered_count=0

        for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
            local ctr_name="stress-worker-${idx}"
            if docker_cmd exec "$ctr_name" test -f /worker-info.json 2>/dev/null; then
                # Count registered and total sites in this container
                local reg total_in_ctr
                reg=$(docker_cmd exec "$ctr_name" python3 -c "
import json
with open('/worker-info.json') as f:
    w = json.load(f)
print(sum(1 for x in w if x.get('registered')), len(w))
" 2>/dev/null || echo "0 0")
                local reg_count=$(echo "$reg" | awk '{print $1}')
                total_in_ctr=$(echo "$reg" | awk '{print $2}')
                registered_count=$((registered_count + reg_count))
                # Container is "done" when all expected sites have been processed
                local expected_in_ctr=$PER_CTR
                [ $((idx + 1)) -eq "$NUM_CONTAINERS" ] && expected_in_ctr=$(( TOTAL - idx * PER_CTR ))
                if [ "$total_in_ctr" -ge "$expected_in_ctr" ] 2>/dev/null; then
                    ready_count=$((ready_count + 1))
                else
                    all_ready=false
                fi
            else
                all_ready=false
            fi
        done

        # Write progress dots to phase log (one dot per newly registered site)
        if [ -n "$PHASE_LOG" ] && [ "$registered_count" -gt "$prev_registered" ]; then
            local new_dots=$((registered_count - prev_registered))
            printf '%0.s.' $(seq 1 "$new_dots") >> "$PHASE_LOG"
            prev_registered=$registered_count
        fi

        local now
        now=$(date +%s)
        if [ $((now - last_status)) -ge 10 ]; then
            log "  Bootstrap: ${ready_count}/${NUM_CONTAINERS} stress containers done, ${registered_count}/${TOTAL} sites registered"
            last_status=$now
        fi

        if [ "$all_ready" = true ]; then
            local elapsed=$(( $(date +%s) - start_ts ))
            [ -n "$PHASE_LOG" ] && echo " ${registered_count}/${TOTAL}" >> "$PHASE_LOG"
            WAIT_RESULT="${registered_count}/${TOTAL} registered in $(fmt_duration $elapsed)"
            log "All stress containers bootstrapped: ${registered_count} sites registered"
            return 0
        fi

        sleep 5
    done

    local elapsed=$(( $(date +%s) - start_ts ))
    [ -n "$PHASE_LOG" ] && echo " ${registered_count}/${TOTAL} (timed out)" >> "$PHASE_LOG"
    WAIT_RESULT="${registered_count}/${TOTAL} registered, timed out after $(fmt_duration $elapsed)"
    log "WARNING: Bootstrap timed out — some sites not ready"
    return 1
}

# Extract all site info from containers into local files.
extract_all_worker_info() {
    log "Extracting site info from containers..."
    local total_registered=0

    for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
        local ctr_name="stress-worker-${idx}"
        docker_cmd exec "$ctr_name" cat /worker-info.json > "${OUTPUT_DIR}/worker-${idx}-info.json" 2>/dev/null || true

        # Count registered
        local reg
        reg=$(python3 -c "
import json, sys
try:
    with open('${OUTPUT_DIR}/worker-${idx}-info.json') as f:
        w = json.load(f)
    print(sum(1 for x in w if x.get('registered')))
except: print(0)
" 2>/dev/null || echo 0)
        total_registered=$((total_registered + reg))
    done

    log "Total registered sites: ${total_registered}"
}

# ── Metrics collection ────────────────────────────────────────────────────────

ONIONHEAVEN_DB_PATH="/var/lib/onionheaven/registry.db"

# Query OnionHeaven /status API — locally via docker exec, or remotely via Tor
# Returns JSON: {"total":N,"healthy":N,"failing":N,"taken_over":N}
# Caches result for 5s to avoid hammering Tor on every metric call.
_ONIONHEAVEN_STATUS_CACHE=""
_ONIONHEAVEN_STATUS_TS=0

query_onionheaven_status() {
    local now
    now=$(date +%s)
    if [ $((now - _ONIONHEAVEN_STATUS_TS)) -lt 5 ] && [ -n "$_ONIONHEAVEN_STATUS_CACHE" ]; then
        echo "$_ONIONHEAVEN_STATUS_CACHE"
        return
    fi

    local result=""
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        result=$(docker_cmd exec onionpress-tor curl -s --max-time 5 http://localhost:8083/status 2>/dev/null) || \
        result=$(docker_cmd exec onionheaven curl -s --max-time 5 http://localhost:8083/status 2>/dev/null) || result=""
    else
        result=$(docker_cmd exec onionpress-tor \
            curl -s --socks5-hostname "status:x@127.0.0.1:9050" --max-time 30 \
            "http://${ONIONHEAVEN_ADDR}:8083/status" 2>/dev/null) || result=""
    fi

    if echo "$result" | grep -q '"total"'; then
        _ONIONHEAVEN_STATUS_CACHE="$result"
        _ONIONHEAVEN_STATUS_TS=$now
        echo "$result"
    else
        echo '{"total":0,"healthy":0,"failing":0,"taken_over":0}'
    fi
}

# Extract a field from OnionHeaven status JSON (lightweight — no python needed)
_onionheaven_field() {
    local field="$1"
    query_onionheaven_status | sed 's/.*"'"$field"'"[[:space:]]*:[[:space:]]*//' | sed 's/[^0-9].*//' | tr -d ' \n\r'
}

get_registry_count() {
    _onionheaven_field "total"
}

get_healthy_count() {
    _onionheaven_field "online"
}

get_heartbeat_healthy_count() {
    _onionheaven_field "heartbeat_healthy"
}

get_wordpress_unhealthy_count() {
    _onionheaven_field "wordpress_unhealthy"
}

get_stress_fail_count() {
    # No direct "failing" field — derive from total minus online minus taken_over
    local total online taken_over
    total=$(_onionheaven_field "total")
    online=$(_onionheaven_field "online")
    taken_over=$(_onionheaven_field "taken_over")
    echo $(( ${total:-0} - ${online:-0} - ${taken_over:-0} ))
}

get_takeover_count() {
    _onionheaven_field "taken_over"
}

get_takeover_container_count() {
    _onionheaven_field "takeover_containers"
}

# Count sites registered from local containers (works on any machine)
get_local_registered_count() {
    local total=0
    for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
        local ctr_name="stress-worker-${idx}"
        local reg
        reg=$(docker_cmd exec "$ctr_name" python3 -c "
import json
with open('/worker-info.json') as f:
    w = json.load(f)
print(sum(1 for x in w if x.get('registered')))
" 2>/dev/null || echo 0)
        total=$((total + reg))
    done
    echo "$total"
}

get_container_mem_mb() {
    local ctr="$1"
    local mem_bytes
    mem_bytes=$(docker_cmd stats --no-stream --format '{{.MemUsage}}' "$ctr" 2>/dev/null | awk '{print $1}' || echo "0")
    if echo "$mem_bytes" | grep -qi gib; then
        echo "$mem_bytes" | sed 's/[Gg][Ii][Bb]//' | awk '{printf "%.0f", $1 * 1024}'
    elif echo "$mem_bytes" | grep -qi mib; then
        echo "$mem_bytes" | sed 's/[Mm][Ii][Bb]//' | awk '{printf "%.0f", $1}'
    elif echo "$mem_bytes" | grep -qi kib; then
        echo "$mem_bytes" | sed 's/[Kk][Ii][Bb]//' | awk '{printf "%.0f", $1 / 1024}'
    else
        echo "0"
    fi
}

get_last_pass_duration() {
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        docker_cmd exec onionheaven sh -c 'grep "heartbeat pass complete" /var/lib/onionheaven/heartbeat.log 2>/dev/null | tail -1 | sed "s/.*in //;s/s$//"' | tr -d ' \n\r' || echo "-"
    else
        echo "-"
    fi
}

get_system_mem_pct() {
    local avail total used
    avail=$(docker_cmd exec onionpress-tor sh -c "awk '/MemAvailable/{print \$2}' /proc/meminfo 2>/dev/null" | tr -d ' \n\r')
    total=$(docker_cmd exec onionpress-tor sh -c "awk '/MemTotal/{print \$2}' /proc/meminfo 2>/dev/null" | tr -d ' \n\r')
    if [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null; then
        used=$((total - avail))
        echo "$((used * 100 / total))"
    else
        echo "0"
    fi
}

print_dashboard() {
    local reg_count tor_mem wp_mem fail_count takeover_count healthy_count mem_pct pass_dur takeover_ctrs stress_ctrs hb_ok wp_bad
    reg_count=$(get_registry_count)
    tor_mem=$(get_container_mem_mb onionpress-tor)
    wp_mem=$(get_container_mem_mb onionpress-wordpress)
    fail_count=$(get_stress_fail_count)
    takeover_count=$(get_takeover_count)
    healthy_count=$(get_healthy_count)
    hb_ok=$(get_heartbeat_healthy_count)
    wp_bad=$(get_wordpress_unhealthy_count)
    mem_pct=$(get_system_mem_pct)
    pass_dur=$(get_last_pass_duration)
    takeover_ctrs=$(get_takeover_container_count)
    stress_ctrs=$(docker_cmd ps --filter "name=stress-worker-" --format "{{.Names}}" 2>/dev/null | wc -l | tr -d ' ')
    poll_ctrs=$(docker_cmd ps --filter "name=stress-poll-client-" --format "{{.Names}}" 2>/dev/null | wc -l | tr -d ' ')

    log "Registry: ${reg_count} entries | Tor mem: ${tor_mem}MB | WP mem: ${wp_mem}MB"
    echo "           Online: ${healthy_count} | Taken over: ${takeover_count} | Heartbeat: ${hb_ok:-0} ok / WP unhealthy: ${wp_bad:-0} | VM mem: ${mem_pct}%"
    echo "           Farm: ${takeover_ctrs:-0} takeover + ${stress_ctrs:-0} stress + ${poll_ctrs:-0} poll containers | Last pass: ${pass_dur:-?}s"

    log_json "\"registry_count\":${reg_count:-0},\"tor_mem_mb\":${tor_mem:-0},\"wp_mem_mb\":${wp_mem:-0},\"online\":${healthy_count:-0},\"failing\":${fail_count:-0},\"takeovers\":${takeover_count:-0},\"heartbeat_healthy\":${hb_ok:-0},\"wordpress_unhealthy\":${wp_bad:-0},\"vm_mem_pct\":${mem_pct:-0},\"pass_duration\":\"${pass_dur}\",\"takeover_containers\":${takeover_ctrs:-0},\"stress_containers\":${stress_ctrs:-0}"
}

# ── Helper: get site addresses from local info files ────────────────────────

# Get content addresses for a range of sites (from local site-info files).
# Usage: get_worker_content_addrs <start> <count>
get_worker_content_addrs() {
    local start="$1"
    local count="$2"
    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"
        python3 -c "
import json, sys
try:
    with open('${info_file}') as f:
        workers = json.load(f)
    w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
    if w and w.get('content_address'):
        print(w['content_address'])
except: pass
" 2>/dev/null
    done
}

# Get healthcheck addresses for a range of sites.
get_worker_hc_addrs() {
    local start="$1"
    local count="$2"
    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"
        python3 -c "
import json, sys
try:
    with open('${info_file}') as f:
        workers = json.load(f)
    w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
    if w and w.get('healthcheck_address'):
        print(w['healthcheck_address'])
except: pass
" 2>/dev/null
    done
}

# ── Parallel reachability check ──────────────────────────────────────────────
# Runs all docker exec curls concurrently (background subshells + temp files).
# Sets: PCHECK_MATCHED, PCHECK_TOTAL, PCHECK_200, PCHECK_302
parallel_check_addrs() {
    local addrs="$1"
    local accept_codes="$2"
    local max_time="${3:-10}"
    local max_parallel="${4:-20}"

    PCHECK_MATCHED=0
    PCHECK_TOTAL=0
    PCHECK_302=0
    PCHECK_200=0
    PCHECK_000=0

    # Use dedicated polling clients for descriptor lookups.
    # Falls back to onionpress-tor if no polling clients running.
    local check_ctrs=""
    local num_check_ctrs=0
    for ci in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local cname="stress-poll-client-${ci}"
        if docker_cmd inspect "$cname" >/dev/null 2>&1; then
            check_ctrs="${check_ctrs} ${cname}"
            num_check_ctrs=$((num_check_ctrs + 1))
        fi
    done
    if [ "$num_check_ctrs" -eq 0 ]; then
        check_ctrs="onionpress-tor"
        num_check_ctrs=1
    fi
    local ctr_arr=($check_ctrs)

    local tmpdir
    tmpdir=$(mktemp -d)
    local pids=""
    local idx=0

    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        idx=$((idx + 1))

        # Round-robin across available containers
        local ctr_name="${ctr_arr[$(( (idx - 1) % num_check_ctrs ))]}"

        (
            code=$(docker_cmd exec "$ctr_name" \
                curl -s --http1.0 --socks5-hostname "reach${idx}:x@127.0.0.1:9050" --max-time "$max_time" \
                -o /dev/null -w "%{http_code}" \
                "http://${addr}/" 2>/dev/null) || code="000"
            echo "$code" > "${tmpdir}/${idx}"
        ) &
        pids="$pids $!"

        # Batch concurrency cap for large site counts
        if [ $((idx % max_parallel)) -eq 0 ]; then
            for pid in $pids; do
                wait "$pid" 2>/dev/null || true
            done
            pids=""
        fi
    done <<< "$addrs"

    # Wait for remaining background jobs
    for pid in $pids; do
        wait "$pid" 2>/dev/null || true
    done

    PCHECK_TOTAL=$idx

    # Collect results
    for f in "${tmpdir}"/*; do
        [ -f "$f" ] || continue
        local code
        code=$(cat "$f")
        case "$code" in
            200) PCHECK_200=$((PCHECK_200 + 1)) ;;
            302) PCHECK_302=$((PCHECK_302 + 1)) ;;
            000) PCHECK_000=$((PCHECK_000 + 1)) ;;
        esac
        for ac in $accept_codes; do
            if [ "$code" = "$ac" ]; then
                PCHECK_MATCHED=$((PCHECK_MATCHED + 1))
                break
            fi
        done
    done

    rm -rf "$tmpdir"
}

# ── Phase: Wait for healthy (distributed) ────────────────────────────────────
# Each stress container checks its own sites via its own SOCKS proxy.
# This avoids the tor-client bottleneck where one Tor process tries to
# reach all sites simultaneously.

wait_for_healthy() {
    local target="$1"
    local phase_name="$2"
    local timeout_secs="${3:-600}"

    log "${phase_name}: Waiting for ${target} sites to be reachable via Tor (timeout: ${timeout_secs}s)..."

    local start_ts
    start_ts=$(date +%s)
    local deadline=$((start_ts + timeout_secs))
    local last_dashboard=0
    local reachable=0
    local prev_reachable=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        # Check reachability from each stress container's own SOCKS proxy
        local tmpdir
        tmpdir=$(mktemp -d)
        local pids=""

        for ctr_idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
            local ctr_name="stress-worker-${ctr_idx}"
            local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"
            [ -f "$info_file" ] || continue

            # Each container checks its own sites in parallel (backgrounded)
            (
                local count=0
                local addrs
                if [ "$NO_HEALTHCHECK" = true ]; then
                    addrs=$(python3 -c "
import json
with open('${info_file}') as f:
    workers = json.load(f)
for w in workers:
    if w.get('content_address'):
        print(w['content_address'])
" 2>/dev/null)
                else
                    addrs=$(python3 -c "
import json
with open('${info_file}') as f:
    workers = json.load(f)
for w in workers:
    if w.get('healthcheck_address'):
        print(w['healthcheck_address'])
" 2>/dev/null)
                fi

                while IFS= read -r addr; do
                    [ -z "$addr" ] && continue
                    code=$(docker_cmd exec "$ctr_name" \
                        curl -s --socks5-hostname 127.0.0.1:9050 --max-time 10 \
                        -o /dev/null -w "%{http_code}" \
                        "http://${addr}/" 2>/dev/null) || code="000"
                    [ "$code" = "200" ] && count=$((count + 1))
                done <<< "$addrs"
                echo "$count" > "${tmpdir}/ctr_${ctr_idx}"
            ) &
            pids="$pids $!"
        done

        for pid in $pids; do
            wait "$pid" 2>/dev/null || true
        done

        reachable=0
        local total_checked=0
        for f in "${tmpdir}"/ctr_*; do
            [ -f "$f" ] || continue
            local c
            c=$(cat "$f")
            reachable=$((reachable + c))
            total_checked=$((total_checked + 1))
        done
        rm -rf "$tmpdir"

        PCHECK_MATCHED=$reachable
        PCHECK_TOTAL=$target

        # Write progress dots to phase log (one dot per newly healthy site)
        if [ -n "$PHASE_LOG" ] && [ "$reachable" -gt "$prev_reachable" ]; then
            local new_dots=$((reachable - prev_reachable))
            printf '%0.s.' $(seq 1 "$new_dots") >> "$PHASE_LOG"
            prev_reachable=$reachable
        fi

        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 10 ]; then
            print_dashboard
            log "  (reachable: ${reachable}/${target} sites)"
            last_dashboard=$now
        fi

        if [ "$reachable" -ge "$target" ] 2>/dev/null; then
            local elapsed=$(( $(date +%s) - start_ts ))
            [ -n "$PHASE_LOG" ] && echo " ${reachable}/${target}" >> "$PHASE_LOG"
            WAIT_RESULT="${reachable}/${target} healthy in $(fmt_duration $elapsed)"
            log "${phase_name}: ${reachable}/${target} sites reachable via Tor"
            print_dashboard
            return 0
        fi

        sleep 5
    done

    local elapsed=$(( $(date +%s) - start_ts ))
    [ -n "$PHASE_LOG" ] && echo " ${reachable:-0}/${target} (timed out)" >> "$PHASE_LOG"
    WAIT_RESULT="${reachable:-0}/${target} healthy, timed out after $(fmt_duration $elapsed)"
    log "${phase_name}: Timed out — only ${reachable:-0} reachable (wanted ${target})"
    print_dashboard
    return 1
}

# ── Phase: Trigger failures ──────────────────────────────────────────────────
# Disable HTTP responders via the Python control API in each site container.

disable_workers() {
    local fail_start="$1"
    local fail_count="$2"

    log "Disabling responders for sites ${fail_start}..$(( fail_start + fail_count - 1 ))..."

    local affected_containers=""
    local del_failures=0

    for i in $(seq "$fail_start" $((fail_start + fail_count - 1))); do
        # Figure out which container and local index
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local ctr_name="stress-worker-${ctr_idx}"
        local cp=$((BASE_PORT + local_idx * 2))
        local hp=$((BASE_PORT + local_idx * 2 + 1))

        # Disable both content and healthcheck ports
        local disable_resp
        disable_resp=$(docker_cmd exec "$ctr_name" \
            curl -s -X POST http://127.0.0.1:9000/disable \
            -H "Content-Type: application/json" \
            -d "{\"ports\": [${cp}, ${hp}]}" 2>&1) || {
            log "WARNING: Failed to disable ports ${cp},${hp} on ${ctr_name}: ${disable_resp}"
        }

        # Also shut down Tor onion services for this site so OnionHeaven's
        # takeover worker becomes the sole publisher for these .onion addresses.
        local content_nick="w${ctr_idx}_${local_idx}_content"
        local hc_nick="w${ctr_idx}_${local_idx}_hc"
        if [ "$TOR_IMPL" = "tor" ]; then
            # C Tor: DEL_ONION via worker-server control API
            local del_resp
            del_resp=$(docker_cmd exec "$ctr_name" \
                curl -s -X POST http://127.0.0.1:9000/del_onion \
                -H "Content-Type: application/json" \
                -d "{\"workers\": [${local_idx}]}" 2>&1)
            if [ $? -ne 0 ] || echo "$del_resp" | grep -qi "fail\|error"; then
                log "WARNING: DEL_ONION failed for site ${i} (worker ${local_idx} on ${ctr_name}): ${del_resp}"
                # Retry once after 2s
                sleep 2
                del_resp=$(docker_cmd exec "$ctr_name" \
                    curl -s -X POST http://127.0.0.1:9000/del_onion \
                    -H "Content-Type: application/json" \
                    -d "{\"workers\": [${local_idx}]}" 2>&1)
                if [ $? -ne 0 ] || echo "$del_resp" | grep -qi "fail\|error"; then
                    log "ERROR: DEL_ONION retry failed for site ${i}: ${del_resp}"
                    del_failures=$((del_failures + 1))
                else
                    log "  DEL_ONION retry succeeded for site ${i}: ${del_resp}"
                fi
            fi
        else
            # Arti: disable in config (no control port equivalent)
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${content_nick}\"\]/,/^enabled = /{s/^enabled = true/enabled = false/}" \
                /etc/arti/arti.toml 2>/dev/null || true
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${hc_nick}\"\]/,/^enabled = /{s/^enabled = true/enabled = false/}" \
                /etc/arti/arti.toml 2>/dev/null || true

            # Track which containers need Arti SIGHUP
            if ! echo "$affected_containers" | grep -q "$ctr_name"; then
                affected_containers="${affected_containers} ${ctr_name}"
            fi
        fi
    done

    # Arti only: SIGHUP to reload config (C Tor uses DEL_ONION above, no SIGHUP needed)
    if [ "$TOR_IMPL" != "tor" ]; then
        for ctr_name in $affected_containers; do
            docker_cmd exec "$ctr_name" sh -c "
                arti_pid=\$(pidof arti 2>/dev/null)
                if [ -n \"\$arti_pid\" ]; then
                    kill -HUP \$arti_pid 2>/dev/null
                else
                    su -s /bin/sh arti -c 'arti proxy -c /etc/arti/arti.toml' &
                fi
            " 2>/dev/null || true
        done
    fi

    if [ "$del_failures" -gt 0 ]; then
        log "WARNING: ${del_failures}/${fail_count} DEL_ONION calls failed — competing descriptors may slow takeover"
    fi
    log "Disabled ${fail_count} sites (HTTP responders + ${TOR_LABEL} $([ "$TOR_IMPL" = "tor" ] && echo "DEL_ONION" || echo "SIGHUP"))"
}

enable_workers() {
    local start="$1"
    local count="$2"

    log "Re-enabling responders and re-registering sites ${start}..$(( start + count - 1 ))..."

    local affected_containers=""
    local add_failures=0

    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local ctr_name="stress-worker-${ctr_idx}"
        local cp=$((BASE_PORT + local_idx * 2))
        local hp=$((BASE_PORT + local_idx * 2 + 1))

        # Re-enable Tor onion services (so descriptor publishing resumes)
        local content_nick="w${ctr_idx}_${local_idx}_content"
        local hc_nick="w${ctr_idx}_${local_idx}_hc"
        if [ "$TOR_IMPL" = "tor" ]; then
            # C Tor: ADD_ONION via worker-server control API
            local add_resp
            add_resp=$(docker_cmd exec "$ctr_name" \
                curl -s -X POST http://127.0.0.1:9000/add_onion \
                -H "Content-Type: application/json" \
                -d "{\"workers\": [${local_idx}]}" 2>&1)
            if [ $? -ne 0 ] || echo "$add_resp" | grep -qi "fail\|error"; then
                log "WARNING: ADD_ONION failed for site ${i} (worker ${local_idx} on ${ctr_name}): ${add_resp}"
                sleep 2
                add_resp=$(docker_cmd exec "$ctr_name" \
                    curl -s -X POST http://127.0.0.1:9000/add_onion \
                    -H "Content-Type: application/json" \
                    -d "{\"workers\": [${local_idx}]}" 2>&1)
                if [ $? -ne 0 ] || echo "$add_resp" | grep -qi "fail\|error"; then
                    log "ERROR: ADD_ONION retry failed for site ${i}: ${add_resp}"
                    add_failures=$((add_failures + 1))
                else
                    log "  ADD_ONION retry succeeded for site ${i}: ${add_resp}"
                fi
            fi
        else
            # Arti: re-enable in config
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${content_nick}\"\]/,/^enabled = /{s/^enabled = false/enabled = true/}" \
                /etc/arti/arti.toml 2>/dev/null || true
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${hc_nick}\"\]/,/^enabled = /{s/^enabled = false/enabled = true/}" \
                /etc/arti/arti.toml 2>/dev/null || true

            if ! echo "$affected_containers" | grep -q "$ctr_name"; then
                affected_containers="${affected_containers} ${ctr_name}"
            fi
        fi

        # Re-enable HTTP responders
        local enable_resp
        enable_resp=$(docker_cmd exec "$ctr_name" \
            curl -s -X POST http://127.0.0.1:9000/enable \
            -H "Content-Type: application/json" \
            -d "{\"ports\": [${cp}, ${hp}]}" 2>&1) || {
            log "WARNING: Failed to re-enable ports ${cp},${hp} on ${ctr_name}: ${enable_resp}"
        }

        # Re-register with OnionHeaven over Tor (like a real OnionPress restart).
        # This triggers immediate release of the taken-over address.
        local pem_path_expr
        if [ "$TOR_IMPL" = "tor" ]; then
            pem_path_expr="'/tmp/w${ctr_idx}_${local_idx}_content.pem'"
        else
            pem_path_expr="f'/var/lib/arti/state/keystore/hss/w${ctr_idx}_${local_idx}_content/ks_hs_id.ed25519_expanded_private'"
        fi
        docker_cmd exec "$ctr_name" \
            python3 -c "
import json, subprocess, sys, time, os
with open('/worker-info.json') as f:
    workers = json.load(f)
w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
if not w or not w.get('content_address'):
    sys.exit(0)
time.sleep(${local_idx} * 1)
import base64
from onion_auth import sign_payload, make_timestamp
pem_path = ${pem_path_expr}
if os.environ.get('TOR_IMPL') == 'tor' and not os.path.exists(pem_path):
    # Convert C Tor key to Arti PEM for registration
    secret = '/var/lib/tor/hidden_service/w${ctr_idx}_${local_idx}_content/hs_ed25519_secret_key'
    subprocess.run(['python3', '/key-convert.py', 'ctor-to-arti', secret, pem_path], capture_output=True, timeout=10)
try:
    with open(pem_path, 'rb') as f:
        pem_b64 = base64.b64encode(f.read()).decode()
except:
    pem_b64 = ''
privkey = base64.b64decode(w.get('privkey_b64', ''))
pubkey = base64.b64decode(w.get('pubkey_b64', ''))
timestamp = make_timestamp()
signature = sign_payload(privkey, pubkey, 'register', w['content_address'], w['healthcheck_address'], timestamp)
payload = json.dumps({
    'content_address': w['content_address'],
    'healthcheck_address': w['healthcheck_address'],
    'arti_key_pem': pem_b64,
    'version': '${STRESS_VERSION}',
    'timestamp': timestamp,
    'signature': signature,
})
subprocess.run([
    'curl', '-s', '-X', 'POST',
    '--socks5-hostname', 'w${ctr_idx}_${local_idx}:x@127.0.0.1:9050',
    '-H', 'Content-Type: application/json',
    '-d', payload,
    '--max-time', '60',
    'http://${ONIONHEAVEN_ADDR}:8083/online',
], capture_output=True, timeout=75)
print(f'Re-registered {w[\"content_address\"]}')
" 2>/dev/null &
    done

    # Arti only: SIGHUP to reload config (C Tor uses ADD_ONION above, no SIGHUP needed)
    if [ "$TOR_IMPL" != "tor" ]; then
        for ctr_name in $affected_containers; do
            docker_cmd exec "$ctr_name" sh -c "
                arti_pid=\$(pidof arti 2>/dev/null)
                if [ -n \"\$arti_pid\" ]; then
                    kill -HUP \$arti_pid 2>/dev/null
                else
                    su -s /bin/sh arti -c 'arti proxy -c /etc/arti/arti.toml' &
                fi
            " 2>/dev/null || true
        done
    fi

    if [ "$add_failures" -gt 0 ]; then
        log "WARNING: ${add_failures}/${count} ADD_ONION calls failed — some sites may not recover"
    fi
    log "Re-enabled ${count} sites, ${TOR_LABEL} $([ "$TOR_IMPL" = "tor" ] && echo "ADD_ONION" || echo "SIGHUP") + re-registrations over Tor (1s apart)"
}

# Re-enable sites WITHOUT re-registering or sending /online.
# Tests pure heartbeat-based recovery: OnionHeaven must discover the service
# is healthy again by polling the healthcheck address.
enable_workers_silent() {
    local start="$1"
    local count="$2"

    log "Re-enabling responders for sites ${start}..$(( start + count - 1 )) (no /online)..."

    local affected_containers=""
    local add_failures=0

    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local ctr_name="stress-worker-${ctr_idx}"
        local cp=$((BASE_PORT + local_idx * 2))
        local hp=$((BASE_PORT + local_idx * 2 + 1))

        # Re-enable Tor onion services
        local content_nick="w${ctr_idx}_${local_idx}_content"
        local hc_nick="w${ctr_idx}_${local_idx}_hc"
        if [ "$TOR_IMPL" = "tor" ]; then
            # C Tor: ADD_ONION via worker-server control API
            local add_resp
            add_resp=$(docker_cmd exec "$ctr_name" \
                curl -s -X POST http://127.0.0.1:9000/add_onion \
                -H "Content-Type: application/json" \
                -d "{\"workers\": [${local_idx}]}" 2>&1)
            if [ $? -ne 0 ] || echo "$add_resp" | grep -qi "fail\|error"; then
                log "WARNING: ADD_ONION failed for site ${i} (worker ${local_idx} on ${ctr_name}): ${add_resp}"
                sleep 2
                add_resp=$(docker_cmd exec "$ctr_name" \
                    curl -s -X POST http://127.0.0.1:9000/add_onion \
                    -H "Content-Type: application/json" \
                    -d "{\"workers\": [${local_idx}]}" 2>&1)
                if [ $? -ne 0 ] || echo "$add_resp" | grep -qi "fail\|error"; then
                    log "ERROR: ADD_ONION retry failed for site ${i}: ${add_resp}"
                    add_failures=$((add_failures + 1))
                else
                    log "  ADD_ONION retry succeeded for site ${i}: ${add_resp}"
                fi
            fi
        else
            # Arti: re-enable in config
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${content_nick}\"\]/,/^enabled = /{s/^enabled = false/enabled = true/}" \
                /etc/arti/arti.toml 2>/dev/null || true
            docker_cmd exec "$ctr_name" \
                sed -i "/^\[onion_services\.\"${hc_nick}\"\]/,/^enabled = /{s/^enabled = false/enabled = true/}" \
                /etc/arti/arti.toml 2>/dev/null || true

            if ! echo "$affected_containers" | grep -q "$ctr_name"; then
                affected_containers="${affected_containers} ${ctr_name}"
            fi
        fi

        # Re-enable HTTP responders
        local enable_resp
        enable_resp=$(docker_cmd exec "$ctr_name" \
            curl -s -X POST http://127.0.0.1:9000/enable \
            -H "Content-Type: application/json" \
            -d "{\"ports\": [${cp}, ${hp}]}" 2>&1) || {
            log "WARNING: Failed to re-enable ports ${cp},${hp} on ${ctr_name}: ${enable_resp}"
        }
    done

    # Arti only: SIGHUP to reload config (C Tor uses ADD_ONION above)
    if [ "$TOR_IMPL" != "tor" ]; then
        for ctr_name in $affected_containers; do
            docker_cmd exec "$ctr_name" sh -c "
                arti_pid=\$(pidof arti 2>/dev/null)
                if [ -n \"\$arti_pid\" ]; then
                    kill -HUP \$arti_pid 2>/dev/null
                else
                    su -s /bin/sh arti -c 'arti proxy -c /etc/arti/arti.toml' &
                fi
            " 2>/dev/null || true
        done
    fi

    if [ "$add_failures" -gt 0 ]; then
        log "WARNING: ${add_failures}/${count} ADD_ONION calls failed — some sites may not recover"
    fi
    log "Re-enabled ${count} sites silently (${TOR_LABEL} $([ "$TOR_IMPL" = "tor" ] && echo "ADD_ONION" || echo "SIGHUP"), no notifications sent)"
}

# Flush client descriptor caches via HSFETCH on polling clients.
# After a handoff (takeover or release), clients may have stale cached
# descriptors pointing to the old service. HSFETCH forces a fresh lookup.
flush_client_descriptor_cache() {
    local start="${1:-}"
    local count="${2:-}"

    [ -z "$start" ] || [ -z "$count" ] && return

    local addrs
    addrs=$(get_worker_content_addrs "$start" "$count")
    [ -z "$addrs" ] && return

    local addr_count
    addr_count=$(echo "$addrs" | grep -c . || echo 0)
    log "HSFETCH for ${addr_count} addresses across ${NUM_POLL_CLIENTS} poll clients..."

    # Build newline-separated list of service IDs (strip .onion suffix)
    local sids=""
    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ' | sed 's/\.onion$//')
        [ -z "$addr" ] && continue
        sids="${sids}${addr}\n"
    done <<< "$addrs"
    [ -z "$sids" ] && return

    # Step 1: NEWNYM on all poll clients (clears descriptor cache)
    local flush_pids=""
    for ci in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local cname="stress-poll-client-${ci}"
        docker_cmd exec "$cname" sh -c "
            cookie=\$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\n')
            [ -z \"\$cookie\" ] && exit 0
            printf 'AUTHENTICATE %s\r\nSIGNAL NEWNYM\r\nQUIT\r\n' \"\$cookie\" | nc -w 5 127.0.0.1 9051 >/dev/null 2>&1
        " &
        flush_pids="$flush_pids $!"
    done
    for pid in $flush_pids; do wait "$pid" 2>/dev/null; done
    sleep 3

    # Step 2: HSFETCH on all poll clients (fetches fresh descriptors)
    flush_pids=""
    for ci in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local cname="stress-poll-client-${ci}"
        docker_cmd exec "$cname" sh -c "
            cookie=\$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\n')
            [ -z \"\$cookie\" ] && exit 0
            (
                printf 'AUTHENTICATE %s\r\n' \"\$cookie\"
                printf '${sids}' | while IFS= read -r sid; do
                    [ -z \"\$sid\" ] && continue
                    printf 'HSFETCH %s\r\n' \"\$sid\"
                done
                printf 'QUIT\r\n'
            ) | nc -w 5 127.0.0.1 9051 >/dev/null 2>&1
        " &
        flush_pids="$flush_pids $!"
    done
    for pid in $flush_pids; do wait "$pid" 2>/dev/null; done
}

# Diagnose stragglers at timeout — identify which addresses are stuck and why.
diagnose_stragglers() {
    local content_addrs="$1"
    local expected_code="$2"  # "200" or "302"

    log "Straggler diagnostic:"
    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        local code
        code=$(docker_cmd exec "stress-poll-client-0" \
            curl -s --http1.0 --socks5-hostname "diag_${RANDOM}:x@127.0.0.1:9050" --max-time 10 \
            -o /dev/null -w "%{http_code}" "http://${addr}/" 2>/dev/null) || code="000"
        if [ "$code" != "$expected_code" ]; then
            log "  STRAGGLER: ${addr:0:30}... → HTTP ${code} (wanted ${expected_code})"
        fi
    done <<< "$content_addrs"
}

# ── Event-driven verification via verify-worker.py ───────────────────────────
# Launches verify-worker.py across ALL poll clients, each handling a subset of
# addresses. Reads JSON results files and aggregates. At 1000+ sites, each
# poll client verifies ~300 addresses instead of one client doing all of them.

run_verify_worker() {
    local expected_code="$1"
    local target_count="$2"
    local timeout_secs="$3"
    local poll_start="$4"
    local poll_count="$5"
    local addr_type="${6:-content}"  # "content" or "healthcheck"

    local addrs
    if [ "$addr_type" = "healthcheck" ]; then
        addrs=$(get_worker_hc_addrs "$poll_start" "$poll_count")
    else
        addrs=$(get_worker_content_addrs "$poll_start" "$poll_count")
    fi
    [ -z "$addrs" ] && return 1

    # Split addresses across poll clients (round-robin)
    local addr_arrays=""
    local ci=0
    for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        eval "poll_addrs_${i}=''"
    done
    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        local target_ci=$((ci % NUM_POLL_CLIENTS))
        eval "poll_addrs_${target_ci}=\"\${poll_addrs_${target_ci}} ${addr}\""
        ci=$((ci + 1))
    done <<< "$addrs"

    # Kill any previous verify-workers and launch new ones
    for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local cname="stress-poll-client-${i}"
        docker_cmd exec "$cname" sh -c 'pkill -f verify-worker.py 2>/dev/null; rm -f /tmp/verify-results.json' 2>/dev/null || true
        local these_addrs
        eval "these_addrs=\"\${poll_addrs_${i}}\""
        [ -z "$these_addrs" ] && continue
        docker_cmd exec "$cname" sh -c \
            "python3 /verify-worker.py ${expected_code} ${these_addrs} > /tmp/verify-worker.log 2>&1 &" 2>/dev/null
    done

    # Wait for all verify-workers to start and write initial results
    sleep 10

    # Poll all results files and aggregate
    local start_ts
    start_ts=$(date +%s)
    local deadline=$((start_ts + timeout_secs))
    local last_dashboard=0
    local prev_verified=0
    while [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 5

        # Aggregate results from all poll clients
        local total_verified=0
        local total_pending=0
        for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
            local cname="stress-poll-client-${i}"
            local results
            results=$(docker_cmd exec "$cname" cat /tmp/verify-results.json 2>/dev/null) || continue
            [ -z "$results" ] && continue
            local v p
            v=$(echo "$results" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verified',0))" 2>/dev/null || echo 0)
            p=$(echo "$results" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('pending',[])))" 2>/dev/null || echo 0)
            total_verified=$((total_verified + v))
            total_pending=$((total_pending + p))
        done

        # Progress dots
        if [ -n "$PHASE_LOG" ] && [ "$total_verified" -gt "$prev_verified" ]; then
            local new_dots=$((total_verified - prev_verified))
            printf '%0.s.' $(seq 1 "$new_dots") >> "$PHASE_LOG"
            prev_verified=$total_verified
        fi

        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 10 ]; then
            print_dashboard
            log "  (verified: ${total_verified}/${target_count}, pending: ${total_pending})"
            last_dashboard=$now
        fi

        if [ "$total_verified" -ge "$target_count" ] 2>/dev/null; then
            local elapsed=$(( $(date +%s) - start_ts ))
            for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
                docker_cmd exec "stress-poll-client-${i}" sh -c 'pkill -f verify-worker.py 2>/dev/null' || true
            done
            WAIT_RESULT="${total_verified}/${target_count} verified in $(fmt_duration $elapsed)"
            return 0
        fi
    done

    # Timed out — kill workers and dump diagnostics
    local elapsed=$(( $(date +%s) - start_ts ))
    for i in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        docker_cmd exec "stress-poll-client-${i}" sh -c 'pkill -f verify-worker.py 2>/dev/null' || true
        local worker_log
        worker_log=$(docker_cmd exec "stress-poll-client-${i}" tail -5 /tmp/verify-worker.log 2>/dev/null)
        [ -n "$worker_log" ] && log "  verify-worker-${i} log: $worker_log"
    done

    WAIT_RESULT="${total_verified:-0}/${target_count} verified, timed out after $(fmt_duration $elapsed)"
    return 1
}

wait_for_takeover() {
    local expected="$1"
    local timeout_secs="${2:-600}"
    local poll_start="${3:-$((TOTAL - FAILING))}"
    local poll_count="${4:-$FAILING}"

    log "Waiting for ${expected} takeovers via verify-worker (timeout: ${timeout_secs}s)..."

    if run_verify_worker "302" "$expected" "$timeout_secs" "$poll_start" "$poll_count"; then
        local elapsed_str
        elapsed_str=$(echo "$WAIT_RESULT" | grep -o '[0-9]*m:[0-9]*s')
        [ -n "$PHASE_LOG" ] && echo " ${expected}/${expected}" >> "$PHASE_LOG"
        log "Takeover: $WAIT_RESULT"
        print_dashboard
        return 0
    fi

    [ -n "$PHASE_LOG" ] && echo " (timed out)" >> "$PHASE_LOG"
    log "Takeover: $WAIT_RESULT"
    local content_addrs
    content_addrs=$(get_worker_content_addrs "$poll_start" "$poll_count")
    diagnose_stragglers "$content_addrs" "302"
    print_dashboard
    return 1
}

wait_for_recovery() {
    local expected_healthy="$1"
    local timeout_secs="${2:-600}"
    local poll_start="${3:-$((TOTAL - FAILING))}"
    local poll_count="${4:-$FAILING}"

    log "Waiting for recovery via verify-worker (${expected_healthy} sites, timeout: ${timeout_secs}s)..."

    if run_verify_worker "200" "$expected_healthy" "$timeout_secs" "$poll_start" "$poll_count"; then
        [ -n "$PHASE_LOG" ] && echo " ${expected_healthy}/${expected_healthy}" >> "$PHASE_LOG"
        log "Recovery complete: $WAIT_RESULT"
        print_dashboard
        return 0
    fi

    [ -n "$PHASE_LOG" ] && echo " (timed out)" >> "$PHASE_LOG"
    log "Recovery: $WAIT_RESULT"
    local content_addrs
    content_addrs=$(get_worker_content_addrs "$poll_start" "$poll_count")
    diagnose_stragglers "$content_addrs" "200"
    print_dashboard
    return 1
}

# ── Graceful offline/online notification ─────────────────────────────────────

# POST /offline to OnionHeaven for a range of sites (triggers immediate takeover).
# This simulates a real OnionPress instance calling /offline before sleeping/quitting.
notify_offline() {
    local start="$1"
    local count="$2"

    log "Sending /offline notifications for sites ${start}..$(( start + count - 1 ))..."

    # Build all offline payloads in one python3 call, then send them in a single
    # docker exec to avoid 300+ subprocess calls (each ~1s under qemu).
    local payloads
    local _notify_log="${OUTPUT_DIR}/notify_offline_debug.log"
    payloads=$(python3 -c "
import json, glob, sys, base64, os
sys.path.insert(0, '${SCRIPT_DIR}/../src')
from onion_auth import sign_payload, make_timestamp
payloads = []
# Use absolute path to avoid cwd issues
output_dir = os.path.abspath('${OUTPUT_DIR}')
files = sorted(glob.glob(os.path.join(output_dir, 'worker-*-info.json')))
if not files:
    print(f'ERROR: no worker-*-info.json in {output_dir}', file=sys.stderr)
for f in files:
    try:
        workers = json.load(open(f))
        for w in workers:
            idx = w.get('global_index', -1)
            if idx >= ${start} and idx < $((start + count)):
                ca = w.get('content_address', '')
                ha = w.get('healthcheck_address', '')
                pk = w.get('privkey_b64', '')
                pub = w.get('pubkey_b64', '')
                if ca and ha and pk and pub:
                    privkey = base64.b64decode(pk)
                    pubkey = base64.b64decode(pub)
                    ts = make_timestamp()
                    sig = sign_payload(privkey, pubkey, 'offline', ca, ha, ts)
                    payloads.append(json.dumps({'content_address': ca, 'healthcheck_address': ha, 'timestamp': ts, 'signature': sig}))
    except Exception as e:
        print(f'ERROR generating payload from {f}: {e}', file=sys.stderr)
print(f'{len(payloads)} /offline payload(s) for sites ${start}..$(( start + count - 1))', file=sys.stderr)
for p in payloads:
    print(p)
" 2>"$_notify_log")

    if [ -z "$payloads" ]; then
        [ -s "$_notify_log" ] && log "  notify_offline debug: $(cat "$_notify_log")"
        log "WARNING: No payloads generated for /offline"
        return
    fi

    local payload_count
    payload_count=$(echo "$payloads" | wc -l | tr -d ' ')
    log "  Generated ${payload_count} /offline payload(s)"

    local notified
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        # Local: send directly over Docker network (fast, reliable — no Tor latency)
        notified=$(echo "$payloads" | docker_cmd exec -i onionpress-tor sh -c '
            tmpdir=$(mktemp -d); i=0
            while IFS= read -r payload; do
                i=$((i+1))
                addr=$(echo "$payload" | sed -n "s/.*\"content_address\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p")
                (for attempt in 1 2 3; do
                    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
                        -X POST "http://127.0.0.1:8083/offline" \
                        -H "Content-Type: application/json" \
                        -d "$payload" 2>/dev/null)
                    if [ "$code" = "200" ]; then
                        touch "$tmpdir/ok.$i"
                        break
                    fi
                    [ "$attempt" -lt 3 ] && sleep 2
                 done
                 [ "$code" != "200" ] && echo "FAILED /offline HTTP $code for $addr (after 3 attempts)" >&2
                 echo "$i:$code" >> "$tmpdir/results") &
                [ $((i % 10)) -eq 0 ] && wait
            done
            wait
            ls "$tmpdir"/ok.* 2>/dev/null | wc -l | tr -d " "
            rm -rf "$tmpdir"
        ' 2>>"$_notify_log")
    else
        # Remote: send over Tor with parallelism (up to 10 concurrent)
        notified=$(echo "$payloads" | docker_cmd exec -i onionpress-tor sh -c '
            tmpdir=$(mktemp -d); i=0
            while IFS= read -r payload; do
                i=$((i+1))
                addr=$(echo "$payload" | sed -n "s/.*\"content_address\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p")
                (for attempt in 1 2 3; do
                    code=$(curl -s -o /dev/null -w "%{http_code}" --socks5-hostname "off${i}r${attempt}:x@127.0.0.1:9050" --max-time 30 \
                        -X POST "http://'"${ONIONHEAVEN_ADDR}"':8083/offline" \
                        -H "Content-Type: application/json" \
                        -d "$payload" 2>/dev/null)
                    if [ "$code" = "200" ]; then
                        touch "$tmpdir/ok.$i"
                        break
                    fi
                    [ "$attempt" -lt 3 ] && sleep 5
                 done
                 [ "$code" != "200" ] && echo "FAILED /offline HTTP $code for $addr (after 3 attempts)" >&2
                 echo "$i:$code" >> "$tmpdir/results") &
                [ $((i % 10)) -eq 0 ] && wait
            done
            wait
            ls "$tmpdir"/ok.* 2>/dev/null | wc -l | tr -d " "
            rm -rf "$tmpdir"
        ' 2>>"$_notify_log")
    fi

    if [ -s "$_notify_log" ]; then
        log "  notify_offline debug: $(cat "$_notify_log")"
    fi
    log "Sent /offline for ${notified:-0} sites"
    if [ "${notified:-0}" -lt "$count" ]; then
        local missed=$(( count - ${notified:-0} ))
        log "WARNING: ${missed} /offline notification(s) failed — those sites will NOT be taken over"
    fi
    log_json "\"event\":\"offline_notify\",\"start\":${start},\"count\":${count},\"notified\":${notified:-0}"
}

# POST /online to OnionHeaven for a range of sites (triggers immediate release).
# This simulates a real OnionPress instance calling /online after waking up.
notify_online() {
    local start="$1"
    local count="$2"

    log "Sending /online notifications for sites ${start}..$(( start + count - 1 ))..."

    local payloads
    local _notify_log="${OUTPUT_DIR}/notify_online_debug.log"
    payloads=$(python3 -c "
import json, glob, sys, base64, os
sys.path.insert(0, '${SCRIPT_DIR}/../src')
from onion_auth import sign_payload, make_timestamp
payloads = []
# Use absolute path to avoid cwd issues
output_dir = os.path.abspath('${OUTPUT_DIR}')
files = sorted(glob.glob(os.path.join(output_dir, 'worker-*-info.json')))
if not files:
    print(f'ERROR: no worker-*-info.json in {output_dir}', file=sys.stderr)
for f in files:
    try:
        workers = json.load(open(f))
        for w in workers:
            idx = w.get('global_index', -1)
            if idx >= ${start} and idx < $((start + count)):
                ca = w.get('content_address', '')
                ha = w.get('healthcheck_address', '')
                pk = w.get('privkey_b64', '')
                pub = w.get('pubkey_b64', '')
                if ca and ha and pk and pub:
                    privkey = base64.b64decode(pk)
                    pubkey = base64.b64decode(pub)
                    ts = make_timestamp()
                    sig = sign_payload(privkey, pubkey, 'online', ca, ha, ts)
                    from onionpress.key_manager import build_openssh_key, derive_public_key
                    pem = build_openssh_key(privkey, pubkey)
                    payloads.append(json.dumps({'content_address': ca, 'healthcheck_address': ha, 'timestamp': ts, 'signature': sig, 'arti_key_pem': base64.b64encode(pem).decode()}))
    except Exception as e:
        print(f'ERROR generating payload from {f}: {e}', file=sys.stderr)
print(f'{len(payloads)} /online payload(s) for sites ${start}..$(( start + count - 1))', file=sys.stderr)
for p in payloads:
    print(p)
" 2>"$_notify_log")

    if [ -z "$payloads" ]; then
        [ -s "$_notify_log" ] && log "  notify_online debug: $(cat "$_notify_log")"
        log "WARNING: No payloads generated for /online"
        return
    fi

    local payload_count
    payload_count=$(echo "$payloads" | wc -l | tr -d ' ')
    log "  Generated ${payload_count} /online payload(s)"

    local notified
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        # Local: send directly over Docker network (fast, reliable — no Tor latency)
        notified=$(echo "$payloads" | docker_cmd exec -i onionpress-tor sh -c '
            tmpdir=$(mktemp -d); i=0
            while IFS= read -r payload; do
                i=$((i+1))
                addr=$(echo "$payload" | sed -n "s/.*\"content_address\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p")
                (for attempt in 1 2 3; do
                    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
                        -X POST "http://127.0.0.1:8083/online" \
                        -H "Content-Type: application/json" \
                        -d "$payload" 2>/dev/null)
                    if [ "$code" = "200" ]; then
                        touch "$tmpdir/ok.$i"
                        break
                    fi
                    [ "$attempt" -lt 3 ] && sleep 2
                 done
                 [ "$code" != "200" ] && echo "FAILED /online HTTP $code for $addr (after 3 attempts)" >&2
                 echo "$i:$code" >> "$tmpdir/results") &
                [ $((i % 10)) -eq 0 ] && wait
            done
            wait
            ls "$tmpdir"/ok.* 2>/dev/null | wc -l | tr -d " "
            rm -rf "$tmpdir"
        ' 2>>"$_notify_log")
    else
        # Remote: send over Tor with parallelism (up to 10 concurrent)
        notified=$(echo "$payloads" | docker_cmd exec -i onionpress-tor sh -c '
            tmpdir=$(mktemp -d); i=0
            while IFS= read -r payload; do
                i=$((i+1))
                addr=$(echo "$payload" | sed -n "s/.*\"content_address\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p")
                (for attempt in 1 2 3; do
                    code=$(curl -s -o /dev/null -w "%{http_code}" --socks5-hostname "on${i}r${attempt}:x@127.0.0.1:9050" --max-time 30 \
                        -X POST "http://'"${ONIONHEAVEN_ADDR}"':8083/online" \
                        -H "Content-Type: application/json" \
                        -d "$payload" 2>/dev/null)
                    if [ "$code" = "200" ]; then
                        touch "$tmpdir/ok.$i"
                        break
                    fi
                    [ "$attempt" -lt 3 ] && sleep 5
                 done
                 [ "$code" != "200" ] && echo "FAILED /online HTTP $code for $addr (after 3 attempts)" >&2
                 echo "$i:$code" >> "$tmpdir/results") &
                [ $((i % 10)) -eq 0 ] && wait
            done
            wait
            ls "$tmpdir"/ok.* 2>/dev/null | wc -l | tr -d " "
            rm -rf "$tmpdir"
        ' 2>>"$_notify_log")
    fi

    if [ -s "$_notify_log" ]; then
        log "  notify_online debug: $(cat "$_notify_log")"
    fi
    log "Sent /online for ${notified:-0} sites"
    if [ "${notified:-0}" -lt "$count" ]; then
        local missed=$(( count - ${notified:-0} ))
        log "WARNING: ${missed} /online notification(s) failed — those sites will NOT be released"
    fi
    log_json "\"event\":\"online_notify\",\"start\":${start},\"count\":${count},\"notified\":${notified:-0}"
}

# Get content addresses that have active takeovers (one per line).
get_taken_over_addresses() {
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        docker_cmd exec onionheaven \
            sqlite3 "$ONIONHEAVEN_DB_PATH" "SELECT content_address FROM registry WHERE status='taken-over'" 2>/dev/null || true
    else
        # On remote machines, use the disabled sites' addresses from local info files.
        # These are the sites we know we disabled, so they should be taken over.
        local fail_start=$((TOTAL - FAILING))
        for i in $(seq "$fail_start" $((TOTAL - 1))); do
            local ctr_idx=$((i / PER_CTR))
            local local_idx=$((i % PER_CTR))
            local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"
            python3 -c "
import json, sys
try:
    with open('${info_file}') as f:
        workers = json.load(f)
    w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
    if w and w.get('content_address'):
        print(w['content_address'])
except: pass
" 2>/dev/null
        done
    fi
}

# Verify that taken-over addresses serve 302 redirects to the Wayback Machine.
# Samples $sample_size random addresses to keep timing reasonable.
verify_redirects() {
    local phase_label="$1"
    local sample_size="${2:-5}"
    local verify_start_ts
    verify_start_ts=$(date +%s)
    local deadline=$((verify_start_ts + REDIRECT_VERIFY_TIMEOUT))

    log "${phase_label}: Verifying 302 redirects on sample of taken-over addresses..."

    local addrs
    addrs=$(get_taken_over_addresses)

    if [ -z "$addrs" ]; then
        log "${phase_label}: No taken-over addresses found — skipping redirect verification"
        log_json "\"event\":\"redirect_verify\",\"phase\":\"${phase_label}\",\"sampled\":0,\"passed\":0,\"failed\":0,\"skipped\":true"
        return 0
    fi

    # Count total taken-over
    local total_taken=0
    while IFS= read -r line; do
        line=$(echo "$line" | tr -d '\r\n ')
        [ -n "$line" ] && total_taken=$((total_taken + 1))
    done <<< "$addrs"

    # Portable random sampling (no sort -R on macOS)
    local sampled_addrs
    sampled_addrs=$(echo "$addrs" | awk 'BEGIN{srand()}{print rand()"\t"$0}' | sort -n | cut -f2 | head -n "$sample_size")

    # Use dedicated polling clients for verification.
    # Falls back to onionpress-tor if none available.
    local verify_ctrs=""
    local num_verify_ctrs=0
    for ci in $(seq 0 $((NUM_POLL_CLIENTS - 1))); do
        local cname="stress-poll-client-${ci}"
        if docker_cmd inspect "$cname" >/dev/null 2>&1; then
            verify_ctrs="${verify_ctrs} ${cname}"
            num_verify_ctrs=$((num_verify_ctrs + 1))
        fi
    done
    local use_stress_ctrs=true
    if [ "$num_verify_ctrs" -eq 0 ]; then
        use_stress_ctrs=false
    fi
    local vctr_arr
    vctr_arr=($verify_ctrs)

    local sampled=0
    local sampled_list=""
    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        sampled=$((sampled + 1))
        sampled_list="${sampled_list} ${addr}"
    done <<< "$sampled_addrs"

    # Retry loop: keep trying failed addresses until all pass or timeout
    local remaining="$sampled_list"
    local passed=0
    local failed_details=""
    local round=0

    while [ -n "$remaining" ] && [ "$(date +%s)" -lt "$deadline" ]; do
        round=$((round + 1))
        local still_failing=""

        local tmpdir
        tmpdir=$(mktemp -d)
        local pids=""
        local idx=0

        for addr in $remaining; do
            [ -z "$addr" ] && continue
            idx=$((idx + 1))

            (
                local http_response="000"
                if [ "$use_stress_ctrs" = true ]; then
                    local vctr="${vctr_arr[$(( (idx - 1) % num_verify_ctrs ))]}"
                    http_response=$(docker_cmd exec "$vctr" \
                        curl -s -o /dev/null -w "%{http_code} %{redirect_url}" \
                        --http1.0 \
                        --socks5-hostname "verify${idx}:x@127.0.0.1:9050" \
                        --max-time 30 \
                        "http://${addr}" 2>/dev/null) || http_response="000"
                else
                    http_response=$(docker_cmd exec onionpress-wordpress \
                        curl -s -o /dev/null -w "%{http_code} %{redirect_url}" \
                        --http1.0 \
                        --socks5-hostname onionpress-tor:9050 \
                        --max-time 30 \
                        "http://${addr}" 2>/dev/null) || http_response="000"
                fi

                local http_code redirect_url
                http_code=$(echo "$http_response" | awk '{print $1}')
                redirect_url=$(echo "$http_response" | awk '{print $2}')

                if [ "$http_code" = "302" ]; then
                    if echo "$redirect_url" | grep -qi 'web.archive.org\|archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion'; then
                        echo "PASS ${addr} ${redirect_url}" > "${tmpdir}/${addr}"
                    else
                        echo "FAIL ${addr} 302-wrong-dest ${redirect_url}" > "${tmpdir}/${addr}"
                    fi
                else
                    echo "FAIL ${addr} HTTP-${http_code}" > "${tmpdir}/${addr}"
                fi
            ) &
            pids="$pids $!"
        done

        for pid in $pids; do
            wait "$pid" 2>/dev/null || true
        done

        # Collect results for this round
        for result_file in "${tmpdir}"/*; do
            [ -f "$result_file" ] || continue
            local result
            result=$(cat "$result_file")
            local status addr_result
            status=$(echo "$result" | awk '{print $1}')
            addr_result=$(echo "$result" | awk '{print $2}')
            if [ "$status" = "PASS" ]; then
                log "  PASS: ${addr_result} → 302 → $(echo "$result" | awk '{print $3}')"
                passed=$((passed + 1))
            else
                still_failing="${still_failing} ${addr_result}"
                failed_details="$(echo "$result" | awk '{print $3}')"
            fi
        done
        rm -rf "$tmpdir"

        remaining=$(echo "$still_failing" | xargs)
        if [ -n "$remaining" ]; then
            local remain_count
            remain_count=$(echo "$remaining" | wc -w | tr -d ' ')
            local elapsed=$(( $(date +%s) - verify_start_ts ))
            log "  ${phase_label}: ${passed}/${sampled} passed, ${remain_count} still failing (${elapsed}s elapsed) — retrying in 15s..."
            sleep 15
        fi
    done

    local failed=$((sampled - passed))
    # Log any final failures
    if [ -n "$remaining" ]; then
        for addr in $remaining; do
            log "  FAIL: ${addr} → still unreachable after timeout"
        done
    fi

    local verify_elapsed=$(( $(date +%s) - verify_start_ts ))
    WAIT_RESULT="${passed}/${sampled} passed, ${failed} failed in $(fmt_duration $verify_elapsed)"
    log "${phase_label}: Redirect verification — ${passed}/${sampled} passed, ${failed} failed (${total_taken} total taken over)"
    log_json "\"event\":\"redirect_verify\",\"phase\":\"${phase_label}\",\"total_taken\":${total_taken},\"sampled\":${sampled},\"passed\":${passed},\"failed\":${failed}"

    # Dump debug log from redirect service (OnionHeaven host only)
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        log "${phase_label}: Redirect debug log:"
        docker_cmd exec onionheaven cat /tmp/onionheaven-redirect-debug.log 2>/dev/null | tail -30 | while IFS= read -r dbgline; do
            echo "           [redirect-dbg] $dbgline"
        done || echo "           [redirect-dbg] (no debug log found)"
    fi
}

# ── Cleanup ───────────────────────────────────────────────────────────────────

cleanup_stress_test() {
    log "Cleaning up stress test artifacts..."

    # Remove all stress containers (parallel)
    for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
        docker_cmd rm -f "stress-worker-${idx}" 2>/dev/null &
    done
    # Also catch any extras
    docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' | while read -r ctr; do
        docker_cmd rm -f "$ctr" 2>/dev/null &
    done || true
    wait
    # Remove polling clients (parallel)
    stop_poll_clients
    log "  Removed stress + polling containers"

    # Unregister stress-test sites from OnionHeaven (signed payloads)
    local payloads
    payloads=$(python3 -c "
import json, glob, sys, base64
sys.path.insert(0, '${SCRIPT_DIR}/../src')
from onion_auth import sign_payload, make_timestamp
for idx in range(${NUM_CONTAINERS}):
    f = '${OUTPUT_DIR}/worker-' + str(idx) + '-info.json'
    try:
        workers = json.load(open(f))
        for w in workers:
            ca = w.get('content_address', '')
            ha = w.get('healthcheck_address', '')
            pk = w.get('privkey_b64', '')
            pub = w.get('pubkey_b64', '')
            if ca and pk and pub:
                privkey = base64.b64decode(pk)
                pubkey = base64.b64decode(pub)
                ts = make_timestamp()
                sig = sign_payload(privkey, pubkey, 'unregister', ca, ha, ts)
                print(json.dumps({'content_address': ca, 'healthcheck_address': ha, 'timestamp': ts, 'signature': sig}))
    except: pass
" 2>/dev/null) || true

    local count=0
    if [ -n "$payloads" ]; then
        while IFS= read -r payload; do
            [ -z "$payload" ] && continue
            docker_cmd exec onionpress-tor \
                curl -s --socks5-hostname "unreg${count}:x@127.0.0.1:9050" --max-time 30 \
                -X POST "http://${ONIONHEAVEN_ADDR}:8083/unregister" \
                -H "Content-Type: application/json" \
                -d "$payload" 2>/dev/null || true
            count=$((count + 1))
        done <<< "$payloads"
    fi

    log "  Unregistered ${count} entries"

    log "Cleanup complete"
}

# ── Site mode (full test) ────────────────────────────────────────────────────

check_previous_artifacts() {
    # Check for leftover stress test artifacts from a previous run
    local stale_containers
    stale_containers=$(docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' || true)
    local stale_count=0
    [ -n "$stale_containers" ] && stale_count=$(echo "$stale_containers" | wc -l | tr -d ' ')

    local registry_count=0
    if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
        registry_count=$(docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
            "SELECT COUNT(*) FROM registry WHERE unregistered_at IS NULL" 2>/dev/null || echo 0)
    fi

    if [ "$stale_count" -gt 0 ] || [ "$registry_count" -gt 0 ]; then
        echo ""
        echo "Found artifacts from a previous stress test:"
        [ "$stale_count" -gt 0 ] && echo "  - ${stale_count} stress container(s)"
        [ "$registry_count" -gt 0 ] && echo "  - ${registry_count} registry entries"
        echo ""
        if [ -t 0 ]; then
            read -r -p "Clean up before starting? [Y/n] " answer
        else
            answer="y"  # auto-clean when not interactive
        fi
        case "$answer" in
            [nN]*)
                log "Keeping previous artifacts"
                ;;
            *)
                log "Cleaning previous artifacts..."
                # Remove stress containers
                if [ "$stale_count" -gt 0 ]; then
                    echo "$stale_containers" | while read -r ctr; do
                        docker_cmd rm -f "$ctr" 2>/dev/null || true
                    done
                    log "  Removed ${stale_count} stress containers"
                fi
                # Clear registry
                if [ "$registry_count" -gt 0 ] && [ "$IS_ONIONHEAVEN_HOST" = true ]; then
                    docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
                        "DELETE FROM registry;" 2>/dev/null || true
                    log "  Cleared ${registry_count} registry entries"
                fi
                # Clean farm DB tables
                if [ "$IS_ONIONHEAVEN_HOST" = true ]; then
                    docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
                        "DELETE FROM takeover_containers; DELETE FROM farm_scale_requests;" 2>/dev/null || true
                fi
                echo ""
                ;;
        esac
    fi
}

run_worker() {
    preflight
    detect_onionheaven_addr

    # Check for leftover artifacts before starting
    check_previous_artifacts

    # Create timestamped run directory so successive runs don't overwrite
    local run_ts
    run_ts=$(date '+%Y%m%d-%H%M%S')
    RUN_DIR="${OUTPUT_DIR}/run-${run_ts}"
    mkdir -p "$RUN_DIR"
    # Update OUTPUT_DIR to point to the run directory for all file writes
    OUTPUT_DIR="$RUN_DIR"
    # Maintain a "latest" symlink for convenience
    ln -sfn "run-${run_ts}" "$(dirname "$RUN_DIR")/latest"

    LOG_FILE="${OUTPUT_DIR}/stress-test.log"
    PHASE_LOG="${OUTPUT_DIR}/phase.log"
    : > "$PHASE_LOG"  # start fresh
    write_phase_header
    open_phase_log_window

    # Detect Tor implementation from running container
    TOR_IMPL=$(docker exec onionpress-tor sh -c 'echo ${TOR_IMPL:-arti}' 2>/dev/null || echo "arti")
    if [ "$TOR_IMPL" = "tor" ]; then
        TOR_LABEL="C Tor"
    else
        TOR_LABEL="Arti"
    fi
    log "=== OnionHeaven Stress Test (${TOR_LABEL}) ==="
    log "OnionHeaven: ${ONIONHEAVEN_ADDR}"
    [ -n "$LOCAL_ONION_ADDR" ] && log "This machine: ${LOCAL_ONION_ADDR}" || true
    log "Sites: ${TOTAL} total (${HEALTHY} stay healthy, ${FAILING} will fail)"
    log "Stress containers: ${NUM_CONTAINERS} × ${PER_CTR} sites/container"
    if [ "$BATCH_SIZE" -gt 0 ] 2>/dev/null; then
        log "Container batch size: ${BATCH_SIZE}"
    fi
    log "Output: ${OUTPUT_DIR}"
    echo ""

    RUN_START_TS=$(date +%s)
    phase_start "1" "Starting ${NUM_CONTAINERS} containers (${TOTAL} sites) (est. <1m)"

    trap 'log "Cleaning up before exit..."; cleanup_stress_test' EXIT
    trap 'log "Interrupted..."; exit 130' INT TERM

    # Phase 1: Start site containers + polling clients
    log "Phase 1: Starting ${NUM_CONTAINERS} site containers + ${NUM_POLL_CLIENTS} polling clients..."
    start_all_workers
    start_poll_clients
    phase_result "1" "Started ${NUM_CONTAINERS} stress + ${NUM_POLL_CLIENTS} poll containers"
    echo ""

    # Track results for summary table
    local r_phase2="" r_phase3=""
    local r_a1="" r_a1v="" r_a2="" r_a_sum=""
    local r_b1="" r_b1v="" r_b2="" r_b_sum=""
    local r_c1="" r_c2="" r_c_sum=""

    # Phase 2: Wait for all sites to bootstrap and self-register over Tor
    phase_start "2" "Waiting for sites to bootstrap and register over Tor (est. 2m)"
    log "Phase 2: Waiting for sites to bootstrap and register over Tor..."
    if ! wait_for_bootstrap "$BOOTSTRAP_TIMEOUT"; then
        log "WARNING: Not all sites bootstrapped"
    fi
    r_phase2="$WAIT_RESULT"
    phase_result "2" "$WAIT_RESULT"
    extract_all_worker_info

    # No SIGHUP after registration — both C Tor and Arti publish descriptors
    # during bootstrap. A SIGHUP forces a full re-publish of all descriptors,
    # resetting the propagation clock and causing false takeovers at scale.
    echo ""

    # If lazy activation is pending, wait for the onionheaven container to come up
    # (the launcher's watcher detects registrations and bootstraps the container)
    if [ "$LAZY_ACTIVATION" = true ]; then
        phase_start "2b" "Waiting for lazy OnionHeaven activation (est. 1-2m)"
        if _wait_for_lazy_activation; then
            phase_result "2b" "OnionHeaven container activated and injected"
        else
            phase_result "2b" "FAILED — onionheaven container did not start"
            log "ERROR: Cannot proceed without onionheaven container"
            exit 1
        fi
        echo ""
    fi

    # HSFETCH all site addresses on poll clients to speed up descriptor discovery
    flush_client_descriptor_cache 0 "$TOTAL"

    # Phase 3: Wait for all sites to be reachable via Tor (verify-worker checks healthcheck addresses)
    phase_start "3" "Waiting for all ${TOTAL} sites to be reachable via Tor (est. 1m)"
    local hc_addr_type="healthcheck"
    [ "$NO_HEALTHCHECK" = true ] && hc_addr_type="content"
    log "Phase 3: Waiting for ${TOTAL} ${hc_addr_type} addresses to be reachable via Tor from poll clients (timeout: ${HEALTHY_TIMEOUT}s)..."
    if run_verify_worker "200" "$TOTAL" "$HEALTHY_TIMEOUT" 0 "$TOTAL" "$hc_addr_type"; then
        log "Phase 3: all sites reachable"
    else
        log "WARNING: Not all sites became healthy, continuing anyway..."
    fi
    r_phase3="$WAIT_RESULT"
    phase_result "3" "$WAIT_RESULT"
    echo ""

    if [ "$FAILING" -gt 0 ]; then
        local fail_start=$((TOTAL - FAILING))
        local scenario_ts

        # ══════════════════════════════════════════════════════════════
        # Scenario A: Graceful offline/online (with /offline + /online)
        # ══════════════════════════════════════════════════════════════

        phase_start "A" "GRACEFUL OFFLINE/ONLINE (with /offline and /online notifications)"

        # A1: Takeover — /offline + disable, then wait for 302s
        phase_start "A.1" "Graceful takeover: /offline + disable ${FAILING} sites, wait for takeover (est. 1-2m)"
        scenario_ts=$(date +%s)
        log "Phase A.1: Graceful offline — disabling responders + sending /offline for ${FAILING} sites..."
        # Disable HTTP responders + DEL_ONION, then send /offline to trigger takeover.
        # The 30s server-side cooldown prevents stale heartbeats from releasing it.
        disable_workers "$fail_start" "$FAILING"
        notify_offline "$fail_start" "$FAILING"
        flush_client_descriptor_cache "$fail_start" "$FAILING"
        log "Phase A.1: Waiting for takeovers..."
        if ! wait_for_takeover "$FAILING" "$TAKEOVER_TIMEOUT"; then
            log "WARNING: Not all expected takeovers happened"
        fi
        local takeover_elapsed=$(( $(date +%s) - scenario_ts ))
        r_a1="$WAIT_RESULT ($(fmt_duration $takeover_elapsed) e2e)"
        phase_result "A.1" "Takeover: $r_a1"
        echo ""

        phase_start "A.1v" "Double-check taken-over addresses redirect (302) to Wayback Machine from here (est. <1m)"
        verify_redirects "A.1v" 5
        r_a1v="$WAIT_RESULT"
        phase_result "A.1v" "$r_a1v"
        echo ""

        # A2: Recovery — re-enable + /online, then wait for 200s
        phase_start "A.2" "Graceful recovery: re-enable + /online for ${FAILING} sites, wait for recovery (est. 1-2m)"
        scenario_ts=$(date +%s)
        log "Phase A.2: Graceful recovery — re-enabling responders + sending /online..."
        enable_workers "$fail_start" "$FAILING"
        notify_online "$fail_start" "$FAILING"
        flush_client_descriptor_cache "$fail_start" "$FAILING"
        log "Phase A.2: Waiting for recovery..."
        if ! wait_for_recovery "$FAILING" "$RECOVERY_TIMEOUT"; then
            log "WARNING: Not all sites recovered from graceful offline"
        fi
        local recovery_elapsed=$(( $(date +%s) - scenario_ts ))
        r_a2="$WAIT_RESULT ($(fmt_duration $recovery_elapsed) e2e)"
        phase_result "A.2" "Recovery: $r_a2"
        echo ""

        r_a_sum="takeover $(fmt_duration $takeover_elapsed), recovery $(fmt_duration $recovery_elapsed)"
        phase_result "A" "Graceful: $r_a_sum"
        echo ""

        # ══════════════════════════════════════════════════════════════
        # Scenario B: Silent crash + silent recovery (heartbeat-only)
        # ══════════════════════════════════════════════════════════════

        phase_start "B" "SILENT CRASH/RECOVERY (no notifications, heartbeat-only detection)"

        # B1: Takeover — disable only (no /offline), wait for heartbeat monitor to detect
        phase_start "B.1" "Silent crash: disable ${FAILING} sites (no /offline), wait for heartbeat monitor takeover (est. 9m)"
        scenario_ts=$(date +%s)
        log "Phase B.1: Silent crash — disabling responders for ${FAILING} sites (no /offline)..."
        disable_workers "$fail_start" "$FAILING"
        flush_client_descriptor_cache "$fail_start" "$FAILING"
        log "Phase B.1: Waiting for heartbeat-detected takeovers..."
        if ! wait_for_takeover "$FAILING" "$TAKEOVER_TIMEOUT"; then
            log "WARNING: Not all expected takeovers happened"
        fi
        takeover_elapsed=$(( $(date +%s) - scenario_ts ))
        r_b1="$WAIT_RESULT ($(fmt_duration $takeover_elapsed) e2e)"
        phase_result "B.1" "Takeover: $r_b1"
        echo ""

        phase_start "B.1v" "Double-check taken-over addresses redirect (302) to Wayback Machine from here (est. <1m)"
        verify_redirects "B.1v" 5
        r_b1v="$WAIT_RESULT"
        phase_result "B.1v" "$r_b1v"
        echo ""

        r_b_sum="takeover $(fmt_duration $takeover_elapsed)"
        phase_result "B" "Silent: $r_b_sum"
        echo ""

    else
        log "No failing sites configured — skipping failure/recovery test"
        echo ""
    fi

    # Summary table in phase log
    if [ -n "$PHASE_LOG" ]; then
        local total_elapsed=$(( $(date +%s) - RUN_START_TS ))
        cat >> "$PHASE_LOG" << SUMMARY

====================================================================
  SUMMARY — $(date '+%Y-%m-%d %H:%M:%S') — total $(fmt_duration $total_elapsed)
--------------------------------------------------------------------
  Phase 2 (bootstrap):   $r_phase2
  Phase 3 (healthy):     $r_phase3
SUMMARY
        if [ "$FAILING" -gt 0 ]; then
            cat >> "$PHASE_LOG" << SUMMARY
  ---
  A. Graceful (/offline + /online):
     A.1  Takeover:       ${r_a1:-(not run)}
     A.1v Verify 302s:    ${r_a1v:-(not run)}
     A.2  Recovery:       ${r_a2:-(not run)}
     => ${r_a_sum:-(incomplete)}
  ---
  B. Silent (heartbeat-only, no notifications):
     B.1  Takeover:       ${r_b1:-(not run)}
     B.1v Verify 302s:    ${r_b1v:-(not run)}
     => ${r_b_sum:-(incomplete)}
SUMMARY
        fi
        cat >> "$PHASE_LOG" << SUMMARY
SUMMARY
        echo "====================================================================" >> "$PHASE_LOG"
    fi

    # Final metrics + cleanup
    phase_start "done" "Final metrics and cleanup"
    log "=== Final metrics ==="
    print_dashboard
    phase_result "done" "Stress test complete"
    echo ""

    # Cleanup
    trap - EXIT
    cleanup_stress_test
    echo ""

    log "=== Stress test complete ==="
    log "Results saved to: ${OUTPUT_DIR}/metrics.jsonl"
    exit 0
}

# ── Coordinator mode ──────────────────────────────────────────────────────────
run_coordinator() {
    preflight
    detect_onionheaven_addr
    # Follow the "latest" symlink if it exists, otherwise use OUTPUT_DIR directly
    if [ -L "${OUTPUT_DIR}/latest" ]; then
        OUTPUT_DIR="${OUTPUT_DIR}/latest"
    fi
    mkdir -p "$OUTPUT_DIR"
    PHASE_LOG="${OUTPUT_DIR}/phase.log"

    log "=== OnionHeaven Stress Test (coordinator — read-only monitor) ==="
    log "Output: ${OUTPUT_DIR}"
    log "Phase log: ${PHASE_LOG}"
    log "Press Ctrl-C to stop"
    echo ""

    # Open a Terminal window tailing the phase log (written by the site process)
    if [ -f "$PHASE_LOG" ]; then
        open_phase_log_window
    else
        log "Phase log not yet created — will open window when test starts"
    fi

    local log_window_opened=false
    [ -f "$PHASE_LOG" ] && log_window_opened=true

    while true; do
        # Open log window once phase.log appears
        if [ "$log_window_opened" = false ] && [ -f "$PHASE_LOG" ]; then
            open_phase_log_window
            log_window_opened=true
        fi

        print_dashboard
        sleep 10
    done
}

# ── Cleanup mode ──────────────────────────────────────────────────────────────
run_cleanup() {
    log "=== OnionHeaven Stress Test Cleanup ==="

    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker"
        exit 1
    fi

    # Detect OnionHeaven host (needed for cleanup routing)
    detect_onionheaven_addr
    local content_addr
    content_addr=$(docker_cmd exec onionpress-tor \
        cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null) || true
    if [ -n "$content_addr" ] && [ "$content_addr" = "$ONIONHEAVEN_ADDR" ]; then
        IS_ONIONHEAVEN_HOST=true
    else
        IS_ONIONHEAVEN_HOST=false
    fi

    # Remove all stress containers
    docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' | while read -r ctr; do
        docker_cmd rm -f "$ctr" 2>/dev/null || true
        log "Removed container: $ctr"
    done || true

    # Also remove old-style container
    docker_cmd rm -f stress-worker-tor 2>/dev/null || true

    # Build signed unregister payloads from local worker-info files.
    # Search both OUTPUT_DIR and all run-* subdirectories, dedup by address.
    local payloads
    payloads=$(python3 -c "
import json, glob, sys, base64, os
sys.path.insert(0, '${SCRIPT_DIR}/../src')
from onion_auth import sign_payload, make_timestamp
# Collect from all run dirs, newest first, dedup by content_address
seen = set()
all_files = sorted(glob.glob('${OUTPUT_DIR}/worker-*-info.json'))
all_files += sorted(glob.glob('${OUTPUT_DIR}/run-*/worker-*-info.json'), key=os.path.getmtime, reverse=True)
for f in all_files:
    try:
        workers = json.load(open(f))
        for w in workers:
            ca = w.get('content_address', '')
            ha = w.get('healthcheck_address', '')
            pk = w.get('privkey_b64', '')
            pub = w.get('pubkey_b64', '')
            if ca and pk and pub and ca not in seen:
                seen.add(ca)
                privkey = base64.b64decode(pk)
                pubkey = base64.b64decode(pub)
                ts = make_timestamp()
                sig = sign_payload(privkey, pubkey, 'unregister', ca, ha, ts)
                print(json.dumps({'content_address': ca, 'healthcheck_address': ha, 'timestamp': ts, 'signature': sig}))
    except: pass
print(f'Found {len(seen)} unique addresses across all runs', file=sys.stderr)
" 2>/dev/null) || true

    local count
    count=$(echo "$payloads" | grep -c 'content_address' 2>/dev/null || true)
    [ -z "$count" ] && count=0
    log "Found ${count} stress-test entries to clean up"

    if [ "$count" -eq 0 ]; then
        log "Nothing to clean up"
        return
    fi

    # Unregister entries in parallel — each /unregister goes over Tor so sequential is slow
    local tmpdir
    tmpdir=$(mktemp -d)
    local pids=""
    local job_count=0

    while IFS= read -r payload; do
        [ -z "$payload" ] && continue

        (
            docker_cmd exec onionpress-tor \
                curl -s --socks5-hostname "cleanup${job_count}:x@127.0.0.1:9050" --max-time 30 \
                -X POST "http://${ONIONHEAVEN_ADDR}:8083/unregister" \
                -H "Content-Type: application/json" \
                -d "$payload" 2>/dev/null || true
            echo "done" > "${tmpdir}/job_${job_count}"
        ) &
        pids="$pids $!"
        job_count=$((job_count + 1))
    done <<< "$payloads"

    # Wait for all unregister jobs
    local failed=0
    for pid in $pids; do
        wait "$pid" 2>/dev/null || failed=$((failed + 1))
    done

    local succeeded
    succeeded=$(ls "$tmpdir" 2>/dev/null | wc -l | tr -d ' ')
    rm -rf "$tmpdir"

    log "Unregistered ${succeeded}/${job_count} entries (parallel)"

    echo ""
    log "Cleanup complete: ${count} stress-test entries removed"
}

# ── Stale cleanup (only removes tests with no activity in N hours) ───────────
run_cleanup_stale() {
    log "=== OnionHeaven Stale Stress Test Cleanup (>${STALE_HOURS}h inactive) ==="

    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker"
        exit 1
    fi

    # Must be running on the OnionHeaven host (need DB access)
    # NOTE: Stale stress-test entries are also auto-cleaned by the heartbeat
    # monitor after 2 hours taken-over. This command is for manual/immediate cleanup.
    detect_onionheaven_addr
    local content_addr
    content_addr=$(docker_cmd exec onionpress-tor \
        cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null) || true
    if [ -z "$content_addr" ] || [ "$content_addr" != "$ONIONHEAVEN_ADDR" ]; then
        echo "ERROR: --cleanup-stale must run on the OnionHeaven host (need DB access)"
        echo "NOTE:  Stale stress-test entries are auto-cleaned by the heartbeat monitor after 2h."
        echo "       No manual cleanup needed from remote machines."
        exit 1
    fi
    IS_ONIONHEAVEN_HOST=true

    # Find stress test versions with no activity in STALE_HOURS
    local stale_versions
    stale_versions=$(docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
        "SELECT version, COUNT(*), MAX(last_healthy) FROM registry
         WHERE unregistered_at IS NULL
           AND version LIKE 'stress-test%'
           AND last_healthy < datetime('now', '-${STALE_HOURS} hours')
         GROUP BY version;" 2>/dev/null) || true

    if [ -z "$stale_versions" ]; then
        log "No stale stress tests found (all have activity within ${STALE_HOURS}h)"

        # Show active stress tests for info
        local active
        active=$(docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
            "SELECT version, COUNT(*), MAX(last_healthy) FROM registry
             WHERE unregistered_at IS NULL AND version LIKE 'stress-test%'
             GROUP BY version;" 2>/dev/null) || true
        if [ -n "$active" ]; then
            log "Active stress tests:"
            echo "$active" | while IFS='|' read -r ver cnt last; do
                log "  ${ver}: ${cnt} entries, last activity ${last}"
            done
        fi
        return
    fi

    log "Stale stress tests to clean up:"
    echo "$stale_versions" | while IFS='|' read -r ver cnt last; do
        log "  ${ver}: ${cnt} entries, last activity ${last}"
    done

    # Mark stale entries as unregistered directly in DB
    local total_cleaned=0
    while IFS='|' read -r ver cnt last; do
        [ -z "$ver" ] && continue
        local now
        now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        docker_cmd exec onionheaven sqlite3 "$ONIONHEAVEN_DB_PATH" \
            "UPDATE registry SET unregistered_at = '${now}',
                                 unregistered_reason = 'stale-cleanup',
                                 status = 'unregistered'
             WHERE version = '${ver}'
               AND unregistered_at IS NULL
               AND last_healthy < datetime('now', '-${STALE_HOURS} hours');" 2>/dev/null || true
        log "  Cleaned up ${cnt} entries from ${ver}"
        total_cleaned=$((total_cleaned + cnt))
    done <<< "$stale_versions"

    # Also remove any local stress containers that aren't running a test
    local stale_containers
    stale_containers=$(docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' || true)
    if [ -n "$stale_containers" ]; then
        # Check if any container has had recent bootstrap activity
        local removed=0
        while read -r ctr; do
            [ -z "$ctr" ] && continue
            # If worker-info.json exists and bootstrap isn't running, container is done
            local has_info
            has_info=$(docker_cmd exec "$ctr" sh -c 'test -f /worker-info.json && echo yes || echo no' 2>/dev/null) || has_info="unknown"
            local has_bootstrap
            has_bootstrap=$(docker_cmd exec "$ctr" sh -c 'ps aux 2>/dev/null | grep -c "[w]orker-bootstrap"' 2>/dev/null) || has_bootstrap="0"
            local has_heartbeat
            has_heartbeat=$(docker_cmd exec "$ctr" sh -c 'ps aux 2>/dev/null | grep -c "[h]eartbeat_loop"' 2>/dev/null) || has_heartbeat="0"

            if [ "$has_info" = "yes" ] && [ "$has_bootstrap" = "0" ] && [ "$has_heartbeat" = "0" ]; then
                docker_cmd rm -f "$ctr" 2>/dev/null || true
                removed=$((removed + 1))
                log "  Removed idle container: ${ctr}"
            else
                log "  Keeping active container: ${ctr} (info=${has_info} bootstrap=${has_bootstrap} heartbeat=${has_heartbeat})"
            fi
        done <<< "$stale_containers"
        [ "$removed" -gt 0 ] && log "Removed ${removed} idle containers"
    fi

    log "Stale cleanup complete: ${total_cleaned} entries cleaned"
}

# ── Main dispatch ─────────────────────────────────────────────────────────────
if [ "$CLEANUP_STALE" = true ]; then
    run_cleanup_stale
    exit 0
fi
if [ "$CLEANUP" = true ]; then
    run_cleanup
    exit 0
fi

case "$MODE" in
    worker)      run_worker ;;
    coordinator) run_coordinator ;;
    *)
        echo "Unknown mode: $MODE (use 'worker' or 'coordinator')"
        exit 1
        ;;
esac
