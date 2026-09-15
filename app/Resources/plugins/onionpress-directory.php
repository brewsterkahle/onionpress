<?php
/**
 * Plugin Name: OnionPress Directory
 * Description: Turns this instance into a naming directory — resolves
 *              GET /follow?name=NAME and GET /NAME lookups against the
 *              local onionnames registry on the tor container, and 302s
 *              to the target .onion when the name resolves to a
 *              different address. No-op when this instance is not
 *              OnionHome: the local registry endpoint is source-IP
 *              protected AND returns 404 off-OnionHome anyway.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// OnionHome's .onion address. Duplicated from app/Resources/docker/tor/
// web-server.py so the plugin has the same notion of "are we OnionHome?"
// without needing to read from disk.
if ( ! defined( 'ONIONPRESS_ONIONHOME_ADDRESS' ) ) {
    define(
        'ONIONPRESS_ONIONHOME_ADDRESS',
        'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion'
    );
}
if ( ! defined( 'ONIONPRESS_CLEARNET_HOST' ) ) {
    define( 'ONIONPRESS_CLEARNET_HOST', 'onionpress.org' );
}

// Hosts on which this plugin activates. Anywhere else we exit early.
function onionpress_directory_is_onionhome_host( $host ) {
    $host = strtolower( (string) $host );
    if ( strpos( $host, ':' ) !== false ) {
        $host = substr( $host, 0, strpos( $host, ':' ) );
    }
    return (
        $host === ONIONPRESS_ONIONHOME_ADDRESS
        || $host === ONIONPRESS_CLEARNET_HOST
        || $host === 'www.' . ONIONPRESS_CLEARNET_HOST
    );
}

/**
 * The host this request arrived on: lower-cased, port stripped.
 *
 * Extracted because the same three lines were hand-rolled in two places and
 * only one of them kept the variable it assigned. The other read an
 * undefined $own, so its clearnet check was constantly false and every
 * clearnet visitor fell through to a redirect at a raw .onion URL their
 * browser cannot open. One definition means the two callers cannot drift
 * apart again.
 */
function onionpress_directory_request_host() {
    $host = strtolower( (string) ( $_SERVER['HTTP_HOST'] ?? '' ) );
    if ( strpos( $host, ':' ) !== false ) {
        $host = substr( $host, 0, strpos( $host, ':' ) );
    }
    return $host;
}

/**
 * True when this request came in over the clearnet bridge (onionpress.org
 * via the Cloudflare tunnel) rather than over Tor.
 */
function onionpress_directory_request_is_clearnet() {
    $host = onionpress_directory_request_host();
    return (
        $host === ONIONPRESS_CLEARNET_HOST
        || $host === 'www.' . ONIONPRESS_CLEARNET_HOST
    );
}

/**
 * Path segments that are WordPress's, never an onionname.
 *
 * The first segment of any URL is now treated as a candidate name (deep
 * paths are carried through to the target), so the handful of segments
 * that unambiguously belong to this site are excluded before we ask the
 * registry about them.
 */
function onionpress_directory_is_reserved_segment( $segment ) {
    $reserved = array(
        'wp-admin', 'wp-json', 'wp-content', 'wp-includes', 'wp-login',
        'feed', 'follow', 'xmlrpc.php',
    );
    return in_array( strtolower( (string) $segment ), $reserved, true );
}

/**
 * The URL a claimed name resolves to: the target's onion ROOT, plus
 * whatever path followed the name, plus the original query string.
 *
 * A name is registry state, not site state. The site is served at the onion
 * root and has no /<name>/ path of its own, so appending the name — which
 * this did until 2026-08-17 — 404s every claimed name. Carrying the
 * remainder through is the other half: a name is only worth having if you
 * can hand someone a link to a page, not just to a homepage.
 */
function onionpress_directory_target_url( $addr, $suffix = '' ) {
    $url = 'http://' . $addr . '/' . ltrim( (string) $suffix, '/' );
    $query = (string) ( $_SERVER['QUERY_STRING'] ?? '' );
    if ( $query !== '' ) {
        $url .= '?' . $query;
    }
    return $url;
}

/**
 * Read this instance's own .onion address from the shared volume the
 * launcher populates. Returns lower-case .onion or null.
 */
function onionpress_directory_own_address() {
    $addr_file = '/var/lib/onionpress/onion_address';
    if ( ! is_readable( $addr_file ) ) {
        return null;
    }
    $addr = strtolower( trim( (string) @file_get_contents( $addr_file ) ) );
    if ( ! preg_match( '/^[a-z2-7]{56}\.onion$/', $addr ) ) {
        return null;
    }
    return $addr;
}

