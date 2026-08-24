<?php
/**
 * Plugin Name: OnionPress Auto-Login
 * Description: One-time magic login links for the OnionPress menubar app.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Handle ?op_login=TOKEN on any front-end request.
 *
 * The menubar app generates a random token, stores it as a WordPress transient
 * (2-minute TTL), and opens the URL with ?op_login=TOKEN.  This plugin
 * validates the token, logs the admin in, deletes the token (single-use),
 * and redirects to the clean URL.
 */
add_action( 'init', function () {
    if ( empty( $_GET['op_login'] ) ) {
        return;
    }

    $token  = sanitize_text_field( $_GET['op_login'] );
    $stored = get_transient( 'op_login_' . $token );
    if ( $stored === false ) {
        return;
    }

    delete_transient( 'op_login_' . $token );

    $user = get_user_by( 'id', (int) $stored );
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
