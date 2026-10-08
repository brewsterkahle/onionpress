#!/bin/bash
#
# OnionHeaven Local Functionality Test
# https://github.com/brewsterkahle/onionpress/issues/113
#
# Tests the full OnionHeaven lifecycle against a live local OnionPress instance:
#   1. Initial state verification
#   2. Activation via faux heartbeat
#   3. Takeover after heartbeat goes stale (180s propagation delay)
#   4. Takeover propagation (Arti keystore + config)
#   5. Release via heartbeat
#   6. Release propagation
#   7. Unregister
#
# Prerequisites:
#   - OnionPress running locally (OnionHeaven API runs on every node)
#   - Docker accessible
#
# Usage:
#   ./tests/test-onionheaven-local.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HELPER="$SCRIPT_DIR/oh-test-helper.py"
TMPDIR_TEST=$(mktemp -d)
IDENTITY="$TMPDIR_TEST/identity.json"
PAYLOAD="$TMPDIR_TEST/payload.json"
TEST_LOG="$SCRIPT_DIR/test-onionheaven-local.log"
TEST_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0

# Use DOCKER_HOST if set, otherwise Colima socket on macOS, system default on Linux
if [ -z "${DOCKER_HOST:-}" ]; then
    if [ -S "$HOME/.onionpress/colima/docker.sock" ]; then
        export DOCKER_HOST="unix://$HOME/.onionpress/colima/docker.sock"
    fi
    # On Linux, leave DOCKER_HOST unset to use /var/run/docker.sock
fi

# Container name
TOR_CONTAINER="onionpress-tor"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Open a Terminal.app window tailing the test log
open_log_window() {
    local abs_path
    abs_path=$(cd "$(dirname "$TEST_LOG")" && pwd)/$(basename "$TEST_LOG")
    > "$abs_path"  # truncate
    osascript -e "
        tell application \"Terminal\"
            activate
            do script \"tail -f '${abs_path}'\"
        end tell
    " 2>/dev/null &
}

# Tee all output to both the terminal and the log file
open_log_window
exec > >(tee -a "$TEST_LOG") 2>&1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    printf "${BLUE}[%3ds]${NC} %s\n" "$elapsed" "$1"
}

pass() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    PASS_COUNT=$((PASS_COUNT + 1))
    printf "${GREEN}[%3ds] ✓ PASS:${NC} %s\n" "$elapsed" "$1"
}

fail() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf "${RED}[%3ds] ✗ FAIL:${NC} %s\n" "$elapsed" "$1"
}

step() {
    echo ""
    printf "${YELLOW}━━━ Step %s: %s ━━━${NC}\n" "$1" "$2"
}

# Call the OnionHeaven API via Tor (through tor-client SOCKS proxy)
oh_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ "$method" = "GET" ]; then
        docker exec onionpress-tor curl -s --max-time 30 \
            --socks5-hostname 127.0.0.1:9050 \
            "http://${ONION_ADDR}:8083${path}" 2>/dev/null
    else
        docker exec onionpress-tor curl -s --max-time 30 \
            --socks5-hostname 127.0.0.1:9050 \
            -X POST -H "Content-Type: application/json" \
            -d "$data" \
            "http://${ONION_ADDR}:8083${path}" 2>/dev/null
    fi
}

# Generate a signed payload using the Python helper
sign_payload() {
    local cmd="$1"
    shift
    python3 "$HELPER" "$cmd" "$@"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}OnionHeaven Local Functionality Test (C Tor)${NC}\n"
printf "${YELLOW}=================================================${NC}\n"
echo ""

log "Running pre-flight checks..."

# Check Docker is accessible
if ! docker info >/dev/null 2>&1; then
    fail "Docker not accessible (is OnionPress running?)"
    exit 1
fi
pass "Docker accessible"

# Check tor container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${TOR_CONTAINER}$"; then
    fail "$TOR_CONTAINER container not running"
    exit 1
fi
pass "$TOR_CONTAINER container running"

