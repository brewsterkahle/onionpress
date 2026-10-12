<?php
/**
 * Plugin Name: OnionPress Auto-Login
 * Description: One-time magic login links for the OnionPress menubar app, tray and launcher.
 * Version:     1.2
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * This container's default gateway from the kernel's IPv4 routing table
 * (hex in host byte order, hence pack('L')): the Docker host's address on
 * onionpress-network. '' when there is none. The same parser as
 * op_proxy_gateway() in src/onion-forward.php (tests/test_auto_login_off_argv.py
 * runs both on the same tables).
 */
function onionpress_auto_login_gateway() {
    $rows = @file( '/proc/net/route', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES );
    foreach ( $rows ?: array() as $row ) {
        // Iface Destination Gateway Flags ...; RTF_UP | RTF_GATEWAY = 3
        $f = preg_split( '/\s+/', trim( $row ) );
        if ( count( $f ) > 3 && '00000000' === $f[1] && '00000000' !== $f[2]
            && preg_match( '/^[0-9A-F]{8}$/Di', $f[2] )
            && preg_match( '/^[0-9A-F]{1,4}$/Di', $f[3] ) && ( hexdec( $f[3] ) & 3 ) === 3 ) {
            return inet_ntop( pack( 'L', hexdec( $f[2] ) ) );
        }
    }
    return '';
}

/**
 * Is this request one the token was minted for?
 *
 * "local": the Host is localhost and the request came through the published
 * port. The Host header is the client's to choose, so the second check is
 * the one that matters. Docker's published port hands the publisher's
 * browser to this container from the onionpress-network gateway, an address
 * no container on the network has: docker-proxy dials out from it (in
 * RootlessKit's namespace too, under rootless Docker on Linux), and without
 * the userland proxy NAT rewrites loopback clients to it. Onion visitors
 * arrive from the tor container, Cloudflare Tunnel visitors from cloudflared,
 * so a token that leaks (browser history, a pasted URL) cannot be redeemed
 * by either. This used to refuse the tor container's address only, which
 * let every other container in. No gateway found: loopback only.
 * "onion": the Host is a v3 .onion (the menubar's and tray's "open my site
 * in Tor Browser").
 */
function onionpress_auto_login_scope_ok( $scope ) {
    $host = isset( $_SERVER['HTTP_HOST'] ) ? strtolower( (string) $_SERVER['HTTP_HOST'] ) : '';
    $name = preg_replace( '/:\d+$/', '', $host );
    if ( 'onion' === $scope ) {
        return 1 === preg_match( '/^[a-z2-7]{56}\.onion$/D', $name );
    }
    if ( 'local' !== $scope || ! in_array( $name, array( 'localhost', '127.0.0.1', '[::1]' ), true ) ) {
        return false;
    }
    $remote = isset( $_SERVER['REMOTE_ADDR'] ) ? (string) $_SERVER['REMOTE_ADDR'] : '';
    if ( 0 === strpos( $remote, '::ffff:' ) ) {
        $remote = substr( $remote, 7 );
    }
    if ( '127.0.0.1' === $remote || '::1' === $remote ) {
        return true;
    }
    $gateway = onionpress_auto_login_gateway();
    return '' !== $gateway && $remote === $gateway;
}

/**
 * Handle ?op_login=TOKEN on any request.
 *
 * The menubar app, the Linux tray or `onionpress dashboard` stores a random
 * token as a network-wide transient (auto_login.MINT_PHP: 2-minute TTL, the
 * admin's user ID and a scope) and opens the site with ?op_login=TOKEN. This
 * plugin deletes the token (single use: only the request whose delete
 * removed it goes on), checks the request against the token's scope, logs
 * the admin in and redirects to the clean URL.
 */
add_action( 'init', function () {
    if ( empty( $_GET['op_login'] ) || ! is_string( $_GET['op_login'] ) ) {
        return;
    }

    $token = wp_unslash( $_GET['op_login'] );
    if ( ! preg_match( '/^[A-Za-z0-9_-]{32,64}$/D', $token ) ) {
        return;
    }
    $key    = 'op_login_' . $token;
    $stored = get_site_transient( $key );
    if ( false === $stored || ! delete_site_transient( $key ) ) {
        return;
    }

    if ( ! is_array( $stored ) || empty( $stored['uid'] ) || ! isset( $stored['scope'] )
        || ! onionpress_auto_login_scope_ok( $stored['scope'] ) ) {
        return;
    }

    $user = get_user_by( 'id', (int) $stored['uid'] );
    if ( ! $user ) {
        return;
    }

    // Set cookie domain to match the actual request host (.onion or localhost)
    // instead of the stored home_url, which may differ.
    $host          = isset( $_SERVER['HTTP_HOST'] ) ? $_SERVER['HTTP_HOST'] : '';
    $cookie_domain = preg_replace( '/:\d+$/', '', $host );

    wp_set_current_user( $user->ID );

    $expiration       = time() + YEAR_IN_SECONDS;
    $auth_cookie      = wp_generate_auth_cookie( $user->ID, $expiration, 'auth' );
    $logged_in_cookie = wp_generate_auth_cookie( $user->ID, $expiration, 'logged_in' );

    setcookie( AUTH_COOKIE,      $auth_cookie,      $expiration, PLUGINS_COOKIE_PATH, $cookie_domain, false, true );
    setcookie( AUTH_COOKIE,      $auth_cookie,      $expiration, ADMIN_COOKIE_PATH,   $cookie_domain, false, true );
    setcookie( LOGGED_IN_COOKIE, $logged_in_cookie, $expiration, COOKIEPATH,          $cookie_domain, false, true );
    setcookie( LOGGED_IN_COOKIE, $logged_in_cookie, $expiration, SITECOOKIEPATH,      $cookie_domain, false, true );

    // Response carries Set-Cookie headers — nocache_headers() stops any
    // intermediate from caching the cookies and serving them to the next
    // visitor.
    nocache_headers();

    // Land the caller wherever it asked (e.g. the static homepage, which
    // never runs PHP and so could never carry the token itself — the
    // menubar app instead points here, at wp-login.php, then redirects
    // back). Only a same-request relative path is accepted: WordPress's
    // core redirect validator checks the *configured* home_url host,
    // which — same as the cookie domain above — can differ from the
    // actual request host (.onion vs localhost), so it can't be used to
    // validate this. Sanitize BEFORE validating, not after: wp_redirect()
    // calls wp_sanitize_redirect() internally right before it sends the
    // Location header, and that strips characters outside a fixed
    // allowlist — validating the raw value first lets a stripped
    // character sitting between two slashes (e.g. "/\evil.com" or
    // "/%09/evil.com") collapse into "//evil.com" after sanitizing,
    // which browsers resolve as protocol-relative. The regex below
    // additionally rejects a leading backslash right after the slash,
    // since some browsers normalize "/\host" the same way.
    $redirect_to = remove_query_arg( 'op_login' );
    if ( ! empty( $_GET['redirect_to'] ) ) {
        $requested = wp_sanitize_redirect( wp_unslash( $_GET['redirect_to'] ) );
        if ( preg_match( '#^/($|[^/\\\\])#', $requested ) ) {
            $redirect_to = $requested;
        }
    }

    wp_redirect( $redirect_to );
    exit;
} );
