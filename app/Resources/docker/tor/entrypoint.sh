#!/bin/sh
# OnionPress Tor entrypoint (C Tor).
# Creates state directories, starts the helper services, launches Tor, and
# writes the hostname files that the launchers and scripts read.
#
# /var/lib/onionpress-keys is the identity volume (onionpress-onion-keys):
# the launchers install the onion service key there on first run and on key
# import/restore, as an OpenSSH PEM at <name>/ks_hs_id.ed25519_expanded_private,
# and this script converts it to C Tor's key files whenever those are missing.
# On macOS it is the only route by which the key reaches the container.
KEYS_DIR="/var/lib/onionpress-keys"

# Write a minimal torrc for the SOCKS + control-port modes below.
write_client_torrc() {
    mkdir -p /var/lib/tor
    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
    chmod 700 /var/lib/tor
    cat > /etc/tor/torrc << TORRC_EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
TORRC_EOF
    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
}

# Takeover worker mode — runs in onionheaven-takeover-N containers
if [ "${TAKEOVER_WORKER}" = "1" ]; then
    echo "Takeover worker mode: starting C Tor (SOCKS + control port), redirect service, and takeover worker..."
    CONTAINER_NAME="${CONTAINER_NAME:-onionheaven-takeover-unknown}"

    # Start OnionHeaven redirect service in background (port 8082)
    /onionheaven-redirect.sh &
    ONIONHEAVEN_REDIRECT_PID=$!
    sleep 1
    if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
        echo "ERROR: onionheaven-redirect.sh failed to start"
    fi

    # C Tor with SOCKS + control port for ADD_ONION/DEL_ONION
    write_client_torrc
    su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
    TOR_PID=$!
    sleep 2
    if ! kill -0 $TOR_PID 2>/dev/null; then
        echo "ERROR: C Tor failed to start"
    fi
    # Start watchdog to monitor Tor health via control port
    python3 /tor-watchdog.py &

    # Start queue manager daemon (rate-limited ADD_ONION pipeline)
    LOG_FILE="/var/lib/onionpress/onionheaven/queue-manager-${CONTAINER_NAME}.log"
    CONTAINER_NAME="${CONTAINER_NAME}" python3 /onionheaven-queue-manager.py daemon 2>"$LOG_FILE" &
    QM_PID=$!
    sleep 1
    if ! kill -0 $QM_PID 2>/dev/null; then
        echo "ERROR: onionheaven-queue-manager.py failed to start"
    fi

    # Start takeover worker (processes DB-mediated takeover/release/audit queues)
    TW_LOG="/var/lib/onionpress/onionheaven/takeover-worker-${CONTAINER_NAME}.log"
    CONTAINER_NAME="${CONTAINER_NAME}" TAKEOVER_WORKER=1 python3 /onionheaven-takeover-worker.py 2>"$TW_LOG" &
    TW_PID=$!
    sleep 1
    if ! kill -0 $TW_PID 2>/dev/null; then
        echo "ERROR: onionheaven-takeover-worker.py failed to start"
    fi

    # Wait on Tor (main process)
    wait $TOR_PID
    exit $?
fi