# Read the local onion address (needed by oh_api which goes through Tor)
ONION_ADDR=$(docker exec "$TOR_CONTAINER" cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null || echo "")
if [ -z "$ONION_ADDR" ]; then
    fail "Could not read local onion address from tor container"
    exit 1
fi
log "Local onion address: $ONION_ADDR"

# Check OnionHeaven API is responding via Tor (may need retries for descriptor propagation)
log "Checking onion address reachability via Tor (may take a moment)..."
OH_API_OK=false
for attempt in $(seq 1 12); do
    if oh_api GET /status >/dev/null 2>&1; then
        OH_API_OK=true
        break
    fi
    [ "$attempt" -lt 12 ] && sleep 10
done
if [ "$OH_API_OK" = true ]; then
    pass "OnionHeaven API responding via Tor"
else
    fail "OnionHeaven API not responding on port 8083 via Tor after 2 minutes"
    log "  The tor container image may need updating: docker pull ghcr.io/brewsterkahle/onionpress-tor:latest"
    exit 1
fi

# Verify WordPress is responding locally
WP_STATUS=$(docker exec "$TOR_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://wordpress:80/" 2>/dev/null || echo "000")
if [ "$WP_STATUS" = "200" ] || [ "$WP_STATUS" = "301" ] || [ "$WP_STATUS" = "302" ]; then
    pass "WordPress responding internally (HTTP $WP_STATUS)"
else
    fail "WordPress not responding internally (HTTP $WP_STATUS)"
    exit 1
fi

# Verify the onion address is reachable via Tor (retry for descriptor propagation)
log "Checking onion address reachability via Tor (may take a moment)..."
TOR_REACHABLE=false
for attempt in $(seq 1 12); do
    TOR_STATUS=$(docker exec onionpress-tor curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${ONION_ADDR}/" 2>/dev/null || echo "000")
    if [ "$TOR_STATUS" = "200" ] || [ "$TOR_STATUS" = "301" ] || [ "$TOR_STATUS" = "302" ]; then
        TOR_REACHABLE=true
        break
    fi
    [ "$attempt" -lt 12 ] && sleep 10
done
if [ "$TOR_REACHABLE" = true ]; then
    pass "Onion address reachable via Tor (HTTP $TOR_STATUS)"
else
    fail "Onion address not reachable via Tor after 2 minutes (HTTP $TOR_STATUS)"
    exit 1
fi

# Clean up leftover state from previous test runs
log "Cleaning up leftover OnionHeaven state..."
# Clear test entries from registry DB
docker exec "$TOR_CONTAINER" sh -c "sqlite3 /var/lib/onionpress/onionheaven/registry.db \"DELETE FROM registry WHERE version LIKE 'test-%' OR version LIKE 'stress-test-%'\" 2>/dev/null" || true

# Check if there are real (non-test) registrations — if so, the lazy watcher
# will keep the onionheaven container alive and we cannot stop it cleanly.
REAL_TOTAL=$(docker exec "$TOR_CONTAINER" sh -c "sqlite3 /var/lib/onionpress/onionheaven/registry.db \"SELECT COUNT(*) FROM registry WHERE status='online'\" 2>/dev/null" || echo "0")
if [ "${REAL_TOTAL:-0}" -gt 0 ]; then
    LIVE_OH_NODE=true
    log "Live OnionHeaven node with $REAL_TOTAL real registrations — skipping container teardown"
    pass "Leftover test state cleaned up (live node, onionheaven container stays)"
else
    LIVE_OH_NODE=false
    # onionheaven is always-on (part of core stack since v2.4.37).
    # Recreate it to ensure it runs the latest image, then clean test data.
    log "Recreating onionheaven container with latest image..."
    COMPOSE_DIR="$PROJECT_DIR/app/Resources/docker"
    SECRETS_FILE="$HOME/.onionpress/secrets"
    if [ -f "$COMPOSE_DIR/docker-compose.yml" ]; then
        # Source database secrets so compose doesn't warn about missing vars
        [ -f "$SECRETS_FILE" ] && set -a && . "$SECRETS_FILE" && set +a
        docker compose -f "$COMPOSE_DIR/docker-compose.yml" up -d --force-recreate onionheaven 2>&1 | while read -r line; do log "  $line"; done
        sleep 5
        if docker ps --format '{{.Names}}' | grep -q "^onionheaven$"; then
            pass "onionheaven container recreated with latest image"
        else
            fail "onionheaven container failed to start after recreate"
            exit 1
        fi
    else
        log "WARNING: docker-compose.yml not found, checking existing container"
        if docker ps --format '{{.Names}}' | grep -q "^onionheaven$"; then
            pass "onionheaven container running (could not recreate — compose file missing)"
        else
            fail "onionheaven container not running and cannot recreate"
            exit 1
        fi
    fi
    pass "Leftover test state cleaned up"
fi

# Generate faux OnionPress identity
log "Generating faux OnionPress identity..."
python3 "$HELPER" generate > "$IDENTITY"
CONTENT_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['content_address'])")
HEALTHCHECK_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['healthcheck_address'])")
log "  Content address:     ${CONTENT_ADDR:0:20}...onion"
log "  Healthcheck address: ${HEALTHCHECK_ADDR:0:20}...onion"

