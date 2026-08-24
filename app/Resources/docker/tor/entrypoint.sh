#!/bin/sh
# OnionPress Tor entrypoint
# Supports both C Tor (default) and Arti via TOR_IMPL env var.
# Creates state directories, starts healthcheck server, launches Tor,
# and writes compat hostname files for existing scripts to read.

# Which Tor implementation to use: "tor" (C Tor, default) or "arti"
TOR_IMPL="${TOR_IMPL:-tor}"

# Bridge / pluggable-transport support (censored networks). Config-driven via
# TOR_BRIDGE_LINES (one "Bridge ..." line per entry, joined with ';' since
# ~/.onionpress/config has no multi-line values) and TOR_CLIENT_TRANSPORT_PLUGIN
# — a comma-separated list of transports ("snowflake", "obfs4", "meek_lite").
# Listing several matters because a censored network rarely leaves every
# transport usable in the same window: snowflake's WebRTC rendezvous, obfs4's
# fixed bridge IPs, and meek's domain-fronting fail independently, so we hand
# Tor bridges for all of them and let it race whichever the network allows
# through right now. Applied to every C-Tor torrc this entrypoint generates —
# main, onionheaven, takeover-worker, and SOCKS-only modes all run their own
# Tor process that needs to reach the network. Must be baked in at generation
# time: /etc/tor/torrc is rewritten from scratch on every start, so a runtime
# edit is silently discarded on restart, and Tor can't pick up a new
# ClientTransportPlugin via SIGHUP — only a real restart applies it.
apply_bridge_config() {
    [ -n "$TOR_BRIDGE_LINES" ] || return 0
    echo "UseBridges 1" >> /etc/tor/torrc
    # Upstream proxy for the pluggable transport's dial-out. When TOR_UPSTREAM_PROXY
    # is set, C Tor exports it to the PT as TOR_PT_PROXY, so obfs4proxy makes the
    # bridge connection THROUGH this proxy (e.g. a local VPN's SOCKS port) — the
    # only thing that reliably crosses an aggressively-censored network in the
    # windows where every direct transport is being disrupted. Emitted only
    # alongside a bridge on purpose: with a proxy but no bridge, Tor would hand the
    # proxy public *relay* IPs, and a VPN that blocklists Tor relays routes those
    # direct (→ GFW reset); a bridge IP isn't relay-listed, so the proxy tunnels it.
    [ -n "${TOR_UPSTREAM_PROXY:-}" ] && echo "Socks5Proxy $TOR_UPSTREAM_PROXY" >> /etc/tor/torrc
    # One ClientTransportPlugin line per named transport. meek_lite/obfs2/obfs3/
    # scramblesuit are all implemented by the same obfs4proxy binary.
    for transport in $(echo "$TOR_CLIENT_TRANSPORT_PLUGIN" | tr ',' ' '); do
        case "$transport" in
            snowflake)
                echo "ClientTransportPlugin snowflake exec /usr/bin/snowflake-client" >> /etc/tor/torrc
                ;;
            obfs4|meek_lite|obfs2|obfs3|scramblesuit)
                echo "ClientTransportPlugin $transport exec /usr/bin/obfs4proxy" >> /etc/tor/torrc
                ;;
        esac
    done
    echo "$TOR_BRIDGE_LINES" | tr ';' '\n' | while IFS= read -r bridge_line; do
        # Trim leading/trailing whitespace (e.g. a space after a ';'
        # separator) before checking for a redundant "Bridge " prefix —
        # otherwise a leading space defeats the prefix-strip below.
        bridge_line=$(echo "$bridge_line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        bridge_line="${bridge_line#Bridge }"
        [ -n "$bridge_line" ] && echo "Bridge $bridge_line" >> /etc/tor/torrc
    done
    echo "Bridge/pluggable-transport support enabled (transport: ${TOR_CLIENT_TRANSPORT_PLUGIN:-none})"
}

# Arti equivalent of apply_bridge_config(): TOR_IMPL=arti is this deployment's
# actual default (~/.onionpress/config sets it explicitly), and every Arti
# code path below launches "arti proxy -c <file>.toml" straight from the
# image-baked config — apply_bridge_config() only ever touches /etc/tor/torrc,
# so on TOR_IMPL=arti, TOR_BRIDGE_LINES was silently never applied to
# anything. Arti's bridges live in a [bridges] TOML table, not a torrc line,
# so this patches the given arti config file instead of any global filename.
# Idempotent against `docker restart` (same container, same on-disk file)
# re-running the entrypoint and appending the table twice, which arti's TOML
# parser rejects as a duplicate key.
apply_arti_bridge_config() {
    target="$1"
    [ -n "$TOR_BRIDGE_LINES" ] || return 0
    grep -q '^\[bridges\]' "$target" 2>/dev/null && return 0
    {
        echo ""
        echo "[bridges]"
        echo "enabled = true"
        echo "bridges = ["
        echo "$TOR_BRIDGE_LINES" | tr ';' '\n' | while IFS= read -r bridge_line; do
            # Same trim/prefix-strip as apply_bridge_config() — TOR_BRIDGE_LINES
            # is shared between both implementations.
            bridge_line=$(echo "$bridge_line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
            bridge_line="${bridge_line#Bridge }"
            [ -n "$bridge_line" ] && printf '  "%s",\n' "$bridge_line"
        done
        echo "]"
        # One [[bridges.transports]] stanza per named transport, same
        # comma-separated list and same race rationale as apply_bridge_config().
        for transport in $(echo "$TOR_CLIENT_TRANSPORT_PLUGIN" | tr ',' ' '); do
            case "$transport" in
                snowflake)
                    pt_path="/usr/bin/snowflake-client"
                    ;;
                obfs4|meek_lite|obfs2|obfs3|scramblesuit)
                    pt_path="/usr/bin/obfs4proxy"
                    ;;
                *)
                    continue
                    ;;
            esac
            echo ""
            echo "[[bridges.transports]]"
            printf 'protocols = ["%s"]\n' "$transport"
            printf 'path = "%s"\n' "$pt_path"
        done
    } >> "$target"
    echo "Bridge/pluggable-transport support enabled for $target (transport: ${TOR_CLIENT_TRANSPORT_PLUGIN:-none})"
}

