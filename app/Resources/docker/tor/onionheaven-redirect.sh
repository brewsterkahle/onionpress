#!/bin/sh
# OnionHeaven 302 Redirect Service
# Listens on port 8082 via socat, reads Host header, and returns 302 redirect
# to the Internet Archive Wayback Machine .onion mirror.
#
# When OnionHeaven takes over a failed .onion address, Tor routes incoming
# connections here. Visitors get redirected to the archived version of the site.

REDIRECT_PORT=8082
WAYBACK_ONION="web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# Handle a single HTTP request (called by socat via --handle-request)
handle_request() {
    local request_line=""
    local host=""
    local path="/"
    local line=""

    # Read request line (e.g., "GET /some-page HTTP/1.1")
    read -r request_line
    request_line=$(echo "$request_line" | tr -d '\r')
    path=$(echo "$request_line" | cut -d' ' -f2)

    # Default path to / if empty
    if [ -z "$path" ]; then
        path="/"
    fi

    # Read headers to find Host
    while read -r line; do
        line=$(echo "$line" | tr -d '\r')
        [ -z "$line" ] && break
        case "$line" in
            Host:*|host:*)
                host=$(echo "$line" | cut -d: -f2 | tr -d ' ')
                ;;
        esac
    done

    if [ -z "$host" ]; then
        # No Host header — return a simple error
        local body="No Host header provided."
        local len=${#body}
        printf "HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" "$len" "$body"
        return
    fi

    # Circuit breaker: if this address has been released (status=online),
    # drop the connection immediately. A stale descriptor may still
    # route traffic here after release — closing the TCP connection forces
    # the Tor client to mark the circuit as failed and rebuild, which may
    # cause it to fetch a fresh descriptor and reach the real service.
    # See: https://github.com/brewsterkahle/onionpress/issues/114
    if command -v sqlite3 >/dev/null 2>&1; then
        safe=$(printf '%s' "$host" | LC_ALL=C tr -d 'a-z2-7.')
        if [ -z "$safe" ]; then
            case "$host" in
                *.onion)
                    status=$(sqlite3 /var/lib/onionpress/onionheaven/registry.db \
                        "SELECT status FROM registry WHERE content_address='${host}' LIMIT 1" 2>/dev/null || echo "")
                    if [ "$status" = "online" ]; then
                        # Address has been released — drop connection (circuit breaker)
                        exit 0
                    fi
                    ;;
            esac
        fi
    fi

    # Build Wayback Machine URL: http://<wayback-onion>/web/http://<host><path>
    local wayback_url="http://${WAYBACK_ONION}/web/http://${host}${path}"

    # Build HTML body for browsers that don't follow redirects automatically
    local body="<html><head><title>Moved</title></head><body>"
    body="${body}<h1>This site has been archived</h1>"
    body="${body}<p>The onion service at <code>${host}</code> is currently offline.</p>"
    body="${body}<p>An archived copy is available at:<br>"
    body="${body}<a href=\"${wayback_url}\">${wayback_url}</a></p>"
    body="${body}</body></html>"
    local len=${#body}

    # X-OnionHeaven-Takeover is an explicit, version-independent marker
    # of "this response is the takeover, not the real site." Clients use
    # it as defense-in-depth alongside the 302 status check, so a future
    # change to status code / redirect shape doesn't silently flip
    # takeover responses past client reachability gates.
    printf "HTTP/1.0 302 Found\r\nLocation: %s\r\nContent-Type: text/html\r\nContent-Length: %d\r\nX-OnionHeaven-Takeover: 1\r\nConnection: close\r\n\r\n%s" "$wayback_url" "$len" "$body"

    # Record redirect timestamp in DB (best-effort, don't block response)
    # Updates all rows for this content_address since the redirect applies to the address, not a specific instance.
    if [ -n "$host" ] && command -v sqlite3 >/dev/null 2>&1; then
        # Validate host: strip valid base32 + dots, check nothing remains
        safe=$(printf '%s' "$host" | LC_ALL=C tr -d 'a-z2-7.')
        if [ -z "$safe" ]; then
            case "$host" in
                *.onion)
                    sqlite3 /var/lib/onionpress/onionheaven/registry.db \
                        "UPDATE registry SET last_redirect=datetime('now') WHERE content_address='${host}'" 2>/dev/null || true
                    ;;
            esac
        fi
    fi
}

# Dispatch: when called with --handle-request, handle a single request
if [ "$1" = "--handle-request" ]; then
    handle_request
    exit 0
fi

# Main: start socat listener
echo "OnionHeaven redirect service starting on port $REDIRECT_PORT..."
exec socat TCP-LISTEN:${REDIRECT_PORT},reuseaddr,fork SYSTEM:"sh /onionheaven-redirect.sh --handle-request"
