#!/bin/sh
# OnionCellar Tor Address Manager
# Manages dynamic HiddenServiceDir entries in torrc for address takeover/release.
#
# Usage:
#   cellar-tor-manager.sh takeover <content_address>
#   cellar-tor-manager.sh release <content_address>
#
# On takeover: copies keys from cellar storage, adds HiddenServiceDir to torrc,
#              signals Tor to reload.
# On release: removes HiddenServiceDir from torrc, cleans up key directory,
#              signals Tor to reload.

TORRC="/etc/tor/torrc"
CELLAR_KEYS_DIR="/var/lib/onionpress/cellar/keys"
CELLAR_SERVICES_DIR="/var/lib/tor/hidden_service/cellar"
CELLAR_UNLOCKED_FILE="/var/lib/onionpress/cellar/.master-key-unlocked"
REDIRECT_PORT=8082

usage() {
    echo "Usage: $0 takeover|release <content_address>"
    exit 1
}

if [ $# -lt 2 ]; then
    usage
fi

ACTION="$1"
CONTENT_ADDRESS="$2"

# Sanitize: strip any trailing whitespace/newlines
CONTENT_ADDRESS=$(echo "$CONTENT_ADDRESS" | tr -d '\n\r ')

# Validate address format (56 chars of base32 + .onion)
if ! echo "$CONTENT_ADDRESS" | grep -qE '^[a-z2-7]{56}\.onion$'; then
    echo "ERROR: Invalid .onion address format: $CONTENT_ADDRESS"
    exit 1
fi

# Create a safe directory name from the address (use the address itself)
SERVICE_DIR="${CELLAR_SERVICES_DIR}/${CONTENT_ADDRESS}"

# Decrypt an .enc file using the master key (AES-256-GCM).
# Format: [12-byte IV][16-byte tag][ciphertext]
# Writes decrypted output to $2.
decrypt_file() {
    local enc_file="$1"
    local out_file="$2"
    local master_key_hex

    master_key_hex=$(od -A n -t x1 "$CELLAR_UNLOCKED_FILE" | tr -d ' \n')

    local file_size
    file_size=$(wc -c < "$enc_file")

    # Extract IV (first 12 bytes), tag (next 16 bytes), ciphertext (rest)
    local iv_hex tag_hex
    iv_hex=$(dd if="$enc_file" bs=1 count=12 2>/dev/null | od -A n -t x1 | tr -d ' \n')
    tag_hex=$(dd if="$enc_file" bs=1 skip=12 count=16 2>/dev/null | od -A n -t x1 | tr -d ' \n')

    # ciphertext starts at byte 28
    local ct_size=$((file_size - 28))
    dd if="$enc_file" bs=1 skip=28 count="$ct_size" 2>/dev/null | \
        openssl enc -d -aes-256-gcm \
            -K "$master_key_hex" \
            -iv "$iv_hex" \
            -tag "$tag_hex" \
            -out "$out_file" 2>/dev/null

    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to decrypt ${enc_file}"
        return 1
    fi
    return 0
}

do_takeover() {
    local keys_src="${CELLAR_KEYS_DIR}/${CONTENT_ADDRESS}"

    # Check for encrypted keys (.enc) or plaintext (legacy)
    local have_encrypted=false
    local have_plaintext=false

    if [ -f "${keys_src}/hs_ed25519_secret_key.enc" ]; then
        have_encrypted=true
    fi
    if [ -f "${keys_src}/hs_ed25519_secret_key" ]; then
        have_plaintext=true
    fi

    if [ "$have_encrypted" = false ] && [ "$have_plaintext" = false ]; then
        echo "ERROR: No keys found for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # Create the HiddenServiceDir
    mkdir -p "$SERVICE_DIR"

    if [ "$have_encrypted" = true ]; then
        # Encrypted keys — need master key to be unlocked
        if [ ! -f "$CELLAR_UNLOCKED_FILE" ]; then
            echo "ERROR: Cellar is locked — cannot decrypt keys"
            exit 2
        fi

        # Decrypt each key file to the service directory
        if ! decrypt_file "${keys_src}/hs_ed25519_secret_key.enc" "${SERVICE_DIR}/hs_ed25519_secret_key"; then
            echo "ERROR: Failed to decrypt secret key for ${CONTENT_ADDRESS}"
            exit 1
        fi
        if ! decrypt_file "${keys_src}/hs_ed25519_public_key.enc" "${SERVICE_DIR}/hs_ed25519_public_key"; then
            echo "ERROR: Failed to decrypt public key for ${CONTENT_ADDRESS}"
            exit 1
        fi
        cp "${keys_src}/hostname" "${SERVICE_DIR}/"
    else
        # Plaintext keys (legacy / backward compat during migration)
        cp "${keys_src}/hs_ed25519_secret_key" "${SERVICE_DIR}/"
        cp "${keys_src}/hs_ed25519_public_key" "${SERVICE_DIR}/"
        cp "${keys_src}/hostname" "${SERVICE_DIR}/"
    fi

    # Set correct ownership and permissions (tor user = uid 100)
    chown -R tor:tor "$SERVICE_DIR"
    chmod 700 "$SERVICE_DIR"
    chmod 600 "${SERVICE_DIR}/hs_ed25519_secret_key"
    chmod 600 "${SERVICE_DIR}/hs_ed25519_public_key"
    chmod 600 "${SERVICE_DIR}/hostname"

    # Add HiddenServiceDir entry to torrc if not already present
    local marker="# cellar:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$TORRC"; then
        echo "HiddenServiceDir entry already exists for ${CONTENT_ADDRESS}"
    else
        cat >> "$TORRC" << EOF

${marker}
HiddenServiceDir ${SERVICE_DIR}
HiddenServiceVersion 3
HiddenServicePort 80 127.0.0.1:${REDIRECT_PORT}
EOF
        echo "Added HiddenServiceDir for ${CONTENT_ADDRESS}"
    fi

    # Signal Tor to reload configuration
    local tor_pid
    tor_pid=$(pgrep -x tor)
    if [ -n "$tor_pid" ]; then
        kill -HUP "$tor_pid"
        echo "Sent SIGHUP to Tor (pid $tor_pid)"
    else
        echo "WARNING: Tor process not found, cannot send SIGHUP"
    fi

    echo "Takeover complete for ${CONTENT_ADDRESS}"
}

do_release() {
    # Remove HiddenServiceDir entry from torrc
    local marker="# cellar:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$TORRC"; then
        # Remove the marker line and the three config lines that follow it
        # (HiddenServiceDir, HiddenServiceVersion, HiddenServicePort)
        # Also remove the blank line before the marker if present
        local tmp_torrc="${TORRC}.tmp"
        awk -v marker="$marker" '
        BEGIN { skip = 0 }
        $0 == marker { skip = 3; next }
        skip > 0 { skip--; next }
        # Remove blank line right before marker (already printed — handled by buffering)
        { print }
        ' "$TORRC" > "$tmp_torrc"

        # Clean up any trailing blank lines left over
        sed -i '/^$/N;/^\n$/d' "$tmp_torrc" 2>/dev/null || true
        mv "$tmp_torrc" "$TORRC"
        echo "Removed HiddenServiceDir entry for ${CONTENT_ADDRESS}"
    else
        echo "No HiddenServiceDir entry found for ${CONTENT_ADDRESS}"
    fi

    # Remove the key directory
    if [ -d "$SERVICE_DIR" ]; then
        rm -rf "$SERVICE_DIR"
        echo "Removed key directory for ${CONTENT_ADDRESS}"
    fi

    # Signal Tor to reload configuration
    local tor_pid
    tor_pid=$(pgrep -x tor)
    if [ -n "$tor_pid" ]; then
        kill -HUP "$tor_pid"
        echo "Sent SIGHUP to Tor (pid $tor_pid)"
    else
        echo "WARNING: Tor process not found, cannot send SIGHUP"
    fi

    echo "Release complete for ${CONTENT_ADDRESS}"
}

case "$ACTION" in
    takeover)
        do_takeover
        ;;
    release)
        do_release
        ;;
    *)
        usage
        ;;
esac