/**
 * Look up NAME in the registry. Returns associative array on success
 * (onionname, onionaddress, url, registered_at, last_seen_at) or null
 * on miss / error.
 *
 * On OnionHome itself the registry lives in the local tor container,
 * so we curl onionpress-tor:8083 directly. On every OTHER instance the
 * local web-server.py refuses /api/name/* by design, so we have to
 * reach the canonical OnionHome over Tor (via SOCKS through the local
 * tor container). Without this branch, every off-OnionHome follow-by-
 * name attempt 404'd silently.
 *
 * Direct curl (not wp_remote_*) so onionpress-tor-proxy's SOCKS routing
 * doesn't try to tunnel a docker-internal hop through Tor.
 */
function onionpress_directory_lookup( $name ) {
    if ( ! is_string( $name ) || $name === '' ) {
        return null;
    }
    if ( onionpress_directory_own_address() === ONIONPRESS_ONIONHOME_ADDRESS ) {
        $url  = 'http://onionpress-tor:8083/api/name/lookup/' . rawurlencode( $name );
        $opts = array(
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 5,
            CURLOPT_CONNECTTIMEOUT => 2,
        );
    } else {
        $url  = 'http://' . ONIONPRESS_ONIONHOME_ADDRESS
              . ':8083/api/name/lookup/' . rawurlencode( $name );
        $opts = array(
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 30,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_PROXY          => 'socks5h://onionheaven:9050',
        );
    }
    $ch = curl_init( $url );
    if ( ! $ch ) {
        return null;
    }
    curl_setopt_array( $ch, $opts );
    $body = curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    if ( $code !== 200 || ! is_string( $body ) ) {
        return null;
    }
    $data = json_decode( $body, true );
    if ( ! is_array( $data ) || empty( $data['onionaddress'] ) ) {
        return null;
    }
    return $data;
}

/**
 * Follow-by-name landing page. Served by OnionHome (or the clearnet face)
 * so it works reliably regardless of whether the target's theme has its
 * own /follow page — there's no guarantee alice's site has one, and
 * sending the user there blind would just 404 on a vanilla install.
 *
 * The page shows the target's .onion address in a copyable block. When
 * we render on onionpress.org we add noindex headers; on the onion side
 * we don't bother since search indexing of onion services isn't a thing.
 */
function onionpress_directory_handle_follow_by_name( $name ) {
    $info = onionpress_directory_lookup( $name );
    if ( ! $info ) {
        status_header( 404 );
        nocache_headers();
        header( 'Content-Type: text/html; charset=utf-8' );
        echo '<!doctype html><meta charset="utf-8"><title>Onionname not found</title>';
        echo '<body style="font-family:system-ui,sans-serif;padding:2em">';
        echo '<h1>Onionname not found</h1>';
        echo '<p>No site is registered for <code>' . esc_html( $name ) . '</code>.</p>';
        echo '</body>';
        exit;
    }

    $is_clearnet = onionpress_directory_request_is_clearnet();

    status_header( 200 );
    header( 'Content-Type: text/html; charset=utf-8' );
    if ( $is_clearnet ) {
        header( 'X-Robots-Tag: noindex, nofollow' );
    }

    $name_h = esc_html( $info['onionname'] );
    $addr_h = esc_html( $info['onionaddress'] );
    $site_url = 'http://' . $info['onionaddress'] . '/';
    $deep_link = 'onionpress://follow/' . $info['onionaddress'];

    echo '<!doctype html><meta charset="utf-8">';
    echo '<title>Follow @' . $name_h . ' — OnionPress</title>';
    if ( $is_clearnet ) {
        echo '<meta name="robots" content="noindex, nofollow">';
    }
    echo '<body style="font-family:system-ui,sans-serif;padding:2em;max-width:640px;margin:auto">';
    echo '<h1>Follow @' . $name_h . '</h1>';
    echo '<p>To follow this site, open it in <a href="https://www.torproject.org/download/">Tor Browser</a>:</p>';
    echo '<p style="background:#f3f4f6;padding:1em;border-radius:6px;word-break:break-all;font-family:ui-monospace,monospace;font-size:14px">' . $addr_h . '</p>';
    echo '<p>Direct link: <a href="' . esc_url( $site_url ) . '">' . esc_html( $site_url ) . '</a></p>';
    echo '<p>If you run OnionPress: <a href="' . esc_url( $deep_link ) . '">+ Follow @' . $name_h . '</a></p>';
    echo '</body>';
    exit;
}

/**
 * Bare-NAME redirect: if the incoming path is a single segment that could
 * be an onionname AND the registry has an entry AND the target is some
 * OTHER .onion, 302 to that site. If the target is this instance, fall
 * through so WP multisite serves the local blog.
 */
