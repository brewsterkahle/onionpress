#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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

# /var/lib/onionheaven is the OnionHeaven hub data volume
# (onionpress-onionheaven-data): the registry and every registrant's onion
# service key. Only onionpress-tor, onionheaven and the takeover workers
# mount it. It used to be onionheaven/ on onionpress-persistent-data, which
# WordPress mounts too; move what an older version left there before
# anything here opens the registry. The script waits for the other
# container's run, removes nothing it has not copied and verified, and fails
# only if the first move fails while registrants' keys are at stake. Then
# this container must not start, and Docker's restart policy tries again.
migrate_onionheaven_data() {
    if ! python3 /onionheaven-data-migrate.py; then
        echo "ERROR: the OnionHeaven data could not be moved to its own volume; not starting (nothing was removed)" >&2
        exit 1
    fi
}

# /var/lib/onionnames is OnionHome's name registry (onionpress-onionnames-data):
# which onionname points at which onion address. Only this container mounts
# it; web-server.py is its only reader and writer. It used to be onionhome/
# on onionpress-persistent-data, which WordPress can write; the script moves
# it (reading the old files as untrusted) before web-server.py starts. If it
# cannot, the container starts anyway: web-server.py serves no name (503)
# until the registry is in place, and the next start tries again.
migrate_onionhome_registry() {
    if ! python3 /onionhome-registry-migrate.py; then
        echo "WARNING: the OnionHome name registry is not in place on its volume; starting, /api/name/* answers 503 until it is (nothing was removed)" >&2
    fi
}

# tor-watchdog.py does sleep/wake (DEL_ONION/ADD_ONION), stall recovery and
# the app's republish requests; nothing else in the container does them, so
# it must not stay down (issue #313). Every mode starts it through this
# loop: log each exit, restart with backoff (1 s doubling to 60 s, back to
# 1 s after 10 min of running), and kill a watchdog that is still running
# but has not touched its alive file in WATCHDOG_STALE_SECS (a hung loop).
# A gap in this loop's own clock much longer than its poll means the VM was
# suspended or frozen: the alive file is old for that reason only, so that
# round is skipped rather than killing a healthy watchdog at wake, just
# before its USR2. The run dir's state (sleep marker, pending republish
# request, alive file) belongs to the previous life of this container and
# is cleared once, here. WATCHDOG_RUN_DIR must match tor-watchdog.py's
# ALIVE_PATH/SLEEPING_PATH folder (test-enforced). Runs as a subshell of
# this script, so `pgrep -f '[t]or-watchdog'` (the host's signal path)
# never matches it: a USR1 meant for the watchdog would kill a shell.
WATCHDOG_RUN_DIR="/run/tor-watchdog"
WATCHDOG_STALE_SECS=300
WATCHDOG_POLL_SECS=5
WATCHDOG_MAX_BACKOFF=60
WATCHDOG_RESET_AFTER=600
supervise_watchdog() {
    mkdir -p "$WATCHDOG_RUN_DIR"
    rm -f "$WATCHDOG_RUN_DIR/alive" "$WATCHDOG_RUN_DIR/sleeping" \
        "$WATCHDOG_RUN_DIR/republish-requested"
    backoff=1
    while true; do
        started=$(date +%s)
        python3 /tor-watchdog.py &
        wd_pid=$!
        last=$started
        while kill -0 "$wd_pid" 2>/dev/null; do
            sleep "$WATCHDOG_POLL_SECS"
            now=$(date +%s)
            gap=$(( now - last ))
            last=$now
            [ "$gap" -le $(( WATCHDOG_POLL_SECS * 6 )) ] || continue
            alive_mtime=$(stat -c %Y "$WATCHDOG_RUN_DIR/alive" 2>/dev/null) || continue
            age=$(( now - alive_mtime ))
            if [ "$age" -gt "$WATCHDOG_STALE_SECS" ] && kill -0 "$wd_pid" 2>/dev/null; then
                echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] watchdog supervisor: tor-watchdog PID $wd_pid has not touched its alive file in ${age}s (threshold ${WATCHDOG_STALE_SECS}s) — killing it"
                kill "$wd_pid" 2>/dev/null
                sleep 2
                kill -9 "$wd_pid" 2>/dev/null
            fi
        done
        wait "$wd_pid"
        status=$?
        ran=$(( $(date +%s) - started ))
        [ "$ran" -lt "$WATCHDOG_RESET_AFTER" ] || backoff=1
        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] watchdog supervisor: tor-watchdog PID $wd_pid exited with status $status after ${ran}s — restarting in ${backoff}s"
        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -le "$WATCHDOG_MAX_BACKOFF" ] || backoff=$WATCHDOG_MAX_BACKOFF
    done
}

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

# Farm controller mode — runs in the onionheaven-farm container, the one
# Tor-side container with the Docker socket. Nothing else from this script
# runs there: no Tor, no volumes but the controller's socket and the Docker
# socket (docs/DOCKER-SOCKET.md, "The farm controller").
if [ "${FARM_CONTROLLER}" = "1" ]; then
    exec python3 /onionheaven-farm.py
