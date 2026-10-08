<?php
/**
 * Plugin Name: OnionPress Tor Proxy
 * Description: Routes all outbound WordPress HTTP requests through the Tor SOCKS proxy to prevent clearnet leaks.
 * Version: 1.1.0
 * Author: OnionPress
 * License: AGPL-3.0
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Hostnames that live inside the Docker network and must NOT be sent through
 * Tor. Proxying these would ask the Tor network to resolve a name that only
 * exists on the compose bridge — it can only fail. wp-cron loopbacks and
 * container-to-container calls land here.
 */
function onionpress_is_local_host( $host ) {
    $host = strtolower( (string) $host );
    if ( '' === $host ) {
        return false;
    }
    $local = array(
        'localhost', '127.0.0.1', '::1', '[::1]',
        'wordpress', 'db', 'onionpress-tor', 'onionheaven',
    );
    if ( in_array( $host, $local, true ) ) {
        return true;
    }
    return (bool) preg_match( '/^127\./', $host );
}

/**
 * Hosts that serve WordPress core/plugin/theme updates.
 *
 * These get two special treatments below: they ride the onionheaven Tor
 * daemon rather than the one hosting the user's onion service, and they skip
 * wp_http_validate_url()'s DNS check.
 */
function onionpress_is_update_host( $host ) {
    $host = strtolower( (string) $host );
    foreach ( array( 'api.wordpress.org', 'downloads.wordpress.org', 'downloads.w.org', 'api.w.org' ) as $h ) {
        if ( $host === $h ) {
            return true;
        }
    }
    return false;
}

/**
 * Pick the Tor SOCKS proxy for a given host.
 *
 * Bulk transfers (a ~30 MB core zip) go via onionheaven: onionpress-tor is
 * also publishing the user's onion service, and a long download competes with
 * inbound visitor circuits. Everything else keeps using onionpress-tor, which
 * is where this plugin has always pointed.
 *
 * Falls back to the other daemon if the preferred one is not accepting
 * connections. It never falls back to a direct connection — no proxy means no
 * request, enforced by the pre_http_request guard below.
 */
function onionpress_socks_proxy_for( $host ) {
    static $cache = array();

    $prefer = onionpress_is_update_host( $host )
        ? array( 'onionheaven', 'onionpress-tor' )
        : array( 'onionpress-tor', 'onionheaven' );
    $key = $prefer[0];

    if ( isset( $cache[ $key ] ) ) {
        return $cache[ $key ];
    }
    foreach ( $prefer as $candidate ) {
        $sock = @fsockopen( $candidate, 9050, $errno, $errstr, 3 );
        if ( $sock ) {
            fclose( $sock );
            return $cache[ $key ] = 'socks5h://' . $candidate . ':9050';
        }
    }
    return $cache[ $key ] = '';
}

/**
 * Force outbound WordPress HTTP API requests through the Tor SOCKS proxy.
 * CURLPROXY_SOCKS5_HOSTNAME resolves the hostname at the proxy, so there is
 * no clearnet DNS lookup either.
 *
 * NOTE: this hook only covers the WordPress HTTP API. wp-cli's own `core
 * check-update` / `core update` / `core verify-checksums` do NOT use it —
 * they call WP_CLI\Utils\http_request() and go straight to wordpress.org over
 * clearnet. Anything in the container that needs core-update data must go
 * through onionpress-core-update.php instead (shipped in the image at
 * /usr/local/lib/, run via `wp eval-file`), which keeps its own copy of this
 * routing logic because it can run before this mu-plugin is installed.
 */
add_action( 'http_api_curl', function ( $handle, $parsed_args = array(), $url = '' ) {
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( onionpress_is_local_host( $host ) ) {
        return;
    }

    $proxy = onionpress_socks_proxy_for( $host );
    if ( '' === $proxy ) {
        return;   // pre_http_request below has already refused the request
    }

    curl_setopt( $handle, CURLOPT_PROXY, $proxy );
    curl_setopt( $handle, CURLOPT_PROXYTYPE, CURLPROXY_SOCKS5_HOSTNAME );
    // Measured circuit-build time to other onion services regularly runs
    // 15-30s cold — 15s was timing out real, reachable destinations before
    // the connection even completed (e.g. the Following feature's feed
    // checks flagging live sites as failed). 45s gives headroom without
    // hanging forever on a truly dead circuit.
    curl_setopt( $handle, CURLOPT_CONNECTTIMEOUT, 45 );

    // Tor is slower than direct, so raise short timeouts — but never LOWER a
    // caller that deliberately asked for more. This used to be a flat
    // CURLOPT_TIMEOUT of 30, which silently capped every large transfer
    // (core zips, social-archive media) at 30 seconds regardless of the
    // timeout the caller passed to wp_remote_get(). Floor raised to 60 to
    // stay above the CONNECTTIMEOUT above — a 45s connect budget inside a
    // 30s total timeout could never succeed.
    $timeout = isset( $parsed_args['timeout'] ) ? (int) $parsed_args['timeout'] : 0;
    curl_setopt( $handle, CURLOPT_TIMEOUT, max( 60, $timeout ) );
}, 10, 3 );

/**
 * Refuse any non-local request we cannot guarantee is proxied.
 *
 * Two ways that happens: the curl transport is missing (WordPress would fall
 * back to fsockopen, which the hook above cannot touch — a silent clearnet
 * leak), or no Tor daemon is accepting connections. Failing the request is
 * the correct outcome for this project; leaking is not.
 */
add_filter( 'pre_http_request', function ( $pre, $args, $url ) {
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( '' === $host || onionpress_is_local_host( $host ) ) {
        return $pre;
    }
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error(
            'onionpress_no_curl',
            'OnionPress: refusing to request ' . $host . ' — no curl transport, cannot guarantee Tor routing'
        );
    }
    if ( '' === onionpress_socks_proxy_for( $host ) ) {
        return new WP_Error(
            'onionpress_no_tor',
            'OnionPress: refusing to request ' . $host . ' — no Tor SOCKS proxy reachable'
        );
    }
    return $pre;
}, 10, 3 );

/**
 * Let .onion and update URLs through WordPress's safe-request URL check.
 *
 * wp_http_validate_url() (applied whenever reject_unsafe_urls is set,
 * e.g. by fetch_feed()/wp_safe_remote_get()) resolves the host with
 * gethostbyname() and rejects the URL when resolution fails. A .onion
 * host can never resolve in clearnet DNS, so every safe request to an
 * onion service fails before the transport runs. The check exists to
 * block SSRF against local/private addresses — moot here, because the
 * http_api_curl hook above forces the request through the Tor SOCKS proxy
 * with proxy-side name resolution: it egresses via the Tor network and
 * cannot reach anything on the local network.
 *
 * The same reasoning covers the wordpress.org update hosts, and there the DNS
 * lookup is actively harmful: WordPress's automatic updater downloads core
 * with wp_safe_remote_get(), so a clearnet gethostbyname() for
 * downloads.wordpress.org would both leak the query and fail outright
 * whenever the VM's resolver is down.
 */
add_filter( 'http_request_args', function ( $args, $url ) {
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( '' === $host ) {
        return $args;
    }
    if ( '.onion' === substr( $host, -6 ) || onionpress_is_update_host( $host ) ) {
        $args['reject_unsafe_urls'] = false;
    }
    return $args;
}, 10, 2 );
