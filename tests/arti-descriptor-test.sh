#!/bin/bash
# Arti Descriptor Propagation Test
#
# Reproduces an issue where ~10-20% of Arti-published onion service
# descriptors never become reachable from a separate Tor client.
#
# Usage:  ./arti-descriptor-test.sh [NUM_SERVICES]  (default: 20)
#
# Requires: Docker (or Podman with `alias docker=podman`)
# Image:    containers.torproject.org/tpo/onion-services/onimages/arti:alpine
#
# 2026-09-24, Arti 2.6.0 (arm64, isolated Colima VM): 17/20 reachable on the
# first pass at t+2min, 20/20 within 31s of retries — with 980 failed HsDir
# upload attempts and 19 "Too many preemptive onion service circuits failed"
# warnings in the publisher log on the way there (upstream arti#2621).
#
# Three things had to change for that run to happen at all:
#   * the publisher now runs one busybox `nc` HTTP responder per service.
#     The probe counts an HTTP status, so without a backend every probe was
#     "connection refused" at the service and the script could only ever
#     report 0/N, whatever Tor did;
#   * Arti 2.6.0 refuses a log file whose parent directory is o+rx, and the
#     alpine image ships /home/arti as 755, so `arti proxy` exited at once
#     and "Publisher did not bootstrap" was the only possible outcome;
#   * `declare -A` needs bash 4; macOS ships 3.2. STATUS is indexed by the
#     service number, so an ordinary array does.

set -euo pipefail

NUM_SERVICES="${1:-20}"
ARTI_IMAGE="containers.torproject.org/tpo/onion-services/onimages/arti:alpine"
NETWORK="arti-test-net"
PUB="arti-test-pub"
CLI="arti-test-cli"
PROBE_TIMEOUT=15
BASE_PORT=9100

log() { echo "[$(date +%H:%M:%S)] $*"; }

