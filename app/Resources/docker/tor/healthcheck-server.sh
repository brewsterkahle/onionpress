#!/bin/sh
# OnionPress Healthcheck Server
# Listens on port 8081 via socat, serves status JSON on GET,
# stores OnionHeaven messages on POST.

HEALTHCHECK_PORT=8081
MESSAGES_DIR="/var/lib/tor/healthcheck-messages"
MESSAGES_MAX=100
MESSAGES_TTL=86400  # 24 hours in seconds
MAX_REQUEST_BODY=1048576  # 1 MB
CONTENT_HOSTNAME_FILE="/var/lib/tor/hidden_service/wordpress/hostname"
HEALTHCHECK_HOSTNAME_FILE="/var/lib/tor/hidden_service/healthcheck/hostname"
VERSION_FILE="/var/lib/tor/healthcheck-version"
STARTED_FILE="/var/lib/tor/healthcheck-started"
# The WordPress facts a GET reports (up, last post, sites), kept for
# CACHE_TTL seconds (issue #393): every anonymous hit made three requests
# to WordPress, so one Tor client could triple the site's load.
CACHE_DIR="/run/healthcheck"
CACHE_FILE="$CACHE_DIR/wordpress-facts"
CACHE_TTL=60
LOG_FIELD_MAX=200  # longest request path / request line we put in a log line

# HTTP status of the response the current handler wrote; read by the
# per-request log line in handle_request.
STATUS=""

# Request logging (issue #74). One line per handled request, to STDERR:
# under socat's --handle-request, stdout IS the client socket, while stderr
# is inherited from socat and lands in the container log (which the
# analytics upload ships to OnionHome). Called after the response has been
# written, so a slow or closed log destination can never delay or break a
# response, and it must never fail the handler.
log_request() {
    # $1=method $2=path $3=outcome
    printf '[%s] healthcheck: %s %s -> %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$3" >&2 || true
}

# Client-supplied text, made safe for a log line: printable ASCII only
# (no CR/LF, so a request cannot forge log lines) and bounded in length.
log_safe() {
    printf '%s' "$1" | LC_ALL=C tr -cd '\041-\176' | cut -c1-"$LOG_FIELD_MAX"
}

# Read version from file if available (set by entrypoint)
get_version() {
    if [ -f "$VERSION_FILE" ]; then
        cat "$VERSION_FILE"
    else
        echo "unknown"
    fi
}

# Delete messages older than MESSAGES_TTL
cleanup_old_messages() {
    local now
    now=$(date +%s)
    for f in "$MESSAGES_DIR"/*.json; do
        [ -f "$f" ] || continue
        # Filename is timestamp.json — extract the seconds portion
        local basename="${f##*/}"
        local file_ts="${basename%.json}"
        # Strip nanosecond suffix if present (keep first 10 digits)
        file_ts=$(echo "$file_ts" | cut -c1-10)
        if [ "$((now - file_ts))" -gt "$MESSAGES_TTL" ] 2>/dev/null; then
            rm -f "$f"
        fi
    done
}

