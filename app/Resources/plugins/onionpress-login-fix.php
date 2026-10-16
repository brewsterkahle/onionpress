<?php
/**
 * Plugin Name: OnionPress Login Fix
 * Description: Fixes cookie-domain issues when logging in via localhost or onionpress
 *              hostname, and replaces unhelpful external help links with inline text.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Extend login session to 1 year.
 *
 * OnionPress is a single-user personal site — short sessions just force
 * the admin to re-login constantly.  A long session keeps the admin bar
 * visible so the site owner always sees the logged-in experience.
 */
add_filter( 'auth_cookie_expiration', function () {
    return YEAR_IN_SECONDS;
} );

/**
 * Replace the "cookies blocked" error with helpful inline text.
 *
 * WordPress's test-cookie check can false-positive when the cookie domain
 * (the .onion address) differs from the hostname in the URL bar (localhost,
 * onionpress, etc.).  The default error links to wordpress.org which is
 * unreachable when offline.
 */
add_filter( 'login_errors', function ( $error ) {
    if ( stripos( $error, 'cookie' ) !== false ) {
        return '<strong>Tip:</strong> If you see a cookie error, try reloading '
             . 'this page and logging in again. Cookies work normally on this site.';
    }
    return $error;
} );

/**
 * Ensure the login redirect stays on the current hostname.
 *
 * After successful login, WordPress may redirect to the stored siteurl
 * (.onion address) even though the user logged in via localhost.  Rewrite
 * the redirect to match the current HTTP_HOST so the session continues
 * on the same hostname.
 */
add_filter( 'login_redirect', function ( $redirect_to ) {
    if ( ! isset( $_SERVER['HTTP_HOST'] ) ) {
        return $redirect_to;
    }

    $current_host = $_SERVER['HTTP_HOST'];

    // Only rewrite if the redirect points to a different host
    $parsed = wp_parse_url( $redirect_to );
    if ( isset( $parsed['host'] ) && $parsed['host'] !== $current_host ) {
        $redirect_to = preg_replace(
            '#//[^/]+#',
            '//' . $current_host,
            $redirect_to,
            1
        );
    }

    return $redirect_to;
}, 99 );

/**
 * Add a "Follow" item with purple onion icon to the admin bar.
 *
 * Placeholder for the Follow feature (issue #133) — no action yet.
 */
$onionpress_follow_icon = content_url( 'mu-plugins/onionpress-follow-icon.png' );

// CSS for the follow icon — must be top-level so it runs before admin_bar_menu
$onionpress_follow_css = '<style>
#wpadminbar #wp-admin-bar-onionpress-follow .ab-icon::before {
    content: "" !important;
    display: inline-block;
    background: url("' . $onionpress_follow_icon . '") no-repeat center !important;
    background-size: 20px 20px !important;
    width: 20px;
    height: 20px;
    top: 4px;
    font: normal 20px/1 dashicons;
}
@media screen and (max-width: 782px) {
    #wpadminbar #wp-admin-bar-onionpress-follow {
        display: block !important;
    }
    #wpadminbar #wp-admin-bar-onionpress-follow .ab-icon::before {
        background-size: 36px 36px !important;
        width: 52px;
        height: 46px !important;
        top: 0;
        line-height: 1.26;
        text-align: center;
    }
}
</style>';
add_action( 'wp_head', function () use ( $onionpress_follow_css ) {
    echo $onionpress_follow_css . "\n";
} );
add_action( 'admin_head', function () use ( $onionpress_follow_css ) {
    echo $onionpress_follow_css . "\n";
} );

// Only for those who can open the page it links to (onionpress-settings
// needs manage_options); authors and editors got "not allowed".
add_action( 'admin_bar_menu', function ( $wp_admin_bar ) {
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }
    $wp_admin_bar->add_node( [
        'id'     => 'onionpress-follow',
        'title'  => '<span class="ab-icon"></span><span class="ab-label">Follow</span>',
        'href'   => admin_url( 'admin.php?page=onionpress-settings' ),
    ] );
}, 100 );
