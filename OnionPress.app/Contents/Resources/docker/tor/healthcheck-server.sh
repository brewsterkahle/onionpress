#!/bin/sh
# OnionPress Healthcheck Server
# Listens on port 8081 via socat, serves status JSON on GET,
# stores relay messages on POST.

HEALTHCHECK_PORT=8081
MESSAGES_DIR="/var/lib/tor/healthcheck-messages"
MESSAGES_MAX=100
MESSAGES_TTL=86400  # 24 hours in seconds
CONTENT_HOSTNAME_FILE="/var/lib/tor/hidden_service/wordpress/hostname"
HEALTHCHECK_HOSTNAME_FILE="/var/lib/tor/hidden_service/healthcheck/hostname"
VERSION_FILE="/var/lib/tor/healthcheck-version"
STARTED_FILE="/var/lib/tor/healthcheck-started"

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

    # Check WordPress health
    local wordpress_ok="false"
    local wp_status="degraded"
    if wget -q -O /dev/null --timeout=5 http://wordpress:80/ 2>/dev/null; then
        wordpress_ok="true"
        wp_status="ok"
    fi

    # Get last_post from WordPress REST API
    local last_post="null"
    local posts_json=""
    posts_json=$(wget -q -O - --timeout=5 "http://wordpress:80/wp-json/wp/v2/posts?per_page=1&orderby=date&order=desc&_fields=date" 2>/dev/null)
    if [ -n "$posts_json" ] && echo "$posts_json" | grep -q '"date"'; then
        last_post=$(echo "$posts_json" | sed -n 's/.*"date":"\([^"]*\)".*/"\1"/p' | head -1)
        if [ -z "$last_post" ]; then
            last_post="null"
        fi
    fi

    # Get sites count from multisite API
    local sites=1
    local sites_json=""
    sites_json=$(wget -q -O - --timeout=5 "http://wordpress:80/wp-json/wp/v2/sites" 2>/dev/null)
    if [ -n "$sites_json" ] && echo "$sites_json" | grep -q '"id"'; then
        sites=$(echo "$sites_json" | grep -o '"id"' | wc -l | tr -d ' ')
    fi

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
    printf "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" "$len" "$body"
}

# Handle POST — store relay message
handle_post() {
    local content_length="$1"
    local body=""

    if [ "$content_length" -gt 0 ] 2>/dev/null; then
        body=$(dd bs=1 count="$content_length" 2>/dev/null)
    fi

    if [ -z "$body" ]; then
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
    printf "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" "$len" "$reply"
}

# Handle a single HTTP request (called by socat via --handle-request)
handle_request() {
    local request_line=""
    local content_length=0
    local line=""

    # Read request line
    read -r request_line
    request_line=$(echo "$request_line" | tr -d '\r')
    method=$(echo "$request_line" | cut -d' ' -f1)

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

    if [ "$method" = "GET" ]; then
        handle_get
    elif [ "$method" = "POST" ]; then
        handle_post "$content_length"
    else
        printf "HTTP/1.0 405 Method Not Allowed\r\nContent-Type: text/plain\r\nContent-Length: 18\r\n\r\nMethod Not Allowed"
    fi
}

# Dispatch: when called with --handle-request, handle a single request from stdin/stdout
if [ "$1" = "--handle-request" ]; then
    handle_request
    exit 0
fi

# Main: record start time, create directories, start socat listener
echo "Healthcheck server starting on port $HEALTHCHECK_PORT..."
date +%s > "$STARTED_FILE"
mkdir -p "$MESSAGES_DIR"
exec socat TCP-LISTEN:${HEALTHCHECK_PORT},reuseaddr,fork SYSTEM:"sh /healthcheck-server.sh --handle-request"
