#!/bin/sh
#
# Unit test for apply_bridge_config() (the C Tor path) in
# app/Resources/docker/tor/entrypoint.sh.
#
# The function is extracted from the real entrypoint with the same awk
# technique used for the Arti test, then its hardcoded /etc/tor/torrc target is
# redirected to a temp file so the test needs no root and no running container.
# Extracting keeps this honest against the shipped code rather than a copy that
# can drift.
#
# Usage: sh tests/test-tor-bridge-config.sh

# entrypoint.sh does not `set -u`; apply_bridge_config() reads
# TOR_BRIDGE_LINES/TOR_CLIENT_TRANSPORT_PLUGIN unguarded (TOR_UPSTREAM_PROXY is
# guarded with :-). Match production rather than tightening past it.
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENTRYPOINT="$SCRIPT_DIR/../app/Resources/docker/tor/entrypoint.sh"
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
pass() { echo "PASS: $1"; }

# Extract the function and redirect its /etc/tor/torrc writes to a temp file.
TORRC="$TMPDIR_TEST/torrc"
FUNC_FILE="$TMPDIR_TEST/func.sh"
awk '/^apply_bridge_config\(\)/,/^}/' "$ENTRYPOINT" \
    | sed "s#/etc/tor/torrc#$TORRC#g" > "$FUNC_FILE"
if [ ! -s "$FUNC_FILE" ]; then
    echo "FAIL: could not extract apply_bridge_config() from $ENTRYPOINT"
    exit 1
fi
# shellcheck source=/dev/null
. "$FUNC_FILE"

reset_torrc() { : > "$TORRC"; }

# --- (a) no-op when TOR_BRIDGE_LINES is unset ---
reset_torrc
TOR_BRIDGE_LINES=""
unset TOR_CLIENT_TRANSPORT_PLUGIN TOR_UPSTREAM_PROXY 2>/dev/null || true
apply_bridge_config
if [ ! -s "$TORRC" ]; then
    pass "no-op when TOR_BRIDGE_LINES is unset"
else
    fail "wrote to torrc even though TOR_BRIDGE_LINES was unset"
fi

# --- (b) obfs4 bridge yields UseBridges + ClientTransportPlugin + Bridge lines ---
reset_torrc
TOR_BRIDGE_LINES="obfs4 192.0.2.1:443 FPRINT1 cert=AAAA iat-mode=0;Bridge obfs4 192.0.2.2:443 FPRINT2 cert=BBBB iat-mode=0"
TOR_CLIENT_TRANSPORT_PLUGIN="obfs4"
unset TOR_UPSTREAM_PROXY 2>/dev/null || true
apply_bridge_config
grep -q '^UseBridges 1$' "$TORRC" && pass "emits UseBridges 1" || fail "missing UseBridges 1"
grep -q '^ClientTransportPlugin obfs4 exec /usr/bin/obfs4proxy$' "$TORRC" \
    && pass "maps obfs4 to obfs4proxy" || fail "obfs4 ClientTransportPlugin wrong/missing"
[ "$(grep -c '^Bridge ' "$TORRC")" = "2" ] \
    && pass "writes both Bridge lines (dedupes the redundant 'Bridge ' prefix)" \
    || fail "expected 2 Bridge lines"

# --- (c) TOR_UPSTREAM_PROXY set -> emits Socks5Proxy with that value ---
reset_torrc
TOR_UPSTREAM_PROXY="172.19.0.1:15235"
apply_bridge_config
grep -q "^Socks5Proxy 172.19.0.1:15235$" "$TORRC" \
    && pass "emits Socks5Proxy when TOR_UPSTREAM_PROXY is set" \
    || fail "missing/incorrect Socks5Proxy line"

# --- (d) TOR_UPSTREAM_PROXY unset -> no Socks5Proxy line ---
reset_torrc
unset TOR_UPSTREAM_PROXY 2>/dev/null || true
apply_bridge_config
if grep -q '^Socks5Proxy' "$TORRC"; then
    fail "emitted Socks5Proxy even though TOR_UPSTREAM_PROXY was unset"
else
    pass "omits Socks5Proxy when TOR_UPSTREAM_PROXY is unset"
fi

# --- (e) multi-transport list -> one ClientTransportPlugin per transport ---
reset_torrc
TOR_CLIENT_TRANSPORT_PLUGIN="obfs4,snowflake"
apply_bridge_config
grep -q '^ClientTransportPlugin snowflake exec /usr/bin/snowflake-client$' "$TORRC" \
    && grep -q '^ClientTransportPlugin obfs4 exec /usr/bin/obfs4proxy$' "$TORRC" \
    && pass "emits one ClientTransportPlugin per listed transport" \
    || fail "multi-transport list did not yield both plugin lines"

if [ "$FAIL" -eq 0 ]; then
    echo "All apply_bridge_config() tests passed."
    exit 0
else
    echo "Some apply_bridge_config() tests FAILED."
    exit 1
fi
