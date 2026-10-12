<?php
/**
 * Plugin Name: OnionPress Tor Proxy
 * Description: Routes all outbound WordPress HTTP requests through the Tor SOCKS proxy to prevent clearnet leaks.
 * Version: 1.2.0
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
 * No DNS lookup in the container for a host this plugin sends through Tor.
 *
 * wp_http_validate_url(), which WordPress runs on every request that sets
 * reject_unsafe_urls (wp_safe_remote_*(): oEmbed discovery and providers,
 * pingbacks and trackbacks, enclosures, fetch_feed() and the dashboard's
 * feeds, download_url() and so plugin, theme and media downloads, the block
 * editor's link previews), resolves the host with gethostbyname() in the
 * container: a DNS query outside Tor naming the site WordPress is about to
 * fetch, sent from the publisher's own network. Tor resolves the name at the
 * exit anyway (CURLPROXY_SOCKS5_HOSTNAME above). The check exists to keep a
 * request off the local network, which a Tor-routed request cannot reach:
 * the exit refuses private addresses, and a container name means nothing
 * there. A .onion never resolves in DNS at all, so the check also failed
 * every safe request to an onion service.
 *
 * So for every host that is not onionpress_is_local_host() this turns the
 * check off, and onionpress_tor_proxy_check_url() below runs
 * onionpress_remote_url_is_safe() in its place: the same rules, by the URL's
 * shape, without the lookup. .onion and wordpress.org update hosts skip it,
 * as they always have. Local hosts keep WordPress's own check: Docker's
 * resolver answers for their names, which are refused as private addresses.
 *
 * Last in line, so no later filter turns the lookup back on.
 * onionpress_follow_guard_args() (Following) runs after it and replaces this
 * check with the follow rule.
 */
function onionpress_tor_proxy_request_args( $args, $url ) {
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( '' === $host || onionpress_is_local_host( $host ) || empty( $args['reject_unsafe_urls'] ) ) {
        return $args;
    }
    $args['reject_unsafe_urls'] = false;
    if ( '.onion' !== substr( $host, -6 ) && ! onionpress_is_update_host( $host ) ) {
        $args['onionpress_check_url'] = true;
    }
    return $args;
}
add_filter( 'http_request_args', 'onionpress_tor_proxy_request_args', PHP_INT_MAX, 2 );

/**
 * pre_http_request: refuse a request that asked for the safe-URL check
 * (onionpress_tor_proxy_request_args() marked it) when its URL fails
 * onionpress_remote_url_is_safe(). WordPress's own error code and message.
 */
function onionpress_tor_proxy_check_url( $pre, $args, $url ) {
    if ( empty( $args['onionpress_check_url'] ) || onionpress_remote_url_is_safe( $url ) ) {
        return $pre;
    }
    return new WP_Error( 'http_request_failed', __( 'A valid URL was not provided.' ) );
}
add_filter( 'pre_http_request', 'onionpress_tor_proxy_check_url', 10, 3 );

/**
 * Whether $host, sent through Tor, is a public address by its shape alone:
 *
 *   - a dotted-quad IPv4 literal outside 127/8, 10/8, 0/8, 172.16/12 and
 *     192.168/16 (wp_http_validate_url()'s ranges; inside them only if the
 *     http_request_host_is_external filter says so, as in WordPress), or
 *   - a name of two or more labels whose last label is alphabetic (or an
 *     xn-- IDN label), not localhost or another name reserved for a local
 *     network, and not one onionpress_is_local_host() names.
 *
 * Everything else is refused: IPv4 in the integer, octal, hex and short
 * spellings curl and inet_aton() accept (none ends in an alphabetic label),
 * single-label names, IPv6 literals. WordPress learns what those stand for
 * from gethostbyname(); this never asks.
 */
function onionpress_remote_host_allowed( $host, $url = '' ) {
    $host = strtolower( trim( (string) $host, '.' ) );
    if ( '' === $host || strlen( $host ) > 253 || onionpress_is_local_host( $host ) ) {
        return false;
    }
    if ( preg_match( '#^(([1-9]?\d|1\d\d|25[0-5]|2[0-4]\d)\.){3}([1-9]?\d|1\d\d|25[0-5]|2[0-4]\d)$#D', $host ) ) {
        $parts = array_map( 'intval', explode( '.', $host ) );
        if ( 127 === $parts[0] || 10 === $parts[0] || 0 === $parts[0]
             || ( 172 === $parts[0] && 16 <= $parts[1] && 31 >= $parts[1] )
             || ( 192 === $parts[0] && 168 === $parts[1] ) ) {
            return (bool) apply_filters( 'http_request_host_is_external', false, $host, $url );
        }
        return true;
    }
    if ( ! preg_match( '/\.(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/D', $host ) ) {
        return false;
    }
    return ! preg_match( '/(?:^|\.)(?:localhost|localdomain|local|internal|home\.arpa)$/D', $host );
}