# Enforce max message count, deleting oldest first
enforce_message_cap() {
    local count
    count=$(ls -1 "$MESSAGES_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
    if [ "$count" -gt "$MESSAGES_MAX" ]; then
        local excess=$((count - MESSAGES_MAX))
        ls -1 "$MESSAGES_DIR"/*.json | sort | head -n "$excess" | while read f; do
            rm -f "$f"
        done
    fi
}

# The three WordPress probes, into WP_OK, WP_STATUS, LAST_POST and SITES.
probe_wordpress() {
    local posts_json="" sites_json=""
    WP_OK="false"
    WP_STATUS="degraded"
    if wget -q -O /dev/null --timeout=5 http://wordpress:80/ 2>/dev/null; then
        WP_OK="true"
        WP_STATUS="ok"
    fi

    # last_post from the WordPress REST API
    LAST_POST="null"
    posts_json=$(wget -q -O - --timeout=5 "http://wordpress:80/wp-json/wp/v2/posts?per_page=1&orderby=date&order=desc&_fields=date" 2>/dev/null)
    if [ -n "$posts_json" ] && echo "$posts_json" | grep -q '"date"'; then
        LAST_POST=$(echo "$posts_json" | sed -n 's/.*"date":"\([^"]*\)".*/"\1"/p' | head -1)
        if [ -z "$LAST_POST" ]; then
            LAST_POST="null"
        fi
    fi

    # sites count from the multisite API
    SITES=1
    sites_json=$(wget -q -O - --timeout=5 "http://wordpress:80/wp-json/wp/v2/sites" 2>/dev/null)
    if [ -n "$sites_json" ] && echo "$sites_json" | grep -q '"id"'; then
        SITES=$(echo "$sites_json" | grep -o '"id"' | wc -l | tr -d ' ')
    fi
}

# The cached facts when the cache is younger than CACHE_TTL (0), else 1.
read_cached_facts() {
    local now mtime age
    [ -f "$CACHE_FILE" ] || return 1
    now=$(date +%s)
    mtime=$(stat -c %Y "$CACHE_FILE" 2>/dev/null) || return 1
    age=$((now - mtime))
    [ "$age" -ge 0 ] && [ "$age" -lt "$CACHE_TTL" ] || return 1
    {
        read -r WP_OK && read -r WP_STATUS && read -r LAST_POST && read -r SITES
    } < "$CACHE_FILE" || return 1
    case "$WP_OK" in true|false) ;; *) return 1 ;; esac
    [ -n "$WP_STATUS" ] && [ -n "$LAST_POST" ] && [ -n "$SITES" ]
}

# WP_OK, WP_STATUS, LAST_POST, SITES: from the cache, else probed and
# cached (written whole, then renamed, so a reader never sees a part).
wordpress_facts() {
    if read_cached_facts; then
        return 0
    fi
    probe_wordpress
    mkdir -p "$CACHE_DIR" 2>/dev/null || return 0
    if printf '%s\n%s\n%s\n%s\n' "$WP_OK" "$WP_STATUS" "$LAST_POST" "$SITES" > "$CACHE_FILE.$$" 2>/dev/null; then
        mv -f "$CACHE_FILE.$$" "$CACHE_FILE" 2>/dev/null || rm -f "$CACHE_FILE.$$"
    fi
    return 0
}