# No-onion-service mode (tor-client = SOCKS only, onionheaven = heartbeat/takeover)
if [ "${NO_ONION_SERVICE}" = "1" ]; then
    if [ "${ONIONHEAVEN}" = "1" ]; then
        # OnionHeaven heartbeat/takeover mode: Tor with control port +
        # heartbeat monitor + redirect. The API server runs in the main tor
        # container — this container only handles monitoring and takeover duties.
        echo "OnionHeaven mode: starting C Tor (SOCKS + control port), redirect service, and heartbeat monitor..."

        # Start OnionHeaven redirect service in background (port 8082)
        /onionheaven-redirect.sh &
        ONIONHEAVEN_REDIRECT_PID=$!
        sleep 1
        if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-redirect.sh failed to start"
        fi

        # C Tor with control port for ADD_ONION/DEL_ONION (no SIGHUP needed)
        write_client_torrc
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
        TOR_PID=$!
        sleep 2
        if ! kill -0 $TOR_PID 2>/dev/null; then
            echo "ERROR: C Tor failed to start"
        fi
        # Start watchdog to monitor Tor health via control port
        python3 /tor-watchdog.py &

        # Start onionheaven heartbeat monitor in background (log to shared volume)
        HEARTBEAT_LOG="/var/lib/onionpress/onionheaven/heartbeat.log"
        mkdir -p "$(dirname "$HEARTBEAT_LOG")"
        python3 /onionheaven-heartbeat.py 2>>"$HEARTBEAT_LOG" &
        HEARTBEAT_PID=$!
        sleep 1
        if ! kill -0 $HEARTBEAT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-heartbeat.py failed to start"
        fi

        # Watchdog: restart heartbeat if its log goes stale (stuck/crashed process)
        WATCHDOG_STALE_SECS=300
        WATCHDOG_CHECK_INTERVAL=60
        (
            while true; do
                sleep $WATCHDOG_CHECK_INTERVAL

                # If heartbeat log doesn't exist yet, skip
                [ -f "$HEARTBEAT_LOG" ] || continue

                # Cap log size to keep VM disk bounded. Truncate to the
                # last ~512 KB when the file exceeds 1 MB; mtime gets
                # refreshed on the next heartbeat write so the staleness
                # check below still works. A brief race with concurrent
                # heartbeat writes can lose a handful of lines, which is
                # acceptable: the file is diagnostic, not transactional.
                log_size=$(stat -c %s "$HEARTBEAT_LOG" 2>/dev/null || echo 0)
                if [ "$log_size" -gt 1048576 ]; then
                    tmp="$HEARTBEAT_LOG.rotate.$$"
                    tail -c 524288 "$HEARTBEAT_LOG" > "$tmp" 2>/dev/null && \
                        cat "$tmp" > "$HEARTBEAT_LOG"
                    rm -f "$tmp"
                fi

                # Get log file age in seconds
                log_mtime=$(stat -c %Y "$HEARTBEAT_LOG" 2>/dev/null) || continue
                now=$(date +%s)
                age=$(( now - log_mtime ))

                if [ "$age" -gt "$WATCHDOG_STALE_SECS" ]; then
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat log stale for ${age}s (threshold: ${WATCHDOG_STALE_SECS}s)" >> "$HEARTBEAT_LOG"

                    # Log diagnostics before killing
                    if kill -0 $HEARTBEAT_PID 2>/dev/null; then
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat PID $HEARTBEAT_PID is alive but not writing logs" >> "$HEARTBEAT_LOG"
                        wchan=$(cat /proc/$HEARTBEAT_PID/wchan 2>/dev/null || echo "unknown")
                        fdcount=$(ls /proc/$HEARTBEAT_PID/fd 2>/dev/null | wc -l || echo "unknown")
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: PID $HEARTBEAT_PID wchan=$wchan open_fds=$fdcount" >> "$HEARTBEAT_LOG"
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: killing stale heartbeat PID $HEARTBEAT_PID" >> "$HEARTBEAT_LOG"
                        kill $HEARTBEAT_PID 2>/dev/null
                        sleep 2
                        kill -9 $HEARTBEAT_PID 2>/dev/null
                    else
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat PID $HEARTBEAT_PID is dead (silent crash)" >> "$HEARTBEAT_LOG"
                    fi

                    # Restart
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: restarting heartbeat monitor" >> "$HEARTBEAT_LOG"
                    python3 /onionheaven-heartbeat.py 2>>"$HEARTBEAT_LOG" &
                    HEARTBEAT_PID=$!
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat restarted as PID $HEARTBEAT_PID" >> "$HEARTBEAT_LOG"
                fi
            done
        ) &

        # Wait on Tor process (main process)
        wait $TOR_PID
        exit $?
    else
        # SOCKS-only mode (tor-client): just a proxy, no onion services
        echo "SOCKS-only mode: starting C Tor SOCKS proxy (no onion services)..."
        write_client_torrc
        # Start watchdog in background (will connect once control port is ready)
        python3 /tor-watchdog.py &
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc"
        # Tor is this container's job; if it exits, so do we, rather than
        # falling through into the full onion-service mode below.
        exit $?
    fi
fi

# ==================== Onion service mode (the main tor container) ====================

# Create compat directories for hostname files
mkdir -p /var/lib/tor/hidden_service/wordpress
mkdir -p /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Forward 127.0.0.1:8080 → wordpress:80 (the onion service target must be an IP)
socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &
SOCAT_PID=$!
sleep 1
if ! kill -0 $SOCAT_PID 2>/dev/null; then
    echo "ERROR: socat (port 8080 forward) failed to start"