/**
 * wp_http_validate_url() without the DNS lookup: an http(s) URL with no
 * userinfo, a host onionpress_remote_host_allowed() accepts (or the site's
 * own), and a port in the http_allowed_safe_ports list (80, 443, 8080) or
 * the site's own.
 */
function onionpress_remote_url_is_safe( $url ) {
    if ( ! is_string( $url ) || '' === $url || is_numeric( $url ) ) {
        return false;
    }
    if ( function_exists( 'wp_kses_bad_protocol' ) ) {
        $checked = wp_kses_bad_protocol( $url, array( 'http', 'https' ) );
        if ( ! $checked || strtolower( $checked ) !== strtolower( $url ) ) {
            return false;
        }
    }
    $parsed_url = parse_url( $url );
    if ( ! $parsed_url || empty( $parsed_url['host'] ) || empty( $parsed_url['scheme'] )
         || ! in_array( strtolower( $parsed_url['scheme'] ), array( 'http', 'https' ), true )
         || isset( $parsed_url['user'] ) || isset( $parsed_url['pass'] )
         || false !== strpbrk( $parsed_url['host'], ':#?[]' ) ) {
        return false;
    }
    $host        = $parsed_url['host'];
    $parsed_home = parse_url( (string) get_option( 'home' ) );
    $same_host   = isset( $parsed_home['host'] ) && strtolower( $parsed_home['host'] ) === strtolower( $host );
    if ( ! $same_host && ! onionpress_remote_host_allowed( $host, $url ) ) {
        return false;
    }
    if ( empty( $parsed_url['port'] ) ) {
        return true;
    }
    $port          = $parsed_url['port'];
    $allowed_ports = apply_filters( 'http_allowed_safe_ports', array( 80, 443, 8080 ), $host, $url );
    if ( is_array( $allowed_ports ) && in_array( $port, $allowed_ports, true ) ) {
        return true;
    }
    return $same_host && isset( $parsed_home['port'] ) && $parsed_home['port'] === $port;
}

/**
 * Every redirect stays on the route its request took.
 *
 * http_api_curl above picks Tor or a direct connection from the URL a request
 * started with, and keeps that choice for every redirect (WordPress hands it
 * the first URL on each hop). A container-network request redirected to an
 * outside host would therefore connect there directly, over clearnet, after
 * a DNS lookup in the container; a Tor request redirected into the container
 * network would ask Tor for a name only Docker knows. So a redirect is
 * followed only from a local host to a local host, or from a Tor-routed host
 * to one onionpress_remote_host_allowed() accepts. That also stands in for
 * WordPress's own check of a safe request's redirects
 * (WP_Http::validate_redirects), which resolved each new host.
 *
 * Registered on each request's own Requests hooks, where WordPress registers
 * its redirect checks and where an exception becomes the request's
 * WP_Error. Requests runs before_request again for every hop with the same
 * hooks, so a redirected request registers the check more than once; it is
 * idempotent.
 */
function onionpress_tor_proxy_hops( $url, $headers = array(), $data = array(), $type = '', $options = array() ) {
    if ( isset( $options['hooks'] ) && is_object( $options['hooks'] )
         && method_exists( $options['hooks'], 'register' ) ) {
        $options['hooks']->register( 'requests.before_redirect', 'onionpress_tor_proxy_redirect' );
    }
}
add_action( 'requests-requests.before_request', 'onionpress_tor_proxy_hops', 10, 5 );

/**
 * Requests' before_redirect hook: refuse a redirect off its request's route.
 * $response is the hop that answered with the redirect, so its URL is on the
 * request's route (every earlier hop was held to it here).
 */
function onionpress_tor_proxy_redirect( $location, $headers = array(), $data = array(), $options = array(), $response = null ) {
    $from      = ( is_object( $response ) && isset( $response->url ) ) ? (string) $response->url : '';
    $from_host = strtolower( (string) parse_url( $from, PHP_URL_HOST ) );
    $to_host   = strtolower( (string) parse_url( (string) $location, PHP_URL_HOST ) );
    if ( '' !== $from_host && '' !== $to_host ) {
        $from_local = onionpress_is_local_host( $from_host );
        $to_local   = onionpress_is_local_host( $to_host );
        if ( $from_local && $to_local ) {
            return;
        }
        if ( ! $from_local && ! $to_local && onionpress_remote_host_allowed( $to_host, (string) $location ) ) {
            return;
        }
    }
    $exception = class_exists( 'WpOrg\Requests\Exception' ) ? 'WpOrg\Requests\Exception' : 'Requests_Exception';
    throw new $exception( 'OnionPress: refusing a redirect off the route its request took (Tor, or the container network)',
        'onionpress_redirect_refused' );
}