cleanup() {
    log "Cleaning up..."
    docker rm -f "$PUB" "$CLI" 2>/dev/null || true
    docker network rm "$NETWORK" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

log "=== Arti Descriptor Propagation Test ==="
log "Services: $NUM_SERVICES  Image: $ARTI_IMAGE"

WORK=$(mktemp -d)

# ── Configs ─────────────────────────────────────────────────────────────────

cat > "$WORK/pub.toml" << 'EOF'
[proxy]
socks_listen = "127.0.0.1:9050"
[storage]
cache_dir = "/home/arti/.local/share/arti/cache"
state_dir = "/home/arti/.local/share/arti/state"
[storage.keystore]
enabled = true
[logging]
console = "info,tor_hsservice=debug"
[[logging.files]]
path = "/home/arti/arti.log"
filter = "info,tor_hsservice=debug,tor_circmgr=debug"
EOF

for i in $(seq 0 $((NUM_SERVICES - 1))); do
    cat >> "$WORK/pub.toml" << EOF
[onion_services."svc${i}"]
enabled = true
proxy_ports = [["80", "127.0.0.1:$((BASE_PORT + i))"]]
EOF
done

cat > "$WORK/cli.toml" << 'EOF'
[proxy]
socks_listen = "0.0.0.0:9050"
[storage]
cache_dir = "/home/arti/.local/share/arti/cache"
state_dir = "/home/arti/.local/share/arti/state"
[logging]
console = "info"
EOF

# ── Start containers ────────────────────────────────────────────────────────

docker network rm "$NETWORK" 2>/dev/null || true
docker network create "$NETWORK" > /dev/null

start_arti() {
    local name="$1" conf="$2"
    docker run -d --name "$name" --network "$NETWORK" \
        --entrypoint sleep "$ARTI_IMAGE" infinity > /dev/null
    docker cp "$conf" "$name:/tmp/arti.toml"
    docker exec --user root "$name" sh -c '
        mv /tmp/arti.toml /home/arti/arti.toml
        chown arti:arti /home/arti/arti.toml
        mkdir -p /home/arti/.local/share/arti/cache /home/arti/.local/share/arti/state
        chown -R arti:arti /home/arti/.local
        chmod 700 /home/arti /home/arti/.local/share/arti/state /home/arti/.local/share/arti/cache
    '
    docker exec -d "$name" arti proxy -c /home/arti/arti.toml
}

# One HTTP responder per service inside the publisher. The alpine image has
# busybox nc and nothing else, and one `nc -l` serves exactly one connection,
# hence the loop.
start_backends() {
    local i
    for i in $(seq 0 $((NUM_SERVICES - 1))); do
        docker exec -d --user root "$PUB" sh -c \
            "while true; do printf 'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok' | nc -l -p $((BASE_PORT + i)) >/dev/null 2>&1; done"
    done
}

log "Starting publisher ($NUM_SERVICES services)..."
start_arti "$PUB" "$WORK/pub.toml"
start_backends

log "Starting client..."
start_arti "$CLI" "$WORK/cli.toml"

# Install curl in client (for SOCKS probing — Alpine base has wget but no SOCKS support)
log "Installing curl in client..."
docker exec --user root "$CLI" apk add --quiet curl > /dev/null 2>&1

# ── Bootstrap ───────────────────────────────────────────────────────────────

log "Waiting for publisher to bootstrap (may take a few minutes)..."
for i in $(seq 1 60); do
    addr=$(docker exec "$PUB" \
        arti hss --nickname svc0 onion-address -c /home/arti/arti.toml \
        2>/dev/null | tr -d '[:space:]') || true
    if [ -n "$addr" ] && echo "$addr" | grep -q ".onion"; then
        log "  Publisher ready ($((i * 5))s)"
        break
    fi
    [ "$i" -eq 60 ] && { log "ERROR: Publisher did not bootstrap after 300s"; exit 1; }
    sleep 5
done

log "Waiting for client to bootstrap..."
for i in $(seq 1 60); do
    if docker exec "$CLI" curl -sf -o /dev/null \
        --socks5-hostname 127.0.0.1:9050 --max-time 8 \
        http://example.com/ 2>/dev/null; then
        log "  Client ready ($((i * 5))s)"
        break
    fi
    [ "$i" -eq 60 ] && log "WARNING: Client may not be ready"
    sleep 5
done

# ── Collect .onion addresses ────────────────────────────────────────────────

log "Collecting addresses..."
declare -a ADDRS
for i in $(seq 0 $((NUM_SERVICES - 1))); do
    for try in $(seq 1 30); do
        a=$(docker exec "$PUB" \
            arti hss --nickname svc${i} onion-address -c /home/arti/arti.toml \
            2>/dev/null | tr -d '[:space:]') || true
        if [ -n "$a" ] && echo "$a" | grep -q ".onion"; then
            ADDRS[$i]="$a"
            break
        fi
        sleep 2
    done
    [ -z "${ADDRS[$i]:-}" ] && { log "  FAILED: svc${i}"; ADDRS[$i]="FAILED"; }
done
log "  Got ${#ADDRS[@]} addresses"

# ── Wait for descriptor propagation ────────────────────────────────────────

log "Waiting 120s for descriptor propagation..."
sleep 120

# ── Probe ───────────────────────────────────────────────────────────────────

probe() {
    local addr="$1" idx="$2"
    local code
    code=$(docker exec "$CLI" curl -s -o /dev/null -w '%{http_code}' \
        --socks5-hostname "p${idx}:x@127.0.0.1:9050" \
        --max-time "$PROBE_TIMEOUT" "http://${addr}/" 2>/dev/null) || true
    [ -n "$code" ] && [ "$code" != "000" ]
}

log "Probing ${NUM_SERVICES} addresses from client container..."
reachable=0
unreachable=0
declare -a STATUS   # indexed by service number; bash 3.2 has no -A

for i in $(seq 0 $((NUM_SERVICES - 1))); do
    [ "${ADDRS[$i]}" = "FAILED" ] && continue
    if probe "${ADDRS[$i]}" "$i"; then
        STATUS[$i]="ok"
        reachable=$((reachable + 1))
    else
        STATUS[$i]="fail"
        unreachable=$((unreachable + 1))
    fi
done
log "  First pass: ${reachable}/${NUM_SERVICES} reachable, ${unreachable} unreachable"

# Retry unreachable for 5 minutes
if [ "$unreachable" -gt 0 ]; then
    log "Retrying unreachable addresses for 300s..."
    t0=$(date +%s)
    while [ $(( $(date +%s) - t0 )) -lt 300 ]; do
        left=0
        for i in $(seq 0 $((NUM_SERVICES - 1))); do
            [ "${STATUS[$i]:-skip}" != "fail" ] && continue
            if probe "${ADDRS[$i]}" "$i"; then
                STATUS[$i]="ok"
                reachable=$((reachable + 1))
                unreachable=$((unreachable - 1))
                log "  svc${i} reachable after $(( $(date +%s) - t0 ))s"
            else
                left=$((left + 1))
            fi
        done
        [ "$left" -eq 0 ] && { log "  All reachable!"; break; }
        log "  $(( $(date +%s) - t0 ))s: ${left} still unreachable"
        sleep 15
    done
fi

# ── Results ─────────────────────────────────────────────────────────────────

echo ""
log "=== RESULTS ==="
log "Total: ${NUM_SERVICES}  Reachable: ${reachable}  Unreachable: ${unreachable}"
[ "$NUM_SERVICES" -gt 0 ] && log "Failure rate: $(( unreachable * 100 / NUM_SERVICES ))%"

if [ "$unreachable" -gt 0 ]; then
    echo ""
    log "Unreachable services:"
    for i in $(seq 0 $((NUM_SERVICES - 1))); do
        [ "${STATUS[$i]:-}" = "fail" ] && log "  svc${i}: ${ADDRS[$i]}"
    done
    echo ""
    log "Publisher Arti log (last 50 lines):"
    docker exec "$PUB" cat /home/arti/arti.log 2>/dev/null | tail -50 || true
fi

echo ""
log "Arti version:"
docker exec "$PUB" arti --version 2>/dev/null || true
log "Done."