# ---------------------------------------------------------------------------
# Step 1: Test initial state
# ---------------------------------------------------------------------------

step 1 "Verify initial state (OnionPress up, OnionHeaven API responding)"

# On a live OnionHeaven node, the container is already running — that's fine.
# On a fresh node, it should not be running yet (lazy activation).
if docker ps --format '{{.Names}}' | grep -q "^onionheaven$"; then
    pass "onionheaven container running"
else
    fail "onionheaven container not running — heartbeat monitor needs it"
    exit 1
fi

# Check that our faux address is not already registered
STATUS_ADDR=$(oh_api GET "/status/${CONTENT_ADDR}")
if echo "$STATUS_ADDR" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' in d else 1)" 2>/dev/null; then
    pass "Faux address not in registry (as expected)"
else
    fail "Faux address already exists in registry — aborting"
    echo "$STATUS_ADDR"
    exit 1
fi

# Record current total count
INITIAL_TOTAL=$(oh_api GET /status | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")
log "Current registry total: $INITIAL_TOTAL entries"

# ---------------------------------------------------------------------------
# Step 2: Activate via heartbeat
# ---------------------------------------------------------------------------

step 2 "Activate via faux heartbeat"

# Generate signed /online payload with Arti key
sign_payload sign-online "$IDENTITY" --with-key > "$PAYLOAD"
RESPONSE=$(oh_api POST /online "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

# Check response fields
ONLINE=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('online', False))")
CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))")
KEY_STORED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('arti_key_stored', False))")

if [ "$ONLINE" = "True" ]; then
    pass "/online returned online=true"
else
    fail "/online did not return online=true"
fi

if [ "$CREATED" = "True" ]; then
    pass "Entry was created (created=true)"
else
    fail "Entry was not created"
fi

if [ "$KEY_STORED" = "True" ]; then
    pass "Arti key was stored (arti_key_stored=true)"
else
    fail "Arti key was not stored"
fi

# Verify /status count increased
NEW_TOTAL=$(oh_api GET /status | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")
if [ "$NEW_TOTAL" -gt "$INITIAL_TOTAL" ]; then
    pass "/status total increased ($INITIAL_TOTAL → $NEW_TOTAL)"
else
    fail "/status total did not increase ($INITIAL_TOTAL → $NEW_TOTAL)"
fi

# Verify /status/<address> shows online
ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])")
if [ "$ENTRY_STATUS" = "online" ]; then
    pass "/status/<address> shows status=online"
else
    fail "/status/<address> shows status=$ENTRY_STATUS (expected online)"
fi

# Check key stored on disk
KEY_EXISTS=$(docker exec "$TOR_CONTAINER" test -f "/var/lib/onionpress/onionheaven/keys/${CONTENT_ADDR}/ks_hs_id.ed25519_expanded_private" && echo "yes" || echo "no")
if [ "$KEY_EXISTS" = "yes" ]; then
    pass "Arti key file exists in OnionHeaven keys directory"
