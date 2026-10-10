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
 * The trimmed request path, or null when this request isn't ours to
 * handle at all: wrong host, or not a GET. Both hooks below opened with
 * the same OnionHome-host check, GET-only check, parse_url() and trim() —
 * this is the one place that logic lives, so the two cannot drift apart.
 */
function onionpress_directory_request_path() {
    if ( ! onionpress_directory_is_onionhome_host( $_SERVER['HTTP_HOST'] ?? '' ) ) {
        return null;
    }

    // Only respond to bare GETs. POSTs, admin, XML-RPC, etc. pass through.
    if ( ( $_SERVER['REQUEST_METHOD'] ?? 'GET' ) !== 'GET' ) {
        return null;
    }

    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return null;
    }
    return trim( $path, '/' );
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
 * Called for a URL WordPress itself would 404 on. NAME is the first path
 * segment; anything after it is carried through as $suffix. If the
 * registry has an entry for NAME and it points at some OTHER .onion, 302
 * to that site (its root + $suffix). If NAME resolves to this instance,
 * or the registry has no entry at all, fall through and leave WordPress's
 * own 404 in place.
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
 * Dispatch the /follow?name=NAME entry point on every request. Runs AFTER
 * WP has parsed the URL into query vars, which is early enough to
 * intercept before the template layer picks up the blog-path.
 */
add_action( 'parse_request', function ( $wp ) {
    $path = onionpress_directory_request_path();
    if ( null === $path ) {
        return;
    }

    // /follow?name=NAME — the named-follow entry point linked by theme headers.
    if ( $path === 'follow' && ! empty( $_GET['name'] ) ) {
        onionpress_directory_handle_follow_by_name(
            sanitize_text_field( $_GET['name'] )
        );
        return;
    }
} );

/**
 * Dispatch /NAME[/REST] only once WordPress has failed to find anything to
 * serve at that URL. A URL this site can serve is never looked up, so
 * WordPress's own routes — whatever their bases, including custom
 * taxonomies, child pages and sitemaps — cost nothing here and cannot be
 * shadowed by a registered name sharing their first segment. Only a URL
 * that would otherwise 404 is offered to the registry, with the path
 * after the name carried through unchanged so /alice/posts/hello still
 * lands on /posts/hello.
 *
 * Consequence worth knowing: an existing local page, post or user archive
 * always wins over a remote name sharing its slug; a renamed post's old
 * slug does not, since wp_old_slug_redirect runs after us at priority 10.
 *
 * Relies on pretty permalinks — with plain permalinks WordPress never
 * 404s an unrecognized path, so is_404() never fires and no name would
 * resolve. OnionHome, the only host this runs on, uses pretty permalinks.
 *
 * Priority 1 so this runs before core's redirect_canonical (priority 10),
 * which otherwise guesses a nearby post for the 404 first and this code
 * never sees it.
 */
add_action( 'template_redirect', function () {
    $path = onionpress_directory_request_path();
    if ( null === $path ) {
        return;
    }

    if ( ! is_404() ) {
        return;
    }

    if ( $path === '' ) {
        return;
    }

    $parts   = explode( '/', $path, 2 );
    $segment = $parts[0];
    $suffix  = isset( $parts[1] ) ? $parts[1] : '';
    onionpress_directory_handle_name_lookup( $segment, $suffix );
}, 1 );