# Create Arti state directories with strict permissions (Arti requires o-rx)
mkdir -p /var/lib/arti/cache /var/lib/arti/state

# Persistent Arti log — survives container restarts (on arti-state volume)
ARTI_LOG="/var/lib/arti/arti.log"

# Rotate log if >10MB
rotate_log() {
    if [ -f "$ARTI_LOG" ]; then
        size=$(stat -c%s "$ARTI_LOG" 2>/dev/null || wc -c < "$ARTI_LOG")
        if [ "$size" -gt 10485760 ] 2>/dev/null; then
            mv "$ARTI_LOG" "${ARTI_LOG}.1"
        fi
    fi
}
rotate_log
echo "=== Arti starting at $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" >> "$ARTI_LOG"

# Clean ephemeral state that causes "Too many preemptive onion service circuits
# failed" after container restarts. The keystore (identity keys) must survive,
# but guards, circuit timeouts, and intro point state become stale/poisoned
# across restarts and should be rebuilt fresh.
rm -rf /var/lib/arti/cache/*
rm -f /var/lib/arti/state/state/guards.json
rm -f /var/lib/arti/state/state/circuit_timeouts.json
rm -rf /var/lib/arti/state/hss/*/iptreplay/
rm -rf /var/lib/arti/state/hss/*/ipts.json
rm -rf /var/lib/arti/state/hss/*/iptpub.json
rm -rf /var/lib/arti/state/keystore/hss/*/ipts/

chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Takeover worker mode — runs in onionheaven-takeover-N containers
if [ "${TAKEOVER_WORKER}" = "1" ]; then
    echo "Takeover worker mode: starting ${TOR_IMPL} (SOCKS + keystore), redirect service, and takeover worker..."
    CONTAINER_NAME="${CONTAINER_NAME:-onionheaven-takeover-unknown}"

    # Start OnionHeaven redirect service in background (port 8082)
    /onionheaven-redirect.sh &
    ONIONHEAVEN_REDIRECT_PID=$!
    sleep 1
    if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
        echo "ERROR: onionheaven-redirect.sh failed to start"
    fi

    if [ "$TOR_IMPL" = "tor" ]; then
        # C Tor with SOCKS + control port for ADD_ONION/DEL_ONION
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
        apply_bridge_config
        chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
        TOR_PID=$!
        sleep 2
        if ! kill -0 $TOR_PID 2>/dev/null; then
            echo "ERROR: C Tor failed to start"
        fi
        # Start watchdog to monitor Tor health via control port
        python3 /tor-watchdog.py &
    else
        # Start Arti with OnionHeaven config (SOCKS + keystore for takeover services)
        apply_arti_bridge_config /etc/arti/arti-onionheaven.toml
        su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-onionheaven.toml" &
        TOR_PID=$!
        sleep 2
        if ! kill -0 $TOR_PID 2>/dev/null; then
            echo "ERROR: Arti failed to start — check config at /etc/arti/arti-onionheaven.toml"
        fi
    fi

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
        # OnionHeaven heartbeat/takeover mode: Tor with takeover keystore +
        # heartbeat monitor + redirect. The API server runs in the main tor
        # container — this container only handles monitoring and takeover duties.
        echo "OnionHeaven mode: starting ${TOR_IMPL} (SOCKS + keystore), redirect service, and heartbeat monitor..."

        # Start OnionHeaven redirect service in background (port 8082)
        /onionheaven-redirect.sh &
        ONIONHEAVEN_REDIRECT_PID=$!
        sleep 1
        if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-redirect.sh failed to start"
        fi

        if [ "$TOR_IMPL" = "tor" ]; then
            # C Tor with control port for ADD_ONION/DEL_ONION (no SIGHUP needed)
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
            apply_bridge_config
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
            TOR_PID=$!
            sleep 2
            if ! kill -0 $TOR_PID 2>/dev/null; then
                echo "ERROR: C Tor failed to start"
            fi
            # Start watchdog to monitor Tor health via control port
            python3 /tor-watchdog.py &
        else
            # Start Arti with OnionHeaven config (SOCKS + keystore)
            apply_arti_bridge_config /etc/arti/arti-onionheaven.toml
            su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-onionheaven.toml" &
            TOR_PID=$!
            sleep 2
            if ! kill -0 $TOR_PID 2>/dev/null; then
                echo "ERROR: Arti failed to start — check config at /etc/arti/arti-onionheaven.toml"
            fi
        fi

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
        if [ "$TOR_IMPL" = "tor" ]; then
            echo "SOCKS-only mode: starting C Tor SOCKS proxy (no onion services)..."
            mkdir -p /var/lib/tor
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
            chmod 700 /var/lib/tor
            # Minimal torrc for SOCKS-only
            cat > /etc/tor/torrc << TORRC_EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
TORRC_EOF
            apply_bridge_config
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
            # Start watchdog in background (will connect once control port is ready)
            python3 /tor-watchdog.py &
            su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc"
        else
            echo "SOCKS-only mode: starting Arti SOCKS proxy (no onion services)..."
            apply_arti_bridge_config /etc/arti/arti-polling.toml
            su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-polling.toml" 2>&1 | tee -a "$ARTI_LOG"
        fi
    fi
fi

# Create compat directories for hostname files
mkdir -p /var/lib/tor/hidden_service/wordpress
mkdir -p /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Forward 127.0.0.1:8080 → wordpress:80 (both Arti and C Tor need IP targets)
socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &
SOCAT_PID=$!
sleep 1
if ! kill -0 $SOCAT_PID 2>/dev/null; then
    echo "ERROR: socat (port 8080 forward) failed to start"
fi

# OnionHeaven API server — runs on EVERY node so any OnionPress instance
# can accept registrations. The onionheaven container (heartbeat monitor +
# takeover Arti) starts lazily when the first registration arrives.
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

if [ "$TOR_IMPL" = "tor" ]; then
    # ==================== C Tor mode ====================
    echo "Starting C Tor (TOR_IMPL=tor)..."

    # Create C Tor data directory
    mkdir -p /var/lib/tor
    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
    chmod 700 /var/lib/tor

    # Convert Arti keys to C Tor format if Arti keystore exists but C Tor keys don't
    for nickname in wordpress healthcheck; do
        ARTI_KEY="/var/lib/arti/state/keystore/hss/${nickname}/ks_hs_id.ed25519_expanded_private"
        CTOR_DIR="/var/lib/tor/hidden_service/${nickname}"
        CTOR_SECRET="${CTOR_DIR}/hs_ed25519_secret_key"
        if [ -f "$ARTI_KEY" ] && [ ! -f "$CTOR_SECRET" ]; then
            echo "Converting Arti key for $nickname to C Tor format..."
            python3 /key-convert.py arti-to-ctor "$ARTI_KEY" "$CTOR_DIR"
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

    apply_bridge_config

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

    # Start C Tor as debian-tor user (log to persistent file + docker logs)
    TOR_LOG="/var/lib/tor/tor.log"
    # Pre-create the log file owned by debian-tor. Otherwise, on a fresh
    # volume, `tee -a` below creates it as root before Tor starts, and Tor's
    # own "Log notice file" directive (torrc.template) then fails with
    # "Permission denied" since debian-tor can't write a root-owned file.
    # It self-heals on the next restart (root:root vs debian-tor ownership
    # only happens once), but costs a restart cycle and logs ERROR/[err].
    touch "$TOR_LOG"
    chown debian-tor:debian-tor "$TOR_LOG" 2>/dev/null || true
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
else
    # ==================== Arti mode ====================

    # Expose port 8083 through the onion service so other nodes can reach the API
    sed -i 's/proxy_ports = \[\["80", "127.0.0.1:8080"\]\]/proxy_ports = [["80", "127.0.0.1:8080"], ["8083", "127.0.0.1:8083"]]/' /etc/arti/arti.toml

    # Every node runs the OnionHeaven API — use max intro points to handle heartbeat traffic
    sed -i 's/num_intro_points = 3/num_intro_points = 10/' /etc/arti/arti.toml

    # Convert C Tor keys to Arti format if switching back from C Tor
    for nickname in wordpress healthcheck; do
        CTOR_SECRET="/var/lib/tor/hidden_service/${nickname}/hs_ed25519_secret_key"
        ARTI_KEY="/var/lib/arti/state/keystore/hss/${nickname}/ks_hs_id.ed25519_expanded_private"
        if [ -f "$CTOR_SECRET" ] && [ ! -f "$ARTI_KEY" ]; then
            echo "Converting C Tor key for $nickname to Arti format..."
            mkdir -p "/var/lib/arti/state/keystore/hss/${nickname}"
            python3 /key-convert.py ctor-to-arti "$CTOR_SECRET" "$ARTI_KEY"
            chown -R arti:arti "/var/lib/arti/state/keystore/hss/${nickname}"
            chmod 700 "/var/lib/arti/state/keystore/hss/${nickname}"
            chmod 600 "$ARTI_KEY"
        fi
    done

    # Start Arti in background (log to persistent file + docker logs)
    apply_arti_bridge_config /etc/arti/arti.toml
    su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" 2>&1 | tee -a "$ARTI_LOG" &
    ARTI_PID=$!
    sleep 2
    if ! kill -0 $ARTI_PID 2>/dev/null; then
        echo "ERROR: Arti failed to start — check config at /etc/arti/arti.toml"
    fi

    # Wait for Arti to generate keys, then write compat hostname files
    # so existing scripts (healthcheck-server.sh, launcher, menubar.py)
    # can read onion addresses from the same paths as before.
    write_compat_hostnames() {
        for nickname in wordpress healthcheck; do
            while true; do
                # --nickname must come before the subcommand; run as arti user (not root)
                addr=$(su -s /bin/sh arti -c "arti hss --nickname $nickname onion-address -c /etc/arti/arti.toml" 2>/dev/null)
                if [ -n "$addr" ]; then
                    echo "$addr" > "/var/lib/tor/hidden_service/$nickname/hostname"
                    echo "Onion address for $nickname: $addr"
                    break
                fi
                sleep 2
            done
        done
    }
    write_compat_hostnames &

    # Wait for Arti process
    wait $ARTI_PID
fi