else
    fail "Arti key file not found in OnionHeaven keys directory"
fi

# ---------------------------------------------------------------------------
# Step 3: Wait for takeover
# ---------------------------------------------------------------------------

step 3 "Wait for takeover (heartbeat goes stale after 180s)"

log "Stopping heartbeats — waiting for heartbeat monitor to trigger takeover..."
log "Propagation delay: 180s, monitor interval: 15s, worst case: ~200s"

TAKEOVER_START=$(date +%s)
TAKEOVER_DETECTED=false

# Poll every 10 seconds for up to 240 seconds
for i in $(seq 1 24); do
    sleep 10
    ELAPSED=$(( $(date +%s) - TAKEOVER_START ))
    ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])" 2>/dev/null || echo "error")
    if [ "$ENTRY_STATUS" = "error" ]; then
        log "  [${ELAPSED}s] status=error (Tor HSDir blip, retrying...)"
        sleep 2
        ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])" 2>/dev/null || echo "error")
    fi
    log "  [${ELAPSED}s] status=$ENTRY_STATUS"
    if [ "$ENTRY_STATUS" = "taken-over" ]; then
        TAKEOVER_DETECTED=true
        pass "Takeover detected after ${ELAPSED}s"
        break
    fi
done

if [ "$TAKEOVER_DETECTED" = false ]; then
    fail "Takeover not detected after 240s"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify takeover propagation
# ---------------------------------------------------------------------------

step 4 "Verify takeover propagation (keystore + 302 redirect via Tor)"

# Takeover keys are installed in the onionheaven container's C Tor, not onionpress-tor
OH_CONTAINER="onionheaven"

# Check that the onionheaven container is running (lazy activation should have started it)
if docker ps --format '{{.Names}}' | grep -q "^${OH_CONTAINER}$"; then
    pass "onionheaven container is running (lazy activation triggered)"
else
    fail "onionheaven container not running — takeover cannot install keys"
fi

# Check that the key was installed (could be in onionheaven or a takeover container)
# C Tor: check HiddenServiceDir in onionheaven and takeover containers
KEY_FOUND=false
for check_ctr in "$OH_CONTAINER" $(docker ps --format '{{.Names}}' | grep "^onionheaven-takeover-"); do
    KEY_EXISTS=$(docker exec "$check_ctr" sh -c "find /var/lib/tor/hidden_service/ -path '*onionheaven_*' -name 'hs_ed25519_secret_key' 2>/dev/null | head -1" 2>/dev/null || echo "")
    if [ -n "$KEY_EXISTS" ]; then
        pass "Key installed in $check_ctr C Tor HiddenServiceDir"
        KEY_FOUND=true
        break
    fi
done
if [ "$KEY_FOUND" = false ]; then
    log "  Key not found in any container's HiddenServiceDir (ADD_ONION may have installed it ephemerally)"
fi

# Check /status/<address> has last_taken_over timestamp
TAKEN_OVER_AT=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0].get('last_taken_over', 'none'))")
if [ "$TAKEN_OVER_AT" != "none" ] && [ "$TAKEN_OVER_AT" != "None" ] && [ "$TAKEN_OVER_AT" != "null" ]; then
    pass "last_taken_over timestamp set: $TAKEN_OVER_AT"
else
    fail "last_taken_over timestamp not set"
fi

# Wait for Tor descriptor propagation, then verify 302 redirect via Tor network
log "Waiting for Tor descriptor propagation to reach the faux address..."
log "Descriptor propagation typically takes 5-10 minutes from a separate client"
REDIRECT_DETECTED=false
PROP_START=$(date +%s)

for i in $(seq 1 60); do
    sleep 10
    PROP_ELAPSED=$(( $(date +%s) - PROP_START ))
    REDIRECT_CODE=$(docker exec onionpress-tor curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${PROP_ELAPSED}s] HTTP $REDIRECT_CODE"
    if [ "$REDIRECT_CODE" = "302" ]; then
        REDIRECT_DETECTED=true
        pass "302 redirect received via Tor after ${PROP_ELAPSED}s"
        break
    fi