function onionpress_directory_handle_name_lookup( $name, $suffix = '' ) {
    // Cheap client-side filter — avoids a curl hop for obviously-invalid
    // segments. Mirrors the server's validate_name rules.
    if (
        strlen( $name ) < 5 || strlen( $name ) > 40
        || ! preg_match( '/^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$/', $name )
        || preg_match( '/^[0-9]+$/', $name )
    ) {
        return;
    }
    $info = onionpress_directory_lookup( $name );
    if ( ! $info ) {
        return; // Fall through; WP may serve a local blog or 404.
    }

    // If the name is registered to this very instance, let WP handle the
    // request as it would normally (the multisite path will match this
    // user's blog). Compare to our own onion address rather than to
    // HTTP_HOST: on clearnet (onionpress.org via Cloudflare tunnel) the
    // host is "onionpress.org" and never matches the onion address, so
    // the old HTTP_HOST check sent clearnet visitors to the Tor-only
    // stub even when the name was our own subsite.
    $own_addr = onionpress_directory_own_address();
    if ( $own_addr && strtolower( $own_addr ) === strtolower( $info['onionaddress'] ) ) {
        return;
    }

    // Clearnet bridge special case: if the visitor came in on
    // onionpress.org we don't ship them to a raw .onion URL in their
    // clearnet browser (which would just error). Surface a simple page
    // pointing at Tor Browser instead. Indexers explicitly blocked.
    if ( onionpress_directory_request_is_clearnet() ) {
        status_header( 200 );
        header( 'X-Robots-Tag: noindex, nofollow' );
        header( 'Content-Type: text/html; charset=utf-8' );
        $name_h  = esc_html( $info['onionname'] );
        $addr_h  = esc_html( $info['onionaddress'] );
        $addr_a  = esc_attr( $info['onionaddress'] );
        $onion_h = esc_html(
            onionpress_directory_target_url( $info['onionaddress'], $suffix )
        );
        echo '<!doctype html><meta charset="utf-8"><title>@' . $name_h . ' — OnionPress</title>';
        echo '<meta name="robots" content="noindex, nofollow">';
        echo '<body style="font-family:system-ui,sans-serif;padding:2em;max-width:640px;margin:auto">';
        echo '<h1>@' . $name_h . '</h1>';
        echo '<p>This OnionPress site publishes as a Tor onion service. To read it you need';
        echo ' <a href="https://www.torproject.org/download/">Tor Browser</a> and the address:</p>';
        echo '<p style="background:#f3f4f6;padding:1em;border-radius:6px;word-break:break-all;font-family:ui-monospace,monospace">' . $addr_h . '</p>';
        echo '<p>Direct link in Tor Browser: <code style="word-break:break-all">' . $onion_h . '</code></p>';
        echo '</body>';
        exit;
    }

    // Onion-side redirect: point at the target's ROOT, carrying any path
    // that followed the name. This used to append /<name>/ on the theory
    // that onionpress-user-path.php would rewrite it to an author archive,
    // but that rewriter only runs on a network-root multisite install
    // (onionpress-user-path.php gates on blog_id 1), so on a self-hosted
    // node serving one site at the root every claimed name 404'd.
    wp_redirect(
        onionpress_directory_target_url( $info['onionaddress'], $suffix ),
        302
    );
    exit;
}

/**
 * Dispatch on every request. Runs AFTER WP has parsed the URL into
 * query vars, which is early enough to intercept before the template
 * layer picks up the blog-path.
 */
add_action( 'parse_request', function ( $wp ) {
    if ( ! onionpress_directory_is_onionhome_host( $_SERVER['HTTP_HOST'] ?? '' ) ) {
        return;
    }

    // Only respond to bare GETs. POSTs, admin, XML-RPC, etc. pass through.
    if ( ( $_SERVER['REQUEST_METHOD'] ?? 'GET' ) !== 'GET' ) {
        return;
    }

    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return;
    }
    $path = trim( $path, '/' );

    // /follow?name=NAME — the named-follow entry point linked by theme headers.
    if ( $path === 'follow' && ! empty( $_GET['name'] ) ) {
        onionpress_directory_handle_follow_by_name(
            sanitize_text_field( $_GET['name'] )
        );
        return;
    }

    // /NAME[/REST] — the first segment is the candidate onionname and
    // everything after it is carried through to the target unchanged, so
    // /alice/posts/hello lands on /posts/hello rather than 404ing. Deep
    // paths used to be skipped entirely, which meant a name could only ever
    // link to a homepage.
    //
    // An unregistered first segment (every ordinary WordPress URL) falls
    // through untouched: lookup returns null. The pre-filter in
    // handle_name_lookup keeps that from costing a registry hop for most of
    // them, and this dispatcher only runs on the OnionHome hosts, where the
    // registry is a local container call rather than a trip over Tor.
    if ( $path !== '' ) {
        $parts  = explode( '/', $path, 2 );
        $segment = $parts[0];
        $suffix  = isset( $parts[1] ) ? $parts[1] : '';
        if ( ! onionpress_directory_is_reserved_segment( $segment ) ) {
            onionpress_directory_handle_name_lookup( $segment, $suffix );
        }
    }
} );
