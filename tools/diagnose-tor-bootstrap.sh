#!/usr/bin/env bash
# Diagnose Tor bootstrap success/failure for a given transport config, in isolation.
#
# Spins up a throwaway onionpress-tor container in NO_ONION_SERVICE=1 mode (SOCKS-only,
# no WordPress dependency), using whatever TOR_IMPL / TOR_BRIDGE_LINES /
# TOR_CLIENT_TRANSPORT_PLUGIN are exported in the calling environment — the same three
# vars entrypoint.sh itself reads in production. Waits for a bootstrap signal or timeout,
# then reports pass/fail, elapsed time, and a log tail. Exists so transport configs
# (no-bridge / snowflake / obfs4) can be compared the same way every time, on any host,
# without hand-watching `docker logs -f`.
#
# Usage:
#   TOR_IMPL=arti TOR_CLIENT_TRANSPORT_PLUGIN=snowflake TOR_BRIDGE_LINES="bridge1;bridge2" \
#     ./diagnose-tor-bootstrap.sh <run-label> [timeout-seconds]
#
# With TOR_CLIENT_TRANSPORT_PLUGIN=snowflake and no TOR_BRIDGE_LINES set, falls back to
# the default snowflake bridge below — last confirmed against a live Tor Browser on
# 2026-08-08. Tor Project rotates these; if this harness starts failing where a fresh
# Tor Browser succeeds, that default is the first thing to re-check, not the image.
#
# There is no equivalent built-in fallback for obfs4: Tor Project deliberately
# CAPTCHA-gates and rate-limits obfs4 bridge distribution to keep it out of automated
# reach, so a real obfs4 line has to come from a human (https://bridges.torproject.org
# or a Tor Browser's own bridge settings) and be passed in via TOR_BRIDGE_LINES.

set -euo pipefail

RUN_LABEL="${1:?usage: $0 <run-label> [timeout-seconds]}"
TIMEOUT="${2:-300}"
IMAGE="${DIAGNOSE_TOR_IMAGE:-ghcr.io/brewsterkahle/onionpress-tor:latest}"
CONTAINER="diag-tor-${RUN_LABEL}"
DOCKER="${DOCKER_CMD:-docker}"

DEFAULT_SNOWFLAKE_BRIDGE='snowflake 192.0.2.3:80 2B280B23E1107BB62ABFC40DDCC8824814F80A72 fingerprint=2B280B23E1107BB62ABFC40DDCC8824814F80A72 url=https://1098762253.rsc.cdn77.org/ fronts=app.datapacket.com,www.datapacket.com ice=stun:stun.epygi.com:3478,stun:stun.uls.co.za:3478,stun:stun.voipgate.com:3478,stun:stun.mixvoip.com:3478,stun:stun.telnyx.com:3478,stun:stun.hot-chilli.net:3478,stun:stun.fitauto.ru:3478,stun:stun.m-online.net:3478 utls-imitate=hellorandomizedalpn max=5'

: "${TOR_IMPL:=arti}"
: "${TOR_CLIENT_TRANSPORT_PLUGIN:=}"
if [ -z "${TOR_BRIDGE_LINES:-}" ] && [ "$TOR_CLIENT_TRANSPORT_PLUGIN" = "snowflake" ]; then
    TOR_BRIDGE_LINES="$DEFAULT_SNOWFLAKE_BRIDGE"
fi

$DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true

$DOCKER run -d --name "$CONTAINER" \
    -e NO_ONION_SERVICE=1 \
    -e TOR_IMPL="$TOR_IMPL" \
    -e TOR_BRIDGE_LINES="${TOR_BRIDGE_LINES:-}" \
    -e TOR_CLIENT_TRANSPORT_PLUGIN="$TOR_CLIENT_TRANSPORT_PLUGIN" \
    "$IMAGE" >/dev/null

echo "started $CONTAINER (impl=$TOR_IMPL transport=${TOR_CLIENT_TRANSPORT_PLUGIN:-none} timeout=${TIMEOUT}s)"

START=$(date +%s)
RESULT="timeout"
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
        break
    fi
    if ! $DOCKER inspect "$CONTAINER" >/dev/null 2>&1; then
        RESULT="container-exited"
        break
    fi
    if $DOCKER logs "$CONTAINER" 2>&1 | grep -qE "Bootstrapped 100%|100% bootstrapped|Sufficiently bootstrapped"; then
        RESULT="bootstrapped"
        break
    fi
    sleep 5
done

END=$(date +%s)
ELAPSED=$((END - START))

echo "=== result: $RESULT (${ELAPSED}s) ==="
echo "=== last 40 log lines ==="
$DOCKER logs "$CONTAINER" 2>&1 | tail -40

$DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true

[ "$RESULT" = "bootstrapped" ]