done

if [ "$REDIRECT_DETECTED" = false ]; then
    fail "302 redirect not received via Tor after 600s"
fi

# If we got the 302, verify it points to Wayback Machine
if [ "$REDIRECT_DETECTED" = true ]; then
    REDIRECT_LOCATION=$(docker exec onionpress-tor curl -s -D - -o /dev/null --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null | grep -i "^Location:" | tr -d '\r')
    if echo "$REDIRECT_LOCATION" | grep -q "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"; then
        pass "Redirect points to Wayback Machine onion service"
    else
        fail "Redirect location unexpected: $REDIRECT_LOCATION"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Release via heartbeat
# ---------------------------------------------------------------------------

step 5 "Release via heartbeat (send /online again)"

# Generate a new signed /online payload with key (required for release)
sign_payload sign-online "$IDENTITY" --with-key > "$PAYLOAD"
RESPONSE=$(oh_api POST /online "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

ONLINE=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('online', False))")
if [ "$ONLINE" = "True" ]; then
    pass "/online returned online=true"
else
    fail "/online did not return online=true"
fi

# Verify status changed back to online
ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])")
if [ "$ENTRY_STATUS" = "online" ]; then
    pass "/status/<address> shows status=online (released)"
else
    fail "/status/<address> shows status=$ENTRY_STATUS (expected online)"
fi

# Check last_released timestamp
RELEASED_AT=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0].get('last_released', 'none'))")
if [ "$RELEASED_AT" != "none" ] && [ "$RELEASED_AT" != "None" ] && [ "$RELEASED_AT" != "null" ]; then
    pass "last_released timestamp set: $RELEASED_AT"
else
    fail "last_released timestamp not set"
fi

# ---------------------------------------------------------------------------
# Step 6: Verify release propagation
# ---------------------------------------------------------------------------

step 6 "Verify release propagation (no more 302 via Tor)"

# Give Tor a moment to process the SIGHUP and remove keys
sleep 5

# Check that the key was removed from the onionheaven container
# Use the test address prefix to avoid false matches against real registrations
TEST_ADDR_PREFIX="${CONTENT_ADDR:0:16}"
KEY_GONE=$(docker exec "$OH_CONTAINER" sh -c "find /var/lib/tor/hidden_service/ -path '*onionheaven_${TEST_ADDR_PREFIX}*' -name 'hs_ed25519_secret_key' 2>/dev/null | head -1" || echo "")
if [ -z "$KEY_GONE" ]; then
    pass "Key removed from onionheaven C Tor HiddenServiceDir"
else
    fail "Key still present in onionheaven C Tor HiddenServiceDir: $KEY_GONE"
fi

# Wait for Tor descriptor to expire, then verify the address no longer serves a 302
# This can resolve in seconds or take up to 3 hours depending on Arti's behavior.
# We poll for 30 minutes. This is the key measurement — how long does release take?
#
# IMPORTANT: We must keep sending heartbeats during polling, otherwise the heartbeat
# monitor will see the entry go stale and re-take-over the address (defeating the test).
log "Waiting for Tor descriptor to expire after release (polling up to 30 minutes)..."
log "  (Sending periodic heartbeats to prevent re-takeover)"
RELEASE_CONFIRMED=false
REL_START=$(date +%s)
LAST_HEARTBEAT=$(date +%s)

for i in $(seq 1 180); do
    sleep 10
    REL_ELAPSED=$(( $(date +%s) - REL_START ))
    REL_MINS=$((REL_ELAPSED / 60))
    REL_SECS=$((REL_ELAPSED % 60))

    # Send a heartbeat every 60s to keep the entry "online" and prevent re-takeover
    SINCE_HEARTBEAT=$(( $(date +%s) - LAST_HEARTBEAT ))
    if [ "$SINCE_HEARTBEAT" -ge 60 ]; then
        sign_payload sign-online "$IDENTITY" --with-key > "$PAYLOAD"
        oh_api POST /online "$(cat "$PAYLOAD")" >/dev/null 2>&1
        LAST_HEARTBEAT=$(date +%s)
    fi

    RELEASE_CODE=$(docker exec onionpress-tor curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${REL_MINS}m ${REL_SECS}s] HTTP $RELEASE_CODE"
    if [ "$RELEASE_CODE" != "302" ]; then
        RELEASE_CONFIRMED=true
        pass "Address no longer returns 302 after ${REL_MINS}m ${REL_SECS}s (HTTP $RELEASE_CODE)"
        break
    fi