fi

# OnionHeaven API server — runs on EVERY node so any OnionPress instance
# can accept registrations. The onionheaven container (heartbeat monitor +
# takeover Tor) starts lazily when the first registration arrives.
mkdir -p /var/lib/onionpress/onionheaven/keys
python3 /web-server.py &
ONIONHEAVEN_SERVER_PID=$!
sleep 1
if ! kill -0 $ONIONHEAVEN_SERVER_PID 2>/dev/null; then
    echo "ERROR: web-server.py failed to start"
fi

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &
HC_PID=$!
sleep 1
if ! kill -0 $HC_PID 2>/dev/null; then
    echo "ERROR: healthcheck-server.sh failed to start"
fi

echo "Starting C Tor..."

# Create C Tor data directory
mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
chmod 700 /var/lib/tor

# Convert the delivered PEM key to C Tor's key files when those are missing
# (first run, key import, restore, or an install that last ran on arti).
for nickname in wordpress healthcheck; do
    PEM_KEY="${KEYS_DIR}/${nickname}/ks_hs_id.ed25519_expanded_private"
    CTOR_DIR="/var/lib/tor/hidden_service/${nickname}"
    CTOR_SECRET="${CTOR_DIR}/hs_ed25519_secret_key"
    if [ -f "$PEM_KEY" ] && [ ! -f "$CTOR_SECRET" ]; then
        echo "Converting delivered key for $nickname to C Tor format..."
        python3 /key-convert.py arti-to-ctor "$PEM_KEY" "$CTOR_DIR"
    fi
done

# Set ownership on hidden service dirs (C Tor is strict about this)
for dir in /var/lib/tor/hidden_service/wordpress /var/lib/tor/hidden_service/healthcheck; do
    chown -R debian-tor:debian-tor "$dir" 2>/dev/null || chown -R tor:tor "$dir" 2>/dev/null || true
    chmod 700 "$dir"
done

# Generate torrc from template — strip HiddenServiceDir lines since the
# watchdog manages onion services via ADD_ONION/DEL_ONION for clean sleep/wake.
cp /etc/tor/torrc.template /etc/tor/torrc
sed -i '/^HiddenServiceDir /d; /^HiddenServicePort /d; /^HiddenServiceNumIntroductionPoints /d; /^# __WORDPRESS_API_PORT__/d' /etc/tor/torrc

# Write onion service definitions for the watchdog to ADD_ONION.
# Keys live on disk at /var/lib/tor/hidden_service/<name>/.
cat > /etc/tor/onion-services.json << 'SERVICES_EOF'
[
  {"name": "wordpress", "ports": ["80,127.0.0.1:8080", "8083,127.0.0.1:8083"]},
  {"name": "healthcheck", "ports": ["80,127.0.0.1:8081"]}
]
SERVICES_EOF
echo "Wrote /etc/tor/onion-services.json for watchdog ADD_ONION"

# Ensure all of /var/lib/tor is owned by debian-tor (C Tor checks this)
chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true

# Start C Tor as debian-tor user (log to persistent file + docker logs).
# The log file is created here, as debian-tor, before tee can create it as
# root: Tor opens it itself for `Log notice file` and cannot write a
# root-owned one, which used to cost a crash-and-restart on every fresh
# volume.
TOR_LOG="/var/lib/tor/tor.log"
touch "$TOR_LOG" && chown debian-tor:debian-tor "$TOR_LOG" 2>/dev/null || true
su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" 2>&1 | tee -a "$TOR_LOG" &
TOR_PID=$!
sleep 2
if ! kill -0 $TOR_PID 2>/dev/null; then
    echo "ERROR: C Tor failed to start — check config at /etc/tor/torrc"
fi

# Start watchdog to monitor Tor health and manage onion services
python3 /tor-watchdog.py &

# Wait for hostname files (first run: Tor creates them; subsequent: watchdog ADD_ONION)
write_ctor_hostnames() {
    for nickname in wordpress healthcheck; do
        local hfile="/var/lib/tor/hidden_service/${nickname}/hostname"
        while [ ! -f "$hfile" ] || [ ! -s "$hfile" ]; do
            sleep 2
        done
        echo "Onion address for $nickname: $(cat "$hfile")"
    done
}
write_ctor_hostnames &

# Wait for C Tor process
wait $TOR_PID
