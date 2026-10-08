#!/bin/sh
# OnionHeaven Tor Address Manager
# Manages dynamic onion service entries for address takeover/release via
# C Tor's control port (ADD_ONION / DEL_ONION), so nothing is reloaded and
# the other services on the same daemon are never disturbed.
#
# Usage:
#   onionheaven-tor-manager.sh takeover <content_address>
#   onionheaven-tor-manager.sh release <content_address>
#
# Keys arrive from OnionHeaven as OpenSSH PEMs (ks_hs_id.ed25519_expanded_private,
# the format arti introduced and OnionHeaven kept as its wire format); the raw
# ed25519 key is extracted from the PEM for ADD_ONION.

CTOR_TORRC="/etc/tor/torrc"
CTOR_HS_DIR="/var/lib/tor/hidden_service"
ONIONHEAVEN_KEYS_DIR="/var/lib/onionpress/onionheaven/keys"
REDIRECT_PORT=8082


usage() {
    echo "Usage: $0 takeover|release <content_address>"
    exit 1
}

if [ $# -lt 1 ]; then
    usage
fi

ACTION="$1"
shift

CONTENT_ADDRESS="${1:-}"

# Sanitize: strip any trailing whitespace/newlines
CONTENT_ADDRESS=$(echo "$CONTENT_ADDRESS" | tr -d '\n\r ')

# Validate address format (56 chars of base32 + .onion)
if ! echo "$CONTENT_ADDRESS" | grep -qE '^[a-z2-7]{56}\.onion$'; then
    echo "ERROR: Invalid .onion address format: $CONTENT_ADDRESS"
    exit 1
fi

# Nickname convention: onionheaven_ + first 16 chars of address (without .onion)
ADDR_PREFIX=$(echo "$CONTENT_ADDRESS" | sed 's/\.onion$//' | cut -c1-16)
NICKNAME="onionheaven_${ADDR_PREFIX}"

# Onion service directory for this service
HS_SERVICE_DIR="${CTOR_HS_DIR}/${NICKNAME}"


# ==================== C Tor takeover/release (control port) ====================
#
# Uses ADD_ONION/DEL_ONION via the control port (127.0.0.1:9051).
# This adds/removes individual onion services WITHOUT reloading config
# or disrupting other services — no SIGHUP, no circuit rebuild storm.

CTOR_CONTROL_PORT="127.0.0.1:9051"

# Send a command to C Tor's control port via netcat.
# Authenticates with the cookie file first.
# Returns the response on stdout.  Exits non-zero on failure.
ctor_control() {
    local cmd="$1"
    local cookie_hex
    cookie_hex=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\n')
    if [ -z "$cookie_hex" ]; then
        echo "ERROR: Cannot read control auth cookie at /var/lib/tor/control_auth_cookie"
        return 1
    fi
    local response
    response=$(printf 'AUTHENTICATE %s\r\n%s\r\nQUIT\r\n' "$cookie_hex" "$cmd" | nc -w 5 127.0.0.1 9051 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        echo "ERROR: Control port not responding at ${CTOR_CONTROL_PORT}"
        return 1
    fi
    echo "$response"
}

do_takeover_ctor() {
    local keys_src="${ONIONHEAVEN_KEYS_DIR}/${CONTENT_ADDRESS}"
    local key_file="${keys_src}/ks_hs_id.ed25519_expanded_private"

    # Check for key (OpenSSH PEM — we extract the raw ed25519 key)
    if [ ! -f "$key_file" ]; then
        echo "ERROR: No key found for ${CONTENT_ADDRESS}"
        exit 1
    fi
    if [ ! -s "$key_file" ]; then
        echo "ERROR: Empty key file for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # Verify the key actually derives to CONTENT_ADDRESS before using it.
    # Tor registers ADD_ONION services under whatever address the key produces;
    # a corrupted KEYS_DIR entry would otherwise serve the wrong onion under
    # this address's takeover slot, and the heartbeat RECONCILE loop would
    # cycle forever (see queue-manager.add_onion for the same guard).
    local derived_addr expected_addr
    derived_addr=$(python3 /key-convert.py pem-to-onion-address "$key_file" 2>/dev/null)
    expected_addr=$(echo "$CONTENT_ADDRESS" | sed 's/\.onion$//')
    if [ -z "$derived_addr" ]; then
        echo "ERROR: Could not derive address from key for ${CONTENT_ADDRESS}"
        exit 1
    fi
    if [ "$derived_addr" != "$expected_addr" ]; then
        echo "ERROR: Key mismatch for ${CONTENT_ADDRESS}: key derives to ${derived_addr}.onion (refusing takeover)"
        exit 1
    fi

    # Extract raw ed25519 expanded key as base64 for ADD_ONION
    local key_b64
    key_b64=$(python3 /key-convert.py pem-to-ed25519-base64 "$key_file")
    if [ -z "$key_b64" ]; then
        echo "ERROR: Key extraction failed for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # ADD_ONION: create ephemeral hidden service via control port (instant, no SIGHUP).
    # Purely ephemeral — no torrc, no key files on disk. The registry DB is the
    # source of truth; on restart, the takeover worker re-ADDs from the DB.
    local response
    response=$(ctor_control "ADD_ONION ED25519-V3:${key_b64} Flags=Detach Port=80,127.0.0.1:${REDIRECT_PORT}")
    if echo "$response" | grep -q "^250 "; then
        # Defense-in-depth post-ADD check (the pre-check above should catch this,
        # but verify Tor's ServiceID matches expected — a wedged Tor with stale
        # hs_service_map state could ignore our key and re-register an old one).
        local actual_sid
        actual_sid=$(echo "$response" | grep "^250-ServiceID=" | sed 's/^250-ServiceID=//' | tr -d "\r\n")
        if [ -n "$actual_sid" ] && [ "$actual_sid" != "$expected_addr" ]; then
            ctor_control "DEL_ONION ${actual_sid}" >/dev/null
            echo "ERROR: ADD_ONION returned wrong ServiceID for ${CONTENT_ADDRESS}: got ${actual_sid} (rolled back)"
            exit 1
        fi
        echo "ADD_ONION succeeded for ${CONTENT_ADDRESS}"
    elif echo "$response" | grep -q "Onion address collision"; then
        echo "Service already active for ${CONTENT_ADDRESS} (collision — OK)"
    else
        echo "ERROR: ADD_ONION failed for ${CONTENT_ADDRESS}:"
        echo "$response"
        exit 1
    fi

    echo "Takeover complete for ${CONTENT_ADDRESS} (C Tor ADD_ONION)"
}

do_release_ctor() {
    # Service ID is the address without .onion
    local service_id
    service_id=$(echo "$CONTENT_ADDRESS" | sed 's/\.onion$//')

    # DEL_ONION: remove ephemeral hidden service via control port (instant, no SIGHUP)
    local response
    response=$(ctor_control "DEL_ONION ${service_id}")
    if echo "$response" | grep -q "^250 "; then
        echo "DEL_ONION succeeded for ${CONTENT_ADDRESS}"
    else
        echo "WARNING: DEL_ONION response for ${CONTENT_ADDRESS}:"
        echo "$response"
        # Not fatal — service may have already been removed (restart, etc.)
    fi

    echo "Release complete for ${CONTENT_ADDRESS} (C Tor DEL_ONION)"
}

# ==================== Dispatch ====================

do_takeover() {
    # ADD_ONION via the control port — instant, no config reload
    do_takeover_ctor
}

do_release() {
    # DEL_ONION via the control port — instant, no config reload
    do_release_ctor
}

case "$ACTION" in
    takeover)
        if [ -z "$CONTENT_ADDRESS" ]; then usage; fi
        do_takeover
        ;;
    release)
        if [ -z "$CONTENT_ADDRESS" ]; then usage; fi
        do_release
        ;;
    *)
        usage
        ;;
esac