fi

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
    supervise_watchdog &

    # Start queue manager daemon (rate-limited ADD_ONION pipeline)
    LOG_FILE="/var/lib/onionheaven/queue-manager-${CONTAINER_NAME}.log"
    CONTAINER_NAME="${CONTAINER_NAME}" python3 /onionheaven-queue-manager.py daemon 2>"$LOG_FILE" &
    QM_PID=$!
    sleep 1
    if ! kill -0 $QM_PID 2>/dev/null; then
        echo "ERROR: onionheaven-queue-manager.py failed to start"
    fi

    # Start takeover worker (processes DB-mediated takeover/release/audit queues)
    TW_LOG="/var/lib/onionheaven/takeover-worker-${CONTAINER_NAME}.log"
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
        migrate_onionheaven_data

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
        supervise_watchdog &

        # Start onionheaven heartbeat monitor in background (log on the hub data volume)
        HEARTBEAT_LOG="/var/lib/onionheaven/heartbeat.log"
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
        supervise_watchdog &
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc"
        # Tor is this container's job; if it exits, so do we, rather than
        # falling through into the full onion-service mode below.
        exit $?
    fi
fi

# ==================== Onion service mode (the main tor container) ====================

migrate_onionheaven_data
migrate_onionhome_registry

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

# OnionHeaven API server — runs on every node (status, OnionHome endpoints),
# but only the OnionHeaven hub accepts registrations: the canonical hub
# address, or ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes (onionheaven_common.py).
mkdir -p /var/lib/onionheaven/keys
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

# Derive C Tor's key files from the delivered PEM whenever they are not an
# exact copy of it: missing (first run, key import, restore, an install that
# last ran on arti), damaged (a torn write or a damaged filesystem left a
# secret key that is not 96 bytes) or holding another key. Issue #311: only
# "missing" was handled, and a damaged secret key made the watchdog mint a
# NEW onion address and write it over the old one. The PEM is the identity;
# the old files are moved aside, never deleted.
for nickname in wordpress healthcheck; do
    PEM_KEY="${KEYS_DIR}/${nickname}/ks_hs_id.ed25519_expanded_private"
    CTOR_DIR="/var/lib/tor/hidden_service/${nickname}"
    [ -f "$PEM_KEY" ] || continue
    key_state=$(python3 /key-convert.py check-ctor "$PEM_KEY" "$CTOR_DIR" 2>&1)
    key_rc=$?
    case "$key_rc" in
        0) continue ;;
        3)
            echo "Key files for $nickname: $key_state — deriving them again from the delivered key"
            # Move whatever is there aside, so nothing that was a key is lost.
            aside="/var/lib/tor/key-set-aside/${nickname}-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
            for f in hs_ed25519_secret_key hs_ed25519_public_key hostname; do
                if [ -e "$CTOR_DIR/$f" ] || [ -L "$CTOR_DIR/$f" ]; then
                    { mkdir -p "$aside" && chmod 700 /var/lib/tor/key-set-aside "$aside" \
                        && mv "$CTOR_DIR/$f" "$aside/$f"; } \
                        || { echo "ERROR: could not move $CTOR_DIR/$f aside — refusing to start rather than overwrite it" >&2; exit 1; }
                fi
            done
            if [ -d "$aside" ]; then
                echo "WARNING: the previous key files for $nickname are kept in $aside" >&2
            fi
            ;;
        *)
            # Nothing to compare with or derive from. C Tor's own files
            # still serve if they are intact; if they are not, the watchdog
            # refuses to mint a new address for a service that had one.
            echo "WARNING: $nickname: $key_state — C Tor's key files cannot be checked against it" >&2
            if [ ! -f "$CTOR_DIR/hs_ed25519_secret_key" ]; then
                echo "ERROR: no usable key for $nickname — refusing to start: C Tor would mint a new onion address" >&2
                exit 1
            fi
            continue
            ;;
    esac
    # Unchecked, a failed conversion left the directory without a secret
    # key and C Tor minted a NEW onion address; the host only saw a
    # "hostname mismatch". A visible restart loop beats a silent re-key.
    python3 /key-convert.py arti-to-ctor "$PEM_KEY" "$CTOR_DIR" \
        || { echo "ERROR: key conversion failed for $nickname — refusing to start: C Tor would mint a new onion address" >&2; exit 1; }
    python3 /key-convert.py check-ctor "$PEM_KEY" "$CTOR_DIR" >/dev/null \
        || { echo "ERROR: $nickname's key files still differ from the delivered key after conversion — refusing to start" >&2; exit 1; }
done

# Set ownership on hidden service dirs (C Tor is strict about this)
for dir in /var/lib/tor/hidden_service/wordpress /var/lib/tor/hidden_service/healthcheck; do
    chown -R debian-tor:debian-tor "$dir" 2>/dev/null || chown -R tor:tor "$dir" 2>/dev/null || true
    chmod 700 "$dir"
done

# torrc comes straight from the template. It carries no HiddenService*
# lines: the watchdog manages onion services via ADD_ONION/DEL_ONION for
# clean sleep/wake, so there is nothing to strip.
cp /etc/tor/torrc.template /etc/tor/torrc

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
supervise_watchdog &

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