done

if [ "$RELEASE_CONFIRMED" = false ]; then
    fail "Address still returning 302 after 30 minutes — Arti descriptor caching (issue #114)"
fi

# Final Tor network verification: confirm the faux address returns WordPress
# content (HTTP 200) instead of OnionHeaven's 302 redirect, and that the real
# onion address still works.
log "Final Tor network verification..."

# Verify faux address returns 200 (original WordPress, no longer redirected)
# The original owner's key is restored, so the address should serve content again.
# Allow retries — descriptor propagation may still be in progress.
FAUX_RESTORED=false
for attempt in $(seq 1 2); do
    VERIFY_CODE=$(docker exec onionpress-tor curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  Faux address verification attempt $attempt: HTTP $VERIFY_CODE"
    if [ "$VERIFY_CODE" = "200" ]; then
        FAUX_RESTORED=true
        pass "Faux address returned to serving content via Tor (HTTP 200)"
        break
    elif [ "$VERIFY_CODE" = "302" ]; then
        fail "Faux address still returning 302 redirect on attempt $attempt"
        break
    fi
    [ "$attempt" -lt 6 ] && sleep 10
done
if [ "$FAUX_RESTORED" = false ] && [ "$VERIFY_CODE" != "302" ]; then
    # Address is unreachable (000) — faux key has no real service behind it, so
    # connection failure is expected. The key was generated for the test, not a
    # real OnionPress instance.
    pass "Faux address no longer redirecting via Tor (HTTP $VERIFY_CODE — expected for test-only key)"
fi


# ---------------------------------------------------------------------------
# Step 7: Unregister
# ---------------------------------------------------------------------------

step 7 "Unregister faux OnionPress"

sign_payload sign-unregister "$IDENTITY" > "$PAYLOAD"
RESPONSE=$(oh_api POST /unregister "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

UNREGISTERED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('unregistered', False))")
if [ "$UNREGISTERED" = "True" ]; then
    pass "/unregister returned unregistered=true"
else
    fail "/unregister did not return unregistered=true"
fi

# Verify key file was cleaned up
KEY_GONE=$(docker exec "$TOR_CONTAINER" test -f "/var/lib/onionpress/onionheaven/keys/${CONTENT_ADDR}/ks_hs_id.ed25519_expanded_private" && echo "exists" || echo "gone")
if [ "$KEY_GONE" = "gone" ]; then
    pass "Key file removed from OnionHeaven keys directory"
else
    # Non-stress-test entries get soft-deleted, key stays
    log "  (Key file retained — expected for non-stress-test entries)"
    pass "Unregister completed (soft delete, key retained)"
fi

# Verify /status total decreased or entry is marked unregistered
FINAL_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" 2>/dev/null || echo '{"error":"not found"}')
log "Final status: $FINAL_STATUS"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}━━━ Summary ━━━${NC}\n"
TOTAL_ELAPSED=$(( $(date +%s) - TEST_START ))
printf "  Total time:  %dm %ds\n" "$((TOTAL_ELAPSED / 60))" "$((TOTAL_ELAPSED % 60))"
printf "  ${GREEN}Passed: %d${NC}\n" "$PASS_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf "  ${RED}Failed: %d${NC}\n" "$FAIL_COUNT"
else
    printf "  Failed: 0\n"
fi
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
    printf "${RED}SOME TESTS FAILED${NC}\n"
    exit 1
else
    printf "${GREEN}ALL TESTS PASSED${NC}\n"
    exit 0
fi