# Handle GET — return status JSON
handle_get() {
    # Clean up expired messages before responding
    cleanup_old_messages

    # Read addresses
    local content_address=""
    local healthcheck_address=""
    if [ -f "$CONTENT_HOSTNAME_FILE" ]; then
        content_address=$(cat "$CONTENT_HOSTNAME_FILE" | tr -d '\n')
    fi
    if [ -f "$HEALTHCHECK_HOSTNAME_FILE" ]; then
        healthcheck_address=$(cat "$HEALTHCHECK_HOSTNAME_FILE" | tr -d '\n')
    fi

    # WordPress health, last post and sites: cached for CACHE_TTL seconds
    local wordpress_ok wp_status last_post sites
    wordpress_facts
    wordpress_ok="$WP_OK"
    wp_status="$WP_STATUS"
    last_post="$LAST_POST"
    sites="$SITES"

    # Read version and start time
    local version
    version=$(get_version)
    local started=0
    if [ -f "$STARTED_FILE" ]; then
        started=$(cat "$STARTED_FILE" | tr -d '\n')
    fi

    # Calculate uptime
    local now
    now=$(date +%s)
    local uptime=$((now - started))

    # Collect pending messages
    local messages="[]"
    if [ -d "$MESSAGES_DIR" ] && [ "$(ls -A "$MESSAGES_DIR" 2>/dev/null)" ]; then
        messages="["
        local first=true
        for f in "$MESSAGES_DIR"/*.json; do
            [ -f "$f" ] || continue
            if [ "$first" = true ]; then
                first=false
            else
                messages="$messages,"
            fi
            messages="$messages$(cat "$f")"
        done
        messages="$messages]"
    fi

    # Build JSON response
    local body
    body=$(printf '{"status":"%s","content_address":"%s","healthcheck_address":"%s","version":"%s","started":%s,"wordpress":%s,"last_post":%s,"sites":%s,"uptime_seconds":%s,"messages":%s}' \
        "$wp_status" \
        "$content_address" \
        "$healthcheck_address" \
        "$version" \
        "$started" \
        "$wordpress_ok" \
        "$last_post" \
        "$sites" \
        "$uptime" \
        "$messages")

    local len=${#body}
    STATUS=200
    printf "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" "$len" "$body"
}

# Handle POST — store OnionHeaven message
handle_post() {
    local content_length="$1"
    local body=""

    if [ "$content_length" -gt "$MAX_REQUEST_BODY" ] 2>/dev/null; then
        STATUS=413
        printf "HTTP/1.0 413 Request Entity Too Large\r\nContent-Type: text/plain\r\nContent-Length: 22\r\n\r\nRequest body too large"
        return
    fi

    if [ "$content_length" -gt 0 ] 2>/dev/null; then
        body=$(dd bs=1 count="$content_length" 2>/dev/null)
    fi

    if [ -z "$body" ]; then
        STATUS="400 empty body"
        printf "HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: 11\r\n\r\nEmpty body."
        return
    fi

    # Enforce cap before writing new message
    enforce_message_cap

    # Save message with timestamp-based filename
    local ts
    ts=$(date +%s%N 2>/dev/null || date +%s)
    echo "$body" > "$MESSAGES_DIR/${ts}.json"

    local reply='{"stored":true}'
    local len=${#reply}
    STATUS="200 stored"
    printf "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" "$len" "$reply"
}

# Handle a single HTTP request (called by socat via --handle-request)
handle_request() {
    local request_line=""
    local content_length=0
    local line=""
    local method=""
    local path=""
    local log_path=""

    # Read request line (e.g. "GET / HTTP/1.1"). `cut -s` so a line with no
    # space yields an empty path instead of the whole line.
    read -r request_line
    request_line=$(printf '%s' "$request_line" | tr -d '\r')
    method=$(printf '%s' "$request_line" | cut -d' ' -f1)
    path=$(printf '%s' "$request_line" | cut -s -d' ' -f2)

    case "$method" in
        ""|*[!A-Z]*) method="" ;;
    esac
    if [ -z "$method" ]; then
        # Not an HTTP request line (a probe, a closed connection, garbage).
        # Logged BEFORE the reply here: the peer may already be gone, and a
        # write to a closed socket ends this handler.
        log_request "-" "'$(log_safe "$request_line")'" "400 malformed request line"
        printf "HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: 11\r\n\r\nBad Request"
        return
    fi
    log_path=$(log_safe "${path:-/}")

    # Read headers
    while read -r line; do
        line=$(echo "$line" | tr -d '\r')
        [ -z "$line" ] && break
        case "$line" in
            Content-Length:*|content-length:*)
                content_length=$(echo "$line" | cut -d: -f2 | tr -d ' ')
                ;;
        esac
    done

    STATUS=""
    if [ "$method" = "GET" ]; then
        handle_get
    elif [ "$method" = "POST" ]; then
        handle_post "$content_length"
    else
        STATUS=405
        printf "HTTP/1.0 405 Method Not Allowed\r\nContent-Type: text/plain\r\nContent-Length: 18\r\n\r\nMethod Not Allowed"
    fi
    # Response written; now the log line (never before, see log_request).
    log_request "$method" "$log_path" "${STATUS:-?}"
}

# Dispatch: when called with --handle-request, handle a single request from stdin/stdout
if [ "$1" = "--handle-request" ]; then
    handle_request
    exit 0
fi

# Main: record start time, create directories, start socat listener.
# -T: a connection idle for 30 s is closed (each is a root sh; a client
# holding thousands open held thousands of them); max-children caps them
# (issue #393). The Dockerfile checks the image's socat knows the option.
echo "Healthcheck server starting on port $HEALTHCHECK_PORT..."
date +%s > "$STARTED_FILE"
mkdir -p "$MESSAGES_DIR"
mkdir -p "$CACHE_DIR"
exec socat -T 30 TCP-LISTEN:${HEALTHCHECK_PORT},reuseaddr,fork,max-children=64 SYSTEM:"sh /healthcheck-server.sh --handle-request"
