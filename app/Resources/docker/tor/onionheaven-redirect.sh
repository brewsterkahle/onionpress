#!/bin/sh
# OnionHeaven 302 Redirect Service
# Listens on port 8082 via socat, reads Host header, and returns 302 redirect
# to the Internet Archive Wayback Machine .onion mirror.
#
# When OnionHeaven takes over a failed .onion address, Tor routes incoming
# connections here. Visitors get redirected to the archived version of the site.

REDIRECT_PORT=8082
WAYBACK_ONION="web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"
LOG_FIELD_MAX=200  # longest request path / host / request line we log

# Request logging (issue #74). One line per handled request, to STDERR:
# under socat's --handle-request, stdout IS the client socket, while stderr
# is inherited from socat and lands in the container log (which the
# analytics upload ships to OnionHome). Called after the response has been
# written, so a slow or closed log destination can never delay or break a
# response, and it must never fail the handler.
log_request() {
    # $1=method $2=path $3=host $4=outcome (status and redirect target)
    printf '[%s] redirect: %s %s Host: %s -> %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$3" "$4" >&2 || true
}

# Client-supplied text, made safe for a log line: printable ASCII only
# (no CR/LF, so a request cannot forge log lines) and bounded in length.
log_safe() {
    printf '%s' "$1" | LC_ALL=C tr -cd '\041-\176' | cut -c1-"$LOG_FIELD_MAX"
}

# Client-supplied text in the redirect URL: printable ASCII without spaces,
# so it stays on the Location header's line, and the characters a URL may
# not carry literally percent-encoded. '%' is left alone: browsers send the
# path already encoded.
url_safe() {
    printf '%s' "$1" | LC_ALL=C tr -cd '\041-\176' | LC_ALL=C sed \
        -e 's/"/%22/g' -e 's/</%3C/g' -e 's/>/%3E/g' -e 's/\\/%5C/g' \
        -e 's/\^/%5E/g' -e 's/`/%60/g' -e 's/{/%7B/g' -e 's/|/%7C/g' -e 's/}/%7D/g'
}

# Text for the HTML page: &, <, >, " and ' as character references.
html_escape() {
    printf '%s' "$1" | LC_ALL=C sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&#39;/g"
}

# Handle a single HTTP request (called by socat via --handle-request)
handle_request() {
    local request_line=""
    local host=""
    local path="/"
    local line=""
    local method=""
    local log_path=""
    local log_host=""

    # Read request line (e.g., "GET /some-page HTTP/1.1"). `cut -s` so a
    # line with no space yields an empty path (defaulted to / below)
    # instead of the whole line.
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
        log_request "-" "'$(log_safe "$request_line")'" "-" "400 malformed request line"
        printf "HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: 11\r\nConnection: close\r\n\r\nBad Request"
        return
    fi

    # Default path to / if empty
    if [ -z "$path" ]; then
        path="/"
    fi
    log_path=$(log_safe "$path")

    # Read headers to find Host. printf, not echo: dash's echo (Debian's
    # sh) turns a "\n" the client sent into a real line feed, which then
    # rode into the Location header.
    while read -r line; do
        line=$(printf '%s\n' "$line" | tr -d '\r')
        [ -z "$line" ] && break
        case "$line" in
            Host:*|host:*)
                host=$(printf '%s\n' "$line" | cut -d: -f2 | tr -d ' ')
                ;;
        esac
    done

    log_host=$(log_safe "$host")

    if [ -z "$host" ]; then
        # No Host header — return a simple error
        local body="No Host header provided."
        local len=${#body}
        printf "HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" "$len" "$body"
        log_request "$method" "$log_path" "-" "400 no Host header"
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
                    status=$(sqlite3 /var/lib/onionheaven/registry.db \
                        "SELECT status FROM registry WHERE content_address='${host}' LIMIT 1" 2>/dev/null || echo "")
                    if [ "$status" = "online" ]; then
                        # Address has been released — drop connection (circuit breaker)
                        log_request "$method" "$log_path" "$log_host" "dropped (address released, status=online)"
                        exit 0
                    fi
                    ;;
            esac
        fi
    fi

    # Build Wayback Machine URL: http://<wayback-onion>/web/http://<host><path>
    # Host and path are whatever the client sent, and not every client is a
    # browser that percent-encodes: url_safe for the header, and on top of
    # it html_escape for the page.
    local wayback_url page_host page_url
    wayback_url="http://${WAYBACK_ONION}/web/http://$(url_safe "$host")$(url_safe "$path")"
    page_host=$(html_escape "$(url_safe "$host")")
    page_url=$(html_escape "$wayback_url")

    # Build HTML body for browsers that don't follow redirects automatically.
    # All ASCII, so ${#body} is its length in bytes in any shell and locale.
    local body="<html><head><title>Moved</title></head><body>"
    body="${body}<h1>This site has been archived</h1>"
    body="${body}<p>The onion service at <code>${page_host}</code> is currently offline.</p>"
    body="${body}<p>An archived copy is available at:<br>"
    body="${body}<a href=\"${page_url}\">${page_url}</a></p>"
    body="${body}</body></html>"
    local len=${#body}

    # X-OnionHeaven-Takeover is an explicit, version-independent marker
    # of "this response is the takeover, not the real site." Clients use
    # it as defense-in-depth alongside the 302 status check, so a future
    # change to status code / redirect shape doesn't silently flip
    # takeover responses past client reachability gates.
    printf "HTTP/1.0 302 Found\r\nLocation: %s\r\nContent-Type: text/html\r\nContent-Length: %d\r\nX-OnionHeaven-Takeover: 1\r\nConnection: close\r\n\r\n%s" "$wayback_url" "$len" "$body"

    # Response written; now the log line (never before, see log_request).
    # The target repeats host and path, so log just the fixed prefix of it.
    log_request "$method" "$log_path" "$log_host" "302 http://${WAYBACK_ONION}/web/http://${log_host}${log_path}"

    # Record redirect timestamp in DB (best-effort, don't block response)
    # Updates all rows for this content_address since the redirect applies to the address, not a specific instance.
    if [ -n "$host" ] && command -v sqlite3 >/dev/null 2>&1; then
        # Validate host: strip valid base32 + dots, check nothing remains
        safe=$(printf '%s' "$host" | LC_ALL=C tr -d 'a-z2-7.')
        if [ -z "$safe" ]; then
            case "$host" in
                *.onion)
                    sqlite3 /var/lib/onionheaven/registry.db \
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
# -T: a connection idle for 30 s is closed; max-children caps the root
# shells one client can hold open (issue #393).
exec socat -T 30 TCP-LISTEN:${REDIRECT_PORT},reuseaddr,fork,max-children=64 SYSTEM:"sh /onionheaven-redirect.sh --handle-request"
